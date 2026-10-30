"""A push the forge does not answer: backoff, a retryable end, and a re-publish (#2833).

THE MEASURED FAILURE (2026-10-30, wf_379721f4e8f045dc891d, task_982546a3b645480887e3).
A step's publish pushed its branch and GitHub answered "remote: Internal Server
Error". `push_branch` tried twice, 5 s apart, `_publish_failed` ended the step
FAILED, its dependants were cancelled and the workflow ended FAILED, with the
finished work only in `swarm-work.patch`.

WHAT IS PINNED.

  * A push that meets two 500s and then lands is published: four tries in
    all, waits that grow and stay under the in-worker bound (invariant 4).
  * A push that meets 500s throughout raises `PushUnreachable`; the publish
    records it as an outage (`forge_unreachable`, `publish: push`, the head
    and base it pushed from), never as `publish_failed`, and the attempt ends
    RETRYABLY with the patch named, the task back in READY so its dependants
    keep waiting.
  * A non-fast-forward and a refusal are still raised at the first try, as
    `PushRejected` and a plain `GitError`.
  * The retry restores the checkpoint; when its clone is still at the head
    and base the failed push carried, it publishes that work without running
    the agent, carrying the previous attempt's uploads; when either moved, the
    agent runs as on any retry.

MUTATIONS: `PUSH_RETRY_WAITS_SECONDS = (5.0,)` -- the two-500s test raises.
Drop the `PushUnreachable` branch in `_publish_git` -- the outage test sees
`publish_failed` and the step ends FAILED. Drop STEP 5e in `_prepare` -- the
skip test sees the runner's own artifact and no carried one. Drop the HEAD
check in `_push_to_republish` -- the moved-head test sees a republish.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

import pytest

from agent_worker import gitops, lifecycle
from agent_worker.gitops import GitError, PushRejected
from agent_worker.lifecycle import PATCH_NAME
from swarm_common.states import EventType, TaskState

from worker_seeds import TENANT, record_as_earlier_attempt, seed_attempt
from test_publish_deliverable_branch import REFUSED, _failing_pushes, _publish_repo, _push
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

#: What git prints for a forge 500 on a push over HTTPS.
HTTP_500 = (
    "error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
    "send-pack: unexpected disconnect while reading sideband packet\n"
    "fatal: the remote end hung up unexpectedly"
)
#: The words the measured failure logged: the reason phrase alone, no code.
SIDEBAND_500 = "remote: Internal Server Error\nerror: failed to push some refs"
NON_FAST_FORWARD = (
    " ! [rejected]        HEAD -> swarm/t-retry (non-fast-forward)\n"
    "error: failed to push some refs\n"
    "hint: Updates were rejected because the tip of your current branch is behind"
)

CONTRIBUTOR = {"strategy": "integrate", "carrier": "checkpoints", "role": "contributor"}


#: The real waits, read before `_failing_pushes` zeroes them. A default, so
#: this module collects on a main without them and each test fails on its own.
WAITS = getattr(gitops, "PUSH_RETRY_WAITS_SECONDS", (gitops.PUSH_RETRY_WAIT_SECONDS,)
                if hasattr(gitops, "PUSH_RETRY_WAIT_SECONDS") else ())


class _Clock:
    """`gitops`'s `time`, with `sleep` recorded rather than slept."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _sleeps(monkeypatch) -> list[float]:
    """The push's own waits, at their real values, recorded rather than slept."""
    clock = _Clock()
    monkeypatch.setattr(gitops, "time", clock)
    monkeypatch.setattr(gitops, "PUSH_RETRY_WAITS_SECONDS", WAITS, raising=False)
    monkeypatch.setattr(gitops, "PUSH_RETRY_WAIT_SECONDS", 5.0, raising=False)
    return clock.slept


# ---------------------------------------------------------------------------
# push_branch: backoff for an outage, nothing new for a refusal
# ---------------------------------------------------------------------------


@pytest_git
def test_a_push_that_meets_two_500s_and_then_lands_is_published(
    tmp_path, monkeypatch, origin, local_urls
):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [HTTP_500, SIDEBAND_500])
    slept = _sleeps(monkeypatch)

    pushed = _push(tmp_path, origin, repo)

    assert len(calls) == 3
    assert refs(origin).get("swarm/t-retry") == pushed
    assert slept == [5.0, 10.0]


@pytest_git
def test_a_push_that_meets_500s_throughout_raises_push_unreachable(
    tmp_path, monkeypatch, origin, local_urls
):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [HTTP_500] * 10)
    slept = _sleeps(monkeypatch)

    with pytest.raises(gitops.PushUnreachable) as raised:
        _push(tmp_path, origin, repo)

    assert len(calls) == 4 and raised.value.tries == 4
    assert "swarm/t-retry" not in refs(origin)
    # Waits that grow, and stay under `max_in_worker_retry_delay_seconds`
    # (45 s), the platform's bound on an in-worker wait (invariant 4).
    assert slept == sorted(slept) and len(set(slept)) == 3
    assert sum(slept) <= 45


@pytest_git
def test_a_non_fast_forward_is_raised_at_once_as_today(tmp_path, monkeypatch, origin, local_urls):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [NON_FAST_FORWARD] * 4)
    slept = _sleeps(monkeypatch)

    with pytest.raises(PushRejected):
        _push(tmp_path, origin, repo)
    assert len(calls) == 1 and slept == []


@pytest_git
def test_a_refused_push_is_raised_at_once_and_is_not_an_outage(
    tmp_path, monkeypatch, origin, local_urls
):
    repo = _publish_repo(tmp_path, origin)
    calls = _failing_pushes(monkeypatch, [REFUSED] * 4)

    with pytest.raises(GitError) as raised:
        _push(tmp_path, origin, repo)
    assert len(calls) == 1
    assert not isinstance(raised.value, gitops.PushUnreachable)


def test_the_reason_phrase_of_a_5xx_alone_reads_as_transient():
    """The measured log carried no status code, only GitHub's sideband words."""
    assert gitops.transient_git_failure(SIDEBAND_500)
    assert gitops.transient_git_failure("remote: Service Unavailable")
    assert not gitops.transient_git_failure("remote: Permission denied\nInternal Server Error")


# ---------------------------------------------------------------------------
# The attempt: an outage, retryable, the patch kept
# ---------------------------------------------------------------------------


def _first_attempt_whose_push_meets_500s(db, worker_factory, monkeypatch, origin):
    """Attempt 1: clone, the agent commits and edits, the final checkpoint, a
    push the forge answers 500 every time."""
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-push-500", attempt=1, dispatch=CONTRIBUTOR
    )
    assert first._maybe_clone(task)["commit"], "the clone did not land"
    repo = first.ws.work / lifecycle.REPO_DIR_NAME
    (repo / "committed.txt").write_text("work the agent committed\n")
    for args in (["add", "committed.txt"], ["commit", "--quiet", "-m", "agent work"]):
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.invalid", *args],
                       cwd=str(repo), check=True, capture_output=True)
    (repo / "agent.txt").write_text("work the agent never committed\n")
    record = first.checkpoints.create(first.ws, label="final")
    calls = _failing_pushes(monkeypatch, [HTTP_500] * 4)
    _sleeps(monkeypatch)
    out = first._harvest_git(publish=True)
    return first, record, out, calls


@pytest_git
def test_a_push_the_forge_never_answers_is_recorded_as_an_outage_not_a_failure(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    first, _, out, calls = _first_attempt_whose_push_meets_500s(
        db, worker_factory, monkeypatch, origin
    )

    assert len(calls) == 4
    assert out["published"] is False
    assert out.get(lifecycle.PUBLISH_FAILED_FIELD) is not True, out.get("publish_reason")
    marker = out[lifecycle.PUBLISH_UNREACHABLE_FIELD]
    assert marker["publish"] == "push" and marker["tries"] == 4
    assert marker["head"] == out["head"]
    assert marker["base"] == first.checkpoints.clone_base == out["base"]
    assert (first.ws.artifacts / PATCH_NAME).is_file(), "the patch was not kept"


def _finish_with(db, worker_factory, git: dict[str, Any]) -> dict[str, Any]:
    seed_attempt(db, task_input={
        "prompt": "do the work", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "notes.md", "artifact_text": "notes\n",
    })
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dict(CONTRIBUTOR)}
    worker, _, _ = worker_factory()

    def harvest(**_kwargs: Any) -> dict[str, Any]:
        (worker.ws.artifacts / PATCH_NAME).write_text("diff --git a/x b/x\n")
        return dict(git)

    worker._harvest_git = harvest  # type: ignore[method-assign]
    worker.run()
    return db.doc("tasks/task_1")


def test_a_step_whose_push_met_an_outage_ends_retryably_with_its_patch_kept(db, worker_factory):
    marker = {"reason": "push failed with exit 128: HTTP 500", "tries": 4,
              "retry_after_seconds": None, "publish": "push",
              "head": "b" * 40, "base": "a" * 40}
    git = {"base": "a" * 40, "head": "b" * 40, "commit_count": 1, "dirty_count": 1,
           "patch": PATCH_NAME, "patch_bytes": 20, "patch_omitted": False,
           "published": False, "publish_reason": marker["reason"],
           lifecycle.PUBLISH_UNREACHABLE_FIELD: marker}

    task = _finish_with(db, worker_factory, git)

    # Back in READY with attempts left: not FAILED, so nothing cancels the
    # dependants, which keep waiting for the retry.
    assert task["state"] == TaskState.READY.value, (task["state"], task.get("last_error"))
    assert [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]
    error = task["last_error"]
    assert error.startswith(f"{lifecycle.FORGE_UNREACHABLE}: the step's branch push"), error
    assert PATCH_NAME in error and "without running the agent" in error
    names = {entry["name"] for entry in task["result_summary"]["artifacts"]}
    assert PATCH_NAME in names


# ---------------------------------------------------------------------------
# The retry: the kept work, published without the agent
# ---------------------------------------------------------------------------


def _patch_entry() -> dict[str, Any]:
    return {"name": PATCH_NAME, "uri": "gs://bucket/x", "bytes": 20}


def _second_attempt(db, worker_factory, monkeypatch, origin, record):
    second, config, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-push-500", attempt=2, dispatch=CONTRIBUTOR
    )
    record_as_earlier_attempt(db, record, task_id="t-push-500")
    second._restore_checkpoint({"attempt_count": 2, "latest_checkpoint": record.uri})
    assert second._maybe_clone(task)["from_checkpoint"] is True
    return second, config, task


@pytest_git
def test_the_retry_publishes_the_kept_work_when_its_head_and_base_still_hold(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _, record, out, _ = _first_attempt_whose_push_meets_500s(
        db, worker_factory, monkeypatch, origin
    )
    second, config, task = _second_attempt(db, worker_factory, monkeypatch, origin, record)
    task["result_summary"] = {"git": out, "artifacts": [_patch_entry()]}

    republish = second._push_to_republish(task)

    assert republish is not None
    assert republish["head"] == out["head"] and republish["base"] == out["base"]
    # The forge answers now: the same harvest and push as any attempt.
    _failing_pushes(monkeypatch, [])
    again = second._harvest_git(publish=True)
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert again["published"] is True, again.get("publish_reason")
    assert {"committed.txt", "agent.txt"} <= tree_at(origin, branch)


@pytest_git
def test_the_retry_runs_the_agent_when_the_restored_head_moved(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _, record, out, _ = _first_attempt_whose_push_meets_500s(
        db, worker_factory, monkeypatch, origin
    )
    second, _, task = _second_attempt(db, worker_factory, monkeypatch, origin, record)
    task["result_summary"] = {"git": out, "artifacts": [_patch_entry()]}
    moved = {**out[lifecycle.PUBLISH_UNREACHABLE_FIELD], "head": "c" * 40}

    assert second._push_to_republish(
        {**task, "result_summary": {"git": {**out, lifecycle.PUBLISH_UNREACHABLE_FIELD: moved},
                                    "artifacts": [_patch_entry()]}}
    ) is None
    rebased = {**out[lifecycle.PUBLISH_UNREACHABLE_FIELD], "base": "d" * 40}
    assert second._push_to_republish(
        {**task, "result_summary": {"git": {**out, lifecycle.PUBLISH_UNREACHABLE_FIELD: rebased},
                                    "artifacts": [_patch_entry()]}}
    ) is None
    # No patch kept: the agent runs again.
    assert second._push_to_republish({**task, "result_summary": {"git": out}}) is None
    # A marker that is not the push's (the probe's outage) is not re-published.
    probe = {k: v for k, v in out[lifecycle.PUBLISH_UNREACHABLE_FIELD].items() if k != "publish"}
    assert second._push_to_republish(
        {**task, "result_summary": {"git": {**out, lifecycle.PUBLISH_UNREACHABLE_FIELD: probe},
                                    "artifacts": [_patch_entry()]}}
    ) is None


def test_a_retry_that_republishes_runs_no_agent_and_carries_the_previous_uploads(
    db, store, worker_factory
):
    """The runner of the mock profile writes `notes.md`; a republish does not
    start it, and the previous attempt's own uploads are this attempt's."""
    seed_attempt(db, task_input={
        "prompt": "do the work", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "notes.md", "artifact_text": "notes\n",
    }, attempt_count=2)
    prefix = f"tenants/{TENANT}/tasks/task_1/attempts/att_0/artifacts/"
    store.upload_bytes(prefix + "pr-title.txt", b"the agent's own title\n")
    store.upload_bytes(prefix + PATCH_NAME, b"diff --git a/x b/x\n")
    foreign = f"tenants/{TENANT}/tasks/task_other/attempts/att_0/artifacts/stolen.md"
    store.upload_bytes(foreign, b"another task's file\n")
    db.doc("tasks/task_1")["result_summary"] = {"artifacts": [
        {"name": "pr-title.txt", "uri": store.uri(prefix + "pr-title.txt"), "bytes": 22},
        {"name": PATCH_NAME, "uri": store.uri(prefix + PATCH_NAME), "bytes": 19},
        {"name": "stolen.md", "uri": store.uri(foreign), "bytes": 20},
        {"name": "../escape.md", "uri": store.uri(prefix + "pr-title.txt").replace(
            "pr-title.txt", "../escape.md"), "bytes": 1},
    ]}
    worker, _, _ = worker_factory()
    # What `_push_to_republish` answers for a restored clone at the failed
    # push's head and base; its own checks are pinned against real git above.
    worker._push_to_republish = lambda task: {  # type: ignore[method-assign]
        "head": "b" * 40, "base": "a" * 40, "tries": 4,
    }

    worker.run()

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    names = {entry["name"] for entry in task["result_summary"]["artifacts"]}
    assert "pr-title.txt" in names, names
    assert "notes.md" not in names, "the agent ran again"
    assert not names & {"stolen.md", "../escape.md"}, names
    runner = task["result_summary"].get("runner") or {}
    assert runner.get("status") == "republished", runner
