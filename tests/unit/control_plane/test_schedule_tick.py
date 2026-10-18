"""The schedule tick and its firings (docs/schedules.md §2, lane S2).

WHAT IS HELD HERE
-----------------
* WHO MAY TICK (§2.1, SD10). `POST /v1/admin/schedules/tick` admits the
  `swarm-schedule-tick` identity on `SCHEDULE_TICK_USERS` and nobody else: not
  an admin, not the rollup sweeper, not a member. Unset, it admits nobody and
  says why. The tick's identity reaches no other route, and the rollup
  sweeper's route set is unchanged.
* EXACTLY ONCE PER SLOT (§2.2). A second tick, an interleaved tick and a
  schedule paused between the query and the claim each create nothing more.
  Work created and not recorded is ADOPTED by its `metadata.schedule` mark,
  never created twice.
* CATCH-UP (§2.4) after a simulated 3-hour gap, under `skip` and `run_once`;
  the bound on skip records; the kill switch's own skip code (§2.11).
* FAIRNESS (§2.10): at most 5 per tenant per tick.
* AS WHOM (§2.7): the stored owner, in the stored tenant, only while a
  member; the work carries the mark; a caller cannot forge it.
* THE STOPS (§4.3-§4.4): overlap, budget, run over budget, consecutive
  failures, owner left, dry run, the run gate, and each auto-pause.

No cloud and no emulator: the in-memory Firestore (`fakes.FakeFirestore`)
and the real app. The emulator test of two truly concurrent ticks is the
integration job's (docs/schedules.md §9, S2 acceptance), not this file's.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import schedulefire, schedules, scheduletypes
from swarm_api.auth import (
    ROLLUP_SWEEPER_ROUTES,
    SCHEDULE_TICK_ROUTES,
    StaticTokenVerifier,
    schedule_tick_users,
)
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, build_context
from swarm_api.errors import ValidationFailed
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.schedulefire import Ticker, TickReport, plan_slots
from swarm_api.schemas import TaskCreate, WorkflowCreate
from swarm_api.validation import (
    SCHEDULE_METADATA_KEY,
    RESERVED_METADATA_KEYS,
    reject_reserved_metadata,
    schedule_mark_allowed,
)
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant
from .test_continuation_scope_is_narrow import SWEPT, _ids, _send
from .test_rollup_sweeper_is_narrow import DECIDED as SWEEPER_DECIDED

#: A service-account address, as S13 renders it. Admitted by being on
#: SCHEDULE_TICK_USERS, never through ALLOWED_USERS or a domain.
TICK = "swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
TICK_HEADERS = {"Authorization": "Bearer token-tick"}
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}

#: THE OWNER'S DECISION (SD10) as a literal: one route.
DECIDED = frozenset({("POST", "/v1/admin/schedules/tick")})

NOW = datetime(2026, 10, 9, 9, 0, 30, tzinfo=timezone.utc)
ALICE = "alice@saga.xyz"
CAROL = "carol@saga.xyz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _context(
    db, tokens, group_map, objects, *, verified: Any = True, tick_users: tuple[str, ...] = (TICK,)
) -> AppContext:
    tokens = dict(tokens)
    tokens["token-tick"] = {"email": TICK, "email_verified": verified, "sub": "sub-tick"}
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,), schedule_tick_users=tick_users),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    context.now = lambda: NOW
    return context


@pytest.fixture
def tick_env(monkeypatch):
    # The tick's address itself is `ApiSettings.schedule_tick_users` (see
    # `_context`): with that field present, `schedule_tick_users` reads it and
    # not the environment, so an env var set here would configure nothing.
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    for code in ("BUDGET_EXHAUSTED", "RUN_OVER_BUDGET", "CONSECUTIVE_FAILURES"):
        monkeypatch.delenv(f"REFUSAL_{code}", raising=False)


@pytest.fixture
def ctx(db, tokens, group_map, objects, tick_env) -> AppContext:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return _context(db, tokens, group_map, objects)


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def seed_repo(db, repo_id: str = "r1", tenant_id: str = "eng") -> None:
    db.docs[f"repositories/{repo_id}"] = {"repo_id": repo_id, "tenant_id": tenant_id}


def seed_schedule(
    db,
    *,
    schedule_id: str = "sch_000000000001",
    tenant_id: str = "eng",
    owner: str = ALICE,
    cron: str = "0 9 * * *",
    next_slot: datetime = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc),
    state: str = "enabled",
    policy: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    repo_ids: tuple[str, ...] = ("r1",),
    type_: str = "issue-sweep",
) -> dict[str, Any]:
    for repo_id in repo_ids:
        if f"repositories/{repo_id}" not in db.docs:
            seed_repo(db, repo_id, tenant_id)
    doc = {
        "schedule_id": schedule_id,
        "tenant_id": tenant_id,
        "name": schedule_id,
        "type": type_,
        "scope": {"mode": "repos", "repo_ids": list(repo_ids)},
        "cron": cron,
        "timezone": "UTC",
        "params": {},
        "gate": {"run": "auto", "plan": "approve", "merge": "approve", "approvers": "members",
                 "approval_ttl_hours": 72, **(gate or {})},
        "budget": {"per_run_usd": 15.0, "per_day_usd": 120.0, "max_concurrent": 8, **(budget or {})},
        "policy": {"overlap": "skip", "catch_up": "skip", "jitter": False, "dry_run": False, **(policy or {})},
        "state": state,
        "pause": None,
        "owner": owner,
        "created_by": owner,
        "created_at": NOW - timedelta(days=1),
        "updated_by": owner,
        "updated_at": NOW - timedelta(days=1),
        "next_run_at": next_slot if state == "enabled" else None,
        "next_slot": next_slot,
        "last_firing": None,
        "consecutive_failures": 0,
        "spend": {"day": "2026-10-09", "reported_usd": 0.0, "unreported_attempts": 0, "reserved_usd": 0.0},
        "revision": 1,
    }
    db.docs[f"schedules/{schedule_id}"] = doc
    return doc


class FakeExecutor:
    """A type's executor as S6 will write one: one task per firing, marked by the seam."""

    def __init__(self, *, kind: str = "task", fail_after_submit: bool = False,
                 refuse: Exception | None = None) -> None:
        self.kind = kind
        self.fail_after_submit = fail_after_submit
        self.refuse = refuse
        self.calls = 0
        self.dry_runs = 0
        self.seen: list[schedulefire.Firing] = []

    def create(self, firing: schedulefire.Firing) -> list[dict[str, Any]]:
        self.calls += 1
        self.seen.append(firing)
        if self.refuse is not None:
            raise self.refuse
        if self.kind == "workflow":
            work = [firing.submit_workflow(WorkflowCreate(
                steps=[{"step_id": "only", "runner_profile": "mock", "input": {"prompt": "sweep"}}]
            ), repo_id="r1")]
        elif self.kind == "none":
            work = []
        else:
            work = firing.submit_tasks(
                [TaskCreate(runner_profile="mock", input={"prompt": "sweep"})], repo_id="r1"
            )
        if self.fail_after_submit:
            self.fail_after_submit = False
            raise RuntimeError("the route timed out after submitting")
        return work

    def dry_run(self, firing: schedulefire.Firing) -> dict[str, Any]:
        self.dry_runs += 1
        return {"would_create": [{"kind": "task", "repo_id": r} for r in firing.repo_ids]}


def ticker(ctx, executor: FakeExecutor | None = None, *, at: datetime = NOW, environ=None) -> Ticker:
    ctx.now = lambda: at
    return Ticker(ctx, executors={"issue-sweep": executor or FakeExecutor()}, environ=environ)


def firings(db) -> dict[str, dict[str, Any]]:
    return {k.split("/", 1)[1]: v for k, v in db.docs.items() if k.startswith("schedule_firings/")}


def tasks(db) -> list[dict[str, Any]]:
    """The task documents, not their `events` subcollections."""
    return [v for k, v in db.docs.items() if k.startswith("tasks/") and k.count("/") == 1]


# ---------------------------------------------------------------------------
# Who may tick (§2.1, SD10)
# ---------------------------------------------------------------------------


def test_the_tick_allow_list_is_the_decided_one() -> None:
    assert set(SCHEDULE_TICK_ROUTES) == set(DECIDED)


def test_the_rollup_sweepers_routes_are_not_widened() -> None:
    assert set(ROLLUP_SWEEPER_ROUTES) == set(SWEEPER_DECIDED)
    assert not set(ROLLUP_SWEEPER_ROUTES) & set(SCHEDULE_TICK_ROUTES)


def test_the_tick_route_is_swept_and_the_sweep_is_not_empty() -> None:
    assert set(DECIDED) <= set(SWEPT), "the tick route was not collected by the sweep"
    assert len(SWEPT) > 20


def test_the_tick_identity_ticks(client) -> None:
    response = client.post("/v1/admin/schedules/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["due"] == 0


@pytest.mark.parametrize("who", ["root", "alice"])
def test_an_admin_and_a_member_are_refused(client, who) -> None:
    response = client.post("/v1/admin/schedules/tick", headers=auth_header(who))
    assert response.status_code == 403, response.text


def test_the_rollup_sweeper_is_refused(client) -> None:
    response = client.post("/v1/admin/schedules/tick", headers=SWEEPER_HEADERS)
    assert response.status_code == 403, response.text
    assert "rollup sweeper" in response.text


def test_unset_the_tick_admits_nobody_and_says_so(db, tokens, group_map, objects, monkeypatch) -> None:
    seed_tenant(db, "eng")
    client = TestClient(
        create_app(_context(db, tokens, group_map, objects, tick_users=())), raise_server_exceptions=False
    )
    admin = client.post("/v1/admin/schedules/tick", headers=auth_header("root"))
    assert admin.status_code == 403
    assert "SCHEDULE_TICK_USERS is not configured" in admin.text
    # The tick's address, unlisted, is an outside-domain caller and gets nothing.
    tick = client.post("/v1/admin/schedules/tick", headers=TICK_HEADERS)
    assert tick.status_code in (401, 403), tick.text


@pytest.mark.parametrize("verified", [False, None])
def test_a_tick_token_without_an_explicitly_verified_email_is_refused(
    db, tokens, group_map, objects, tick_env, verified
) -> None:
    seed_tenant(db, "eng")
    client = TestClient(create_app(_context(db, tokens, group_map, objects, verified=verified)),
                        raise_server_exceptions=False)
    assert client.post("/v1/admin/schedules/tick", headers=TICK_HEADERS).status_code == 401


REFUSED = [r for r in SWEPT if r not in DECIDED]


@pytest.mark.parametrize("method,path", REFUSED, ids=_ids(REFUSED))
def test_every_other_authenticated_route_is_refused_to_the_tick(client, method, path) -> None:
    response = _send(client, method, path, TICK_HEADERS)
    assert response.status_code == 403, f"{method} {path} answered {response.status_code} {response.text[:200]}"
    assert "schedule tick" in response.text, response.text[:200]


def test_the_tick_is_neither_an_admin_nor_a_member(ctx) -> None:
    auth = ctx.authenticator.authenticate("Bearer token-tick")
    assert auth.is_schedule_tick is True
    assert auth.is_admin is False and auth.is_rollup_sweeper is False
    assert auth.member_scope == "" and auth.tenant_choices == ()


@pytest.mark.parametrize("other", ["rollup_sweeper_users", "admin_users", "allowed_users"])
def test_an_address_on_two_lists_is_refused_at_start(other) -> None:
    settings = api_settings(schedule_tick_users=(TICK,), **{other: (TICK,)})
    with pytest.raises(ValueError, match="SCHEDULE_TICK_USERS"):
        schedule_tick_users(settings)


def test_a_persons_address_is_refused_as_the_tick() -> None:
    with pytest.raises(ValueError, match="service-account"):
        schedule_tick_users(api_settings(schedule_tick_users=(ALICE,)))


def test_unset_is_nobody() -> None:
    assert schedule_tick_users(api_settings(), {}) == ()


# ---------------------------------------------------------------------------
# Exactly once per slot (§2.2)
# ---------------------------------------------------------------------------


def test_a_due_schedule_fires_once_and_advances(db, ctx) -> None:
    seed_schedule(db)
    executor = FakeExecutor()
    report = ticker(ctx, executor).tick()
    assert (report.claimed, report.fired) == (1, 1)
    fid = "sch_000000000001:" + str(int(datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc).timestamp()) // 60)
    firing = firings(db)[fid]
    assert firing["state"] == "created" and firing["trigger"] == "cron"
    assert firing["lateness_seconds"] == 30.0
    schedule = db.docs["schedules/sch_000000000001"]
    assert schedule["next_slot"] == datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    assert schedule["last_firing"]["firing_id"] == fid
    # A second tick, a minute later, finds nothing due and creates nothing.
    again = ticker(ctx, executor, at=NOW + timedelta(minutes=1)).tick()
    assert again.claimed == 0 and executor.calls == 1 and len(tasks(db)) == 1


def test_an_interleaved_second_tick_claims_nothing(db, ctx) -> None:
    """Both read the due list; one claims; the other's transaction re-reads and stops."""
    seed_schedule(db)
    first, second = ticker(ctx), ticker(ctx)
    due_a, due_b = first._due(NOW), second._due(NOW)
    assert len(due_a) == len(due_b) == 1
    report = TickReport(started_at=NOW)
    assert first.claim("sch_000000000001", NOW, (), report)
    assert second.claim("sch_000000000001", NOW, (), report) is None
    assert len(firings(db)) == 1


def test_an_existing_firing_document_aborts_the_claim(db, ctx) -> None:
    doc = seed_schedule(db)
    fid = schedulefire.firing_id(doc["schedule_id"], doc["next_slot"])
    db.docs[f"schedule_firings/{fid}"] = {"firing_id": fid, "state": "created", "marker": "first"}
    report = TickReport(started_at=NOW)
    assert ticker(ctx).claim(doc["schedule_id"], NOW, (), report) is None
    assert db.docs[f"schedule_firings/{fid}"]["marker"] == "first"
    assert db.docs["schedules/sch_000000000001"]["next_slot"] == doc["next_slot"]


def test_a_schedule_paused_between_the_query_and_the_claim_fires_nothing(db, ctx) -> None:
    seed_schedule(db)
    tick = ticker(ctx)
    assert len(tick._due(NOW)) == 1
    db.docs["schedules/sch_000000000001"]["state"] = "paused"
    assert tick.claim("sch_000000000001", NOW, (), TickReport(started_at=NOW)) is None
    assert firings(db) == {}


def test_jitter_moves_next_run_at_and_not_the_slot(db, ctx) -> None:
    seed_schedule(db, cron="*/30 * * * *", policy={"jitter": True},
                  next_slot=datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc))
    ticker(ctx).tick()
    doc = db.docs["schedules/sch_000000000001"]
    offset = schedules.jitter_seconds("sch_000000000001", timedelta(minutes=30))
    assert doc["next_slot"] == datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc)
    assert doc["next_run_at"] == doc["next_slot"] + timedelta(seconds=offset)


def test_a_retried_task_creation_adopts_the_first_work(db, ctx) -> None:
    """The step submitted, then failed to record: the next finish adopts the task."""
    seed_schedule(db)
    executor = FakeExecutor(fail_after_submit=True)
    report = ticker(ctx, executor).tick()
    assert report.errors == 1
    (fid, firing), = firings(db).items()
    assert firing["state"] == "claimed" and len(tasks(db)) == 1
    later = ticker(ctx, executor, at=NOW + timedelta(minutes=3)).tick()
    firing = firings(db)[fid]
    assert firing["state"] == "created", later.to_api()
    assert executor.calls == 1, "the work was created twice"
    assert firing["work"] == [{"kind": "task", "id": tasks(db)[0]["id"], "repo_id": None}]


def test_a_retried_workflow_creation_adopts_the_first_workflow(db, ctx) -> None:
    seed_schedule(db)
    executor = FakeExecutor(kind="workflow", fail_after_submit=True)
    ticker(ctx, executor).tick()
    ticker(ctx, executor, at=NOW + timedelta(minutes=3)).tick()
    (firing,) = firings(db).values()
    workflow_ids = {t["workflow_id"] for t in tasks(db)}
    assert executor.calls == 1 and len(workflow_ids) == 1
    assert firing["work"] == [{"kind": "workflow", "id": workflow_ids.pop(), "repo_id": None}]


def test_a_fresh_claim_is_not_finished_twice_by_a_stale_sweep(db, ctx) -> None:
    """A claim younger than 2 minutes is the claiming tick's, not the stale sweep's."""
    seed_schedule(db)
    executor = FakeExecutor(fail_after_submit=True)
    ticker(ctx, executor).tick()
    ticker(ctx, executor, at=NOW + timedelta(minutes=1)).tick()
    assert executor.calls == 1
    assert next(iter(firings(db).values()))["state"] == "claimed"


def _claimed(db, ctx) -> str:
    doc = seed_schedule(db)
    fid = ticker(ctx).claim(doc["schedule_id"], NOW, (), TickReport(started_at=NOW))
    assert fid
    return fid


def test_a_lease_is_dated_from_the_wall_clock_not_the_ticks_start(db, ctx) -> None:
    """Review blocker: tick A starts at T and leases at T+200 s; tick D starts at
    T+180 s. Dated from A's start, A's lease would already have lapsed and D
    would create the work a second time."""
    fid = _claimed(db, ctx)
    late = Ticker(ctx, executors={"issue-sweep": FakeExecutor()})
    ctx.now = lambda: NOW + timedelta(seconds=200)
    assert late._lease(fid) is not None
    executor = FakeExecutor()
    ctx.now = lambda: NOW + timedelta(seconds=180)
    other = Ticker(ctx, executors={"issue-sweep": executor})
    assert other.finish(fid, NOW + timedelta(seconds=180), TickReport(started_at=NOW)) is None
    assert executor.calls == 0 and tasks(db) == []


def test_a_finisher_that_lost_its_lease_cannot_move_the_firing(db, ctx) -> None:
    fid = _claimed(db, ctx)
    first = ticker(ctx)
    assert first._lease(fid) is not None
    db.docs[f"schedule_firings/{fid}"]["finisher"] = "someone-else"
    with pytest.raises(Exception, match="another finisher"):
        first._move(fid, NOW, TickReport(started_at=NOW), "created", {"work": []})
    assert db.docs[f"schedule_firings/{fid}"]["state"] == "claimed"


def test_a_recorded_api_action_is_adopted_not_repeated(db, ctx) -> None:
    """Review major: work that is not a task has no mark, so it is recorded on
    the firing the moment it exists and adopted from there after a crash."""

    class Acts(FakeExecutor):
        def create(self, firing):
            self.calls += 1
            firing.api_action("label-prs", outcome="succeeded")
            raise RuntimeError("crashed after the action")

    seed_schedule(db)
    executor = Acts()
    ticker(ctx, executor).tick()
    ticker(ctx, executor, at=NOW + timedelta(minutes=3)).tick()
    (firing,) = firings(db).values()
    assert executor.calls == 1, "the API action ran twice"
    assert firing["state"] == "done" and firing["outcome"] == "succeeded"
    assert [w["kind"] for w in firing["work"]] == ["api_action"]


def test_a_skip_record_never_overwrites_an_existing_firing(db, ctx) -> None:
    doc = seed_schedule(db, cron="0 * * * *", next_slot=GAP_START)
    fid = schedulefire.firing_id(doc["schedule_id"], GAP_START)
    db.docs[f"schedule_firings/{fid}"] = {"firing_id": fid, "state": "created", "slot": GAP_START,
                                          "marker": "live"}
    ticker(ctx, at=GAP_NOW).tick()
    assert db.docs[f"schedule_firings/{fid}"]["marker"] == "live"


def test_another_tenants_marked_task_is_never_adopted(db, ctx) -> None:
    doc = seed_schedule(db)
    fid = schedulefire.firing_id(doc["schedule_id"], doc["next_slot"])
    db.docs["tasks/planted"] = {"id": "planted", "tenant_id": "research", "state": "QUEUED",
                                "metadata": {"schedule": {"firing_id": fid}}}
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 1
    assert firings(db)[fid]["work"][0]["id"] != "planted"


# ---------------------------------------------------------------------------
# Catch-up (§2.4) and the kill switch (§2.11)
# ---------------------------------------------------------------------------

GAP_NOW = datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc)
GAP_START = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)


def _codes(db) -> list[tuple[str, str, Any]]:
    out = []
    for firing in sorted(firings(db).values(), key=lambda f: f["slot"] or NOW):
        out.append((firing["slot"].strftime("%H:%M"), firing["state"],
                    (firing.get("skip") or {}).get("code")))
    return out


def test_catch_up_skip_after_a_three_hour_gap(db, ctx) -> None:
    seed_schedule(db, cron="0 * * * *", next_slot=GAP_START)
    executor = FakeExecutor()
    ticker(ctx, executor, at=GAP_NOW).tick()
    assert _codes(db) == [(h, "skipped", "MISSED_SLOT") for h in ("06:00", "07:00", "08:00", "09:00")]
    assert executor.calls == 0
    assert db.docs["schedules/sch_000000000001"]["next_slot"] == datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


def test_catch_up_run_once_after_a_three_hour_gap(db, ctx) -> None:
    seed_schedule(db, cron="0 * * * *", next_slot=GAP_START, policy={"catch_up": "run_once"})
    executor = FakeExecutor()
    ticker(ctx, executor, at=GAP_NOW).tick()
    assert _codes(db) == [
        ("06:00", "skipped", "MISSED_SLOT"),
        ("07:00", "skipped", "MISSED_SLOT"),
        ("08:00", "skipped", "MISSED_SLOT"),
        ("09:00", "created", None),
    ]
    (fired,) = [f for f in firings(db).values() if f["state"] == "created"]
    assert fired["trigger"] == "catch_up" and executor.calls == 1


def test_a_long_outage_writes_at_most_fifty_records() -> None:
    doc = {"cron": "0 * * * *", "timezone": "UTC", "policy": {"catch_up": "skip"},
           "next_slot": GAP_NOW - timedelta(days=30)}
    plan = plan_slots(doc, GAP_NOW)
    assert plan.fire is None
    assert len(plan.skips) == schedulefire.MAX_SKIP_RECORDS
    first_slot, code, detail = plan.skips[0]
    assert code == "MISSED_SLOT" and first_slot == doc["next_slot"]
    total = 30 * 24 + 1  # every hour from the start through 09:00 today
    assert detail["slots"] + (len(plan.skips) - 1) == total


def test_a_tick_a_minute_late_fires_the_slot_it_was_due_for() -> None:
    doc = {"cron": "0 9 * * *", "timezone": "UTC", "policy": {"catch_up": "skip"},
           "next_slot": datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)}
    plan = plan_slots(doc, datetime(2026, 10, 9, 9, 1, tzinfo=timezone.utc))
    assert plan.fire == doc["next_slot"] and plan.trigger == "cron" and plan.skips == []


def test_the_kill_switch_stops_the_tick_and_names_its_slots_after(db, ctx) -> None:
    seed_schedule(db, cron="0 * * * *", next_slot=GAP_START)
    off = ticker(ctx, at=GAP_START - timedelta(minutes=1), environ={"SCHEDULES_ENABLED": "false"}).tick()
    assert off.to_api() == {"disabled": True}
    assert firings(db) == {}
    ticker(ctx, at=GAP_NOW, environ={"SCHEDULES_ENABLED": "true"}).tick()
    assert {code for _, _, code in _codes(db)} == {"SCHEDULES_DISABLED"}


def test_a_misspelt_switch_is_off() -> None:
    assert schedulefire.schedules_enabled(api_settings(), {"SCHEDULES_ENABLED": "flase"}) is False
    assert schedulefire.schedules_enabled(api_settings(), {}) is True


# ---------------------------------------------------------------------------
# Fairness (§2.10)
# ---------------------------------------------------------------------------


def test_at_most_five_per_tenant_per_tick(db, ctx) -> None:
    for i in range(8):
        seed_schedule(db, schedule_id=f"sch_eng00000000{i}")
    seed_schedule(db, schedule_id="sch_research0001", tenant_id="research", owner="bob@saga.xyz",
                  repo_ids=("rr",))
    report = ticker(ctx).tick()
    by_tenant: dict[str, int] = {}
    for firing in firings(db).values():
        assert firing["state"] == "created", firing
        by_tenant[firing["tenant_id"]] = by_tenant.get(firing["tenant_id"], 0) + 1
    assert by_tenant == {"eng": 5, "research": 1}
    assert report.deferred == 3 and report.truncated is True


def test_fair_pick_is_round_robin() -> None:
    rows = [{"tenant_id": "a", "n": i} for i in range(3)] + [{"tenant_id": "b", "n": 0}]
    picked, deferred = schedulefire.fair_pick(rows, per_tenant=2)
    assert [(r["tenant_id"], r["n"]) for r in picked] == [("a", 0), ("b", 0), ("a", 1)]
    assert deferred == 1


# ---------------------------------------------------------------------------
# As whom, and the mark (§2.7)
# ---------------------------------------------------------------------------


def test_the_work_is_the_owners_in_the_schedules_tenant_and_marked(db, ctx) -> None:
    seed_schedule(db)
    ticker(ctx).tick()
    (task,) = tasks(db)
    (firing,) = firings(db).values()
    assert task["tenant_id"] == "eng" and task["submitted_by"] == ALICE
    assert task["metadata"][SCHEDULE_METADATA_KEY] == {
        "schedule_id": "sch_000000000001",
        "firing_id": firing["firing_id"],
        "slot": "2026-10-09T09:00:00+00:00",
        "type": "issue-sweep",
    }
    # Invariant 1: the work is queued, and the firing took no lease.
    assert task["state"] in ("QUEUED", "READY", "SUBMITTED")
    assert not [k for k in db.docs if k.startswith("leases/")]


def test_an_owner_who_left_the_tenant_pauses_the_schedule(db, ctx) -> None:
    seed_schedule(db, owner=CAROL)
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    (firing,) = firings(db).values()
    assert firing["state"] == "skipped" and firing["skip"]["code"] == "OWNER_NOT_MEMBER"
    schedule = db.docs["schedules/sch_000000000001"]
    assert schedule["state"] == "auto_paused" and schedule["pause"]["code"] == "OWNER_NOT_MEMBER"
    assert schedule["next_run_at"] is None
    assert executor.calls == 0 and tasks(db) == []


def test_schedule_owner_auth_is_an_ordinary_member(ctx) -> None:
    auth = schedulefire.schedule_owner_auth(ctx, {"schedule_id": "sch_1", "tenant_id": "eng", "owner": ALICE})
    assert auth.tenant_id == "eng" and auth.email == ALICE
    assert auth.is_admin is False and auth.member_scope == "" and auth.tenant_member == ""
    assert auth.tenant_principal == "eng@saga.xyz"
    with pytest.raises(schedulefire.ScheduleOwnerNotMember):
        schedulefire.schedule_owner_auth(ctx, {"schedule_id": "sch_1", "tenant_id": "eng", "owner": "bob@saga.xyz"})


def test_a_caller_cannot_set_the_mark(client) -> None:
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}, "metadata": {"schedule": {"firing_id": "x"}}},
    )
    assert response.status_code == 422, response.text
    assert "schedule" in response.json()["detail"]["reserved_metadata_keys"], response.text


def test_the_mark_exemption_is_the_exact_value_and_only_inside_its_block() -> None:
    assert SCHEDULE_METADATA_KEY in RESERVED_METADATA_KEYS
    mark = {"schedule_id": "s", "firing_id": "f", "slot": None, "type": "t"}
    with pytest.raises(ValidationFailed):
        reject_reserved_metadata({SCHEDULE_METADATA_KEY: mark})
    with schedule_mark_allowed(mark):
        reject_reserved_metadata({SCHEDULE_METADATA_KEY: copy.deepcopy(mark)})
        with pytest.raises(ValidationFailed):
            reject_reserved_metadata({SCHEDULE_METADATA_KEY: {**mark, "firing_id": "other"}})
        with pytest.raises(ValidationFailed):
            reject_reserved_metadata({SCHEDULE_METADATA_KEY: mark, "dispatch": {}})
    with pytest.raises(ValidationFailed):
        reject_reserved_metadata({SCHEDULE_METADATA_KEY: mark})


def test_an_executor_cannot_create_past_max_concurrent(db, ctx) -> None:
    seed_schedule(db, budget={"max_concurrent": 1})

    class Greedy(FakeExecutor):
        def create(self, firing):
            return firing.submit_tasks([TaskCreate(runner_profile="mock", input={})] * 2)

    report = ticker(ctx, Greedy()).tick()
    assert report.errors == 1 and tasks(db) == []
    assert next(iter(firings(db).values()))["state"] == "claimed"


# ---------------------------------------------------------------------------
# Overlap, budget, dry run, the run gate (§2.5, §2.8, §4.3)
# ---------------------------------------------------------------------------


def _fire_twice(db, ctx, executor, **schedule) -> None:
    seed_schedule(db, cron="0 * * * *", next_slot=datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc), **schedule)
    ticker(ctx, executor).tick()
    ticker(ctx, executor, at=NOW + timedelta(hours=1)).tick()


def test_overlap_skip_names_the_live_firing(db, ctx) -> None:
    executor = FakeExecutor()
    _fire_twice(db, ctx, executor)
    first, second = sorted(firings(db).values(), key=lambda f: f["slot"])
    assert second["state"] == "skipped" and second["skip"]["code"] == "OVERLAP"
    assert second["skip"]["detail"]["live_firing"] == first["firing_id"]
    assert executor.calls == 1


def test_overlap_queue_one_queues_one_and_creates_it_when_the_live_one_ends(db, ctx) -> None:
    executor = FakeExecutor()
    _fire_twice(db, ctx, executor, policy={"overlap": "queue_one"})
    ticker(ctx, executor, at=NOW + timedelta(hours=2)).tick()
    states = [f["state"] for f in sorted(firings(db).values(), key=lambda f: f["slot"])]
    assert states == ["created", "queued", "skipped"], "never more than one queued"
    for task in tasks(db):
        task["state"] = "SUCCEEDED"
    ticker(ctx, executor, at=NOW + timedelta(hours=2, minutes=1)).tick()
    ordered = sorted(firings(db).values(), key=lambda f: f["slot"])
    assert ordered[0]["state"] == "done" and ordered[0]["outcome"] == "succeeded"
    assert ordered[1]["state"] == "created" and ordered[1]["trigger"] == "queued"
    assert executor.calls == 2


def test_a_firing_ends_when_its_work_ends_and_a_success_resets_failures(db, ctx) -> None:
    seed_schedule(db)
    db.docs["schedules/sch_000000000001"]["consecutive_failures"] = 2
    ticker(ctx).tick()
    for task in tasks(db):
        task["state"] = "SUCCEEDED"
    report = ticker(ctx, at=NOW + timedelta(minutes=1)).tick()
    (firing,) = firings(db).values()
    assert report.advanced == 1
    assert firing["state"] == "done" and firing["outcome"] == "succeeded"
    assert firing["cost"] == {"reported_usd": 0.0, "unreported_attempts": 0}
    schedule = db.docs["schedules/sch_000000000001"]
    assert schedule["consecutive_failures"] == 0
    assert schedule["last_firing"]["outcome"] == "succeeded"


def test_nothing_due_is_an_answer(db, ctx) -> None:
    seed_schedule(db)
    ticker(ctx, FakeExecutor(kind="none")).tick()
    (firing,) = firings(db).values()
    assert firing["state"] == "done" and firing["outcome"] == "succeeded" and firing["work"] == []


def _spent(db, usd: float) -> None:
    db.docs["schedules/sch_000000000001"]["spend"]["reported_usd"] = usd


def test_budget_exhausted_is_report_only_until_switched_on(db, ctx, monkeypatch) -> None:
    seed_schedule(db, budget={"per_run_usd": 15.0, "per_day_usd": 20.0})
    _spent(db, 19.0)
    # Off (the default): the firing goes ahead, and the log names the stop.
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 1


def test_budget_exhausted_skips_when_on(db, ctx, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_BUDGET_EXHAUSTED", "on")
    seed_schedule(db, budget={"per_run_usd": 15.0, "per_day_usd": 20.0})
    _spent(db, 6.0)
    db.docs["schedules/sch_000000000001"]["spend"]["unreported_attempts"] = 1
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    (firing,) = firings(db).values()
    assert firing["skip"]["code"] == "BUDGET_EXHAUSTED" and executor.calls == 0
    assert db.docs["schedules/sch_000000000001"]["state"] == "enabled"


def test_a_dry_run_creates_nothing(db, ctx) -> None:
    seed_schedule(db, policy={"dry_run": True})
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    (firing,) = firings(db).values()
    assert firing["state"] == "skipped" and firing["skip"]["code"] == "DRY_RUN"
    assert firing["dry_run"] == {"would_create": [{"kind": "task", "repo_id": "r1"}]}
    assert executor.calls == 0 and tasks(db) == []


def test_a_run_gate_holds_the_firing_as_a_document(db, ctx) -> None:
    seed_schedule(db, gate={"run": "approve"})
    executor = FakeExecutor()
    report = ticker(ctx, executor).tick()
    (firing,) = firings(db).values()
    assert firing["state"] == "awaiting_approval" and firing["params_digest"].startswith("sha256:")
    assert report.held == 1 and executor.calls == 0 and tasks(db) == []


def test_a_repository_no_longer_registered_is_refused(db, ctx) -> None:
    seed_schedule(db)
    del db.docs["repositories/r1"]
    ticker(ctx).tick()
    (firing,) = firings(db).values()
    assert firing["state"] == "refused" and firing["skip"]["code"] == "REPOSITORY_NOT_GRANTED"
    assert db.docs["schedules/sch_000000000001"]["consecutive_failures"] == 1


def test_a_type_withdrawn_from_the_catalogue_disables_the_schedule(db, ctx) -> None:
    seed_schedule(db, type_="no-such-type")
    ticker(ctx).tick()
    (firing,) = firings(db).values()
    assert firing["skip"]["code"] == "TYPE_UNAVAILABLE"
    assert db.docs["schedules/sch_000000000001"]["state"] == "disabled"


def test_an_unbuilt_executor_is_never_imported_as_available(db, ctx) -> None:
    """A type whose `swarm_api/schedtypes/` module does not exist yet: the real loader finds none.

    Chosen at run time, because each type lane (S6, S10a-h) adds a module.
    """
    unbuilt = [entry.name for entry in scheduletypes.TYPES if not scheduletypes.executor_present(entry)]
    if not unbuilt:
        pytest.skip("every type's executor is built")
    seed_schedule(db, type_=unbuilt[0])
    Ticker(ctx).tick()
    (firing,) = firings(db).values()
    assert firing["skip"]["code"] == "TYPE_UNAVAILABLE" and tasks(db) == []


# ---------------------------------------------------------------------------
# Consecutive failures and run over budget (§4.3, §4.4)
# ---------------------------------------------------------------------------


def _refusing() -> FakeExecutor:
    error = ValidationFailed("the workspace is not ready")
    error.code = "WORKSPACE_NOT_READY"
    return FakeExecutor(refuse=error)


def test_three_refusals_in_a_row_pause_the_schedule_when_on(db, ctx, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_CONSECUTIVE_FAILURES", "on")
    seed_schedule(db, cron="0 * * * *", next_slot=datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc))
    executor = _refusing()
    for hour in range(3):
        ticker(ctx, executor, at=NOW + timedelta(hours=hour)).tick()
    schedule = db.docs["schedules/sch_000000000001"]
    assert [f["skip"]["code"] for f in firings(db).values()] == ["WORKSPACE_NOT_READY"] * 3
    assert schedule["state"] == "auto_paused" and schedule["pause"]["code"] == "CONSECUTIVE_FAILURES"
    assert schedule["pause"]["reason"] == "Paused after 3 failed runs in a row"


def test_consecutive_failures_are_report_only_by_default(db, ctx) -> None:
    seed_schedule(db, cron="0 * * * *", next_slot=datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc))
    executor = _refusing()
    for hour in range(3):
        ticker(ctx, executor, at=NOW + timedelta(hours=hour)).tick()
    schedule = db.docs["schedules/sch_000000000001"]
    assert schedule["state"] == "enabled" and schedule["consecutive_failures"] == 3


def _attempt(db, task: dict[str, Any], cost: float | None) -> None:
    db.docs[f"attempts/{task['id']}-1"] = {
        "attempt_id": f"{task['id']}-1", "task_id": task["id"], "tenant_id": task["tenant_id"],
        "lease_id": "l", "generation": 1, "state": "SUCCEEDED", "created_at": NOW, "started_at": NOW,
        "completed_at": NOW + timedelta(minutes=5), "cost_usd": cost,
    }


def test_a_run_over_budget_fails_the_firing_and_pauses_when_on(db, ctx, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_RUN_OVER_BUDGET", "on")
    seed_schedule(db, budget={"per_run_usd": 2.0, "per_day_usd": 10.0})
    ticker(ctx).tick()
    (task,) = tasks(db)
    task["state"] = "SUCCEEDED"
    _attempt(db, task, 3.5)
    ticker(ctx, at=NOW + timedelta(minutes=1)).tick()
    (firing,) = firings(db).values()
    assert firing["outcome"] == "failed" and firing["skip"]["code"] == "RUN_OVER_BUDGET"
    assert firing["cost"]["reported_usd"] == 3.5
    schedule = db.docs["schedules/sch_000000000001"]
    assert schedule["state"] == "auto_paused" and schedule["pause"]["code"] == "RUN_OVER_BUDGET"
    assert schedule["spend"]["reported_usd"] == 3.5


def test_an_unreported_cost_is_counted_not_zero(db, ctx) -> None:
    seed_schedule(db)
    ticker(ctx).tick()
    (task,) = tasks(db)
    task["state"] = "SUCCEEDED"
    _attempt(db, task, None)
    ticker(ctx, at=NOW + timedelta(minutes=1)).tick()
    (firing,) = firings(db).values()
    assert firing["cost"] == {"reported_usd": 0.0, "unreported_attempts": 1}
    assert db.docs["schedules/sch_000000000001"]["spend"]["unreported_attempts"] == 1


def test_combine_is_the_workflow_rule() -> None:
    combine = schedulefire.combine
    assert combine([], independent=False) == "succeeded"
    assert combine(["succeeded", "failed"], independent=True) == "partial"
    assert combine(["succeeded", "failed"], independent=False) == "failed"
    assert combine(["cancelled"], independent=True) == "cancelled"
    assert schedulefire.run_outcome("NOT_READY") == "succeeded"
    assert schedulefire.run_outcome("PLANNED") is None


# ---------------------------------------------------------------------------
# The tick's report (§2.10) and run now
# ---------------------------------------------------------------------------


def test_the_tick_writes_its_report(db, ctx) -> None:
    seed_schedule(db)
    ticker(ctx).tick()
    report = db.docs[f"schedule_ticks/{int(NOW.timestamp()) // 60}"]
    assert report["fired"] == 1 and report["expire_at"] == NOW + timedelta(days=7)
    assert report["lateness"][0]["seconds"] == 30.0


def test_the_tick_stops_starting_work_at_its_budget(db, ctx) -> None:
    for i in range(3):
        seed_schedule(db, schedule_id=f"sch_budget00000{i}")
    clock = iter([0.0] + [300.0] * 50)
    report = Ticker(ctx, executors={"issue-sweep": FakeExecutor()}, clock=lambda: next(clock)).tick()
    assert report.truncated is True and report.claimed == 0


def test_run_now_fires_a_paused_schedule_once_as_its_owner(db, ctx) -> None:
    doc = seed_schedule(db, state="paused")
    executor = FakeExecutor()
    firing = ticker(ctx, executor).fire_now(doc, actor=ALICE)
    assert firing["trigger"] == "run_now" and firing["state"] == "created"
    assert firing["firing_id"].startswith("sch_000000000001:run_now:")
    assert tasks(db)[0]["submitted_by"] == ALICE
