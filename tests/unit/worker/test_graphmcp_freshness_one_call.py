"""One git call per answer (docs/design/graph-service.md §4.3, lane GS-CORE).

`Freshness.observe` used to make three git calls an answer (`update-index
--refresh`, `diff --name-only <base>`, `ls-files --others`) against a copy of
the agent's index. It now makes one, `git status --porcelain=v2`, against a
private index holding the base commit's tree, built once at start.

Held here on a real repository, in the six cases §4.3 names -- staged,
committed, deleted, added-and-committed, renamed, an untracked directory --
the new dirty set equals the old three-call set, which `three_call_dirty`
below computes exactly as the old code did. In the seventh, a file committed
and then put back as it was, the new set is a superset by that file only:
over-marking errs safe.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest
import repo_index_fixtures as fx

from agent_worker.graphmcp import freshness as freshness_mod
from agent_worker.graphmcp.freshness import DIRTY, Freshness, parse_status

FILES = {
    ".gitignore": "*.log\nbuild/\n",
    "src/app.py": "def handle():\n    return 1\n",
    "src/store.py": "def fetch():\n    return 2\n",
    "src/util.py": "def helper():\n    return 3\n",
    "README.md": "fixture\n",
}


def git(repo: Path, *args: str) -> str:
    env = fx.git_env(repo.parent / "home")
    env.update(GIT_AUTHOR_DATE="@1780000500 +0000", GIT_COMMITTER_DATE="@1780000500 +0000")
    done = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True,
                          check=True)
    return done.stdout.decode()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    return fx.build_repo(tmp_path_factory.mktemp("one-call") / "repo", FILES)


@pytest.fixture
def repo(built, tmp_path) -> Path:
    (tmp_path / "home").mkdir()
    return Path(shutil.copytree(built, tmp_path / "repo", symlinks=True))


def three_call_dirty(repo: Path, base: str, start_index: Path) -> set[str]:
    """The old `observe`: refresh a copy of the agent's index taken at start,
    then `diff --name-only <base>` and `ls-files --others --directory`."""
    env = {"GIT_INDEX_FILE": str(start_index)}

    def run(*args: str) -> str:
        done = subprocess.run(["git", "--no-optional-locks", "-c", "core.quotepath=off",
                               "-C", str(repo), *args], capture_output=True, check=False,
                              env={**fx.git_env(repo.parent / "home"), **env})
        return done.stdout.decode()

    subprocess.run(["git", "-C", str(repo), "update-index", "-q", "--refresh", "--unmerged"],
                   capture_output=True, check=False,
                   env={**fx.git_env(repo.parent / "home"), **env})
    tracked = {p for p in run("diff", "--name-only", "-z", "--no-renames", base).split("\0")
               if p}
    untracked = {p for p in run("ls-files", "-z", "--others", "--exclude-standard",
                                "--directory").split("\0") if p}
    return tracked | untracked


def staged(repo: Path) -> None:
    (repo / "src/app.py").write_text("def handle():\n    return 10\n")
    git(repo, "add", "src/app.py")


def committed(repo: Path) -> None:
    (repo / "src/store.py").write_text("def fetch():\n    return 20\n")
    git(repo, "commit", "-qam", "change store")


def deleted(repo: Path) -> None:
    (repo / "src/util.py").unlink()


def added_and_committed(repo: Path) -> None:
    (repo / "src/new.py").write_text("def fresh():\n    return 4\n")
    git(repo, "add", "src/new.py")
    git(repo, "commit", "-qm", "add new")


def renamed(repo: Path) -> None:
    git(repo, "mv", "src/util.py", "src/tools.py")
    git(repo, "commit", "-qm", "rename util")


def untracked_directory(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs/a.md").write_text("a\n")
    (repo / "docs/b.md").write_text("b\n")
    (repo / "build").mkdir()
    (repo / "build/out.txt").write_text("ignored\n")
    (repo / "run.log").write_text("ignored\n")


CASES: dict[str, tuple[Callable[[Path], None], set[str]]] = {
    "staged": (staged, {"src/app.py"}),
    "committed": (committed, {"src/store.py"}),
    "deleted": (deleted, {"src/util.py"}),
    "added_and_committed": (added_and_committed, {"src/new.py"}),
    "renamed": (renamed, {"src/util.py", "src/tools.py"}),
    "untracked_directory": (untracked_directory, {"docs/"}),
}


def start(repo: Path, tmp_path: Path) -> tuple[Freshness, str, Path]:
    """The server's start, and the agent's index as the old code copied it."""
    base = git(repo, "rev-parse", "HEAD").strip()
    fresh = Freshness.establish(base, workdir=repo, record=None)
    copy = tmp_path / "old-index"
    shutil.copyfile(repo / ".git" / "index", copy)
    return fresh, base, copy


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_one_call_dirty_set_equals_the_three_call_set(repo, tmp_path, case):
    change, expected = CASES[case]
    fresh, base, old_index = start(repo, tmp_path)
    assert fresh.observe().block["dirty"] == 0
    change(repo)
    obs = fresh.observe()
    new = set(obs.dirty) | set(obs.dirty_dirs)
    assert new == expected, case
    assert new == three_call_dirty(repo, base, old_index), case
    assert obs.block["dirty"] == len(expected)


def test_all_six_cases_at_once(repo, tmp_path):
    fresh, base, old_index = start(repo, tmp_path)
    for name in ("staged", "committed", "deleted", "added_and_committed", "untracked_directory"):
        CASES[name][0](repo)
    obs = fresh.observe()
    new = set(obs.dirty) | set(obs.dirty_dirs)
    assert new == three_call_dirty(repo, base, old_index)
    assert new == {"src/app.py", "src/store.py", "src/util.py", "src/new.py", "docs/"}
    assert obs.stale("docs/a.md") == DIRTY
    assert obs.stale("src/app.py") == DIRTY
    assert obs.stale("README.md") is None
    assert obs.stale("run.log") is None


def test_a_file_committed_then_put_back_is_over_marked_and_only_it(repo, tmp_path):
    fresh, base, old_index = start(repo, tmp_path)
    original = (repo / "src/app.py").read_text()
    (repo / "src/app.py").write_text("def handle():\n    return 99\n")
    git(repo, "commit", "-qam", "change app")
    (repo / "src/app.py").write_text(original)
    new = set(fresh.observe().dirty)
    old = three_call_dirty(repo, base, old_index)
    assert old == set()
    assert new - old == {"src/app.py"}


def test_each_answer_makes_exactly_one_git_call(repo, tmp_path, monkeypatch):
    fresh, _base, _old = start(repo, tmp_path)
    staged(repo)
    calls: list[list[str]] = []
    real = subprocess.run

    def counting(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(freshness_mod.subprocess, "run", counting)
    for _ in range(3):
        assert fresh.observe().dirty == frozenset({"src/app.py"})
    assert len(calls) == 3
    assert all("status" in argv and "--porcelain=v2" in argv for argv in calls)
    # The private index is the server's own: its stat cache may be written.
    assert all("--no-optional-locks" not in argv for argv in calls)


def test_the_agents_index_is_never_written(repo, tmp_path):
    before = (repo / ".git" / "index").read_bytes()
    fresh, _base, _old = start(repo, tmp_path)
    (repo / "src/app.py").write_text("def handle():\n    return 5\n")
    for _ in range(2):
        fresh.observe()
    assert (repo / ".git" / "index").read_bytes() == before
    assert not (repo / ".git" / "index.lock").exists()


def test_a_git_call_that_fails_says_unknown_never_zero(repo, tmp_path, monkeypatch):
    fresh, _base, _old = start(repo, tmp_path)
    monkeypatch.setattr(freshness_mod, "_git", lambda *a, **k: None)
    assert fresh.observe().block["dirty"] == "unknown"


def test_a_base_whose_tree_cannot_be_read_says_unknown(repo):
    record = {"index_sha": "f" * 40, "base_sha": "e" * 40, "behind_by": 0, "changed": []}
    fresh = Freshness.establish("f" * 40, workdir=repo, record=record)
    block = fresh.observe().block
    assert block["dirty"] == "unknown"
    assert "base commit's tree could not be read" in block["note"]


def test_the_private_index_is_of_the_recorded_base_not_the_agents_head(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    committed(repo)
    record = {"index_sha": base, "base_sha": base, "behind_by": 0, "changed": []}
    fresh = Freshness.establish(base, workdir=repo, record=record)
    assert fresh.observe().dirty == frozenset({"src/store.py"})


def test_parse_status_reads_every_record_kind():
    output = "\0".join([
        "# branch.oid abc",
        "1 .M N... 100644 100644 100644 aaa aaa src/a b.py",
        "1 D. N... 100644 000000 000000 aaa 000 src/gone.py",
        "u UU N... 100644 100644 100644 100644 aaa bbb ccc src/conflict.py",
        "? new dir/",
        "? loose.txt",
        "! ignored.log",
    ]) + "\0"
    files, dirs = parse_status(output)
    assert files == {"src/a b.py", "src/gone.py", "src/conflict.py", "loose.txt"}
    assert dirs == {"new dir/"}
