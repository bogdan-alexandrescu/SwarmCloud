"""A stacked lane that merges the default branch is scanned on its own lines only.

WHAT WENT WRONG (publish scan, merged-main floor, reported 2026-11-10). When a
lane merged a commit of the default branch, `final_tree_leak` diffed from the
FLOOR (`Worker._scan_floor`, the proven forge commit) INSTEAD of the clone
base. On a stacked branch the clone base is a parent branch already published
on the forge, holding lines the default branch does not, so every such line
was scanned again as if the lane had added it, and the lane was refused three
times for a parent's line it never wrote.

WHAT IS PINNED. Only a line the branch adds over BOTH the clone base and the
floor is asked about. The clone base is the commit the worker recorded when it
cloned from the forge -- never a value the agent names -- so a line only in it
is published already; a lane whose base held an unpublished secret is not a
case this has to allow. A line the lane adds itself is still refused, naming
its own file and its own line, with or without the floor.
"""

from __future__ import annotations

import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle
from agent_worker.gitops import GitError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_RNG = random.Random(20261110)


class _QuietLog:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _credential_line() -> str:
    """A real-shaped AWS key id in a quoted assignment, built at run time so
    the source never holds one."""
    while True:
        tail = "".join(_RNG.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        if not lifecycle._PLACEHOLDER.search(tail):
            return "aws_key = '" + "AK" + "IA" + tail + "'\n"


def _git(repo: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    return subprocess.run(
        ["git", *ident, *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _stacked(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository where `parent` (the stacked lane's clone base) holds a
    credential-shaped line the default branch does not, `main` moved on after
    `parent` forked -- gaining its own credential-shaped fixture -- and the
    lane (HEAD) is `parent` with `main` merged in. (repo, base, floor)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "logs").mkdir(exist_ok=True)
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    (repo / "README.md").write_text("a repository\n")
    _commit_all(repo, "start")
    _git(repo, "checkout", "--quiet", "-b", "parent")
    (repo / "config").mkdir()
    (repo / "config" / "parent_settings.py").write_text(_credential_line())
    base = _commit_all(repo, "the parent branch, published on the forge")
    _git(repo, "checkout", "--quiet", "main")
    (repo / "src").mkdir()
    (repo / "src" / "main_fixture.py").write_text(_credential_line())
    (repo / "notes.md").write_text("main moved on\n")
    floor = _commit_all(repo, "main moves on")
    _git(repo, "checkout", "--quiet", "-b", "lane", base)
    _git(repo, "merge", "--quiet", "--no-ff", "--no-edit", "main")
    return repo, base, floor


def _final(repo: Path, tmp_path: Path, base: str | None, floor: str | None) -> str | None:
    return lifecycle.final_tree_leak(
        repo=repo, base=base, leaks=lifecycle._credential_in, private_dir=tmp_path,
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_QuietLog(), floor=floor,
    )


def test_a_stacked_lane_that_merged_main_publishes_because_its_base_is_always_a_forge_ref(
    tmp_path,
):
    repo, base, floor = _stacked(tmp_path)
    (repo / "lane.txt").write_text("the lane's own work\n")
    _commit_all(repo, "the lane's own work")

    # Neither the parent's line (in the clone base, a forge ref the worker
    # recorded) nor main's fixture (in the floor) is the lane's addition.
    assert _final(repo, tmp_path, base, floor) is None


def test_a_credential_a_stacked_lane_adds_after_merging_main_is_still_refused_by_file(
    tmp_path,
):
    repo, base, floor = _stacked(tmp_path)
    (repo / "src" / "lane_settings.py").write_text(_credential_line())
    _commit_all(repo, "the lane adds a key")

    reason = _final(repo, tmp_path, base, floor)

    assert reason is not None
    assert "src/lane_settings.py" in reason, reason
    assert "parent_settings" not in reason and "main_fixture" not in reason, reason


def test_a_credential_appended_beside_a_base_line_is_refused_on_its_own_line(tmp_path):
    # The floor's diff adds the parent's whole file, the base's diff only the
    # lane's line: the hunk is cut to that line, and the refusal names it.
    repo, base, floor = _stacked(tmp_path)
    settings = repo / "config" / "parent_settings.py"
    settings.write_text(settings.read_text() + _credential_line())
    _commit_all(repo, "the lane appends a key")

    reason = _final(repo, tmp_path, base, floor)

    assert reason is not None
    assert "config/parent_settings.py" in reason, reason
    assert "line 2)" in reason, reason


def test_a_credential_appended_beside_a_main_line_is_refused_on_its_own_line(tmp_path):
    repo, base, floor = _stacked(tmp_path)
    fixture = repo / "src" / "main_fixture.py"
    fixture.write_text("# main's fixture, then the lane's key\n" + fixture.read_text()
                       + _credential_line())
    _commit_all(repo, "the lane edits main's fixture")

    reason = _final(repo, tmp_path, base, floor)

    assert reason is not None
    assert "src/main_fixture.py" in reason, reason
    assert "line 3)" in reason, reason


def test_without_a_floor_the_stacked_lanes_own_key_is_refused_as_before(tmp_path):
    repo, base, _floor = _stacked(tmp_path)
    _git(repo, "reset", "--quiet", "--hard", base)
    (repo / "src").mkdir()
    (repo / "src" / "lane_settings.py").write_text(_credential_line())
    _commit_all(repo, "the lane adds a key")

    reason = _final(repo, tmp_path, base, None)

    assert reason is not None and "src/lane_settings.py" in reason, reason


def test_an_unknown_base_is_still_refused_when_a_floor_is_given(tmp_path):
    repo, _base, floor = _stacked(tmp_path)

    with pytest.raises(GitError, match="clone base is unknown"):
        _final(repo, tmp_path, None, floor)


def test_the_empty_clone_base_with_a_floor_asks_only_what_the_floor_lacks(tmp_path):
    repo, _base, floor = _stacked(tmp_path)

    # From the empty tree everything is added; over the floor only the
    # parent's file is, so it is the one named.
    reason = _final(repo, tmp_path, lifecycle.EMPTY_CLONE_BASE, floor)

    assert reason is not None and "config/parent_settings.py" in reason, reason


@pytest.mark.parametrize("chunk", [1, 7, 1 << 20])
def test_the_filtered_scan_names_the_kept_line_in_small_windows_and_any_chunking(chunk):
    # One hunk of eight added lines (new lines 10-17): a credential on line
    # 11, which the other diff does not add, and one on line 16, which it
    # does. Windows far smaller than the hunk, and the stream fed in pieces
    # that cut lines, still skip line 11 and name line 16.
    lines = ["filler number %d\n" % i for i in range(8)]
    lines[1] = _credential_line()
    lines[6] = _credential_line()
    diff = (
        "diff --git a/app/settings.py b/app/settings.py\n"
        "--- a/app/settings.py\n+++ b/app/settings.py\n"
        "@@ -9,0 +10,8 @@\n" + "".join("+" + line for line in lines)
    ).encode()
    scanner = lifecycle._DiffLeakScanner(
        lifecycle._credential_in, window=40, overlap=8,
        within={"app/settings.py": [(14, 3), (30, 2)]},
    )
    for at in range(0, len(diff), chunk):
        scanner.feed(diff[at : at + chunk])
    hit = scanner.close()

    assert hit is not None
    assert (hit.path, hit.line) == ("app/settings.py", 16), hit
    assert scanner.added == {"app/settings.py": [(14, 3)]}


def test_a_hunk_the_other_diff_does_not_add_is_never_asked_about():
    asked: list[str] = []

    def leaks(path: str, text: str):
        asked.append(text)
        return lifecycle._credential_in(path, text)

    diff = (
        "diff --git a/app/settings.py b/app/settings.py\n"
        "--- a/app/settings.py\n+++ b/app/settings.py\n"
        "@@ -1,0 +2,1 @@\n+" + _credential_line()
        + "@@ -5,0 +7,1 @@\n+kept = 1\n"
    ).encode()
    scanner = lifecycle._DiffLeakScanner(
        leaks, window=4096, overlap=64, within={"app/settings.py": [(7, 1)]}
    )
    scanner.feed(diff)

    assert scanner.close() is None
    assert asked == ["kept = 1\n"]
