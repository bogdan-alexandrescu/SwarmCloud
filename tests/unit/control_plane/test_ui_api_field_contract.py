"""Every FIELD the browser reads must be a field this API sends, and back.

THE GAP THIS CLOSES. `test_every_path_the_ui_calls_exists_on_this_api` (in
test_account_api.py, widened in test_runtimes_screen.py) holds the two ends
together at the level of the ROUTE. It would not have caught any of the last
three field-level failures, because in all three the route was right:

  * `attempt_from_dict` dropped five spend fields, so `/v1/tasks/{id}/attempts`
    returned 200 with `input_tokens: null` on every attempt that ever ran;
  * `types.ts` declared 21 of `task_to_api`'s 31 keys, so `last_error`,
    `result_summary`, `latest_checkpoint` and `next_eligible_at` were served
    and unreadable -- the trouble board was built around a field the client
    type did not admit existed;
  * `TaskPage` said `next_cursor`, which this API has never sent, so paging
    could never have advanced past the first page. A 200 every time.

Nothing else in this repository holds a TypeScript shape to the Python.
`check-contract-parity.sh` reads shell and jq and no `.ts` file; no workflow
runs `tsc`. A renamed field does not fail to compile -- it renders as
`undefined`, which this UI prints as an em dash, so a column that quietly
reads "not measured" on every row is the visible symptom, and it looks like a
platform with nothing to report.

BOTH DIRECTIONS, always:

  * every REQUIRED field in the TypeScript interface is in the payload. A
    client reading a field nobody sends renders an em dash forever.
  * every field in the payload is DECLARED in the interface, optional or not.
    A server sending a field no client declares is a feature nobody can reach
    -- which is how the spend numbers stayed invisible after they were fixed.

The client source is read as TEXT on purpose, exactly as the route-level seam
test does: this is a check on the shipped `types.ts`, and a mock of it would
agree with whatever the screens happen to do. That is a legitimate STATIC
check -- it is about the source's declarations -- which is why it lives here
in Python rather than in the new Vitest layer, which tests behaviour.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups. No credentials, no
emulator, no network.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_common.models import Attempt, Lease, ProviderState, QuotaState
from swarm_common.states import EventType, TaskState

from .conftest import ADMIN_GROUP, ENG_GROUP, auth_header, seed_pool, seed_task, seed_tenant

REPO = Path(__file__).resolve().parents[3]
TYPES_TS = REPO / "apps/swarm-ui/src/types.ts"
API_TS = REPO / "apps/swarm-ui/src/api.ts"

NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Reading the client's declarations
# --------------------------------------------------------------------------

def _source(path: Path) -> str:
    # A missing client file must FAIL, not skip. The UI is checked in, and a
    # rename this test cannot follow is precisely the silent hole it exists to
    # close -- in CI output a skip is indistinguishable from a pass.
    assert path.is_file(), f"{path} is not present; this test would check nothing"
    text = path.read_text()
    assert text.strip(), f"{path} is empty; this test would check nothing"
    return text


def _interface_body(source: str, name: str) -> str:
    """The text between the braces of one exported interface.

    Braces are counted rather than the first `\\n}` matched, so a field whose
    type is an inline object (`thresholds: { a: number; b: number }`) does not
    truncate the body and silently drop every field after it.
    """
    marker = f"export interface {name} {{"
    assert marker in source, f"{marker!r} is not in types.ts; this check would be vacuous"
    start = source.index(marker) + len(marker)
    depth = 1
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i]
    raise AssertionError(f"interface {name} in types.ts is not closed")


def _fields(name: str) -> dict[str, bool]:
    """Field name -> whether it is OPTIONAL (`?:`), for one interface.

    Only fields at the interface's own indent level: a nested object type's
    members belong to that type, not to this one.
    """
    body = _interface_body(_source(TYPES_TS), name)
    found = dict(re.findall(r"^  (\w+)(\??):", body, flags=re.MULTILINE))
    assert found, f"no fields parsed out of interface {name}; the check would be vacuous"
    return {field: mark == "?" for field, mark in found.items()}


# --------------------------------------------------------------------------
# One deployment's worth of real data, served by the real routes
# --------------------------------------------------------------------------

@pytest.fixture
def seeded(client, db):
    """Every shape the UI reads, in one populated deployment.

    Populated rather than minimal on purpose: a payload whose optional fields
    are all null cannot distinguish "the API serves this key" from "the API
    does not", which is the assertion half that was missing when five spend
    fields served null for months.
    """
    tenant = seed_tenant(db, "eng", credentials=("anthropic",))
    seed_tenant(db, "research")
    for name, limit, active in (
        ("global", 40, 12), ("resource:standard", 20, 4), ("runner:mock", 10, 1),
        ("backend:cloudrun", 30, 5), ("provider:anthropic", 6, 6),
        ("provider:anthropic:tenant:eng", 4, 4),
    ):
        seed_pool(db, name, hard_limit=limit, active=active)
    seed_pool(db, "resource:large", hard_limit=4, active=4, enabled=False)

    task = seed_task(db, task_id="task_1", tenant_id="eng", state="RUNNING",
                     provider="anthropic", workflow_id="wf_1")
    # Every optional field carries a value, so "served" and "not served" are
    # distinguishable for each of them.
    task.update(
        {
            "model": "claude-opus-4",
            "started_at": NOW - timedelta(minutes=5),
            "next_eligible_at": NOW + timedelta(minutes=5),
            "last_error": "dispatch_failed (attempt att_1)",
            "result_summary": {"artifacts": ["out.txt"], "exit_code": 0},
            "latest_checkpoint": "gs://bucket/checkpoints/task_1/3",
            "repository_url": "https://github.com/saga/agent-swarm-infra",
            "repository_ref": "main",
            "metadata": {"dispatch": {"strategy": "integrate", "carrier": "branches",
                                      "role": "integrator", "integrates": ["task_0"]}},
            "blocked_by": [{"reason": "TENANT_LIMIT", "pool": "tenant:eng",
                            "limit": 20, "active": 20}],
            "step_id": "s1",
            "current_lease_id": "lease_1",
            "current_generation": 3,
            "input": {"prompt": "do the thing"},
        }
    )

    db.docs["attempts/att_1"] = asdict(
        Attempt(
            attempt_id="att_1", task_id="task_1", tenant_id="eng", generation=3,
            lease_id="lease_1", backend="cloudrun", created_at=NOW - timedelta(minutes=6),
            execution_name="executions/task_1-3", started_at=NOW - timedelta(minutes=5),
            completed_at=NOW - timedelta(minutes=1), exit_code=0, error=None,
            peak_rss_bytes=6_400_000_000, peak_disk_bytes=1_200_000_000,
            oom_near_miss=True, checkpoints=["gs://bucket/checkpoints/task_1/3"],
            input_tokens=12_345, output_tokens=678, cache_read_input_tokens=900,
            cache_creation_input_tokens=100, cost_usd=0.0642028,
        )
    )

    lease = Lease(
        lease_id="lease_1", task_id="task_1", attempt_id="att_1", tenant_id="eng",
        generation=3, pools=["global", "tenant:eng"], units=1, state=TaskState.DISPATCHED,
        created_at=NOW - timedelta(minutes=6), dispatch_deadline=NOW + timedelta(minutes=4),
        expires_at=NOW + timedelta(minutes=30), heartbeat_at=NOW - timedelta(seconds=30),
        released_at=None, release_reason=None,
    )
    doc = asdict(lease)
    doc["state"] = lease.state.value
    db.docs["leases/lease_1"] = doc

    quota = QuotaState(
        provider="anthropic", tenant_id="eng", state=ProviderState.THROTTLED,
        updated_at=NOW, configured_hard_max=50, adaptive_target=6,
        quota_derived_limit=8, requests_remaining=12, tokens_remaining=90_000,
        reset_at=NOW + timedelta(minutes=20), cooldown_until=None,
        last_429_at=NOW - timedelta(minutes=2), retry_after_seconds=30,
        success_count=140, rate_limit_count=3,
    )
    qdoc = asdict(quota)
    qdoc["state"] = quota.state.value
    db.docs["quota/anthropic:eng"] = qdoc

    db.docs["tasks/task_1/events/ev_1"] = {
        "event_id": "ev_1", "task_id": "task_1", "tenant_id": "eng",
        "type": next(iter(EventType)).value, "at": NOW - timedelta(minutes=6),
        "attempt_id": "att_1", "lease_id": "lease_1", "generation": 3,
        "detail": {"note": "admitted"},
    }

    db.docs["workflows/wf_1"] = {
        "workflow_id": "wf_1", "tenant_id": "eng", "created_at": NOW - timedelta(minutes=10),
        "updated_at": NOW, "state": TaskState.RUNNING.value, "submitted_by": "alice@saga.xyz",
        "steps": [{"step_id": "s1", "runner_profile": "mock", "input": {"prompt": "a"},
                   "depends_on": [], "resource_class": "standard",
                   "input_from": {"s0": "out.txt"}, "timeout_seconds": 900,
                   "task_id": "task_1"}],
        "on_step_failure": "continue", "priority": 3, "cancel_requested": False,
    }

    return {"client": client, "tenant": tenant}


def _get(client, path: str, user: str = "root") -> dict[str, Any]:
    response = client.get(path, headers=auth_header(user))
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def _first(rows: list[Any], path: str) -> dict[str, Any]:
    assert rows, f"{path} served no rows; a field comparison against nothing passes vacuously"
    return rows[0]


# --------------------------------------------------------------------------
# The table: one TypeScript interface, one payload the API really served
# --------------------------------------------------------------------------

def _payloads(client) -> dict[str, dict[str, Any]]:
    capacity = _get(client, "/v1/capacity", "alice")
    profile_name, profile = next(iter(sorted(capacity["runner_profiles"].items())))
    assert profile.get("admission"), (
        f"runner profile {profile_name} carries no admission block; the "
        "ProfileAdmission comparison would be vacuous"
    )
    admission = profile["admission"]

    leases = _get(client, "/v1/admin/leases")
    attempts = _get(client, "/v1/tasks/task_1/attempts", "alice")
    events = _get(client, "/v1/tasks/task_1/events", "alice")
    tasks = _get(client, "/v1/tasks?limit=200", "alice")
    workflow = _get(client, "/v1/workflows/wf_1", "alice")
    providers = _get(client, "/v1/providers", "alice")

    shapes: dict[str, dict[str, Any]] = {
        "Capacity": capacity,
        "Pool": _first(capacity["pools"], "/v1/capacity .pools"),
        "RunnerProfile": profile,
        "ProfileAdmission": admission,
        "Task": _get(client, "/v1/tasks/task_1", "alice")["task"],
        "TaskPage": tasks,
        "AttemptRow": _first(attempts["attempts"], "/v1/tasks/{id}/attempts"),
        "LeasePage": leases,
        "LeaseRow": _first(leases["leases"], "/v1/admin/leases .leases"),
        "QuotaState": _first(_get(client, "/v1/admin/quota")["quota"], "/v1/admin/quota .quota"),
        "Tenant": _first(_get(client, "/v1/admin/tenants")["tenants"], "/v1/admin/tenants"),
        "Stats": _get(client, "/v1/stats", "alice"),
        "Me": _get(client, "/v1/tenants/me", "alice"),
        "DispatchControl": _get(client, "/v1/admin/dispatch"),
        "Workflow": workflow["workflow"] if "workflow" in workflow else workflow,
        "ProvidersPage": providers,
        "ProviderEntry": _first(providers["providers"], "/v1/providers .providers"),
        "TaskEvent": _first(events["events"], "/v1/tasks/{id}/events"),
    }

    blockers = admission.get("blockers") or []
    if blockers:
        shapes["ProfileBlocker"] = blockers[0]
    counterfactual = admission.get("counterfactual") or []
    if counterfactual:
        shapes["Counterfactual"] = counterfactual[0]
    return shapes


#: Keys a payload carries that the client deliberately does not declare, with
#: the reason. An entry here is a decision; an undeclared key without one is
#: the drift this file exists to catch.
UNDECLARED_BY_DESIGN: dict[str, dict[str, str]] = {
    "Task": {
        "dispatch": "declared as the optional TaskDispatch, whose own fields are checked separately",
    },
    "Workflow": {
        "steps": "declared as WorkflowStep[], whose own fields are checked separately",
    },
    "Capacity": {
        "runner_profiles": "declared as Record<string, RunnerProfile>, checked as RunnerProfile",
        "pools": "declared as Pool[], checked as Pool",
    },
    "ProfileAdmission": {
        "blockers": "declared as ProfileBlocker[], checked as ProfileBlocker",
        "counterfactual": "declared as Counterfactual[], checked as Counterfactual",
    },
    "LeasePage": {
        "leases": "declared as LeaseRow[], checked as LeaseRow",
        # Served by the api-seams lane (data-gaps audit 2026-09-20 section 1)
        # ahead of the client. The Holders screen's drift check is what reads
        # them, and declaring them in types.ts is the UI lane's change; delete
        # these two lines in that change.
        "truncated": "served ahead of the UI lane; types.ts declares it there",
        "examined": "served ahead of the UI lane; types.ts declares it there",
    },
    "Me": {
        # ui-audit §B9.S5, served by the api-seams lane for Brand.tsx's badge.
        # Declaring them in types.ts is the UI lane's change; delete these two
        # lines in that change.
        "environment": "served ahead of the UI lane; types.ts declares it there",
        "environment_declared": "served ahead of the UI lane; types.ts declares it there",
    },
    "TaskPage": {"tasks": "declared as Task[], checked as Task"},
    "ProvidersPage": {"providers": "declared as ProviderEntry[], checked as ProviderEntry"},
}


def test_the_payload_table_covers_something(seeded):
    """A table that collected nothing would make every case below vacuous."""
    shapes = _payloads(seeded["client"])
    assert len(shapes) >= 16, f"only {len(shapes)} shapes were collected: {sorted(shapes)}"
    for name, payload in shapes.items():
        assert payload, f"{name} came back empty; its comparison would be vacuous"


def test_every_required_client_field_is_served(seeded):
    """The direction that renders an em dash forever when it breaks."""
    shapes = _payloads(seeded["client"])
    problems: list[str] = []
    for name, payload in shapes.items():
        for field, optional in _fields(name).items():
            if optional or field in payload:
                continue
            problems.append(f"{name}.{field}")
    assert not problems, (
        f"types.ts declares these as required and the API serves none of them: "
        f"{sorted(problems)}. Each renders as undefined, which this client "
        "prints as an em dash -- a column that reads 'not measured' on every row."
    )


def test_every_field_the_api_serves_is_declared_by_the_client(seeded):
    """The direction the spend fields broke in, AFTER they were fixed.

    A field the server sends and the client does not declare is a feature
    nobody can reach: the decoder was fixed, the numbers flowed, and
    `AgentDetail.tsx` still could not render them because the type did not
    admit they existed.
    """
    shapes = _payloads(seeded["client"])
    problems: list[str] = []
    for name, payload in shapes.items():
        declared = set(_fields(name))
        allowed = set(UNDECLARED_BY_DESIGN.get(name, {}))
        for key in payload:
            if key in declared or key in allowed:
                continue
            problems.append(f"{name}.{key}")
    assert not problems, (
        f"the API serves these and types.ts declares none of them: "
        f"{sorted(problems)}. Either declare them, or record the reason in "
        "UNDECLARED_BY_DESIGN."
    )


def test_the_exclusion_list_does_not_outlive_its_fields(seeded):
    """An exemption for a key nobody sends silences a real gap later."""
    shapes = _payloads(seeded["client"])
    stale: list[str] = []
    for name, excused in UNDECLARED_BY_DESIGN.items():
        payload = shapes.get(name)
        if payload is None:
            stale.append(f"{name} (no payload)")
            continue
        for key in excused:
            if key not in payload:
                stale.append(f"{name}.{key}")
    assert not stale, f"UNDECLARED_BY_DESIGN excuses keys nothing serves: {sorted(stale)}"


# --------------------------------------------------------------------------
# The regressions, named. Each of these was a shipped bug.
# --------------------------------------------------------------------------

def test_the_five_spend_fields_arrive_with_values_not_nulls(seeded):
    """`attempt_from_dict` dropped all five; a green suite asserted only nulls."""
    attempt = _first(
        _get(seeded["client"], "/v1/tasks/task_1/attempts", "alice")["attempts"],
        "/v1/tasks/{id}/attempts",
    )
    assert attempt["input_tokens"] == 12_345
    assert attempt["output_tokens"] == 678
    assert attempt["cache_read_input_tokens"] == 900
    assert attempt["cache_creation_input_tokens"] == 100
    assert attempt["cost_usd"] == pytest.approx(0.0642028)
    # And the client declares all five, so a screen can actually read them.
    declared = _fields("AttemptRow")
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "cost_usd"):
        assert field in declared, f"AttemptRow does not declare {field}"


def test_the_paging_key_is_the_one_this_api_actually_sends(seeded):
    """`types.ts` said `next_cursor`. This API has never sent it.

    A page token nobody reads is not a crash: the first page renders, the
    control to advance never appears, and the screen looks complete.
    """
    page = _get(seeded["client"], "/v1/tasks?limit=1", "alice")
    declared = _fields("TaskPage")
    assert "next_page_token" in declared
    assert "next_cursor" not in declared
    assert "next_cursor" not in page


def test_the_fields_the_trouble_board_is_built_on_are_declared(seeded):
    """types.ts once declared 21 of task_to_api's 31 keys.

    The ten it omitted were the ones the remaining screens need, so the screens
    were built against a type that said their data did not exist.
    """
    declared = _fields("Task")
    for field in ("last_error", "result_summary", "latest_checkpoint",
                  "next_eligible_at", "metadata", "input", "model",
                  "timeout_seconds", "repository_ref"):
        assert field in declared, f"Task does not declare {field}, which task_to_api sends"


def test_the_lease_state_is_not_offered_to_the_client_as_the_task_state(seeded):
    """Trap B: nothing ever writes STARTING or RUNNING to a lease document.

    Serving it as `state` invites a client to put it beside the task's state
    and read a DISPATCHED lease under a RUNNING task as a contradiction.
    """
    row = _first(_get(seeded["client"], "/v1/admin/leases")["leases"], "/v1/admin/leases")
    assert "dispatch_state" in row
    assert "state" not in row
    assert "dispatch_state" in _fields("LeaseRow")


def test_the_thresholds_arrive_with_the_leases_rather_than_being_restated(seeded):
    """A `const GRACE = 90` in the client is the restatement drift, one layer out."""
    page = _get(seeded["client"], "/v1/admin/leases")
    grace = page["thresholds"]["heartbeat_grace_seconds"]
    assert grace > 0
    assert page["thresholds"]["lease_timeout_seconds"] > 0

    # The decision itself lives in `leaseLiveliness`, which takes the served
    # thresholds as an argument and therefore cannot hold a copy of them.
    types = _source(TYPES_TS)
    assert "thresholds.heartbeat_grace_seconds" in types, (
        "leaseLiveliness no longer reads the served grace period; a constant "
        "in its place colours rows at a boundary the reconciler does not act on"
    )

    # And every caller passes the served block rather than one of its own.
    #
    # BOTH extensions, not just .tsx. The derivation moved out of Overview.tsx
    # into checks.ts so it could be RUN -- it is the part of that screen whose
    # failure mode is silence, and a module a test can import is the only way
    # to drive it. A .tsx-only glob then found no caller and this assertion
    # failed for a refactor rather than for a regression, which is the wrong
    # thing for a contract test to notice.
    callers = [
        p for p in sorted(
            (REPO / "apps/swarm-ui/src").glob("*.ts"),
        ) + sorted((REPO / "apps/swarm-ui/src").glob("*.tsx"))
        if "leaseLiveliness(" in p.read_text()
        and "export function leaseLiveliness" not in p.read_text()
    ]
    assert callers, "nothing calls leaseLiveliness; the thresholds reach no pixel"
    for path in callers:
        text = path.read_text()
        assert re.search(r"leaseLiveliness\([^)]*\.thresholds", text), (
            f"{path.name} calls leaseLiveliness with something other than the "
            "thresholds the API sent"
        )


def test_an_admin_sees_the_platform_counts_and_a_non_admin_gets_no_key_at_all(seeded):
    """An ABSENT field is not a count of nothing, and the client must be able
    to tell them apart -- which needs the field to be optional in the type."""
    admin = _get(seeded["client"], "/v1/stats")
    member = _get(seeded["client"], "/v1/stats", "alice")
    assert "platform_tasks_by_state" in admin
    assert "platform_tasks_by_state" not in member, (
        "a non-admin received the platform counts key; an empty dict here would "
        "render as a platform with no work"
    )
    assert _fields("Stats")["platform_tasks_by_state"] is True, (
        "Stats.platform_tasks_by_state must be OPTIONAL in types.ts: a required "
        "field that is absent reads as undefined, which is indistinguishable "
        "from a zero once a screen sums it"
    )


def test_the_groups_the_client_needs_are_named_here(seeded):
    """The fixture would be meaningless if alice were an admin by accident."""
    assert ENG_GROUP != ADMIN_GROUP
