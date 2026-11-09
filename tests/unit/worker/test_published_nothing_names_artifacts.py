"""A `published_nothing:` failure names the step's artifacts and the remedy.

The measured failure (2026-11-07, part of #2688): seven read-only analysis
steps under `direct-pr` each wrote triage.tsv and a summary, committed
nothing as told, and FAILED with "published_nothing: the step was to open a
pull request and its branch has no commits beyond <base>" -- an error that
named neither the artifacts that were the step's result nor
`allow_empty_diff`, the declaration that would have made them so.

So when the step's agent left artifacts beyond the worker's own (the pull
request's title and body, the hand-off note, the work patch, the runner's
captured streams), today's sentence, word for word, is followed by "; it
produced N artifacts (<up to five names>); a step whose deliverable is its
artifacts is submitted with allow_empty_diff: true". A name outside a plain
file-name shape is counted and never shown.

MUTATIONS: drop `_artifacts_remedy` from `_published_nothing` -- the named
test sees today's sentence alone. Stop skipping the worker's own files -- the
no-clause test sees a clause. Show every name -- the odd-name test sees it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REMEDY = "a step whose deliverable is its artifacts is submitted with allow_empty_diff: true"


def _first_sentence(base: str) -> str:
    return (
        "published_nothing: the step was to open a pull request and its branch "
        f"has no commits beyond {base}"
    )


def _worker(db, worker_factory, monkeypatch, origin: Path, task_input: dict[str, Any]):
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    seed_attempt(db, task_input={"steps": 1, "sleep_seconds": 0.01, **task_input})
    db.doc("tasks/task_1")["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    monkeypatch.setattr(worker, "_git_token", lambda **_: "not-a-real-token")
    return worker


def _direct_pr(db, worker_factory, monkeypatch, origin: Path):
    """A worker whose task is not loaded yet, asked `_published_nothing`
    directly: its step is held to a pull request as a `direct-pr` step is."""
    worker = _worker(db, worker_factory, monkeypatch, origin, {"prompt": "do the work"})
    monkeypatch.setattr(worker, "_opens_pull_request", lambda: True)
    return worker


def _unchanged(base: str, names: list[str]) -> dict[str, Any]:
    return {
        "artifacts": [{"name": name, "uri": f"gs://example-bucket/{name}"} for name in names],
        "git": {"published": False, "commit_count": 0, "dirty_count": 0, "base": base},
    }


def test_an_analysis_step_that_changed_nothing_is_told_its_artifacts_and_the_flag(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    base = refs(origin)["main"]
    worker = _worker(db, worker_factory, monkeypatch, origin, {
        "prompt": "classify every branch; change nothing",
        "artifact_name": "triage.tsv", "artifact_text": "branch\tclass\n",
    })

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["last_error"] == (
        f"{_first_sentence(base)}; it produced 1 artifacts (triage.tsv); {REMEDY}"
    ), task["last_error"]


def test_no_clause_when_only_the_workers_own_files_exist(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    base = refs(origin)["main"]
    worker = _worker(db, worker_factory, monkeypatch, origin, {
        "prompt": "do the work",
        "artifact_name": "pr-body.md", "artifact_text": "What changed\n",
    })

    assert worker.run() == ExitCode.FAILED
    assert db.doc("tasks/task_1")["last_error"] == _first_sentence(base)


def test_the_workers_own_names_are_not_counted(db, worker_factory, monkeypatch, origin):
    worker = _direct_pr(db, worker_factory, monkeypatch, origin)
    summary = _unchanged("abc123", [
        "pr-title.txt", "pr-body.md", "handoff.md", "swarm-work.patch", "summary.md",
    ])
    assert worker._published_nothing(summary) == (
        f"{_first_sentence('abc123')}; it produced 1 artifacts (summary.md); {REMEDY}"
    )


def test_an_odd_name_is_counted_but_not_shown(db, worker_factory, monkeypatch, origin):
    worker = _direct_pr(db, worker_factory, monkeypatch, origin)
    summary = _unchanged("abc123", ["triage.tsv", "notes/../odd name.txt", ".hidden"])
    assert worker._published_nothing(summary) == (
        f"{_first_sentence('abc123')}; it produced 3 artifacts (triage.tsv); {REMEDY}"
    )


def test_only_odd_names_are_counted_with_no_list(db, worker_factory, monkeypatch, origin):
    worker = _direct_pr(db, worker_factory, monkeypatch, origin)
    summary = _unchanged("abc123", ["odd name.txt"])
    assert worker._published_nothing(summary) == (
        f"{_first_sentence('abc123')}; it produced 1 artifacts; {REMEDY}"
    )


def test_at_most_five_names_are_shown(db, worker_factory, monkeypatch, origin):
    worker = _direct_pr(db, worker_factory, monkeypatch, origin)
    names = [f"part-{index}.tsv" for index in range(7)]
    error = worker._published_nothing(_unchanged("abc123", names))
    assert error == (
        f"{_first_sentence('abc123')}; it produced 7 artifacts "
        f"({', '.join(names[:5])}); {REMEDY}"
    )
