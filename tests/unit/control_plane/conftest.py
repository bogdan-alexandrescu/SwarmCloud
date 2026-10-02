"""Shared fixtures: the real services, wired to an in-memory Firestore.

Nothing here patches a module or monkeypatches an import. Every collaborator is
injected through the same constructor the production entrypoint uses, so what
the tests exercise is the shipped code path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.config import Settings
from swarm_common.models import Tenant

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.settings import ApiSettings
from swarm_api.store import Store
from swarm_api.waker import NullWaker

from scheduler.dispatch import BackendRouter
from scheduler.loop import Scheduler
from scheduler.metrics import SchedulerMetrics
from scheduler.settings import SchedulerSettings
from scheduler.store import SchedulerStore

from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .fakes import FakeFirestore

PROJECT = "saga-agents-staging"
REGION = "us-central1"

ENG_GROUP = "eng@saga.xyz"
RESEARCH_GROUP = "research@saga.xyz"
ADMIN_GROUP = "swarm-admins@saga.xyz"


def core_settings(**overrides: Any) -> Settings:
    base = dict(
        project_id=PROJECT,
        region=REGION,
        environment="test",
        firestore_database="swarm",
        artifact_bucket=f"{PROJECT}-swarm-artifacts",
        allowed_domains=("saga.xyz",),
        max_batch_size=100,
        max_input_bytes=256 * 1024,
        max_workflow_steps=50,
        requests_per_second=20,
        default_tenant_max_active=20,
        default_tenant_capacity_units=40,
        prewarm_max_agents=10,
        prewarm_lead_seconds=120,
    )
    base.update(overrides)
    return Settings(**base)


def api_settings(**overrides: Any) -> ApiSettings:
    core = overrides.pop("core", None) or core_settings(**overrides.pop("core_overrides", {}))
    base = dict(
        core=core,
        tenant_groups=(ENG_GROUP, RESEARCH_GROUP),
        admin_groups=(ADMIN_GROUP,),
        group_cache_ttl_seconds=60,
        dispatch_topic="",
        max_page_size=200,
        default_page_size=50,
        rate_limit_burst=200,
    )
    base.update(overrides)
    return ApiSettings(**base)


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """Who is in which admin-registered group.

    `alice` is in eng, `bob` is in research, `carol` is in neither (so she gets
    a personal tenant), `root` is an admin.
    """
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def tokens(group_map) -> dict[str, dict[str, Any]]:
    return {
        f"token-{email.split('@')[0]}": {
            "email": email,
            "email_verified": True,
            "sub": f"sub-{email}",
            "hd": "saga.xyz",
        }
        for email in group_map
    }


@pytest.fixture
def objects() -> InMemoryObjectReader:
    """The artifact bucket, in memory.

    Injected for the same reason `db` is: the checkpoint and log routes must be
    exercisable with no credentials and no network. It is a real reader over a
    dict rather than a mock, so a test drives the shipped code path, and it can
    be told to FAIL on a prefix -- which is the only way to prove that a failed
    read is reported as a failed read rather than as an absence.
    """
    return InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}")


@pytest.fixture
def api_context(db, tokens, group_map, objects):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


def auth_header(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-{user}"}


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------

def seed_pool(db: FakeFirestore, name: str, *, hard_limit: int, active: int = 0,
              enabled: bool = True, quota_derived_limit: int | None = None,
              adaptive_target: int | None = None) -> None:
    db.docs[f"pools/{name}"] = {
        "name": name,
        "hard_limit": hard_limit,
        "adaptive_target": adaptive_target,
        "quota_derived_limit": quota_derived_limit,
        "active": active,
        "enabled": enabled,
        "updated_at": datetime.now(timezone.utc),
    }


def seed_tenant(db: FakeFirestore, tenant_id: str, *, credentials: tuple[str, ...] = (),
                max_active: int = 20, enabled: bool = True) -> Tenant:
    tenant = Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=f"{tenant_id}@saga.xyz",
        created_at=datetime.now(timezone.utc),
        display_name=tenant_id,
        max_active=max_active,
        capacity_units=max_active * 2,
        enabled=enabled,
        credentials=list(credentials),
        service_account=f"swarm-agent-worker-{tenant_id}@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{tenant_id}",
        namespace=f"swarm-tenant-{tenant_id}",
    )
    from dataclasses import asdict

    db.docs[f"tenants/{tenant_id}"] = asdict(tenant)
    seed_pool(db, f"tenant:{tenant_id}", hard_limit=max_active)
    return tenant


def seed_task(
    db: FakeFirestore,
    *,
    task_id: str,
    tenant_id: str,
    state: str = "READY",
    runner_profile: str = "mock",
    resource_class: str = "standard",
    priority: int = 0,
    created_at: datetime | None = None,
    provider: str | None = None,
    depends_on: tuple[str, ...] = (),
    park_reason: str | None = None,
    next_eligible_at: datetime | None = None,
    workflow_id: str | None = None,
    cancel_requested: bool = False,
) -> dict[str, Any]:
    moment = created_at or datetime.now(timezone.utc)
    doc = {
        "id": task_id,
        "tenant_id": tenant_id,
        "created_at": moment,
        "updated_at": moment,
        "state": state,
        "runner_profile": runner_profile,
        "resource_class": resource_class,
        "input": {},
        "submitted_by": f"seed@{tenant_id}",
        "provider": provider,
        "model": None,
        "priority": priority,
        "metadata": {},
        "repository_url": None,
        "repository_ref": None,
        "timeout_seconds": 600,
        "max_attempts": 3,
        "attempt_count": 0,
        "next_eligible_at": next_eligible_at,
        "park_reason": park_reason,
        "blocked_by": [],
        "current_lease_id": None,
        "current_generation": 0,
        "workflow_id": workflow_id,
        "step_id": None,
        "depends_on": list(depends_on),
        "cancel_requested": cancel_requested,
        "started_at": None,
        "completed_at": None,
        "last_error": None,
        "result_summary": None,
        "latest_checkpoint": None,
    }
    db.docs[f"tasks/{task_id}"] = doc
    return doc


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

class RecordingDispatcher:
    """Stands in for Cloud Run / GKE. Records what would have been started."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.fail_for = fail_for or set()

    def dispatch(self, *, task, lease, profile, tenant) -> str:
        if task.id in self.fail_for:
            from scheduler.dispatch import DispatchError

            raise DispatchError(f"injected dispatch failure for {task.id}")
        self.dispatched.append(
            {
                "task_id": task.id,
                "tenant_id": task.tenant_id,
                "lease_id": lease.lease_id,
                "generation": lease.generation,
                "profile": profile.name,
            }
        )
        return f"executions/{task.id}-{lease.generation}"


def scheduler_settings(**overrides: Any) -> SchedulerSettings:
    core = overrides.pop("core", None) or core_settings()
    base = dict(
        core=core,
        candidate_batch_size=200,
        max_leases_per_run=200,
        max_passes_per_run=25,
        max_run_seconds=45.0,
        aging_interval_seconds=60,
        aging_step=1,
        aging_max_bonus=50,
        dependency_sweep_size=200,
        enable_prewarm=True,
        project_id=PROJECT,
        region=REGION,
        artifact_registry_host=f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-images",
        worker_image_tag="test",
    )
    base.update(overrides)
    return SchedulerSettings(**base)


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def make_scheduler(db, dispatcher):
    def _make(*, settings: SchedulerSettings | None = None, now=None) -> Scheduler:
        settings = settings or scheduler_settings()
        router = BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings)
        kwargs: dict[str, Any] = {}
        if now is not None:
            kwargs["now"] = now
        return Scheduler(
            settings=settings,
            store=SchedulerStore(db),
            router=router,
            metrics=SchedulerMetrics(),
            **kwargs,
        )

    return _make


@pytest.fixture
def broker(db) -> QuotaBroker:
    return QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))


@pytest.fixture
def api_store(db) -> Store:
    return Store(db)


def minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)
