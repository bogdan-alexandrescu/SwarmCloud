"""The issue fetch runs beside the clone, not after it (#721, P29).

Owner decision 2026-10-06 (chunk-3 observer P29): the issue fetch -- the
`input.issue` read that becomes `work/issue.md` -- waited for the clone, the
staging and the credentials before it asked the forge anything, though all it
needs is the token and the issue number. It now starts in a thread as soon as
those are known, before the clone, and is joined before the agent starts.

Pinned here:

* the fetch's start and end are on the same clock as the startup marks, and
  are written into `agent_started`, the event that already says when the agent
  began;
* end to end, the fetch runs WHILE the clone runs (a fake clock, and a clone
  that waits for the fetch to have happened);
* the agent still finds the issue, and a fetch still running when the clone
  ends is waited for;
* a failure behaves exactly as before: the same exception, the same end cause,
  the agent never starts. Only its timing moved.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_worker import issue as issue_mod
from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from test_issue_input import (  # noqa: F401 - fixtures
    TOKEN,
    _agent_record,
    _seed,
    _worker,
    keep_workspace,
    lane_agent,
    needs_git,
    origin,
)

REPO = "https://github.com/octo/widgets.git"


def _issue(number: int = 265) -> issue_mod.Issue:
    return issue_mod.Issue(
        repository="octo/widgets", number=number, title="Point a step at an issue",
        state="open", author="bogdan", created_at="", url="", body="the body",
    )


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _logger():
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="eng", generation=1,
        runner_profile="claude-code", stream=io.StringIO(),
    )


# ---------------------------------------------------------------------------
# the prefetch on its own
# ---------------------------------------------------------------------------


def test_the_prefetch_records_when_it_started_and_ended(monkeypatch):
    clock = _Clock(12.0)

    def fetch(*, repository_url, number, token, on_request=None):
        clock.now += 4.5
        return _issue(number)

    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    prefetch = issue_mod.IssuePrefetch(
        number=265, repository_url=REPO, token=None, clock=clock,
    )
    prefetch.start()
    prefetch.wait()

    assert prefetch.outcome().number == 265
    assert prefetch.timing() == {"started_seconds": 12.0, "ended_seconds": 16.5, "ok": True}


def test_a_failed_prefetch_raises_its_own_exception_when_asked(monkeypatch):
    raised = issue_mod.IssueUnavailable("could not fetch issue #31 of octo/widgets: gone (410)")

    def fetch(**kwargs):
        raise raised

    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    prefetch = issue_mod.IssuePrefetch(number=31, repository_url=REPO, token=None)
    prefetch.start()

    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        prefetch.outcome()
    assert caught.value is raised
    assert prefetch.timing()["ok"] is False


def _staged_failure(tmp_path: Path, monkeypatch, *, prefetched: bool) -> str:
    def fetch(**kwargs):
        raise issue_mod.IssueUnavailable("could not fetch issue #31 of octo/widgets: gone (410)")

    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    prefetch = None
    if prefetched:
        prefetch = issue_mod.IssuePrefetch(number=31, repository_url=REPO, token=None)
        prefetch.start()
    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.stage_issue(
            number=31, repository_url=REPO, token=None, refusal="memory is not protected",
            work_dir=tmp_path, scrub=lambda value: value, logger=_logger(),
            prefetched=prefetch,
        )
    assert not (tmp_path / "issue.md").exists()
    return str(caught.value)


def test_a_failed_prefetch_fails_stage_issue_with_the_message_it_always_had(tmp_path, monkeypatch):
    assert _staged_failure(tmp_path, monkeypatch, prefetched=True) == _staged_failure(
        tmp_path, monkeypatch, prefetched=False
    )


def test_an_unreachable_forge_in_the_prefetch_is_still_issue_unreachable(tmp_path, monkeypatch):
    def fetch(**kwargs):
        raise issue_mod.IssueUnreachable("could not reach api.github.com: timed out")

    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    policy = issue_mod.RetryPolicy.bounded(
        attempts=2, max_in_worker_retry_delay_seconds=45, remaining_seconds=600,
        sleep=lambda seconds: None, log=_logger(),
    )
    prefetch = issue_mod.IssuePrefetch(number=72, repository_url=REPO, token=None, retry=policy)
    prefetch.start()

    with pytest.raises(issue_mod.IssueUnreachable) as caught:
        issue_mod.stage_issue(
            number=72, repository_url=REPO, token=None, refusal=None, work_dir=tmp_path,
            scrub=lambda value: value, logger=_logger(), prefetched=prefetch,
        )
    assert "#72" in str(caught.value) and caught.value.tries == 2


def test_a_prefetch_for_another_repository_is_not_used(tmp_path, monkeypatch):
    """The clone settles the repository URL; a prefetch of a different one is
    dropped and the fetch made as it always was."""
    asked: list[str] = []

    def fetch(*, repository_url, number, token, on_request=None):
        asked.append(repository_url)
        return _issue(number)

    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    prefetch = issue_mod.IssuePrefetch(
        number=265, repository_url="https://github.com/octo/other.git", token=None
    )
    prefetch.start()
    prefetch.wait()

    issue_mod.stage_issue(
        number=265, repository_url=REPO, token=None, refusal=None, work_dir=tmp_path,
        scrub=lambda value: value, logger=_logger(), prefetched=prefetch,
    )
    assert asked == ["https://github.com/octo/other.git", REPO]


# ---------------------------------------------------------------------------
# end to end: the fetch overlaps the clone
# ---------------------------------------------------------------------------


def _agent_started(db) -> dict[str, Any]:
    (detail,) = [
        e["detail"] for e in db.events("task_1")
        if (e.get("detail") or {}).get("cause") == "agent_started"
    ]
    return detail


@needs_git
def test_the_issue_fetch_runs_while_the_clone_does(
    db, store, worker_factory, monkeypatch, tmp_path, origin, lane_agent, keep_workspace
):
    """A clone that takes 30 s on the fake clock and waits, up to 5 real
    seconds, for the fetch to have happened. MUTATION: fetch after the clone
    (as before P29) and the clone sees no fetch, and the recorded fetch starts
    after the clone ended."""
    clock = _Clock(100.0)
    fetched = threading.Event()
    window: dict[str, float] = {}
    asked: list[dict[str, Any]] = []

    def fetch(*, repository_url, number, token, on_request=None):
        asked.append({"token": token, "main": threading.current_thread() is threading.main_thread()})
        clock.now += 5.0
        fetched.set()
        return _issue(number)

    _seed(db)
    worker = _worker(worker_factory, monkeypatch, origin, fetch)
    worker.phases._clock = clock
    worker.phases._t0 = worker.phases._since = 100.0
    real = lifecycle.shallow_clone

    def clone(**kwargs: Any):
        window["start"] = clock.now
        window["overlapped"] = fetched.wait(5)
        if worker._issue_prefetch is not None:
            worker._issue_prefetch.wait()
        result = real(**kwargs)
        clock.now += 30.0
        window["end"] = clock.now
        return result

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)

    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1")

    assert window["overlapped"] is True, "the issue fetch did not run while the clone did"
    assert asked == [{"token": TOKEN, "main": False}]
    timing = _agent_started(db)["issue_fetch"]
    clone_start, clone_end = window["start"] - 100.0, window["end"] - 100.0
    assert timing["started_seconds"] <= clone_start + 5.0
    assert timing["ended_seconds"] <= clone_end
    assert timing["started_seconds"] < clone_end and timing["ended_seconds"] >= clone_start
    assert timing["ok"] is True and timing["beside_clone"] is True
    record = _agent_record(tmp_path)
    assert record is not None and "# Issue #265" in (record["issue_md"] or "")


@needs_git
def test_a_fetch_still_running_after_the_clone_is_waited_for(
    db, store, worker_factory, monkeypatch, tmp_path, origin, lane_agent, keep_workspace
):
    cloned = threading.Event()

    def fetch(*, repository_url, number, token, on_request=None):
        assert cloned.wait(10), "the clone never finished"
        return _issue(number)

    _seed(db)
    worker = _worker(worker_factory, monkeypatch, origin, fetch)
    real = lifecycle.shallow_clone

    def clone(**kwargs: Any):
        result = real(**kwargs)
        cloned.set()
        return result

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)

    assert worker.run() == ExitCode.OK
    record = _agent_record(tmp_path)
    assert record is not None and "# Issue #265" in (record["issue_md"] or "")


@needs_git
def test_a_prefetch_that_fails_ends_the_attempt_as_a_fetch_always_did(
    db, store, worker_factory, monkeypatch, tmp_path, origin, lane_agent, keep_workspace
):
    threads: list[bool] = []

    def fetch(*, repository_url, number, token, on_request=None):
        threads.append(threading.current_thread() is threading.main_thread())
        raise issue_mod.IssueUnavailable(f"could not fetch issue #{number} of octo/widgets: gone (410)")

    _seed(db, number=31)
    worker = _worker(worker_factory, monkeypatch, origin, fetch)

    code = worker.run()

    task = db.doc("tasks/task_1")
    assert threads == [False], "the fetch did not run beside the clone"
    assert code != ExitCode.OK
    assert task["state"] == TaskState.FAILED.value, task
    assert task.get("end_cause") == EndCause.INPUTS_UNAVAILABLE.value, task
    assert "#31" in (task.get("last_error") or ""), task
    assert _agent_record(tmp_path) is None, "the agent started without its issue"
