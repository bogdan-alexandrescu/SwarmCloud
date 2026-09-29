"""The branch keeps each of the agent's commits, rewritten as the worker's (#242).

WHAT WENT WRONG. The worker folded every commit the agent made into ONE commit
it wrote (`gitops.fold_agent_commits`), so a direct-pr run told to push a
tests-only commit and then the fix -- the red-first shape this repository
requires -- reached the forge as a single commit, and CI could never show the
tests failing before the fix (PR #238: the agent made 2 commits, the branch
carried 1).

WHAT IS PINNED. Each commit on the agent's first-parent line is kept, in
order, with its own tree, rewritten as the platform identity with its message
cleaned of trailers and attribution footers; then ONE worker commit carries
anything left uncommitted. #219's rule is unchanged: the worker writes every
commit it pushes, and `verify_worker_authorship` still checks each one where
the work leaves.

Real git against local `file://` remotes; only the HTTP forge is faked, as in
`test_strategy_end_to_end.py`, whose fixtures these reuse.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _commit_as_claude,
    assert_only_the_worker_wrote,
    empty_origin,
    forge,
    local_urls,
    origin,
    pushed_commits,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

KEY = "sk-ant-supersecret-value-0123456789"


def _attempt(worker_factory, monkeypatch, remote: Path, *, task_id: str, edit, register=()):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{remote}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    for value in register:
        worker.log.register_secret(value)
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)
    return worker, config, worker._harvest_git(publish=True)


def _commit(repo: Path, message: str, *, name: str = "Claude", email: str = "noreply@anthropic.com"):
    ident = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    subprocess.run(["git", *ident, "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", *ident, "commit", "--quiet", "--allow-empty", "-m", message],
        cwd=str(repo), check=True, capture_output=True,
    )


TESTS_MESSAGE = (
    "Test that the widget refuses a negative size\n"
    "\n"
    "The widget accepted -1 and drew nothing. This commit adds the test only,\n"
    "so CI shows it failing before the fix.\n"
    "\n"
    "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
    "\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\n"
    "Signed-off-by: An Agent <agent@example.invalid>\n"
)
FIX_MESSAGE = (
    "The widget refuses a negative size\n"
    "\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\n"
)


def _edit_tests_then_fix_then_uncommitted(repo: Path) -> None:
    (repo / "test_widget.py").write_text("def test_negative(): assert False\n")
    _commit(repo, TESTS_MESSAGE)
    (repo / "widget.py").write_text("def size(n): raise ValueError if n < 0 else n\n")
    _commit(repo, FIX_MESSAGE)
    (repo / "notes.txt").write_text("left uncommitted\n")


def _files_at(bare: Path, sha: str) -> set[str]:
    return tree_at(bare, sha)


def _log(bare: Path, branch: str) -> list[str]:
    """The branch's commits over main, OLDEST first, as shas."""
    return subprocess.run(
        ["git", "rev-list", "--reverse", f"main..{branch}"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout.split()


def test_each_agent_commit_is_kept_in_order_then_one_worker_commit(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, out = _attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-kept", edit=_edit_tests_then_fix_then_uncommitted,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = list(reversed(pushed_commits(origin, branch)))  # oldest first
    assert len(commits) == 3, [c["message"] for c in commits]
    assert_only_the_worker_wrote(commits, config)
    for commit in commits:
        assert len(commit["parents"]) == 1, commit

    first, second, last = commits
    assert first["message"].splitlines()[0] == "Test that the widget refuses a negative size"
    assert "so CI shows it failing before the fix." in first["message"]
    assert "signed-off-by" not in first["message"].lower(), first["message"]
    assert second["message"].strip() == "The widget refuses a negative size", second["message"]
    assert "uncommitted" in last["message"], last["message"]

    # Each kept commit has ITS OWN tree: the tests commit has no fix in it,
    # which is the whole point -- CI can run it and watch it fail.
    shas = _log(origin, branch)
    assert "test_widget.py" in _files_at(origin, shas[0])
    assert "widget.py" not in _files_at(origin, shas[0])
    assert {"test_widget.py", "widget.py"} <= _files_at(origin, shas[1])
    assert "notes.txt" not in _files_at(origin, shas[1])
    assert "notes.txt" in _files_at(origin, shas[2])

    assert out["agent_commits_kept"] == 2, out
    assert "agent_commits_folded" not in out, out


def test_nothing_left_uncommitted_adds_no_worker_commit(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        (repo / "a.txt").write_text("a\n")
        _commit(repo, "Add a")
        (repo / "b.txt").write_text("b\n")
        _commit(repo, "Add b")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-clean", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    commits = list(reversed(pushed_commits(origin, branch)))
    assert [c["message"].strip() for c in commits] == ["Add a", "Add b"], commits
    assert_only_the_worker_wrote(commits, config)
    assert out["auto_committed"] is False


def test_a_registered_secret_in_an_agent_commit_message_never_reaches_the_forge(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        (repo / "a.txt").write_text("a\n")
        _commit(repo, f"Configure the client\n\nIt reads ANTHROPIC_API_KEY={KEY} at start.\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-secret", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, branch)
    assert commits and all(KEY not in c["message"] for c in commits), commits
    assert commits[-1]["message"].splitlines()[0] == "Configure the client"


def test_a_message_that_was_only_attribution_gets_a_worker_subject(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        (repo / "a.txt").write_text("a\n")
        _commit(repo, "Co-Authored-By: Claude <noreply@anthropic.com>\n")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-bare", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1, commits
    assert commits[0]["message"].strip(), "an empty commit message was pushed"
    assert config.task_id in commits[0]["message"], commits[0]["message"]
    assert_only_the_worker_wrote(commits, config)


def test_an_agent_merge_is_kept_as_one_commit_and_its_side_branch_is_not_pushed(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A merge's second parent is a line of commits the agent (or anyone) made;
    pushed as a parent, every one of them would reach the forge unrewritten.
    The merge is kept as one single-parent commit with the merged tree."""
    def edit(repo: Path) -> None:
        subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=str(repo), check=True)
        (repo / "side.txt").write_text("from the side branch\n")
        _commit(repo, "Side work")
        subprocess.run(["git", "checkout", "-q", "-"], cwd=str(repo), check=True)
        (repo / "main.txt").write_text("on the cloned branch\n")
        _commit(repo, "Main work")
        subprocess.run(
            ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
             "merge", "--no-ff", "-q", "-m", "Merge side", "side"],
            cwd=str(repo), check=True, capture_output=True,
        )

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-merge", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = list(reversed(pushed_commits(origin, branch)))
    assert [c["message"].strip() for c in commits] == ["Main work", "Merge side"], commits
    assert all(len(c["parents"]) == 1 for c in commits), commits
    assert_only_the_worker_wrote(commits, config)
    assert {"side.txt", "main.txt"} <= tree_at(origin, branch)


def test_an_empty_repository_keeps_the_agents_commits_from_a_parentless_first(
    worker_factory, monkeypatch, empty_origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        (repo / "one.txt").write_text("1\n")
        _commit_as_claude(repo, "one.txt")
        (repo / "two.txt").write_text("2\n")
        _commit(repo, "Add two")

    worker, config, _ = worker_factory(
        task_id="t-empty-kept", attempt_id="att-t-empty-kept", lease_id="lease-t-empty-kept",
        repository_url=f"file://{empty_origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-empty-kept", "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"] is None, "the repository was not empty"
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)

    out = worker._harvest_git(publish=True)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = list(reversed(pushed_commits(empty_origin, branch, since=None)))
    assert len(commits) == 2, [c["message"] for c in commits]
    assert commits[0]["parents"] == [], commits[0]
    assert commits[1]["message"].strip() == "Add two"
    assert_only_the_worker_wrote(commits, config)


def _all_object_bytes(bare: Path) -> bytes:
    """Every object in the bare repository, loose or packed, contents included."""
    return subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        cwd=str(bare), check=True, capture_output=True,
    ).stdout


def test_a_secret_added_then_deleted_is_folded_and_reaches_no_pushed_object(
    worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    """Kept one by one, the first commit's TREE would carry the key to the
    forge although the final tree is clean: a pushed object on a public
    repository is published for good. So a registered secret in any kept
    commit's diff folds the history into ONE worker commit of the final tree,
    and only the offending commit's index is logged (#259 review)."""
    def edit(repo: Path) -> None:
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        (repo / "client.py").write_text("import os\nKEY = os.environ['ANTHROPIC_API_KEY']\n")
        _commit(repo, "Configure the client")
        (repo / ".env").unlink()
        _commit(repo, "Stop committing the env file")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-leak", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1, [c["message"] for c in commits]
    assert_only_the_worker_wrote(commits, config)
    assert out.get("agent_commits_folded") == 2, out
    assert "agent_commits_kept" not in out, out
    files = tree_at(origin, branch)
    assert "client.py" in files and ".env" not in files, files
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"
    logged = log_stream.getvalue()
    assert '"commit_index": 1' in logged, "the dropped commit's index was not logged"
    assert KEY not in logged


#: Shaped like an AWS access key id (AWS's own documentation example), and
#: registered NOWHERE: only the credential patterns can catch it.
UNREGISTERED_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_an_unregistered_credential_in_an_intermediate_commit_folds_the_history(
    worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    """Owner decision 4, 2026-09-28: the per-commit scan matches the
    credential PATTERNS too (`swarm_redaction.RULES`), not only the task's
    registered secrets. A key the agent pasted into an `.env` is registered
    by no one; kept one by one, the first commit's tree would publish it.

    MUTATION: drop `_adds_a_credential` from `leaks=` in `_publish_git` and
    this history is kept as two commits, the first carrying the key."""
    def edit(repo: Path) -> None:
        (repo / ".env").write_text(f"AWS_SECRET_ACCESS_KEY={UNREGISTERED_AWS_KEY}\n")
        (repo / "client.py").write_text("import os\nREGION = os.environ.get('AWS_REGION')\n")
        _commit(repo, "Configure the client")
        (repo / ".env").unlink()
        _commit(repo, "Stop committing the env file")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-leak-pattern", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1, [c["message"] for c in commits]
    assert out.get("agent_commits_folded") == 2, out
    assert "agent_commits_kept" not in out, out
    assert ".env" not in tree_at(origin, branch)
    assert UNREGISTERED_AWS_KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"
    logged = log_stream.getvalue()
    assert '"commit_index": 1' in logged, "the dropped commit's index was not logged"
    assert UNREGISTERED_AWS_KEY not in logged


@pytest.mark.parametrize(
    ("diff", "found"),
    [
        (f"+++ b/.env\n@@ -0,0 +1 @@\n+AWS_SECRET_ACCESS_KEY={UNREGISTERED_AWS_KEY}\n", True),
        (f"--- a/.env\n+++ /dev/null\n@@ -1 +0,0 @@\n-AWS_SECRET_ACCESS_KEY={UNREGISTERED_AWS_KEY}\n", False),
        ("+++ b/notes.txt\n@@ -1 +1 @@\n-plain text\n+plain text, edited\n", False),
        # A family's prefix inside an identifier is not a credential: the JWT
        # rule alone would read `eyword_only_args` as a token.
        ("+++ b/api.py\n@@ -0,0 +1 @@\n+def f(*, keyword_only_args=None): pass\n", False),
        # Assembled, so no secret scanner reads this file as holding a key.
        ("+++ b/key.pem\n@@ -0,0 +2 @@\n+" + "-----" + "BEGIN RSA " + "PRIVATE KEY" + "-----"
         + "\n+MIIEowIBAAKCAQEA\n", True),
    ],
    ids=["added", "only-removed", "no-credential", "prefix-inside-an-identifier", "private-key"],
)
def test_the_pattern_scan_reads_only_the_lines_a_commit_adds(diff, found):
    """The control for the fold above: a line shaped like a key that a commit
    REMOVES was in its parent's tree already, and the commit is kept."""
    assert lifecycle._adds_a_credential(diff) is found


def test_a_history_that_does_not_descend_from_the_base_is_folded_as_before(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """An agent that moved its branch off the clone base (here: onto an orphan
    line) has no first-parent line from the base to keep. The fold still
    applies: one worker commit on the base with everything the agent left."""
    def edit(repo: Path) -> None:
        subprocess.run(["git", "checkout", "-q", "--orphan", "fresh"], cwd=str(repo), check=True)
        (repo / "fresh.txt").write_text("a new start\n")
        _commit(repo, "Start over")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-orphan", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1, commits
    assert_only_the_worker_wrote(commits, config)
    assert out.get("agent_commits_folded") == 1, out
