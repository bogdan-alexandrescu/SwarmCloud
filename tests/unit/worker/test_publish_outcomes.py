"""The worker's publish outcomes: four owner decisions of 2026-10-08 (#872).

  1. #872. A step whose job is to open a pull request and whose push FAILS
     ends FAILED, its error naming the cause and the patch's uri so the work
     can be recovered with `swarm_apply`. Measured on task_6a0c9afdbaea449eb53d:
     "push failed with exit 1", state SUCCEEDED, the workflow green. A step that
     publishes nothing by design (a review, an `integrate` contributor) is
     unaffected.
  2. Proposal J. No change AND `verification.md` is the `no_change` result,
     whatever `allow_empty_diff` says, with the verification as the reason.
     No change and no verification fails as before.
  3. Proposal A. Every claude-code (and codex) prompt with a repository ends
     with one fixed paragraph: the worker publishes; commit, and write the
     title and body files. 31 answers on 2026-10-08 said no pull request
     existed because the agent lacked GitHub credentials.
  4. Proposal B. An issue step is asked for `pr-title.txt` too, given ONE
     follow-up turn for it, and only then titled from its issue -- prefixed
     `[title missing] `. 17 of 32 pull requests were silently titled with the
     issue's title.

MUTATIONS: drop the `_publish_failed` call from `_record_finalised` -- the
push-failed test sees SUCCEEDED. Drop the `publish_failed` field from
`_publish_git`'s GitError handler -- the end-to-end push test fails. Drop the
`_verified_no_change` branch -- the verification tests see FAILED. Drop the
paragraph -- the prompt tests fail. Put `_title_owed` back in
`_declared_outputs` -- the issue-step request test fails. Drop the prefix --
the fallback test fails. Drop the title-only break in the repair loop -- the
one-turn test sees three runs.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.gitops import GitError
from agent_worker.lifecycle import PATCH_NAME, PR_TITLE_FILE
from agent_worker.runners import claude_code, cliagent
from swarm_common.states import EventType, TaskState

from worker_seeds import seed_attempt
from test_claude_repair_turns import DONE, SESSION, _ctx, _install, _runs
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    agent_edits_without_committing,
    forge,
    local_urls,
    origin,
    run_attempt,
)

#: Spelled out rather than read from `lifecycle`, so each test fails on its
#: own without the change instead of the module failing to collect.
PUBLISH_FAILED_FIELD = "publish_failed"
VERIFICATION_FILE = "verification.md"
NO_CHANGE_REASON_KEY = "no_change_reason"

#: What `_harvest_git` reports for a clone the agent left exactly as it found it.
EMPTY_DIFF = {
    "base": "a" * 40, "head": "a" * 40, "patch": None, "patch_bytes": 0,
    "patch_omitted": False, "commit_count": 0, "dirty_count": 0,
    "published": False, "publish_reason": "the agent changed nothing in the repository",
}

PUSH_ERROR = (
    "push failed with exit 1: error: failed to push some refs to "
    "'https://github.com/example/repo'"
)


def _seed(db: Any, *, dispatch: dict[str, Any], expected: list[str] | None = None,
          artifact_name: str = "notes.md", artifact_text: str = "notes\n") -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "do the work", "steps": 1, "sleep_seconds": 0.01,
            "artifact_name": artifact_name, "artifact_text": artifact_text,
        },
    )
    metadata: dict[str, Any] = {"dispatch": dict(dispatch)}
    if expected is not None:
        metadata["expected_outputs"] = expected
    db.doc("tasks/task_1")["metadata"] = metadata


def _harvest(worker: Any, git: dict[str, Any], *, write_patch: bool = False) -> None:
    """Stand in for the harvest and its publish; the real one is end to end below."""

    def harvest(**_kwargs: Any) -> dict[str, Any]:
        if write_patch:
            (worker.ws.artifacts / PATCH_NAME).write_text("diff --git a/x b/x\n")
        return dict(git)

    worker._harvest_git = harvest  # type: ignore[method-assign]


def _retried(db: Any) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]


# ---------------------------------------------------------------------------
# 1. #872: a pull-request step whose push failed is not SUCCEEDED
# ---------------------------------------------------------------------------

PUSH_FAILED = {
    "base": "a" * 40, "head": "b" * 40, "commit_count": 0, "dirty_count": 18,
    "patch": PATCH_NAME, "patch_bytes": 20, "patch_omitted": False,
    "published": False, "auto_committed": True, "publish_reason": PUSH_ERROR,
    PUBLISH_FAILED_FIELD: True,
}


def test_a_direct_pr_step_whose_push_failed_ends_failed_naming_the_patch(db, worker_factory):
    _seed(db, dispatch={"strategy": "direct-pr"})
    worker, _, _ = worker_factory()
    _harvest(worker, PUSH_FAILED, write_patch=True)

    assert worker.run() == ExitCode.FAILED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task.get("last_error")
    error = task["last_error"]
    assert error.startswith("publish_failed: "), error
    assert PUSH_ERROR in error
    (patch,) = [a for a in task["result_summary"]["artifacts"] if a["name"] == PATCH_NAME]
    assert patch["uri"] in error, error
    assert "swarm_apply" in error
    assert task["end_cause"] == "outputs_missing"
    # Not retried: the same branch meets the same refusal.
    assert _retried(db) == []


def test_without_an_uploaded_patch_the_error_says_where_the_work_is(db, worker_factory):
    _seed(db, dispatch={"strategy": "integrate", "role": "integrator", "integrates": []})
    worker, _, _ = worker_factory()
    _harvest(worker, {**PUSH_FAILED, "patch": None, "patch_omitted": True,
                      "patch_note": "the diff was over the cap"})

    assert worker.run() == ExitCode.FAILED
    error = db.doc("tasks/task_1")["last_error"]
    assert f"No {PATCH_NAME} was uploaded" in error, error
    assert "final checkpoint" in error


@pytest.mark.parametrize(
    "dispatch",
    [
        {"strategy": "integrate", "carrier": "checkpoints", "role": "contributor"},
        {"strategy": "collect"},
    ],
    ids=["integrate-contributor", "collect-review"],
)
def test_a_step_that_opens_no_pull_request_is_unaffected_by_a_failed_push(
    db, worker_factory, dispatch
):
    _seed(db, dispatch=dispatch)
    worker, _, _ = worker_factory()
    _harvest(worker, PUSH_FAILED, write_patch=True)

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


pytest_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest_git
def test_a_push_git_refuses_is_recorded_as_publish_failed(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The field the finish reads is set by the real `_publish_git`."""

    def refuse(**_kwargs: Any) -> str:
        raise GitError(PUSH_ERROR)

    monkeypatch.setattr(lifecycle, "push_branch", refuse)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-push-refused",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
    )
    assert out["published"] is False
    assert out[PUBLISH_FAILED_FIELD] is True
    assert PUSH_ERROR in out["publish_reason"]
    assert forge.pulls == []


# ---------------------------------------------------------------------------
# 2. Proposal J: no change plus verification.md is a result
# ---------------------------------------------------------------------------

VERIFIED = "Already on main: commit abc1234 adds the guard; tests/unit/test_x.py covers it.\n"


def test_a_direct_pr_step_that_verified_there_is_nothing_to_change_succeeds(db, worker_factory):
    _seed(db, dispatch={"strategy": "direct-pr"},
          artifact_name=VERIFICATION_FILE, artifact_text=VERIFIED)
    worker, _, _ = worker_factory()
    _harvest(worker, EMPTY_DIFF)

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert summary["no_change"] is True
    assert summary[NO_CHANGE_REASON_KEY] == VERIFIED.strip()
    assert VERIFICATION_FILE in [a["name"] for a in summary["artifacts"]]


def test_a_step_without_the_flag_that_wrote_a_verification_does_not_fail_empty_diff(
    db, worker_factory
):
    _seed(db, dispatch={"strategy": "collect", "carrier": "checkpoints"},
          expected=[PATCH_NAME, VERIFICATION_FILE],
          artifact_name=VERIFICATION_FILE, artifact_text=VERIFIED)
    worker, _, _ = worker_factory()
    _harvest(worker, EMPTY_DIFF)

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["no_change"] is True
    assert "expected_outputs_missing" not in task["result_summary"]


def test_no_change_without_a_verification_still_fails(db, worker_factory):
    _seed(db, dispatch={"strategy": "direct-pr"})
    worker, _, _ = worker_factory()
    _harvest(worker, EMPTY_DIFF)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["last_error"].startswith("published_nothing: "), task["last_error"]
    assert "no_change" not in task["result_summary"]


def test_a_blank_verification_is_no_verification(db, worker_factory):
    _seed(db, dispatch={"strategy": "direct-pr"},
          artifact_name=VERIFICATION_FILE, artifact_text="  \n")
    worker, _, _ = worker_factory()
    _harvest(worker, EMPTY_DIFF)

    assert worker.run() == ExitCode.FAILED


def test_a_verification_beside_a_change_is_not_no_change(db, worker_factory):
    _seed(db, dispatch={"strategy": "collect"},
          artifact_name=VERIFICATION_FILE, artifact_text=VERIFIED)
    worker, _, _ = worker_factory()
    _harvest(worker, {**EMPTY_DIFF, "head": "b" * 40, "commit_count": 1,
                      "patch": PATCH_NAME, "patch_bytes": 20}, write_patch=True)

    assert worker.run() == ExitCode.OK
    assert "no_change" not in db.doc("tasks/task_1")["result_summary"]


# ---------------------------------------------------------------------------
# 3. Proposal A: the platform paragraph
# ---------------------------------------------------------------------------


def test_a_prompt_with_a_repository_ends_with_the_publish_paragraph(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"}, repo=True)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    claude_code.body(ctx)
    prompt = _runs(ctx)[0]["prompt"]
    assert prompt.rstrip().endswith(cliagent.PUBLISH_PARAGRAPH), prompt[-600:]
    assert prompt.count(cliagent.PUBLISH_PARAGRAPH) == 1
    for phrase in ("You do not push or open a pull request",
                   "$SWARM_ARTIFACTS_DIR/pr-title.txt", "$SWARM_ARTIFACTS_DIR/pr-body.md",
                   "Commit your work."):
        assert phrase in cliagent.PUBLISH_PARAGRAPH


def test_a_prompt_without_a_repository_has_no_publish_paragraph(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "summarise the notes"}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    claude_code.body(ctx)
    assert cliagent.PUBLISH_PARAGRAPH not in _runs(ctx)[0]["prompt"]


def test_the_publish_paragraph_carries_no_marker_a_cli_echo_would_trip():
    text = cliagent.PUBLISH_PARAGRAPH.lower()
    markers = (*cliagent._RATE_LIMIT_MARKERS, *cliagent._CREDENTIAL_MARKERS)
    assert [m for m in markers if m in text] == []


# ---------------------------------------------------------------------------
# 4. Proposal B: an issue step is asked for its title, then marked
# ---------------------------------------------------------------------------

REPO = "https://github.com/example/repo"
ISSUE_TASK = {
    "task_id": "task_1",
    "repository_url": REPO,
    "input": {"prompt": "fix it", "issue": 5},
    "metadata": {"dispatch": {"strategy": "direct-pr"}},
}


def test_an_issue_step_is_asked_for_its_title(worker_factory):
    worker, _, _ = worker_factory(repository_url=REPO)
    worker._task = dict(ISSUE_TASK)
    assert PR_TITLE_FILE in worker._declared_outputs(worker._task)


def test_an_issue_steps_missing_title_is_excused_and_the_fallback_is_marked(worker_factory):
    worker, _, _ = worker_factory(repository_url=REPO)
    worker._task = dict(ISSUE_TASK)
    worker._issue_title = "The widget accepts a negative size"
    worker._declared_outputs(worker._task)

    assert worker._report_missing_outputs({"artifacts": []}, fails_the_attempt=True) == []
    assert worker._generated_pull_request_title() == (
        "[title missing] The widget accepts a negative size (part of #5)"
    )
    # Both the marked fallback and an older unmarked one are the worker's own.
    assert worker._is_default_title("[title missing] The widget accepts a negative size (part of #5)")
    assert worker._is_default_title("The widget accepts a negative size (part of #5)")


def test_a_step_without_an_issue_still_fails_for_a_missing_title(worker_factory):
    worker, _, _ = worker_factory(repository_url=REPO)
    task = {**ISSUE_TASK, "input": {"prompt": "fix it"}}
    worker._task = task
    worker._declared_outputs(task)
    assert worker._generated_pull_request_title() is None
    assert worker._report_missing_outputs({"artifacts": []}, fails_the_attempt=True) == [
        PR_TITLE_FILE
    ]


def test_a_title_a_dependant_declared_is_never_excused(worker_factory):
    worker, _, _ = worker_factory(repository_url=REPO)
    task = {**ISSUE_TASK, "metadata": {**ISSUE_TASK["metadata"],
                                       "expected_outputs": [PR_TITLE_FILE]}}
    worker._task = task
    worker._declared_outputs(task)
    assert worker._report_missing_outputs({"artifacts": []}, fails_the_attempt=True) == [
        PR_TITLE_FILE
    ]


def test_a_missing_title_gets_exactly_one_follow_up_turn(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it", "expected_outputs": [PR_TITLE_FILE]}, repo=True)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert [run["resume"] for run in runs] == [None, SESSION], runs
    assert str(Path(ctx.artifacts_dir).resolve() / PR_TITLE_FILE) in runs[1]["prompt"]
    assert out["repair_turns"] == 1
    assert out["repair_unresolved"]["missing_outputs"] == [PR_TITLE_FILE]
