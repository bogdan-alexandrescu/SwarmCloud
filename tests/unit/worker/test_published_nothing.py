"""GUARD 2, published nothing is a failure; and contract request 29's writers.

THE MEASURED FAILURE (2026-10-02, workflow wf_b9b337e107494c10a416). The fix
step of an implement -> review -> fix `integrate` workflow cloned a moved
main, its staged patch did not apply, its agent finished, and the forge
answered "No commits between main and branch". The worker ended the step
SUCCEEDED, the rollup read the workflow SUCCEEDED, and no pull request
existed. Nothing in any state said the implementation was lost.

WHAT IS PINNED. A step whose job is to OPEN A PULL REQUEST -- a `direct-pr`
step, the `integrate` integrator, a `single-pr` author -- that ends with
nothing beyond its base, or whose pull request the forge refuses, ends FAILED
with `last_error` "published_nothing: ...", and is NOT retried (it is
deterministic). Every other step that changes nothing still SUCCEEDS: a
contributor, a reader, a `collect` step. An unreachable forge is not a
refusal and is left as it was.

And contract request 29 (applied 2026-10-02): the credential scan's and the
refused title's failures end, once their attempts are spent, with
`end_cause = publish_refused`, not `runner_error` / `outputs_missing`.

MUTATIONS: drop the `_published_nothing` call from `_finalise` -- the
direct-pr and refused tests see SUCCEEDED. Fail it with `fail_retryably` --
they see READY and a retrying event. Count a contributor or reader as a pull
request step -- the parametrised SUCCEEDED test fails. Put RUNNER_ERROR or
OUTPUTS_MISSING back at either refusal site -- the end-cause tests fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import forge as forge_mod, lifecycle
from agent_worker.errors import ExitCode
from swarm_common.states import EventType, TaskState

from conftest import seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _run(db, worker_factory, monkeypatch, origin: Path, *, dispatch: dict,
         task_input: dict | None = None, attempt_count: int = 1):
    seed_attempt(
        db,
        task_input=task_input or {"prompt": "do the work", "steps": 1, "sleep_seconds": 0.01},
        attempt_count=attempt_count,
    )
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dict(dispatch)}
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    return worker


def _retried(db) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]


def test_a_direct_pr_step_that_changed_nothing_fails_and_is_not_retried(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    base = refs(origin)["main"]
    worker = _run(db, worker_factory, monkeypatch, origin, dispatch={"strategy": "direct-pr"})

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    # Attempt 1 of 3, and FAILED rather than READY: a retry repeats it.
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["last_error"] == (
        "published_nothing: the step was to open a pull request and its branch "
        f"has no commits beyond {base}"
    ), task["last_error"]
    # The closest existing cause until contract request 46 is decided.
    assert task["end_cause"] == "outputs_missing"
    assert _retried(db) == [], "a deterministic failure was retried"
    assert forge.pulls == []
    assert task["result_summary"]["git"]["published"] is False


def _refusing_forge(monkeypatch, message: str) -> list[str]:
    """The REAL `open_pull_request` against a forge that answers 422, so the
    worker reads the refusal in the forge module's own words."""
    posts: list[str] = []

    def request(url, *, token, method="GET", payload=None):
        if method == "POST":
            posts.append(url)
            return 422, {"message": "Validation Failed", "errors": [{"message": message}]}
        return 200, []  # no open pull request to reuse

    monkeypatch.setattr(forge_mod, "_request", request)
    monkeypatch.setattr(lifecycle, "open_pull_request", forge_mod.open_pull_request)
    return posts


TITLED = {
    "prompt": "bring the work together", "steps": 1, "sleep_seconds": 0.01,
    "artifact_name": "pr-title.txt", "artifact_text": "Bring the implementation together\n",
}


def test_an_integrator_whose_pull_request_is_refused_fails_naming_the_refusal(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """The measured shape: the integrator has nothing to merge (its contributor's
    branch is gone) and nothing of its own, so the forge answers "No commits
    between"."""
    message = "No commits between main and swarm/task_1"
    posts = _refusing_forge(monkeypatch, message)
    worker = _run(
        db, worker_factory, monkeypatch, origin,
        dispatch={"strategy": "integrate", "role": "integrator", "integrates": ["t-gone"]},
        task_input=TITLED,
    )

    assert worker.run() == ExitCode.FAILED
    assert posts, "the pull request was never asked for"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["last_error"] == f"published_nothing: the forge refused the pull request: {message}"
    assert _retried(db) == []
    git = task["result_summary"]["git"]
    assert git["pull_request_refused"].startswith(lifecycle.PULL_REQUEST_REFUSED)


def test_an_unreachable_forge_is_not_a_refusal(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """The boundary of the refusal: an outage pushed the branch and says so,
    as before. Only the forge's answer "no" is a fact a retry would meet."""
    def unreachable(**_kwargs):
        raise forge_mod.ForgeError("could not open a pull request (502): Bad Gateway")

    monkeypatch.setattr(lifecycle, "open_pull_request", unreachable)
    worker = _run(
        db, worker_factory, monkeypatch, origin,
        dispatch={"strategy": "integrate", "role": "integrator", "integrates": ["t-gone"]},
        task_input=TITLED,
    )
    assert worker.run() == 0
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert "pull_request_refused" not in task["result_summary"]["git"]


@pytest.mark.parametrize(
    "dispatch",
    [
        {"strategy": "integrate", "carrier": "checkpoints", "role": "contributor"},
        {"strategy": "collect"},
        {"strategy": "single-pr", "pr_role": "reader", "pr_author": "t-impl"},
    ],
    ids=["integrate-contributor", "collect-review", "single-pr-reader"],
)
def test_a_step_that_opens_no_pull_request_still_succeeds_having_changed_nothing(
    db, worker_factory, monkeypatch, origin, local_urls, forge, dispatch
):
    if dispatch.get("pr_author"):
        # A single-pr reader clones its author's branch (#295), which the
        # author pushed before the reader started.
        subprocess.run(["git", "branch", f"swarm/{dispatch['pr_author']}", "main"],
                       cwd=str(origin), check=True, capture_output=True)
    worker = _run(db, worker_factory, monkeypatch, origin, dispatch=dispatch)
    assert worker.run() == 0
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["git"]["published"] is False


@pytest.mark.parametrize(
    ("dispatch", "opens"),
    [
        ({"strategy": "direct-pr"}, True),
        ({"strategy": "integrate", "role": "integrator", "integrates": ["t"]}, True),
        ({"strategy": "integrate", "role": "integrator", "integrates": []}, True),
        ({"strategy": "single-pr", "pr_role": "author"}, True),
        ({"strategy": "integrate", "role": "contributor"}, False),
        ({"strategy": "single-pr", "pr_role": "reader"}, False),
        ({"strategy": "single-pr", "pr_role": "amender"}, False),
        ({"strategy": "single-pr", "pr_role": "none"}, False),
        ({"strategy": "collect"}, False),
        ({}, False),
    ],
)
def test_which_steps_owe_a_pull_request(worker_factory, dispatch, opens):
    worker, _, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": dispatch}}
    assert worker._opens_pull_request() is opens


EMPTY_GIT = {"base": "b" * 40, "published": False, "commit_count": 0, "dirty_count": 0,
             "publish_reason": "the agent changed nothing in the repository"}


def test_a_step_whose_verdict_gate_kept_its_agent_from_running_is_exempt(worker_factory):
    """The gate closing is the outcome it exists for, not lost work."""
    worker, _, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._verdict = {"verdict": "MERGE", "verdict_in": ["NOT_YET"], "agent_ran": False}
    assert worker._published_nothing({"git": dict(EMPTY_GIT)}) is None
    worker._verdict = {"verdict": "NOT_YET", "verdict_in": ["NOT_YET"], "agent_ran": True}
    assert worker._published_nothing({"git": dict(EMPTY_GIT)}).startswith("published_nothing: ")


def test_a_step_with_work_is_not_published_nothing(worker_factory):
    worker, _, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": {"strategy": "direct-pr"}}}
    git = dict(EMPTY_GIT, published=True, commit_count=2)
    assert worker._published_nothing({"git": git}) is None
    assert worker._published_nothing({}) is None


# -- contract request 29: PUBLISH_REFUSED at both refusal sites ----------------


def test_a_final_tree_leak_on_the_last_attempt_ends_publish_refused(db, worker_factory, monkeypatch):
    seed_attempt(db, task_input={"prompt": "fix the widget", "steps": 1, "sleep_seconds": 0.01},
                 attempt_count=3)
    db.doc("tasks/task_1")["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    worker, _, _ = worker_factory()
    reason = "the final tree adds a credential in .env (rule aws_access_key_id, line 1); remove it"
    monkeypatch.setattr(worker, "_harvest_git", lambda **_kwargs: {
        "published": False, "final_tree_leak": reason,
        "publish_reason": f"refusing to publish: {reason}",
    })

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "publish_refused", task["end_cause"]
    assert reason in task["last_error"]


def test_a_refused_title_on_the_last_attempt_ends_publish_refused(db, worker_factory, monkeypatch):
    seed_attempt(
        db,
        task_input={"prompt": "fix the widget", "steps": 1, "sleep_seconds": 0.01,
                    "artifact_name": "pr-title.txt", "artifact_text": "Fix task_1 now\n"},
        attempt_count=3,
    )
    db.doc("tasks/task_1")["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: True)
    worker, _, _ = worker_factory()

    def harvest(**_kwargs):
        worker._deferred_publish = {"repo": None, "work_head": None, "publish_repo": None}
        return {"base": "a" * 40}

    monkeypatch.setattr(worker, "_harvest_git", harvest)
    monkeypatch.setattr(worker, "_publish_git", lambda **kwargs: {
        "published": False, "publish_reason": kwargs.get("withheld", ""),
    })

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "publish_refused", task["end_cause"]
    assert "pr-title.txt refused" in task["last_error"]
