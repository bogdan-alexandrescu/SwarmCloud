"""Attempts across every task of the caller's tenant.

`GET /v1/tasks/{id}/attempts` answers "what happened on each try of THIS task".
Nothing answered "what did this tenant's runs cost" short of one request per
task -- the N+1 loop docs/web-ui/ui-audit-and-build-prompt.md §B9.S3 predicts
will otherwise be written in a browser. `Store.list_attempts` had accepted
`task_id=None` all along, and the `attempts-tenant-created` index was declared
for it; only a route was missing.

TENANT SCOPE (invariant 9). The tenant comes from `tenant_scope` -- the verified
identity, through the principal-collision check -- never from a path, a query
string or a header. A `tenant_id` in the query string is not a parameter here
and is ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from swarm_common.models import Attempt, Task
from swarm_common.profiles import RUNNER_PROFILES

from ..attempt_state import attempt_is_over
from ..codec import attempt_to_api
from ..deps import AppContext, get_context, paged_limit, tenant_scope
from ..errors import ValidationFailed
from ..task_input import masking_for

router = APIRouter(prefix="/v1/attempts", tags=["attempts"])


def _utc(moment: datetime | None) -> datetime | None:
    """A caller's timestamp with no offset is read as UTC, as every stored one is."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


#: The runner modules that report what an attempt cost: the two CLI runners,
#: which parse the provider's usage block (`agent_worker.runners.cliagent`).
#: `browser`, `generic` and the worker actions report no cost at all, by
#: design (docs/web-ui/ui-audit-and-build-prompt.md, "Spend coverage").
#:
#: Matched against each profile's `runner_argv` rather than a list of profile
#: names, so a profile added on one of these runners -- as `claude-code-review`
#: is on the claude-code runner -- owes a cost the moment the catalogue has it.
#: The catalogue itself does not say which runners report a cost; a
#: `RunnerProfile` flag for it would be a frozen-contract change, filed as
#: request 45 in docs/contract-change-requests.md. Until then a unit test holds
#: every module named here to a file under `agent_worker/runners`.
COST_REPORTING_RUNNERS: frozenset[str] = frozenset(
    {"agent_worker.runners.claude_code", "agent_worker.runners.codex"}
)


def profile_reports_cost(task: Task) -> bool | None:
    """Whether an attempt of `task` is expected to record a `cost_usd`.

    None when the task names a profile the catalogue does not hold: the API
    cannot say whether a cost was due, and does not guess.

    A profile that DECLARES its cost (`RunnerProfile.cost_declared`, the mock)
    reports one only when its input carries `spend.total_cost_usd`
    (`agent_worker/runners/mock.py`): asked to and silent is a gap, not asked
    is by design. `cost_declared` stands in for "the mock" here only because
    the mock is the one declared profile in the catalogue; a test pins that,
    so a second declared profile fails it instead of silently inheriting the
    mock's input rule.
    """
    profile = RUNNER_PROFILES.get(task.runner_profile)
    if profile is None:
        return None
    if profile.cost_declared:
        spend = task.input.get("spend") if isinstance(task.input, dict) else None
        cost = spend.get("total_cost_usd") if isinstance(spend, dict) else None
        return isinstance(cost, (int, float)) and not isinstance(cost, bool)
    return bool(profile.runner_argv) and profile.runner_argv[-1] in COST_REPORTING_RUNNERS


def attempt_has_ended(attempt: Attempt, task: Task | None) -> bool:
    """Whether `attempt` is over, `completed_at` or not.

    `completed_at` alone is not the answer: a worker hard-killed (OOM, SIGKILL
    after the grace period, node loss, a kill between `ControlPlane.finish` and
    `record_attempt_end`) leaves the attempt with no `completed_at` FOREVER, and
    those are exactly the attempts that spent and recorded nothing. So the
    judgement is `attempt_is_over`'s, the one `/answer` and the UI use: an
    attempt superseded by a newer generation, on a terminal task, or whose
    task no longer holds its lease, has ended.

    A row whose task is gone has ended too: nothing can hold a lease for a task
    that does not exist, so nothing is running it.
    """
    if attempt.completed_at is not None:
        return True
    if task is None:
        return True
    return attempt_is_over(attempt, task, is_latest=attempt.generation >= task.current_generation)


def spend_coverage(rows: Iterable[Attempt], tasks: Mapping[str, Task]) -> dict:
    """The `coverage` block over `rows`, one page. See `list_attempts`."""
    counts = dict.fromkeys(
        (
            "attempts",
            "finished",
            "with_spend",
            "in_flight_unreported",
            "not_reported_by_profile",
            "not_recorded",
            "profile_unknown",
        ),
        0,
    )
    for attempt in rows:
        counts["attempts"] += 1
        task = tasks.get(attempt.task_id)
        finished = attempt_has_ended(attempt, task)
        if finished:
            counts["finished"] += 1
        if attempt.cost_usd is not None:
            counts["with_spend"] += 1
            continue
        if not finished:
            counts["in_flight_unreported"] += 1
            continue
        owed = None if task is None else profile_reports_cost(task)
        if owed is None:
            counts["profile_unknown"] += 1
        elif owed:
            counts["not_recorded"] += 1
        else:
            counts["not_reported_by_profile"] += 1
    return {"scope": "page", **counts}


@router.get("")
def list_attempts(
    since: datetime | None = Query(default=None, description="created_at >= since"),
    until: datetime | None = Query(default=None, description="created_at < until"),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every attempt of the caller's tenant, newest first, paged.

    COVERAGE IS WHAT MAKES A COST FIGURE HONEST. A sum of `cost_usd` over rows
    where some costs were never reported is a lower bound that looks like a
    total. `coverage` says, over the rows on THIS page, how many carry a
    reported cost and what each of the others means -- so a caller can write
    "measured on 2 of 7; 2 still running, 2 on profiles that report no cost,
    1 not recorded" without a second pass (#72).

    `with_spend` counts `cost_usd is not None`, and only that:

      * $0.00 IS a measurement -- a `mock` run costs nothing on purpose -- and
        is counted;
      * an absent cost is "not reported" (`control.record_spend` omits a key
        the runner did not report rather than writing zero) and is not;
      * token counts without a cost do not make a COST measured, so they are
        not counted either. The token fields are served on every row for a
        caller that wants to count those instead.

    Every row WITHOUT a cost lands in exactly one of four counters, so

        with_spend + in_flight_unreported + not_reported_by_profile
                   + not_recorded + profile_unknown == attempts

      * `in_flight_unreported`: the attempt is still live -- `completed_at` is
        null AND its task still holds its lease at its generation
        (`attempt_has_ended`). It has not failed to report; it has not
        finished. A null `completed_at` on a task that is terminal, has moved
        to a newer generation or let the lease go is NOT in flight: that
        worker was killed without writing its end, and the row is judged by
        its profile like any finished one. (A running row that already
        carries a cost is in `with_spend`: spend is recorded before
        `completed_at`.)
      * `not_reported_by_profile`: finished, on a profile whose runner never
        reports a cost (`profile_reports_cost`) -- `generic`, `browser`, the
        worker actions, a `mock` not asked to. No cost is by design.
      * `not_recorded`: finished, on a profile that reports a cost, and none
        was recorded -- a CLI runner killed on SIGTERM writes nothing. THIS is
        the gap in a spend total; the other three are not. It is an UPPER
        BOUND on unrecorded spend, not a count of costly runs: an attempt that
        ended before the agent ran (a stale-generation fence exit, an input
        refusal before launch, a quota park before any usage) spent nothing
        and lands here too.
      * `profile_unknown`: finished, and its task is gone or names a profile
        the catalogue no longer holds, so whether a cost was due is unknown.

    `finished` counts rows that have ended (`attempt_has_ended`), with or
    without a cost -- so it can exceed the rows carrying a `completed_at`.

    `scope` is "page": every count is over this page's rows, never the
    window. A caller wanting "N of M this week" sums each counter across the
    pages; the counters add, the ratios do not.

    `since` is inclusive and `until` exclusive, so adjacent windows tile. The
    rows are `attempt_to_api`'s shape, the one `/v1/tasks/{id}/attempts`
    serves, so there is one serialiser for the five spend fields to be right in.
    """
    since, until = _utc(since), _utc(until)
    if since is not None and until is not None and since >= until:
        raise ValidationFailed(
            "since must be earlier than until",
            detail={"since": since.isoformat(), "until": until.isoformat()},
        )
    page = ctx.store.page_attempts(
        tenant_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
        since=since,
        until=until,
    )
    rows = page.items
    read_at = ctx.now()
    # Each row's `error` is masked by its OWN task's masker (the PR #229
    # review), as `/v1/tasks/{id}/attempts` masks it, so the two routes serve
    # one attempt the same way. One batched read for the page's tasks; a row
    # whose task is gone is masked by the rules alone.
    tasks = ctx.store.tasks_by_id(tenant_id, (attempt.task_id for attempt in rows))
    maskings = {task_id: masking_for(task) for task_id, task in tasks.items()}
    return {
        "tenant_id": tenant_id,
        # The clock each row's `cpu_reading_age_seconds` is taken against
        # (contract request #26).
        "read_at": read_at,
        "attempts": [
            attempt_to_api(attempt, masking=maskings.get(attempt.task_id), read_at=read_at)
            for attempt in rows
        ],
        "next_page_token": page.next_page_token,
        "coverage": spend_coverage(rows, tasks),
    }
