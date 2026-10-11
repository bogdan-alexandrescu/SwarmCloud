"""`issue-plan-only`: plan the ready issues and wait for a person (docs/schedules.md §3.2, lane S6).

THE SAME SELECTION AS `issue-sweep` (`issue_sweep.select`, which is SWEEP's
`issuesweep.collect_candidates`), with `plan_approval: required` FORCED,
whatever the stored gate says: the gate's floor for this type is `approve
plan`, and this module does not rely on the floor alone. Each run's plan
waits in the inbox (§4.5 projects every PLANNED run), and approving it is
the issue run's existing approve. Nothing executes until a person approves.

`max_pending_plans` (default 5): a new plan is not made while this many of
THIS schedule's runs are waiting -- PLANNED, or still PLANNING and about to
be. Another schedule's or a person's runs do not count against it; they
count, as every live run does, against nothing here but the overlap rule
that skips an issue with a live run.

The room is the least of `max_new_per_firing`, `max_pending_plans` less this
schedule's waiting runs, and `max_concurrent` less this schedule's live work.
Adoption, refusals and the dry run are `issue_sweep`'s.

THE MERGE, ONCE A PLAN IS APPROVED, follows the gate's `merge` point, which
for this type is never below `approve` (§3 table): the pull request waits in
the inbox, or with `merge: off` is opened and never merged.
"""

from __future__ import annotations

from typing import Any

from ..issueruns import IssueRun, RunState
from . import issue_sweep

TYPE = "issue-plan-only"

#: A run of this schedule in either state is a plan made or being made, and
#: waits for a person before anything executes.
WAITING = frozenset({RunState.PLANNING, RunState.PLANNED})


def waiting_plans(live_rows: list[IssueRun], schedule_id: str) -> int:
    return sum(
        1 for row in live_rows
        if row.state in WAITING and (row.schedule or {}).get("schedule_id") == schedule_id
    )


def _limits(firing: Any, params: Any) -> issue_sweep.Limits:
    schedule_id = str(firing.schedule["schedule_id"])

    def bounds(live_rows: list[IssueRun], adopted: list[IssueRun]) -> dict[str, int]:
        # Adopted runs are this schedule's live runs, so they are already in
        # the waiting count; they still spend the firing's own allowances.
        return {
            "max_new_per_firing": params.max_new_per_firing - len(adopted),
            "max_pending_plans": params.max_pending_plans - waiting_plans(live_rows, schedule_id),
            "max_concurrent": firing.room - len(adopted),
        }
    return bounds


def _options(firing: Any, params: Any) -> issue_sweep.RunOptions:
    return issue_sweep.run_options(firing.schedule, params.fix_rounds, plan_required=True)


def _select(firing: Any, params: Any) -> issue_sweep.Selection:
    return issue_sweep.select(firing, params, limits=_limits(firing, params),
                              max_live_runs=params.max_pending_plans)


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    return issue_sweep.start(firing, _select(firing, params), _options(firing, params))


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    return issue_sweep.describe(_select(firing, params), _options(firing, params))
