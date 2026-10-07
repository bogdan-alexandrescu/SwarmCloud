"""`carrier: branches` and fencing: the #463 security review's boxes on #453.

Invariant 5 says a stale generation exits without running the agent and
without touching the lease. These extend it to the carrier's pushes and to the
runner child:

  * a dependant clones its parent's branch at the head the parent RECORDED,
    never the branch's current tip, and only from the parent's own repository;
  * a worker whose memory guard refuses the git token fails before the agent
    runs, instead of running an attempt whose branch could never be pushed;
  * the SIGTERM exits make no forge call inside the grace window before the
    park or terminal write;
  * a fence met by the control-plane-outage checkpoint stops the runner it
    was handed before the attempt stands down;
  * a fence that lands after a checkpoint's pointer write stops the carrier
    push that follows it.

Real git against a bare repository on disk, as in test_carrier_branches.py;
only HTTP to the forge is faked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import expected_outputs, lifecycle
from agent_worker.errors import ExitCode, FencedError, FencedWriteRefused, WorkerError
from agent_worker.hardening import FAILED, MemoryProtection
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt
from test_carrier_branches import (
    BRANCHES,
    _agent_commits,
    _git,
    _seeded_branches_worker,
    _worker,
)
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)


class StubChild:
    """A runner that has already been asked nothing; records what it is asked."""

    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.calls: list[str] = []

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self, _grace: float, *, reason: str = "") -> None:
        self.calls.append(f"terminate:{reason}")
        self.alive = False

    def finish(self) -> None:
        self.calls.append("finish")


def _forge_spy(monkeypatch) -> list[str]:
    """Every git call the carrier makes to the forge, in order."""
    calls: list[str] = []
    for name in ("fetch_branch_tip", "push_branch"):
        real = getattr(lifecycle, name)

        def spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(lifecycle, name, spy)
    return calls


def _push_onto(origin: Path, tmp_path: Path, branch: str, name: str) -> str:
    """Move `branch` on the bare origin past what was recorded, as any token holder could."""
    other = tmp_path / f"elsewhere-{name}"
    subprocess.run(["git", "clone", "--quiet", "--branch", branch, str(origin), str(other)],
                   check=True, capture_output=True)
    _agent_commits(other, name, "pushed after the parent's record\n")
    _git(other, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    return refs(origin)[branch]


# ---------------------------------------------------------------------------
# A carried clone is pinned to the parent's recorded head, in its repository
# ---------------------------------------------------------------------------


def _parent_pushed(db, worker_factory, monkeypatch, origin, **record: Any) -> str:
    parent, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-parent",
                              dispatch=BRANCHES)
    _agent_commits(repo, "from-parent.txt", "the parent's work\n")
    parent._push_carrier_branch("final")
    recorded = dict(parent._carrier_pushed or {})
    assert recorded.get("head"), "the parent's push did not land"
    db.seed("tasks/t-parent", {
        "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "result_summary": {"branch": recorded}, **record,
    })
    return recorded["head"]


def test_a_dependant_clones_the_parents_recorded_head_not_the_tip(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    head = _parent_pushed(db, worker_factory, monkeypatch, origin,
                          repository_url=f"file://{origin}")
    tip = _push_onto(origin, tmp_path, "swarm/t-parent", "late.txt")
    assert tip != head, "the control: the tip moved past the record"

    _, child_repo, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-child", dispatch=BRANCHES,
        depends_on=["t-parent"],
    )

    assert cloned["carried_from"] == "t-parent"
    assert cloned["commit"] == head, "the child cloned the branch's tip, not the recorded head"
    assert cloned["carried_head"] == head
    assert (child_repo / "from-parent.txt").exists()
    assert not (child_repo / "late.txt").exists(), "a commit pushed after the record was cloned"


def test_a_recorded_head_the_forge_no_longer_has_fails_the_clone_never_the_tip(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _parent_pushed(db, worker_factory, monkeypatch, origin)
    gone = "0123456789" * 4  # a full sha no commit in the origin has
    db.doc("tasks/t-parent")["result_summary"]["branch"]["head"] = gone

    child, config, _ = worker_factory(
        task_id="t-child3", attempt_id="att-t-child3", lease_id="lease-t-child3",
        repository_url=f"file://{origin}",
    )
    child.ws = lifecycle.workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-child3", "metadata": {"dispatch": BRANCHES}, "depends_on": ["t-parent"]}
    monkeypatch.setattr(child, "_git_token", lambda: "not-a-real-token")

    child._task = task
    with pytest.raises(WorkerError) as caught:
        child._maybe_clone(task)
    assert gone in str(caught.value) and "could not be fetched" in str(caught.value)
    assert not any((child.ws.work / lifecycle.REPO_DIR_NAME).iterdir()), "the tip was cloned"


def test_a_parent_in_another_repository_is_not_carried(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """`swarm/t-parent` exists in THIS repository -- pushed there -- but the
    parent's own task names another one, so it is not the parent's work."""
    _parent_pushed(db, worker_factory, monkeypatch, origin,
                   repository_url=f"file://{tmp_path}/someone-else.git")
    assert "swarm/t-parent" in refs(origin), "the control: the same-named branch is there"

    _, child_repo, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-child2", dispatch=BRANCHES,
        depends_on=["t-parent"],
    )

    assert "carried_from" not in cloned
    assert cloned["ref"] != "swarm/t-parent", cloned
    assert not (child_repo / "from-parent.txt").exists()


def test_a_branch_record_without_a_full_sha_is_not_carried(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _parent_pushed(db, worker_factory, monkeypatch, origin)
    db.doc("tasks/t-parent")["result_summary"]["branch"]["head"] = "abc123"

    _, _, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-child4", dispatch=BRANCHES,
        depends_on=["t-parent"],
    )
    assert "carried_from" not in cloned


@pytest.mark.parametrize("a, b, same", [
    ("https://github.com/acme/widgets.git", "git@github.com:Acme/Widgets", True),
    ("https://github.com/acme/widgets", "https://github.com/acme/widgets/", True),
    ("https://github.com/acme/widgets", "https://github.com/acme/gadgets", False),
    ("https://github.com/acme/widgets", "https://ghe.example.com/acme/widgets", False),
    ("file:///tmp/x/origin.git", "file:///tmp/x/origin", True),
    ("file:///tmp/x/origin.git", "file:///tmp/y/origin.git", False),
])
def test_same_repository(a, b, same):
    assert lifecycle._same_repository(a, b) is same


# ---------------------------------------------------------------------------
# A refused git token fails a carrier=branches attempt before the agent runs
# ---------------------------------------------------------------------------


def test_a_memory_guard_refusal_fails_before_the_agent_runs(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    worker, started = _seeded_branches_worker(db, worker_factory, monkeypatch, origin)
    worker.memory = MemoryProtection(FAILED, "stand-in: prctl refused")

    assert worker.run() == ExitCode.FAILED
    assert started == [], "the agent ran for a branch that could never be pushed"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "cannot_start"
    assert task["last_error"].startswith("git_token_refused:"), task["last_error"]
    assert task["result_summary"]["carrier_check"]["cause"] == "git_token_refused"
    assert "unreadable to the agent" in task["result_summary"]["carrier_check"]["reason"]
    assert forge.probes == [], "the forge was asked with no token in hand"
    assert "swarm/task_1" not in refs(origin)


# ---------------------------------------------------------------------------
# No forge call in the SIGTERM grace window
# ---------------------------------------------------------------------------


def _interrupted_worker(worker_factory, monkeypatch, origin, task_id: str):
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id=task_id,
                              dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")
    order: list[str] = []
    forge_calls = _forge_spy(monkeypatch)

    def note(name: str):
        def record(**_kwargs: Any) -> None:
            order.append(name)
        return record

    monkeypatch.setattr(worker.control, "park", note("park"))
    monkeypatch.setattr(worker.control, "finish", note("finish"))
    monkeypatch.setattr(worker, "_upload_outputs", lambda **_kwargs: {})
    return worker, order, forge_calls


def test_a_sigterm_parks_without_calling_the_forge(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, order, forge_calls = _interrupted_worker(worker_factory, monkeypatch, origin, "t-int")
    child = StubChild()

    outcome = worker._handle_interruption(child)

    assert outcome.exit_code == ExitCode.PARKED
    assert child.calls[0].startswith("terminate:"), child.calls
    assert order == ["park"], order
    assert forge_calls == [], f"the forge was called inside the grace window: {forge_calls}"
    assert worker._last_checkpoint is not None, "the interrupted checkpoint was not taken"
    assert "swarm/t-int" not in refs(origin)


def test_a_sigterm_cancel_ends_without_calling_the_forge(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, order, forge_calls = _interrupted_worker(worker_factory, monkeypatch, origin, "t-cx")
    worker.db.doc("tasks/t-cx")["cancel_requested"] = True

    outcome = worker._handle_interruption(StubChild())

    assert outcome.exit_code == ExitCode.CANCELLED
    assert order == ["finish"], order
    assert forge_calls == [], f"the forge was called inside the grace window: {forge_calls}"
    assert worker._last_checkpoint is not None


# ---------------------------------------------------------------------------
# A fenced outage checkpoint stops the runner it was handed
# ---------------------------------------------------------------------------


def test_a_fenced_outage_checkpoint_stops_the_runner_before_standing_down(
    db, worker_factory, monkeypatch
):
    seed_attempt(db)
    worker, _, _ = worker_factory()

    def fenced(label: str, **_kwargs: Any) -> None:
        raise FencedWriteRefused(1, 2, "superseded", write=f"checkpoint ({label})")

    monkeypatch.setattr(worker, "_checkpoint", fenced)
    # An index phase's runner is not `self._child`, so the stand-down's own
    # `_stop_runner` never reaches it.
    worker._child = None
    child = StubChild()

    with pytest.raises(FencedError):
        worker._exit_control_plane_outage(child, RuntimeError("unavailable"))

    assert child.calls == ["terminate:generation fenced", "finish"], child.calls
    assert not child.alive


# ---------------------------------------------------------------------------
# A fence after the checkpoint's pointer write stops the push
# ---------------------------------------------------------------------------


def test_a_fence_landing_after_the_checkpoint_stops_the_carrier_push(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-fence",
                              dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")
    real_verify = lifecycle.verify_worker_authorship

    def reclaimed_meanwhile(**kwargs: Any) -> Any:
        # The reconciler reclaims the task between the pointer write and the push.
        worker.db.doc("tasks/t-fence")["current_generation"] = 2
        return real_verify(**kwargs)

    monkeypatch.setattr(lifecycle, "verify_worker_authorship", reclaimed_meanwhile)

    with pytest.raises(FencedError):
        worker._checkpoint("final")

    assert worker._last_checkpoint is not None, "the control: the checkpoint itself was recorded"
    assert "swarm/t-fence" not in refs(origin), "a superseded worker pushed its branch"
    assert worker._carrier_pushed is None


def test_an_owner_check_that_cannot_be_made_pushes_nothing_and_never_raises(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-dark",
                              dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")

    def unreachable(*, write: str) -> None:
        raise RuntimeError("503 the control plane is unavailable")

    monkeypatch.setattr(worker.control, "ensure_owner", unreachable)

    worker._push_carrier_branch("final")  # must not raise

    assert "swarm/t-dark" not in refs(origin)
    assert worker._carrier_pushed is None


# ---------------------------------------------------------------------------
# expected_outputs says what the carrier does with a missing output
# ---------------------------------------------------------------------------


def test_expected_outputs_says_a_missing_output_opens_no_pr_and_the_carrier_keeps_the_work():
    doc = expected_outputs.__doc__ or ""
    assert "PUBLISHES NOTHING" not in doc, "the carrier pushes the committed work under branches"
    assert "OPENS NO PULL REQUEST" in doc
    assert "carrier: branches" in doc and "swarm/<task>" in doc
