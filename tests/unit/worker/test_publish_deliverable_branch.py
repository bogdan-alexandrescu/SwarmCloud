"""G1 DELIVERABLE-BRANCH (docs/runner-git-tools.md §6.1, §7; part of #1225).

THE MEASURED FAILURE (wf_e55610ded71c4d9d9fae, wf_0260c920acf8414395b2). A
review step failed cloning "Remote branch swarm/task_... not found": the build
step it `builds_on` had ended SUCCEEDED without pushing its branch. That build
step was an `integrate` contributor, and #872 failed a step for an unpushed
branch only when the step opened a pull request (`_opens_pull_request`), so a
contributor read SUCCEEDED however its push went.

WHAT IS PINNED.

  * A step whose BRANCH is its deliverable -- an `integrate` contributor, a
    `carrier: branches` step, a `single-pr` amender, a pull-request step, and
    so a step another step `builds_on` -- ends FAILED `publish_failed:` when
    it changed something and its branch was not pushed, by ANY path: a push
    git refused, a publish that declined (no write access, a refused token,
    a forge the worker cannot publish to, publishing disabled), a harvest
    that failed or met survivors of the reap, and the catch-all in
    `_publish_checked`.
  * A PR step whose publish declined for `can_push=False` ends FAILED (today
    SUCCEEDED, with only a patch).
  * A push that fails once with a connection reset and then succeeds is
    published (`gitops.push_branch` tries once more on git's transient words).
    A refusal is not tried again, and a second transient failure is raised.
  * Controls: a `collect` step without a carrier still SUCCEEDS with nothing
    pushed; a contributor that changed nothing still SUCCEEDS; a contributor
    whose forge was unreachable at the probe fails RETRYABLY, not for good.

MUTATIONS: make `_deliverable_branch` return "" for every step but a
pull-request step -- the contributor, carrier, amender and builds-on tests see
SUCCEEDED. Drop the "any path" half of `_publish_failed` (require
`publish_failed: true`) -- the declined-publish tests and the `can_push=False`
test see SUCCEEDED. Drop `PUBLISH_FAILED_FIELD` from `_publish_checked`'s
catch-all -- its test fails. Set `PUSH_TRIES = 1` -- the reset-then-succeeds
test raises `GitError`. Drop `transient_git_failure` from the retry -- the
refusal test sees two pushes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle
from agent_worker.gitops import GitError
from agent_worker.lifecycle import PATCH_NAME
from swarm_common.states import EventType, TaskState

from worker_seeds import seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    agent_edits_without_committing,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
)

#: Spelled out rather than read from `lifecycle`, so each test fails on its
#: own without the change instead of the module failing to collect.
PUBLISH_FAILED_FIELD = "publish_failed"
PUBLISH_FAILED_PREFIX = "publish_failed: "

pytest_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PUSH_ERROR = (
    "push failed with exit 1: error: failed to push some refs to "
    "'https://github.com/example/repo'"
)

#: A harvest that found work: commits or uncommitted files, so the step had
#: a branch to push.
WORK = {
    "base": "a" * 40, "head": "b" * 40, "commit_count": 2, "dirty_count": 3,
    "patch": PATCH_NAME, "patch_bytes": 20, "patch_omitted": False,
}

#: `_publish_git`'s GitError handler: the push (or the commit, fold or
#: authorship check before it) failed.
PUSH_FAILED = {
    **WORK, "published": False, "auto_committed": True, "publish_reason": PUSH_ERROR,
    PUBLISH_FAILED_FIELD: True,
}

#: What `_harvest_git` reports for a clone the agent left exactly as it found it.
EMPTY_DIFF = {
    "base": "a" * 40, "head": "a" * 40, "patch": None, "patch_bytes": 0,
    "patch_omitted": False, "commit_count": 0, "dirty_count": 0,
    "published": False, "publish_reason": "the agent changed nothing in the repository",
}

CONTRIBUTOR = {"strategy": "integrate", "carrier": "checkpoints", "role": "contributor"}
CARRIER_COLLECT = {"strategy": "collect", "carrier": "branches"}
AMENDER = {"strategy": "single-pr", "pr_role": "amender", "pr_author": "t-impl"}


def _seed(db: Any, *, dispatch: dict[str, Any], attempt_count: int = 1) -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "do the work", "steps": 1, "sleep_seconds": 0.01,
            "artifact_name": "notes.md", "artifact_text": "notes\n",
        },
        attempt_count=attempt_count,
    )
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dict(dispatch)}


def _harvest(worker: Any, git: dict[str, Any], *, write_patch: bool = False) -> None:
    """Stand in for the harvest and its publish; the real publish is below."""

    def harvest(**_kwargs: Any) -> dict[str, Any]:
        if write_patch:
            (worker.ws.artifacts / PATCH_NAME).write_text("diff --git a/x b/x\n")
        return dict(git)

    worker._harvest_git = harvest  # type: ignore[method-assign]


def _finish(db: Any, worker_factory: Any, *, dispatch: dict[str, Any],
            git: dict[str, Any], write_patch: bool = False) -> dict[str, Any]:
    _seed(db, dispatch=dispatch)
    worker, _, _ = worker_factory()
    _harvest(worker, git, write_patch=write_patch)
    worker.run()
    return db.doc("tasks/task_1")


def _retried(db: Any) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]


def _assert_publish_failed(db: Any, task: dict[str, Any], *, cause: str) -> str:
    assert task["state"] == TaskState.FAILED.value, (task["state"], task.get("last_error"))
    error = task["last_error"]
    assert error.startswith(PUBLISH_FAILED_PREFIX), error
    assert cause in error, error
    assert "branch was not pushed" in error, error
    assert task["end_cause"] == "outputs_missing", task["end_cause"]
    # Not retried: the same branch meets the same refusal.
    assert _retried(db) == []
    return error


# ---------------------------------------------------------------------------
# A push git refused: every step whose branch is its deliverable
# ---------------------------------------------------------------------------


def test_an_integrate_contributor_whose_push_failed_ends_failed(db, worker_factory):
    """The #1225 shape: today SUCCEEDED, and the review that builds on it
    fails cloning a branch that was never pushed."""
    task = _finish(db, worker_factory, dispatch=CONTRIBUTOR, git=PUSH_FAILED, write_patch=True)

    error = _assert_publish_failed(db, task, cause=PUSH_ERROR)
    assert "contributor" in error, error
    (patch,) = [a for a in task["result_summary"]["artifacts"] if a["name"] == PATCH_NAME]
    assert patch["uri"] in error, error
    assert "swarm_apply" in error


@pytest.mark.parametrize(
    ("dispatch", "names"),
    [
        (CARRIER_COLLECT, "carrier: branches"),
        ({**CONTRIBUTOR, "carrier": "branches"}, "carrier: branches"),
        (AMENDER, "amender"),
    ],
    ids=["collect-carrier-branches", "contributor-carrier-branches", "single-pr-amender"],
)
def test_every_step_whose_branch_is_its_deliverable_fails_for_a_failed_push(
    db, worker_factory, dispatch, names
):
    task = _finish(db, worker_factory, dispatch=dispatch, git=PUSH_FAILED)

    error = _assert_publish_failed(db, task, cause=PUSH_ERROR)
    assert names in error, error
    assert "final checkpoint" in error, error


@pytest_git
def test_a_contributor_whose_real_push_raises_ends_failed(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """End to end through the real `_publish_git`: its GitError handler's
    record, read by the finish, fails the contributor."""

    def refuse(**_kwargs: Any) -> str:
        raise GitError(PUSH_ERROR)

    monkeypatch.setattr(lifecycle, "push_branch", refuse)
    before = refs(origin)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-contributor-refused", dispatch=CONTRIBUTOR,
        edit=agent_edits_without_committing,
    )
    assert out[PUBLISH_FAILED_FIELD] is True
    assert refs(origin) == before

    task = _finish(db, worker_factory, dispatch=CONTRIBUTOR, git=out)
    _assert_publish_failed(db, task, cause=PUSH_ERROR)


# ---------------------------------------------------------------------------
# A publish that declined, or a harvest that never reached it
# ---------------------------------------------------------------------------

#: Every path #1225 §6.1 names that ends with work and no pushed branch
#: WITHOUT `publish_failed`, in the shape `_harvest_git` / `_publish_git`
#: record it.
DECLINED = {
    "no-write-access": {
        **WORK, "published": False, "can_push": False,
        "publish_reason": "the token has pull but not push",
    },
    "token-refused": {
        **WORK, "published": False,
        "publish_reason": "refusing to publish: the worker is still dumpable",
    },
    "no-forge-access": {
        **WORK, "published": False,
        "publish_reason": "this repository is not on a forge this worker can publish to",
    },
    "publishing-disabled": {
        **WORK, "published": False, "publish_reason": "publishing is disabled for this worker",
    },
    "survivors-of-the-reap": {
        "base": "a" * 40, "published": False,
        "publish_reason": "refusing to publish: 1 process(es) the agent started are still alive",
        "error": "the agent's work was not harvested: processes it started survived the reap",
    },
    "harvest-git-error": {
        "base": "a" * 40, "published": False,
        "publish_reason": "nothing was published: could not fetch the agent's commits",
        "error": "could not fetch the agent's commits",
    },
}


@pytest.mark.parametrize("why", sorted(DECLINED))
def test_a_step_another_step_builds_on_fails_when_its_publish_declined(
    db, worker_factory, why
):
    """The build step a review `builds_on` is a contributor (`builds_on` is
    accepted only under `direct-pr` and `integrate`). Each of these left the
    review cloning a missing branch from a SUCCEEDED build step."""
    git = DECLINED[why]
    task = _finish(db, worker_factory, dispatch=CONTRIBUTOR, git=git)

    _assert_publish_failed(db, task, cause=git["publish_reason"])


@pytest_git
def test_a_pr_step_whose_publish_declined_for_can_push_false_ends_failed(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """The forge said the token cannot push: today a stated fact and SUCCEEDED,
    with the work only in a patch and no pull request."""
    forge.can_push = False
    before = refs(origin)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-pr-readonly", dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
    )
    assert out["published"] is False and out["can_push"] is False
    assert refs(origin) == before
    assert forge.pulls == []

    task = _finish(db, worker_factory, dispatch={"strategy": "direct-pr"}, git=out)
    error = _assert_publish_failed(db, task, cause="pull but not push")
    assert "open a pull request" in error, error


def test_the_publish_catch_all_is_recorded_as_publish_failed(worker_factory, monkeypatch):
    """`_publish_checked` never raises; what it records when the publish did
    is what the finish reads."""
    worker, _, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": dict(CONTRIBUTOR)}}
    worker._deferred_publish = {"repo": Path("/nonexistent"), "work_head": "b" * 40,
                                "publish_repo": None}

    def boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(worker, "_publish_git", boom)
    summary: dict[str, Any] = {"git": dict(WORK)}
    worker._publish_checked(summary, None)

    assert summary["git"]["published"] is False
    assert summary["git"][PUBLISH_FAILED_FIELD] is True
    assert worker._publish_failed(summary) is not None


# ---------------------------------------------------------------------------
# A push the forge did not answer is tried once more
# ---------------------------------------------------------------------------

RESET = (
    "fatal: unable to access 'https://github.com/acme/widgets/': "
    "Recv failure: Connection reset by peer"
)
REFUSED = "remote: Permission denied to someone.\nfatal: unable to access: 403"


def _publish_repo(tmp_path: Path, origin: Path) -> Path:
    repo = tmp_path / "publish"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(repo)],
                   check=True, capture_output=True)
    (repo / "work.txt").write_text("the step's work\n")
    for args in (["add", "-A"], ["commit", "--quiet", "-m", "work"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        *args], cwd=str(repo), check=True, capture_output=True)
    return repo


def _failing_pushes(monkeypatch, failures: list[str]) -> list[int]:
    """The first `len(failures)` pushes fail with git's words; later ones run."""
    real = gitops._git_text
    calls: list[int] = []

    def git_text(argv: list[str], **kwargs: Any) -> tuple[int, str]:
        if kwargs.get("slug") != "publish-push":
            return real(argv, **kwargs)
        calls.append(1)
        if len(calls) <= len(failures):
            logs = Path(kwargs["logs_dir"])
            (logs / "git-publish-push.err.log").write_text(failures[len(calls) - 1] + "\n")
            return 128, ""
        return real(argv, **kwargs)

    monkeypatch.setattr(gitops, "_git_text", git_text)
    # `raising=False`: absent before G1, so the test fails on the push, not here.
    monkeypatch.setattr(gitops, "PUSH_RETRY_WAIT_SECONDS", 0, raising=False)
    return calls


def _push(tmp_path: Path, origin: Path, repo: Path) -> str:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return gitops.push_branch(
        repo=repo,
        url=f"file://{origin}",
        branch="swarm/t-retry",
        # Built at runtime: nothing here may look like a credential.
        token="fake-" + "0" * 8,
        private_dir=tmp_path / "private",
        logs_dir=logs,
        timeout_seconds=60,
        logger=_Log(),
        protected=("main",),
    )


class _Log:
    """A logger that keeps nothing: these tests read the remote, not the log."""

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


@pytest_git
def test_a_push_reset_once_and_then_accepted_is_published(
    tmp_path, monkeypatch, origin, local_urls
):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [RESET])

    pushed = _push(tmp_path, origin, repo)

    assert len(calls) == 2
    assert refs(origin).get("swarm/t-retry") == pushed
    assert pushed


@pytest_git
def test_a_refused_push_is_not_tried_again(tmp_path, monkeypatch, origin, local_urls):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [REFUSED])

    with pytest.raises(GitError, match="push failed"):
        _push(tmp_path, origin, repo)
    assert len(calls) == 1
    assert "swarm/t-retry" not in refs(origin)


@pytest_git
def test_a_push_reset_twice_is_retried_only_once(tmp_path, monkeypatch, origin, local_urls):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [RESET, RESET, RESET])

    with pytest.raises(GitError, match="Connection reset"):
        _push(tmp_path, origin, repo)
    assert len(calls) == 2
    assert "swarm/t-retry" not in refs(origin)


# ---------------------------------------------------------------------------
# The controls: what still SUCCEEDS
# ---------------------------------------------------------------------------


def test_a_collect_step_without_a_carrier_succeeds_with_nothing_pushed(db, worker_factory):
    git = {
        **WORK, "strategy": "collect", "published": False,
        "publish_reason": "strategy is 'collect': the patch is harvested and nothing is pushed",
    }
    task = _finish(db, worker_factory, dispatch={"strategy": "collect"}, git=git)

    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")


@pytest.mark.parametrize(
    "dispatch", [CONTRIBUTOR, CARRIER_COLLECT, AMENDER],
    ids=["contributor", "collect-carrier-branches", "single-pr-amender"],
)
def test_a_deliverable_branch_step_that_changed_nothing_still_succeeds(
    db, worker_factory, dispatch
):
    """No branch to push: the integrator and `builds_on` already step past it."""
    task = _finish(db, worker_factory, dispatch=dispatch, git=EMPTY_DIFF)

    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")


def test_a_contributor_whose_forge_was_unreachable_is_failed_retryably(db, worker_factory):
    """An outage may be over by the next attempt; a refusal would not be."""
    marker = {"reason": "could not reach api.github.com: timed out", "tries": 4,
              "retry_after_seconds": None}
    git = {**WORK, "published": False, "publish_reason": "could not reach the forge",
           lifecycle.PUBLISH_UNREACHABLE_FIELD: marker}
    task = _finish(db, worker_factory, dispatch=CONTRIBUTOR, git=git)

    assert task["state"] != TaskState.SUCCEEDED.value
    assert task["last_error"].startswith(f"{lifecycle.FORGE_UNREACHABLE}: "), task["last_error"]
