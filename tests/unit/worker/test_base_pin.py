"""GUARD 1, the workflow base pin (owner decision 2026-10-02, lane B46).

THE MEASURED FAILURE. Workflow wf_b9b337e107494c10a416 (implement -> review
-> fix, strategy integrate): the implement step cloned main at b2c1094 and
staged a 246 KB change.diff; the fix step started later, cloned the MOVING
main tip (1402814), the diff did not apply, and the implementation was lost.
Every worker already recorded the commit it cloned as
`result_summary.git.base`; no downstream step read it.

WHAT IS PINNED. A workflow step with upstream dependencies that does not start
from an upstream branch clones the commit its parents recorded, not the tip:

  * every parent that recorded a base agrees -> that commit, and
    `base_pin = {"pinned": true, "sha", "from"}`;
  * they disagree, none recorded one, or the commit cannot be fetched -> the
    tip, as before, and `base_pin = {"pinned": false, "reason"}`;
  * a root step, a non-workflow task and a `builds_on` step are unchanged:
    no upstream read, no pin, no `base_pin`.

MUTATIONS: skip the pin in `_maybe_clone` -- the pinned test clones the tip.
Pick the first parent's base instead of requiring agreement -- the
disagreement test pins. Pin a root or builds_on step -- their tests see a
`base_pin`. Drop the branch fallback in `clone_at_commit` -- the refused-sha
test fails to pin.

Real git against a bare repository on disk, as in test_strategy_end_to_end.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle, workspace as workspace_mod
from swarm_common.states import TaskState

from worker_seeds import TENANT
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _advance_main(origin: Path, tmp_path: Path, name: str) -> str:
    """Push one more commit to the remote's `main`: the tip moving under a workflow."""
    work = tmp_path / f"advance-{name}"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True, capture_output=True)
    (work / f"{name}.txt").write_text(f"landed on main after the parent cloned: {name}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", f"move main: {name}")
    _git(work, "push", "--quiet", "origin", "HEAD:main")
    return refs(origin)["main"]


def _seed_parent(db, task_id: str, *, base: str | None, branch: dict | None = None) -> None:
    summary: dict[str, Any] = {"git": {"base": base}}
    if branch is not None:
        summary["branch"] = branch
    db.seed(f"tasks/{task_id}", {
        "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value, "result_summary": summary,
    })


INTEGRATOR = {"strategy": "integrate", "role": "integrator", "integrates": ["t-impl"]}


def _clone(worker_factory, monkeypatch, origin: Path, *, task_id: str, dispatch: dict,
           depends_on: list[str] | None = None, workflow_id: str | None = "wf_pin"):
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task: dict[str, Any] = {
        "task_id": task_id, "metadata": {"dispatch": dispatch},
        "depends_on": list(depends_on or []),
    }
    if workflow_id:
        task["workflow_id"] = workflow_id
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    return worker, repo, cloned


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def test_a_downstream_step_clones_its_parents_base_after_the_tip_has_moved(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    base = refs(origin)["main"]
    _seed_parent(db, "t-impl", base=base)
    moved = _advance_main(origin, tmp_path, "later")
    assert moved != base

    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-fix", dispatch=INTEGRATOR,
        depends_on=["t-impl"],
    )

    assert _head(repo) == base, "the step cloned the moved tip, not its parent's base"
    assert cloned["commit"] == base
    assert worker._base_pin == {"pinned": True, "sha": base, "from": ["t-impl"]}
    assert cloned["base_pin"] == worker._base_pin
    assert not (repo / "later.txt").exists()
    # The publish base is the pinned commit, so the harvest diffs against it
    # and records the pin where a reader of `result_summary.git` looks.
    assert worker._publish_base == base
    out = worker._harvest_git(publish=False)
    assert out["base"] == base
    assert out["base_pin"] == {"pinned": True, "sha": base, "from": ["t-impl"]}


def test_parents_that_agree_pin_and_a_parent_with_no_base_does_not_veto(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    base = refs(origin)["main"]
    _seed_parent(db, "t-a", base=base)
    _seed_parent(db, "t-b", base=base)
    _seed_parent(db, "t-quiet", base=None)
    _advance_main(origin, tmp_path, "later")

    worker, repo, _ = _clone(
        worker_factory, monkeypatch, origin, task_id="t-join",
        dispatch={"strategy": "direct-pr"}, depends_on=["t-a", "t-b", "t-quiet"],
    )
    assert _head(repo) == base
    assert worker._base_pin == {"pinned": True, "sha": base, "from": ["t-a", "t-b"]}


def test_parents_that_disagree_fall_back_to_the_tip_and_say_so(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path, log_stream
):
    first = refs(origin)["main"]
    second = _advance_main(origin, tmp_path, "second")
    _seed_parent(db, "t-a", base=first)
    _seed_parent(db, "t-b", base=second)
    tip = _advance_main(origin, tmp_path, "tip")

    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-int",
        dispatch={"strategy": "integrate", "role": "integrator", "integrates": ["t-a", "t-b"]},
        depends_on=["t-a", "t-b"],
    )

    assert _head(repo) == tip, "disagreeing parents must not pick one of them"
    assert worker._base_pin == {"pinned": False, "reason": "parents_disagree"}
    assert cloned["base_pin"] == {"pinned": False, "reason": "parents_disagree"}
    logged = log_stream.getvalue()
    assert "parents_disagree" in logged and first in logged and second in logged, (
        "the fallback did not name the parents and their bases"
    )


def test_parents_that_recorded_no_base_fall_back_to_the_tip(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _seed_parent(db, "t-impl", base=None)
    worker, repo, _ = _clone(
        worker_factory, monkeypatch, origin, task_id="t-fix", dispatch=INTEGRATOR,
        depends_on=["t-impl"],
    )
    assert _head(repo) == refs(origin)["main"]
    assert worker._base_pin == {"pinned": False, "reason": "no_upstream_base"}


def test_a_base_the_remote_does_not_have_falls_back_to_the_tip(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _seed_parent(db, "t-impl", base="0123456789abcdef" * 2 + "01234567")
    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-fix", dispatch=INTEGRATOR,
        depends_on=["t-impl"],
    )
    assert _head(repo) == refs(origin)["main"]
    assert worker._base_pin == {"pinned": False, "reason": "fetch_failed"}
    assert worker._publish_base == refs(origin)["main"]
    assert cloned["base_pin"] == {"pinned": False, "reason": "fetch_failed"}


def test_a_root_step_is_unchanged(db, worker_factory, monkeypatch, origin, local_urls, forge):
    def never(*_args: Any, **_kwargs: Any):
        raise AssertionError("a root step read an upstream task or pinned its clone")

    monkeypatch.setattr(lifecycle.inputs_mod, "fetch_upstream_task", never)
    monkeypatch.setattr(lifecycle, "clone_at_commit", never)
    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-root", dispatch={"strategy": "direct-pr"},
    )
    assert _head(repo) == refs(origin)["main"]
    assert worker._base_pin is None
    assert "base_pin" not in cloned
    assert "base_pin" not in worker._harvest_git(publish=False)


def test_a_task_outside_a_workflow_is_unchanged(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    def never(*_args: Any, **_kwargs: Any):
        raise AssertionError("a non-workflow task read an upstream task or pinned its clone")

    monkeypatch.setattr(lifecycle.inputs_mod, "fetch_upstream_task", never)
    monkeypatch.setattr(lifecycle, "clone_at_commit", never)
    worker, _, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-solo", dispatch={"strategy": "direct-pr"},
        depends_on=["t-impl"], workflow_id=None,
    )
    assert worker._base_pin is None
    assert "base_pin" not in cloned


def test_a_builds_on_step_is_unchanged(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """It starts from the upstream's BRANCH, which already holds the upstream's
    work: the recommended way for review and fix steps to receive one."""
    base = refs(origin)["main"]
    seed = tmp_path / "impl"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(seed)], check=True, capture_output=True)
    (seed / "feature.py").write_text("def feature():\n    return 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "the implementation")
    _git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/swarm/t-impl")
    _seed_parent(db, "t-impl", base=base)
    _advance_main(origin, tmp_path, "later")

    monkeypatch.setattr(
        lifecycle, "clone_at_commit",
        lambda **_kwargs: pytest.fail("a builds_on step was pinned"),
    )
    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-fix",
        dispatch={"strategy": "integrate", "role": "contributor", "builds_on": "t-impl"},
        depends_on=["t-impl"],
    )
    assert cloned["ref"] == "swarm/t-impl"
    assert (repo / "feature.py").exists()
    assert worker._base_pin is None
    assert "base_pin" not in cloned


def test_a_single_pr_step_is_unchanged(db, worker_factory, monkeypatch, origin, local_urls, forge):
    """A `single-pr` reader clones its author's branch (#295, merge-step.md §3),
    never a pinned base: its `pr_role` decides what it clones."""
    monkeypatch.setattr(
        lifecycle.inputs_mod, "fetch_upstream_task",
        lambda *_a, **_k: pytest.fail("a single-pr step read its parents for a pin"),
    )
    _git(origin, "branch", "swarm/t-impl", "main")
    worker, _, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-read",
        dispatch={"strategy": "single-pr", "pr_role": "reader", "pr_author": "t-impl"},
        depends_on=["t-impl"],
    )
    assert worker._base_pin is None
    assert "base_pin" not in cloned
    assert cloned["ref"] == "swarm/t-impl"


def test_a_server_that_refuses_a_fetch_by_sha_is_served_from_the_branch_history(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The fallback (a): the sha is fetched from the branch's full history and
    checked out. Simulated by refusing the sha fetch step itself."""
    base = refs(origin)["main"]
    _seed_parent(db, "t-impl", base=base)
    _advance_main(origin, tmp_path, "later")
    real = gitops._run_git_steps
    labels: list[str] = []

    def refuse_sha(steps, **kwargs):
        labels.append(kwargs.get("label", "git"))
        if kwargs.get("label") == "git-pin-sha":
            raise gitops.GitError("git-pin-sha step 0 failed with exit 128: not our ref")
        return real(steps, **kwargs)

    monkeypatch.setattr(gitops, "_run_git_steps", refuse_sha)
    worker, repo, _ = _clone(
        worker_factory, monkeypatch, origin, task_id="t-fix", dispatch=INTEGRATOR,
        depends_on=["t-impl"],
    )
    assert labels == ["git-pin", "git-pin-sha", "git-pin-branch"], labels
    assert _head(repo) == base
    assert worker._base_pin == {"pinned": True, "sha": base, "from": ["t-impl"]}


def test_clone_at_commit_refuses_an_abbreviated_sha(tmp_path):
    with pytest.raises(gitops.GitError, match="not a full commit sha"):
        gitops.clone_at_commit(
            url="https://github.com/acme/widgets.git", branch="main", commit="abc1234",
            destination=tmp_path / "d", private_dir=tmp_path / "p", logs_dir=tmp_path,
            timeout_seconds=5, logger=None,
        )
