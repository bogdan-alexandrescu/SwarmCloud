"""GET /v1/outcomes and POST /v1/admin/outcomes/rollup, through the real app.

Issue #185's owner decisions, as the route serves them, over FakeFirestore with a
fixed clock -- the real routes, real tenant scoping, the real rollup documents.

The fixture is one week of eng's work with every shape the ledger has to get
right: a task submitted one day and finished the next (the throughput lane's
basis), cancels that must not dilute the rate, a timeout and a dead letter with
no reason, a workflow whose failed step cancelled its dependant, a step whose
parent belongs to ANOTHER tenant, a retry, an attempt that never started, a
spend of exactly $0.00, and a terminal task with no completed_at. research has
work on the same days, and none of it may ever reach eng.

NOW is 2026-09-25 11:10:02Z; `span=7d` in UTC is 19-25 Sep, bucket index 0-6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import GroupLookupError, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.outcomes import DERIVE_VERSION, Outcomes, wilson
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, api_settings, auth_header, seed_task, seed_tenant
from .fakes import FakeFirestore

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 11, 10, 2, tzinfo=UTC)
WEEK = {"tz": "UTC", "span": "7d"}


def at(day: int, hour: int, minute: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def task(
    db,
    task_id: str,
    *,
    state: str,
    created: datetime,
    completed: datetime | None = None,
    tenant: str = "eng",
    profile: str = "mock",
    submitted_by: str = "alice@saga.xyz",
    workflow_id: str | None = None,
    step_id: str | None = None,
    depends_on: tuple[str, ...] = (),
    attempt_count: int = 1,
    last_error: str | None = None,
    cancel_requested: bool = False,
) -> dict[str, Any]:
    doc = seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant,
        state=state,
        runner_profile=profile,
        created_at=created,
        workflow_id=workflow_id,
        depends_on=depends_on,
        cancel_requested=cancel_requested,
    )
    doc.update(
        {
            "completed_at": completed,
            "submitted_by": submitted_by,
            "step_id": step_id,
            "attempt_count": attempt_count,
            "last_error": last_error,
            "timeout_seconds": 600,
        }
    )
    return doc


def attempt(
    db,
    attempt_id: str,
    *,
    task_id: str,
    generation: int = 1,
    created: datetime,
    started: datetime | None = None,
    exit_code: int | None = None,
    cost: float | None = None,
    tenant: str = "eng",
) -> None:
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": None,
        "created_at": created,
        "started_at": started,
        "completed_at": None,
        "exit_code": exit_code,
        "error": None,
        "cost_usd": cost,
    }


def seed_week(db) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")

    # 22 Sep (bucket 3)
    task(db, "s1", state="SUCCEEDED", created=at(21, 10), completed=at(22, 9),
         profile="claude-code", workflow_id="wf1", step_id="a")
    attempt(db, "a_s1", task_id="s1", created=at(22, 8), started=at(22, 8, 1), exit_code=0, cost=1.5)
    task(db, "s2", state="SUCCEEDED", created=at(22, 8), completed=at(22, 12), attempt_count=2)
    attempt(db, "a_s2a", task_id="s2", generation=1, created=at(22, 8, 5), started=at(22, 8, 10),
            exit_code=75, cost=None)
    attempt(db, "a_s2b", task_id="s2", generation=2, created=at(22, 10, 55), started=at(22, 11),
            exit_code=0, cost=0.0)
    task(db, "f1", state="FAILED", created=at(22, 9), completed=at(22, 13), profile="claude-code",
         workflow_id="wf1", step_id="b", depends_on=("s1",),
         last_error="runner exceeded its 600s timeout and was killed")
    attempt(db, "a_f1", task_id="f1", created=at(22, 12, 45), started=at(22, 12, 50),
            exit_code=137, cost=2.25)
    for name in ("c1", "c2", "c3"):
        task(db, name, state="CANCELLED", created=at(22, 7), completed=at(22, 7, 30),
             attempt_count=0, cancel_requested=True)
    task(db, "c4", state="CANCELLED", created=at(22, 9), completed=at(22, 13, 5),
         workflow_id="wf1", step_id="c", depends_on=("f1",), attempt_count=0,
         last_error="an upstream workflow step did not succeed")

    # 23 Sep (bucket 4)
    task(db, "d1", state="DEAD_LETTERED", created=at(23, 1), completed=at(23, 2),
         profile="generic", attempt_count=3, submitted_by="bob@saga.xyz")
    attempt(db, "a_d1a", task_id="d1", generation=1, created=at(23, 1, 5), started=at(23, 1, 10),
            exit_code=1)
    attempt(db, "a_d1b", task_id="d1", generation=2, created=at(23, 1, 35), started=at(23, 1, 40),
            exit_code=1)
    task(db, "s3", state="SUCCEEDED", created=at(23, 3), completed=at(23, 4))
    attempt(db, "a_s3", task_id="s3", created=at(23, 3, 25), started=at(23, 3, 30), cost=0.0)
    # A step whose parent is research's task: read as unreadable, never used.
    task(db, "x1", state="SUCCEEDED", created=at(23, 5), completed=at(23, 6),
         workflow_id="wf2", step_id="x", depends_on=("r1",))
    attempt(db, "a_x1", task_id="x1", created=at(23, 5, 25), started=at(23, 5, 30), exit_code=0)

    # 24 Sep (bucket 5): only cancels, so no rate
    for name in ("c5", "c6"):
        task(db, name, state="CANCELLED", created=at(24, 10), completed=at(24, 10, 30),
             attempt_count=0, last_error="cancelled on request; runner stopped on SIGTERM")
    task(db, "q1", state="RUNNING", created=at(24, 12))

    # 25 Sep (bucket 6, today, in progress)
    task(db, "s4", state="SUCCEEDED", created=at(25, 8), completed=at(25, 9))
    attempt(db, "a_s4", task_id="s4", created=at(25, 8, 25), started=at(25, 8, 30), exit_code=0, cost=0.0)

    # Outside the span
    task(db, "old", state="SUCCEEDED", created=at(10, 10), completed=at(12, 10))
    task(db, "t_nocomp", state="FAILED", created=at(10, 11), completed=None)

    db.docs["workflows/wf1"] = {
        "workflow_id": "wf1", "tenant_id": "eng", "created_at": at(21, 10),
        "updated_at": at(21, 10), "state": "RUNNING", "submitted_by": "alice@saga.xyz",
        "steps": [
            {"step_id": "a", "runner_profile": "claude-code", "input": {}, "depends_on": [],
             "task_id": "s1"},
            {"step_id": "b", "runner_profile": "claude-code", "input": {}, "depends_on": ["a"],
             "task_id": "f1"},
            {"step_id": "c", "runner_profile": "mock", "input": {}, "depends_on": ["b"],
             "task_id": "c4"},
        ],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False,
    }

    # research, on the same days
    task(db, "r1", tenant="research", state="SUCCEEDED", created=at(22, 8), completed=at(22, 10),
         submitted_by="bob@saga.xyz")
    task(db, "r2", tenant="research", state="FAILED", created=at(22, 8, 30), completed=at(22, 11),
         submitted_by="bob@saga.xyz", last_error="boom")
    attempt(db, "a_r2", task_id="r2", tenant="research", created=at(22, 10, 30),
            started=at(22, 10, 31), exit_code=1)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def clock() -> Clock:
    return Clock()


def _context(db, tokens, groups, clock, **settings):
    return build_context(
        settings=api_settings(**settings),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        now=clock,
    )


@pytest.fixture
def ctx(db, tokens, group_map, clock):
    seed_week(db)
    return _context(db, tokens, StaticGroups(group_map), clock)


@pytest.fixture
def api(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def get(api: TestClient, user: str, **params: Any):
    return api.get("/v1/outcomes", params=params, headers=auth_header(user))


def ok(api: TestClient, user: str, **params: Any) -> dict[str, Any]:
    response = get(api, user, **params)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Exact counts per bucket
# --------------------------------------------------------------------------

def test_every_bucket_is_served_with_its_exact_counts(api):
    body = ok(api, "alice", **WEEK)
    assert body["scope"] == {"kind": "tenant", "tenant_id": "eng"}
    assert body["since"] == "2026-09-19T00:00:00+00:00"
    assert body["until"] == "2026-09-25T11:10:02+00:00"
    assert body["generated_at"] == "2026-09-25T11:10:02Z"
    assert body["bucket"] == "day" and body["bucket_chosen_by"] == "server"
    assert body["basis"] == {"outcomes": "completed_at", "submitted": "created_at"}

    buckets = body["buckets"]
    assert [b["start"][:10] for b in buckets] == [f"2026-09-{d}" for d in range(19, 26)]

    # Before any work: MEASURED zeros, sealed, and no rate.
    for early in buckets[:3]:
        assert early["state"] == "sealed" and early["ended"] == 0 and early["rate"] is None
        assert early["cost"] == {"sum_usd": None, "attempts": 0, "reporting": 0}

    sep22 = buckets[3]
    assert sep22["succeeded"] == 2 and sep22["failed"] == 1 and sep22["dead_lettered"] == 0
    # c4 followed f1, which FAILED: after a failure, not after a cancel (#185, decision 2).
    assert sep22["cancelled"] == {
        "total": 4, "requested": 3, "after_failure": 1, "after_cancel": 0,
        "workflow_sweep": 0, "other": 0,
    }
    assert sep22["ended"] == 7
    assert sep22["rate"] == wilson(2, 3)
    assert sep22["failure_classes"]["timeout"] == 1
    assert sum(sep22["failure_classes"].values()) == 1
    assert sep22["cost"] == {"sum_usd": 3.75, "attempts": 4, "reporting": 3}

    sep23 = buckets[4]
    assert (sep23["succeeded"], sep23["failed"], sep23["dead_lettered"]) == (2, 0, 1)
    assert sep23["failure_classes"]["no_reason"] == 1
    assert sep23["cost"] == {"sum_usd": 0.0, "attempts": 4, "reporting": 1}

    sep24 = buckets[5]
    assert sep24["ended"] == 2 and sep24["rate"] is None, "only cancels: a gap, not 0 %"
    assert sep24["cancelled"]["requested"] == 2
    assert sep24["state"] == "sealed"

    today = buckets[6]
    assert today["succeeded"] == 1 and today["in_progress"] is True and today["state"] == "open"


def test_the_throughput_lane_places_arrivals_by_created_at(api):
    """s1 was submitted on 21 Sep and finished on 22 Sep: counted in each lane once."""
    buckets = ok(api, "alice", **WEEK)["buckets"]
    assert [b["submitted"] for b in buckets] == [0, 0, 1, 6, 3, 3, 1]
    assert buckets[2]["ended"] == 0


def test_the_totals_and_the_rate_exclude_every_cancel(api):
    totals = ok(api, "alice", **WEEK)["totals"]
    assert totals["complete"] is True and totals["buckets"] == 7 and totals["buckets_read"] == 7
    assert (totals["succeeded"], totals["failed"], totals["dead_lettered"]) == (5, 1, 1)
    assert totals["cancelled"]["total"] == 6
    assert totals["ended"] == 13
    assert totals["submitted"] == 14
    assert totals["rate"] == wilson(5, 7)
    assert totals["rate"]["n"] == 7 != totals["ended"]


def test_reported_cost_is_by_task_end_and_says_what_reported(api):
    cost = ok(api, "alice", **WEEK)["totals"]["cost"]
    assert (cost["sum_usd"], cost["attempts"], cost["reporting"]) == (3.75, 9, 5)
    assert cost["by_outcome"]["succeeded"] == {"sum_usd": 1.5, "attempts": 6, "reporting": 4}
    assert cost["by_outcome"]["failed"] == {"sum_usd": 2.25, "attempts": 1, "reporting": 1}
    assert cost["by_outcome"]["dead_lettered"] == {"sum_usd": None, "attempts": 2, "reporting": 0}
    assert cost["by_outcome"]["cancelled"] == {"sum_usd": None, "attempts": 0, "reporting": 0}
    per = cost["per_succeeded_task"]
    assert (per["n"], per["of"]) == (3, 5), "only tasks whose every started attempt reported"
    assert (per["p50_usd"], per["p95_usd"], per["max_usd"]) == (0.0, 1.5, 1.5)
    assert per["values_usd"] == [0.0, 0.0, 1.5]
    assert cost["retries"] == {"sum_usd": 0.0, "attempts": 2, "reporting": 1}
    assert cost["declared"]["profiles"] == ["mock"]
    assert (cost["declared"]["sum_usd"], cost["declared"]["attempts"],
            cost["declared"]["reporting"]) == (0.0, 5, 3)


def test_retries_count_admissions_and_name_the_exits_that_were_not_final(api):
    retries = ok(api, "alice", **WEEK)["retries"]
    tries = {row["attempts"]: row for row in retries["tries"]}
    assert tries["0"]["tasks"] == 6 and tries["0"]["cancelled"] == 6
    assert tries["1"]["tasks"] == 5 and tries["1"]["succeeded"] == 4 and tries["1"]["failed"] == 1
    assert tries["2"]["tasks"] == 1 and tries["2"]["succeeded"] == 1
    assert tries["3+"]["tasks"] == 1 and tries["3+"]["dead_lettered"] == 1
    assert retries["needed_retry"] == {"k": 2, "of": 7}
    assert retries["rescued"] == 1 and retries["failed_after_retry"] == 1
    assert retries["not_final"] == {
        "attempts": 2,
        "by_exit": [
            {"exit_code": 1, "label": "failed", "n": 1},
            {"exit_code": 75, "label": "parked", "n": 1},
        ],
    }
    assert retries["admissions_without_attempt_doc"] == 1


def test_latency_is_per_profile_nearest_rank_and_never_guesses_a_wait(api):
    body = ok(api, "alice", **WEEK)
    rows = body["latency"]["by_profile"]
    assert body["latency"]["percentile_method"] == "nearest_rank"
    assert [r["runner_profile"] for r in rows] == ["mock", "claude-code", "generic"]
    mock = rows[0]
    assert mock["timeout_s"] == 600
    assert mock["succeeded"]["n"] == 4 and mock["failed"]["n"] == 0
    assert mock["failed"]["wait"] is None
    # x1's parent is research's task, so its wait is excluded, not computed.
    assert mock["succeeded"]["wait"] == {
        "n": 3, "p50_s": 1800.0, "p95_s": 1800.0, "max_s": 1800.0,
        "values_s": [600.0, 1800.0, 1800.0],
    }
    assert mock["succeeded"]["run"]["p95_s"] == 13800.0
    assert mock["succeeded"]["total"]["values_s"] == [3600.0, 3600.0, 3600.0, 14400.0]
    claude = rows[1]
    assert claude["succeeded"]["wait"]["values_s"] == [79260.0]
    # f1 became eligible when its parent s1 finished, not when it was created.
    assert claude["failed"]["wait"]["values_s"] == [13800.0]
    assert claude["failed"]["run"]["values_s"] == [600.0]
    assert rows[2]["failed"]["n"] == 1 and rows[2]["failed"]["run"]["values_s"] == [3000.0]
    assert body["coverage"]["wait_excluded"] == 1


def test_groups_are_sorted_by_failures_and_carry_a_series(api):
    groups = ok(api, "alice", **WEEK)["groups"]
    assert groups["by"] == "runner_profile" and groups["rows_total"] == 3
    keys = [row["key"] for row in groups["rows"]]
    assert keys == ["claude-code", "generic", "mock"]
    claude, generic, mock = groups["rows"]
    assert (claude["submitted"], claude["ended"], claude["succeeded"], claude["failed"]) == (2, 2, 1, 1)
    assert claude["cost"] == {"sum_usd": 3.75, "attempts": 2, "reporting": 2}
    assert claude["series"][3] == {"k": 1, "n": 2}
    assert claude["series"][0] == {"k": 0, "n": 0}
    assert generic["rate"] == wilson(0, 1)
    assert (mock["submitted"], mock["ended"], mock["succeeded"]) == (11, 10, 4)
    assert mock["declared_cost"] is True and claude["declared_cost"] is False


def test_workflows_that_failed_name_the_first_failed_step(api, db):
    block = ok(api, "alice", **WEEK)["workflows_failed"]
    assert block["applicable"] is True
    assert (block["with_ended_steps"], block["with_failed_steps"], block["rows_total"]) == (2, 1, 1)
    row = block["rows"][0]
    assert row["workflow_id"] == "wf1" and row["tenant_id"] == "eng"
    assert row["submitted_by"] == "alice@saga.xyz"
    assert row["first_failed"] == {
        "step_id": "b", "task_id": "f1", "failure_class": "timeout",
        "ended_at": "2026-09-22T13:00:00Z",
    }
    assert row["cascade_cancelled"] == 1
    assert row["last_ended_at"] == "2026-09-22T13:05:00Z"
    # Through the owner-chosen rollup path, which also repairs the stored state.
    assert row["state"] == "FAILED"
    assert row["steps"] == {
        "total": 3, "succeeded": 1, "failed": 1, "dead_lettered": 0, "cancelled": 1,
        "open": 0, "unreadable": 0,
    }
    assert block["failing_steps"] == [{"step_id": "b", "n": 1}]
    assert db.docs["workflows/wf1"]["state"] == "FAILED"


def test_coverage_says_what_was_read_and_what_cannot_be_placed(api):
    coverage = ok(api, "alice", **WEEK)["coverage"]
    assert coverage["days"] == {"total": 7, "sealed": 6, "live": 1, "unread": 0}
    assert coverage["unread"] == []
    assert coverage["derived_now"] == 7
    assert coverage["built_through"] == "2026-09-25T11:08:02Z"
    assert coverage["reopened"] == 0
    assert coverage["terminal_without_completed_at"] == 1
    assert coverage["seal_grace_s"] == 900


def test_the_filters_narrow_every_figure(api):
    claude = ok(api, "alice", profile="claude-code", **WEEK)
    assert claude["filters"]["profile"] == ["claude-code"]
    assert (claude["totals"]["succeeded"], claude["totals"]["failed"]) == (1, 1)
    assert claude["totals"]["submitted"] == 2

    bob = ok(api, "alice", submitted_by="BOB@saga.xyz", **WEEK)
    assert bob["totals"]["dead_lettered"] == 1 and bob["totals"]["ended"] == 1

    steps = ok(api, "alice", kind="steps", **WEEK)
    assert (steps["totals"]["succeeded"], steps["totals"]["failed"]) == (2, 1)
    assert steps["totals"]["cancelled"]["total"] == 1

    standalone = ok(api, "alice", kind="standalone", **WEEK)
    assert standalone["workflows_failed"]["applicable"] is False
    assert standalone["workflows_failed"]["rows"] == []
    # Not applicable is not a measured zero (the review of #196).
    assert standalone["workflows_failed"]["with_failed_steps"] is None

    people = ok(api, "alice", group="submitted_by", **WEEK)["groups"]
    assert sorted(row["key"] for row in people["rows"]) == ["alice@saga.xyz", "bob@saga.xyz"]


def test_compare_previous_reads_the_span_before(api):
    body = ok(api, "alice", compare="previous", **WEEK)
    previous = body["previous"]
    assert previous["since"] == "2026-09-12T00:00:00+00:00"
    assert previous["until"] == "2026-09-19T00:00:00+00:00"
    assert previous["complete"] is True
    assert previous["succeeded"] == 1 and previous["failed"] == 0
    assert previous["rate"] == wilson(1, 1)
    assert ok(api, "alice", **WEEK)["previous"] is None


# --------------------------------------------------------------------------
# Time zones, through the route
# --------------------------------------------------------------------------

@pytest.fixture
def october(db, tokens, group_map, clock):
    clock.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    seed_tenant(db, "eng")
    for name, completed in [
        ("e3", datetime(2026, 10, 24, 20, 30, tzinfo=UTC)),  # 24 Oct 23:30+03
        ("e4", datetime(2026, 10, 24, 21, 30, tzinfo=UTC)),  # 25 Oct 00:30+03
        ("e1", datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),   # 25 Oct 03:30+03
        ("e2", datetime(2026, 10, 25, 1, 30, tzinfo=UTC)),   # 25 Oct 03:30+02
        ("e5", datetime(2026, 10, 25, 21, 30, tzinfo=UTC)),  # 25 Oct 23:30+02
        ("e6", datetime(2026, 10, 25, 22, 30, tzinfo=UTC)),  # 26 Oct 00:30+02
    ]:
        task(db, name, state="SUCCEEDED", created=completed - timedelta(minutes=20),
             completed=completed)
    return TestClient(
        create_app(_context(db, tokens, StaticGroups(group_map), clock)),
        raise_server_exceptions=False,
    )


def test_hourly_buckets_across_a_fall_back_keep_the_repeated_hour_apart(october):
    body = ok(
        october, "alice", tz="Europe/Bucharest", bucket="hour",
        since="2026-10-25T02:00:00+03:00", until="2026-10-25T05:00:00+02:00",
    )
    assert body["bucket_chosen_by"] == "caller"
    assert [b["start"] for b in body["buckets"]] == [
        "2026-10-25T02:00:00+03:00",
        "2026-10-25T03:00:00+03:00",
        "2026-10-25T03:00:00+02:00",
        "2026-10-25T04:00:00+02:00",
    ]
    assert [b["succeeded"] for b in body["buckets"]] == [0, 1, 1, 0]


def test_a_local_day_is_25_hours_on_the_fall_back_date(october):
    body = ok(
        october, "alice", tz="Europe/Bucharest", bucket="day",
        since="2026-10-24", until="2026-10-27",
    )
    buckets = body["buckets"]
    assert [(b["start"], b["end"]) for b in buckets] == [
        ("2026-10-24T00:00:00+03:00", "2026-10-25T00:00:00+03:00"),
        ("2026-10-25T00:00:00+03:00", "2026-10-26T00:00:00+02:00"),
        ("2026-10-26T00:00:00+02:00", "2026-10-27T00:00:00+02:00"),
    ]
    assert [b["succeeded"] for b in buckets] == [1, 4, 1]
    assert buckets[2]["in_progress"] is True


# --------------------------------------------------------------------------
# Tenant isolation (invariant 9)
# --------------------------------------------------------------------------

def test_another_tenants_work_is_never_counted_or_read(api, db):
    body = ok(api, "alice", tenant_id="research", **WEEK)  # tenant_id is not a parameter
    assert body["scope"] == {"kind": "tenant", "tenant_id": "eng"}
    assert (body["totals"]["succeeded"], body["totals"]["failed"]) == (5, 1)
    written = db.paths("outcome_days/")
    assert written and all(path.startswith("outcome_days/eng_") for path in written), written


def test_the_other_tenant_sees_only_its_own(api):
    body = ok(api, "bob", **WEEK)
    assert body["scope"] == {"kind": "tenant", "tenant_id": "research"}
    assert (body["totals"]["succeeded"], body["totals"]["failed"]) == (1, 1)
    assert body["totals"]["failure_classes"]["runner_error"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"scope": "platform"},
        {"tenant": "research"},
        {"exclude_tenant": "verify"},
        {"group": "tenant_id"},
    ],
)
def test_a_non_admin_asking_beyond_their_tenant_is_refused_before_validation(api, params):
    """No tz on purpose: the 403 comes first, so nothing about tenants leaks."""
    response = get(api, "alice", **params)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_an_admin_reads_the_platform_with_include_and_exclude(api):
    platform = ok(api, "root", scope="platform", **WEEK)
    assert platform["scope"] == {
        "kind": "platform", "tenants": ["eng", "research"], "excluded": [],
        "tenants_complete": True,
    }
    assert (platform["totals"]["succeeded"], platform["totals"]["failed"]) == (6, 2)
    assert platform["totals"]["rate"] == wilson(6, 9)

    without = ok(api, "root", exclude_tenant="research", **WEEK)
    assert without["scope"]["tenants"] == ["eng"] and without["scope"]["excluded"] == ["research"]
    assert (without["totals"]["succeeded"], without["totals"]["failed"]) == (5, 1)

    only = ok(api, "root", tenant="research", **WEEK)
    assert (only["totals"]["succeeded"], only["totals"]["failed"]) == (1, 1)

    by_tenant = ok(api, "root", scope="platform", group="tenant_id", **WEEK)["groups"]
    assert [row["key"] for row in by_tenant["rows"]] == ["eng", "research"]


def test_an_admins_own_scope_is_still_their_tenant(api):
    body = ok(api, "root", **WEEK)
    assert body["scope"] == {"kind": "tenant", "tenant_id": "eng"}


def test_an_unknown_tenant_is_refused_to_an_admin_with_the_known_ones(api):
    response = get(api, "root", tenant="nope", **WEEK)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["detail"]["known_tenants"] == ["eng", "research"]


def test_an_explicit_tenant_scope_refuses_platform_parameters(api):
    response = get(api, "root", scope="tenant", tenant="research", **WEEK)
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "conflicting_scope"


def test_a_missing_tz_is_the_service_envelope(api):
    response = get(api, "alice", span="7d")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["detail"]["parameter"] == "tz"
    assert body["detail"]["reason"] == "unknown_zone"


class AdminLookupFails:
    """Tenant groups answer; any question about an ADMIN group raises."""

    def __init__(self, mapping):
        self._answers = StaticGroups(mapping)

    def groups_for(self, member_email, candidate_groups):
        if any(g.lower() == ADMIN_GROUP for g in candidate_groups):
            raise GroupLookupError("cloud identity did not answer (test double)")
        return self._answers.groups_for(member_email, candidate_groups)


def test_platform_scope_is_503_when_the_admin_lookup_did_not_answer(db, tokens, group_map, clock):
    seed_week(db)
    client = TestClient(
        create_app(_context(db, tokens, AdminLookupFails(group_map), clock)),
        raise_server_exceptions=False,
    )
    response = client.get("/v1/outcomes", params={"tz": "UTC", "scope": "platform"},
                          headers=auth_header("root"))
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "upstream_unavailable"
    # The caller's own tenant is still readable during the blip.
    own = client.get("/v1/outcomes", params=WEEK, headers=auth_header("root"))
    assert own.status_code == 200, own.text


def test_a_pool_admin_is_not_an_admin_here(db, tokens, group_map, clock):
    tokens = dict(tokens)
    tokens["token-gate"] = {"email": "gate@saga.xyz", "email_verified": True, "sub": "sub-gate"}
    seed_week(db)
    client = TestClient(
        create_app(
            _context(
                db, tokens, StaticGroups(group_map), clock,
                allowed_users=("gate@saga.xyz",), admin_pool_users=("gate@saga.xyz",),
            )
        ),
        raise_server_exceptions=False,
    )
    headers = {"Authorization": "Bearer token-gate"}
    refused = client.get("/v1/outcomes", params={"tz": "UTC", "scope": "platform"}, headers=headers)
    assert refused.status_code == 403, refused.text
    own = client.get("/v1/outcomes", params=WEEK, headers=headers)
    assert own.status_code == 200, own.text
    assert own.json()["totals"]["ended"] == 0


# --------------------------------------------------------------------------
# Unread is reported, never zeroed
# --------------------------------------------------------------------------

def test_past_the_derive_budget_days_are_unread_and_a_rerequest_continues(ctx, clock):
    ctx.outcomes = Outcomes(
        store=ctx.store, rollups=ctx.rollups, metrics=ctx.metrics, now=clock,
        derive_read_budget=1,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    first = ok(client, "alice", **WEEK)
    buckets = first["buckets"]
    assert buckets[6]["state"] == "open" and buckets[6]["succeeded"] == 1, "newest derived first"
    for unread in buckets[:6]:
        assert unread["state"] == "unread" and unread["unread_reason"] == "derive_budget"
        for key in ("submitted", "ended", "succeeded", "failed", "dead_lettered",
                    "cancelled", "rate", "failure_classes", "cost"):
            assert unread[key] is None, (unread["start"], key)
    assert first["totals"]["complete"] is False
    assert first["totals"]["buckets_read"] == 1
    assert first["totals"]["succeeded"] == 1
    assert len(first["coverage"]["unread"]) == 6
    assert first["coverage"]["days"]["unread"] == 6
    assert first["cached"] is False
    for row in first["groups"]["rows"]:
        assert row["series"][:6] == [None] * 6

    second = ok(client, "alice", **WEEK)
    assert second["cached"] is False, "a partial payload is not cached"
    assert second["coverage"]["derived_now"] == 1
    assert len(second["coverage"]["unread"]) == 5
    assert second["buckets"][5]["state"] == "sealed"


class RollupReadsFail(FakeFirestore):
    """The rollup collection cannot be read at all."""

    def get_all(self, references, field_paths=None, transaction=None):
        references = list(references)
        if any(ref.path.startswith("outcome_days/") for ref in references):
            raise gexc.ServiceUnavailable("injected: outcome_days unreadable")
        return super().get_all(references, field_paths, transaction)


def test_a_failed_read_is_unread_everywhere_and_no_number_is_invented(tokens, group_map, clock):
    db = RollupReadsFail()
    seed_week(db)
    client = TestClient(
        create_app(_context(db, tokens, StaticGroups(group_map), clock)),
        raise_server_exceptions=False,
    )
    body = ok(client, "alice", **WEEK)
    assert {b["state"] for b in body["buckets"]} == {"unread"}
    assert {b["unread_reason"] for b in body["buckets"]} == {"read_failed"}
    assert all(b["succeeded"] is None and b["rate"] is None for b in body["buckets"])
    assert body["totals"]["complete"] is False and body["totals"]["buckets_read"] == 0
    assert body["totals"]["rate"] is None
    assert body["coverage"]["days"] == {"total": 7, "sealed": 0, "live": 0, "unread": 7}


def test_a_day_too_large_to_store_is_unread_not_truncated(ctx, clock):
    ctx.outcomes = Outcomes(
        store=ctx.store, rollups=ctx.rollups, metrics=ctx.metrics, now=clock,
        shard_bytes=600, max_shards=0,
    )
    body = ok(TestClient(create_app(ctx), raise_server_exceptions=False), "alice", **WEEK)
    assert body["buckets"][0]["state"] == "sealed", "an empty day fits"
    assert body["buckets"][3]["state"] == "unread"
    assert body["buckets"][3]["unread_reason"] == "too_large"
    assert body["buckets"][3]["succeeded"] is None


def test_a_large_day_is_sharded_and_read_back_whole(ctx, clock, db):
    ctx.outcomes = Outcomes(
        store=ctx.store, rollups=ctx.rollups, metrics=ctx.metrics, now=clock,
        shard_bytes=900, max_shards=11,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    first = ok(client, "alice", **WEEK)
    base = db.docs["outcome_days/eng_2026-09-22"]
    assert base["shards"] >= 1
    assert "outcome_days/eng_2026-09-22_s1" in db.docs
    clock.now = NOW + timedelta(minutes=2)
    second = ok(client, "alice", **WEEK)
    assert second["coverage"]["derived_now"] == 0
    assert second["buckets"][3] == first["buckets"][3]


def test_the_fake_refuses_an_in_filter_over_thirty_values_as_firestore_does(db):
    """Pins the fake's fidelity, which the next test relies on. Real Firestore,
    read-only on dev, 2026-09-25: 30 values streamed, 31 raised this text."""
    thirty = [f"t{i}" for i in range(30)]
    assert list(db.collection("attempts").where(
        filter=FieldFilter("task_id", "in", thirty)).stream()) == []
    with pytest.raises(gexc.InvalidArgument, match="'IN' supports up to 30 comparison values"):
        list(db.collection("attempts").where(
            filter=FieldFilter("task_id", "in", [*thirty, "t30"])).stream())


def test_a_day_of_more_than_thirty_ended_tasks_reads_every_attempt(db, tokens, group_map, clock):
    """A busy day's attempts are read in chunks of at most 30 task ids, because
    Firestore refuses a larger `in`. 35 tasks, one reporting attempt each: the
    day is read, every attempt is found, and none is admitted without its doc.
    tests/integration/test_outcomes_emulator.py runs the same day on the emulator."""
    seed_tenant(db, "eng")
    for i in range(35):
        task(db, f"b{i:02d}", state="SUCCEEDED", created=at(24, 9), completed=at(24, 10, i))
        attempt(db, f"a_b{i:02d}", task_id=f"b{i:02d}", created=at(24, 9, 30),
                started=at(24, 9, 31), exit_code=0, cost=0.25)
    client = TestClient(
        create_app(_context(db, tokens, StaticGroups(group_map), clock)),
        raise_server_exceptions=False,
    )
    body = ok(client, "alice", **WEEK)
    day = body["buckets"][5]
    assert day["state"] == "sealed" and day["unread_reason"] is None
    assert day["succeeded"] == 35
    assert day["cost"] == {"sum_usd": 8.75, "attempts": 35, "reporting": 35}
    assert body["retries"]["admissions_without_attempt_doc"] == 0


# --------------------------------------------------------------------------
# The rollup: reused, re-derived when it must be, drift-checked on request
# --------------------------------------------------------------------------

def test_the_rollup_is_written_once_and_reused(api, db, clock):
    first = ok(api, "alice", **WEEK)
    assert first["coverage"]["derived_now"] == 7
    assert db.docs["outcome_days/eng_2026-09-24"]["sealed"] is True
    live = db.docs["outcome_days/eng_2026-09-25"]
    assert live["sealed"] is False
    assert live["built_through"] == NOW - timedelta(seconds=120)

    cached = ok(api, "alice", **WEEK)
    assert cached["cached"] is True
    assert cached["generated_at"] == first["generated_at"], "a cache hit keeps its real age"

    clock.now = NOW + timedelta(minutes=2)
    again = ok(api, "alice", **WEEK)
    assert again["cached"] is False
    assert again["coverage"]["derived_now"] == 0
    assert again["reads"] < first["reads"]
    assert again["totals"] == first["totals"]


def test_a_sealed_day_is_not_re_derived_on_read_but_the_drift_check_finds_it(api, db, clock):
    ok(api, "alice", **WEEK)
    db.docs["tasks/s3"]["state"] = "FAILED"  # changed under a sealed day

    clock.now = NOW + timedelta(minutes=2)
    reused = ok(api, "alice", **WEEK)
    assert reused["buckets"][4]["succeeded"] == 2, "the sealed doc was served as written"

    report = api.post(
        "/v1/admin/outcomes/rollup",
        params={"tenant_id": "eng", "since": "2026-09-22", "until": "2026-09-24"},
        headers=auth_header("root"),
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["report"]["examined"] == 2
    assert body["report"]["agreed"] == 1 and body["report"]["disagreed"] == 1
    assert body["report"]["repaired"] == 0
    assert body["drifted"] == [
        {
            "day": "2026-09-23", "stored_n": 3, "derived_n": 3, "stored_arrived_n": 3,
            "derived_arrived_n": 3, "missing_ids": [], "extra_ids": [], "changed_ids": ["s3"],
            "repaired": False,
        }
    ]

    repaired = api.post(
        "/v1/admin/outcomes/rollup",
        params={"tenant_id": "eng", "since": "2026-09-23", "until": "2026-09-24", "repair": "true"},
        headers=auth_header("root"),
    ).json()
    assert repaired["report"]["repaired"] == 1
    assert repaired["drifted"][0]["repaired"] is True, "a repair stays visible as a drift"

    clock.now = NOW + timedelta(minutes=4)
    after = ok(api, "alice", **WEEK)["buckets"][4]
    assert (after["succeeded"], after["failed"], after["dead_lettered"]) == (1, 1, 1)
    assert after["failure_classes"]["no_reason"] == 2


def test_the_drift_route_is_admin_only_and_bounded(api):
    assert api.post(
        "/v1/admin/outcomes/rollup", params={"tenant_id": "eng", "since": "2026-09-20"},
        headers=auth_header("alice"),
    ).status_code == 403
    assert api.post(
        "/v1/admin/outcomes/rollup", params={"tenant_id": "nope", "since": "2026-09-20"},
        headers=auth_header("root"),
    ).status_code == 404
    too_long = api.post(
        "/v1/admin/outcomes/rollup",
        params={"tenant_id": "eng", "since": "2026-07-01", "until": "2026-09-01"},
        headers=auth_header("root"),
    )
    assert too_long.status_code == 422 and too_long.json()["detail"]["max"] == 31


def test_the_drift_route_backfills_missing_days(api, db):
    response = api.post(
        "/v1/admin/outcomes/rollup",
        params={"tenant_id": "eng", "since": "2026-09-19", "until": "2026-09-26"},
        headers=auth_header("root"),
    )
    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert report["examined"] == 7 and report["built"] == 7
    assert db.docs["outcome_days/eng_2026-09-22"]["sealed"] is True
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is False


def test_a_day_on_another_derive_version_is_re_derived(api, db, clock):
    ok(api, "alice", **WEEK)
    db.docs["outcome_days/eng_2026-09-20"]["derive_version"] = 0
    clock.now = NOW + timedelta(minutes=2)
    assert ok(api, "alice", **WEEK)["coverage"]["derived_now"] == 1
    # The module's own version, not a number restated here: it moves with every
    # change to the tuple or the classifier (it went 1 -> 2 on 2026-09-25).
    assert db.docs["outcome_days/eng_2026-09-20"]["derive_version"] == DERIVE_VERSION


def test_the_live_day_folds_in_what_ended_since_its_cut(api, db, clock):
    ok(api, "alice", **WEEK)
    task(db, "s5", state="SUCCEEDED", created=at(25, 11, 9), completed=at(25, 11, 11))
    clock.now = NOW + timedelta(minutes=2)
    body = ok(api, "alice", **WEEK)
    assert body["coverage"]["derived_now"] == 0
    assert body["buckets"][6]["succeeded"] == 2
    assert body["buckets"][6]["submitted"] == 2
    rewritten = db.docs["outcome_days/eng_2026-09-25"]
    assert rewritten["n_ended"] == 2, "a live doc older than a minute is rewritten"
    assert rewritten["built_at"] == NOW + timedelta(minutes=2)


def test_a_live_day_is_sealed_by_a_full_derive_once_its_grace_has_passed(api, db, clock):
    ok(api, "alice", **WEEK)
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is False
    clock.now = datetime(2026, 9, 26, 0, 20, tzinfo=UTC)
    body = ok(api, "alice", **WEEK)
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is True
    assert body["buckets"][-2]["start"].startswith("2026-09-25")
    assert body["buckets"][-2]["state"] == "sealed"
    assert body["buckets"][-1]["in_progress"] is True
