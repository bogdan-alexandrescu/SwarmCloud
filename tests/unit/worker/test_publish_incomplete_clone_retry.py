"""A publish fetch a partial clone still fails names the object, and the attempt is retried (issue 367).

WHAT WENT WRONG (2026-11-24). A fix step's publish fetch died on an object
its partial clone never downloaded; the step ended `publish_failed` with end
cause `outputs_missing`, `fail_workflow` cancelled the re-review, and about
thirteen agent-hours stayed only in `swarm-work.patch`. Step one of the fix
completes such a clone from origin; this file pins what happens when that
completion still cannot make the fetch succeed.

WHAT IS PINNED.

  * `prepare_publish_repo` raises `PublishFetchIncomplete` carrying the
    missing object's full sha, its message today's fixed text first and
    verbatim, then the object and one of "origin has it", "origin does not
    have it", "origin was not asked" (`probe_on_origin`, asked at most once).
    A failure that names no object is today's plain `GitError`.
  * `forge_holds_object` answers for any object type, a blob included.
  * The harvest records it as a retryable publish failure: the attempt ends
    RETRYABLY with the patch kept, the task back in READY.
  * The retry publishes the kept work without the agent when origin has, or
    was not asked about, the object and the head and base still hold; it
    runs the agent when origin does not have it.
  * A plain `GitError` from the publish fetch still ends `publish_failed`.
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle
from agent_worker.gitops import GitError
from agent_worker.lifecycle import PATCH_NAME
from swarm_common.states import TaskState

from test_publish_partial_clone import FIXED, PartialMerge, _logger
from test_harvest_shallow_merge import MergedBase
from test_push_retry_republish import CONTRIBUTOR, _finish_with, _patch_entry
from worker_seeds import record_as_earlier_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _attempt,
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)

pytest_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ---------------------------------------------------------------------------
# prepare_publish_repo: the object, and origin's answer
# ---------------------------------------------------------------------------


def _incomplete(pr: PartialMerge, **kwargs: Any) -> Any:
    with pytest.raises(gitops.PublishFetchIncomplete) as raised:
        pr.prepare(**kwargs)
    return raised.value


@pytest_git
def test_the_failure_names_the_missing_object_and_says_origin_was_not_asked(tmp_path: Path):
    pr = PartialMerge(tmp_path)
    exc = _incomplete(pr)
    assert isinstance(exc, GitError)
    assert exc.missing in pr.missing
    assert exc.on_origin is None
    text = str(exc)
    assert text.startswith(FIXED + " (an object failed git's checks, or the clone's history "
                           "is incomplete); nothing was pushed")
    assert exc.missing in text and "origin was not asked" in text


@pytest_git
def test_the_failure_says_origin_has_it_when_the_probe_answers_yes(tmp_path: Path):
    pr = PartialMerge(tmp_path)
    asked: list[str] = []

    def probe(sha: str) -> bool:
        asked.append(sha)
        return True

    exc = _incomplete(pr, probe_on_origin=probe)
    assert asked == [exc.missing]
    assert exc.on_origin is True
    assert str(exc).startswith(FIXED) and "origin has it" in str(exc)


@pytest_git
def test_the_failure_says_origin_does_not_have_it_when_the_probe_answers_no(tmp_path: Path):
    pr = PartialMerge(tmp_path)
    exc = _incomplete(pr, probe_on_origin=lambda sha: False)
    assert exc.on_origin is False
    assert str(exc).startswith(FIXED) and "origin does not have it" in str(exc)


@pytest_git
def test_a_probe_that_raises_is_no_answer(tmp_path: Path):
    pr = PartialMerge(tmp_path)

    def boom(sha: str) -> bool:
        raise RuntimeError("the forge is down")

    exc = _incomplete(pr, probe_on_origin=boom)
    assert exc.on_origin is None and "origin was not asked" in str(exc)


@pytest_git
def test_a_failure_that_names_no_object_is_todays_plain_git_error(tmp_path: Path):
    """A shallow cut-off the walk crossed ("Could not read"), not a promisor
    miss: no object is parsed and the probe is never asked."""
    pr = MergedBase(tmp_path)
    asked: list[str] = []
    with pytest.raises(GitError) as raised:
        pr.prepare(probe_on_origin=lambda sha: asked.append(sha) or True)
    assert type(raised.value) is GitError
    assert str(raised.value).startswith(FIXED)
    assert asked == []


# ---------------------------------------------------------------------------
# forge_holds_object
# ---------------------------------------------------------------------------


@pytest_git
def test_forge_holds_object_answers_for_a_blob(tmp_path: Path, local_urls):
    pr = PartialMerge(tmp_path)
    blob = subprocess.run(["git", "-C", str(pr.origin), "rev-parse", "main:f1"], check=True,
                          capture_output=True, text=True).stdout.strip()
    common = {"url": pr.url, "token": secrets.token_hex(8), "private_dir": tmp_path / "probe",
              "logs_dir": pr.logs, "timeout_seconds": 60, "logger": _logger()}
    assert gitops.forge_holds_object(sha=blob, **common) is True
    assert gitops.forge_holds_object(sha=pr.contrib, **common) is True
    assert gitops.forge_holds_object(sha=secrets.token_hex(20), **common) is False
    assert gitops.forge_holds_object(sha="not-a-sha", **common) is False
    # No probe repository or credential file is left behind.
    assert not list((tmp_path / "probe").glob("forge-probe-*"))
    assert not list((tmp_path / "probe").glob("*cred*"))


# ---------------------------------------------------------------------------
# The worker: a retryable end with the work kept, and the retry
# ---------------------------------------------------------------------------


def _raising(on_origin: bool | None):
    """The clean repository's build fails as a partial clone's would; the
    tree patch's own repository (built from the base alone) is built as ever."""
    missing = secrets.token_hex(20)
    real = lifecycle.prepare_publish_repo

    def prepare(**kwargs: Any) -> Path:
        if kwargs.get("local_branch") == "swarm-tree-patch":
            return real(**kwargs)
        raise gitops.PublishFetchIncomplete(
            f"{FIXED} (an object failed git's checks, or the clone's history is incomplete); "
            f"nothing was pushed; the agent's clone lacks object {missing}",
            missing=missing, on_origin=on_origin,
        )

    return prepare


def _first_attempt_whose_fetch_is_incomplete(db, worker_factory, monkeypatch, origin,
                                             on_origin: bool | None):
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-partial", attempt=1, dispatch=CONTRIBUTOR
    )
    assert first._maybe_clone(task)["commit"], "the clone did not land"
    repo = first.ws.work / lifecycle.REPO_DIR_NAME
    (repo / "committed.txt").write_text("work the agent committed\n")
    for args in (["add", "committed.txt"], ["commit", "--quiet", "-m", "agent work"]):
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.invalid", *args],
                       cwd=str(repo), check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout.strip()
    record = first.checkpoints.create(first.ws, label="final")
    real = lifecycle.prepare_publish_repo
    monkeypatch.setattr(lifecycle, "prepare_publish_repo", _raising(on_origin))
    out = first._harvest_git(publish=True)
    monkeypatch.setattr(lifecycle, "prepare_publish_repo", real)
    return first, record, out, head


@pytest_git
@pytest.mark.parametrize("on_origin", [True, False, None])
def test_the_harvest_records_an_incomplete_clone_as_retryable(
    db, worker_factory, monkeypatch, origin, local_urls, forge, on_origin
):
    first, _, out, head = _first_attempt_whose_fetch_is_incomplete(
        db, worker_factory, monkeypatch, origin, on_origin
    )
    assert out["published"] is False
    assert out.get(lifecycle.PUBLISH_FAILED_FIELD) is not True
    marker = out[lifecycle.PUBLISH_UNREACHABLE_FIELD]
    assert marker["publish"] == lifecycle.REPUBLISH_FETCH
    assert marker["head"] == head
    assert marker["base"] == first.checkpoints.clone_base
    assert marker["on_origin"] is on_origin
    assert len(marker["missing"]) == 40
    assert (first.ws.artifacts / PATCH_NAME).is_file(), "the patch was not kept"


def test_a_step_whose_publish_fetch_was_incomplete_ends_retryably_with_its_patch_kept(
    db, worker_factory
):
    missing = secrets.token_hex(20)
    marker = {"reason": f"{FIXED} ...; nothing was pushed; the agent's clone lacks object "
                        f"{missing}, and origin has it",
              "tries": 1, "retry_after_seconds": None, "publish": lifecycle.REPUBLISH_FETCH,
              "head": "b" * 40, "base": "a" * 40, "missing": missing, "on_origin": True}
    git = {"published": False, "publish_reason": f"nothing was published: {marker['reason']}",
           "error": marker["reason"], lifecycle.PUBLISH_UNREACHABLE_FIELD: marker}

    task = _finish_with(db, worker_factory, git)

    assert task["state"] == TaskState.READY.value, (task["state"], task.get("last_error"))
    error = task["last_error"]
    assert error.startswith(f"{lifecycle.PUBLISH_FETCH_INCOMPLETE}: "), error
    assert missing in error and "origin has it" in error
    assert PATCH_NAME in error
    names = {entry["name"] for entry in task["result_summary"]["artifacts"]}
    assert PATCH_NAME in names


def test_a_plain_git_error_from_the_publish_fetch_still_ends_publish_failed(
    db, worker_factory
):
    reason = (f"{FIXED} (an object failed git's checks, or the clone's history is "
              "incomplete); nothing was pushed; git: fatal: Could not read 1234")
    git = {"published": False, "publish_reason": f"nothing was published: {reason}",
           "error": reason}

    task = _finish_with(db, worker_factory, git)

    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["last_error"].startswith("publish_failed"), task["last_error"]


@pytest_git
def test_the_harvest_still_reads_a_plain_git_error_as_a_failure(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-plain", attempt=1, dispatch=CONTRIBUTOR
    )
    assert first._maybe_clone(task)["commit"]

    def prepare(**kwargs: Any) -> Path:
        raise GitError(f"{FIXED} (...); nothing was pushed")

    monkeypatch.setattr(lifecycle, "prepare_publish_repo", prepare)
    out = first._harvest_git(publish=True)
    assert out["published"] is False
    assert lifecycle.PUBLISH_UNREACHABLE_FIELD not in out


def _second_attempt(db, worker_factory, monkeypatch, origin, record):
    second, config, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-partial", attempt=2, dispatch=CONTRIBUTOR
    )
    record_as_earlier_attempt(db, record, task_id="t-partial")
    second._restore_checkpoint({"attempt_count": 2, "latest_checkpoint": record.uri})
    assert second._maybe_clone(task)["from_checkpoint"] is True
    return second, config, task


@pytest_git
@pytest.mark.parametrize("on_origin", [True, None])
def test_the_retry_publishes_the_kept_work_when_origin_has_or_was_not_asked(
    db, worker_factory, monkeypatch, origin, local_urls, forge, on_origin
):
    _, record, out, head = _first_attempt_whose_fetch_is_incomplete(
        db, worker_factory, monkeypatch, origin, on_origin
    )
    second, config, task = _second_attempt(db, worker_factory, monkeypatch, origin, record)
    task["result_summary"] = {"git": out, "artifacts": [_patch_entry()]}

    republish = second._push_to_republish(task)

    assert republish is not None
    assert republish["head"] == head
    again = second._harvest_git(publish=True)
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert again["published"] is True, again.get("publish_reason")
    assert "committed.txt" in tree_at(origin, branch)


@pytest_git
def test_the_retry_runs_the_agent_when_origin_does_not_have_the_object(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _, record, out, _ = _first_attempt_whose_fetch_is_incomplete(
        db, worker_factory, monkeypatch, origin, False
    )
    second, _, task = _second_attempt(db, worker_factory, monkeypatch, origin, record)
    task["result_summary"] = {"git": out, "artifacts": [_patch_entry()]}

    assert second._push_to_republish(task) is None
    # The same marker with origin's answer flipped would republish: the
    # answer, not the shape, decides.
    flipped = {**out[lifecycle.PUBLISH_UNREACHABLE_FIELD], "on_origin": True}
    assert second._push_to_republish(
        {**task, "result_summary": {"git": {**out, lifecycle.PUBLISH_UNREACHABLE_FIELD: flipped},
                                    "artifacts": [_patch_entry()]}}
    ) is not None
    # A moved head runs the agent, as for a push outage.
    moved = {**flipped, "head": "c" * 40}
    assert second._push_to_republish(
        {**task, "result_summary": {"git": {**out, lifecycle.PUBLISH_UNREACHABLE_FIELD: moved},
                                    "artifacts": [_patch_entry()]}}
    ) is None


@pytest_git
def test_the_clean_repository_is_built_with_the_workers_own_origin_probe(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """`_build_clean_repo` hands `prepare_publish_repo` a probe that asks the
    forge itself (`forge_holds_object`), with the worker's own token."""
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-probe", attempt=1, dispatch=CONTRIBUTOR
    )
    assert first._maybe_clone(task)["commit"]
    seen: dict[str, Any] = {}
    real = lifecycle.prepare_publish_repo

    def prepare(**kwargs: Any) -> Path:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "prepare_publish_repo", prepare)
    first._harvest_git(publish=False)
    probe = seen["probe_on_origin"]
    assert callable(probe)
    on_main = subprocess.run(["git", "-C", str(origin), "rev-parse", "main:README.md"],
                             check=True, capture_output=True, text=True).stdout.strip()
    assert probe(on_main) is True
    assert probe(secrets.token_hex(20)) is False
