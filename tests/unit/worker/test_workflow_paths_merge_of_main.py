"""A lane that merges `main` is not routed to the broker for `main`'s workflow files (#2452).

WHAT WENT WRONG. `Worker._workflow_paths` listed the branch's new commits with
`git log --name-only --no-renames -m`. `-m` diffs a merge against each parent,
so a merge of `main` was read against its branch parent and named every
workflow file `main` had changed since the branch was cut. On 2026-10-26 a
ci-fix and a conflict-fix step that merged `main` after #2446 (which changed
`application.yml` and `release.yml`) were both routed to swarm-api's
`git_push_branch` for files the branch never changed, and ended FAILED.

WHAT IS PINNED, with `--diff-merges=combined` -- a merge names a path only
where its result differs from EVERY parent -- through the real publish
(`_harvest_git`) against a local bare remote, as in
`test_publish_workflow_files.py`, whose fake broker and push recorder are
reused:

* a clean merge of `main` that brings in a workflow change publishes through
  the worker's own `push_branch`, never `_push_through_broker`, and the
  branch carries `main`'s workflow file;
* an evil merge (the result edits the workflow file `main` changed), a branch
  commit changing a workflow file, and a rename away from `.github/workflows/`
  are still listed and take the broker route;
* a conflict in a workflow file resolved to a version neither side had is
  listed; resolved to the branch's own earlier, already pushed version it is
  NOT -- the known consequence docs/runner-git-tools.md records: the worker's
  own push carries it, and GitHub's workflow-scope refusal, if it comes, fails
  the publish (`PUBLISH_FAILED_FIELD`) as any refused push does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_publish_workflow_files import (  # noqa: F401 - fixtures are used by name
    FakeBroker,
    _with_broker,
    own_pushes,
)
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    refs,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WF = ".github/workflows/x.yml"
OTHER_WF = ".github/workflows/y.yml"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=check, capture_output=True, text=True,
    ).stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text + "\n")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository whose `main` already holds a workflow file."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    _write(seed, "README.md", "the repository as the agent finds it")
    _write(seed, WF, "name: x v1")
    _commit(seed, "base")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(bare))
    return bare


def _advance_main(origin: Path, tmp_path: Path, path: str = WF,
                  text: str = "name: x from main") -> str:
    """`main` changes `path` after the clone, as #2446 changed `application.yml`."""
    other = tmp_path / "upstream-dev"
    _git(tmp_path, "clone", "--quiet", str(origin), str(other))
    _write(other, path, text)
    tip = _commit(other, f"main changes {path}")
    _git(other, "push", "--quiet", "origin", "main")
    return tip


def _merge_main(repo: Path, origin: Path, *, result=None) -> None:
    """Merge `main`; `result(repo)` edits the merge's result before it commits."""
    _git(repo, "fetch", "--quiet", f"file://{origin}", "main")
    _git(repo, "merge", "--quiet", "--no-ff", "--no-commit", "FETCH_HEAD", check=False)
    if result is not None:
        result(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "--no-edit")


def _lane_work(repo: Path) -> None:
    _write(repo, "lane.txt", "the lane's own work")
    _commit(repo, "the lane's own work")


def _publish(worker_factory, monkeypatch, origin: Path, tmp_path: Path, *, task_id: str,
             edit, main_path: str = WF):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    _advance_main(origin, tmp_path, main_path)
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME, origin)
    out = worker._harvest_git(publish=True)
    return f"{config.git_branch_prefix}{task_id}", out


@pytest.fixture
def broker(monkeypatch, origin, tmp_path) -> FakeBroker:
    """The attempt is offered the git tools, so a listed path WOULD take the broker."""
    relay = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, relay)
    return relay


def test_a_merge_of_main_that_brings_in_a_workflow_change_publishes_with_the_workers_own_push(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, broker, tmp_path
):
    def edit(repo: Path, origin: Path) -> None:
        _lane_work(repo)
        _merge_main(repo, origin)

    branch, out = _publish(worker_factory, monkeypatch, origin, tmp_path,
                           task_id="t-merge-main-wf", edit=edit)

    # MUTATION: put `-m` back in `Worker._workflow_paths` -> main's x.yml is
    # listed, the broker is called and the worker's own push never runs (#2452).
    assert broker.calls == [], "a merge of main was routed to git_push_branch"
    assert own_pushes == [branch]
    assert out["published"] is True and out.get(lifecycle.PUBLISH_FAILED_FIELD) is None, out
    assert "workflow_paths" not in out and "workflow_publish" not in out, out
    # The branch is the merge, with main's workflow file and the lane's work.
    assert {WF, "lane.txt"} <= tree_at(origin, branch)
    shown = _git(origin, "show", f"{branch}:{WF}")
    assert shown == "name: x from main"


def test_an_evil_merge_of_a_workflow_file_takes_the_broker_route(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, broker, tmp_path
):
    def edit(repo: Path, origin: Path) -> None:
        _lane_work(repo)
        _merge_main(repo, origin, result=lambda r: _write(r, WF, "name: x from the merge"))

    branch, out = _publish(worker_factory, monkeypatch, origin, tmp_path,
                           task_id="t-evil-merge", edit=edit)

    # MUTATION: list merges with `--no-merges` -> the merge's own edit is not
    # seen and the tenant token pushes a changed workflow file.
    assert own_pushes == [] and [c[0] for c in broker.calls] == ["git_push_branch"]
    assert out["workflow_paths"] == [WF] and out["workflow_publish"] == "broker"


def test_a_branch_commit_changing_a_workflow_is_listed_through_a_merge_of_main(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, broker, tmp_path
):
    def edit(repo: Path, origin: Path) -> None:
        _write(repo, OTHER_WF, "name: y")
        _commit(repo, "the lane adds y.yml")
        _merge_main(repo, origin)

    _branch, out = _publish(worker_factory, monkeypatch, origin, tmp_path,
                            task_id="t-own-wf", edit=edit)

    # Only the branch's own file: main's x.yml stays out of the listing.
    assert own_pushes == [] and len(broker.calls) == 1
    assert out["workflow_paths"] == [OTHER_WF] and out["workflow_publish"] == "broker"


def test_a_rename_away_from_github_workflows_still_counts(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, broker, tmp_path
):
    def edit(repo: Path, origin: Path) -> None:
        (repo / "ci").mkdir()
        _git(repo, "mv", WF, "ci/x.yml")
        _commit(repo, "the lane moves x.yml away")

    _branch, out = _publish(worker_factory, monkeypatch, origin, tmp_path,
                            task_id="t-rename-away", edit=edit, main_path="other.txt")

    # MUTATION: drop `--no-renames` -> the move lists only ci/x.yml and the
    # deletion of a workflow file goes out on the tenant token.
    assert own_pushes == [] and len(broker.calls) == 1
    assert out["workflow_paths"] == [WF]


# -- a conflict IN a workflow file, the branch's side already pushed ------------


def _listing_after_conflict(worker_factory, origin: Path, tmp_path: Path, *,
                            resolved: str) -> list[str]:
    """The lane pushed its own change to `x.yml` before this publish (the clone
    base holds it, as a fix step's clone of its branch does); `main` changed
    `x.yml` too; the lane merges `main` and resolves the conflict to `resolved`.
    Returns what `Worker._workflow_paths` lists, with the publish's negatives."""
    lane = tmp_path / "lane"
    _git(tmp_path, "clone", "--quiet", str(origin), str(lane))
    _git(lane, "checkout", "--quiet", "-b", "lane")
    _write(lane, WF, "name: x from the lane")
    base = _commit(lane, "the lane changes x.yml (pushed earlier)")
    _git(lane, "push", "--quiet", "origin", "lane")
    tip = _advance_main(origin, tmp_path)
    _merge_main(lane, origin, result=lambda r: _write(r, WF, resolved))
    worker, config, _ = worker_factory(task_id="t-conflict", attempt_id="att-t-conflict",
                                       lease_id="lease-t-conflict",
                                       repository_url=f"file://{origin}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._publish_base = base
    return worker._workflow_paths(lane, floor=tip)


def test_a_conflict_resolved_in_a_workflow_file_is_listed(worker_factory, origin, tmp_path):
    # MUTATION: list merges with `--no-merges` -> the resolution is not seen.
    assert _listing_after_conflict(worker_factory, origin, tmp_path,
                                   resolved="name: x resolved") == [WF]


def test_a_merge_keeping_the_branchs_earlier_workflow_version_is_not_listed(
    worker_factory, origin, tmp_path
):
    """THE KNOWN CONSEQUENCE of `--diff-merges=combined`, pinned so it is a
    decision, not a surprise: the result equals the branch parent, so the
    merge names no file and the worker's own push carries it. GitHub may
    refuse that push for workflow scope; the publish then fails through
    `PUBLISH_FAILED_FIELD` without losing the work. `-m` listed it."""
    assert _listing_after_conflict(worker_factory, origin, tmp_path,
                                   resolved="name: x from the lane") == []
