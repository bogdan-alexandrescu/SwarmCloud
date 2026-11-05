"""A moved HEAD publishes nothing, and its line is kept as a bundle (#2094).

REPORTED by Product Oracle's session on 5 November 2026 (about 06:20
Pacific): task_884a993427ce44feb7a6 FAILED with "published_nothing: HEAD moved
off this task's branch (to rebuild/pr-1233)". The refusal was right -- a line
that does not stand on this step's work is never pushed onto its branch -- but
nothing of it was kept, and a person rebuilt the agent's merge by hand.

Against a real local bare origin, through the worker's own harvest:

  * a step that ends on another branch under the pre-publish gate is handed
    back once, returns, and publishes its work, merge and all;
  * one that stays away publishes nothing and leaves `moved-head.bundle` in
    its artifacts, which `git bundle verify` accepts and which holds the
    agent's merge commit with both parents;
  * without the gate, the bundle is kept the same, and the reason is today's
    sentence plus the bundle's name;
  * a bundle that cannot be written never fails the step;
  * a HEAD that descends from the start is untouched: it publishes, and no
    bundle is written.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, prepublish
from agent_worker.logs import build_logger

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    pushed_commits,
    refs,
    run_attempt,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def _no_checks(monkeypatch):
    """The gate's check set is not under test here: its moved-HEAD finding is."""
    monkeypatch.setattr(prepublish, "select_checks", lambda *a, **k: [])

OTHER = "rebuild/pr-1233"
TRAIN = "swarm/task_train"
IMPLEMENT = {"strategy": "direct-pr"}
TODAY = "HEAD moved off this task's branch (to {}); nothing of this task's was published"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _branches(origin: Path, tmp: Path) -> None:
    """A merge-train branch over main, and another pull request's branch with
    no history of main, both on the remote."""
    clone = tmp / "author"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "checkout", "--quiet", "-b", TRAIN)
    (clone / "train.txt").write_text("the train's change\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "The train's change")
    _git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{TRAIN}")
    _git(clone, "checkout", "--quiet", "--orphan", OTHER)
    _git(clone, "rm", "-rq", "--cached", ".")
    for path in ("README.md", "train.txt"):
        (clone / path).unlink()
    (clone / "other.txt").write_text("another pull request\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "Another pull request")
    _git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{OTHER}")


def _own_work_and_a_merge(repo: Path) -> None:
    (repo / "mine.py").write_text("this step's own change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "This step's own change")
    _git(repo, "fetch", "--quiet", "origin", TRAIN)
    _git(repo, "merge", "--quiet", "--no-edit", f"origin/{TRAIN}")


def _wanders(repo: Path) -> None:
    """Its own commit and a merge on its branch, then onto another pull
    request's branch, where it merges its work in: the line it ends on holds
    a merge commit with both parents and is not this step's branch."""
    _own_work_and_a_merge(repo)
    start = _git(repo, "symbolic-ref", "--short", "HEAD")
    _git(repo, "fetch", "--quiet", "origin", OTHER)
    _git(repo, "checkout", "--quiet", "-b", OTHER, f"origin/{OTHER}")
    _git(repo, "merge", "--quiet", "--no-edit", "--allow-unrelated-histories", start)


def _gate_turn(worker: lifecycle.Worker, repo: Path, tmp: Path, *acts) -> tuple[dict, list]:
    """The claude-code runner's gate over the clone, with the environment the
    worker hands its runner (`_build_child_env`)."""
    env = worker._build_child_env()
    prompts: list[str] = []
    todo = list(acts)

    def resume(text: str) -> bool:
        prompts.append(text)
        if todo:
            todo.pop(0)(repo)
        return True

    artifacts = tmp / "gate-artifacts"
    artifacts.mkdir(exist_ok=True)
    log = build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                       runner_profile="claude-code", stream=io.StringIO())
    gate = prepublish.Gate(repo=repo, env=env, secrets=(), resume=resume,
                           remaining=lambda: 3600.0, log=log, artifacts_dir=artifacts)
    return gate.run(), prompts


def _bundle(worker: lifecycle.Worker) -> Path:
    return worker.ws.artifacts / lifecycle.MOVED_HEAD_BUNDLE


def _unbundled(bundle: Path, origin: Path, tmp: Path) -> Path:
    """A fresh clone of the remote with the bundle fetched in, as a person
    would get the work back: `git fetch moved-head.bundle`."""
    clone = tmp / "fetched-back"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "bundle", "verify", str(bundle))
    _git(clone, "fetch", "--quiet", str(bundle))
    return clone


def test_a_step_handed_back_by_the_gate_that_returns_publishes_its_work(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _branches(origin, tmp_path)
    seen: dict = {}

    def edit(repo: Path) -> None:
        _own_work_and_a_merge(repo)
        start = _git(repo, "symbolic-ref", "--short", "HEAD")
        _git(repo, "fetch", "--quiet", "origin", OTHER)
        _git(repo, "checkout", "--quiet", "-b", OTHER, f"origin/{OTHER}")
        worker = seen["worker"]
        seen["record"], seen["prompts"] = _gate_turn(
            worker, repo, tmp_path,
            lambda r: _git(r, "checkout", "--quiet", start),
        )
        seen["start"] = start

    def factory(**kwargs):
        built = worker_factory(**kwargs)
        seen["worker"] = built[0]
        return built

    _, config, out = run_attempt(
        factory, monkeypatch, origin, task_id="t-moved-returns",
        dispatch=IMPLEMENT, edit=edit,
    )

    # MUTATION: drop the gate's finding and the step ends exactly as
    # task_884a993427ce44feb7a6 did: nothing published.
    assert len(seen["prompts"]) == 1, seen["prompts"]
    assert f"`{seen['start']}`" in seen["prompts"][0] and f"`{OTHER}`" in seen["prompts"][0]
    assert seen["record"]["moved_head"]["returned"] is True
    assert out["published"] is True, out
    assert "head_moved" not in out, out
    assert pushed_commits(origin, f"swarm/{config.task_id}"), "nothing was pushed"
    # The work it brought back: its own change and the train it merged.
    tree = _git(origin, "ls-tree", "--name-only", f"swarm/{config.task_id}").split()
    assert {"mine.py", "train.txt"} <= set(tree) and "other.txt" not in tree, tree
    assert not _bundle(seen["worker"]).exists()


def test_a_step_that_stays_away_publishes_nothing_and_keeps_its_line_as_a_bundle(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _branches(origin, tmp_path)
    seen: dict = {}

    def factory(**kwargs):
        built = worker_factory(**kwargs)
        seen["worker"] = built[0]
        return built

    def edit(repo: Path) -> None:
        _wanders(repo)
        seen["head"] = _git(repo, "rev-parse", "HEAD")
        seen["record"], seen["prompts"] = _gate_turn(seen["worker"], repo, tmp_path)

    worker, config, out = run_attempt(
        factory, monkeypatch, origin, task_id="t-moved-stays",
        dispatch=IMPLEMENT, edit=edit,
    )

    assert len(seen["prompts"]) == 1 and seen["record"]["moved_head"]["returned"] is False
    assert out["published"] is False and out["head_moved"] == OTHER, out
    assert forge.pulls == [] and f"swarm/{config.task_id}" not in refs(origin)
    # MUTATION: keep nothing and the merge is lost, as it was on 5 November.
    bundle = _bundle(worker)
    assert bundle.is_file(), out
    assert out["moved_head_bundle"] == lifecycle.MOVED_HEAD_BUNDLE
    clone = _unbundled(bundle, origin, tmp_path)
    assert _git(clone, "rev-parse", "FETCH_HEAD") == seen["head"]
    parents = _git(clone, "rev-list", "--parents", "-n", "1", seen["head"]).split()[1:]
    assert len(parents) == 2, parents
    # The merge on the step's own branch rides in the bundle too.
    own_merge = [line.split()[0] for line in
                 _git(clone, "rev-list", "--parents", seen["head"]).splitlines()
                 if len(line.split()) == 3 and line.split()[0] != seen["head"]]
    assert len(own_merge) == 1, own_merge


def test_a_step_without_the_gate_keeps_the_bundle_and_says_so(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _branches(origin, tmp_path)

    worker, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-moved-no-gate",
        dispatch=IMPLEMENT, edit=_wanders,
    )

    assert out["published"] is False, out
    # MUTATION: leave the bundle's name out and nobody knows the line was kept.
    assert out["publish_reason"] == (
        TODAY.format(OTHER) + "; its commits are kept in the step's artifacts as "
        f"{lifecycle.MOVED_HEAD_BUNDLE} (git fetch {lifecycle.MOVED_HEAD_BUNDLE})"
    ), out
    _unbundled(_bundle(worker), origin, tmp_path)


def test_a_bundle_that_cannot_be_written_never_fails_the_step(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _branches(origin, tmp_path)
    seen: dict = {}

    def factory(**kwargs):
        built = worker_factory(**kwargs)
        seen["worker"] = built[0]
        return built

    def edit(repo: Path) -> None:
        _wanders(repo)
        # A directory where the bundle goes: git cannot write it.
        _bundle(seen["worker"]).mkdir(parents=True)

    worker, _, out = run_attempt(
        factory, monkeypatch, origin, task_id="t-moved-unbundled",
        dispatch=IMPLEMENT, edit=edit,
    )

    assert out["published"] is False and out["head_moved"] == OTHER, out
    assert out["publish_reason"] == TODAY.format(OTHER), out
    assert "moved_head_bundle" not in out
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out


def test_a_head_that_descends_from_the_start_is_untouched(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _branches(origin, tmp_path)

    worker, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-not-moved",
        dispatch=IMPLEMENT, edit=_own_work_and_a_merge,
    )

    assert out["published"] is True and "head_moved" not in out, out
    assert "moved_head_bundle" not in out
    assert not _bundle(worker).exists()
