"""A real `issue-sweep` firing starts what its dry run would start, and never succeeds silently (#1133).

WHAT WENT WRONG
---------------
`sch_6ecbe1307511` fired four times on 2026-10-11 and every firing ended
`done / succeeded` with `work: []` and no `skip`, while a run-now dry run on
the same `params_digest` listed one issue in `would_start`. The executor's
`create` threw its selection away -- the per-issue `passed_over` reasons, the
repositories it could not read, the room and what bounded it -- and the tick
read an empty list as "nothing was due", so a firing that started nothing
looked exactly like one that had nothing to start, and nobody could say which.

WHAT IS HELD HERE
-----------------
* PARITY. A dry run and a real firing on the same params and the same
  repository state choose the same issues: the dry run's `would_start` is
  what the tick starts and records in `work` (a fixed fixture, and a seeded
  sweep of generated ones).
* A REASON, ALWAYS. A real firing that creates nothing carries a `skip` --
  `NOTHING_STARTED`, with the dry run's per-issue reasons, failures, room and
  limits -- whatever the executor; and every real issue-sweep firing records
  the selection it made (`selection`), so a firing can be compared with its
  dry run after the fact.
* THE GATES ARE UNCHANGED: the default gate's run plans automatically and
  asks before it merges.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real client, as `test_schedtypes_issue_sweep.py`.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

import pytest

from swarm_api import schedulefire
from swarm_api.issueruns import IssueRuns, RunState
from swarm_api.schedtypes import issue_sweep
from swarm_api.schemas import WorkflowCreate

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    NOW,
    REPOSITORY,
    ctx,
    firings,
    github,
    issue,
    issue_runs,
    repo_id,
    sweep,
    writes,
)


def _dry_run(ctx, schedule: dict[str, Any]) -> dict[str, Any]:
    firing = schedulefire.Ticker(ctx).fire_now(schedule, actor=ALICE, dry_run=True)
    assert firing["skip"]["code"] == schedulefire.DRY_RUN, firing
    return firing["dry_run"]


def _real(ctx, db) -> dict[str, Any]:
    """The cron firing a tick makes: the one firing that is not a dry run."""
    report = schedulefire.Ticker(ctx).tick()
    assert report.fired == 1 and report.errors == 0, report.to_api()
    (firing,) = [f for f in firings(db).values() if f["trigger"] != "run_now"]
    return firing


def _started(firing: dict[str, Any], db) -> list[str]:
    by_id = {d["id"]: d for d in issue_runs(db)}
    return [f"{REPOSITORY}#{by_id[w['id']]['issue']['number']}" for w in firing["work"]]


# ---------------------------------------------------------------------------
# (a) the dry run's issue is the real firing's
# ---------------------------------------------------------------------------


def test_a_real_firing_starts_the_issue_its_dry_run_would_start(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1109)]
    schedule = sweep(db, repo_id)

    preview = _dry_run(ctx, schedule)
    assert [row["issue"] for row in preview["would_start"]] == [f"{REPOSITORY}#1109"]
    assert issue_runs(db) == []

    firing = _real(ctx, db)

    (run,) = issue_runs(db)
    assert run["issue"]["number"] == 1109
    assert firing["state"] == "created" and firing["skip"] is None
    assert [w["id"] for w in firing["work"]] == [run["id"]]
    # The firing records the selection it made, in the dry run's shape.
    assert [row["issue"] for row in firing["selection"]["would_start"]] == [f"{REPOSITORY}#1109"]
    assert firing["selection"]["started"] == [{"issue": f"{REPOSITORY}#1109", "run_id": run["id"]}]
    # Gates unchanged: the plan is automatic, the merge asks first.
    assert run["plan_approval"] == "auto"
    assert run["auto_merge"] is True and run["merge_approval"] == "required"


# ---------------------------------------------------------------------------
# (b) a firing that starts nothing says why
# ---------------------------------------------------------------------------


def test_a_firing_with_no_eligible_issue_records_each_issues_reason(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1, labels=("blocked",)), issue(2, author="NONE")]
    sweep(db, repo_id)

    firing = _real(ctx, db)

    assert issue_runs(db) == [] and firing["work"] == []
    assert firing["state"] == "done" and firing["outcome"] == "succeeded"
    skip = firing["skip"]
    assert skip is not None and skip["code"] == schedulefire.NOTHING_STARTED
    assert skip["detail"]["reason"] == "no_eligible_issue"
    assert skip["detail"]["passed_over_by_reason"] == {"author": 1, "label": 1}
    reasons = {row["issue"]: row["reason"] for row in firing["selection"]["passed_over"]}
    assert reasons == {f"{REPOSITORY}#1": "label: blocked", f"{REPOSITORY}#2": "author: NONE"}


def test_a_firing_whose_repository_could_not_be_read_says_so(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1)]
    github.status = {"/issues": 502}
    sweep(db, repo_id)

    firing = _real(ctx, db)

    assert issue_runs(db) == [] and firing["work"] == []
    skip = firing["skip"]
    assert skip is not None and skip["code"] == schedulefire.NOTHING_STARTED
    assert skip["detail"]["reason"] == "repositories_not_read"
    assert [f["repository"] for f in skip["detail"]["failures"]] == [REPOSITORY]


def test_a_firing_with_no_room_names_the_bound(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1)]
    sweep(db, repo_id, max_concurrent=0)

    firing = _real(ctx, db)

    assert issue_runs(db) == [] and firing["work"] == []
    skip = firing["skip"]
    assert skip["code"] == schedulefire.NOTHING_STARTED
    assert skip["detail"]["reason"] == "no_room" and skip["detail"]["bound"] == "max_concurrent"
    assert skip["detail"]["passed_over_by_reason"] == {"room": 1}


def test_any_executor_that_creates_nothing_gets_a_skip(db, ctx, repo_id) -> None:
    """The tick's own guarantee: an empty `create` with no explanation is still explained."""

    class Silent:
        @staticmethod
        def create(firing: Any) -> list[dict[str, Any]]:
            return []

        @staticmethod
        def dry_run(firing: Any) -> dict[str, Any]:
            return {}

    sweep(db, repo_id)
    schedulefire.Ticker(ctx, executors={"issue-sweep": Silent}).tick()

    (firing,) = firings(db).values()
    assert firing["work"] == [] and firing["outcome"] == "succeeded"
    assert firing["skip"]["code"] == schedulefire.NOTHING_STARTED
    assert firing["skip"]["detail"]["reason"] == "executor_created_nothing"


# ---------------------------------------------------------------------------
# (c) for any fixture, the real firing starts exactly the dry run's set
# ---------------------------------------------------------------------------

_LABELS = ("bug", "ready", "blocked", "epic", "security", "wontfix", "docs")
_AUTHORS = ("MEMBER", "OWNER", "COLLABORATOR", "NONE", "CONTRIBUTOR")


def _fixture(seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int]:
    rng = random.Random(seed)
    numbers = rng.sample(range(1, 400), rng.randint(0, 9))
    issues = [
        issue(n, minutes_ago=rng.randint(1, 5000),
              labels=tuple(rng.sample(_LABELS, rng.randint(0, 2))),
              author=rng.choice(_AUTHORS))
        for n in numbers
    ]
    claimed = [n for n in numbers if rng.random() < 0.2]
    pulls = [{"number": 900 + i, "title": "Change", "body": f"Closes #{n}", "draft": False,
              "head": {"ref": f"b{i}"}, "user": {"login": "x"}} for i, n in enumerate(claimed)]
    params: dict[str, Any] = {
        "max_new_per_firing": rng.randint(1, 4),
        "max_live_runs": rng.randint(1, 8),
    }
    if rng.random() < 0.3:
        params["labels_include"] = ["ready"]
    return issues, pulls, params, rng.randint(0, 5)


@pytest.mark.parametrize("seed", range(40))
def test_the_real_firing_starts_exactly_the_dry_runs_set(db, ctx, repo_id, github, seed) -> None:
    issues, pulls, params, max_concurrent = _fixture(seed)
    github.issues, github.pulls = issues, pulls
    schedule = sweep(db, repo_id, params=params, max_concurrent=max_concurrent)

    preview = _dry_run(ctx, schedule)
    firing = _real(ctx, db)

    would = [row["issue"] for row in preview["would_start"]]
    assert len(would) <= min(params["max_new_per_firing"], params["max_live_runs"], max_concurrent)
    assert _started(firing, db) == would
    assert len(issue_runs(db)) == len(would)
    if not would:
        assert firing["skip"] is not None and firing["skip"]["code"] == schedulefire.NOTHING_STARTED
        assert firing["skip"]["detail"]["reason"] in (
            "no_eligible_issue", "no_room", "repositories_not_read", "no_open_issue",
        )
    else:
        assert firing["skip"] is None


# ---------------------------------------------------------------------------
# (d) work already under way on an issue passes it over (2026-10-12)
# ---------------------------------------------------------------------------
#
# Firing sch_6ecbe1307511:29862877 took #1030 for idle -- no open pull request
# at that moment -- while ~25 workflows titled "... (LB-Lx; part of #1030)"
# were building it, and its run was refused at planning (WORKFLOWS_PATH),
# which made the FIRING `failed`. The same day it opened PR 1136 beside a
# lane's PR 1134 for #1133.


def _workflow(ctx, schedule: dict[str, Any], *, title: str | None = None,
              metadata: dict[str, Any] | None = None) -> str:
    """A live (QUEUED) workflow of the tenant, submitted as a member would."""
    meta = dict(metadata or {})
    if title is not None:
        meta["title"] = title
    owner = schedulefire.schedule_owner_auth(ctx, schedule)
    result = ctx.submissions.submit_workflow(owner, WorkflowCreate(
        steps=[{"step_id": "only", "runner_profile": "mock", "input": {"prompt": "build"}}],
        metadata=meta,
    ))
    return result.workflow.workflow_id


def _reasons(selection: dict[str, Any]) -> dict[str, str]:
    return {row["issue"]: row["reason"] for row in selection["passed_over"]}


@pytest.mark.parametrize("title", [
    "A viewer signs in on the live browser (LB-L13; part of #7)",
    "Live browser slot leases (#7)",
    "Wire #7 into acceptance",
])
def test_an_issue_a_running_workflow_names_is_passed_over_as_in_flight(
    db, ctx, repo_id, github, title,
) -> None:
    github.issues = [issue(7), issue(70)]
    schedule = sweep(db, repo_id)
    workflow_id = _workflow(ctx, schedule, title=title)

    preview = _dry_run(ctx, schedule)
    firing = _real(ctx, db)

    reason = f"in_flight_workflow: {workflow_id}"
    assert _reasons(preview)[f"{REPOSITORY}#7"] == reason
    assert _reasons(firing["selection"])[f"{REPOSITORY}#7"] == reason
    # #70 is not #7: a mention is bounded by non-digits.
    assert [row["issue"] for row in preview["would_start"]] == [f"{REPOSITORY}#70"]
    assert _started(firing, db) == [f"{REPOSITORY}#70"]
    assert preview["in_flight"]["workflows_read"] == 1
    assert preview["in_flight"]["workflow_scan"] == issue_sweep.WORKFLOW_SCAN


def test_an_issue_run_link_on_a_live_workflow_passes_the_issue_over(db, ctx, repo_id, github) -> None:
    github.issues = [issue(8)]
    schedule = sweep(db, repo_id)
    workflow_id = _workflow(ctx, schedule, metadata={
        "issue_run": {"run_id": "run_elsewhere", "issue": f"{REPOSITORY}#8"},
    })

    firing = _real(ctx, db)

    assert issue_runs(db) == []
    assert _reasons(firing["selection"]) == {f"{REPOSITORY}#8": f"in_flight_workflow: {workflow_id}"}
    assert firing["skip"]["detail"]["passed_over_by_reason"] == {"in_flight_workflow": 1}


def test_a_finished_workflow_does_not_hold_its_issue(db, ctx, repo_id, github) -> None:
    github.issues = [issue(9)]
    schedule = sweep(db, repo_id)
    workflow_id = _workflow(ctx, schedule, title="Done already (part of #9)")
    for path, doc in list(db.docs.items()):
        if path.startswith("tasks/") and doc.get("workflow_id") == workflow_id:
            doc["state"] = "SUCCEEDED"
    db.docs[f"workflows/{workflow_id}"]["state"] = "SUCCEEDED"

    firing = _real(ctx, db)

    assert _started(firing, db) == [f"{REPOSITORY}#9"]


def test_a_pull_request_merged_within_the_cooldown_passes_its_issue_over(db, ctx, repo_id, github) -> None:
    github.issues = [issue(11), issue(12)]
    merged = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    stale = (NOW - timedelta(hours=200)).isoformat().replace("+00:00", "Z")
    github.pulls = [
        {"number": 1148, "state": "closed", "title": "A viewer signs in (LB-L13; part of #11)",
         "body": "", "merged_at": merged, "updated_at": merged, "head": {"ref": "a"}},
        {"number": 900, "state": "closed", "title": "Older work (part of #12)",
         "body": "", "merged_at": stale, "updated_at": stale, "head": {"ref": "b"}},
    ]
    schedule = sweep(db, repo_id, params={"cooldown_hours": 72})

    preview = _dry_run(ctx, schedule)
    firing = _real(ctx, db)

    assert _reasons(preview) == {f"{REPOSITORY}#11": "recently_worked: PR 1148"}
    assert _reasons(firing["selection"]) == _reasons(preview)
    assert _started(firing, db) == [f"{REPOSITORY}#12"]


# ---------------------------------------------------------------------------
# (e) a run refused at planning for what its issue asks does not fail the firing
# ---------------------------------------------------------------------------


def _end_run(db, ctx, run_id: str, error: str) -> None:
    IssueRuns(db, now=ctx.now).transition("eng", run_id, RunState.FAILED, by="system",
                                          patch={"error": error})


def _advanced(ctx, db) -> dict[str, Any]:
    schedulefire.Ticker(ctx).tick()
    (firing,) = [f for f in firings(db).values() if f["trigger"] != "run_now"]
    return firing


def test_a_workflows_path_refusal_leaves_the_firing_succeeded_with_a_refused_run(
    db, ctx, repo_id, github,
) -> None:
    github.issues = [issue(1030)]
    sweep(db, repo_id)
    firing = _real(ctx, db)
    (run,) = issue_runs(db)
    assert firing["state"] == "created"

    _end_run(db, ctx, run["id"], "WORKFLOWS_PATH: the plan changes .github/workflows/accept.yml. "
                                 "SwarmCloud's forge credential cannot push workflow files")
    firing = _advanced(ctx, db)

    assert firing["state"] == "done" and firing["outcome"] == "succeeded", firing
    assert firing["refused_runs"] == [
        {"run_id": run["id"], "issue": f"{REPOSITORY}#1030", "code": "WORKFLOWS_PATH"},
    ]
    schedule = db.docs[f"schedules/{firing['schedule_id']}"]
    assert schedule["consecutive_failures"] == 0
    assert schedule["last_firing"]["outcome"] == "succeeded"


def test_a_refused_issue_is_passed_over_as_refused_until_it_changes(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1030, minutes_ago=600)]
    schedule = sweep(db, repo_id)
    _real(ctx, db)
    (run,) = issue_runs(db)
    _end_run(db, ctx, run["id"], "WORKFLOWS_PATH: the plan changes .github/workflows/accept.yml")

    preview = _dry_run(ctx, schedule)

    assert _reasons(preview) == {
        f"{REPOSITORY}#1030": f"unchanged_since_run: {run['id']} (REFUSED: WORKFLOWS_PATH)",
    }


def test_a_run_that_really_failed_still_fails_the_firing(db, ctx, repo_id, github) -> None:
    github.issues = [issue(5)]
    sweep(db, repo_id)
    _real(ctx, db)
    (run,) = issue_runs(db)

    _end_run(db, ctx, run["id"], "the planner task t_1 ended FAILED: RuntimeError")
    firing = _advanced(ctx, db)

    assert firing["outcome"] == "failed"
    assert "refused_runs" not in firing


def test_a_workflows_path_error_after_planning_is_not_a_refusal(db, ctx, repo_id, github) -> None:
    """Only a run that never left PLANNING is refused for its issue."""
    github.issues = [issue(6)]
    sweep(db, repo_id)
    _real(ctx, db)
    (run,) = issue_runs(db)
    db.docs[f"issue_runs/{run['id']}"]["history"].append(
        {"at": NOW, "from": "PLANNING", "to": "PLANNED", "by": "system"})
    db.docs[f"issue_runs/{run['id']}"]["history"].append(
        {"at": NOW, "from": "PLANNED", "to": "FAILED", "by": "system"})
    db.docs[f"issue_runs/{run['id']}"].update({"state": "FAILED", "error": "WORKFLOWS_PATH: late"})

    firing = _advanced(ctx, db)

    assert firing["outcome"] == "failed"


def test_an_executor_exception_still_ends_the_firing_unsucceeded(db, ctx, repo_id) -> None:
    class Broken:
        @staticmethod
        def create(firing: Any) -> list[dict[str, Any]]:
            raise RuntimeError("the forge fell over")

        @staticmethod
        def dry_run(firing: Any) -> dict[str, Any]:
            return {}

    sweep(db, repo_id)
    report = schedulefire.Ticker(ctx, executors={"issue-sweep": Broken}).tick()

    assert report.errors == 1
    (firing,) = firings(db).values()
    assert firing.get("outcome") != "succeeded"
