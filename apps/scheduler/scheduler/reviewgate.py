"""The no-op precheck: a fix round for a pull request that already ended is not leased (#2524).

The token analysis of the week 2026-10-20..27 (#2524) found fix rounds
started for pull requests that had already merged. A merge step's appended
CI-fix, conflict-fix and pick-fix rounds (`swarm_api.mergewake`, metadata
`merge_fix_round`, or `merge_conflict_round` for a conflict round) are
checked at admission, before a lease (check (a)): when the merge task they
serve records that its pull request was merged or closed, the round ends
without a lease, CANCELLED with `result_summary.skipped.reason` (READY ->
SUCCEEDED is not a transition the frozen contract has). What is read is only
what mergewake's tick already wrote -- `merge_wait.wake_reason` from its
pull request read, cached per `issueci.CI_READ_SECONDS` -- or the merge
task's own SUCCEEDED; no GitHub call is added, and the scheduler holds no
forge token.

Check (b) (the branch head already holds a commit a review gave MERGE) needs
the clone and is the worker's (`agent_worker.verdict.merged_commit_contained`);
check (c) (a NOT_YET with no finding) is swarm-api's
(`swarm_api.verdictpublish.noop_verdict_reason`).

THE REVIEW GATE IS NOT DECIDED HERE. BGD, 2026-10-28: the scheduler holds a
gated review PARKED, as it holds `integrate`'s integrator for the #748
publish (`loop.held_for_control_publish`), and swarm-api decides it and ends
it PARKED -> SUCCEEDED (`swarm_api.reviewgate`). Only the metadata key is
restated here, for that hold.

THE PLATFORM'S WORDS ARE RESTATED, NOT SHARED, for the reason `runtime` says:
the scheduler image carries `swarm_common` and `quota_broker` only.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

#: The task metadata key a review gate lives under.
#: `swarm_api.validation.REVIEW_GATE_METADATA_KEY`, held equal by the test.
REVIEW_GATE_METADATA_KEY = "review_gate"
#: The block's flag naming an issue run's requirements check.
#: `swarm_api.validation.REVIEW_GATE_REQUIREMENTS_CHECK`, held equal by the test.
REVIEW_GATE_REQUIREMENTS_CHECK = "requirements_check"

#: The merge task's recorded pull-request endings. `swarm_api.mergewake`'s
#: `_Reading("merged"/"closed")` and `PULL_REQUEST_MERGED` / `_CLOSED`.
MERGE_FIX_ROUND_KEY = "merge_fix_round"
#: A conflict-fix round's block. `swarm_api.validation.CONFLICT_ROUND_METADATA_KEY`.
CONFLICT_ROUND_KEY = "merge_conflict_round"
ROUND_KEYS = (MERGE_FIX_ROUND_KEY, CONFLICT_ROUND_KEY)
MERGE_WAIT_KEY = "merge_wait"
PR_ENDED_REASONS: Mapping[str, str] = {
    "merged": "merged",
    "closed": "closed",
    "pull_request_merged": "merged",
    "pull_request_closed": "closed",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _round_block(metadata: Any) -> Mapping[str, Any]:
    for key in ROUND_KEYS:
        block = _mapping(_mapping(metadata).get(key))
        if block:
            return block
    return {}


def round_merge_task(metadata: Any) -> str | None:
    """The merge task a CI-fix, conflict-fix or pick-fix round serves, or None."""
    raw = _round_block(metadata).get("merge_task")
    return raw if isinstance(raw, str) and raw else None


def pull_request_ended(merge_task: Mapping[str, Any] | None) -> str | None:
    """`merged` / `closed` when the merge task records its pull request ended, else None.

    From the merge task only: SUCCEEDED (the merge step merged it, or saw it
    merged outside the workflow), or the ending mergewake's pull request read
    wrote into `merge_wait.wake_reason`.
    """
    if merge_task is None:
        return None
    if merge_task.get("state") == "SUCCEEDED":
        return "merged"
    reason = _mapping(_mapping(merge_task.get("metadata")).get(MERGE_WAIT_KEY)).get("wake_reason")
    return PR_ENDED_REASONS.get(reason) if isinstance(reason, str) else None


def noop_round_reason(
    metadata: Any, read: Callable[[str], Mapping[str, Any] | None],
) -> str | None:
    """Why a fix round would buy nothing, or None when it runs (check (a))."""
    merge_task = round_merge_task(metadata)
    if merge_task is None:
        return None
    ended = pull_request_ended(read(merge_task))
    if ended is None:
        return None
    number = _round_block(metadata).get("pull_request")
    which = f"pull request #{number}" if isinstance(number, int) else "its pull request"
    return f"{which} is already {ended}, so the fix round has nothing to fix"


def end_if_noop(store: Any, task: Any) -> Any | None:
    """End a READY fix round whose pull request already ended, before a lease.

    Called from admission (`Scheduler._admit_one`). None when the round runs;
    else the store's `GuardedWrite` for the READY -> CANCELLED write.
    """
    reason = noop_round_reason(
        getattr(task, "metadata", None),
        lambda task_id: store.task_document(task_id, tenant_id=task.tenant_id),
    )
    if reason is None or getattr(getattr(task, "state", None), "value", None) != "READY":
        return None
    return store.skip_before_lease(task, reason)


#: The ends of a requirements check that are not a failure of its run.
_CHECK_ENDS_WITHOUT_LIST = ("FAILED", "DEAD_LETTERED")


def is_requirements_check(metadata: Any) -> bool:
    """`swarm_api.reviewgate.is_requirements_check`, restated: an issue run's
    cheap requirements check, which stands beside the gated review (#2524)."""
    block = _mapping(_mapping(metadata).get(REVIEW_GATE_METADATA_KEY))
    return block.get(REVIEW_GATE_REQUIREMENTS_CHECK) is True


def check_ended_without_list(state: Any, requirements_check: bool) -> bool:
    """A requirements check that FAILED or was DEAD_LETTERED: it confirms nothing.

    A FAILED CHECK IS NOT A FAILED RUN (#2524, the review of REVIEW-GATE-DEFAULT).
    The check only confirms requirements; it neither reviews nor changes code.
    When it times out, runs out of attempts or errors, it reads as "no list":
    the review beside it still ends as the gate decides, and the pull request
    says "part of #N" (`swarm_api.verdictpublish`). So the dependency rule does
    not cancel the review over it, and `fail_workflow` does not count it. A
    check CANCELLED by somebody's stop is still a cancel, and still cascades.
    """
    value = getattr(state, "value", state)
    return requirements_check and value in _CHECK_ENDS_WITHOUT_LIST
