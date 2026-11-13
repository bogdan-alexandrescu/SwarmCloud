"""A workflow step's own `repository_ref` (#2895), as the worker clones it.

swarm-api writes a step's own ref as that step task's `repository_ref` and
marks the step with `metadata.dispatch.step_ref: true` (absent on every other
step). Such a step clones its own ref exactly as a root step clones the
workflow's:

  * it never starts from a parent's branch under `carrier: branches`
    (`_carrier_parent` is not consulted);
  * it is never base-pinned (`_upstream_base_pin` is not read): its parents
    cloned another ref, so their base is not this step's;
  * it records its own clone start as `result_summary.git.base`, and its
    `input_from` files and its patch are handled as for any step.

And the other direction: a downstream step whose parent ran on a different
ref is not pinned to that parent's base (`_upstream_base_pin` ignores it).

A step without `step_ref` is unchanged; test_base_pin.py and
test_carrier_branches.py hold that.

MUTATIONS: consult `_carrier_parent` for a step_ref step -- the carrier test
clones the parent's branch. Pin a step_ref step -- the pin test clones main's
old base. Drop the parent-ref filter in `_upstream_base_pin` -- the downstream
test pins to the other ref's base. Honour a `step_ref` that is not `True` --
the string test is not pinned.

Real git against a bare repository on disk, as in test_strategy_end_to_end.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from swarm_common.states import TaskState

from worker_seeds import TENANT
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

STEP_REF = "release"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _push_commit(origin: Path, tmp_path: Path, *, branch: str, name: str, start: str = "main") -> str:
    """One more commit on `branch` (created from `start` if it is new); its sha."""
    work = tmp_path / f"push-{branch.replace('/', '-')}-{name}"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "--quiet", "-B", branch, f"origin/{branch}" if branch in refs(origin) else f"origin/{start}")
    (work / f"{name}.txt").write_text(f"{name} on {branch}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", f"{name} on {branch}")
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    return refs(origin)[branch]


def _seed_parent(db, task_id: str, *, base: str | None, repository_ref: str | None = None,
                 branch: dict | None = None, artifacts: list | None = None) -> None:
    summary: dict[str, Any] = {"git": {"base": base}}
    if branch is not None:
        summary["branch"] = branch
    if artifacts is not None:
        summary["artifacts"] = artifacts
    doc: dict[str, Any] = {
        "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value, "result_summary": summary,
    }
    if repository_ref is not None:
        doc["repository_ref"] = repository_ref
    db.seed(f"tasks/{task_id}", doc)


def _never(what: str):
    def fail(*_args: Any, **_kwargs: Any):
        raise AssertionError(what)
    return fail


def _clone(worker_factory, monkeypatch, origin: Path, *, task_id: str, dispatch: dict,
           repository_ref: str | None, depends_on: list[str] | None = None,
           metadata: dict | None = None):
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}", repository_ref=repository_ref,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task: dict[str, Any] = {
        "task_id": task_id, "workflow_id": "wf_stepref",
        "metadata": {**(metadata or {}), "dispatch": dispatch},
        "depends_on": list(depends_on or []),
    }
    if repository_ref is not None:
        task["repository_ref"] = repository_ref
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda **_: "not-a-real-token")
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    return worker, worker.ws.work / lifecycle.REPO_DIR_NAME, cloned


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def test_a_step_ref_step_clones_its_ref_and_records_its_head_as_the_base(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    head = _push_commit(origin, tmp_path, branch=STEP_REF, name="release-only")
    assert head != refs(origin)["main"]
    _seed_parent(db, "t-impl", base=refs(origin)["main"], repository_ref="main")

    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-step",
        dispatch={"strategy": "collect", "step_ref": True},
        repository_ref=STEP_REF, depends_on=["t-impl"],
    )

    assert _head(repo) == head and cloned["commit"] == head
    assert (repo / "release-only.txt").exists()
    # The patch is harvested against the ref's head, as for any step.
    (repo / "agent.txt").write_text("the step's work\n")
    out = worker._harvest_git(publish=False)
    assert out["base"] == head, "the attempt did not report its own ref's head as its clone"
    patch = (worker.ws.artifacts / lifecycle.PATCH_NAME).read_text()
    assert "agent.txt" in patch and "release-only.txt" not in patch


def test_a_step_ref_step_is_not_base_pinned(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    old_main = refs(origin)["main"]
    _seed_parent(db, "t-impl", base=old_main, repository_ref=STEP_REF)
    _push_commit(origin, tmp_path, branch="main", name="later")
    head = _push_commit(origin, tmp_path, branch=STEP_REF, name="release-only")
    monkeypatch.setattr(lifecycle.Worker, "_upstream_base_pin",
                        _never("a step_ref step read its parents for a base pin"))
    monkeypatch.setattr(lifecycle, "clone_at_commit", _never("a step_ref step was pinned"))

    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-step",
        dispatch={"strategy": "integrate", "role": "contributor", "step_ref": True},
        repository_ref=STEP_REF, depends_on=["t-impl"],
    )

    assert _head(repo) == head
    assert worker._base_pin is None
    assert "base_pin" not in cloned
    assert "base_pin" not in worker._harvest_git(publish=False)


def test_under_carrier_branches_a_step_ref_step_still_clones_its_own_ref(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    parent_head = _push_commit(origin, tmp_path, branch="swarm/t-parent", name="from-parent")
    _seed_parent(db, "t-parent", base=refs(origin)["main"], repository_ref="main",
                 branch={"name": "swarm/t-parent", "head": parent_head})
    head = _push_commit(origin, tmp_path, branch=STEP_REF, name="release-only")
    monkeypatch.setattr(lifecycle.Worker, "_carrier_parent",
                        _never("a step_ref step consulted its parent's branch"))

    _, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-step",
        dispatch={"strategy": "collect", "carrier": "branches", "step_ref": True},
        repository_ref=STEP_REF, depends_on=["t-parent"],
    )

    assert _head(repo) == head
    assert cloned["ref"] == STEP_REF
    assert "carried_from" not in cloned
    assert not (repo / "from-parent.txt").exists()


def test_a_step_ref_steps_input_from_is_still_staged(
    db, store, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    text = b"what the parent handed on\n"
    key = f"tenants/{TENANT}/tasks/t-impl/attempts/att-impl/artifacts/summary.md"
    store.upload_bytes(key, text)
    _seed_parent(db, "t-impl", base=refs(origin)["main"], repository_ref="main", artifacts=[
        {"name": "summary.md", "uri": store.uri(key), "bytes": len(text)},
    ])
    _push_commit(origin, tmp_path, branch=STEP_REF, name="release-only")

    worker, _, _ = _clone(
        worker_factory, monkeypatch, origin, task_id="t-step",
        dispatch={"strategy": "collect", "step_ref": True},
        repository_ref=STEP_REF, depends_on=["t-impl"],
        metadata={"input_from": {"t-impl": "summary.md"}},
    )
    staged = worker._stage_declared_inputs(worker._task)

    assert [(item.upstream_task_id, item.filename) for item in staged] == [("t-impl", "summary.md")]
    assert (worker.ws.work / "summary.md").read_bytes() == text


def test_a_downstream_step_is_not_pinned_to_a_parent_that_ran_on_another_ref(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    release_head = _push_commit(origin, tmp_path, branch=STEP_REF, name="release-only")
    _seed_parent(db, "t-other", base=release_head, repository_ref=STEP_REF)
    tip = _push_commit(origin, tmp_path, branch="main", name="later")

    worker, repo, cloned = _clone(
        worker_factory, monkeypatch, origin, task_id="t-down",
        dispatch={"strategy": "collect"}, repository_ref="main", depends_on=["t-other"],
    )

    assert _head(repo) == tip, "the step was pinned to a base on another ref"
    assert not (repo / "release-only.txt").exists()
    assert worker._base_pin == {"pinned": False, "reason": "no_upstream_base"}
    assert cloned["base_pin"] == {"pinned": False, "reason": "no_upstream_base"}


def test_a_downstream_step_pins_to_the_parents_on_its_own_ref_only(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    base = refs(origin)["main"]
    release_head = _push_commit(origin, tmp_path, branch=STEP_REF, name="release-only")
    _seed_parent(db, "t-same", base=base, repository_ref="main")
    _seed_parent(db, "t-other", base=release_head, repository_ref=STEP_REF)
    _push_commit(origin, tmp_path, branch="main", name="later")

    worker, repo, _ = _clone(
        worker_factory, monkeypatch, origin, task_id="t-join",
        dispatch={"strategy": "collect"}, repository_ref="main",
        depends_on=["t-same", "t-other"],
    )

    assert _head(repo) == base
    assert worker._base_pin == {"pinned": True, "sha": base, "from": ["t-same"]}


def test_a_step_ref_that_is_not_true_is_not_a_step_ref(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    base = refs(origin)["main"]
    _seed_parent(db, "t-impl", base=base, repository_ref="main")
    _push_commit(origin, tmp_path, branch="main", name="later")

    worker, repo, _ = _clone(
        worker_factory, monkeypatch, origin, task_id="t-fix",
        dispatch={"strategy": "collect", "step_ref": "true"},
        repository_ref="main", depends_on=["t-impl"],
    )

    assert _head(repo) == base
    assert worker._base_pin == {"pinned": True, "sha": base, "from": ["t-impl"]}
