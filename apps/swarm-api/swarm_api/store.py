"""Firestore access for the API. Every read path is tenant-scoped.

The tenant boundary is enforced HERE and not in the routes, because a route is
easy to add and easy to forget. `get_task` takes a tenant_id and treats another
tenant's document as absent -- not as a 403, which would confirm the id exists.
Every list method starts from an equality filter on `tenant_id`.

Composite indexes this module relies on (owned by the terraform track):

    tasks:      tenant_id ASC, state ASC, created_at DESC
    tasks:      tenant_id ASC, created_at DESC
    tasks:      tenant_id ASC, workflow_id ASC, created_at DESC
    workflows:  tenant_id ASC, created_at DESC

Pagination uses an inequality on `created_at` rather than a Firestore cursor
token so a page token stays a plain, opaque timestamp the caller can hold across
processes. Two tasks created in the same microsecond would collapse a page
boundary; ids are generated with microsecond-resolution timestamps and random
suffixes, so that is a theoretical rather than an operational concern, and the
`id` tiebreak below makes it deterministic anyway.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import (
    ProviderState,
    QuotaState,
    SlotPool,
    Task,
    TaskEvent,
    Tenant,
    Workflow,
    new_id,
    utcnow,
)
from swarm_common.states import EventType, TaskState, assert_transition

from .codec import (
    event_from_dict,
    event_to_firestore,
    pool_from_dict,
    quota_from_dict,
    task_from_dict,
    task_to_firestore,
    tenant_from_dict,
    tenant_to_firestore,
    workflow_from_dict,
    workflow_to_firestore,
)
from .errors import Conflict, NotFound

log = logging.getLogger(__name__)

TASKS = "tasks"
WORKFLOWS = "workflows"
TENANTS = "tenants"
POOLS = "pools"
QUOTA = "quota"
LEASES = "leases"
CONTROL = "control"
EVENTS = "events"
ARTIFACTS = "artifacts"

CONTROL_DOC = "dispatch"

#: Firestore caps a write batch at 500 operations. Each task costs two (the
#: document and its `submitted` event), so a chunk of 200 tasks is the ceiling.
_BATCH_CHUNK = 200

#: `evaluate_capacity` treats a MISSING pool as unlimited, but a Firestore
#: document has no "absent integer" -- so a pool created only to carry an
#: enabled/disabled flag (a drain, say) needs a hard limit that will never bind.
#: Writing 0 there instead would turn "drain this resource class" into "cap it at
#: zero forever", and undraining would silently leave it blocked.
UNLIMITED_HARD_LIMIT = 1_000_000


def encode_cursor(moment: datetime) -> str:
    return base64.urlsafe_b64encode(moment.isoformat().encode("utf-8")).decode("ascii")


def decode_cursor(token: str | None) -> datetime | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parsed = datetime.fromisoformat(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise NotFound("invalid page_token") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Page:
    items: list[Any]
    next_page_token: str | None


class Store:
    def __init__(self, db: Any, *, now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._now = now

    @property
    def db(self) -> Any:
        return self._db

    # -- helpers ----------------------------------------------------------

    def _count(self, query: Any) -> int:
        result = query.count().get()
        for row in result:
            for item in row:
                return int(item.value)
        return 0

    def _chunks(self, items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
        for start in range(0, len(items), size):
            yield items[start : start + size]

    # -- tenants ----------------------------------------------------------

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        if not snap.exists:
            return None
        return tenant_from_dict(snap.to_dict())

    def list_tenants(self, limit: int = 200) -> list[Tenant]:
        query = self._db.collection(TENANTS).order_by("tenant_id").limit(limit)
        return [tenant_from_dict(snap.to_dict()) for snap in query.stream()]

    def ensure_tenant(
        self,
        tenant_id: str,
        *,
        principal: str,
        default_max_active: int,
        default_capacity_units: int,
        project_id: str,
        artifact_bucket: str,
    ) -> Tenant:
        """Read the tenant, creating it on first sight.

        Invariant 9 is set up here: the tenant gets its own service account, its
        own GCS prefix and its own namespace at creation, so nothing downstream
        has to invent them and accidentally share one.
        """
        existing = self.get_tenant(tenant_id)
        if existing is not None:
            return existing
        kind = "user" if tenant_id.startswith("u-") else "group"
        tenant = Tenant(
            tenant_id=tenant_id,
            kind=kind,
            principal=principal,
            created_at=self._now(),
            display_name=tenant_id,
            max_active=default_max_active,
            capacity_units=default_capacity_units,
            service_account=f"swarm-t-{tenant_id}@{project_id}.iam.gserviceaccount.com",
            gcs_prefix=f"gs://{artifact_bucket}/tenants/{tenant_id}",
            namespace=f"swarm-{tenant_id}",
        )
        self._db.collection(TENANTS).document(tenant_id).set(tenant_to_firestore(tenant))
        # A tenant with no pool document is unlimited by construction in
        # `evaluate_capacity`, so the pool is created WITH the tenant rather
        # than lazily on first admission.
        self.upsert_pool(f"tenant:{tenant_id}", hard_limit=tenant.max_active)
        return tenant

    def set_tenant_limits(
        self,
        tenant_id: str,
        *,
        max_active: int | None = None,
        capacity_units: int | None = None,
        monthly_budget_usd: float | None = None,
        enabled: bool | None = None,
    ) -> Tenant:
        ref = self._db.collection(TENANTS).document(tenant_id)
        snap = ref.get()
        if not snap.exists:
            raise NotFound(f"tenant {tenant_id!r} does not exist")
        patch: dict[str, Any] = {}
        if max_active is not None:
            patch["max_active"] = int(max_active)
        if capacity_units is not None:
            patch["capacity_units"] = int(capacity_units)
        if monthly_budget_usd is not None:
            patch["monthly_budget_usd"] = float(monthly_budget_usd)
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if patch:
            ref.update(patch)
        if max_active is not None:
            # The tenant pool is what the scheduler actually enforces, so the
            # document and the pool must move together or the limit is a lie.
            self.upsert_pool(f"tenant:{tenant_id}", hard_limit=int(max_active))
        return tenant_from_dict(ref.get().to_dict())

    def register_credential(self, tenant_id: str, provider: str) -> Tenant:
        """Record that the tenant has a key for `provider`. Never the key itself."""
        ref = self._db.collection(TENANTS).document(tenant_id)
        snap = ref.get()
        if not snap.exists:
            raise NotFound(f"tenant {tenant_id!r} does not exist")
        tenant = tenant_from_dict(snap.to_dict())
        providers = sorted(set(tenant.credentials) | {provider})
        ref.update({"credentials": providers})
        tenant.credentials = providers
        return tenant

    # -- tasks ------------------------------------------------------------

    def create_tasks(self, tasks: Sequence[Task]) -> list[Task]:
        """Write tasks and their `submitted` events.

        Batched so a partially written submission cannot leave a task document
        with no event trail. Ordering within the batch matters for workflows:
        callers pass tasks in topological order so a child is never written
        before its parent.
        """
        now = self._now()
        for chunk in self._chunks(list(tasks), _BATCH_CHUNK):
            batch = self._db.batch()
            for task in chunk:
                ref = self._db.collection(TASKS).document(task.id)
                batch.set(ref, task_to_firestore(task))
                event = TaskEvent(
                    event_id=new_id("ev"),
                    task_id=task.id,
                    tenant_id=task.tenant_id,
                    type=EventType.SUBMITTED,
                    at=now,
                    detail={
                        "runner_profile": task.runner_profile,
                        "resource_class": task.resource_class,
                        "state": task.state.value,
                        "workflow_id": task.workflow_id,
                    },
                )
                batch.set(ref.collection(EVENTS).document(event.event_id),
                          event_to_firestore(event))
            batch.commit()
        return list(tasks)

    def get_task(self, tenant_id: str, task_id: str) -> Task:
        snap = self._db.collection(TASKS).document(task_id).get()
        if not snap.exists:
            raise NotFound(f"task {task_id!r} not found")
        data = snap.to_dict()
        if data.get("tenant_id") != tenant_id:
            # Deliberately the SAME error as a genuinely missing task: a
            # different status here would confirm the id exists in another
            # tenant, which is an enumeration oracle.
            raise NotFound(f"task {task_id!r} not found")
        return task_from_dict(data)

    def list_tasks(
        self,
        tenant_id: str,
        *,
        state: TaskState | None = None,
        workflow_id: str | None = None,
        runner_profile: str | None = None,
        limit: int = 50,
        page_token: str | None = None,
    ) -> Page:
        query = self._db.collection(TASKS).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        )
        if state is not None:
            query = query.where(filter=FieldFilter("state", "==", state.value))
        if workflow_id is not None:
            query = query.where(filter=FieldFilter("workflow_id", "==", workflow_id))
        if runner_profile is not None:
            query = query.where(filter=FieldFilter("runner_profile", "==", runner_profile))
        before = decode_cursor(page_token)
        if before is not None:
            query = query.where(filter=FieldFilter("created_at", "<", before))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit + 1)

        rows = [task_from_dict(snap.to_dict()) for snap in query.stream()]
        rows.sort(key=lambda t: (t.created_at, t.id), reverse=True)
        next_token = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_token = encode_cursor(rows[-1].created_at)
        return Page(items=rows, next_page_token=next_token)

    def request_cancel(self, tenant_id: str, task_id: str, *, by: str) -> Task:
        """Flag the task for cancellation, terminating it immediately if idle.

        A task that holds no capacity (SUBMITTED / QUEUED / READY / PARKED) goes
        straight to CANCELLED. A task that does hold capacity keeps it until the
        worker or the reconciler releases the lease, because releasing it from
        here would decrement a pool that the running container still occupies.
        """
        ref = self._db.collection(TASKS).document(task_id)
        snap = ref.get()
        if not snap.exists or snap.to_dict().get("tenant_id") != tenant_id:
            raise NotFound(f"task {task_id!r} not found")
        task = task_from_dict(snap.to_dict())
        now = self._now()
        if task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
                          TaskState.DEAD_LETTERED}:
            raise Conflict(
                f"task {task_id!r} is already terminal ({task.state.value})",
                detail={"state": task.state.value},
            )

        patch: dict[str, Any] = {"cancel_requested": True, "updated_at": now}
        if task.state in {TaskState.SUBMITTED, TaskState.QUEUED, TaskState.READY,
                          TaskState.PARKED}:
            assert_transition(task.state, TaskState.CANCELLED)
            patch["state"] = TaskState.CANCELLED.value
            patch["completed_at"] = now
            patch["park_reason"] = None
            patch["blocked_by"] = []
        ref.update(patch)
        immediate = "state" in patch
        self.append_event(
            task_id=task_id,
            tenant_id=tenant_id,
            type=EventType.CANCELLED,
            detail={
                "requested_by": by,
                "from_state": task.state.value,
                # "cancel_requested" means the flag is set but the task still
                # holds capacity: the worker or reconciler releases the lease.
                "phase": "cancelled" if immediate else "cancel_requested",
            },
        )
        return task_from_dict(ref.get().to_dict())

    def append_event(
        self,
        *,
        task_id: str,
        tenant_id: str,
        type: EventType,
        detail: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        lease_id: str | None = None,
        generation: int | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=new_id("ev"),
            task_id=task_id,
            tenant_id=tenant_id,
            type=type,
            at=self._now(),
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            detail=dict(detail or {}),
        )
        (
            self._db.collection(TASKS)
            .document(task_id)
            .collection(EVENTS)
            .document(event.event_id)
            .set(event_to_firestore(event))
        )
        return event

    def list_events(self, tenant_id: str, task_id: str, *, limit: int = 200) -> list[TaskEvent]:
        self.get_task(tenant_id, task_id)         # tenant check before any read
        query = (
            self._db.collection(TASKS)
            .document(task_id)
            .collection(EVENTS)
            .order_by("at", direction=firestore.Query.ASCENDING)
            .limit(limit)
        )
        return [event_from_dict(snap.to_dict()) for snap in query.stream()]

    def list_artifacts(self, tenant_id: str, task_id: str, *, limit: int = 200) -> list[dict]:
        """Artifact METADATA only.

        Artifacts live in the tenant's own GCS prefix and are passed by
        reference; nothing about them is inlined through Firestore, and this
        endpoint never mints a download URL -- the caller reads GCS with their
        own credentials, which keeps the tenant boundary in one place.
        """
        self.get_task(tenant_id, task_id)
        query = (
            self._db.collection(TASKS)
            .document(task_id)
            .collection(ARTIFACTS)
            .order_by("created_at", direction=firestore.Query.ASCENDING)
            .limit(limit)
        )
        out = []
        for snap in query.stream():
            data = dict(snap.to_dict())
            data.setdefault("name", snap.id)
            out.append(data)
        return out

    def count_tasks_by_state(self, tenant_id: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for state in TaskState:
            query = self._db.collection(TASKS).where(
                filter=FieldFilter("state", "==", state.value)
            )
            if tenant_id is not None:
                query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            counts[state.value] = self._count(query)
        return counts

    # -- workflows --------------------------------------------------------

    def create_workflow(self, workflow: Workflow, tasks: Sequence[Task]) -> Workflow:
        self.create_tasks(tasks)
        (
            self._db.collection(WORKFLOWS)
            .document(workflow.workflow_id)
            .set(workflow_to_firestore(workflow))
        )
        return workflow

    def get_workflow(self, tenant_id: str, workflow_id: str) -> Workflow:
        snap = self._db.collection(WORKFLOWS).document(workflow_id).get()
        if not snap.exists:
            raise NotFound(f"workflow {workflow_id!r} not found")
        data = snap.to_dict()
        if data.get("tenant_id") != tenant_id:
            raise NotFound(f"workflow {workflow_id!r} not found")
        return workflow_from_dict(data)

    def list_workflows(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        page_token: str | None = None,
    ) -> Page:
        query = self._db.collection(WORKFLOWS).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        )
        before = decode_cursor(page_token)
        if before is not None:
            query = query.where(filter=FieldFilter("created_at", "<", before))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit + 1)
        rows = [workflow_from_dict(snap.to_dict()) for snap in query.stream()]
        rows.sort(key=lambda w: (w.created_at, w.workflow_id), reverse=True)
        next_token = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_token = encode_cursor(rows[-1].created_at)
        return Page(items=rows, next_page_token=next_token)

    def cancel_workflow(self, tenant_id: str, workflow_id: str, *, by: str) -> dict[str, Any]:
        workflow = self.get_workflow(tenant_id, workflow_id)
        (
            self._db.collection(WORKFLOWS)
            .document(workflow_id)
            .update({"cancel_requested": True, "updated_at": self._now()})
        )
        cancelled, already_terminal = [], []
        for step in workflow.steps:
            if not step.task_id:
                continue
            try:
                self.request_cancel(tenant_id, step.task_id, by=by)
                cancelled.append(step.task_id)
            except Conflict:
                already_terminal.append(step.task_id)
            except NotFound:
                already_terminal.append(step.task_id)
        return {
            "workflow_id": workflow_id,
            "cancel_requested": True,
            "tasks_cancelled": cancelled,
            "tasks_already_terminal": already_terminal,
        }

    # -- pools, quota, control -------------------------------------------

    def list_pools(self, limit: int = 500) -> list[SlotPool]:
        query = self._db.collection(POOLS).limit(limit)
        return [pool_from_dict(snap.id, snap.to_dict()) for snap in query.stream()]

    def get_pool(self, name: str) -> SlotPool | None:
        snap = self._db.collection(POOLS).document(name).get()
        if not snap.exists:
            return None
        return pool_from_dict(name, snap.to_dict())

    def upsert_pool(
        self,
        name: str,
        *,
        hard_limit: int | None = None,
        enabled: bool | None = None,
        adaptive_target: int | None = None,
        quota_derived_limit: int | None = None,
    ) -> SlotPool:
        ref = self._db.collection(POOLS).document(name)
        snap = ref.get()
        now = self._now()
        if not snap.exists:
            pool = SlotPool(
                name=name,
                hard_limit=int(
                    hard_limit if hard_limit is not None else UNLIMITED_HARD_LIMIT
                ),
                adaptive_target=adaptive_target,
                quota_derived_limit=quota_derived_limit,
                active=0,
                enabled=True if enabled is None else bool(enabled),
                updated_at=now,
            )
            ref.set(
                {
                    "name": pool.name,
                    "hard_limit": pool.hard_limit,
                    "adaptive_target": pool.adaptive_target,
                    "quota_derived_limit": pool.quota_derived_limit,
                    "active": 0,
                    "enabled": pool.enabled,
                    "updated_at": now,
                }
            )
            return pool
        patch: dict[str, Any] = {"updated_at": now}
        if hard_limit is not None:
            patch["hard_limit"] = int(hard_limit)
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if adaptive_target is not None:
            patch["adaptive_target"] = int(adaptive_target)
        if quota_derived_limit is not None:
            patch["quota_derived_limit"] = int(quota_derived_limit)
        ref.update(patch)
        return pool_from_dict(name, ref.get().to_dict())

    def list_quota(self, tenant_id: str | None = None, limit: int = 500) -> list[QuotaState]:
        query: Any = self._db.collection(QUOTA)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        query = query.limit(limit)
        return [quota_from_dict(snap.to_dict()) for snap in query.stream()]

    def set_provider_enabled(self, provider: str, enabled: bool) -> None:
        """Enable/disable a provider platform-wide.

        Flips the provider pool and every per-tenant provider pool, because a
        provider-wide disable that left the per-tenant pools open would still
        admit work.
        """
        self.upsert_pool(f"provider:{provider}", enabled=enabled)
        prefix = f"provider:{provider}:tenant:"
        for pool in self.list_pools():
            if pool.name.startswith(prefix):
                self.upsert_pool(pool.name, enabled=enabled)

    def set_provider_quota_state(self, provider: str, state: ProviderState) -> int:
        """Force every (provider, tenant) quota document to one state.

        Used by the admin disable path so the broker's AIMD loop cannot raise
        the adaptive target back up underneath an operator's drain.
        """
        query = self._db.collection(QUOTA).where(
            filter=FieldFilter("provider", "==", provider)
        )
        touched = 0
        now = self._now()
        for snap in query.stream():
            patch: dict[str, Any] = {"state": state.value, "updated_at": now}
            if state is ProviderState.DISABLED:
                patch["quota_derived_limit"] = 0
            else:
                # None means "no quota-derived cap", which is what re-enabling
                # has to mean: the hard max takes over again.
                patch["quota_derived_limit"] = None
                patch["cooldown_until"] = None
            self._db.collection(QUOTA).document(snap.id).update(patch)
            touched += 1
        return touched

    def get_control(self) -> dict[str, Any]:
        snap = self._db.collection(CONTROL).document(CONTROL_DOC).get()
        if not snap.exists:
            return {"dispatch_paused": False, "updated_at": None, "updated_by": None,
                    "reason": None}
        data = dict(snap.to_dict())
        data.setdefault("dispatch_paused", False)
        return data

    def set_dispatch_paused(self, paused: bool, *, by: str, reason: str | None = None) -> dict:
        payload = {
            "dispatch_paused": bool(paused),
            "updated_at": self._now(),
            "updated_by": by,
            "reason": reason,
        }
        self._db.collection(CONTROL).document(CONTROL_DOC).set(payload)
        return payload
