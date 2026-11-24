"""A lane head runs each check once, on its pull request (owner, 2026-11-02).

LANE CI-ONCE-PER-HEAD (part of archived issue 2688). LANE CI-EARLY (2026-10-20) added a
`push` trigger on `swarm/**` to application.yml, security.yml and
terraform.yml, so a lane head that also has a pull request ran about 100
checks twice, once per event. In the 14 days to 2 November 2026 merge steps
spent 352 hours parked on CI_PENDING along workflows' slowest chains; on 28
October one head's checks took 1.5 to 4 hours behind 23 to 28 queued merges
although no job ran over about 20 minutes. The owner chose to run CI once per
head: on the pull request.

What this holds: the merge step and swarm-api's check reading decide a head
that carries only pull_request runs (they never required a push run). The
pins that read the workflow YAML and docs/ci.md (one event per lane head,
main's push runs, every required check on every pull request) were deleted
by issue 71.
"""

from __future__ import annotations

import pytest


def _run(run_id: int, conclusion: str | None, status: str = "completed") -> dict:
    return {"status": status, "conclusion": conclusion, "run_id": run_id, "event": "pull_request"}


PULL_REQUEST_ONLY = [
    ([_run(1, "success")], "green"),
    ([_run(1, None, "in_progress")], "pending"),
    ([_run(1, "failure")], "failed"),
    # The up-to-date rule's re-test is a new head, but a re-run at one head
    # replaces the earlier run of its event.
    ([_run(1, "failure"), _run(2, "success")], "green"),
    ([], "pending"),
]


@pytest.mark.parametrize("runs, state", PULL_REQUEST_ONLY)
def test_the_merge_step_decides_a_head_with_only_pull_request_runs(runs, state):
    """Neither reader waits for a push run that no longer comes. MUTATION
    (merge.py or forgechecks.py): require a `push` event among the runs ->
    the green case reads pending."""
    # Imported here, not at module top: test_collection_stays_cheap holds every
    # module under tests/unit/scripts to collecting without the service stack.
    from agent_worker import merge
    from swarm_api import forgechecks

    assert merge.required_check_state(runs) == state
    # swarm-api names the worker's "failed" `RED`.
    assert forgechecks.required_check_state(runs) == {
        "green": forgechecks.GREEN, "pending": forgechecks.PENDING, "failed": forgechecks.RED}[state]
