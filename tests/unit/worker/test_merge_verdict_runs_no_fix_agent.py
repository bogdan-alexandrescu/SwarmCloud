"""After a MERGE verdict the fix step starts no agent and opens the one pull request (owner, 2026-10-05).

    implement ──> review ──> fix (when: review verdict_in [NOT_YET])

On MERGE the fix step's verdict gate stays shut (#264), so no agent runs. What
was missing is what the pull request is TITLED with: under `integrate` the
integrator owes a `pr-title.txt` that only an agent writes, so a MERGE run
either failed for the missing title or opened nothing. Now:

  * the implementer's own `pr-title.txt` and `pr-body.md` (uploaded artifacts
    of the `builds_on` step, read through the tenant-checked upstream read)
    title and describe the pull request;
  * without them, the title and body are generated from the workflow's label
    (`dispatch.pr_label`, written by swarm-api inside the signed block);
  * the step ends SUCCEEDED with `result_summary.skipped_agent ==
    "review verdict MERGE"`;
  * NOT_YET keeps today's fix agent, and no `skipped_agent`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt
from test_input_from import run_upstream
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    run_attempt,
    tree_at,
)

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

IMPLEMENTER_TITLE = "Add the feature function the issue asks for"
IMPLEMENTER_BODY = "The feature returns 1, as the issue describes."


def _verdict(verdict: str) -> str:
    return json.dumps({"verdict": verdict, "findings": ["nothing blocks"]})


def _seed_implementer(db: Any, store: Any, files: dict[str, str]) -> None:
    """task_impl SUCCEEDED, with `files` uploaded as its artifacts."""
    artifacts = []
    for name, text in files.items():
        key = f"tenants/{TENANT}/tasks/task_impl/attempts/att_impl/artifacts/{name}"
        size = store.upload_bytes(key, text.encode("utf-8"))
        artifacts.append({"name": name, "uri": store.uri(key), "bytes": size})
    db.seed("tasks/task_impl", {
        "id": "task_impl", "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "result_summary": {"artifacts": artifacts},
    })


def _seed_fix(db: Any, *, verdict_in: list[str], label: str | None = None) -> None:
    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    dispatch: dict[str, Any] = {
        "strategy": "integrate",
        "role": "integrator",
        "integrates": ["task_impl"],
        "builds_on": "task_impl",
        "verdict_gate": {"task_id": "task_up", "verdict_in": verdict_in},
    }
    if label is not None:
        dispatch["pr_label"] = label
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "verdict.json"},
        "dispatch": dispatch,
    }


def _implement(worker_factory: Any, monkeypatch: Any, origin: Path) -> None:
    def edit(repo: Path) -> None:
        (repo / "feature.py").write_text("def feature():\n    return 1\n")

    run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="task_impl",
        dispatch={"strategy": "integrate", "role": "contributor"},
        edit=edit,
    )


def _run_fix(worker_factory: Any, monkeypatch: Any, origin: Path) -> int:
    worker, _config, _ = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        repository_url=f"file://{origin}",
    )
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a fix agent was started after a MERGE verdict")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    return worker.run()


@pytestmark_git
def test_a_merge_verdict_opens_the_pr_with_the_implementers_title_and_no_agent(
    db, store, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(worker_factory, monkeypatch, origin)
    _seed_implementer(db, store, {"pr-title.txt": IMPLEMENTER_TITLE + "\n",
                                  "pr-body.md": IMPLEMENTER_BODY})
    run_upstream(db, worker_factory, artifact_name="verdict.json", artifact_text=_verdict("MERGE"))
    _seed_fix(db, verdict_in=["NOT_YET"], label="Widget feature")

    assert _run_fix(worker_factory, monkeypatch, origin) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert summary["skipped_agent"] == "review verdict MERGE"
    assert summary["verdict_gate"]["agent_ran"] is False
    assert summary["pull_request_text_from"] == {"title": "implementer", "body": "implementer"}
    # ONE pull request, from the implementer's branch merged into the fix's.
    assert len(forge.pulls) == 1
    pull = forge.pulls[0]
    assert pull["title"] == IMPLEMENTER_TITLE
    assert IMPLEMENTER_BODY in pull["body"]
    assert "Review verdict: **MERGE**" in pull["body"]
    assert "feature.py" in tree_at(origin, "swarm/task_2")


@pytestmark_git
def test_without_the_implementers_text_the_title_comes_from_the_label(
    db, store, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(worker_factory, monkeypatch, origin)
    _seed_implementer(db, store, {})
    run_upstream(db, worker_factory, artifact_name="verdict.json", artifact_text=_verdict("MERGE"))
    _seed_fix(db, verdict_in=["NOT_YET"], label="Widget feature")

    assert _run_fix(worker_factory, monkeypatch, origin) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["skipped_agent"] == "review verdict MERGE"
    assert task["result_summary"]["pull_request_text_from"] == {"title": "label", "body": "label"}
    assert len(forge.pulls) == 1
    assert forge.pulls[0]["title"] == "Widget feature"
    assert "Widget feature" in forge.pulls[0]["body"]
    # The generated title never carries a task id (owner rule, 2026-09-28).
    assert "task_" not in forge.pulls[0]["title"]


@pytestmark_git
def test_an_implementer_title_that_is_refused_falls_back_to_the_label(
    db, store, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(worker_factory, monkeypatch, origin)
    _seed_implementer(db, store, {"pr-title.txt": "two\nlines"})
    run_upstream(db, worker_factory, artifact_name="verdict.json", artifact_text=_verdict("MERGE"))
    _seed_fix(db, verdict_in=["NOT_YET"], label="Widget feature")

    assert _run_fix(worker_factory, monkeypatch, origin) == ExitCode.OK
    assert forge.pulls[0]["title"] == "Widget feature"


def test_a_merge_verdict_records_the_skipped_agent_on_any_strategy(
    db, worker_factory, monkeypatch
):
    run_upstream(db, worker_factory, artifact_name="verdict.json", artifact_text=_verdict("MERGE"))
    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "verdict.json"},
        "dispatch": {"strategy": "collect", "carrier": "checkpoints",
                     "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"]}},
    }
    worker, _config, _ = worker_factory(task_id="task_2", attempt_id="att_2", lease_id="lease_2")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a fix agent was started after a MERGE verdict")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["result_summary"]["skipped_agent"] == "review verdict MERGE"


def test_a_not_yet_verdict_runs_the_fix_agent_and_skips_nothing(
    db, worker_factory, runner_inputs
):
    run_upstream(
        db, worker_factory, artifact_name="verdict.json", artifact_text=_verdict("NOT_YET")
    )
    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "verdict.json"},
        "dispatch": {"strategy": "collect", "carrier": "checkpoints",
                     "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"]}},
    }
    worker, _config, _ = worker_factory(task_id="task_2", attempt_id="att_2", lease_id="lease_2")

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert "skipped_agent" not in task["result_summary"]
    assert task["result_summary"]["verdict_gate"]["agent_ran"] is True
    assert runner_inputs[-1]["task_id"] == "task_2"
