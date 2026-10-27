"""A task naming a registered MCP server is asked about it before the lease (MCP-SCHED).

docs/design/registered-mcp-servers.md §4.10: in the scheduler, beside the
existing credential park and before any lease,

  * a named server whose registration is disabled, or no longer allows the
    profile, parks CREDENTIAL_MISSING with code `mcp_server_unavailable` and the
    server's name, and the credential sweep returns it to READY once the
    registration is enabled again;
  * a `dispatch` server whose credential row is destroyed or past `expires_at`
    is never leased. On a WORKFLOW's task (a step, or a child of one) it parks
    CREDENTIAL_MISSING with `credential: "mcp"`, the code, the server and
    `expired_at`, because its caller can re-store the credential (BGD
    2026-10-26, Product Oracle's runs); the sweep promotes it once the row is
    live, and ends it FAILED past `MCP_CREDENTIAL_PARK_MAX_SECONDS`. On a task
    that is no workflow's it ends FAILED `mcp_credential_expired` from READY
    or PARKED (contract request 99), no lease, no reservation, no retry.

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
import re
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
from swarm_common.states import InvalidTransition, TaskState, can_transition  # noqa: E402

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
# A standalone task's expired credential: no lease, and no promotion ever
# --------------------------------------------------------------------------


EXPIRED_ROWS = pytest.mark.parametrize(
    "why, row",
    [
        ("expired", {"expires_in": -timedelta(seconds=1)}),
        ("destroyed", {"state": "destroyed"}),
        ("missing", None),
    ],
)


def test_a_waiting_task_may_end_failed_without_a_lease():
    """Contract request 99 (accepted by the owner 2026-10-18): the two states
    admission and the credential sweep hold a task in may reach FAILED. This
    replaces the tripwire that pinned both edges absent."""
    assert can_transition(TaskState.READY, TaskState.FAILED)
    assert can_transition(TaskState.PARKED, TaskState.FAILED)


@EXPIRED_ROWS
@pytest.mark.parametrize("start", ["READY", "PARKED"])
def test_an_expired_credential_is_never_leased(db, make_scheduler, dispatcher, why, row, start):
    """§4.10, for a task that is no workflow's (nothing can re-store its own
    credential): FAILED with the terminal code, no lease, no retry. From READY it
    is admission's write; from PARKED (a CREDENTIAL_MISSING park left before
    request 99, or one parked for a disabled server whose credential then
    expired) it is the credential sweep's. Either way one guarded write, no
    lease, no pool moved, nothing LEASED to count (invariants 1, 2 and 3),
    and no later drain ever returns it to READY, even once a live credential
    row appears for it."""
    world(db)
    register(db)
    if row is not None:
        credential_row(db, "task_expired", **row)
    parked = {"park_reason": "CREDENTIAL_MISSING"} if start == "PARKED" else {}
    task(db, "task_expired", state=start, **parked)

    report = make_scheduler().drain()

    code = "mcp_credential_missing" if why == "missing" else "mcp_credential_expired"
    doc = db.docs["tasks/task_expired"]
    assert (doc["state"], doc["last_error"]) == ("FAILED", code), doc
    assert doc["park_reason"] is None and doc["end_cause"] is None
    assert doc["completed_at"] is not None
    assert doc["attempt_count"] == 0, "no attempt ran"
    assert doc.get("current_lease_id") is None
    assert report.failed_unrunnable == 1
    assert (report.leased, report.parked, report.promoted_credentials) == (0, 0, 0)
    assert_reserved_nothing(db, dispatcher)
    assert [p for p in db.docs if p.startswith("attempts/")] == []
    failed = events(db, "task_expired", "failed")
    assert len(failed) == 1
    assert failed[0]["detail"]["reason"] == code
    assert failed[0]["detail"]["mcp_servers"] == [{"name": "kb", "code": code, "why": why}]
    assert events(db, "task_expired", "parked") == []
    assert events(db, "task_expired", "ready") == []

    # NEVER RETRIED. A live row now exists and the server is enabled, so a
    # retry would admit it; nothing reads a FAILED task back to READY.
    credential_row(db, "task_expired")
    for _ in range(2):
        report = make_scheduler().drain()
        assert report.failed_unrunnable == 0
    doc = db.docs["tasks/task_expired"]
    assert (doc["state"], doc["last_error"]) == ("FAILED", code), doc
    assert events(db, "task_expired", "ready") == []
    assert len(events(db, "task_expired", "failed")) == 1
    assert_reserved_nothing(db, dispatcher)


# --------------------------------------------------------------------------
# A workflow's expired credential: a park its caller ends by re-storing it
# --------------------------------------------------------------------------


def mcp_parks(db, task_id: str) -> list[dict]:
    return [b for b in db.docs[f"tasks/{task_id}"].get("blocked_by") or []
            if b.get("credential") == "mcp"]


@EXPIRED_ROWS
@pytest.mark.parametrize("start", ["READY", "PARKED"])
def test_a_workflow_steps_expired_credential_parks_and_never_fails(
    db, make_scheduler, dispatcher, why, row, start
):
    """BGD 2026-10-26 (Product Oracle's five Code map runs, failed at once on
    promotion at 22:45Z): a step whose workflow credential lapsed while it
    waited parks CREDENTIAL_MISSING, before any lease, with the detail a
    caller watches for, and stays parked drain after drain at no cost."""
    world(db)
    register(db)
    if row is not None:
        credential_row(db, "wf_oracle", **row)
    parked = {"park_reason": "CREDENTIAL_MISSING"} if start == "PARKED" else {}
    task(db, "task_step", state=start, workflow_id="wf_oracle", **parked)

    for _ in range(2):
        report = make_scheduler().drain()
        assert report.failed_unrunnable == 0

    code = "mcp_credential_missing" if why == "missing" else "mcp_credential_expired"
    doc = db.docs["tasks/task_step"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
    assert doc.get("last_error") is None and doc.get("completed_at") is None
    assert events(db, "task_step", "failed") == []
    assert_reserved_nothing(db, dispatcher)
    if start == "READY":
        # Admission's park: the event and the task's `blocked_by` both say it.
        [event] = events(db, "task_step", "parked")
        detail = event["detail"]
        assert (detail["credential"], detail["code"], detail["server"]) == ("mcp", code, "kb")
        [block] = mcp_parks(db, "task_step")
        assert block["reason"] == "CREDENTIAL_MISSING"
        assert (block["code"], block["server"]) == (code, "kb")
        assert block["expired_at"] == detail["expired_at"]
        if why == "missing":
            assert block["expired_at"] is None
        else:
            expected = db.docs[f"mcp_dispatch_credentials/wf_oracle~{server_id(TENANT, 'kb')}"]
            assert block["expired_at"] == expected["expires_at"].isoformat()
        assert block["parked_at"]
        # Names only: never a secret id or a server id.
        assert "mcpc" not in repr(block) and server_id(TENANT, "kb") not in repr(block)


def test_a_child_of_a_workflow_step_parks_on_the_workflows_expired_credential(
    db, make_scheduler, dispatcher
):
    world(db)
    register(db)
    credential_row(db, "wf_parent", expires_in=-timedelta(minutes=5))
    task(db, "task_parent", state="RUNNING", workflow_id="wf_parent")
    child = task(db, "task_child")
    child["parent_task_id"] = "task_parent"

    make_scheduler().drain()

    doc = db.docs["tasks/task_child"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
    assert [b["server"] for b in mcp_parks(db, "task_child")] == ["kb"]
    assert_reserved_nothing(db, dispatcher)


def test_a_restored_workflow_credential_promotes_the_park_and_it_runs(
    db, make_scheduler, dispatcher
):
    """What `PUT /v1/workflows/{id}/mcp-credentials` writes -- a new live row
    with a new `expires_at` -- is what the credential sweep promotes on."""
    world(db)
    register(db)
    credential_row(db, "wf_oracle", expires_in=-timedelta(hours=3))
    task(db, "task_step", workflow_id="wf_oracle")
    make_scheduler().drain()
    assert db.docs["tasks/task_step"]["state"] == "PARKED"

    credential_row(db, "wf_oracle", expires_in=timedelta(hours=2))
    report = make_scheduler().drain()

    assert report.promoted_credentials == 1
    assert events(db, "task_step", "ready")[-1]["detail"]["mcp_servers"] == "available"
    assert db.docs["tasks/task_step"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_step"]
    assert events(db, "task_step", "failed") == []


@pytest.mark.parametrize("hours, ends", [(23, False), (25, True)])
def test_a_park_past_the_limit_ends_failed_and_one_inside_it_waits(
    db, make_scheduler, dispatcher, hours, ends
):
    """The limit (`MCP_CREDENTIAL_PARK_MAX_SECONDS`, 24 h): PARKED -> FAILED
    exists (contract request 99), so a park nobody re-stored ends FAILED
    `mcp_credential_expired`, no lease. Inside the limit it waits."""
    assert mcp_admission.MCP_CREDENTIAL_PARK_MAX_SECONDS == 24 * 3600
    world(db)
    register(db)
    credential_row(db, "wf_gone", expires_in=-timedelta(hours=hours + 1))
    task(db, "task_step", workflow_id="wf_gone")
    make_scheduler().drain()
    doc = db.docs["tasks/task_step"]
    assert doc["state"] == "PARKED"
    began = datetime.now(timezone.utc) - timedelta(hours=hours)
    doc["updated_at"] = began
    for block in doc["blocked_by"]:
        block["parked_at"] = began.isoformat()

    report = make_scheduler().drain()

    doc = db.docs["tasks/task_step"]
    if ends:
        assert (doc["state"], doc["last_error"]) == ("FAILED", "mcp_credential_expired"), doc
        assert report.failed_unrunnable == 1
        [failed] = events(db, "task_step", "failed")
        assert failed["detail"]["limit_seconds"] == 24 * 3600
        assert failed["detail"]["credential"] == "mcp"
    else:
        assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
        assert report.failed_unrunnable == 0
    assert_reserved_nothing(db, dispatcher)


def test_a_forged_later_park_instant_does_not_hold_the_park_past_the_limit(
    db, make_scheduler, dispatcher
):
    """`blocked_by` is in a document an agent may be able to write: the
    earliest instant is taken, so a forged one only ends the wait sooner."""
    world(db)
    register(db)
    credential_row(db, "wf_gone", expires_in=-timedelta(days=3))
    task(db, "task_step", workflow_id="wf_gone")
    make_scheduler().drain()
    doc = db.docs["tasks/task_step"]
    doc["updated_at"] = datetime.now(timezone.utc) - timedelta(days=2)
    for block in doc["blocked_by"]:
        block["parked_at"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    make_scheduler().drain()

    assert db.docs["tasks/task_step"]["state"] == "FAILED"


def test_the_store_never_promotes_a_failed_task(db):
    """`promote_to_ready` is the scheduler's one READY writer, and its guard
    matches a FAILED snapshot as readily as a PARKED one (no park reason on
    either side), while the table allows FAILED -> READY. So it refuses
    anything but PARKED, and a task ended before its lease cannot be retried
    through it."""
    world(db)
    register(db)
    task(db, "task_dead", state="FAILED")
    db.docs["tasks/task_dead"]["last_error"] = "mcp_credential_expired"
    store = SchedulerStore(db)
    snapshot = store.get_task("task_dead")
    before = dict(db.docs["tasks/task_dead"])

    with pytest.raises(InvalidTransition):
        store.promote_to_ready(snapshot, detail={"reason": "credential_available"})

    assert db.docs["tasks/task_dead"] == before
    assert events(db, "task_dead", "ready") == []


#: A write whose source is FAILED and whose target is READY, spelled the
#: ways this repository spells a transition.
_FAILED_TO_READY = re.compile(
    r"(?:assert|can)_transition\(\s*TaskState\.FAILED\s*,\s*TaskState\.READY"
    r"|\(\s*TaskState\.FAILED\s*,\s*TaskState\.READY\s*\)"
)


def test_no_writer_moves_a_failed_task_back_to_ready():
    """The table keeps FAILED -> READY, and no component writes it: every
    READY write is from PARKED (`promote_to_ready`, swarm-api's `human-ready`,
    the reconciler's dependency promotion) or from a lease being returned.
    This holds that so: a writer that names the edge must first say why a
    task ended `mcp_credential_expired` may not take it."""
    apps = Path(__file__).resolve().parents[3] / "apps"
    common = apps / "common" / "swarm_common"
    scanned = 0
    found: list[str] = []
    for path in sorted(apps.rglob("*.py")):
        if common in path.parents:
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(apps)}" for _ in _FAILED_TO_READY.finditer(text)]
    assert scanned > 100, scanned
    assert found == []


def test_a_task_leased_meanwhile_is_not_failed(db):
    """Guarded on the state read: if another scheduler leased the task between
    this drain's read and its write, FAILED over LEASED would be a terminal
    task holding a lease. The write is skipped and nothing else is touched."""
    world(db)
    task(db, "task_raced")
    store = SchedulerStore(db)
    snapshot = store.get_task("task_raced")
    db.docs["tasks/task_raced"]["state"] = "LEASED"

    outcome = store.fail_before_lease(snapshot, "mcp_credential_expired")

    assert not outcome.applied
    assert db.docs["tasks/task_raced"]["state"] == "LEASED"
    assert events(db, "task_raced", "failed") == []


@pytest.mark.parametrize("state", ["SUBMITTED", "QUEUED", "LEASED", "RUNNING", "FAILED"])
def test_only_ready_or_parked_is_failed_before_a_lease(db, state):
    world(db)
    task(db, "task_other", state=state)
    store = SchedulerStore(db)
    snapshot = store.get_task("task_other")

    with pytest.raises(InvalidTransition):
        store.fail_before_lease(snapshot, "mcp_credential_expired")
    assert db.docs["tasks/task_other"]["state"] == state


def test_the_expired_answer_is_terminal_and_the_disabled_one_is_not(db):
    """The answer says which §4.10 outcome it is: a disabled registration
    waits to be enabled (`terminal` False), an expired credential of a task
    that is no workflow's can never become runnable (`terminal` True, FAILED
    in §4.10)."""
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


def test_a_workflow_steps_expired_answer_waits_and_names_the_credential(db):
    """The same expired row, owned by the step's workflow, is not terminal:
    its caller can re-store it, so the answer waits, and its detail carries
    what a caller watches for: `credential == "mcp"`, the code, the server
    by name and `expired_at`."""
    seed_tenant(db, TENANT, credentials=("anthropic",))
    profile = RUNNER_PROFILES[PROFILE]
    register(db)
    path = credential_row(db, "wf_w", expires_in=-timedelta(minutes=1))
    step = replace(_task("task_w"), workflow_id="wf_w")
    answer = credential_for(profile, _tenant(), loop_mod.AccountPool.unwired(scheduler_settings()),
                            task=step, forge=loop_mod.ForgeConnections(db),
                            secrets=loop_mod.WorkspaceGrants(db))
    assert not answer.runnable and not answer.terminal
    detail = answer.park_detail()
    assert {k: detail[k] for k in ("credential", "code", "server", "expired_at")} == {
        "credential": "mcp", "code": "mcp_credential_expired", "server": "kb",
        "expired_at": db.docs[path]["expires_at"].isoformat(),
    }


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
    tenant = Tenant(tenant_id=TENANT, kind="group", principal="eng@example.com",
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
        input=inputs, submitted_by="alice@example.com", parent_task_id=parent,
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT, kind="group", principal="eng@example.com", created_at=NOW,
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
