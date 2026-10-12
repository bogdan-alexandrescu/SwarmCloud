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
from typing import Any

import pytest

from swarm_api import schedulefire

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
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
