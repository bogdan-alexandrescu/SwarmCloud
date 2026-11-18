"""`result_summary.git.files`: the per-file counts the diff viewer reads first.

The harvest used to keep totals only, so a page that wanted "Changes · 12
files +886 −866", or a files × steps matrix, had to download and parse the
whole patch to say so -- and could say nothing at all when the patch was over
its cap and discarded. `summarize_work` now keeps one row per file of the same
diff the patch is taken from (`git diff -M <base>`, committed and uncommitted
work together), capped at `MAX_FILES_LISTED`.

Real git runs here, as in `test_harvest.py`: the parsing under test is of
`git diff --numstat -z` output, and a fixture of it would pin what I believe
git prints rather than what it prints.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agent_worker import gitops
from agent_worker.gitops import FileChange
from test_harvest import _git, _harvest, dirs, repo  # noqa: F401 -- fixtures
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    run_attempt,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _by_path(work) -> dict[str, FileChange]:
    return {f.path: f for f in work.files}


def test_an_empty_diff_lists_no_files(repo, dirs):
    path, base = repo
    work = _harvest(path, base, dirs)
    assert work.files == ()
    assert work.files_truncated is False


def test_an_added_file_is_status_a_with_its_line_count(repo, dirs):
    path, base = repo
    (path / "new.py").write_text("a\nb\nc\n")
    work = _harvest(path, base, dirs)
    # Untracked, never committed: `--intent-to-add` is what puts it in the diff.
    assert work.files == (
        FileChange(path="new.py", old_path=None, status="A", insertions=3, deletions=0, binary=False),
    )


def test_a_modified_file_is_status_m(repo, dirs):
    path, base = repo
    (path / "README.md").write_text("two\nthree\n")
    work = _harvest(path, base, dirs)
    assert work.files == (
        FileChange(path="README.md", old_path=None, status="M", insertions=2, deletions=1, binary=False),
    )


def test_a_deleted_file_is_status_d(repo, dirs):
    path, base = repo
    (path / "README.md").unlink()
    work = _harvest(path, base, dirs)
    assert work.files == (
        FileChange(path="README.md", old_path=None, status="D", insertions=0, deletions=1, binary=False),
    )


def test_a_rename_is_status_r_naming_both_paths(repo, dirs):
    path, base = repo
    body = "".join(f"line {n}\n" for n in range(40))
    (path / "old name.txt").write_text(body)
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "seed a file to move")
    base2 = _git_head(path)
    (path / "sub").mkdir()
    _git(path, "mv", "old name.txt", "sub/new name.txt")
    (path / "sub" / "new name.txt").write_text(body + "one more\n")
    work = _harvest(path, base2, dirs)
    assert work.files == (
        FileChange(
            path="sub/new name.txt",
            old_path="old name.txt",
            status="R",
            insertions=1,
            deletions=0,
            binary=False,
        ),
    )


def test_a_binary_file_reports_binary_with_no_counts(repo, dirs):
    path, base = repo
    (path / "logo.bin").write_bytes(bytes(range(256)) * 4)
    work = _harvest(path, base, dirs)
    assert work.files == (
        FileChange(path="logo.bin", old_path=None, status="A", insertions=None, deletions=None, binary=True),
    )


def test_committed_and_uncommitted_files_are_listed_together(repo, dirs):
    """The rows describe the patch, and the patch is base -> working tree."""
    path, base = repo
    (path / "a.txt").write_text("committed\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "agent commit")
    (path / "b.txt").write_text("not committed\n")
    work = _harvest(path, base, dirs)
    assert set(_by_path(work)) == {"a.txt", "b.txt"}


def test_the_list_is_capped_and_says_so(repo, dirs, monkeypatch):
    path, base = repo
    monkeypatch.setattr(gitops, "MAX_FILES_LISTED", 3)
    for n in range(5):
        (path / f"f{n}.txt").write_text(f"{n}\n")
    work = _harvest(path, base, dirs)
    assert len(work.files) == 3
    assert work.files_truncated is True


def test_exactly_the_cap_is_not_truncated(repo, dirs, monkeypatch):
    path, base = repo
    monkeypatch.setattr(gitops, "MAX_FILES_LISTED", 3)
    for n in range(3):
        (path / f"f{n}.txt").write_text(f"{n}\n")
    work = _harvest(path, base, dirs)
    assert len(work.files) == 3
    assert work.files_truncated is False


def test_files_are_kept_when_the_patch_is_over_its_cap(repo, dirs):
    """The case the totals-only summary could least describe: no patch, so no
    reader could count files from it. The counts do not depend on the patch."""
    path, base = repo
    (path / "big.txt").write_text("x" * 100 + "\n" * 50)
    work = _harvest(path, base, dirs, max_patch_bytes=64)
    assert work.patch_omitted is True
    assert [f.path for f in work.files] == ["big.txt"]


def test_paths_are_repository_paths_and_never_contents(repo, dirs):
    path, base = repo
    secret_like = "content-" + "z" * 30
    (path / "notes.txt").write_text(secret_like + "\n")
    work = _harvest(path, base, dirs)
    record = work.files[0].as_record(lambda v: v)
    assert record == {
        "path": "notes.txt",
        "old_path": None,
        "status": "A",
        "insertions": 1,
        "deletions": 0,
        "binary": False,
    }
    assert secret_like not in repr(work.files)


def test_as_record_scrubs_both_paths():
    change = FileChange(path="a/TOK", old_path="b/TOK", status="R", insertions=0, deletions=0, binary=False)
    record = change.as_record(lambda v: v.replace("TOK", "[redacted]"))
    assert record["path"] == "a/[redacted]"
    assert record["old_path"] == "b/[redacted]"


def test_a_path_with_a_tab_or_newline_survives(repo, dirs):
    """`-z` is what keeps git from C-quoting such a path, and keeps the row
    separator unambiguous."""
    path, base = repo
    odd = "we\tird\nname.txt"
    (path / odd).write_text("x\n")
    work = _harvest(path, base, dirs)
    assert [f.path for f in work.files] == [odd]


def test_no_base_means_files_were_not_measured(repo, dirs):
    path, _base = repo
    (path / "new.py").write_text("x\n")
    work = _harvest(path, None, dirs)
    assert work.files is None
    assert work.files_truncated is False


# -- reaching result_summary ------------------------------------------------


def test_the_harvest_record_carries_git_files(worker_factory, monkeypatch, origin, local_urls, forge):
    def edit(repo_path):
        (repo_path / "agent.txt").write_text("one\ntwo\n")
        (repo_path / "README.md").unlink()

    _worker, _config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-files",
        dispatch={"strategy": "collect", "carrier": "checkpoints"},
        edit=edit,
    )
    assert out["files_truncated"] is False
    assert sorted(out["files"], key=lambda f: f["path"]) == [
        {"path": "README.md", "old_path": None, "status": "D", "insertions": 0, "deletions": 1, "binary": False},
        {"path": "agent.txt", "old_path": None, "status": "A", "insertions": 2, "deletions": 0, "binary": False},
    ]


def _git_head(path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_a_capture_cut_mid_record_lists_only_whole_records():
    """`_git_text_full` caps its capture; the tail past the last NUL is a cut
    path, and a cut rename leaves an old path with no new one."""
    numstat = "1\t0\ta.txt\0" + "2\t1\tb.t"
    assert [f.path for f in gitops._parse_file_changes(numstat, "")] == ["a.txt"]
    cut_rename = "1\t0\ta.txt\0" + "0\t0\t\0old.txt\0new.t"
    assert [f.path for f in gitops._parse_file_changes(cut_rename, "")] == ["a.txt"]
