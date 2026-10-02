"""`carrier: branches`, wired (D13, owner decision 2026-10-01).

The carrier was accepted, stored and read (`_dispatch_carrier`), and nothing
branched on it. With `carrier: branches`:

  * each checkpoint taken once the runner has stopped pushes the step's
    COMMITTED work to `<prefix><task id>` -- never forced, a failure logged and
    never ending the attempt; a checkpoint taken while the runner runs pushes
    nothing, because the tenant token is never in hand while agent code can run;
  * the finish pushes too, under `collect` as well, and records
    `result_summary.branch` with its name and head;
  * a dependant whose parent kept its work on a branch starts from it;
  * `carrier: checkpoints`, the default, is unchanged.

Real git against a bare repository on disk, as in test_strategy_end_to_end.py;
only HTTP to the forge is faked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _agent_commits(repo: Path, name: str, text: str = "work\n") -> None:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "--quiet", "-m", f"add {name}")


def _worker(worker_factory, monkeypatch, origin: Path, *, task_id: str, dispatch: dict,
            depends_on: list[str] | None = None):
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": task_id, "metadata": {"dispatch": dispatch},
                    "depends_on": list(depends_on or [])}
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    cloned = worker._maybe_clone(worker._task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    return worker, worker.ws.work / lifecycle.REPO_DIR_NAME, cloned


BRANCHES = {"strategy": "collect", "carrier": "branches"}


def _is_ancestor(bare: Path, old: str, new: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", old, new], cwd=str(bare)
    ).returncode == 0


def test_a_checkpoint_pushes_the_committed_work_and_never_forces(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    pushes: list[dict[str, Any]] = []
    real_push = lifecycle.push_branch

    def spy(**kwargs: Any) -> str:
        pushes.append(kwargs)
        return real_push(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", spy)
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-br", dispatch=BRANCHES)

    _agent_commits(repo, "one.txt")
    (repo / "dirty.txt").write_text("not committed\n")
    worker._push_carrier_branch("final")
    first = refs(origin)["swarm/t-br"]
    assert "one.txt" in tree_at(origin, "swarm/t-br")
    assert "dirty.txt" not in tree_at(origin, "swarm/t-br"), "a checkpoint pushes committed work only"

    _agent_commits(repo, "two.txt")
    worker._push_carrier_branch("park")
    second = refs(origin)["swarm/t-br"]
    assert second != first
    assert _is_ancestor(origin, first, second), "the second push did not fast-forward the first"
    assert {"one.txt", "two.txt"} <= tree_at(origin, "swarm/t-br")
    assert len(pushes) == 2
    assert all("force" not in call for call in pushes), "a push was asked to force"
    assert worker._carrier_pushed == {"name": "swarm/t-br", "head": second}
    assert refs(origin)["main"], "the default branch is untouched"


def test_a_checkpoint_while_the_runner_runs_pushes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-live", dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")

    class Running:
        def poll(self):
            return None

    worker._child = Running()
    worker._push_carrier_branch("periodic")
    assert "swarm/t-live" not in refs(origin)
    assert forge.probes == [], "the token was put in hand while the agent could run"


def test_a_failed_push_is_logged_and_never_raises(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def refused(**_kwargs: Any) -> str:
        raise lifecycle.GitError("push failed with exit 1: non-fast-forward")

    monkeypatch.setattr(lifecycle, "push_branch", refused)
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-ff", dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")

    worker._push_carrier_branch("final")  # must not raise
    assert worker._carrier_pushed is None


def test_a_read_only_token_pushes_nothing(worker_factory, monkeypatch, origin, local_urls, forge):
    forge.can_push = False
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-ro", dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")

    worker._push_carrier_branch("final")
    assert "swarm/t-ro" not in refs(origin)


def test_checkpoints_mode_is_unchanged(worker_factory, monkeypatch, origin, local_urls, forge):
    def never(**_kwargs: Any) -> str:
        raise AssertionError("carrier: checkpoints reached a push")

    monkeypatch.setattr(lifecycle, "push_branch", never)
    worker, repo, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-ck",
        dispatch={"strategy": "collect", "carrier": "checkpoints"},
    )
    _agent_commits(repo, "one.txt")
    worker._push_carrier_branch("final")
    out = worker._harvest_git(publish=True)

    assert forge.probes == []
    assert out["published"] is False and "nothing is pushed" in out["publish_reason"]
    assert worker._carrier_pushed is None
    assert "carried_from" not in cloned


def test_the_finish_pushes_on_top_of_the_checkpoint_pushes_and_records_the_branch(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-fin", dispatch=BRANCHES)
    _agent_commits(repo, "one.txt")
    worker._push_carrier_branch("final")
    checkpointed = refs(origin)["swarm/t-fin"]
    (repo / "dirty.txt").write_text("finished, not committed\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out.get("publish_reason")
    assert forge.pulls == [], "collect opened a pull request"
    head = refs(origin)["swarm/t-fin"]
    assert _is_ancestor(origin, checkpointed, head), "the finish did not fast-forward"
    assert {"one.txt", "dirty.txt"} <= tree_at(origin, "swarm/t-fin")
    assert worker._carrier_pushed == {"name": "swarm/t-fin", "head": head}


def test_the_finish_records_result_summary_branch(db, worker_factory):
    """`_finalise` writes what the last push left, by name and head."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    worker, _, _ = worker_factory()
    upload = worker._upload_outputs

    def pushed_then_upload(**kwargs: Any) -> dict[str, Any]:
        worker._carrier_pushed = {"name": "swarm/task_1", "head": "c" * 40}
        return upload(**kwargs)

    worker._upload_outputs = pushed_then_upload  # type: ignore[method-assign]
    assert worker.run() == 0
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["result_summary"]["branch"] == {"name": "swarm/task_1", "head": "c" * 40}


def test_a_dependant_starts_from_its_parents_branch(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    parent, repo, _ = _worker(worker_factory, monkeypatch, origin, task_id="t-parent", dispatch=BRANCHES)
    _agent_commits(repo, "from-parent.txt", "the parent's work\n")
    parent._push_carrier_branch("final")
    db.seed("tasks/t-parent", {
        "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "result_summary": {"branch": dict(parent._carrier_pushed or {})},
    })

    child, child_repo, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-child", dispatch=BRANCHES,
        depends_on=["t-parent"],
    )

    assert cloned["ref"] == "swarm/t-parent"
    assert cloned["carried_from"] == "t-parent"
    assert (child_repo / "from-parent.txt").read_text() == "the parent's work\n"


def test_a_dependant_of_a_parent_with_no_branch_starts_from_the_default(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    db.seed("tasks/t-quiet", {"tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
                              "result_summary": {}})
    _, child_repo, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-child2", dispatch=BRANCHES,
        depends_on=["t-quiet"],
    )
    assert "carried_from" not in cloned
    assert (child_repo / "README.md").exists()


@pytest.mark.parametrize("dispatch", [
    {"strategy": "collect", "carrier": "checkpoints"},
    {"strategy": "integrate", "carrier": "branches", "role": "integrator", "integrates": ["t-p"]},
])
def test_no_parent_branch_is_cloned_for_checkpoints_or_an_integrator(
    db, worker_factory, monkeypatch, origin, local_urls, forge, dispatch
):
    """An integrator merges its parents' branches in step order at its publish
    (`merge_branches`); checkpoints mode never reads a parent at all."""
    _, _, cloned = _worker(
        worker_factory, monkeypatch, origin, task_id="t-x", dispatch=dispatch, depends_on=["t-p"],
    )
    assert "carried_from" not in cloned
