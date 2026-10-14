"""The `cost-report` schedule type (docs/schedules.md §3.11, lane S10g).

WHAT IS HELD HERE
-----------------
* THE REPORT sums `cost_usd` over the attempts CREATED in the window, by
  schedule, repository, profile and person, under the schedule's tenant
  only. Every row and the total carry their coverage; an attempt with no cost
  is counted and makes its rows a lower bound; a row none of whose attempts
  reported is null, never $0.00; a reported $0.00 is a measurement.
* BOUNDED: past `MAX_ROWS` the rest fold into one counted `other` row; past
  `MAX_ATTEMPTS` the report says `truncated` and the total is a floor.
* AN API ACTION: the report is one `api_action` item, recorded on the firing;
  no task is created.
* `post_to_issue` posts ONE comment, with the tenant's credential, only to a
  registered repository, never the by-person rows, and never twice for one
  firing. A comment GitHub refuses is its own FAILED item naming the
  permission, so the firing is `partial`, not a silent success.
* THE PLATFORM VARIANT reads every tenant as aggregates (tenant, profile) and
  is never posted.
* A DRY RUN records nothing, writes nothing to GitHub and reads no credential.

No cloud and no emulator: the in-memory Firestore, GitHub as a fake transport
under the real client.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any


from swarm_api import forge, schedulefire, scheduletypes, schedules
from swarm_api.schedtypes import cost_report

from . import forge_fakes
from .conftest import seed_task, seed_tenant
from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    NOW,
    REPOSITORY,
    SLOT,
    ctx,
    firing_for,
    firings,
    github,
    make_context,
    make_schedule,
    register,
    tasks,
    writes,
)

BOB = "bob@saga.xyz"


def report_schedule(db, *, params: dict | None = None, scope_mode: str = "repos",
                    repo_ids: tuple[str, ...] = (), cron: str = "0 9 * * *") -> dict[str, Any]:
    return make_schedule(db, type_="cost-report", params=params, scope_mode=scope_mode,
                         repo_ids=repo_ids, cron=cron, max_concurrent=2)


def task(db, task_id: str, *, tenant_id: str = "eng", profile: str = "claude-code",
         person: str = ALICE, repository: str | None = REPOSITORY,
         schedule_id: str | None = None) -> None:
    seed_task(db, task_id=task_id, tenant_id=tenant_id, runner_profile=profile,
              state="SUCCEEDED", created_at=NOW - timedelta(hours=2))
    doc = db.docs[f"tasks/{task_id}"]
    doc["submitted_by"] = person
    doc["repository_url"] = f"https://github.com/{repository}" if repository else None
    if schedule_id:
        doc["metadata"] = {"schedule": {"schedule_id": schedule_id, "firing_id": "f", "type": "issue-sweep"}}


def attempt(db, attempt_id: str, task_id: str, *, tenant_id: str = "eng", cost: float | None,
            hours_ago: float = 1) -> None:
    created = NOW - timedelta(hours=hours_ago)
    doc: dict[str, Any] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant_id, "generation": 1,
        "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB", "execution_name": None,
        "created_at": created, "started_at": created + timedelta(seconds=5),
        "completed_at": created + timedelta(minutes=1), "exit_code": 0, "error": None,
        "peak_rss_bytes": None, "oom_near_miss": False, "checkpoints": [],
    }
    # Absent, never zero, as `control.record_spend` writes it.
    if cost is not None:
        doc["cost_usd"] = cost
    db.docs[f"attempts/{attempt_id}"] = doc


def seed_work(db) -> None:
    """eng: three attempts in the day (one with no cost), one a week back; research: one."""
    task(db, "t1", schedule_id="sch_sweep")
    task(db, "t2", profile="codex", person=BOB, repository="saga-xyz/gadgets")
    task(db, "t3", repository=None)
    attempt(db, "a1", "t1", cost=1.5)
    attempt(db, "a2", "t1", cost=2.0)
    attempt(db, "a3", "t2", cost=None)
    attempt(db, "a4", "t3", cost=4.0, hours_ago=72)
    task(db, "r1", tenant_id="research", person="carol@saga.xyz")
    attempt(db, "ra1", "r1", tenant_id="research", cost=99.0)


def by(report: dict[str, Any], dimension: str) -> dict[str, dict[str, Any]]:
    return {row["key"]: {k: v for k, v in row.items() if k != "key"} for row in report["by"][dimension]}


def params(**values: Any) -> Any:
    return scheduletypes.CostReportParams.model_validate(values)


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("cost-report")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is cost_report
    # Two items at most (the report, its comment), and a budget a schedule can hold.
    assert scheduletypes.budget_caps(entry).max_concurrent == 2
    assert schedules.resolve_budget(entry, None)["per_run_usd"] > 0


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_report_is_the_tenants_own_spend_by_four_dimensions_with_coverage(db, ctx) -> None:
    seed_work(db)

    report = cost_report.compute(ctx, report_schedule(db), params(window="day"), NOW)

    assert report["scope"] == "tenant" and report["truncated"] == []
    # a4 is three days back; research's ra1 is another tenant's.
    assert report["total"] == {"attempts": 3, "attempts_with_cost": 2, "cost_usd": 3.5, "lower_bound": True}
    assert report["coverage"]["attempts"] == 3 and report["coverage"]["with_spend"] == 2
    assert by(report, "profile") == {
        "claude-code": {"attempts": 2, "attempts_with_cost": 2, "cost_usd": 3.5, "lower_bound": False},
        # NULL IS NOT ZERO: its one attempt reported nothing.
        "codex": {"attempts": 1, "attempts_with_cost": 0, "cost_usd": None, "lower_bound": True},
    }
    assert set(by(report, "schedule")) == {"sch_sweep", cost_report.UNSCHEDULED}
    assert set(by(report, "repository")) == {REPOSITORY, "saga-xyz/gadgets"}
    assert set(by(report, "person")) == {ALICE, BOB}
    assert "carol@saga.xyz" not in str(report)


def test_a_week_reaches_back_seven_days(db, ctx) -> None:
    seed_work(db)

    report = cost_report.compute(ctx, report_schedule(db), params(window="week"), NOW)

    assert report["total"]["attempts"] == 4 and report["total"]["cost_usd"] == 7.5
    assert by(report, "repository")[cost_report.NO_REPOSITORY]["cost_usd"] == 4.0


def test_a_reported_zero_is_a_measurement_and_no_attempts_is_null(db, ctx) -> None:
    empty = cost_report.compute(ctx, report_schedule(db), params(), NOW)
    assert empty["total"] == {"attempts": 0, "attempts_with_cost": 0, "cost_usd": None, "lower_bound": False}

    task(db, "t1", profile="mock")
    attempt(db, "a1", "t1", cost=0.0)
    report = cost_report.compute(ctx, report_schedule(db), params(), NOW)
    assert report["total"] == {"attempts": 1, "attempts_with_cost": 1, "cost_usd": 0.0, "lower_bound": False}


def test_rows_past_the_bound_fold_into_one_counted_other_row() -> None:
    rows = {f"k{n:02d}": {"attempts": 1, "attempts_with_cost": 1, "cost_usd": float(n)} for n in range(5)}

    kept = cost_report.bounded_rows(rows, limit=2)

    assert [row["key"] for row in kept] == ["k04", "k03", cost_report.OTHER]
    assert kept[-1]["keys"] == 3 and kept[-1]["cost_usd"] == 3.0 and kept[-1]["attempts"] == 3


def test_a_read_cut_at_its_bound_says_so_and_the_total_is_a_floor(db, ctx, monkeypatch) -> None:
    task(db, "t1")
    for n in range(5):
        attempt(db, f"a{n}", "t1", cost=1.0, hours_ago=1 + n / 10)
    monkeypatch.setattr(cost_report, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cost_report, "PAGE_SIZE", 2)

    report = cost_report.compute(ctx, report_schedule(db), params(), NOW)

    assert report["truncated"] == ["attempts"]
    assert report["total"]["attempts"] == 2 and report["total"]["lower_bound"] is True


# ---------------------------------------------------------------------------
# The firing
# ---------------------------------------------------------------------------


def test_the_report_is_one_api_action_recorded_on_the_firing_and_no_task(db, ctx, writes) -> None:
    seed_work(db)
    recorded: list[dict[str, Any]] = []
    before = len(tasks(db))

    work = cost_report.create(firing_for(ctx, report_schedule(db), room=2, recorded=recorded))

    assert [(w["kind"], w["id"], w["outcome"]) for w in work] == [("api_action", "report", "succeeded")]
    assert recorded == work and work[0]["detail"]["total"]["cost_usd"] == 3.5
    assert len(tasks(db)) == before and writes.calls == []


def test_post_to_issue_posts_one_comment_without_persons_and_never_twice(db, ctx, writes) -> None:
    seed_work(db)
    register(db)
    schedule = report_schedule(db, params={"post_to_issue": f"{REPOSITORY}#7"})

    work = cost_report.create(firing_for(ctx, schedule, room=2))

    assert [(w["id"], w["outcome"]) for w in work] == [("report", "succeeded"), ("comment", "succeeded")]
    (comment,) = writes.on_issue(7)
    fid = schedulefire.firing_id(schedule["schedule_id"], SLOT)
    assert comment["body"].startswith(cost_report.marker(fid))
    assert "at least $3.50" in comment["body"] and "sch_sweep" in comment["body"]
    assert ALICE not in comment["body"] and BOB not in comment["body"]
    assert work[1]["detail"]["issue"] == f"{REPOSITORY}#7"

    # Finished again after a crash before the record: the marker finds it.
    again = cost_report.create(firing_for(ctx, schedule, room=2))
    assert len(writes.on_issue(7)) == 1 and again[1]["detail"]["already_posted"] is True


def test_a_refused_comment_is_a_failed_item_naming_the_permission(db, ctx, writes) -> None:
    seed_work(db)
    register(db)
    writes.status = {"POST": 403}

    work = cost_report.create(firing_for(ctx, report_schedule(db, params={"post_to_issue": f"{REPOSITORY}#7"}), room=2))

    report, comment = work
    assert report["outcome"] == "succeeded"
    assert comment["outcome"] == "failed" and comment["detail"]["code"] == "writeback_forbidden"
    assert "issues: write" in comment["detail"]["message"]
    # Two items: the firing is partial, not a quiet success.
    assert schedulefire.combine([w["outcome"] for w in work], independent=len(work) > 1) == "partial"


def test_a_tenant_without_a_forge_credential_is_reported_on_the_comment(db, tokens, group_map, objects,
                                                                          writes) -> None:
    seed_tenant(db, "eng")
    context = make_context(db, tokens, group_map, objects, writes=writes,
                           forge_tokens=forge_fakes.AnyTenantTokens(missing=("swarm-tenant-eng-git",)))
    register(db)

    work = cost_report.create(firing_for(context, report_schedule(db, params={"post_to_issue": f"{REPOSITORY}#7"}), room=2))

    assert work[1]["outcome"] == "failed" and work[1]["detail"]["code"] == forge.NoForgeCredential.code
    assert writes.calls == []


def test_an_unregistered_repository_is_not_written_to(db, ctx, writes) -> None:
    seed_work(db)

    work = cost_report.create(firing_for(ctx, report_schedule(db, params={"post_to_issue": "someone/else#3"}), room=2))

    assert work[1]["outcome"] == "failed"
    assert work[1]["detail"]["code"] == cost_report.REPOSITORY_NOT_REGISTERED
    assert writes.calls == []


def test_room_for_one_item_stores_the_report_and_says_why_nothing_was_posted(db, ctx, writes) -> None:
    register(db)
    schedule = report_schedule(db, params={"post_to_issue": f"{REPOSITORY}#7"})

    (only,) = cost_report.create(firing_for(ctx, schedule, room=1))

    assert only["id"] == "report" and only["detail"]["comment"]["code"] == cost_report.NO_ROOM
    assert writes.calls == []
    assert cost_report.create(firing_for(ctx, schedule, room=0)) == []


def test_the_platform_variant_is_aggregates_across_tenants_and_never_posted(db, ctx, writes) -> None:
    seed_work(db)
    register(db)
    schedule = report_schedule(db, scope_mode="platform", params={"post_to_issue": f"{REPOSITORY}#7"})

    report, comment = cost_report.create(firing_for(ctx, schedule, room=2))

    detail = report["detail"]
    assert detail["scope"] == "platform" and set(detail["by"]) == {"tenant", "profile"}
    assert by(detail, "tenant")["research"]["cost_usd"] == 99.0
    assert by(detail, "tenant")["eng"]["cost_usd"] == 3.5
    for private in (ALICE, BOB, "carol@saga.xyz", REPOSITORY, "sch_sweep"):
        assert private not in str(detail)
    assert comment["outcome"] == "failed" and comment["detail"]["code"] == cost_report.PLATFORM_NOT_POSTED
    assert writes.calls == []


def test_a_dry_run_records_nothing_writes_nothing_and_reads_no_credential(db, ctx, writes) -> None:
    seed_work(db)
    register(db)
    recorded: list[dict[str, Any]] = []
    schedule = report_schedule(db, params={"post_to_issue": f"{REPOSITORY}#7"})

    output = cost_report.dry_run(firing_for(ctx, schedule, room=2, recorded=recorded))

    assert output["report"]["total"]["cost_usd"] == 3.5
    assert output["comment"]["would"] == "post" and output["comment"]["bytes"] > 0
    assert recorded == [] and writes.calls == [] and ctx.forge_tokens.asked == []


def test_a_tick_records_the_report_as_the_firings_work(db, ctx) -> None:
    seed_work(db)
    report_schedule(db, repo_ids=(register(db),), cron="*/15 * * * *")

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    assert [(w["kind"], w["id"]) for w in firing["work"]] == [("api_action", "report")], firing
    assert sorted(t["id"] for t in tasks(db)) == ["r1", "t1", "t2", "t3"]  # no task made
