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

import base64
import json
import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt
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


# -- credential-shaped values, built at run time (#373) ----------------------
#
# Every value below that a secret scanner would take for a real credential is
# ASSEMBLED when the tests run, from parts or from a seeded generator: this
# repository is public and its own CI runs trivy's secret scan over the
# source, so no contiguous credential-shaped literal may sit in this file.

_RNG = random.Random(373)
_ALNUM = string.ascii_letters + string.digits


def _shape(*parts: str) -> str:
    """Join parts into one value, so the source never holds it whole."""
    return "".join(parts)


def _random_token(length: int, alphabet: str = _ALNUM, digits: int = 4) -> str:
    """`length` DISTINCT characters, at least `digits` of them digits and the
    rest letters: high entropy and a letter/digit mix by construction, so a
    test that needs a credential-shaped value always gets one."""
    letters = [c for c in alphabet if not c.isdigit()]
    numbers = [c for c in alphabet if c.isdigit()]
    chars = _RNG.sample(numbers, digits) + _RNG.sample(letters, length - digits)
    _RNG.shuffle(chars)
    return "".join(chars)


def _pem_marker(edge: str) -> str:
    return _shape("-----", edge, " RSA ", "PRIVATE", " KEY", "-----")


def _pem_lines(body_chars: int) -> list[str]:
    """A private-key block whose body is `body_chars` random base64
    characters, in 64-character lines between its markers."""
    body = "".join(_RNG.choice(_ALNUM + "+/") for _ in range(body_chars))
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return [_pem_marker("BEGIN"), *lines, _pem_marker("END")]


def _jwt() -> str:
    """A token whose header decodes to JSON naming an `alg`."""
    def part(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = part(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = part(json.dumps({"sub": _random_token(12)}).encode())
    return f"{header}.{claims}.{_random_token(40)}"


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

    MUTATION: drop `_credential_in` from `Worker._leaks_in_added_text` and
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
        # A marker over a STUB body is not a key (owner decision, 2026-09-30,
        # #373): only a body of about 100 high-entropy base64 characters is.
        ("+++ b/key.pem\n@@ -0,0 +2 @@\n+" + "-----" + "BEGIN RSA " + "PRIVATE KEY" + "-----"
         + "\n+MIIEowIBAAKCAQEA\n", False),
        ("+++ b/key.pem\n@@ -0,0 +4 @@\n+" + "\n+".join(_pem_lines(200)) + "\n", True),
        # The key/value rule counts only for a LITERAL (owner decision,
        # 2026-09-28): a quoted string, or a bare credential-shaped token.
        ("+++ b/auth.py\n@@ -0,0 +1 @@\n+token = get_token()\n", False),
        ("+++ b/auth.py\n@@ -0,0 +1 @@\n+    password: str\n", False),
        ("+++ b/auth.py\n@@ -0,0 +1 @@\n+secret = self.secret_name\n", False),
        ("+++ b/auth.py\n@@ -0,0 +1 @@\n+password = \"hunter-correct-horse\"\n", True),
        ("+++ b/auth.py\n@@ -0,0 +1 @@\n+password = 'hunter-correct-horse'\n", True),
        ("+++ b/.env\n@@ -0,0 +1 @@\n+DB_SECRET=a1b2c3d4e5f6g7h8\n", True),
    ],
    ids=[
        "added", "only-removed", "no-credential", "prefix-inside-an-identifier",
        "private-key-stub-body", "private-key-real-body",
        "kv-call", "kv-annotation", "kv-attribute", "kv-double-quoted", "kv-single-quoted",
        "kv-bare-token",
    ],
)
def test_the_pattern_scan_reads_only_the_lines_a_commit_adds(diff, found):
    """The control for the fold above: a line shaped like a key that a commit
    REMOVES was in its parent's tree already, and the commit is kept."""
    assert lifecycle._adds_a_credential(diff) is found


def test_code_that_names_a_credential_without_holding_one_keeps_the_history(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Owner decision, 2026-09-28: `token = get_token()` and `password: str`
    in an intermediate commit are code, not credentials, and do not fold."""
    def edit(repo: Path) -> None:
        (repo / "auth.py").write_text(
            "class Login:\n    password: str\n\n\ndef load():\n    token = get_token()\n    return token\n"
        )
        _commit(repo, "Add the login")
        (repo / "notes.txt").write_text("done\n")
        _commit(repo, "Add notes")

    _, _, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-kv-code", edit=edit)

    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_kept") == 2, out
    assert "agent_commits_folded" not in out, out


def test_a_quoted_credential_assigned_in_an_intermediate_commit_folds_the_history(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The counterpart: a literal assigned to a credential's name folds."""
    literal = "sk-" + "live-" + "0123456789abcdefABCDEF"  # assembled for secret scanners

    def edit(repo: Path) -> None:
        (repo / "settings.py").write_text(f'API_KEY = "{literal}"\n')
        _commit(repo, "Configure the key")
        (repo / "settings.py").write_text("import os\nAPI_KEY = os.environ['API_KEY']\n")
        _commit(repo, "Read the key from the environment")

    _, _, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-kv-literal", edit=edit)

    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_folded") == 2, out
    assert literal.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


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


# -- a graft cannot make the replay skip a commit (#259 re-review) --


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_a_graft_that_skips_the_secret_commit_still_folds(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """C1 adds a registered key, C2 an unrelated file, C3 deletes the key; the
    agent grafts C2 straight onto the base. `rev-list` and `merge-base` follow
    `.git/info/grafts`, so the list read [C2, C3]: C2 scanned against its
    real parent C1 added nothing, C3 only removed, and C2's rewrite -- whose
    TREE still holds `.env` -- was pushed with the key in it."""
    def edit(repo: Path) -> None:
        base = _rev(repo, "HEAD")
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        _commit(repo, "Configure the client")
        (repo / "a.txt").write_text("a\n")
        _commit(repo, "Add a")
        second = _rev(repo, "HEAD")
        (repo / ".env").unlink()
        _commit(repo, "Stop committing the env file")
        (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{second} {base}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graft", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_folded") == 3, out
    assert "agent_commits_kept" not in out, out
    assert ".env" not in tree_at(origin, branch)
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


class _QuietLog:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def test_a_list_that_skips_a_commit_counts_as_a_hit(tmp_path, origin):
    """The chain check on its own, with nothing leaking: a list whose first
    entry does not sit on the base, or whose next does not sit on the entry
    before it, is not the chain the replay writes, and folds."""
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(repo)], check=True, capture_output=True)
    base = _rev(repo, "HEAD")
    shas = []
    for name in ("one", "two", "three"):
        (repo / f"{name}.txt").write_text(f"{name}\n")
        _commit(repo, f"Add {name}")
        shas.append(_rev(repo, "HEAD"))
    (tmp_path / "logs").mkdir()

    def first(listed: list[str]) -> int | None:
        return lifecycle._first_leaking_commit(
            shas=listed, keep=None, leaks=lambda _path, _text: None, git=["git"], repo=repo,
            private_dir=tmp_path, logs_dir=tmp_path / "logs", timeout_seconds=60,
            logger=_QuietLog(), floor=base,
        )

    assert first(shas) is None
    assert first(shas[1:]) == 1
    assert first([shas[0], shas[2]]) == 2


# -- the per-commit scan cannot be shown a replacement object (#259 review, M1) --


def test_a_replace_ref_over_a_secret_blob_does_not_hide_it_from_the_scan(
    worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    """The scan runs in the agent's own clone, whose refs the agent owns. A
    `refs/replace/<blob>` entry makes `cat-file`/`diff` -- read commands --
    show a clean replacement for a blob that still holds the real, registered
    secret; `git replace` never touches the tree a commit points at, so
    `commit-tree`, pack-objects and the eventual push always carry the REAL
    object regardless. Without `GIT_NO_REPLACE_OBJECTS=1` on every worker git
    command, the scan would read the clean replacement, call this commit
    clean, and push the real secret unfolded.

    Verified directly against git 2.40.1 (not asserted here, but what this
    test exercises): `git replace <blob> <clean>` makes `git cat-file -p
    <blob>` and `git diff` print the clean content, while the real object
    stays in the store and is what a clone of the pushed branch would carry.
    """
    def edit(repo: Path) -> None:
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        # A file that stays, so the final tree differs from the base and the
        # fold has a commit to push: with `.env` alone, added and deleted,
        # the branch equalled main and there was nothing to count.
        (repo / "client.py").write_text("import os\nKEY = os.environ['ANTHROPIC_API_KEY']\n")
        _commit(repo, "Configure the client")
        # This clone already has history from `origin`, so the sha to replace
        # a blob under is THIS commit's, not the repository's true root.
        first_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), check=True, capture_output=True, text=True,
        ).stdout.strip()
        (repo / ".env").unlink()
        _commit(repo, "Stop committing the env file")

        # The agent creates a replacement for the FIRST commit's `.env` blob.
        # This only changes what read commands print for that object id; it
        # is not a rewrite of history, and it is never pushed (the worker's
        # publish repository is a separate, worker-owned clone).
        real_blob = subprocess.run(
            ["git", "rev-parse", f"{first_sha}:.env"],
            cwd=str(repo), check=True, capture_output=True, text=True,
        ).stdout.strip()
        clean_blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(repo), input="ANTHROPIC_API_KEY=not-a-secret\n",
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "replace", real_blob, clean_blob],
            cwd=str(repo), check=True, capture_output=True, text=True,
        )

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-replace-hidden", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1, [c["message"] for c in commits]
    assert out.get("agent_commits_folded") == 2, out
    assert "agent_commits_kept" not in out, out
    assert ".env" not in tree_at(origin, branch)
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


# -- a big diff is scanned whole, in chunks, and never refused for its size --
#
# Owner decision, 2026-09-29 (#259): the scans stream every diff through
# overlapping windows (`_DiffLeakScanner`). A diff past 32 MiB -- the cap the
# capture used to cut at -- is read to its end: clean, it publishes; a key past
# the old cap is still found. Both files are CRLF lines, so the scan's bytes
# are never newline-translated either (#259 review, M2).

#: 40 MB of CRLF lines: `+x\r\n` per line puts the diff at about 53 MB.
BIG_CLEAN = b"x\r\n" * 13_300_000


def test_a_40_mb_clean_diff_publishes_commit_by_commit(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        (repo / "big.py").write_bytes(BIG_CLEAN)
        _commit(repo, "Add a big CRLF file")
        (repo / "notes.txt").write_text("done\n")
        _commit(repo, "Add notes")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-big-clean", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert "final_tree_leak" not in out, out
    assert out.get("agent_commits_kept") == 2, out
    assert "agent_commits_folded" not in out, out
    assert {"big.py", "notes.txt"} <= tree_at(origin, branch)


def test_a_key_past_the_32_mib_mark_of_a_40_mb_diff_is_caught(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The key sits about 46 MB into the diff, past everything the old
    32 MiB capture kept, and is found and named by file."""
    head = b"x\r\n" * 11_500_000
    tail = b"x\r\n" * 1_800_000
    content = head + f"AWS_ACCESS_KEY_ID={UNREGISTERED_AWS_KEY}\r\n".encode() + tail

    def edit(repo: Path) -> None:
        (repo / "big.py").write_bytes(content)
        _commit(repo, "Add a big CRLF file")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-big-key", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is False, out
    assert out.get("final_tree_leak") == (
        "the final tree adds a credential in big.py (rule aws_access_key_id, line 11500001); remove it"
    ), out
    assert not _branch_exists(origin, branch), "a branch was pushed"


def _scan_hit(diff: bytes, *, window: int, overlap: int, piece: int, leaks=None):
    """The scanner's whole finding: (path, rule, line), or None."""
    scanner = lifecycle._DiffLeakScanner(
        leaks or lifecycle._credential_in, window=window, overlap=overlap
    )
    for start in range(0, len(diff), piece):
        scanner.feed(diff[start : start + piece])
    return scanner.close()


def _scan(diff: bytes, *, window: int, overlap: int, piece: int, leaks=None) -> str | None:
    """The path the scanner names, or None."""
    hit = _scan_hit(diff, window=window, overlap=overlap, piece=piece, leaks=leaks)
    return None if hit is None else hit.path


def _one_file_diff(path: str, lines: list[str]) -> bytes:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    ).encode()


@pytest.mark.parametrize("piece", [1, 7, 4096])
def test_a_key_straddling_a_window_edge_is_found(piece):
    """With 200-character windows overlapping by 64, a key starting four
    characters before the first window's edge -- too few for the AWS rule to
    match there -- is whole in the second; fed a byte at a time, in odd
    pieces, or whole, the parse is the same."""
    filler = ["y" * 30] * 6  # 186 characters with newlines
    diff = _one_file_diff("a.txt", [*filler, f"{'z' * 9} {UNREGISTERED_AWS_KEY}", *filler])
    assert _scan(diff, window=200, overlap=64, piece=piece) == "a.txt"
    # The line is counted across the window edge too: six filler lines, then the key.
    assert _scan_hit(diff, window=200, overlap=64, piece=piece) == ("a.txt", "aws_access_key_id", 7)


def test_a_registered_value_straddling_a_window_edge_is_found():
    secret = "registered-" + "s" * 90  # longer than the base overlap
    filler = ["y" * 30] * 6
    diff = _one_file_diff("b.txt", [*filler, secret, *filler])
    def leaks(_path: str, text: str):
        at = text.find(secret)
        return None if at < 0 else lifecycle.CredentialHit("registered_secret", at)

    found = _scan_hit(diff, window=200, overlap=32 + len(secret), piece=5, leaks=leaks)
    assert found == ("b.txt", "registered_secret", 7)


def test_the_stream_scan_names_the_file_and_reads_only_added_lines():
    diff = (
        "diff --git a/clean.txt b/clean.txt\n--- a/clean.txt\n+++ b/clean.txt\n"
        f"@@ -1 +1 @@\n-{UNREGISTERED_AWS_KEY}\n+nothing here\n"
        "diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n"
        f"@@ -0,0 +1 @@\n+x = 1\r{UNREGISTERED_AWS_KEY}\n"
    ).encode()
    assert _scan(diff, window=1 << 20, overlap=64, piece=3) == "cfg.py"
    removed_only = diff.split(b"diff --git a/cfg.py")[0]
    assert _scan(removed_only, window=1 << 20, overlap=64, piece=3) is None


def test_a_multibyte_character_split_across_chunks_is_decoded_whole():
    diff = _one_file_diff("c.txt", [f"café {UNREGISTERED_AWS_KEY}"])
    assert _scan(diff, window=1 << 20, overlap=64, piece=1) == "c.txt"


def test_a_lone_carriage_return_does_not_hide_a_credential_from_the_scan(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A LONE `\\r` (not part of `\\r\\n`) inside a line git's own diff leaves
    embedded (git splits lines on `\\n` only). Reading the capture with the
    default newline handling translates that `\\r` into a `\\n`, splitting
    `+x = 1\\rAKIAIOSFODNN7EXAMPLE` into `+x = 1` and `AKIAIOSFODNN7EXAMPLE` --
    the second half loses its `+` prefix, so `_adds_a_credential` (which reads
    only lines starting with `+`) never sees the credential that follows.
    Reading with `newline=""` keeps the `\\r` embedded in one line.

    Verified directly (git 2.40.1): a file written with an embedded `\\r` (no
    `\\n`) produces a diff whose added line literally contains
    `x = 1\\rAKIAIOSFODNN7EXAMPLE`, unescaped.
    """
    def edit(repo: Path) -> None:
        (repo / "config.py").write_bytes(f"x = 1\r{UNREGISTERED_AWS_KEY}\n".encode())
        _commit(repo, "Configure the client")
        # Deleted again, so the final tree is clean and passes its own scan
        # (`final_tree_leak`); only the first commit's tree holds the key.
        (repo / "config.py").unlink()
        (repo / "notes.txt").write_text("done\n")
        _commit(repo, "Stop committing the config")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-cr-credential", edit=edit,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1, [c["message"] for c in commits]
    assert out.get("agent_commits_folded") == 2, out
    assert "agent_commits_kept" not in out, out
    assert UNREGISTERED_AWS_KEY.encode() not in _all_object_bytes(origin), (
        "the key reached a pushed object"
    )


# -- the branch as it will be pushed is scanned before any push (#259 review, M4) --
#
# Owner decision, 2026-09-28: the per-commit scan decides only between keeping
# the agent's commits and folding them; the fold's one commit, and the
# worker's own commit of uncommitted work, carry the FINAL tree, which it never
# read. So the same leak check runs over base..final before any push. On a
# hit nothing is published, and the attempt fails retryably, naming the file
# and never the value.


def _branch_exists(bare: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(bare), capture_output=True, text=True,
    ).returncode == 0


def test_an_env_committed_with_a_registered_key_and_never_deleted_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    """The per-commit scan folds this history, and the fold's one commit then
    carried `.env` -- key and all -- to the forge, because the fold pushes the
    final tree unscanned."""
    def edit(repo: Path) -> None:
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        (repo / "client.py").write_text("import os\nKEY = os.environ['ANTHROPIC_API_KEY']\n")
        _commit(repo, "Configure the client")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-final-leak", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is False, out
    assert out.get("final_tree_leak") == (
        "the final tree adds a credential in .env (rule registered_secret, line 1); remove it"
    ), out
    assert ".env" in out["publish_reason"], out["publish_reason"]
    assert KEY not in str(out), "the value reached the publish result"
    assert not _branch_exists(origin, branch), "a branch was pushed"
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"
    assert KEY not in log_stream.getvalue()


def test_an_uncommitted_credential_in_the_final_tree_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The worker's own commit of uncommitted work is scanned too: an `.env`
    the agent wrote and never committed, holding a key registered nowhere,
    was committed by the worker and pushed."""
    def edit(repo: Path) -> None:
        (repo / "client.py").write_text("import os\nREGION = os.environ.get('AWS_REGION')\n")
        _commit(repo, "Configure the client")
        (repo / ".env").write_text(f"AWS_ACCESS_KEY_ID={UNREGISTERED_AWS_KEY}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-final-uncommitted", edit=edit,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is False, out
    assert out.get("final_tree_leak") == (
        "the final tree adds a credential in .env (rule aws_access_key_id, line 1); remove it"
    ), out
    assert UNREGISTERED_AWS_KEY not in str(out), "the value reached the publish result"
    assert not _branch_exists(origin, branch), "a branch was pushed"
    assert UNREGISTERED_AWS_KEY.encode() not in _all_object_bytes(origin), (
        "the key reached a pushed object"
    )


def test_a_clean_final_tree_publishes(worker_factory, monkeypatch, origin, local_urls, forge):
    """The control: the check refuses a credential, not a change. A final tree
    that adds none -- including code that names a credential without holding
    one -- is pushed."""
    def edit(repo: Path) -> None:
        (repo / "auth.py").write_text("token = get_token()\n")
        _commit(repo, "Read the token at start")
        (repo / "notes.txt").write_text("uncommitted\n")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-final-clean", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert "final_tree_leak" not in out, out
    assert {"auth.py", "notes.txt"} <= tree_at(origin, branch)


def test_a_final_tree_leak_fails_the_attempt_retryably_naming_the_file(
    db, worker_factory, monkeypatch
):
    """The publish half returns `final_tree_leak`; the finish turns it into a
    retryable failure of the attempt, with the file-naming reason as its
    error, instead of a SUCCEEDED task whose work was never published."""
    seed_attempt(db, task_input={"prompt": "fix the widget", "steps": 1, "sleep_seconds": 0.01})
    db.doc("tasks/task_1")["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    worker, _config, _exporter = worker_factory()
    reason = "the final tree adds a credential in .env (rule aws_access_key_id, line 1); remove it"

    def harvest(*, publish: bool, **_kwargs):
        return {
            "published": False,
            "final_tree_leak": reason,
            "publish_reason": f"refusing to publish: {reason}",
        }

    monkeypatch.setattr(worker, "_harvest_git", harvest)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task["state"]
    assert reason in task["last_error"], task["last_error"]
    retrying = [e for e in db.events("task_1") if e["type"] == "retrying"]
    assert len(retrying) == 1, db.event_types("task_1")
    assert retrying[0]["detail"]["cause"] == "final_tree_adds_a_credential"
    assert "succeeded" not in db.event_types("task_1")


def test_an_added_line_that_reads_like_a_file_header_is_still_scanned():
    """An added line whose own text starts `++ ` prints as `+++ ...`. Only the
    `+++ ` line before a file's first `@@` is its header; after that, every
    `+` line is content, or a credential could hide behind two plus signs."""
    diff = (
        "diff --git a/notes.txt b/notes.txt\n"
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -0,0 +1 @@\n"
        f"+++ {UNREGISTERED_AWS_KEY}\n"
    )
    assert lifecycle._adds_a_credential(diff) is True


# -- the tiered, path-aware publish guard (#373) ------------------------------
#
# OWNER DECISION, 2026-09-30 (#373). The publish scans refused every test of
# the redaction rules, because such a test has to hold text the rules match
# (`"password": "********"`, `secret="<name>"`), and no SwarmCloud agent could
# publish one. The guard is now tiered by the file's path:
#
# * TIER 1, every file: a registered secret (never relaxed); a vendor-shaped
#   key, which in a TEST path must also carry a credential-shaped tail; a JWT
#   whose header decodes to JSON naming `alg`; a private-key block with a real
#   body of about 100 high-entropy base64 characters.
# * TIER 2, outside test paths: the generic rules (key/value, Bearer/Basic,
#   the Authorization scheme) refuse exactly as before.
# * TIER 3, in test paths: a generic match refuses only when its value looks
#   like a credential -- 16 or more characters, high entropy, letters and
#   digits mixed, not a placeholder.
#
# Both scans -- per commit and final tree -- ask the same predicate, so they
# agree. A refusal names the rule and the line, never the value.


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/unit/worker/helpers.py", True),
        ("test/fixture.go", True),
        ("web/src/__tests__/App.jsx", True),
        ("apps/swarm-api/tests/conftest.py", True),
        ("src/test_module.py", True),
        ("src/app/test_settings.py", True),
        ("tests/unit/control_plane/test_json_artifact_masking.py", True),
        ("test_top_level.py", True),
        ("pkg/parse/parse_test.go", True),
        ("src/module_test.py", True),
        ("web/src/App.test.tsx", True),
        ("web/src/App.spec.ts", True),
        ("web/src/util.test.js", True),
        ("web/src/util.spec.jsx", True),
        ("web/src/App.test.py", True),
        ("Tests/x.py", True),
        ("src/TESTS/x.sh", True),
        # Config and data files are strict everywhere, under tests/ too.
        ("pkg/parse/testdata/input.txt", False),
        ("fixtures/sample.json", False),
        ("tests/fixtures/secrets.yaml", False),
        ("tests/secrets.yaml", False),
        ("tests/prod.env", False),
        ("tests/service-account.json", False),
        ("deploy/fixtures/prod.yaml", False),
        ("testdata/aws_config", False),
        ("tests/settings.toml", False),
        ("tests/app.ini", False),
        ("tests/Makefile", False),
        ("src/app.py", False),
        ("testing/helpers.py", False),
        ("contest/entry.py", False),
        ("src/latest/app.py", False),
        ("src/test.py", False),
        ("src/mytest_module.py", False),
        ("web/src/App.tests.ts", False),
        ("", False),
        # NEVER a test path, even under tests/: the files real credentials live in.
        ("tests/fixtures/.env", False),
        ("tests/.env.local", False),
        ("tests/certs/server.pem", False),
        ("tests/keys/id_rsa.key", False),
        ("tests/fixtures/credentials.json", False),
        ("tests/unit/test_credential_store.py", False),
        ("tests/fixtures/client_secret.json", False),
        ("fixtures/secret.json", False),
    ],
)
def test_the_test_path_classifier(path, is_test):
    assert lifecycle.is_test_path(path) is is_test


def _rule_for(worker, path: str, text: str) -> str | None:
    hit = worker._leaks_in_added_text(path, text)
    return None if hit is None else hit.rule


EVERYWHERE = ["src/app.py", "tests/unit/test_masking.py", "tests/fixtures/data.txt"]


@pytest.mark.parametrize("path", EVERYWHERE)
def test_a_registered_secret_is_refused_in_every_path(worker_factory, path):
    worker, _, _ = worker_factory()
    secret = _shape("retained-", "name")  # low entropy: only the registration catches it
    worker.log.register_secret(secret)
    assert _rule_for(worker, path, f'extra = {{"value": "{secret}"}}\n') == "registered_secret"


@pytest.mark.parametrize("path", EVERYWHERE)
@pytest.mark.parametrize(
    ("make", "rule"),
    [
        (lambda: _shape("gh", "p_", _random_token(36)), "github_token"),
        (lambda: _shape("AK", "IA", _random_token(16, string.ascii_uppercase + string.digits)),
         "aws_access_key_id"),
        (_jwt, "jwt"),
        (lambda: "\n".join(_pem_lines(400)), "private_key_block"),
    ],
    ids=["github-token", "aws-key", "decodable-jwt", "private-key-real-body"],
)
def test_a_tier_one_credential_is_refused_in_every_path(worker_factory, path, make, rule):
    worker, _, _ = worker_factory()
    value = make()
    assert _rule_for(worker, path, f"value = {value!r}\n") == rule


def test_a_high_entropy_token_under_tests_is_refused(worker_factory):
    worker, _, _ = worker_factory()
    text = f'payload = {{"token": "{_random_token(32)}"}}\n'
    assert _rule_for(worker, "tests/unit/test_api.py", text) == "key_value_assignment"


def test_anything_in_an_env_file_under_tests_is_judged_as_outside_tests(worker_factory):
    """`.env*` is never a test path, so its generic matches refuse as in src."""
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "tests/fixtures/.env", 'API_TOKEN="hunter2"\n') == "key_value_assignment"
    low_entropy = "DB_PASSWORD=p4ssw0rd-p4ssw0rd\n"
    assert _rule_for(worker, "tests/fixtures/.env", low_entropy) == "key_value_assignment"
    assert _rule_for(worker, "tests/fixtures/settings.py", low_entropy) is None


@pytest.mark.parametrize(
    "text",
    ['config = {"secret": "retained"}\n', 'login(password="hunter2")\n', "password = 'hunter2'\n"],
    ids=["json-secret", "kwarg-password", "assigned-password"],
)
def test_a_generic_literal_outside_tests_is_refused_as_before(worker_factory, text):
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "src/app.py", text) == "key_value_assignment"


#: Allowed in a test path, each refused outside one (the paired assertion).
TEST_ONLY_ALLOWED = [
    'assert masked == {"secret": "retained"}\n',
    'assert redact(\'{"token": "x"}\') == \'{"token": "********"}\'\n',
    "env = 'DB_PASSWORD=supersecretpassword'\n",
    'env = "DB_PASSWORD=p4ssw0rd-p4ssw0rd"\n',
    'login(password="hunter2")\n',
    # The four shapes that blocked real runs: #376's `extra=` registration,
    # #379's keyword argument, and #327's masked-JSON assertions.
    'log.info("resolved", extra={"secret": "tenant-git-token", "token": "anthropic-api-key"})\n',
    'resolve(secret="swarm-tenant-acme-git")\n',
    "assert masked == '{\"password\": \"********\"}'\n",
    'assert body["password"] == "********"\n',
]


@pytest.mark.parametrize("text", TEST_ONLY_ALLOWED)
def test_a_generic_match_in_a_test_path_passes_unless_credential_shaped(worker_factory, text):
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "tests/unit/control_plane/test_json_artifact_masking.py", text) is None


@pytest.mark.parametrize(
    "text",
    [
        'assert masked == {"secret": "retained"}\n',
        'login(password="hunter2")\n',
        'resolve(secret="swarm-tenant-acme-git")\n',
    ],
)
def test_the_same_generic_match_outside_tests_is_refused(worker_factory, text):
    """The control for the test above: the path alone made the difference."""
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "apps/swarm-api/swarm_api/routes.py", text) == "key_value_assignment"


@pytest.mark.parametrize(
    "text",
    [
        _shape("key = '", "sk-", "test-aaaa", "'\n"),
        _shape("key = '", "AK", "IA", "IOSFODNN7", "EXAMPLE", "'\n"),
        _shape("key = '", "gh", "p_", "x" * 36, "'\n"),
    ],
    ids=["sk-test", "aws-documentation-example", "github-placeholder"],
)
def test_a_vendor_fixture_with_a_placeholder_tail_passes_in_tests_only(worker_factory, text):
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "tests/unit/test_rules.py", text) is None
    assert _rule_for(worker, "src/rules.py", text) is not None


@pytest.mark.parametrize("path", EVERYWHERE)
def test_a_private_key_marker_over_a_stub_body_passes(worker_factory, path):
    worker, _, _ = worker_factory()
    text = "\n".join([_pem_marker("BEGIN"), "MIIEowIBAAKCAQEA...", _pem_marker("END")]) + "\n"
    assert _rule_for(worker, path, text) is None
    assert _rule_for(worker, path, _pem_marker("BEGIN") + "\n") is None


def _der_key_body(prefix_hex: str, random_len: int) -> str:
    """A PKCS#8-shaped body: a fixed DER prefix plus random bytes, base64.
    Built at run time, so no key-shaped literal sits in the repository."""
    return base64.b64encode(bytes.fromhex(prefix_hex) + _RNG.randbytes(random_len)).decode()


def _pkcs8(body: str, kind: str = "") -> str:
    return "\n".join(
        [_shape("-----", "BEGIN", kind, " PRIVATE", " KEY", "-----"), body,
         _shape("-----", "END", kind, " PRIVATE", " KEY", "-----")]
    ) + "\n"


def _ed25519_body() -> str:
    # 302e020100300506032b657004220420 + 32 random bytes: a 64-character body.
    body = _der_key_body("302e020100300506032b657004220420", 32)
    assert len(body) == 64
    return body


@pytest.mark.parametrize("path", ["src/app.py", "tests/unit/test_x.py"])
def test_a_short_pkcs8_ed25519_key_is_refused_in_every_path(worker_factory, path):
    """BLOCKER: a 64-character body sat under the 100-character floor."""
    worker, _, _ = worker_factory()
    assert _rule_for(worker, path, _pkcs8(_ed25519_body())) == "private_key_block"


@pytest.mark.parametrize("path", ["src/app.py", "tests/unit/test_x.py"])
def test_a_json_escaped_short_key_is_refused(worker_factory, path):
    worker, _, _ = worker_factory()
    escaped = json.dumps(_pkcs8(_ed25519_body()))
    assert _rule_for(worker, path, f"data = {escaped}\n") == "private_key_block"


def test_an_rsa_2048_key_split_into_short_quoted_pieces_is_refused_from_src(worker_factory):
    """MAJOR: no piece reached 16 characters, so no run counted."""
    worker, _, _ = worker_factory()
    body = "".join(_RNG.choice(_ALNUM + "+/") for _ in range(1600))
    pieces = [body[i : i + 15] for i in range(0, len(body), 15)]
    lines = [_shape('    "', piece, '" +') for piece in pieces]
    text = "\n".join(
        ["KEY = (", f'    "{_pem_marker("BEGIN")}\\n" +', *lines, f'    "{_pem_marker("END")}"', ")"]
    ) + "\n"
    assert _rule_for(worker, "src/keys.py", text) == "private_key_block"


@pytest.mark.parametrize("kind", ["", " EC", " RSA"])
def test_a_der_shaped_key_of_any_size_is_refused(worker_factory, kind):
    worker, _, _ = worker_factory()
    # SEQUENCE, long-form length 0x0140 = 320, then 320 random bytes.
    body = _der_key_body("30820140", 320)
    assert _rule_for(worker, "src/k.py", _pkcs8(body, kind)) == "private_key_block"


def test_a_sixteen_character_stub_between_markers_under_tests_passes(worker_factory):
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "tests/unit/test_x.py", _pkcs8("MIIEowIBAAKCAQEA")) is None


@pytest.mark.parametrize("path", EVERYWHERE)
def test_an_ey_run_that_is_not_a_jwt_passes(worker_factory, path):
    """`ey` and eight characters is the read-time rule's JWT; a header that
    does not decode to JSON naming `alg` is not one at publish."""
    worker, _, _ = worker_factory()
    assert _rule_for(worker, path, _shape("cursor = 'ey", "abcdefghijKLMNOP", "'\n")) is None


@pytest.mark.parametrize(
    ("value", "shaped"),
    [
        (_random_token(16), True),
        (_random_token(32), True),
        (_random_token(15), False),  # too short
        ("abcdefghijklmnopqrstuvwxyz", False),  # no digit
        ("12345678901234567890", False),  # no letter
        ("aaaaaaaa11111111", False),  # low entropy
        ("p4ssw0rd-p4ssw0rd", False),  # low entropy
        ("********", False),
        (_shape("xxxx", _random_token(16)), False),
        (_shape("<", _random_token(16), ">"), False),
        (_shape("${", _random_token(16), "}"), False),
        (_shape("fake", _random_token(16)), False),
        (_shape(_random_token(16), "Test"), False),
        (_shape("dummy", _random_token(16)), False),
        (_shape(_random_token(16), "EXAMPLE"), False),
    ],
)
def test_what_counts_as_credential_shaped(value, shaped):
    assert lifecycle._looks_like_a_credential(value) is shaped


def test_every_generic_rule_the_guard_names_is_a_redaction_rule():
    """A rename in `swarm_redaction.RULES` would silently move a generic rule
    into the vendor tier; this names the drift instead."""
    names = {rule.name for rule in lifecycle.CREDENTIAL_RULES}
    assert lifecycle.GENERIC_CREDENTIAL_RULES <= names
    assert {"jwt", "private_key_block"} <= names


def test_a_refusal_names_the_rule_and_the_line_and_never_the_value():
    """The line is the file's own: the hunk starts at line 41, and the token
    is the hunk's third added line."""
    token = _random_token(32)
    diff = (
        "diff --git a/tests/unit/test_api.py b/tests/unit/test_api.py\n"
        "--- a/tests/unit/test_api.py\n+++ b/tests/unit/test_api.py\n"
        "@@ -1,0 +2 @@\n+import json\n"
        "@@ -40,0 +41,3 @@\n"
        '+def test_x():\n+    masked = {"password": "********"}\n'
        f'+    sent = {{"token": "{token}"}}\n'
    ).encode()
    hit = _scan_hit(diff, window=1 << 20, overlap=64, piece=7)
    assert hit == ("tests/unit/test_api.py", "key_value_assignment", 43)
    assert token not in repr(hit)


def test_the_per_commit_scan_reads_a_test_files_path():
    """`_adds_a_credential` judges each file by its own path: the same line
    passes in a test file and is caught in source."""
    line = 'assert masked == {"secret": "retained"}'
    in_tests = f"+++ b/tests/unit/test_mask.py\n@@ -0,0 +1 @@\n+{line}\n"
    in_src = f"+++ b/src/mask.py\n@@ -0,0 +1 @@\n+{line}\n"
    assert lifecycle._adds_a_credential(in_tests) is False
    assert lifecycle._adds_a_credential(in_src) is True


def _akia() -> str:
    """A real-shaped AWS access key id: AKIA and 16 random upper/digits, never
    carrying a placeholder word by accident."""
    while True:
        tail = "".join(_RNG.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        if not lifecycle._PLACEHOLDER.search(tail):
            return _shape("AK", "IA", tail)


def test_no_real_shaped_aws_key_passes_under_tests():
    """The reviewer measured 22.15% passing the entropy/digit judgement."""
    from agent_worker.lifecycle import _credential_in

    refused = sum(
        _credential_in("tests/test_x.py", f"AWS_KEY = {_akia()!r}\n") is not None
        for _ in range(200)
    )
    assert refused == 200


@pytest.mark.parametrize(
    "text",
    [
        _shape("key = '", "AK", "IA", "IOSFODNN7", "EXAMPLE", "'\n"),
        _shape("key = '", "gh", "p_", "x" * 36, "'\n"),
        _shape("key = '", "AK", "IA", "A" * 16, "'\n"),
    ],
    ids=["aws-example", "github-xxxx", "repeated-character"],
)
def test_an_explicit_placeholder_vendor_token_passes_under_tests(worker_factory, text):
    worker, _, _ = worker_factory()
    assert _rule_for(worker, "tests/test_x.py", text) is None
    assert _rule_for(worker, "src/app.py", text) is not None


def test_a_placeholder_token_in_a_config_file_under_tests_is_refused(worker_factory):
    worker, _, _ = worker_factory()
    text = _shape("key: ", "AK", "IA", "IOSFODNN7", "EXAMPLE", "\n")
    assert _rule_for(worker, "tests/secrets.yaml", text) is not None
    assert _rule_for(worker, "tests/test_x.py", text) is None


# -- a key whose body sits outside the block, or whose lines carry a prefix (#411)
#
# The guard once judged only the text between BEGIN and END. Main refused any
# marker; the tiered guard let a key through when its body was held elsewhere
# (an f-string, `%`, a variable) or when its lines carried a log prefix.

OUTSIDE_PATHS = ["src/app.py", "tests/test_x.py"]
LOG_PATHS = ["src/app.py", "tests/test_x.py", "tests/fixtures/run.log"]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _rsa_der() -> bytes:
    # SEQUENCE, long-form length, INTEGER 0, then random bytes: PKCS#1-shaped.
    inner = bytes.fromhex("020100") + _RNG.randbytes(600)
    return bytes.fromhex("3082") + len(inner).to_bytes(2, "big") + inner


def _ed25519_der() -> bytes:
    return bytes.fromhex("302e020100300506032b657004220420") + _RNG.randbytes(32)


def _openssh_body() -> bytes:
    return b"openssh-key-v1\x00" + _RNG.randbytes(250)


KEY_BODIES = {"rsa": _rsa_der, "ed25519": _ed25519_der, "openssh": _openssh_body}


def _begin(kind: str = "") -> str:
    return _shape("-----", "BEGIN", kind, " PRIVATE", " KEY", "-----")


def _end(kind: str = "") -> str:
    return _shape("-----", "END", kind, " PRIVATE", " KEY", "-----")


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(KEY_BODIES))
def test_a_key_assembled_by_an_f_string_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(KEY_BODIES[which]())
    text = (f'BODY = "{body}"\n'
            f'K = f"{_begin()}\\n{{BODY}}\\n{_end()}"\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(KEY_BODIES))
def test_a_key_assembled_by_percent_with_the_pieces_after_end_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(KEY_BODIES[which]())
    text = (f'key_b64 = "{body}"\n'
            f'K = "{_begin(" RSA")}\\n%s\\n{_end(" RSA")}" % key_b64\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(KEY_BODIES))
def test_a_body_in_a_separate_private_key_b64_variable_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(KEY_BODIES[which]())
    text = (f'private_key_b64 = "{body}"\n'
            f'HEAD = "{_begin()}"\nTAIL = "{_end()}"\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", LOG_PATHS)
@pytest.mark.parametrize("which", sorted(KEY_BODIES))
def test_a_log_prefixed_key_with_no_end_marker_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(KEY_BODIES[which]())
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    out = [f"2026-09-30T12:00:00Z INFO {_begin(' RSA')}"]
    out += [f"2026-09-30T12:00:{i + 1:02d}Z INFO {line}" for i, line in enumerate(lines)]
    assert _rule_for(worker, path, "\n".join(out) + "\n") == "private_key_block"


def test_a_stub_marker_and_an_unrelated_non_der_blob_in_a_test_file_passes(worker_factory):
    worker, _, _ = worker_factory()
    blob = "".join(_RNG.choice(_ALNUM + "+/") for _ in range(200))
    # Start the blob so it cannot decode to a SEQUENCE.
    blob = "A" + blob[1:]
    text = f'{_pkcs8("MIIEowIBAAKCAQEA")}\nBLOB = "{blob}"\n'
    assert _rule_for(worker, "tests/test_x.py", text) is None


def test_a_stub_marker_and_a_certificate_shaped_der_in_a_test_file_passes(worker_factory):
    worker, _, _ = worker_factory()
    # SEQUENCE { SEQUENCE {...} ...}: first element is a SEQUENCE, not INTEGER 0/1.
    inner = bytes.fromhex("30820100") + _RNG.randbytes(256) + _RNG.randbytes(300)
    cert = bytes.fromhex("3082") + len(inner).to_bytes(2, "big") + inner
    text = f'{_pkcs8("MIIEowIBAAKCAQEA")}\nCERT = "{_b64(cert)}"\n'
    assert _rule_for(worker, "tests/test_x.py", text) is None


# --- #411 third review: encrypted keys and an orphan END ---------------------
#
# An encrypted PKCS#8 opens SEQUENCE { SEQUENCE { OID PBES2 ... } OCTET STRING },
# so its first element is a SEQUENCE and the INTEGER 0/1 shape check missed it.
# A traditional encrypted PEM carries `Proc-Type` / `DEK-Info` headers and then
# raw ciphertext after a blank line. Both are built at run time.


def _der(tag: int, payload: bytes) -> bytes:
    return bytes([tag, 0x82]) + len(payload).to_bytes(2, "big") + payload


def _encrypted_pkcs8(oid_hex: str = "06092a864886f70d01050d") -> bytes:
    # PBES2 by default; 06092a864886f70d010503 is PBES1, ...010c0103 is PKCS#12.
    alg = _der(0x30, bytes.fromhex(oid_hex) + _RNG.randbytes(40))
    return _der(0x30, alg + _der(0x04, _RNG.randbytes(300)))


ENCRYPTED_BODIES = {
    "pbes2": lambda: _encrypted_pkcs8(),
    "pbes1": lambda: _encrypted_pkcs8("06092a864886f70d010503"),
    "pkcs12": lambda: _encrypted_pkcs8("060a2a864886f70d010c0103"),
}


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(ENCRYPTED_BODIES))
def test_an_encrypted_pkcs8_in_an_f_string_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(ENCRYPTED_BODIES[which]())
    text = (f'BODY = "{body}"\n'
            f'K = f"{_begin(" ENCRYPTED")}\\n{{BODY}}\\n{_end(" ENCRYPTED")}"\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(ENCRYPTED_BODIES))
def test_an_encrypted_pkcs8_by_percent_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(ENCRYPTED_BODIES[which]())
    text = (f'key_b64 = "{body}"\n'
            f'K = "{_begin(" ENCRYPTED")}\\n%s\\n{_end(" ENCRYPTED")}" % key_b64\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(ENCRYPTED_BODIES))
def test_an_encrypted_pkcs8_in_a_private_key_b64_variable_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(ENCRYPTED_BODIES[which]())
    text = (f'private_key_b64 = "{body}"\n'
            f'HEAD = "{_begin(" ENCRYPTED")}"\nTAIL = "{_end(" ENCRYPTED")}"\n')
    assert _rule_for(worker, path, text) == "private_key_block"


def _traditional_encrypted_pem(with_end: bool) -> str:
    cipher = _b64(_RNG.randbytes(96))
    lines = [cipher[i : i + 64] for i in range(0, len(cipher), 64)]
    out = [_begin(" RSA"), "Proc-Type: 4,ENCRYPTED",
           "DEK-Info: AES-128-CBC," + _RNG.randbytes(16).hex().upper(), "", *lines]
    if with_end:
        out.append(_end(" RSA"))
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("with_end", [True, False])
def test_a_traditional_encrypted_pem_is_refused(worker_factory, path, with_end):
    worker, _, _ = worker_factory()
    assert _rule_for(worker, path, _traditional_encrypted_pem(with_end)) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
def test_a_marker_followed_by_dek_info_alone_is_refused(worker_factory, path):
    worker, _, _ = worker_factory()
    text = f"{_begin(' RSA')}\nDEK-Info: AES-256-CBC,{_RNG.randbytes(16).hex()}\n"
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
@pytest.mark.parametrize("which", sorted(KEY_BODIES))
def test_an_orphan_end_with_a_split_begin_is_refused(worker_factory, path, which):
    worker, _, _ = worker_factory()
    body = _b64(KEY_BODIES[which]())
    text = (f'HEAD = "-----BEGIN" + " RSA PRIVATE" + " KEY-----"\n'
            f'BODY = "{body}"\n'
            f'TAIL = "{_end(" RSA")}"\n')
    assert _rule_for(worker, path, text) == "private_key_block"


def test_a_stub_marker_and_a_certificate_with_a_context_sequence_in_tests_passes(worker_factory):
    worker, _, _ = worker_factory()
    # Certificate: SEQUENCE { SEQUENCE { [0] { INTEGER 2 } ... } ... }, `a0 03`.
    tbs = _der(0x30, bytes.fromhex("a003020102") + _RNG.randbytes(300))
    cert = _der(0x30, tbs + _RNG.randbytes(100))
    text = f'{_pkcs8("MIIEowIBAAKCAQEA")}\nCERT = "{_b64(cert)}"\n'
    assert _rule_for(worker, "tests/test_x.py", text) is None


# --- #411 fourth review: headers and ciphertext away from the markers --------
#
# A traditional encrypted PEM's headers may sit in a variable defined before
# the BEGIN, or further than the window after it; an orphan END may have only
# its ciphertext lines above it. Main refuses all of these.


def _trad_parts() -> tuple[str, str]:
    hdr = "Proc-Type: 4,ENCRYPTED\\nDEK-Info: AES-128-CBC," + _RNG.randbytes(16).hex().upper() + "\\n\\n"
    return hdr, _b64(_RNG.randbytes(300))


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
def test_a_traditional_encrypted_pem_with_headers_defined_before_it_is_refused(worker_factory, path):
    worker, _, _ = worker_factory()
    hdr, body = _trad_parts()
    text = (f'HDR = "{hdr}"\nBODY = "{body}"\n'
            f'K = f"{_begin(" RSA")}\\n{{HDR}}{{BODY}}\\n{_end(" RSA")}"\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
def test_a_traditional_encrypted_pem_by_percent_is_refused(worker_factory, path):
    worker, _, _ = worker_factory()
    hdr, body = _trad_parts()
    text = (f'HDR = "{hdr}"\nBODY = "{body}"\n'
            f'K = "{_begin(" RSA")}\\n%s%s\\n{_end(" RSA")}" % (HDR, BODY)\n')
    assert _rule_for(worker, path, text) == "private_key_block"


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
def test_a_traditional_encrypted_pem_with_headers_far_after_it_is_refused(worker_factory, path):
    worker, _, _ = worker_factory()
    hdr, body = _trad_parts()
    filler = "".join(f"# filler line number {i} of padding text\n" for i in range(200))
    text = (f'K = f"{_begin(" RSA")}\\n{{HDR}}{{BODY}}\\n{_end(" RSA")}"\n'
            f'{filler}HDR = "{hdr}"\nBODY = "{body}"\n')
    assert len(filler) > 4096
    assert _rule_for(worker, path, text) == "private_key_block"


def _cipher_lines(raw_len: int = 300) -> list[str]:
    cipher = _b64(_RNG.randbytes(raw_len))
    return [cipher[i : i + 64] for i in range(0, len(cipher), 64)]


@pytest.mark.parametrize("path", OUTSIDE_PATHS)
def test_an_orphan_end_with_ciphertext_lines_above_it_is_refused(worker_factory, path):
    worker, _, _ = worker_factory()
    text = "\n".join([*_cipher_lines(), _end(" RSA")]) + "\n"
    assert _rule_for(worker, path, text) == "private_key_block"


def test_a_stub_marker_and_a_certificate_blob_in_tests_passes(worker_factory):
    worker, _, _ = worker_factory()
    cert = _der(0x30, _der(0x30, bytes.fromhex("a003020102") + _RNG.randbytes(300)) + _RNG.randbytes(100))
    text = f'{_pkcs8("MIIEowIBAAKCAQEA")}\nCERT = "{_b64(cert)}"\n'
    assert _rule_for(worker, "tests/test_x.py", text) is None


def test_a_stub_marker_and_a_random_blob_in_a_quoted_variable_in_tests_passes(worker_factory):
    worker, _, _ = worker_factory()
    text = f'{_pkcs8("MIIEowIBAAKCAQEA")}\nBLOB = "{_b64(_RNG.randbytes(500))}"\n'
    assert _rule_for(worker, "tests/test_x.py", text) is None


def test_an_orphan_end_preceded_by_a_non_base64_line_in_tests_passes(worker_factory):
    worker, _, _ = worker_factory()
    text = "\n".join([*_cipher_lines(), "# not part of any key", _end(" RSA")]) + "\n"
    assert _rule_for(worker, "tests/test_x.py", text) is None
