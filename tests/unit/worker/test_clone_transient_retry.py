"""A clone or forge call that cannot reach github.com is retried, not terminal (#623).

Measured 2026-10-05 (history analysis, F3): three tasks in 24 h failed for
good on attempt 1 of 3 because github.com did not answer --
`Failed to connect to github.com port 443 after 134 s` on the clone
(task_943349914a88, task_cb020e97d585) and `could not reach api.github.com:
timed out` on the forge (task_fbd5bfd38f70). `_maybe_clone` turned every
`GitError` into a terminal `WorkerError`, and a forge network failure whose
reason was not literally "timed out" was a plain `ForgeError`.

Pinned here:

* the classifier, on the exact strings measured and on the answers that must
  stay permanent (404, refused authentication, a ref that is not there);
* the bounded in-attempt retry: a transient failure then a success clones;
  a transient failure that outlasts the tries ends the ATTEMPT retryably, so
  the task's attempt budget applies and its capacity is released;
* a permanent failure is terminal at once, with no retry and no wait.

The fake git is a real executable: it fails the network steps the way curl
fails them for the first N calls, then hands over to real git, so the clone
the worker makes after a retry is a real one.
"""

from __future__ import annotations

import io
import shutil
import socket
import ssl
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

from agent_worker import forge, gitops, lifecycle
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from conftest import seed_attempt

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: The two messages measured on 2026-10-05, as git and the forge wrote them.
CLONE_CONNECT = (
    "fatal: unable to access 'https://github.com/octo/widgets.git/': "
    "Failed to connect to github.com port 443 after 134 s: Connection timed out"
)
FORGE_TIMEOUT = "could not reach api.github.com: timed out"


def _logger(stream=None):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="eng", generation=1,
        runner_profile="mock", stream=stream if stream is not None else io.StringIO(),
    )


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        CLONE_CONNECT,
        "fatal: unable to access 'https://github.com/o/r.git/': Failed to connect to "
        "github.com port 443 after 134049 ms: Couldn't connect to server",
        FORGE_TIMEOUT,
        "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com",
        "fatal: unable to access 'https://github.com/o/r.git/': "
        "Temporary failure in name resolution",
        "fatal: unable to access 'https://github.com/o/r.git/': "
        "The requested URL returned error: 502",
        "error: RPC failed; HTTP 503 curl 22 The requested URL returned error: 503",
        "error: RPC failed; curl 56 GnuTLS recv error (-54): Error in the pull function.\n"
        "fatal: early EOF",
        "fatal: unable to access 'https://github.com/o/r.git/': Recv failure: "
        "Connection reset by peer",
        "git step 0 timed out after 300s",
    ],
)
def test_a_connect_timeout_dns_or_5xx_failure_is_transient(text):
    assert gitops.transient_git_failure(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "remote: Repository not found.\n"
        "fatal: repository 'https://github.com/octo/gone.git/' not found",
        "fatal: Authentication failed for 'https://github.com/octo/private.git/'",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "warning: Could not find remote branch nope to clone.\n"
        "fatal: Remote branch nope not found in upstream origin",
        "fatal: couldn't find remote ref refs/heads/nope",
        "fatal: unable to access 'https://github.com/o/r.git/': "
        "The requested URL returned error: 403",
        "fatal: unable to access 'https://github.com/o/r.git/': "
        "The requested URL returned error: 404",
        "fatal: unable to access 'https://github.com/o/r.git/': "
        "SSL certificate problem: unable to get local issuer certificate",
        # A permanent answer wins even when the transport also complained.
        "remote: Repository not found.\nfatal: the remote end hung up unexpectedly",
        "",
    ],
)
def test_a_missing_repository_refused_authentication_or_bad_ref_is_permanent(text):
    assert gitops.transient_git_failure(text) is False


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("Failed to connect to github.com port 443 after 134 s"),
        urllib.error.URLError("Connection reset by peer"),
        urllib.error.URLError("Temporary failure in name resolution"),
        urllib.error.URLError(FORGE_TIMEOUT),
        urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")),
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    ],
)
def test_a_forge_network_failure_is_transient(exc):
    assert forge.transient_network_error(exc) is True


class _Refusing:
    """Stands in for `forge._NO_REDIRECT_OPENER`, the one opener `_request` uses."""

    def __init__(self, refuse) -> None:  # noqa: ANN001
        self.open = refuse


def test_a_forge_url_error_with_a_connect_reason_is_forge_unavailable(monkeypatch):
    """The probe's `_request`: a connect failure is the retryable class."""

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("Failed to connect to github.com port 443 after 134 s")

    monkeypatch.setattr(forge, "_NO_REDIRECT_OPENER", _Refusing(refuse))
    with pytest.raises(forge.ForgeUnavailable) as raised:
        forge.probe_repository(url="https://github.com/octo/widgets.git", token="t" * 8)
    assert "Failed to connect to github.com port 443" in str(raised.value)


def test_a_forge_certificate_failure_stays_permanent(monkeypatch):
    def refuse(*args, **kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    monkeypatch.setattr(forge, "_NO_REDIRECT_OPENER", _Refusing(refuse))
    with pytest.raises(forge.ForgeError) as raised:
        forge.probe_repository(url="https://github.com/octo/widgets.git", token="t" * 8)
    assert not isinstance(raised.value, forge.ForgeUnavailable)


# ---------------------------------------------------------------------------
# a fake git that fails like curl, then hands over to real git
# ---------------------------------------------------------------------------

_FAKE_GIT = """\
#!{python}
import os, sys
argv = sys.argv[1:]
network = "clone" in argv or "fetch" in argv
counter = {counter!r}
if network:
    count = int(open(counter).read()) if os.path.exists(counter) else 0
    count += 1
    with open(counter, "w") as fh:
        fh.write(str(count))
    if count <= {failures}:
        sys.stderr.write({message!r} + "\\n")
        sys.exit(128)
os.execv({git!r}, [{git!r}] + argv)
"""


def _fake_git(tmp_path: Path, *, failures: int, message: str = CLONE_CONNECT) -> tuple[Path, Path]:
    counter = tmp_path / "git-network-calls"
    script = tmp_path / "flaky-git"
    script.write_text(
        _FAKE_GIT.format(
            python=sys.executable, counter=str(counter), failures=failures,
            message=message, git=shutil.which("git") or "git",
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, counter


def _network_calls(counter: Path) -> int:
    return int(counter.read_text()) if counter.exists() else 0


@needs_git
def test_a_clone_that_cannot_connect_raises_the_transient_class(tmp_path):
    git, _ = _fake_git(tmp_path, failures=99)
    (tmp_path / "logs").mkdir()
    with pytest.raises(gitops.GitTransient) as raised:
        gitops.shallow_clone(
            url="https://github.com/octo/widgets.git", ref=None,
            destination=tmp_path / "repo", private_dir=tmp_path / "private",
            logs_dir=tmp_path / "logs", timeout_seconds=30, logger=_logger(),
            git_binary=str(git),
        )
    assert "Failed to connect to github.com port 443" in str(raised.value)


@needs_git
def test_a_clone_of_a_missing_repository_raises_a_permanent_git_error(tmp_path):
    git, _ = _fake_git(
        tmp_path, failures=99,
        message="remote: Repository not found.\n"
        "fatal: repository 'https://github.com/octo/gone.git/' not found",
    )
    (tmp_path / "logs").mkdir()
    with pytest.raises(gitops.GitError) as raised:
        gitops.shallow_clone(
            url="https://github.com/octo/gone.git", ref=None,
            destination=tmp_path / "repo", private_dir=tmp_path / "private",
            logs_dir=tmp_path / "logs", timeout_seconds=30, logger=_logger(),
            git_binary=str(git),
        )
    assert not isinstance(raised.value, gitops.GitTransient)


@needs_git
def test_a_pinned_fetch_that_cannot_connect_is_transient_not_a_branch_fallback(tmp_path):
    """The base pin's fetch by sha: a network failure is raised as transient,
    so the retry asks again, instead of falling through to a full branch
    fetch that would meet the same outage."""
    git, counter = _fake_git(tmp_path, failures=99)
    (tmp_path / "logs").mkdir()
    with pytest.raises(gitops.GitTransient):
        gitops.clone_at_commit(
            url="https://github.com/octo/widgets.git", branch="main", commit="a" * 40,
            destination=tmp_path / "repo", private_dir=tmp_path / "private",
            logs_dir=tmp_path / "logs", timeout_seconds=30, logger=_logger(),
            git_binary=str(git),
        )
    assert _network_calls(counter) == 1


# ---------------------------------------------------------------------------
# the bounded retry
# ---------------------------------------------------------------------------


def _flaky(outcomes):
    calls: list[int] = []

    def call():
        calls.append(1)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return call, calls


def test_the_waits_are_ten_then_thirty_seconds_within_the_in_worker_limit():
    assert gitops.CLONE_RETRY_WAITS_SECONDS == (10.0, 30.0)
    # Three tries in all; the sleeping fits the platform's 45 s in-worker wait.
    assert sum(gitops.CLONE_RETRY_WAITS_SECONDS) <= 45


def test_a_transient_failure_then_a_success_is_retried(tmp_path):
    destination = tmp_path / "repo"
    destination.mkdir()
    (destination / "half-written").write_text("x")
    call, calls = _flaky([gitops.GitTransient(CLONE_CONNECT), "cloned"])
    slept: list[float] = []

    result = gitops.retry_clone(
        call, destination=destination, sleep=slept.append,
        max_wait_seconds=45, remaining_seconds=lambda: 3600, logger=_logger(),
    )

    assert result == "cloned"
    assert len(calls) == 2 and slept == [10.0]
    assert list(destination.iterdir()) == [], "the retry cloned into a half-written folder"


def test_a_transient_failure_that_outlasts_the_tries_raises_it_with_the_count(tmp_path):
    call, calls = _flaky([gitops.GitTransient(CLONE_CONNECT)])
    slept: list[float] = []

    with pytest.raises(gitops.GitTransient) as raised:
        gitops.retry_clone(
            call, destination=tmp_path / "repo", sleep=slept.append,
            max_wait_seconds=45, remaining_seconds=lambda: 3600, logger=_logger(),
        )

    assert len(calls) == 3 and slept == [10.0, 30.0]
    assert raised.value.tries == 3


def test_a_permanent_failure_is_raised_at_once(tmp_path):
    call, calls = _flaky([gitops.GitError("remote: Repository not found.")])
    slept: list[float] = []

    with pytest.raises(gitops.GitError):
        gitops.retry_clone(
            call, destination=tmp_path / "repo", sleep=slept.append,
            max_wait_seconds=45, remaining_seconds=lambda: 3600, logger=_logger(),
        )

    assert len(calls) == 1 and slept == []


def test_a_wait_past_the_in_worker_limit_or_the_deadline_is_not_slept(tmp_path):
    for limit, remaining in ((15, 3600), (45, 20)):
        call, calls = _flaky([gitops.GitTransient(CLONE_CONNECT)])
        slept: list[float] = []
        with pytest.raises(gitops.GitTransient):
            gitops.retry_clone(
                call, destination=tmp_path / "repo", sleep=slept.append,
                max_wait_seconds=limit, remaining_seconds=lambda r=remaining: r,
                logger=_logger(),
            )
        assert slept == [10.0] and len(calls) == 2, (limit, remaining, slept)


# ---------------------------------------------------------------------------
# end to end, through the real lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path, monkeypatch) -> str:
    """A bare repository reachable as `file://`, and only it (as test_issue_input.py)."""
    import subprocess

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=str(cwd), check=True, capture_output=True, text=True,
        )

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "base")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
                   check=True, capture_output=True)
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(
        gitops, "validate_repository_url",
        lambda url: url if url.startswith(allowed) else real(url),
    )
    return f"file://{bare}"


def _flaky_worker(worker_factory, monkeypatch, tmp_path, origin, *, failures, message=CLONE_CONNECT):
    git, counter = _fake_git(tmp_path, failures=failures, message=message)
    real_clone = gitops.shallow_clone
    monkeypatch.setattr(
        lifecycle, "shallow_clone", lambda **kwargs: real_clone(**kwargs, git_binary=str(git))
    )
    worker, _, _ = worker_factory(repository_url=origin)
    slept: list[float] = []
    worker.forge_sleep = slept.append
    return worker, counter, slept


def _capacity_released(db) -> bool:
    lease = db.doc("leases/lease_1")
    pools = [db.doc(name if name.startswith("pools/") else f"pools/{name}")
             for name in lease["pools"]]
    return lease.get("released_at") is not None and all(int(p["active"]) < 1 for p in pools)


@needs_git
def test_a_clone_that_cannot_connect_once_is_retried_and_the_task_succeeds(
    db, store, worker_factory, monkeypatch, tmp_path, origin
):
    seed_attempt(db)
    worker, counter, slept = _flaky_worker(
        worker_factory, monkeypatch, tmp_path, origin, failures=1
    )

    code = worker.run()

    task = db.doc("tasks/task_1")
    assert code == ExitCode.OK, task
    assert task["state"] == TaskState.SUCCEEDED.value
    assert _network_calls(counter) == 2 and slept == [10.0]


@needs_git
def test_a_clone_that_never_connects_ends_the_attempt_retryably(
    db, store, worker_factory, monkeypatch, tmp_path, origin
):
    seed_attempt(db)
    worker, counter, slept = _flaky_worker(
        worker_factory, monkeypatch, tmp_path, origin, failures=99
    )

    code = worker.run()

    task = db.doc("tasks/task_1")
    assert code != ExitCode.OK
    # Attempt 1 of 3: READY again, for the scheduler to admit as a new attempt.
    assert task["state"] == TaskState.READY.value, task
    assert (task.get("last_error") or "").startswith("forge_unreachable:"), task["last_error"]
    assert "Failed to connect to github.com port 443" in task["last_error"]
    assert task["result_summary"]["clone_check"] == {"cause": "forge_unreachable", "tries": 3}
    assert task.get("next_eligible_at") is not None
    assert _network_calls(counter) == 3 and slept == [10.0, 30.0]
    assert _capacity_released(db), db.doc("leases/lease_1")


@needs_git
def test_a_clone_that_never_connects_on_the_last_attempt_ends_cannot_start(
    db, store, worker_factory, monkeypatch, tmp_path, origin
):
    seed_attempt(db)
    db.doc("tasks/task_1")["attempt_count"] = 3
    worker, _, _ = _flaky_worker(worker_factory, monkeypatch, tmp_path, origin, failures=99)

    worker.run()

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task
    assert task.get("end_cause") == EndCause.CANNOT_START.value, task
    assert _capacity_released(db)


@needs_git
@pytest.mark.parametrize(
    "message",
    [
        "remote: Repository not found.\n"
        "fatal: repository 'https://github.com/octo/gone.git/' not found",
        "fatal: Authentication failed for 'https://github.com/octo/private.git/'",
    ],
)
def test_a_missing_repository_or_refused_authentication_fails_at_once(
    db, store, worker_factory, monkeypatch, tmp_path, origin, message
):
    seed_attempt(db)
    worker, counter, slept = _flaky_worker(
        worker_factory, monkeypatch, tmp_path, origin, failures=99, message=message
    )

    worker.run()

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task
    assert (task.get("last_error") or "").startswith("repository clone failed"), task
    assert _network_calls(counter) == 1 and slept == []
