"""Is a pull request's CI green at its head? The pure half of #454's CI loop.

An issue run that has opened its pull request is DONE only when every
required check is green at the pull request's CURRENT head sha, and is given
a fix round when one is red (`issueci`). This module turns what GitHub lists
at a sha -- the branch's rules, the check runs, the commit statuses -- into
one reading: `green`, `pending`, `red`, or `none` (nothing required and
nothing reported). It reads nothing itself; `forgewrite.GitHubWriter` is the
wire.

THE LOGIC IS THE MERGE STEP'S, PORTED, NOT IMPORTED. The worker's merge
action (`agent_worker.merge`, docs/merge-step.md §5.2) already decides "are
the required checks green at this sha", and two answers to that question
that drift apart would let a run report green on a pull request the merge
then refuses. swarm-api cannot import the worker, so the minimal pure parts
are restated here and held to the same rules:

  * the required set is every `required_status_checks` rule's checks, once
    each, with the App each is pinned to (`integration_id`);
  * a required check's runs are the LATEST of that name (`filter=latest`)
    by its pinned App, when it has one -- a check of the same name from
    another App does not satisfy it;
  * a required check is decided by the LATEST run of EACH event that ran
    it (BGD, 2026-10-22; CHECK-RULE, part of #1999 and #1778): a re-run
    replaces the earlier runs of its own event, and every event's latest
    run must be green, so a red push run is never outvoted by a green
    pull_request run (CI-EARLY, #1694). The event is what
    `forgewrite.GitHubWriter.check_runs` adds to each run; without one a
    run's check suite is its group. tests/unit/control_plane/
    test_forgechecks_per_event.py runs this rule and the merge step's over
    one table;
  * green is `success` or `skipped`, nothing else, `neutral` included;
  * no runs, or an event's latest run not completed, is pending; so is a
    check whose every latest run not green is `cancelled` (#1778): a run
    someone stopped waits for its re-run, and red in another event wins;
  * a check run that finished other than `success` inside an Actions run that concluded
    `cancelled` is read as `cancelled` when a newer run of its workflow and
    event is at the sha or another event passed the check
    (`cancelled_by_its_workflow`, #2582). The Actions run's conclusion is
    what `mark_workflow_runs` puts on each check run.

WHERE THIS IS DELIBERATELY WIDER than the merge step:

  * A required check pinned to no App that no check run reports is looked
    for among the commit STATUSES by its context (`success` green, `pending` pending, `failure`
    and `error` red): a CI that reports through the statuses API is still
    CI. The merge refuses such a check as unpinned; that is the merge's
    stricter call to make, not a reason for a run to wait for ever.
  * With NO required checks -- a repository on classic branch protection,
    which the rules endpoint does not list, or none at all -- every check run
    and status at the sha counts, with the merge's tolerance for checks it
    does not require (`success`, `skipped`, `neutral`). Nothing at all is
    `none`, which `issueci` waits on for a bounded time before it calls it.

Red wins over pending: a red required check will not turn green by waiting,
and a fix round started now is CI time saved.

WHAT THE READING ALSO COUNTS (#503). Beside the one state, a reading keeps a
count per bucket -- passed, failed, pending, skipped -- and one entry per check
it evaluated (`{name, state, url}`), so the run page can say "5 passed · 7
pending · 3 skipped" and link each check, where it once said only "pending".
These are for DISPLAY: they never decide the state, which is the rule above,
unchanged (a `cancelled` run counts as skipped here and pending there, #1778).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: What counts as green for a REQUIRED check (agent_worker.merge.REQUIRED_GREEN).
REQUIRED_GREEN = frozenset({"success", "skipped"})
#: A run someone stopped: pending, never red (#1778; `cancelled_run`,
#: agent_worker.merge.CANCELLED_CONCLUSION).
CANCELLED_CONCLUSION = "cancelled"
#: The one conclusion a check run keeps inside a cancelled Actions run
#: (`cancelled_by_its_workflow`, #2582; agent_worker.merge.PASSED_IN_ITS_RUN).
PASSED_IN_ITS_RUN = "success"
#: What any OTHER check run may conclude and still not hold the run
#: (agent_worker.merge.OTHER_TOLERATED).
OTHER_TOLERATED = frozenset({"success", "skipped", "neutral"})

#: Commit status states, as GitHub names them.
STATUS_GREEN = "success"
STATUS_PENDING = "pending"

GREEN, PENDING, RED, NONE = "green", "pending", "red", "none"

#: The display buckets a reading counts its checks into (`CiReading.counts`).
PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
COUNT_BUCKETS = (PASSED, FAILED, PENDING, SKIPPED)
#: Which GitHub check-run conclusions land in which bucket. A run not yet
#: finished (`check_run_finished`: queued, in_progress, waiting, requested,
#: with no conclusion and completed_at) is pending. `success` is passed; `neutral`, `skipped` and `cancelled`
#: are skipped; every other conclusion -- `failure`, `timed_out`,
#: `action_required`, `startup_failure`, `stale`, or none at all -- is failed.
PASSED_CONCLUSIONS = frozenset({"success"})
SKIPPED_CONCLUSIONS = frozenset({"neutral", "skipped", "cancelled"})


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    #: The App the rule pins the check to, or None when it names only the context.
    app_id: int | None


def required_status_checks(rules: Sequence[Mapping[str, Any]]) -> list[RequiredCheck]:
    """The required checks every `required_status_checks` rule names, once each.

    The same reading as `agent_worker.forge.required_status_checks`.
    """
    seen: dict[tuple[str, int | None], RequiredCheck] = {}
    for rule in rules:
        if not isinstance(rule, Mapping) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), Mapping) else {}
        for check in parameters.get("required_status_checks") or []:
            if not isinstance(check, Mapping) or not isinstance(check.get("context"), str):
                continue
            raw = check.get("integration_id", check.get("app_id"))
            app_id = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
            seen.setdefault((check["context"], app_id), RequiredCheck(check["context"], app_id))
    return list(seen.values())


def _event_group(run: Mapping[str, Any]) -> str | None:
    """The event that triggered the run's check suite; without one its check
    suite, so one suite never outvotes another; without either, None."""
    event = run.get("event")
    if isinstance(event, str) and event:
        return event
    suite = run.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, Mapping) else None
    return f"check suite {suite_id}" if suite_id is not None else None


def _run_id(run: Mapping[str, Any]) -> int | None:
    value = run.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def latest_per_event(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The latest run of each event among the runs of ONE check.

    Ordered as the merge step's `latest_reading` orders them: by GitHub's run
    id, which a re-run increases, then by position; runs with no id first.
    The result is oldest latest run first.
    """
    ordered = sorted(enumerate(runs), key=lambda item: (
        _run_id(item[1]) is not None, _run_id(item[1]) or 0, item[0]))
    latest: dict[str | None, tuple[int, Mapping[str, Any]]] = {}
    for rank, (_position, run) in enumerate(ordered):
        latest[_event_group(run)] = (rank, run)
    deciding = [run for _rank, run in sorted(latest.values(), key=lambda item: item[0])]
    return [{**run, "conclusion": CANCELLED_CONCLUSION}
            if cancelled_by_its_workflow(run, deciding) else run for run in deciding]


def cancelled_by_its_workflow(run: Mapping[str, Any], deciding: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a check run that finished red is read as `cancelled` because
    its Actions run was (#2582, BGD 2026-10-28).

    `ci-gate` and security.yml's folded name-keepers run under `if:
    always()`, so in a workflow run concurrency cancelled they conclude
    `failure` over needs that were only cancelled (PR #2565 at 968d9949). A
    check run finished with any conclusion but `success` (`PASSED_IN_ITS_RUN`;
    `skipped` too, which a gate under `!cancelled()` may say there), whose
    Actions run concluded `cancelled` (`workflow_conclusion`), is read as
    cancelled -- pending, never green -- when a newer run of the same
    workflow and event is at the sha (`workflow_superseded`) or another
    event's deciding run passed the check. A red check in a run nobody
    cancelled stays red, as does one in a cancelled run with neither. The
    worker's `merge.cancelled_by_its_workflow` is the same rule, held equal by
    tests/unit/control_plane/test_forgechecks_cancelled_workflow_run.py.
    """
    if run.get("workflow_conclusion") != CANCELLED_CONCLUSION or not check_run_finished(run):
        return False
    if (run.get("conclusion") or "") in (PASSED_IN_ITS_RUN, CANCELLED_CONCLUSION):
        return False
    if run.get("workflow_superseded") is True:
        return True
    return any(other is not run and check_run_finished(other)
               and (other.get("conclusion") or "") in REQUIRED_GREEN for other in deciding)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def mark_workflow_runs(runs: Sequence[Mapping[str, Any]],
                       workflow_runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copies of `runs` (check runs), each whose check suite an Actions run
    in `workflow_runs` (`GET actions/runs?head_sha=`) made carrying that
    run's `workflow_conclusion` and `workflow_superseded`: whether another
    run at the sha has the same `workflow_id`, event and head branch and a
    larger id -- the run a concurrency group cancelled this one for, which
    may not have created this check yet (#2582). The worker's
    `merge.actions_suites` reads Actions runs the same way. Each also carries
    the run's `id` as `workflow_run_id`, the run whose failed jobs the merge
    wake tick's free re-run asks GitHub to re-run (#2841); no reader here
    judges a check by it.
    """
    rows = [row for row in workflow_runs if isinstance(row, Mapping)]
    marks: dict[int, tuple[Any, bool, int | None]] = {}
    for row in rows:
        suite, workflow, run_id = (_int(row.get("check_suite_id")), _int(row.get("workflow_id")),
                                   _int(row.get("id")))
        if suite is None:
            continue
        superseded = workflow is not None and run_id is not None and any(
            _int(other.get("workflow_id")) == workflow
            and other.get("event") == row.get("event")
            and other.get("head_branch") == row.get("head_branch")
            and (_int(other.get("id")) or 0) > run_id
            for other in rows)
        marks[suite] = (row.get("conclusion"), superseded, run_id)
    out: list[dict[str, Any]] = []
    for run in runs:
        suite = run.get("check_suite")
        mark = marks.get(_int(suite.get("id"))) if isinstance(suite, Mapping) else None
        out.append(dict(run) if mark is None else
                   {**run, "workflow_conclusion": mark[0], "workflow_superseded": mark[1],
                    **({"workflow_run_id": mark[2]} if mark[2] is not None else {})})
    return out


#: An Actions check run's page, `.../actions/runs/<run id>/job/<job id>`:
#: only the digits are read, and only when the job is the check run itself.
_ACTIONS_JOB_URL = re.compile(r"/actions/runs/(\d{1,20})/job/(\d{1,20})/?$")


def workflow_run_of(run: Mapping[str, Any]) -> int | None:
    """The Actions workflow run a failing Actions check run is a job of, or None.

    The merge wake tick re-runs its failed jobs through it (#2841); the CI
    excerpt reads a red gate's failed leaf jobs from it (`issueci`, #2937).

    `workflow_run_id` when the reading stamped it from the Actions runs it
    read; else the run id in the check's `details_url` or `html_url`, an int
    and nothing more, and only when the URL's job is this very check run.
    """
    stamped = run.get("workflow_run_id")
    if isinstance(stamped, int) and not isinstance(stamped, bool) and stamped > 0:
        return stamped
    for key in ("details_url", "html_url"):
        url = run.get(key)
        if not isinstance(url, str):
            continue
        found = _ACTIONS_JOB_URL.search(url.partition("?")[0].partition("#")[0])
        if found is not None and int(found.group(2)) == run.get("id") and int(found.group(1)) > 0:
            return int(found.group(1))
    return None


def check_run_finished(run: Mapping[str, Any]) -> bool:
    """Whether a check run has finished: `status` is `completed`, OR it
    carries a conclusion together with a `completed_at`.

    BGD, 2026-10-23 (CHECK-STUCK, part of #2031): a run that carries a
    conclusion and a completed_at is judged by its conclusion, whatever
    `status` says -- GitHub left PR 2006's `secret scan` at `in_progress`
    with `conclusion: success` and a `completed_at`. A run with no
    conclusion stays pending. The worker's `merge.check_run_finished` is the
    same rule, held equal by tests/unit/scripts/test_auto_merge_workflow.py.
    """
    if run.get("status") == "completed":
        return True
    return bool(run.get("conclusion")) and bool(run.get("completed_at"))


def cancelled_run(run: Mapping[str, Any]) -> bool:
    """A run that finished `cancelled`: pending, never red.

    #1778's second expectation (BGD released bundle H09, 2026-10-27): "a
    check whose LATEST run is cancelled is still not green and waits, rather
    than failing the merge". A red latest run of another event still wins.
    The worker's `merge.cancelled_run` is the same rule, held equal by
    tests/unit/control_plane/test_forgechecks_per_event.py.
    """
    return check_run_finished(run) and run.get("conclusion") == CANCELLED_CONCLUSION


def required_check_state(runs: Sequence[Mapping[str, Any]]) -> str:
    """The runs of ONE required check by its pinned App: green, pending or red,
    decided by the latest run of each event (`latest_per_event`); pending when
    every latest run not green is cancelled (`cancelled_run`)."""
    latest = latest_per_event(runs)
    if not latest:
        return PENDING
    if not all(check_run_finished(run) for run in latest):
        return PENDING
    if all((run.get("conclusion") or "") in REQUIRED_GREEN for run in latest):
        return GREEN
    if all(cancelled_run(run) or (run.get("conclusion") or "") in REQUIRED_GREEN for run in latest):
        return PENDING
    return RED


def other_check_state(run: Mapping[str, Any]) -> str:
    """A check run nothing requires: pending until finished or when cancelled
    (`cancelled_run`), then tolerated or red."""
    if not check_run_finished(run) or cancelled_run(run):
        return PENDING
    return GREEN if (run.get("conclusion") or "") in OTHER_TOLERATED else RED


def status_state(status: Mapping[str, Any]) -> str:
    state = status.get("state")
    if state == STATUS_GREEN:
        return GREEN
    if state == STATUS_PENDING:
        return PENDING
    return RED


def run_bucket(run: Mapping[str, Any]) -> str:
    """The display bucket of one check run (see `PASSED_CONCLUSIONS`)."""
    if not check_run_finished(run):
        return PENDING
    conclusion = run.get("conclusion") or ""
    if conclusion in PASSED_CONCLUSIONS:
        return PASSED
    if conclusion in SKIPPED_CONCLUSIONS:
        return SKIPPED
    return FAILED


def status_bucket(status: Mapping[str, Any]) -> str:
    """A commit status: `success` passed, `pending` pending, `failure`/`error` failed."""
    return {GREEN: PASSED, PENDING: PENDING}.get(status_state(status), FAILED)


def _url(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def run_entry(run: Mapping[str, Any]) -> dict[str, Any]:
    """`{name, state, url}` for a check run: its `html_url`, else its `details_url`."""
    return {
        "name": str(run.get("name") or "check"),
        "state": run_bucket(run),
        "url": _url(run.get("html_url")) or _url(run.get("details_url")),
    }


def status_entry(status: Mapping[str, Any]) -> dict[str, Any]:
    """`{name, state, url}` for a commit status: its `target_url`."""
    return {
        "name": str(status.get("context") or "status"),
        "state": status_bucket(status),
        "url": _url(status.get("target_url")),
    }


def _app_id(run: Mapping[str, Any]) -> Any:
    app = run.get("app")
    return app.get("id") if isinstance(app, Mapping) else None


@dataclass
class CiReading:
    """One reading of CI at one sha."""

    state: str
    #: The check runs that are red, as GitHub listed them (their output is
    #: what the excerpt is made of).
    failing_runs: list[Mapping[str, Any]] = field(default_factory=list)
    #: The commit statuses that are red.
    failing_statuses: list[Mapping[str, Any]] = field(default_factory=list)
    #: Names of what is still pending.
    pending: list[str] = field(default_factory=list)
    #: One `{name, state, url}` per check evaluated, `state` a display bucket
    #: (`COUNT_BUCKETS`) and `url` None when GitHub gave none. Display only.
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """How many evaluated checks are in each display bucket."""
        counts = {bucket: 0 for bucket in COUNT_BUCKETS}
        for check in self.checks:
            counts[check["state"]] += 1
        return counts

    def failing_names(self) -> list[str]:
        names = [str(run.get("name") or "check") for run in self.failing_runs]
        names += [str(status.get("context") or "status") for status in self.failing_statuses]
        return sorted(dict.fromkeys(names))


def evaluate(
    required: Sequence[RequiredCheck],
    runs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
) -> CiReading:
    """The reading at one sha, from the required set, its check runs and statuses."""
    runs = [run for run in runs if isinstance(run, Mapping)]
    statuses = [status for status in statuses if isinstance(status, Mapping)]
    reading = CiReading(state=GREEN)
    if required:
        for check in required:
            mine = [
                run for run in runs
                if run.get("name") == check.context
                and (check.app_id is None or _app_id(run) == check.app_id)
            ]
            if mine:
                # What decides it, the latest run of each event, is what is
                # counted and excerpted: a flake re-run green is neither.
                deciding = latest_per_event(mine)
                reading.checks += [run_entry(run) for run in deciding]
                state = required_check_state(mine)
                if state == RED:
                    reading.failing_runs += [
                        run for run in deciding
                        if check_run_finished(run) and not cancelled_run(run)
                        and (run.get("conclusion") or "") not in REQUIRED_GREEN
                    ]
                elif state == PENDING:
                    reading.pending.append(check.context)
                continue
            # No check run reports it: a commit status may, but only for a
            # check pinned to no App. A status carries no App, so it cannot
            # satisfy a check pinned to one -- as a run by another App cannot.
            reported = (
                [s for s in statuses if s.get("context") == check.context]
                if check.app_id is None else []
            )
            state = status_state(reported[0]) if reported else PENDING
            reading.checks.append(
                status_entry(reported[0]) if reported
                else {"name": check.context, "state": PENDING, "url": None}
            )
            if state == RED:
                reading.failing_statuses.append(reported[0])
            elif state == PENDING:
                reading.pending.append(check.context)
    else:
        if not runs and not statuses:
            return CiReading(state=NONE)
        # One rule for every check (BGD, 2026-10-22, CHECK-RULE-2, part of
        # #1999): each check -- its name and App -- is judged by the latest
        # run of each event, as the worker's `judge_other_checks` judges it,
        # and only those deciding runs are counted and excerpted: a
        # superseded failure, or a superseded run still in progress, is
        # neither. Held equal by tests/unit/scripts/test_auto_merge_workflow.py.
        by_check: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
        for run in runs:
            by_check.setdefault((run.get("name"), _app_id(run)), []).append(run)
        for mine in by_check.values():
            for run in latest_per_event(mine):
                reading.checks.append(run_entry(run))
                state = other_check_state(run)
                if state == RED:
                    reading.failing_runs.append(run)
                elif state == PENDING:
                    reading.pending.append(str(run.get("name") or "check"))
        for status in statuses:
            reading.checks.append(status_entry(status))
            state = status_state(status)
            if state == RED:
                reading.failing_statuses.append(status)
            elif state == PENDING:
                reading.pending.append(str(status.get("context") or "status"))
    if reading.failing_runs or reading.failing_statuses:
        reading.state = RED
    elif reading.pending:
        reading.state = PENDING
    return reading
