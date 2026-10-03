"""A `direct-pr` step that continues another task's branch pushes onto THAT branch (#263).

The CI fixer (`.github/workflows/ci-fix.yml`) exists to put a fix commit on
the pull request that went red. Before this, a step could clone a swarm branch
(`repository_ref`) but every push went to `swarm/<its own task id>`, so the fix
would have arrived as a second pull request stacked on the first, and the red
one would have stayed red.

As in `test_strategy_end_to_end.py`, only HTTP is faked. The clone, the
harvest, the fold and the push are the production code against a real bare
repository, and the assertions are made against the REMOTE.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import continuation, gitops, lifecycle, workspace as workspace_mod
from agent_worker.errors import WorkerError
from agent_worker.forge import PullRequest, RepoAccess, RepoRef
from swarm_api.validation import DISPATCH_METADATA_KEY, DispatchOptions
from swarm_common.models import new_id

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ROOT = "task_0123456789abcdef0123"
FIX = "task_fedcba9876543210fedc"


@pytest.fixture(autouse=True)
def _the_agent_titles_its_pull_request(monkeypatch):
    """Since 2026-09-28 the platform invents no pull request title: the agent
    writes `pr-title.txt`, or the step's `issue` input names one, or no pull
    request is opened (`test_pull_request_text.py` pins that). This file is
    about the continuation path -- which branch is cloned and pushed, and
    that the push lands as a fast-forward -- not titles, so every attempt
    here stands in for an agent that wrote one (mirrors the fixture of the
    same name in `test_strategy_end_to_end.py`)."""
    monkeypatch.setattr(
        lifecycle.Worker, "_generated_pull_request_title", lambda self: "The agent's title"
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """`main`, plus `swarm/<ROOT>`: the branch of the pull request that went red."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("main\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
    _git(seed, "checkout", "--quiet", "-b", f"swarm/{ROOT}")
    (seed / "feature.py").write_text("def broken():\n    return 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "the work that went red")
    _git(seed, "checkout", "--quiet", "main")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


def refs(bare: Path) -> dict[str, str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout.split()
    return dict(zip(out[0::2], out[1::2]))


def parent_of(bare: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{sha}^"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout.strip()


def show(bare: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def local_urls(monkeypatch, tmp_path: Path) -> None:
    """A `file://` remote under tmp_path passes; every other URL is validated as usual."""
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"

    def validate(url: str) -> str:
        return url if url.startswith(allowed) else real(url)

    monkeypatch.setattr(gitops, "validate_repository_url", validate)


class FakeForge:
    def __init__(self) -> None:
        self.pulls: list[dict] = []

    def probe(self, *, url: str, token: str | None) -> RepoAccess:
        return RepoAccess(
            ref=RepoRef(host="github.com", owner="acme", name="widgets"),
            default_branch="main",
            can_push=True,
            reason="the token has write permission on this repository",
        )

    def open_pull_request(self, **kwargs) -> PullRequest:
        # GitHub answers a pull request for a head that already has an open
        # one with that one (`forge._find_open_pull_request`); so does this.
        self.pulls.append(kwargs)
        return PullRequest(
            number=7, url="https://github.com/acme/widgets/pull/7", state="open", created=False
        )


@pytest.fixture
def forge(monkeypatch) -> FakeForge:
    fake = FakeForge()
    monkeypatch.setattr(lifecycle, "probe_repository", fake.probe)
    monkeypatch.setattr(lifecycle, "open_pull_request", fake.open_pull_request)
    return fake


def _worker(worker_factory, monkeypatch, origin: Path, dispatch: dict):
    worker, config, _ = worker_factory(
        task_id=FIX,
        attempt_id=f"att-{FIX}",
        lease_id=f"lease-{FIX}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": FIX, "metadata": {DISPATCH_METADATA_KEY: dispatch}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    return worker, config, task


def fix_the_test(repo: Path) -> None:
    (repo / "feature.py").write_text("def broken():\n    return 2\n")


# --------------------------------------------------------------------------
# The whole attempt
# --------------------------------------------------------------------------

def test_the_fix_lands_on_the_red_branch_as_a_fast_forward(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    before = refs(origin)
    red = f"swarm/{ROOT}"
    worker, config, task = _worker(
        worker_factory, monkeypatch, origin, {"strategy": "direct-pr", "continues": ROOT}
    )

    cloned = worker._maybe_clone(task)
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    # The agent is handed the red branch, not main: the failure is only there.
    assert cloned["commit"] == before[red]
    assert (repo / "feature.py").exists()

    fix_the_test(repo)
    out = worker._harvest_git(publish=True)

    after = refs(origin)
    assert out["published"] is True, out
    assert out["branch"] == red
    assert after[red] != before[red], "nothing reached the red branch"
    assert parent_of(origin, after[red]) == before[red], "the push was not a fast-forward"
    assert "return 2" in show(origin, red, "feature.py")
    # No branch of its own and no other ref moved.
    assert f"swarm/{FIX}" not in after
    assert after["main"] == before["main"]
    # The pull request asked for is the one already open on that head.
    assert [p["head"] for p in forge.pulls] == [red]


def test_a_branch_that_moved_after_the_clone_is_not_overwritten(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """Somebody else pushed while the fixer worked. The push is never forced,
    so their commit survives and the attempt says it did not publish."""
    red = f"swarm/{ROOT}"
    worker, _, task = _worker(
        worker_factory, monkeypatch, origin, {"strategy": "direct-pr", "continues": ROOT}
    )
    worker._maybe_clone(task)
    fix_the_test(worker.ws.work / lifecycle.REPO_DIR_NAME)

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "--quiet", "--branch", red, str(origin), str(other)],
        check=True, capture_output=True,
    )
    (other / "theirs.txt").write_text("a human got there first\n")
    _git(other, "add", "-A")
    _git(other, "commit", "--quiet", "-m", "theirs")
    _git(other, "push", "--quiet", "origin", red)
    theirs = refs(origin)[red]

    out = worker._harvest_git(publish=True)
    assert out["published"] is False, out
    assert refs(origin)[red] == theirs


def test_a_malformed_continuation_fails_the_clone_and_pushes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """swarm-api only ever writes a task id. Anything else was not written by
    it, and the worker must not turn it into a branch name."""
    before = refs(origin)
    for value in ("main", "../main", f"{ROOT}/../../main", 7, ""):
        worker, _, task = _worker(
            worker_factory, monkeypatch, origin, {"strategy": "direct-pr", "continues": value}
        )
        with pytest.raises(WorkerError):
            worker._maybe_clone(task)
    assert refs(origin) == before
    assert forge.pulls == []


# --------------------------------------------------------------------------
# The seam with swarm-api
# --------------------------------------------------------------------------

def test_every_id_the_api_mints_is_one_the_worker_accepts():
    """The API writes `new_id("task")`; the worker's pattern must take it."""
    for _ in range(50):
        task_id = new_id("task")
        block = DispatchOptions(strategy="direct-pr", continues=task_id).to_metadata()
        metadata = {DISPATCH_METADATA_KEY: block}
        assert continuation.clone_ref(metadata, "swarm/") == f"swarm/{task_id}"
        assert continuation.publish_branch(metadata, "swarm/", FIX) == f"swarm/{task_id}"


def test_no_continuation_means_the_tasks_own_branch_and_the_callers_ref():
    for metadata in (None, {}, {DISPATCH_METADATA_KEY: {"strategy": "direct-pr"}},
                     {DISPATCH_METADATA_KEY: DispatchOptions(strategy="direct-pr").to_metadata()}):
        assert continuation.clone_ref(metadata, "swarm/") is None
        assert continuation.publish_branch(metadata, "swarm/", FIX) == f"swarm/{FIX}"


def test_the_continued_branch_is_only_honoured_for_direct_pr():
    """`collect` pushes nothing and `integrate` has its own branch rules; a
    `continues` key beside either was not written by swarm-api."""
    for strategy in ("collect", "integrate"):
        metadata = {DISPATCH_METADATA_KEY: {"strategy": strategy, "continues": ROOT}}
        with pytest.raises(WorkerError):
            continuation.clone_ref(metadata, "swarm/")


def test_a_continued_integrator_is_the_branch_the_integrator_pushed():
    """#454's CI loop continues an `integrate` workflow's INTEGRATOR, whose pull
    request is the run's. The integrator publishes `<prefix><its own task id>`
    (it carries no `continues`), and a fix naming it clones and pushes exactly
    that branch -- so the fix lands on the integrator's pull request and the
    worker needs no second rule for it."""
    integrator = new_id("task")
    contributor = new_id("task")
    integrator_block = DispatchOptions(strategy="integrate").with_role(
        "integrator", integrates=[contributor]
    ).to_metadata()
    pushed = continuation.publish_branch(
        {DISPATCH_METADATA_KEY: integrator_block}, "swarm/", integrator
    )
    assert pushed == f"swarm/{integrator}"

    fix = {DISPATCH_METADATA_KEY: DispatchOptions(
        strategy="direct-pr", continues=integrator
    ).to_metadata()}
    assert continuation.clone_ref(fix, "swarm/") == pushed
    assert continuation.publish_branch(fix, "swarm/", FIX) == pushed
