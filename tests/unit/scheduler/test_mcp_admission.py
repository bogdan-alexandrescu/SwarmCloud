"""A task that names registered MCP servers is judged before any lease (MCP-SCHED, #1433).

docs/design/registered-mcp-servers.md §4.10 and §4.6 ("The attempt key must
exist"). Pinned here, against the production `Scheduler` and `SchedulerStore`
over the in-memory Firestore the control-plane suite uses:

  S-1  A named server whose registration is disabled, or no longer allows the
       task's profile, parks CREDENTIAL_MISSING (code `mcp_server_unavailable`)
       before any lease: nothing reserved, nothing dispatched.
  S-2  The credential sweep returns that park to READY once the registration
       is enabled again, and admission then dispatches it.
  S-3  A `dispatch` server whose credential row is destroyed, past its
       `expires_at` or missing is final: `credential_for` names the failure
       `mcp_credential_expired` and takes no lease, and the sweep never
       promotes it.
  S-4  A task naming a server gets the attempt-key nonce, on the MCP rule
       alone; a task naming none gets exactly the environment it got before.
  S-5  A task that names no server reads no MCP document, and is admitted as
       before.

No credential value exists anywhere here: the documents swarm-api writes hold
none (§4.2, §4.4), and these fixtures follow them.
"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from scheduler import mcp_admission  # noqa: E402
from scheduler.credentials import AccountPool, ForgeConnections, credential_for  # noqa: E402
from scheduler.dispatch import BackendRouter, worker_env  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.mcp_admission import (  # noqa: E402
    McpRegistrations,
    credential_row_id,
    judge,
    named_servers,
    server_id_for,
)
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.secret_slots import WorkspaceGrants  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

TENANT = "eng"
OTHER = "research"
PROFILE = "claude-code"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fixtures: the documents as swarm-api writes them (design §4.2, §4.4)
# --------------------------------------------------------------------------


@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    seed_pool(store, "global", hard_limit=10)
    # claude-code needs a provider key; the tenant has one, so the provider
    # half of `credential_for` says yes and only the MCP half decides.
    seed_tenant(store, TENANT, credentials=("anthropic",))
    seed_tenant(store, OTHER, credentials=("anthropic",))
    return store


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


def scheduler(db: FakeFirestore, dispatcher: RecordingDispatcher) -> Scheduler:
    settings = scheduler_settings()
    return Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )


def register(db: FakeFirestore, name: str = "kb", *, tenant_id: str = TENANT,
             enabled: bool = True, mode: str = "none",
             allowed_profiles: list[str] | None = None) -> str:
    server_id = server_id_for(tenant_id, name)
    db.docs[f"mcp_servers/{server_id}"] = {
        "server_id": server_id, "tenant_id": tenant_id, "name": name,
        "url": f"https://{name}.example.com/mcp", "transport": "streamable-http",
        "description": "the tenant's knowledge base",
        "auth": {"mode": mode, "header": "Authorization", "scheme": "Bearer"},
        "allowed_profiles": allowed_profiles or [], "allowed_tools": [],
        "enabled": enabled, "revision": 1,
    }
    return server_id


def credential_row(db: FakeFirestore, owner_id: str, name: str = "kb", *,
                   tenant_id: str = TENANT, state: str = "live",
                   expires_at: Any = None) -> None:
    server_id = server_id_for(tenant_id, name)
    db.docs[f"mcp_dispatch_credentials/{credential_row_id(owner_id, server_id)}"] = {
        "owner_id": owner_id, "tenant_id": tenant_id, "server_id": server_id, "name": name,
        "secret_id": f"swarm-tenant-{tenant_id}-mcpc-{secrets.token_hex(8)}",
        "version": "1", "state": state,
        "expires_at": expires_at if expires_at is not None
        else datetime.now(timezone.utc) + timedelta(hours=1),
    }


def task(db: FakeFirestore, task_id: str, *, servers: list[str] | None = None,
         profile: str = PROFILE, **fields) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=TENANT, runner_profile=profile, **fields)
    doc["input"] = {"prompt": "look it up"}
    if servers is not None:
        doc["input"]["mcp_servers"] = servers
    return doc


def parked_detail(db: FakeFirestore, task_id: str) -> dict:
    prefix = f"tasks/{task_id}/events/"
    events = [doc for path, doc in sorted(db.docs.items())
              if path.startswith(prefix) and doc.get("type") == "parked"]
    assert events, f"{task_id} recorded no parked event"
    return events[-1]["detail"]


def assert_reserved_nothing(db: FakeFirestore, dispatcher: RecordingDispatcher) -> None:
    # Invariant 1: no lease, no worker. Invariant 2: no pool moved, so no
    # half-made reservation. Invariant 3: nothing LEASED to count.
    assert [path for path in db.docs if path.startswith("leases/")] == []
    assert dispatcher.dispatched == []
    pools = {path: doc["active"] for path, doc in db.docs.items() if path.startswith("pools/")}
    assert pools and all(active == 0 for active in pools.values()), pools


def answer_for(db: FakeFirestore, task_id: str):
    store = SchedulerStore(db)
    snapshot = store.get_task(task_id)
    tenant = Tenant(**{k: v for k, v in db.docs[f"tenants/{TENANT}"].items()
                       if k in Tenant.__dataclass_fields__})
    return credential_for(
        RUNNER_PROFILES[PROFILE], tenant, AccountPool(broker_url="", read_accounts=list),
        task=snapshot, forge=ForgeConnections(db), secrets=WorkspaceGrants(db),
    )


# --------------------------------------------------------------------------
# S-1, S-2: a disabled registration parks before any lease, and is promoted
# --------------------------------------------------------------------------


def test_a_disabled_registration_parks_credential_missing_before_any_lease(db, dispatcher):
    register(db, enabled=False)
    task(db, "task_disabled", servers=["kb"])

    report = scheduler(db, dispatcher).drain()

    doc = db.docs["tasks/task_disabled"]
    assert doc["state"] == "PARKED", doc
    assert doc["park_reason"] == "CREDENTIAL_MISSING"
    assert report.parked == 1
    detail = parked_detail(db, "task_disabled")
    assert detail["code"] == "mcp_server_unavailable"
    assert detail["mcp_servers"] == [{"name": "kb", "reason": "disabled"}]
    # Names, never the URL or the server id.
    assert "example.com" not in repr(detail) and "mcp_" not in repr(detail["mcp_servers"])
    assert_reserved_nothing(db, dispatcher)


def test_the_sweep_promotes_the_park_once_the_registration_is_enabled(db, dispatcher):
    register(db, enabled=False)
    task(db, "task_waits", servers=["kb"])
    scheduler(db, dispatcher).drain()
    assert db.docs["tasks/task_waits"]["state"] == "PARKED"

    scheduler(db, dispatcher).drain()
    assert db.docs["tasks/task_waits"]["state"] == "PARKED", "nothing changed"
    assert dispatcher.dispatched == []

    register(db, enabled=True)
    report = scheduler(db, dispatcher).drain()

    assert report.promoted_credentials == 1
    assert db.docs["tasks/task_waits"]["state"] == "DISPATCHED"
    readied = [doc for path, doc in sorted(db.docs.items())
               if path.startswith("tasks/task_waits/events/")
               and doc.get("detail", {}).get("reason") == "credential_available"]
    assert readied and readied[-1]["detail"].get("mcp_servers") == "available"


def test_a_registration_that_no_longer_allows_the_profile_parks(db, dispatcher):
    register(db, allowed_profiles=["codex"])
    task(db, "task_refused", servers=["kb"])

    scheduler(db, dispatcher).drain()

    assert db.docs["tasks/task_refused"]["park_reason"] == "CREDENTIAL_MISSING"
    assert parked_detail(db, "task_refused")["mcp_servers"] == [
        {"name": "kb", "reason": "profile_refused"}
    ]
    assert_reserved_nothing(db, dispatcher)


def test_another_tenants_registration_is_not_this_tenants(db, dispatcher):
    """Invariant 9: `research` registered `kb`; `eng`'s task has none to use."""
    register(db, tenant_id=OTHER)
    task(db, "task_borrows", servers=["kb"])

    scheduler(db, dispatcher).drain()

    assert parked_detail(db, "task_borrows")["mcp_servers"] == [
        {"name": "kb", "reason": "unregistered"}
    ]
    assert_reserved_nothing(db, dispatcher)


def test_a_document_that_names_another_tenant_is_absent(db, dispatcher):
    """The id is `eng`'s; the document's own `tenant_id` says otherwise."""
    server_id = register(db)
    db.docs[f"mcp_servers/{server_id}"]["tenant_id"] = OTHER
    task(db, "task_forged", servers=["kb"])

    scheduler(db, dispatcher).drain()

    assert db.docs["tasks/task_forged"]["state"] == "PARKED"
    assert_reserved_nothing(db, dispatcher)


def test_an_enabled_registration_admits(db, dispatcher):
    register(db)
    task(db, "task_ok", servers=["kb"])

    report = scheduler(db, dispatcher).drain()

    assert report.leased == 1
    assert db.docs["tasks/task_ok"]["state"] == "DISPATCHED"


# --------------------------------------------------------------------------
# S-3: an expired credential is final, with no lease
# --------------------------------------------------------------------------


@pytest.mark.parametrize("why, row", [
    ("expired", {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}),
    ("expired", {"expires_at": "not an instant"}),
    ("destroyed", {"state": "destroyed"}),
    ("missing", None),
])
def test_an_expired_credential_fails_with_no_lease(db, dispatcher, why, row):
    register(db, mode="dispatch")
    task(db, "task_expired", servers=["kb"])
    if row is not None:
        credential_row(db, "task_expired", **row)

    answer = answer_for(db, "task_expired")

    assert not answer.runnable
    assert answer.failure == (
        "mcp_credential_expired",
        {"code": "mcp_credential_expired", "mcp_credentials": [{"name": "kb", "reason": why}]},
    )
    # Admission takes no lease for it, and the sweep never makes it READY:
    # nothing can hand a queued task a new value.
    scheduler(db, dispatcher).drain()
    scheduler(db, dispatcher).drain()
    assert db.docs["tasks/task_expired"]["state"] != "READY"
    assert db.docs["tasks/task_expired"]["state"] != "DISPATCHED"
    assert_reserved_nothing(db, dispatcher)


def test_an_expired_credential_is_final_even_when_the_server_is_disabled(db):
    register(db, mode="dispatch", enabled=False)
    task(db, "task_both", servers=["kb"])
    credential_row(db, "task_both", state="destroyed")

    assert answer_for(db, "task_both").failure[0] == "mcp_credential_expired"


def test_a_disabled_server_with_a_live_credential_parks_and_does_not_fail(db):
    register(db, mode="dispatch", enabled=False)
    task(db, "task_live", servers=["kb"])
    credential_row(db, "task_live")

    answer = answer_for(db, "task_live")

    assert not answer.runnable and answer.failure is None
    assert answer.park_detail()["code"] == "mcp_server_unavailable"


@pytest.mark.parametrize("owner_field", ["workflow_id", "parent_task_id"])
def test_a_workflows_or_parents_live_credential_admits(db, dispatcher, owner_field):
    """§4.3: a workflow's map serves every step; a child uses its parent's row (Q7)."""
    register(db, mode="dispatch")
    doc = task(db, "task_step", servers=["kb"])
    doc[owner_field] = "owner_1"
    credential_row(db, "owner_1")

    answer = answer_for(db, "task_step")

    assert answer.runnable and answer.failure is None


def test_a_live_credential_admits(db, dispatcher):
    register(db, mode="dispatch")
    task(db, "task_keyed", servers=["kb"])
    credential_row(db, "task_keyed")

    report = scheduler(db, dispatcher).drain()

    assert report.leased == 1
    assert db.docs["tasks/task_keyed"]["state"] == "DISPATCHED"


def test_another_tenants_credential_row_is_not_this_tasks(db):
    register(db, mode="dispatch")
    task(db, "task_other_row", servers=["kb"])
    credential_row(db, "task_other_row")
    row = next(k for k in db.docs if k.startswith("mcp_dispatch_credentials/"))
    db.docs[row]["tenant_id"] = OTHER

    assert answer_for(db, "task_other_row").failure is not None


# --------------------------------------------------------------------------
# The rule, pure
# --------------------------------------------------------------------------


def test_the_rule_is_pure_and_names_every_server():
    regs = {
        "a": {"enabled": True, "auth": {"mode": "none"}},
        "b": {"enabled": False, "auth": {"mode": "none"}},
        "c": None,
        "d": {"enabled": True, "auth": {"mode": "dispatch"}},
    }
    creds = {"d": {"state": "live", "expires_at": NOW - timedelta(seconds=1)}}

    verdict = judge(PROFILE, ("a", "b", "c", "d"), regs, creds, NOW)

    assert [u.to_detail() for u in verdict.unavailable] == [
        {"name": "b", "reason": "disabled"}, {"name": "c", "reason": "unregistered"},
    ]
    assert [u.to_detail() for u in verdict.expired] == [{"name": "d", "reason": "expired"}]
    assert verdict.final and not verdict.runnable

    creds["d"]["expires_at"] = NOW + timedelta(seconds=1)
    assert judge(PROFILE, ("a", "d"), regs, creds, NOW).runnable


def test_only_a_profile_declaring_the_input_names_servers():
    inputs = {"mcp_servers": ["kb", "kb", "docs"]}
    assert named_servers(RUNNER_PROFILES["claude-code"], inputs) == ("kb", "docs")
    assert named_servers(RUNNER_PROFILES["codex"], inputs) == ("kb", "docs")
    assert named_servers(RUNNER_PROFILES["claude-code-browser"], inputs) == ("kb", "docs")
    for name in ("indexer", "claude-code-review", "mock", "browser", "merge"):
        assert named_servers(RUNNER_PROFILES[name], inputs) == (), name
    assert named_servers(RUNNER_PROFILES["claude-code"], {}) == ()
    assert named_servers(RUNNER_PROFILES["claude-code"], {"mcp_servers": "kb"}) == ()


def test_the_server_id_is_the_designs_recipe():
    """§4.2: `mcp_` + the first 16 hex of sha256(tenant_id + NUL + name)."""
    import hashlib

    expected = "mcp_" + hashlib.sha256(b"eng\0kb").hexdigest()[:16]
    assert server_id_for("eng", "kb") == expected
    assert server_id_for("research", "kb") != expected
    assert credential_row_id("task_1", expected) == f"task_1~{expected}"


# --------------------------------------------------------------------------
# S-5: a task that names no server reads nothing and is unchanged
# --------------------------------------------------------------------------


class Counting:
    """A Firestore that counts reads of the MCP collections."""

    def __init__(self, inner: FakeFirestore) -> None:
        self._inner = inner
        self.reads: list[str] = []

    def collection(self, name: str):
        if name in (mcp_admission.SERVERS_COLLECTION, mcp_admission.CREDENTIALS_COLLECTION):
            self.reads.append(name)
        return self._inner.collection(name)


def test_a_task_naming_no_server_reads_nothing_and_is_admitted(db, dispatcher):
    counting = Counting(db)
    reader = McpRegistrations(counting)
    task(db, "task_plain")
    snapshot = SchedulerStore(db).get_task("task_plain")

    assert reader.verdict(snapshot, RUNNER_PROFILES[PROFILE]) is None
    assert counting.reads == []

    report = scheduler(db, dispatcher).drain()
    assert report.leased == 1
    assert not any(k.startswith("mcp_") for k in db.docs)


def test_each_document_is_read_once_per_drain_and_forgotten(db):
    register(db)
    task(db, "task_a", servers=["kb"])
    counting = Counting(db)
    reader = McpRegistrations(counting)
    snapshot = SchedulerStore(db).get_task("task_a")

    reader.verdict(snapshot, RUNNER_PROFILES[PROFILE])
    reader.verdict(snapshot, RUNNER_PROFILES[PROFILE])
    assert counting.reads == ["mcp_servers"]

    reader.forget()
    reader.verdict(snapshot, RUNNER_PROFILES[PROFILE])
    assert counting.reads == ["mcp_servers", "mcp_servers"]


def test_forge_connections_forget_forgets_the_mcp_reads(db):
    """The reader travels on ForgeConnections, which the drain forgets each run."""
    forge = ForgeConnections(db)
    register(db, enabled=False)
    task(db, "task_f", servers=["kb"])
    snapshot = SchedulerStore(db).get_task("task_f")
    assert not forge.mcp.verdict(snapshot, RUNNER_PROFILES[PROFILE]).runnable

    register(db, enabled=True)
    assert not forge.mcp.verdict(snapshot, RUNNER_PROFILES[PROFILE]).runnable, "cached"
    forge.forget()
    assert forge.mcp.verdict(snapshot, RUNNER_PROFILES[PROFILE]).runnable


# --------------------------------------------------------------------------
# S-4: the attempt-key nonce
# --------------------------------------------------------------------------


def _task(*, servers: list[str] | None, profile: str = PROFILE,
          parent: str | None = "task_its_parent") -> Task:
    inputs: dict[str, Any] = {"prompt": "look it up"}
    if servers is not None:
        inputs["mcp_servers"] = servers
    return Task(
        id="task_child", tenant_id=TENANT, created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=profile,
        resource_class=RUNNER_PROFILES[profile].resource_class,
        input=inputs, submitted_by="alice@saga.xyz", parent_task_id=parent,
    )


def _env(task: Task) -> dict[str, str]:
    lease = Lease(
        lease_id="lease_1", task_id=task.id, attempt_id="att_1", tenant_id=TENANT,
        generation=1, pools=["global"], units=1, state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )
    tenant = Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz", created_at=NOW,
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{TENANT}",
    )
    settings = scheduler_settings(
        child_key=secrets.token_hex(16), swarm_api_url="https://api.example"
    )
    return worker_env(task=task, lease=lease, tenant=tenant, settings=settings)


@pytest.fixture
def mcp_alone(monkeypatch):
    """The MCP condition on its own: every profile that declares `mcp_servers`
    today also offers git tools, so without this the nonce would be issued
    whatever the MCP rule said, and the test could not fail."""
    monkeypatch.setattr(dispatch_mod, "offers_git_tools", lambda _name: False)
    monkeypatch.setattr(dispatch_mod, "consumes_workspace_secrets", lambda *_a: False)


@pytest.mark.parametrize("profile", ["claude-code", "codex", "claude-code-browser"])
def test_a_task_naming_a_server_gets_the_attempt_key_nonce(mcp_alone, profile):
    env = _env(_task(servers=["kb"], profile=profile))

    assert len(env.get("SWARM_CHILD_NONCE", "")) == 64
    assert env["SWARM_API_URL"] == "https://api.example"


def test_a_task_naming_no_server_gets_no_nonce_on_the_mcp_rule(mcp_alone):
    assert "SWARM_CHILD_NONCE" not in _env(_task(servers=None))
    assert "SWARM_CHILD_NONCE" not in _env(_task(servers=[]))


def test_a_stray_mcp_servers_key_on_another_profile_adds_no_key(mcp_alone):
    assert "SWARM_CHILD_NONCE" not in _env(_task(servers=["kb"], profile="mock"))


@pytest.mark.parametrize("profile", sorted(RUNNER_PROFILES))
@pytest.mark.parametrize("parent", [None, "task_its_parent"])
def test_a_task_naming_no_server_gets_exactly_the_environment_it_got_before(
    monkeypatch, profile, parent
):
    """Unchanged: the environment with the MCP rule in place equals the one
    with it held to "no", for every profile, top-level and child."""
    t = _task(servers=None, profile=profile, parent=parent)
    now_env = _env(t)
    monkeypatch.setattr(dispatch_mod, "names_mcp_servers", lambda *_a: False)
    before = _env(t)

    assert set(now_env) == set(before)
    assert ("SWARM_CHILD_NONCE" in now_env) == ("SWARM_CHILD_NONCE" in before)
