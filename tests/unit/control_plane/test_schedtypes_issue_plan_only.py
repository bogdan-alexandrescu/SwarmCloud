"""The `issue-plan-only` schedule type (docs/schedules.md §3.2, lane S6).

WHAT IS HELD HERE
-----------------
* Every run it makes waits for a person: `plan_approval: required` whatever
  the stored gate says, so nothing executes until a member approves the plan.
* `max_pending_plans`: a new plan is not made while that many of THIS
  schedule's runs wait; another schedule's runs do not count against it.
* The selection is `issue-sweep`'s, which is SWEEP's.
* A dry run creates nothing.
"""

from __future__ import annotations

from swarm_api import schedulefire, scheduletypes
from swarm_api.issueruns import IssueRuns, RunState
from swarm_api.schedtypes import issue_plan_only, issue_sweep

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    REPOSITORY,
    ctx,
    firing_for,
    github,
    issue,
    issue_runs,
    make_schedule,
    repo_id,
    started,
    tasks,
    writes,
)


def plan_only(db, repo_id, **kwargs):
    return make_schedule(db, type_="issue-plan-only", repo_ids=(repo_id,), **kwargs)


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("issue-plan-only")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is issue_plan_only


def test_every_run_waits_for_a_person_whatever_the_stored_gate_says(db, ctx, repo_id) -> None:
    # A gate below the floor can only be a stored record gone wrong; the
    # executor does not rely on the floor to keep the plan waiting.
    schedule = plan_only(db, repo_id, gate={"plan": "auto"})

    work = issue_plan_only.create(firing_for(ctx, schedule))

    (run,) = issue_runs(db)
    assert work == [{"kind": "issue_run", "id": run["id"], "repo_id": repo_id}]
    assert run["plan_approval"] == "required"
    assert run["metadata"]["schedule"]["type"] == "issue-plan-only"
    # The merge, once a plan is approved, waits in the inbox (the floor).
    assert run["auto_merge"] is True and run["merge_approval"] == "required"
    # Planning is the only work: the planner, waiting for admission.
    (planner,) = tasks(db)
    assert planner["id"] == run["planner_task_id"] and planner["state"] in ("QUEUED", "READY")


def test_merge_off_opens_and_never_merges(db, ctx, repo_id) -> None:
    issue_plan_only.create(firing_for(ctx, plan_only(db, repo_id, gate={"merge": "off"})))
    (run,) = issue_runs(db)
    assert run["auto_merge"] is False and run["merge_approval"] is None


def test_no_new_plan_while_max_pending_plans_wait(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 8)]
    schedule = plan_only(db, repo_id, params={"max_pending_plans": 2, "max_new_per_firing": 8})

    first = issue_plan_only.create(firing_for(ctx, schedule))
    # One of them reaches PLANNED: it still waits, and still counts.
    IssueRuns(db).transition("eng", first[0]["id"], RunState.PLANNED, by="t")
    second = issue_plan_only.create(firing_for(ctx, schedule, fid="sch_000000000001:next"))

    assert started(first, db) == [1, 2]
    assert second == []
    preview = issue_plan_only.dry_run(firing_for(ctx, schedule, fid="sch_000000000001:peek"))
    assert preview["limits"]["max_pending_plans"] == 0 and preview["would_start"] == []


def test_a_decided_plan_frees_its_place(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 4)]
    schedule = plan_only(db, repo_id, params={"max_pending_plans": 1})
    (first,) = issue_plan_only.create(firing_for(ctx, schedule))
    IssueRuns(db).transition("eng", first["id"], RunState.CANCELLED, by="t")

    second = issue_plan_only.create(firing_for(ctx, schedule, fid="sch_000000000001:next"))

    assert started(second, db) == [2]


def test_another_schedules_waiting_plans_do_not_count(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 4)]
    other = make_schedule(db, type_="issue-sweep", repo_ids=(repo_id,), schedule_id="sch_000000000009",
                          gate={"plan": "approve"}, params={"max_new_per_firing": 1})
    issue_sweep.create(firing_for(ctx, other))
    schedule = plan_only(db, repo_id, params={"max_pending_plans": 1})

    work = issue_plan_only.create(firing_for(ctx, schedule))

    assert started(work, db) == [2]


def test_the_selection_is_sweeps(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1, labels=("security",)), issue(2, author="CONTRIBUTOR"), issue(3)]

    preview = issue_plan_only.dry_run(firing_for(ctx, plan_only(db, repo_id)))

    assert [row["issue"] for row in preview["would_start"]] == [f"{REPOSITORY}#3"]
    assert {row["reason"] for row in preview["passed_over"]} == {"label: security", "author: CONTRIBUTOR"}
    assert preview["runs"]["plan_approval"] == "required"
    assert issue_runs(db) == [] and tasks(db) == []
