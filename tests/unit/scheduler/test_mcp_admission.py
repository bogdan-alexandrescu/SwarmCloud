"""A task naming a registered MCP server is asked about it before the lease (MCP-SCHED).

docs/design/registered-mcp-servers.md §4.10: in the scheduler, beside the
existing credential park and before any lease,

  * a named server whose registration is disabled, or no longer allows the
    profile, parks CREDENTIAL_MISSING with code `mcp_server_unavailable` and the
    server's name, and the credential sweep returns it to READY once the
    registration is enabled again;
  * a `dispatch` server whose credential row is destroyed or past `expires_at`
    is never leased: `mcp_credential_expired`, no lease, no retry.

and §4.6: the attach route is signed with the attempt key, so
`dispatch.worker_env` issues the key's registration nonce to a task whose
signed input names an MCP server. A task that names none reads nothing new,
records nothing new and is dispatched exactly as before: the feature ships
dark, and with `SWARM_MCP_SERVERS` off no task can name a server at all.

The documents are written here in the shapes §4.2 and §4.4 give, because the
lane that writes them (MCP-API) has not landed; the scheduler cannot import
swarm-api in any case.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    PROJECT,
    RecordingDispatcher,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

from scheduler import dispatch as dispatch_mod  # noqa: E402
from scheduler import loop as loop_mod  # noqa: E402
from scheduler import mcp_admission  # noqa: E402
from scheduler.credentials import credential_for  # noqa: E402
from scheduler.dispatch import BackendRouter, worker_env  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

TENANT = "eng"
OTHER = "research"
PROFILE = "claude-code"
#: `mock` with the `mcp_servers` input: a profile no git-tools or secret-slot
#: clause keys, so only the MCP clause can give its child a nonce.
UNKEYED = "mcp-unkeyed"


def _profiles() -> dict:
    out = dict(RUNNER_PROFILES)
    out[UNKEYED] = replace(
        RUNNER_PROFILES["mock"], name=UNKEYED,
        inputs={**RUNNER_PROFILES["mock"].inputs,
                "mcp_servers": RUNNER_PROFILES[PROFILE].inputs["mcp_servers"]},
    )
    return out


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    patched = MappingProxyType(_profiles())
    monkeypatch.setattr(loop_mod, "RUNNER_PROFILES", patched)
    monkeypatch.setattr(dispatch_mod, "RUNNER_PROFILES", patched)
    monkeypatch.setattr(mcp_admission, "RUNNER_PROFILES", patched)
    return patched


class CountingFirestore(FakeFirestore):
    """The control plane's fake, counting which collections were opened."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[str] = []

    def collection(self, path: str):
        self.opened.append(path)
        return super().collection(path)


@pytest.fixture
def db() -> CountingFirestore:
    return CountingFirestore()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def make_scheduler(db, dispatcher):
    def _make() -> Scheduler:
        settings = scheduler_settings()
        router = BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings)
        return Scheduler(
            settings=settings, store=SchedulerStore(db), router=router,
            metrics=SchedulerMetrics(),
        )

    return _make


def world(db) -> None:
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT, credentials=("anthropic",))
    seed_tenant(db, OTHER, credentials=("anthropic",))


def server_id(tenant_id: str, name: str) -> str:
    """§4.2: `mcp_` + the first 16 hex of sha256(tenant_id + "\\0" + name)."""
    return "mcp_" + hashlib.sha256(f"{tenant_id}\0{name}".encode()).hexdigest()[:16]


def register(db, name: str = "kb", *, tenant_id: str = TENANT, enabled: bool = True,
             mode: str = "dispatch", allowed_profiles: tuple[str, ...] = (),
             stored_tenant: str | None = None) -> str:
    sid = server_id(tenant_id, name)
    db.docs[f"mcp_servers/{sid}"] = {
        "server_id": sid, "tenant_id": stored_tenant or tenant_id, "name": name,
        "url": f"https://{name}.example.com/mcp", "transport": "streamable-http",
        "description": "the knowledge base",
        "auth": {"mode": mode, "header": "Authorization", "scheme": "Bearer"},
        "allowed_profiles": list(allowed_profiles), "allowed_tools": [],
        "enabled": enabled, "revision": 1,
    }
    return sid


def credential_row(db, owner_id: str, name: str = "kb", *, tenant_id: str = TENANT,
                   expires_in: timedelta = timedelta(hours=1), state: str = "live") -> str:
    sid = server_id(tenant_id, name)
    path = f"mcp_dispatch_credentials/{owner_id}~{sid}"
    db.docs[path] = {
        "owner_id": owner_id, "tenant_id": tenant_id, "server_id": sid, "name": name,
        "secret_id": f"swarm-tenant-{tenant_id}-mcpc-{secrets.token_hex(8)}",
        "version": "1", "expires_at": datetime.now(timezone.utc) + expires_in,
        "state": state,
    }
    return path


def task(db, task_id: str, *, servers: list[str] | None = ("kb",), profile: str = PROFILE,
         tenant_id: str = TENANT, **fields) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, runner_profile=profile, **fields)
    doc["input"] = {"prompt": "look it up"}
    if servers is not None:
        doc["input"]["mcp_servers"] = list(servers)
    return doc


def events(db, task_id: str, kind: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for path, doc in sorted(db.docs.items())
            if path.startswith(prefix) and doc.get("type") == kind]


def assert_reserved_nothing(db, dispatcher) -> None:
    # Invariant 1: no lease, no worker. Invariant 2: no pool moved, so no
    # half-made reservation. Invariant 3: nothing LEASED to count.
    assert [p for p in db.docs if p.startswith("leases/")] == []
    assert dispatcher.dispatched == []
    pools = {p: d["active"] for p, d in db.docs.items() if p.startswith("pools/")}
    assert pools and all(active == 0 for active in pools.values()), pools


# --------------------------------------------------------------------------
# A disabled registration parks before any lease, and is promoted when enabled
# --------------------------------------------------------------------------


def test_a_disabled_registration_parks_credential_missing_before_any_lease(
    db, make_scheduler, dispatcher
):
    world(db)
    register(db, enabled=False)
    credential_row(db, "task_disabled")
    task(db, "task_disabled")

    report = make_scheduler().drain()

    doc = db.docs["tasks/task_disabled"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
    assert report.parked == 1
    detail = events(db, "task_disabled", "parked")[-1]["detail"]
    assert detail["code"] == "mcp_server_unavailable"
    assert detail["mcp_servers"] == [
        {"name": "kb", "code": "mcp_server_unavailable", "why": "disabled"}
    ]
    # Names only: never a URL, a secret id or a server id.
    assert "mcpc" not in repr(detail) and "example.com" not in repr(detail)
    assert server_id(TENANT, "kb") not in repr(detail)
    assert_reserved_nothing(db, dispatcher)


def test_the_sweep_promotes_the_park_once_the_registration_is_enabled_again(
    db, make_scheduler, dispatcher
):
    world(db)
    register(db, enabled=False)
    credential_row(db, "task_waits")
    task(db, "task_waits")
    make_scheduler().drain()
    assert db.docs["tasks/task_waits"]["state"] == "PARKED"

    report = make_scheduler().drain()
    assert report.promoted_credentials == 0
    assert db.docs["tasks/task_waits"]["state"] == "PARKED", "still disabled"

    register(db, enabled=True)
    report = make_scheduler().drain()
    assert report.promoted_credentials == 1
    promoted = events(db, "task_waits", "ready")
    assert promoted and promoted[-1]["detail"]["mcp_servers"] == "available"
    assert db.docs["tasks/task_waits"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_waits"]


@pytest.mark.parametrize(
    "why, setup",
    [
        ("unregistered", lambda db: None),
        ("unregistered", lambda db: register(db, stored_tenant=OTHER)),
        ("profile_refused", lambda db: register(db, allowed_profiles=("codex",))),
    ],
    ids=["never-registered", "another-tenants-document", "profile-not-allowed"],
)
def test_a_server_the_task_may_not_use_parks_too(db, make_scheduler, dispatcher, why, setup):
    world(db)
    setup(db)
    credential_row(db, "task_refused")
    task(db, "task_refused")

    make_scheduler().drain()

    assert db.docs["tasks/task_refused"]["park_reason"] == "CREDENTIAL_MISSING"
    detail = events(db, "task_refused", "parked")[-1]["detail"]
    assert detail["mcp_servers"] == [{"name": "kb", "code": "mcp_server_unavailable", "why": why}]
    assert_reserved_nothing(db, dispatcher)


def test_an_enabled_registration_with_a_live_credential_admits(db, make_scheduler, dispatcher):
    world(db)
    register(db, allowed_profiles=(PROFILE,))
    credential_row(db, "task_ok")
    task(db, "task_ok")

    make_scheduler().drain()

    assert db.docs["tasks/task_ok"]["state"] == "DISPATCHED"


@pytest.mark.parametrize("mode", ["none", "workspace_secret"])
def test_a_server_that_takes_no_dispatch_credential_reads_no_row(
    db, make_scheduler, dispatcher, mode
):
    world(db)
    register(db, mode=mode)
    task(db, "task_no_row")

    make_scheduler().drain()

    assert db.docs["tasks/task_no_row"]["state"] == "DISPATCHED"
    assert "mcp_dispatch_credentials" not in db.opened


def test_a_workflow_steps_credential_is_the_workflows_row(db, make_scheduler, dispatcher):
    """§4.3: a workflow's one `mcp_credentials` map serves every step."""
    world(db)
    register(db)
    credential_row(db, "wf_1")
    task(db, "task_step", workflow_id="wf_1")

    make_scheduler().drain()

    assert db.docs["tasks/task_step"]["state"] == "DISPATCHED"


def test_a_child_uses_its_parents_row(db, make_scheduler, dispatcher):
    """§4.3, Q7: a child uses the parent's stored credential row."""
    world(db)
    register(db)
    credential_row(db, "task_parent")
    task(db, "task_parent", state="RUNNING")
    child = task(db, "task_child")
    child["parent_task_id"] = "task_parent"

    make_scheduler().drain()

    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"


def test_a_child_uses_its_parents_workflow_row(db, make_scheduler, dispatcher):
    world(db)
    register(db)
    credential_row(db, "wf_parent")
    task(db, "task_parent", state="RUNNING", workflow_id="wf_parent")
    child = task(db, "task_child")
    child["parent_task_id"] = "task_parent"

    make_scheduler().drain()

    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"


def test_another_tenants_row_is_no_credential(db, make_scheduler, dispatcher):
    world(db)
    register(db)
    path = credential_row(db, "task_foreign")
    db.docs[path]["tenant_id"] = OTHER
    task(db, "task_foreign")

    make_scheduler().drain()

    assert db.docs["tasks/task_foreign"]["state"] != "DISPATCHED"
    assert_reserved_nothing(db, dispatcher)


# --------------------------------------------------------------------------
# An expired credential: no lease, and no promotion ever
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "why, row",
    [
        ("expired", {"expires_in": -timedelta(seconds=1)}),
        ("destroyed", {"state": "destroyed"}),
        ("missing", None),
    ],
)
def test_an_expired_credential_is_never_leased(db, make_scheduler, dispatcher, why, row):
    """§4.10 and the brief say FAILED with no lease. The frozen state machine
    has no QUEUED/READY -> FAILED edge and admission's park is `loop.py`'s,
    outside this lane, so what holds today is pinned exactly: PARKED
    CREDENTIAL_MISSING with the terminal code, no lease, and the same state
    after every later drain. Which terminal state it should end in
    (DEAD_LETTERED via `loop.py`, or FAILED via a contract change) is the
    owner's question in the MCP-SCHED pull request; this test changes with
    that answer."""
    world(db)
    register(db)
    if row is not None:
        credential_row(db, "task_expired", **row)
    task(db, "task_expired")

    report = make_scheduler().drain()

    doc = db.docs["tasks/task_expired"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
    assert report.parked == 1
    assert_reserved_nothing(db, dispatcher)
    detail = events(db, "task_expired", "parked")[-1]["detail"]
    code = "mcp_credential_missing" if why == "missing" else "mcp_credential_expired"
    assert detail["code"] == code
    assert detail["mcp_servers"] == [{"name": "kb", "code": code, "why": why}]

    # Nobody can hand a queued task a new value, so the sweep never promotes it.
    register(db, enabled=True)
    report = make_scheduler().drain()
    assert report.promoted_credentials == 0
    doc = db.docs["tasks/task_expired"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
    assert_reserved_nothing(db, dispatcher)


def test_the_expired_answer_is_terminal_and_the_disabled_one_is_not(db):
    """The answer says which of the two §4.10 outcomes it is: a disabled
    registration waits to be enabled (`terminal` False), an expired
    credential can never become runnable (`terminal` True, FAILED in §4.10)."""
    seed_tenant(db, TENANT, credentials=("anthropic",))
    tenant = _tenant()
    profile = RUNNER_PROFILES[PROFILE]
    grants = loop_mod.WorkspaceGrants(db)
    pool = loop_mod.AccountPool.unwired(scheduler_settings())
    forge = loop_mod.ForgeConnections(db)

    register(db, enabled=False)
    credential_row(db, "task_a")
    answer = credential_for(profile, tenant, pool, task=_task("task_a"), forge=forge,
                            secrets=grants)
    assert not answer.runnable and not answer.terminal

    register(db, enabled=True)
    credential_row(db, "task_a", expires_in=-timedelta(minutes=1))
    answer = credential_for(profile, tenant, pool, task=_task("task_a"), forge=forge,
                            secrets=grants)
    assert not answer.runnable and answer.terminal

    # One server terminal among several makes the whole answer terminal.
    register(db, "docs", enabled=False, mode="none")
    answer = credential_for(profile, tenant, pool, task=_task("task_a", ["docs", "kb"]),
                            forge=forge, secrets=grants)
    assert answer.terminal
    assert answer.park_detail()["code"] == "mcp_credential_expired"
    assert [m["name"] for m in answer.park_detail()["mcp_servers"]] == ["docs", "kb"]


def test_a_credential_expiring_is_read_against_the_clock_it_is_given(db):
    register(db)
    credential_row(db, "task_clock", expires_in=timedelta(minutes=10))
    regs = mcp_admission.McpRegistrations(db)
    t = _task("task_clock")
    now = datetime.now(timezone.utc)
    assert regs.answer(t, RUNNER_PROFILES[PROFILE], now=now).runnable
    later = regs.answer(t, RUNNER_PROFILES[PROFILE], now=now + timedelta(minutes=11))
    assert later.terminal and [r.why for r in later.refusals] == ["expired"]


# --------------------------------------------------------------------------
# A task without a server is unchanged
# --------------------------------------------------------------------------


def test_a_task_without_a_server_reads_nothing_new_and_records_nothing_new(
    db, make_scheduler, dispatcher
):
    world(db)
    task(db, "task_plain", servers=None)

    make_scheduler().drain()

    assert db.docs["tasks/task_plain"]["state"] == "DISPATCHED"
    assert not {"mcp_servers", "mcp_dispatch_credentials"} & set(db.opened)


def test_a_task_without_a_server_parks_and_promotes_with_the_same_detail(db):
    """A provider-key park's detail and its promotion's are what they were."""
    seed_tenant(db, TENANT)
    tenant = Tenant(tenant_id=TENANT, kind="group", principal="eng@saga.xyz",
                    created_at=datetime.now(timezone.utc))
    profile = RUNNER_PROFILES[PROFILE]
    pool = loop_mod.AccountPool.unwired(scheduler_settings())
    answer = credential_for(profile, tenant, pool, task=_task("task_plain", servers=None),
                            forge=loop_mod.ForgeConnections(db),
                            secrets=loop_mod.WorkspaceGrants(db))
    assert answer.park_detail() == {"provider": "anthropic",
                                    "account_pool": "no_broker_configured"}
    assert not answer.terminal

    keyed = _tenant()
    answer = credential_for(profile, keyed, pool, task=_task("task_plain", servers=None),
                            forge=loop_mod.ForgeConnections(db),
                            secrets=loop_mod.WorkspaceGrants(db))
    assert answer.promote_detail() == {"reason": "credential_available",
                                       "provider": "anthropic", "credential": "tenant_key",
                                       "forge_connection": "no_user_slot"}


def test_a_profile_that_does_not_declare_the_input_is_never_asked(db, make_scheduler, dispatcher):
    """Only the declared input names a server (contract request 88): a stray
    key on a profile without it is the worker's spec check's to refuse."""
    world(db)
    task(db, "task_mock", profile="mock")

    make_scheduler().drain()

    assert db.docs["tasks/task_mock"]["state"] == "DISPATCHED"
    assert "mcp_servers" not in db.opened


# --------------------------------------------------------------------------
# The attempt-key nonce
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _task(task_id: str = "task_k", servers: list[str] | None = ("kb",), *,
          profile: str = PROFILE, parent: str | None = None) -> Task:
    inputs: dict[str, Any] = {"prompt": "look it up"}
    if servers is not None:
        inputs["mcp_servers"] = list(servers)
    return Task(
        id=task_id, tenant_id=TENANT, created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=profile,
        resource_class=RUNNER_PROFILES["mock"].resource_class,
        input=inputs, submitted_by="alice@saga.xyz", parent_task_id=parent,
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz", created_at=NOW,
        credentials=("anthropic",),
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{TENANT}",
    )


def _env(task_: Task) -> dict[str, str]:
    lease = Lease(
        lease_id="lease_k_1", task_id=task_.id, attempt_id="att_k_1", tenant_id=TENANT,
        generation=1, pools=["global"], units=1, state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )
    settings = scheduler_settings(child_key=secrets.token_hex(32),
                                  swarm_api_url="https://api.example")
    return worker_env(task=task_, lease=lease, tenant=_tenant(), settings=settings)


def test_a_child_naming_a_server_gets_the_attempt_key_nonce():
    """The attach is signed with the attempt key (§4.6), so a task whose
    signed input names a server needs one even when nothing else keys it."""
    env = _env(_task(profile=UNKEYED, parent="task_parent"))
    assert env.get("SWARM_CHILD_NONCE"), "a child naming a server has no key to attach with"
    assert env["SWARM_API_URL"] == "https://api.example"


def test_a_child_without_a_server_is_unchanged():
    """The control: the same child naming no server still gets no nonce."""
    for servers in (None, []):
        env = _env(_task(servers=servers, profile=UNKEYED, parent="task_parent"))
        assert "SWARM_CHILD_NONCE" not in env and "SWARM_API_URL" not in env


def test_naming_a_server_is_read_from_the_declared_input_only():
    assert mcp_admission.names_mcp_servers(UNKEYED, {"mcp_servers": ["kb"]})
    assert mcp_admission.names_mcp_servers(PROFILE, {"mcp_servers": ["kb"]})
    assert not mcp_admission.names_mcp_servers(UNKEYED, {"mcp_servers": []})
    assert not mcp_admission.names_mcp_servers(UNKEYED, {})
    assert not mcp_admission.names_mcp_servers(UNKEYED, None)
    assert not mcp_admission.names_mcp_servers(UNKEYED, {"mcp_servers": "kb"})
    # `mock` does not declare the input, so a stray key names nothing.
    assert not mcp_admission.names_mcp_servers("mock", {"mcp_servers": ["kb"]})
    assert not mcp_admission.names_mcp_servers("no-such-profile", {"mcp_servers": ["kb"]})


def test_a_top_level_task_is_keyed_with_or_without_a_server():
    """Unchanged: a task that is not a child was keyed before this lane."""
    assert _env(_task(profile=UNKEYED)).get("SWARM_CHILD_NONCE")
    assert _env(_task(servers=None, profile=UNKEYED)).get("SWARM_CHILD_NONCE")


def test_the_server_id_recipe_is_the_designs():
    assert mcp_admission.server_id_for(TENANT, "kb") == server_id(TENANT, "kb")
    assert mcp_admission.server_id_for(OTHER, "kb") != server_id(TENANT, "kb")
