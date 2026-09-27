"""Getting the agent's work back out: the harvest, and the publish gate.

Until this path existed the loop was open at the far end -- a worker cloned a
repository, the agent committed into it, the whole workspace was archived to
GCS every two minutes, and nothing ever read that archive except a retry of the
same attempt. These tests pin the two properties that make the new path safe to
leave switched on by default:

* the HARVEST needs no credential and never pushes, so it can run on every exit
  path including a crash;
* the PUBLISH is refused unless the forge itself confirms the push bit, so a
  platform whose tenants hold clone-only tokens (the state today) gets a patch
  and a stated reason rather than a failure or a silent skip.

Real git runs here. That is deliberate: the parsing this file checks is of
`git log --numstat` output, and a fixture of that output would pin what I
believe git prints rather than what it prints.
"""

from __future__ import annotations

import io
import shutil
import subprocess

import pytest

from agent_worker import forge
from agent_worker.forge import RepoAccess, RepoRef, parse_repo, probe_repository
from agent_worker.gitops import GitError, _parse_log, push_branch, commit_dirty, summarize_work
from agent_worker.logs import build_logger

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_FS = "\x1f"
_RS = "\x1e"


def _logger():
    return build_logger(
        task_id="task_1",
        attempt_id="att_1",
        tenant_id="u-test",
        generation=1,
        runner_profile="mock",
        stream=io.StringIO(),
    )


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path):
    """A repository with one commit, standing in for a fresh clone."""
    path = tmp_path / "work" / "repo"
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    (path / "README.md").write_text("one\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return path, base


@pytest.fixture()
def dirs(tmp_path):
    private = tmp_path / "private"
    logs = tmp_path / "logs"
    artifacts = tmp_path / "artifacts"
    for d in (private, logs, artifacts):
        d.mkdir(parents=True, exist_ok=True)
    return private, logs, artifacts


def _harvest(repo_path, base, dirs, **kw):
    private, logs, artifacts = dirs
    return summarize_work(
        repo=repo_path,
        base=base,
        private_dir=private,
        logs_dir=logs,
        patch_path=artifacts / "swarm-work.patch",
        max_patch_bytes=kw.pop("max_patch_bytes", 1024 * 1024),
        timeout_seconds=60,
        logger=_logger(),
        **kw,
    )


# -- the harvest -----------------------------------------------------------


def test_an_untouched_repository_harvests_as_empty(repo, dirs):
    path, base = repo
    work = _harvest(path, base, dirs)
    assert work.is_empty
    assert work.commits == ()
    assert work.dirty == ()
    assert work.patch_name is None


def test_uncommitted_edits_are_the_common_case_and_reach_the_patch(repo, dirs):
    """The behaviour that made this feature necessary.

    Most agents edit files and never run `git commit`. A harvest that only
    reported commits would report nothing for the majority of real runs, and a
    push would produce a branch identical to its base.
    """
    path, base = repo
    (path / "README.md").write_text("one\ntwo\n")
    (path / "new.py").write_text("print('hello')\n")

    work = _harvest(path, base, dirs)

    assert work.commits == ()
    assert set(work.dirty) == {"README.md", "new.py"}
    assert work.patch_name == "swarm-work.patch"
    patch = (dirs[2] / "swarm-work.patch").read_text()
    # The untracked file is the one a plain `git diff` would have missed.
    assert "new.py" in patch
    assert "README.md" in patch


def test_the_patch_covers_committed_and_uncommitted_work_together(repo, dirs):
    """One patch, not two. A reader wants "what did this agent do", and
    splitting that by whether the agent happened to commit is an artefact of
    the agent's habits rather than a property of the change."""
    path, base = repo
    (path / "a.txt").write_text("committed\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "agent commit")
    (path / "b.txt").write_text("not committed\n")

    work = _harvest(path, base, dirs)

    assert len(work.commits) == 1
    assert work.commits[0].subject == "agent commit"
    assert "b.txt" in work.dirty
    patch = (dirs[2] / "swarm-work.patch").read_text()
    assert "a.txt" in patch and "b.txt" in patch


def test_an_oversized_patch_is_discarded_not_truncated(repo, dirs):
    """A truncated patch applies cleanly and silently drops the rest of the
    change. That is strictly worse than having no patch, because it looks like
    a complete one."""
    path, base = repo
    (path / "big.txt").write_text("x" * 200_000)

    work = _harvest(path, base, dirs, max_patch_bytes=1024)

    assert work.patch_omitted is True
    assert work.patch_name is None
    assert not (dirs[2] / "swarm-work.patch").exists()
    assert work.patch_bytes > 1024


def test_no_base_still_reports_what_is_dirty(repo, dirs):
    """A resumed attempt whose clone-base marker was lost must degrade to the
    dirty list rather than raising -- knowing WHICH files changed is still most
    of the value."""
    path, _ = repo
    (path / "c.txt").write_text("hello\n")

    work = _harvest(path, None, dirs)

    assert "c.txt" in work.dirty
    assert work.patch_name is None
    assert work.is_empty is False


def test_commit_subjects_containing_newlines_do_not_split_the_record():
    """Why the parser uses \\x1f and \\x1e rather than splitting on lines.

    A commit subject cannot contain a newline, but an AUTHOR NAME can, and git
    prints both on the header line. Line-splitting would turn one commit into
    two, the second with a garbage sha.
    """
    stream = (
        f"{_RS}abc123{_FS}subject one{_FS}Real Name{_FS}2026-09-20T10:00:00+00:00\n"
        "3\t1\tsrc/a.py\n"
        f"{_RS}def456{_FS}subject two{_FS}Other\nName{_FS}2026-09-20T11:00:00+00:00\n"
        "10\t0\tsrc/b.py\n"
        "-\t-\timg.png\n"
    )
    commits, adds, dels = _parse_log(stream)

    assert [c.sha for c in commits] == ["abc123", "def456"]
    assert commits[1].binary_files == 1
    assert commits[1].files_changed == 2
    assert (adds, dels) == (13, 1)


def test_a_binary_file_is_counted_as_a_file_not_as_zero_lines():
    """git prints `-` for both counts on a binary change. Folding that into a
    0/0 line count would report an image swap as a commit that changed
    nothing."""
    commits, adds, dels = _parse_log(
        f"{_RS}aaa{_FS}art{_FS}A{_FS}2026-09-20T10:00:00+00:00\n-\t-\tlogo.png\n"
    )
    assert commits[0].files_changed == 1
    assert commits[0].binary_files == 1
    assert (adds, dels) == (0, 0)


def test_the_harvest_never_needs_a_credential(repo, dirs):
    """The property that lets harvesting default to on: it is read-only with
    respect to history and talks to no network."""
    path, base = repo
    (path / "x.txt").write_text("y\n")
    work = _harvest(path, base, dirs)
    assert work.patch_name is not None
    # No credential file was created anywhere in the worker's scratch.
    assert not list(dirs[0].rglob(".git-credentials"))


def test_harvesting_does_not_create_a_commit(repo, dirs):
    """`add --intent-to-add` touches the index and nothing else. A harvest that
    committed would change what a resumed attempt restores."""
    path, base = repo
    (path / "x.txt").write_text("y\n")
    _harvest(path, base, dirs)
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == base


# -- commit_dirty ----------------------------------------------------------


def test_commit_dirty_returns_none_when_the_agent_committed_everything(repo, dirs):
    path, _ = repo
    private, logs, _ = dirs
    assert (
        commit_dirty(
            repo=path,
            message="m",
            author_name="a",
            author_email="a@example.com",
            private_dir=private,
            logs_dir=logs,
            timeout_seconds=60,
            logger=_logger(),
        )
        is None
    )


def test_commit_dirty_captures_work_that_would_otherwise_never_reach_a_branch(repo, dirs):
    path, base = repo
    private, logs, _ = dirs
    (path / "left.txt").write_text("behind\n")

    sha = commit_dirty(
        repo=path,
        message="swarm: uncommitted changes",
        author_name="swarmcloud agent",
        author_email="a@example.com",
        private_dir=private,
        logs_dir=logs,
        timeout_seconds=60,
        logger=_logger(),
    )

    assert sha and sha != base
    listed = subprocess.run(
        ["git", "-C", str(path), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "left.txt" in listed


def test_a_repository_hook_is_never_run_by_the_harvest_or_the_commit(repo, dirs):
    """The agent can write `.git/hooks/pre-commit` in its own workspace. A
    worker-side commit that ran it would hand arbitrary code an execution point
    AFTER the runner has exited and the sandbox is meant to be finished."""
    path, _ = repo
    private, logs, _ = dirs
    hooks = path / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    marker = path.parent / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (path / "z.txt").write_text("z\n")

    commit_dirty(
        repo=path,
        message="m",
        author_name="a",
        author_email="a@example.com",
        private_dir=private,
        logs_dir=logs,
        timeout_seconds=60,
        logger=_logger(),
    )

    assert not marker.exists(), "a repository-supplied hook executed in the worker"


# -- the check before the push ---------------------------------------------
#
# The fold makes every pushed commit the worker's; `verify_worker_authorship`
# checks it, from the raw commit objects, immediately before the push. These
# pin what it refuses, one reason at a time, so a check that quietly stopped
# looking at one field would fail here rather than in production.

WORKER = ("swarmcloud agent", "a@example.com")


def _verify(path, base, dirs):
    # Imported here, not at the top, because the function arrives with the
    # change these tests are written ahead of.
    from agent_worker.gitops import verify_worker_authorship

    private, logs, _ = dirs
    return verify_worker_authorship(
        repo=path,
        base=base,
        author_name=WORKER[0],
        author_email=WORKER[1],
        private_dir=private,
        logs_dir=logs,
        timeout_seconds=60,
        logger=_logger(),
    )


def _commit(path, message, *, author=WORKER, committer=WORKER):
    subprocess.run(
        [
            "git",
            "-c", f"author.name={author[0]}", "-c", f"author.email={author[1]}",
            "-c", f"committer.name={committer[0]}", "-c", f"committer.email={committer[1]}",
            "-C", str(path), "commit", "--quiet", "--allow-empty", "-m", message,
        ],
        check=True,
        capture_output=True,
    )


def test_the_push_check_passes_a_history_the_worker_wrote(repo, dirs):
    path, base = repo
    _commit(path, "swarm: work from t-1")
    _commit(path, "swarm: integrate swarm/t-0")
    assert _verify(path, base, dirs) == 2


@pytest.mark.parametrize(
    ("author", "committer", "message", "reason"),
    [
        (("Claude", "noreply@anthropic.com"), WORKER, "swarm: work", "authored by Claude"),
        (WORKER, ("Claude", "noreply@anthropic.com"), "swarm: work", "committed by Claude"),
        (
            WORKER, WORKER,
            "swarm: work\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
            "attribution",
        ),
        (
            WORKER, WORKER,
            "swarm: work\n\nGenerated with [Claude Code](https://claude.com/claude-code)",
            "attribution",
        ),
    ],
    ids=["author", "committer", "trailer", "footer"],
)
def test_the_push_check_refuses_a_commit_the_worker_did_not_write(
    repo, dirs, author, committer, message, reason
):
    path, base = repo
    _commit(path, message, author=author, committer=committer)
    with pytest.raises(GitError, match=reason):
        _verify(path, base, dirs)


def test_the_push_check_refuses_a_signature_the_worker_never_makes(repo, dirs):
    """The worker never signs (`commit.gpgSign=false` on every call), so a
    `gpgsig` header on a commit it is about to push was written by a program
    somebody else chose -- with text in it the worker did not write."""
    path, base = repo
    _commit(path, "swarm: work")
    raw = subprocess.run(
        ["git", "-C", str(path), "cat-file", "commit", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    header, _, body = raw.partition("\n\n")
    signed = (
        header
        + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n Generated with Claude Code\n"
        + " -----END PGP SIGNATURE-----\n\n"
        + body
    )
    sha = subprocess.run(
        ["git", "-C", str(path), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=signed, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(path), "update-ref", "HEAD", sha], check=True)

    with pytest.raises(GitError, match="signed"):
        _verify(path, base, dirs)


def test_the_push_check_refuses_without_a_base(repo, dirs):
    path, _ = repo
    _commit(path, "swarm: work")
    with pytest.raises(GitError, match="clone base"):
        _verify(path, None, dirs)


# -- push refusals ---------------------------------------------------------


def _push(repo_path, dirs, branch, **kw):
    private, logs, _ = dirs
    return push_branch(
        repo=repo_path,
        url="https://github.com/acme/widgets.git",
        branch=branch,
        token="t",
        private_dir=private,
        logs_dir=logs,
        timeout_seconds=30,
        logger=_logger(),
        **kw,
    )


def test_a_branch_outside_the_prefix_is_refused_before_any_network_call(repo, dirs):
    path, _ = repo
    with pytest.raises(GitError, match="outside"):
        _push(path, dirs, "main")


def test_the_default_branch_is_refused_even_with_the_right_prefix(repo, dirs):
    """The guard that matters is not the prefix -- it is that the repository's
    OWN default branch, read back from the forge, is never a push target."""
    path, _ = repo
    with pytest.raises(GitError, match="protected"):
        _push(path, dirs, "swarm/main", protected=("swarm/main",))


def test_a_branch_name_git_would_reject_is_refused(repo, dirs):
    path, _ = repo
    with pytest.raises(GitError):
        _push(path, dirs, "swarm/../../etc/passwd")


def test_no_credential_file_survives_a_refused_push(repo, dirs):
    path, _ = repo
    with pytest.raises(GitError):
        _push(path, dirs, "nope/x")
    assert not list(dirs[0].rglob(".git-credentials"))


# -- the forge probe -------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/widgets.git", ("github.com", "acme", "widgets")),
        ("https://github.com/acme/widgets", ("github.com", "acme", "widgets")),
        ("git@github.com:acme/widgets.git", ("github.com", "acme", "widgets")),
        ("ssh://git@github.com/acme/widgets.git", ("github.com", "acme", "widgets")),
        ("https://git.corp.example/acme/widgets.git", ("git.corp.example", "acme", "widgets")),
    ],
)
def test_clone_urls_parse_into_owner_and_name(url, expected):
    ref = parse_repo(url)
    assert (ref.host, ref.owner, ref.name) == expected


def test_a_url_that_is_not_a_forge_repository_parses_to_none():
    """None is not an error. A repository on a host this worker does not
    understand harvests a patch and says so; it does not fail the attempt."""
    assert parse_repo("https://example.com/") is None
    assert parse_repo("") is None


def test_github_enterprise_uses_the_api_v3_path_not_the_api_subdomain():
    """`api.<host>` does not exist for GitHub Enterprise Server. A probe sent
    there fails DNS, which would read as "the forge is down" rather than "the
    URL was built wrong"."""
    assert RepoRef("github.com", "a", "b").api_base == "https://api.github.com"
    assert RepoRef("git.corp.example", "a", "b").api_base == "https://git.corp.example/api/v3"


def _stub(monkeypatch, status, data):
    calls = []

    def fake(url, *, token, method="GET", payload=None):
        calls.append((method, url, payload))
        return status, data

    monkeypatch.setattr(forge, "_request", fake)
    return calls


def test_push_permission_is_read_from_the_forge_not_from_the_scope_header(monkeypatch):
    """`X-OAuth-Scopes` works for classic PATs ONLY. A platform that trusted it
    would read every fine-grained token and every App installation token as
    unscoped and refuse work it was entitled to do."""
    _stub(monkeypatch, 200, {"permissions": {"push": True, "pull": True}, "default_branch": "main"})
    access = probe_repository(url="https://github.com/acme/widgets.git", token="t")
    assert access.can_push is True
    assert access.default_branch == "main"


def test_a_missing_push_key_reads_as_no_rather_than_as_unknown(monkeypatch):
    _stub(monkeypatch, 200, {"permissions": {"pull": True}, "default_branch": "main"})
    access = probe_repository(url="https://github.com/acme/widgets.git", token="t")
    assert access.can_push is False
    assert "pull" in access.reason and "not push" in access.reason


def test_a_null_push_value_does_not_sneak_through_as_truthy(monkeypatch):
    """`permissions.get("push")` returning None is falsy today and one refactor
    away from being treated as unknown-so-try-anyway. The check is `is True`."""
    _stub(monkeypatch, 200, {"permissions": {"push": None}, "default_branch": "main"})
    assert probe_repository(url="https://github.com/acme/widgets.git", token="t").can_push is False


def test_the_default_branch_is_read_back_rather_than_assumed_to_be_main(monkeypatch):
    """Plenty of repositories still default to `master`, and a guard that
    protects a branch the repository does not have protects nothing."""
    _stub(monkeypatch, 200, {"permissions": {"push": True}, "default_branch": "master"})
    access = probe_repository(url="https://github.com/acme/widgets.git", token="t")
    assert access.default_branch == "master"


def test_no_token_is_a_stated_reason_not_a_crash(monkeypatch):
    """The state of this platform today: tenants hold clone-only tokens or
    none. The operator has to be able to read WHY nothing was published."""
    access = probe_repository(url="https://github.com/acme/widgets.git", token=None)
    assert access.can_push is False
    assert "no git credential" in access.reason


@pytest.mark.parametrize("status,fragment", [(404, "cannot see"), (401, "rejected")])
def test_forge_refusals_are_reported_verbatim_enough_to_act_on(monkeypatch, status, fragment):
    _stub(monkeypatch, status, {"message": "Not Found"})
    access = probe_repository(url="https://github.com/acme/widgets.git", token="t")
    assert access.can_push is False
    assert fragment in access.reason


def test_the_probe_makes_exactly_one_request_and_it_is_a_get(monkeypatch):
    """It runs on every terminal attempt, so it must stay one call with no side
    effect -- discovering the permission by ATTEMPTING a push would leave a
    branch behind on a token that turned out to be read-only."""
    calls = _stub(monkeypatch, 200, {"permissions": {"push": False}, "default_branch": "main"})
    probe_repository(url="https://github.com/acme/widgets.git", token="t")
    assert len(calls) == 1
    assert calls[0][0] == "GET"


def test_an_existing_pull_request_is_adopted_rather_than_duplicated(monkeypatch):
    """A task retried after a park pushes the same branch again. A second pull
    request for the same work is noise a human then has to close by hand."""
    access = RepoAccess(
        ref=RepoRef("github.com", "acme", "widgets"),
        default_branch="main",
        can_push=True,
        reason="ok",
    )
    seen = []

    def fake(url, *, token, method="GET", payload=None):
        seen.append(method)
        if method == "POST":
            return 422, {"errors": [{"message": "A pull request already exists"}]}
        return 200, [{"number": 47, "html_url": "https://github.com/acme/widgets/pull/47",
                      "state": "open"}]

    monkeypatch.setattr(forge, "_request", fake)
    pr = forge.open_pull_request(
        access=access, token="t", head="swarm/task_1", base="main", title="t", body="b"
    )
    assert pr.number == 47
    assert pr.created is False
    assert seen == ["POST", "GET"]


def test_no_commits_between_base_and_head_is_surfaced_not_swallowed(monkeypatch):
    access = RepoAccess(
        ref=RepoRef("github.com", "acme", "widgets"),
        default_branch="main",
        can_push=True,
        reason="ok",
    )

    def fake(url, *, token, method="GET", payload=None):
        if method == "POST":
            return 422, {"errors": [{"message": "No commits between main and swarm/task_1"}]}
        return 200, []

    monkeypatch.setattr(forge, "_request", fake)
    with pytest.raises(forge.ForgeError, match="No commits between"):
        forge.open_pull_request(
            access=access, token="t", head="swarm/task_1", base="main", title="t", body="b"
        )
