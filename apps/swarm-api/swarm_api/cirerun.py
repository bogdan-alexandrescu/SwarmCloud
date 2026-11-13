"""The re-run of a red head's failed Actions jobs, before any agent is spent on it.

A FLAKE COSTS NO AGENT. A red reading whose failing checks are all Actions
jobs may be a flake -- a 30 s vitest timeout in a shard the change never
touched -- and a re-run of the failed jobs pushes nothing and costs no
round. Two callers ask for one, once per head, before they spend or refuse
a CI-fix round:

* the merge wake tick (`mergewake._rerun_first`, SPEED-D2, #2841), for a
  merge step, recording it in the task's `merge_fix.reruns`;
* an issue run in CHECKING (`issueci._rerun_red_head`, #3200: run_7eddc5b3
  on PR #3150 failed at its cap of 3 on one flaky swarm-ui shard of a pull
  request that touched no swarm-ui file, and nothing was ever re-run),
  recording it in the run's `pull_request.reruns`.

This module is the rule they share, so neither restates it: which failing
checks a re-run can reach (`rerunnable`), what to ask GitHub to re-run
(`rerun_targets`), whether an accepted re-run is still running
(`rerun_running`), and the bounds. Each caller keeps its own bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .forgechecks import CiReading, workflow_run_of
from .validation import IssueRef

log = logging.getLogger(__name__)

#: How long after a re-run was requested the reading keeps the same failed
#: check runs as "GitHub has not made the new ones yet" rather than as the
#: re-run's answer. Ten minutes: GitHub queues a re-run in seconds, and past
#: this a re-run that never registered gives way to a round -- unless a
#: workflow run it asked of still reads not `completed` (#3006), which holds
#: on; the window is the floor when that status cannot be read.
RERUN_SETTLE_SECONDS = 600
#: The re-run requests GitHub may refuse at one head before the caller goes
#: on without one (#3006): a refused re-run ran nothing, so it may be asked
#: once more, and a credential without `actions: write` costs one request.
RERUN_REFUSALS_MAX = 2
#: The re-runs GitHub may accept at one head (#3080, owner decision
#: 2026-11-12). A head gets one free re-run; a second is earned only after a
#: CI-fix round ended there `no_change` (it read the red as a flake) while
#: the first re-run had already answered red on other jobs -- PR #3049 at
#: dd72fa3e and PR #3055 at d82d2635 refused `checks_failed` on a second
#: flake and later merged unchanged. Two, so a persistent red cannot loop.
RERUNS_PER_HEAD_MAX = 2
#: The App whose check runs are Actions jobs, the only ones a re-run reaches.
ACTIONS_APP_SLUG = "github-actions"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def rerunnable(reading: CiReading | None) -> list[int]:
    """The failing Actions jobs a re-run could turn green, or [] when it could not.

    Every failing check must be an Actions job: a red commit status or
    another App's check run is not re-run by this, so re-running the rest
    could not make the reading green and would only delay the round.
    """
    if reading is None or reading.failing_statuses or not reading.failing_runs:
        return []
    jobs: list[int] = []
    for run in reading.failing_runs:
        job = run.get("id")
        app = _mapping(run.get("app"))
        if (app.get("slug") != ACTIONS_APP_SLUG or not isinstance(job, int)
                or isinstance(job, bool)):
            return []
        jobs.append(job)
    return sorted(set(jobs))


def rerun_targets(reading: CiReading | None, jobs: list[int]) -> tuple[list[int], list[int]]:
    """(the workflow runs whose failed jobs to re-run, the jobs to re-run alone).

    A failing job whose run can be resolved is re-run through its run, which
    re-runs its failed needs too (#2841); the rest as their own job.
    """
    runs: set[int] = set()
    alone: set[int] = set()
    by_id = {r.get("id"): r for r in (reading.failing_runs if reading else [])}
    for job in jobs:
        run_id = workflow_run_of(by_id.get(job) or {})
        if run_id is None:
            alone.add(job)
        else:
            runs.add(run_id)
    return sorted(runs), sorted(alone)


def rerun_running(writer: Any, ref: IssueRef, entry: Mapping[str, Any], token: str) -> bool:
    """True while a workflow run an accepted re-run asked of has a latest
    attempt that has not completed (#3006: a fan-in `ci-gate` run still
    re-running its needs past RERUN_SETTLE_SECONDS). A run whose status
    cannot be read counts as answered: the window alone holds then."""
    for run_id in entry.get("runs") or []:
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        try:
            status = writer.read_run(ref, run_id, token).get("status")
        except Exception as exc:  # noqa: BLE001 - an unread run falls back to the window
            log.info("ci re-run: the status of re-run Actions run %d is unread (%s)",
                     run_id, type(exc).__name__)
            continue
        if status != "completed":
            return True
    return False
