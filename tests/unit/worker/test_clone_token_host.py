"""The tenant's forge token goes to the forge it was issued for, and nowhere else (#307).

A task's `repository_url` is a claim made by whoever submitted the task. Before
this, `gitops._write_credentials` stored the tenant token for whatever host
that URL named, so `https://<a host the submitter controls>/o/r` answered with
a 401 was enough for git to hand the token over. The rule is now the one the
issue fetch (#270) already used: the token is written only for a host in
`forge.GITHUB_HOSTS`, decided in one place (`forge.may_receive_forge_token`).
Any other host is cloned without a credential, as a public read, and a clone
that fails there says why.

Three things are pinned, each against what git is actually handed:

* a clone of another https host gets no credential -- not in argv, not in the
  environment, not in a credential file that exists while git runs;
* a clone of github.com still gets it, bound to github.com and nothing else;
* a redirect to another host does not receive it: git re-asks its credential
  helper for the host it was redirected to, and the helper the worker
  configures has no entry for any host but the forge's (checked with real git's
  own credential lookup).

The fake git below is a real executable: `shallow_clone` runs it exactly as it
would run git, and it records what it was given.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker import forge, gitops
from agent_worker.forge import RepoAccess, RepoRef
from agent_worker.gitops import GitError
from agent_worker.logs import build_logger


def _token() -> str:
    # Built at runtime: nothing token-shaped is a literal in this file.
    return "tok-" + secrets.token_hex(20)


def _logger(stream=None):
    return build_logger(
        task_id="task_1",
        attempt_id="att_1",
        tenant_id="eng",
        generation=1,
        runner_profile="mock",
        stream=stream if stream is not None else io.StringIO(),
    )


_FAKE_GIT = """\
#!{python}
import json, os, sys
record = {{"argv": sys.argv[1:], "env": dict(os.environ), "credential_files": {{}}}}
for arg in sys.argv[1:]:
    if arg.startswith("credential.helper=store --file="):
        path = arg.split("--file=", 1)[1]
        try:
            with open(path) as handle:
                record["credential_files"][path] = handle.read()
        except OSError as exc:
            record["credential_files"][path] = "<unreadable: %s>" % exc
with open({record!r}, "a") as out:
    out.write(json.dumps(record) + "\\n")
sys.exit({exit_code})
"""


def _fake_git(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    record = tmp_path / "git-calls.jsonl"
    script = tmp_path / "fake-git"
    script.write_text(
        _FAKE_GIT.format(python=sys.executable, record=str(record), exit_code=exit_code)
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, record


def _calls(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


def _clone(tmp_path: Path, url: str, token: str, *, exit_code: int = 0) -> list[dict]:
    git, record = _fake_git(tmp_path, exit_code=exit_code)
    private = tmp_path / "private"
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    try:
        gitops.shallow_clone(
            url=url,
            ref="main",
            destination=tmp_path / "repo",
            private_dir=private,
            logs_dir=logs,
            timeout_seconds=30,
            logger=_logger(),
            token=token,
            git_binary=str(git),
        )
    finally:
        # Whatever happened, the credential file is gone afterwards.
        assert not (private / ".git-credentials").exists()
    return _calls(record)


def _everything_git_saw(call: dict) -> str:
    return json.dumps(call)


# -- the clone ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/o/r.git",
        # Look-alikes: a suffix, a subdomain, the name in the path, the name as
        # userinfo, and the real name on a port github.com does not serve git on.
        "https://github.com.evil.example/o/r.git",
        "https://gist.github.com.evil.example/o/r.git",
        "https://evil.example/github.com/r.git",
        "https://github.com@evil.example/o/r.git",
        "https://github.com:8443/o/r.git",
        # A GitHub Enterprise host: not one this worker holds a token for.
        "https://ghe.example.com/o/r.git",
    ],
)
def test_a_clone_of_another_https_host_is_handed_no_credential(tmp_path, url):
    token = _token()
    calls = _clone(tmp_path, url, token)
    assert calls, "the fake git never ran, so nothing was checked"
    for call in calls:
        seen = _everything_git_saw(call)
        assert token not in seen
        assert not any(a.startswith("credential.helper=store") for a in call["argv"])
        assert call["credential_files"] == {}
    assert not (tmp_path / "private" / ".git-credentials").exists()


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r.git", "https://GitHub.com/o/r", "https://www.github.com/o/r"]
)
def test_a_clone_of_github_still_gets_the_token_bound_to_github_alone(tmp_path, url):
    token = _token()
    calls = _clone(tmp_path, url, token)
    clones = [c for c in calls if "clone" in c["argv"]]
    assert clones, "the clone itself never ran"
    for call in clones:
        # Never in argv (world-readable through /proc) nor the environment.
        assert token not in json.dumps(call["argv"])
        assert token not in json.dumps(call["env"])
        files = call["credential_files"]
        assert len(files) == 1
        (body,) = files.values()
        lines = [line for line in body.splitlines() if line]
        assert len(lines) == 1
        # The host exactly as the URL spells it: git's credential lookup is
        # case-sensitive, so a lower-cased entry would not match "GitHub.com".
        host = gitops.urlparse(url).netloc
        assert lines[0] == f"https://x-access-token:{token}@{host}"


def test_a_clone_over_ssh_never_writes_the_https_token(tmp_path):
    token = _token()
    calls = _clone(tmp_path, "ssh://git@github.com/o/r.git", token)
    assert calls
    for call in calls:
        assert token not in _everything_git_saw(call)
        assert call["credential_files"] == {}


def test_a_sha_clone_of_another_host_is_handed_no_credential_on_any_step(tmp_path):
    token = _token()
    git, record = _fake_git(tmp_path)
    gitops.shallow_clone(
        url="https://evil.example/o/r.git",
        ref="0123456789abcdef0123456789abcdef01234567",
        destination=tmp_path / "repo",
        private_dir=tmp_path / "private",
        logs_dir=tmp_path,
        timeout_seconds=30,
        logger=_logger(),
        token=token,
        git_binary=str(git),
    )
    calls = _calls(record)
    assert len(calls) >= 3, "init, remote add, fetch and checkout should all have run"
    for call in calls:
        assert token not in _everything_git_saw(call)


def test_a_failed_clone_of_another_host_says_the_token_was_withheld(tmp_path):
    """The issue's "what you expected": a clone that fails because it ran
    without the tenant's credential gets a clear error, naming the host and
    the rule -- and never the token."""
    token = _token()
    with pytest.raises(GitError) as raised:
        _clone(tmp_path, "https://evil.example/o/r.git", token, exit_code=128)
    message = str(raised.value)
    assert "evil.example" in message
    assert "without the tenant's git credential" in message
    assert token not in message


def test_a_failed_clone_of_github_does_not_claim_the_token_was_withheld(tmp_path):
    with pytest.raises(GitError) as raised:
        _clone(tmp_path, "https://github.com/o/r.git", _token(), exit_code=128)
    assert "without the tenant's git credential" not in str(raised.value)


def test_withholding_the_token_is_logged_without_the_token(tmp_path):
    token = _token()
    stream = io.StringIO()
    git, _ = _fake_git(tmp_path)
    gitops.shallow_clone(
        url="https://evil.example/o/r.git",
        ref=None,
        destination=tmp_path / "repo",
        private_dir=tmp_path / "private",
        logs_dir=tmp_path,
        timeout_seconds=30,
        logger=_logger(stream),
        token=token,
        git_binary=str(git),
    )
    out = stream.getvalue()
    assert "withheld" in out and "evil.example" in out
    assert token not in out


# -- the one rule ---------------------------------------------------------------


def test_write_credentials_writes_nothing_for_a_host_outside_the_rule(tmp_path):
    assert gitops._write_credentials("https://evil.example/o/r", _token(), tmp_path) is None
    assert not (tmp_path / ".git-credentials").exists()


def test_the_clone_and_the_issue_fetch_share_one_host_rule():
    """#307's "one rule, stated in one place": the credential decision reads
    the same set the issue fetch reads, through one function."""
    for host in forge.GITHUB_HOSTS:
        assert forge.may_receive_forge_token(host)
        assert forge.may_receive_forge_token(host.upper())
    for host in ("evil.example", "github.com.evil.example", "api.github.com.evil", "", None):
        assert not forge.may_receive_forge_token(host)


# -- a redirect to another host -------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_redirect_to_another_host_is_not_handed_the_token(tmp_path):
    """On a redirect git re-reads its credential for the host it was sent to
    (`http_request_reauth` -> `credential_from_url(base_url)`), then asks the
    configured helpers. So what reaches a redirect target is exactly what the
    worker's helper answers for that host. Asked through real git, with the
    file and helper the clone uses: the forge gets the token; another host,
    including a look-alike, gets nothing."""
    token = _token()
    cred = gitops._write_credentials("https://github.com/o/r.git", token, tmp_path / "private")
    assert cred is not None
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def fill(host: str) -> str:
        done = subprocess.run(
            ["git", "-c", "credential.helper=", "-c", f"credential.helper=store --file={cred}",
             "credential", "fill"],
            input=f"protocol=https\nhost={host}\npath=o/r.git\n\n",
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
            timeout=30,
        )
        return done.stdout + done.stderr

    assert f"password={token}" in fill("github.com"), "control: the forge must get it"
    for other in ("evil.example", "github.com.evil.example", "api.github.com"):
        assert token not in fill(other)


# -- the publish path uses the same rule ---------------------------------------


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "publish"
    repo.mkdir()
    return repo


def test_a_push_to_another_host_is_handed_no_credential(tmp_path, monkeypatch):
    token = _token()
    git, record = _fake_git(tmp_path)
    monkeypatch.setattr(gitops, "_git_text", _recording_git_text(record))
    gitops.push_branch(
        repo=_git_repo(tmp_path),
        url="https://evil.example/o/r.git",
        branch="swarm/t-1",
        token=token,
        private_dir=tmp_path / "private",
        logs_dir=tmp_path,
        timeout_seconds=30,
        logger=_logger(),
        git_binary=str(git),
    )
    calls = _calls(record)
    assert calls
    for call in calls:
        assert token not in _everything_git_saw(call)
        assert not any(a.startswith("credential.helper=store") for a in call["argv"])


def test_a_push_to_github_still_carries_the_credential(tmp_path, monkeypatch):
    token = _token()
    git, record = _fake_git(tmp_path)
    monkeypatch.setattr(gitops, "_git_text", _recording_git_text(record))
    gitops.push_branch(
        repo=_git_repo(tmp_path),
        url="https://github.com/o/r.git",
        branch="swarm/t-1",
        token=token,
        private_dir=tmp_path / "private",
        logs_dir=tmp_path,
        timeout_seconds=30,
        logger=_logger(),
        git_binary=str(git),
    )
    push = [c for c in _calls(record) if "push" in c["argv"]]
    assert push
    (body,) = push[0]["credential_files"].values()
    assert body.strip() == f"https://x-access-token:{token}@github.com"


def test_an_integrator_fetch_from_another_host_is_handed_no_credential(tmp_path, monkeypatch):
    token = _token()
    git, record = _fake_git(tmp_path)
    monkeypatch.setattr(gitops, "_git_text", _recording_git_text(record))
    gitops.merge_branches(
        repo=_git_repo(tmp_path),
        url="https://evil.example/o/r.git",
        branches=["swarm/t-a"],
        token=token,
        private_dir=tmp_path / "private",
        logs_dir=tmp_path,
        timeout_seconds=30,
        logger=_logger(),
        author_name="swarm",
        author_email="swarm@example.invalid",
        git_binary=str(git),
    )
    calls = _calls(record)
    assert calls
    for call in calls:
        assert token not in _everything_git_saw(call)


def _recording_git_text(record: Path):
    """Run the argv `_git_text` is given through the fake git, synchronously,
    so the credential file is read while it exists."""

    def run(argv, *, repo, private_dir, logs_dir, slug, timeout_seconds, logger, **_):
        done = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, timeout=30)
        return done.returncode, "0" * 40 if "rev-parse" in argv else ""

    return run


def test_the_forge_api_is_not_handed_the_token_for_another_host(monkeypatch):
    """`probe_repository` would otherwise send the token as a bearer header to
    `https://<any host>/api/v3`, ahead of any push."""
    calls = []

    def fake(url, *, token, method="GET", payload=None):
        calls.append(token)
        return 200, {"permissions": {"push": True}, "default_branch": "main"}

    monkeypatch.setattr(forge, "_request", fake)
    token = _token()
    access = forge.probe_repository(url="https://evil.example/o/r.git", token=token)
    assert token not in calls
    assert access is not None and access.can_push is False
    assert "evil.example" in access.reason

    with pytest.raises(forge.ForgeError):
        forge.open_pull_request(
            access=RepoAccess(
                ref=RepoRef("evil.example", "o", "r"), default_branch="main",
                can_push=True, reason="",
            ),
            token=token, head="swarm/t-1", base="main", title="t", body="b",
        )
    assert token not in calls

    # Control: the forge itself is still asked, with the token.
    forge.probe_repository(url="https://github.com/o/r.git", token=token)
    assert calls == [token]
