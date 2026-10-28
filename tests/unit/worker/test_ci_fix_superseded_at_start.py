"""A CI fix whose red head was superseded while it waited ends before its agent runs (#2276).

THE MEASURED FAILURE. PR #2224's two CI-fix tasks started 1.5-2 h after
`scripts/ci-fix.sh` submitted them. By then the owner had fixed and merged
the branch: the clone started from b9a8d23 while `ci_fix.head_sha` was
2508b6b. One agent committed nothing and failed `published_nothing`; the
other checked out `refs/swarm/fetched/<red sha>` and failed with HEAD moved
off the task's branch. The only check that the red commit was still the
head ran at submission.

WHAT IS PINNED. A fix whose signed dispatch block carries `ci_fix_head`
(`agent_worker.cifixstart`) compares, on a fresh clone of the continued
branch, the commit it cloned with the red head, and reads the pull request
through the forge. A branch moved past the red commit, a merged or closed
pull request, or a head the forge reports moved ends the attempt SUCCEEDED
with `no_change` and a reason starting 'superseded', and the runner is never
started. A fix still on its red head, on an open pull request, runs as
before; an unreadable forge leaves the clone alone to decide; an attempt
restored from a checkpoint is not checked.

Real git against a bare repository on disk; only HTTP is faked, as in
test_strategy_end_to_end.py.

MUTATIONS: drop the `_ci_fix_superseded` call from `_prepare` -- the moved,
merged and closed tests see the runner start. Compare against the PR head
only -- the forge-unreadable moved test runs the agent. Check a restored
attempt -- the restored test's forge read fails it.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import cifixstart, lifecycle
from agent_worker import forge as forge_mod
from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode, WorkerError
from agent_worker.forge import PullHead
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NUMBER = 2224
#: The red branch's agent, whose `swarm/<id>` branch the fix continues.
CONTINUED = "task_" + secrets.token_hex(10)
BRANCH = f"swarm/{CONTINUED}"
REPO = "acme/widgets"  # what the fake probe in test_strategy_end_to_end reports


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _push_commit(origin: Path, tmp: Path, name: str, *, start: str) -> str:
    """A commit on BRANCH of the remote, made by someone other than the worker."""
    work = tmp / f"other-{name}"
    subprocess.run(["git", "clone", "--quiet", "--branch", start, str(origin), str(work)],
                   check=True, capture_output=True)
    (work / f"{name}.txt").write_text(f"{name}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", name)
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    return _git(work, "rev-parse", "HEAD")


class PullForge:
    """GitHub's pull request #NUMBER, its head read live from the remote."""

    def __init__(self, origin: Path) -> None:
        self.origin = origin
        self.reads = 0
        self.overrides: dict[str, Any] = {}
        self.raises: Exception | None = None

    def read(self, *, ref, token, number) -> PullHead:
        self.reads += 1
        if self.raises is not None:
            raise self.raises
        fields = {
            "number": number, "url": f"https://github.com/{REPO}/pull/{number}",
            "state": "open", "merged": False, "head_ref": BRANCH,
            "head_sha": refs(self.origin).get(BRANCH, ""),
            "head_repo": REPO, "base_repo": REPO,
        }
        fields.update(self.overrides)
        return PullHead(**fields)


@pytest.fixture
def red(origin, tmp_path) -> str:
    """The red commit: the continued branch's head when the fix was submitted."""
    return _push_commit(origin, tmp_path, "red", start="main")


@pytest.fixture
def pull(monkeypatch, origin) -> PullForge:
    fake = PullForge(origin)
    monkeypatch.setattr(lifecycle, "read_pull_request", fake.read)
    return fake


def _dispatch(head_sha: str) -> dict[str, Any]:
    return {
        "strategy": "direct-pr", "carrier": "checkpoints", "continues": CONTINUED,
        cifixstart.FIELD: {"pull_request": NUMBER, "head_sha": head_sha},
    }


def _worker(db, worker_factory, monkeypatch, origin: Path, dispatch: dict[str, Any]):
    seed_attempt(db, task_input={"prompt": "fix CI", "steps": 1, "sleep_seconds": 0.01})
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dict(dispatch)}
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    return worker


def _no_runner(worker, monkeypatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an agent was started for a superseded CI fix")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)


def _counting_runner(worker, monkeypatch) -> list[int]:
    started: list[int] = []
    real = worker._run_child_supervised

    def run(*args: Any, **kwargs: Any):
        started.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(worker, "_run_child_supervised", run)
    return started


def _assert_superseded(db, before: dict[str, str], origin: Path) -> str:
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["end_cause"] is None
    summary = task["result_summary"]
    assert summary["no_change"] is True
    reason = summary[lifecycle.NO_CHANGE_REASON_SUMMARY_KEY]
    assert reason.startswith("superseded"), reason
    assert refs(origin) == before, "a superseded CI fix pushed"
    assert db.doc("leases/lease_1")["released_at"] is not None
    return reason


# -- superseded: the runner never starts ---------------------------------------


def test_a_branch_moved_past_the_red_commit_ends_superseded_before_the_agent(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, red, tmp_path
):
    fixed = _push_commit(origin, tmp_path, "fixed", start=BRANCH)
    before = refs(origin)
    worker = _worker(db, worker_factory, monkeypatch, origin, _dispatch(red))
    _no_runner(worker, monkeypatch)

    assert worker.run() == ExitCode.OK
    reason = _assert_superseded(db, before, origin)
    assert red[:12] in reason and fixed[:12] in reason
    assert forge.pulls == []


@pytest.mark.parametrize(
    ("overrides", "words"),
    [({"state": "closed", "merged": True}, "merged"), ({"state": "closed"}, "closed")],
    ids=["merged", "closed"],
)
def test_a_merged_or_closed_pull_request_ends_superseded_before_the_agent(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, red, overrides, words
):
    pull.overrides = overrides
    before = refs(origin)
    worker = _worker(db, worker_factory, monkeypatch, origin, _dispatch(red))
    _no_runner(worker, monkeypatch)

    assert worker.run() == ExitCode.OK
    reason = _assert_superseded(db, before, origin)
    assert words in reason and f"#{NUMBER}" in reason
    assert pull.reads >= 1


def test_a_branch_moved_with_the_forge_unreadable_still_ends_superseded(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, red, tmp_path
):
    _push_commit(origin, tmp_path, "fixed", start=BRANCH)
    pull.raises = forge_mod.ForgeUnavailable("GitHub answered 503")
    before = refs(origin)
    worker = _worker(db, worker_factory, monkeypatch, origin, _dispatch(red))
    _no_runner(worker, monkeypatch)

    assert worker.run() == ExitCode.OK
    _assert_superseded(db, before, origin)


# -- not superseded: the agent runs as before ---------------------------------


def _assert_ran(db, started: list[int]) -> None:
    assert started, "the agent never ran for a CI fix still on its red head"
    summary = db.doc("tasks/task_1").get("result_summary") or {}
    reason = str(summary.get(lifecycle.NO_CHANGE_REASON_SUMMARY_KEY) or "")
    assert not reason.startswith("superseded"), reason


def test_a_fix_on_its_red_head_of_an_open_pull_request_runs_the_agent(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, red
):
    worker = _worker(db, worker_factory, monkeypatch, origin, _dispatch(red))
    started = _counting_runner(worker, monkeypatch)
    worker.run()
    _assert_ran(db, started)
    assert pull.reads >= 1, "the pull request was never read at the start"


@pytest.mark.parametrize(
    "error",
    [forge_mod.ForgeUnavailable("GitHub answered 503"), forge_mod.ForgeError("no answer")],
    ids=["unavailable", "error"],
)
def test_an_unreadable_forge_with_the_head_unchanged_runs_the_agent(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, red, error
):
    pull.raises = error
    worker = _worker(db, worker_factory, monkeypatch, origin, _dispatch(red))
    started = _counting_runner(worker, monkeypatch)
    worker.run()
    _assert_ran(db, started)


def test_a_fix_without_the_signed_record_is_not_checked(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, red, tmp_path
):
    """`metadata.ci_fix` alone is unsigned, and is never acted on."""
    _push_commit(origin, tmp_path, "fixed", start=BRANCH)
    dispatch = {"strategy": "direct-pr", "carrier": "checkpoints", "continues": CONTINUED}
    worker = _worker(db, worker_factory, monkeypatch, origin, dispatch)
    db.doc("tasks/task_1")["metadata"]["ci_fix"] = {"pull_request": NUMBER, "head_sha": red}
    started = _counting_runner(worker, monkeypatch)
    worker.run()
    _assert_ran(db, started)
    assert pull.reads == 0


def test_an_attempt_restored_from_a_checkpoint_is_not_checked(
    worker_factory, origin, local_urls, forge, pull, red, monkeypatch
):
    """Its agent already ran on the red head; the push judges its work."""
    pull.overrides = {"state": "closed", "merged": True}
    worker, config, _ = worker_factory(repository_url=f"file://{origin}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": "task_1", "metadata": {"dispatch": _dispatch("0" * 40)}}
    worker._repo_url = f"file://{origin}"
    worker._clone_commit = red
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._ci_fix_superseded({"path": lifecycle.REPO_DIR_NAME,
                                      "from_checkpoint": True}) is None
    assert pull.reads == 0
    # The same worker on a fresh clone is checked: the control.
    assert worker._ci_fix_superseded({"path": lifecycle.REPO_DIR_NAME,
                                      "commit": red}).startswith("superseded")


# -- the pure halves -----------------------------------------------------------


RED = "a" * 40
NEXT = "b" * 40


def _pull(**overrides: Any) -> PullHead:
    fields = {
        "number": NUMBER, "url": "", "state": "open", "merged": False,
        "head_ref": BRANCH, "head_sha": RED, "head_repo": REPO, "base_repo": REPO,
    }
    fields.update(overrides)
    return PullHead(**fields)


REC = cifixstart.Record(pull_request=NUMBER, head_sha=RED)


@pytest.mark.parametrize(
    ("clone", "pull_head"),
    [
        (NEXT, _pull()),
        (NEXT, None),
        (RED, _pull(merged=True, state="closed")),
        (RED, _pull(state="closed")),
        (RED, _pull(head_sha=NEXT)),
        (None, _pull(state="closed")),
    ],
    ids=["clone-moved", "clone-moved-forge-unread", "merged", "closed", "pr-head-moved",
         "no-clone-commit-closed"],
)
def test_superseded_says_why(clone, pull_head):
    assert cifixstart.superseded(REC, clone, pull_head).startswith("superseded: ")


@pytest.mark.parametrize(
    ("clone", "pull_head"),
    [(RED, _pull()), (RED, None), (None, None), ("not-a-sha", None), (RED, _pull(head_sha=""))],
    ids=["red-and-open", "red-forge-unread", "nothing-known", "clone-not-a-sha",
         "pr-reports-no-head"],
)
def test_not_superseded(clone, pull_head):
    assert cifixstart.superseded(REC, clone, pull_head) is None


def test_record_reads_the_signed_field_only():
    assert cifixstart.record(
        {"dispatch": {cifixstart.FIELD: {"pull_request": NUMBER, "head_sha": RED}}}
    ) == REC
    assert cifixstart.record({"dispatch": {"strategy": "direct-pr"}}) is None
    assert cifixstart.record({"ci_fix": {"pull_request": NUMBER, "head_sha": RED}}) is None
    assert cifixstart.record(None) is None


@pytest.mark.parametrize("bad", [0, -1, True, "2224", None])
def test_record_refuses_a_number_swarm_api_could_not_have_signed(bad):
    with pytest.raises(WorkerError, match="pull request number"):
        cifixstart.record({"dispatch": {cifixstart.FIELD: {"pull_request": bad, "head_sha": RED}}})


@pytest.mark.parametrize("bad", ["abc", "A" * 40, "a" * 41, None, 7])
def test_record_refuses_a_head_swarm_api_could_not_have_signed(bad):
    with pytest.raises(WorkerError, match="head sha"):
        cifixstart.record({"dispatch": {cifixstart.FIELD: {"pull_request": NUMBER, "head_sha": bad}}})


def test_record_refuses_a_value_that_is_not_an_object():
    with pytest.raises(WorkerError):
        cifixstart.record({"dispatch": {cifixstart.FIELD: True}})


def test_the_field_is_the_one_swarm_api_signs():
    from swarm_api.validation import CI_FIX_HEAD_FIELD

    assert cifixstart.FIELD == CI_FIX_HEAD_FIELD
