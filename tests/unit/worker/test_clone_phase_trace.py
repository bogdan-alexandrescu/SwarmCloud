"""Where a clone's time goes, from git's own traces (#667, lane OB1).

Measured by the chunk-1 observer on 2026-10-06: 229 clones in a day, median
37.8 s, p90 70.9 s, clustered at ~38 s and ~71 s at every load level -- one
depth-1 `git clone` of a 33 MB tree, no retry, no NAT errors: a fixed stall,
not bandwidth. Nothing recorded which part of the clone stalled.

Pinned here:

* the parser, over a trace built in this file: DNS, connect, TLS, ref
  negotiation, the server's pack wait, the pack transfer and the checkout,
  each from git's own timestamps, and nothing but numbers out of it;
* a real clone of a `file://` repository is timed and its trace files are
  gone when `shallow_clone` returns;
* an `Authorization` header in the trace never reaches a log line, the
  result or an event -- the trace is read in-process and thrown away;
* the worker writes the timings as ONE `clone_timed` event per attempt, even
  when the clone was retried.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker import gitops, lifecycle
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.states import EventType, TaskState

from conftest import seed_attempt

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

CLONE_TIMED = "clone_timed"


def _logger(stream=None):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="eng", generation=1,
        runner_profile="mock", stream=stream if stream is not None else io.StringIO(),
    )


def _auth_value() -> str:
    # Built at runtime from random bytes: no credential-shaped literal in this
    # file, and a value no other line of any output could contain by chance.
    user = "x-access-" + "token"
    return base64.b64encode(f"{user}:{secrets.token_hex(20)}".encode()).decode()


# ---------------------------------------------------------------------------
# a trace, as git 2.39 writes it, built here
# ---------------------------------------------------------------------------

TOP = "20261006T100000.000000Z-H00000001-P00000001"
CHILD = TOP + "/20261006T100000.100000Z-H00000001-P00000002"


def _event(kind: str, time: str, sid: str = TOP, **fields) -> str:
    return json.dumps({"event": kind, "sid": sid, "time": f"2026-10-06T{time}Z", **fields})


def _events(*, day: str = "2026-10-06", start: str = "10:00:00.000000") -> str:
    lines = [
        _event("version", start, exe="2.39.5"),
        _event("start", start, t_abs=0.0005,
               argv=["git", "clone", "--depth", "1", "--", "https://github.com/o/r.git", "r"]),
        _event("child_start", "10:00:00.050000", child_id=0, child_class="transport/https",
               argv=["git", "remote-https", "origin", "https://github.com/o/r.git"]),
        _event("version", "10:00:00.100000", sid=CHILD, exe="2.39.5"),
        _event("region_enter", "10:00:02.000000", category="fetch-pack", label="negotiation_v2"),
        _event("region_leave", "10:00:03.400000", category="fetch-pack", label="negotiation_v2"),
        _event("child_start", "10:00:03.600000", child_id=1, child_class="?",
               argv=["git", "index-pack", "--stdin", "--fix-thin"]),
        _event("child_exit", "10:00:40.500000", child_id=1, code=0, t_rel=36.9),
        _event("child_exit", "10:00:40.550000", child_id=0, code=0, t_rel=40.5),
        _event("region_enter", "10:00:40.600000", category="unpack_trees", label="unpack_trees"),
        _event("region_enter", "10:00:40.700000", category="unpack_trees", label="traverse_trees"),
        _event("region_leave", "10:00:41.500000", category="unpack_trees", label="traverse_trees"),
        _event("region_leave", "10:00:41.600000", category="unpack_trees", label="unpack_trees"),
        _event("exit", "10:00:41.700000", t_abs=41.7, code=0),
        "not json at all",
    ]
    text = "\n".join(lines) + "\n"
    return text.replace("2026-10-06T", f"{day}T")


def _wire(auth: str) -> str:
    def line(clock: str, where: str, message: str) -> str:
        head = f"{clock} {where}"
        return f"{head:<40}{message}"

    return "\n".join([
        line("10:00:00.200000", "http.c:756", "== Info: Couldn't find host github.com in the .netrc"),
        line("10:00:00.200000", "http.c:756", "== Info:   Trying [2606:50c0:8000::153]:443..."),
        line("10:00:00.700000", "http.c:756", "== Info:   Trying 140.82.112.3:443..."),
        line("10:00:01.200000", "http.c:756", "== Info: Connected to github.com (140.82.112.3) port 443"),
        line("10:00:01.400000", "http.c:756", "== Info: SSL connection using TLSv1.3 / TLS_AES_128_GCM_SHA256"),
        line("10:00:01.500000", "http.c:703", "=> Send header, 0000000214 bytes (0x000000d6)"),
        line("10:00:01.500000", "http.c:715", "=> Send header: GET /o/r.git/info/refs?service=git-upload-pack HTTP/1.1"),
        line("10:00:01.500000", "http.c:715", f"=> Send header: Authorization: Basic {auth}"),
        line("10:00:01.900000", "pkt-line.c:80", "packet:          git< version 2"),
        line("10:00:02.000000", "http.c:703", "=> Send header, 0000000300 bytes (0x0000012c)"),
        line("10:00:02.000000", "http.c:756", "== Info: Re-using existing connection with host github.com"),
        line("10:00:03.500000", "pkt-line.c:80", "packet:        clone< packfile"),
        line("10:00:33.500000", "pkt-line.c:80", "packet:     sideband< \\1PACK ..."),
        line("garbage", "x", "y"),
    ]) + "\n"


def test_the_parser_times_every_phase_from_gits_own_timestamps():
    auth = _auth_value()
    phases = gitops.clone_phase_timings(_events(), _wire(auth))

    assert phases == {
        "total_seconds": 41.7,
        "dns_seconds": 0.2,
        "connect_seconds": 1.0,
        "tls_seconds": 0.3,
        "negotiation_seconds": 2.0,
        "pack_wait_seconds": 30.0,
        "pack_seconds": 37.0,
        "checkout_seconds": 1.0,
        "connect_tries": 2,
        "connections": 1,
        "http_requests": 2,
        "git_version": "2.39.5",
    }
    assert auth not in json.dumps(phases)


def test_a_trace_across_midnight_is_read_on_the_next_day():
    events = _events().replace("T10:00:00.000000Z", "T23:59:59.900000Z")
    wire = "\n".join([
        f"{'00:00:00.100000 http.c:756':<40}== Info:   Trying 140.82.112.3:443...",
        f"{'00:00:00.300000 http.c:756':<40}== Info: Connected to github.com port 443",
    ]) + "\n"
    phases = gitops.clone_phase_timings(events, wire)
    assert phases["dns_seconds"] == 0.2
    assert phases["connect_seconds"] == 0.2


def test_no_trace_is_no_timings_and_never_an_error():
    assert gitops.clone_phase_timings("", "") == {}
    assert gitops.clone_phase_timings("{not json\n", "nonsense\n") == {}


def test_a_clone_without_tls_or_curl_reports_no_network_phases_it_did_not_see():
    phases = gitops.clone_phase_timings(_events(), "")
    assert "dns_seconds" not in phases and "tls_seconds" not in phases
    assert phases["checkout_seconds"] == 1.0
    assert phases["total_seconds"] == 41.7


# ---------------------------------------------------------------------------
# a real clone
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


@pytest.fixture
def origin(tmp_path: Path, monkeypatch) -> str:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    for index in range(120):
        (seed / f"file-{index}.txt").write_text(f"line {index}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
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


#: A git that writes an Authorization header into the wire trace -- where a
#: real https clone's GIT_TRACE_CURL would put one -- and then runs real git.
_HEADER_GIT = """\
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
    wire = os.environ.get("GIT_TRACE_CURL")
    if wire:
        with open(wire, "a") as fh:
            fh.write("10:00:00.000000 http.c:715              => Send header: Authorization: Basic {auth}\\n")
    if count <= {failures}:
        sys.stderr.write("fatal: unable to access 'x': Failed to connect to github.com port 443 after 1 ms\\n")
        sys.exit(128)
os.execv({git!r}, [{git!r}] + argv)
"""


def _header_git(tmp_path: Path, auth: str, *, failures: int = 0) -> tuple[Path, Path]:
    counter = tmp_path / "git-network-calls"
    script = tmp_path / "header-git"
    script.write_text(_HEADER_GIT.format(
        python=sys.executable, counter=str(counter), auth=auth, failures=failures,
        git=shutil.which("git") or "git",
    ))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, counter


@needs_git
def test_a_real_clone_is_timed_and_its_trace_files_are_removed(tmp_path, origin):
    private = tmp_path / "private"
    (tmp_path / "logs").mkdir()
    result = gitops.shallow_clone(
        url=origin, ref=None, destination=tmp_path / "repo", private_dir=private,
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_logger(),
    )
    phases = result.phases
    assert phases["total_seconds"] > 0
    for key in ("negotiation_seconds", "pack_seconds", "checkout_seconds"):
        assert isinstance(phases[key], float) and phases[key] >= 0, (key, phases)
    assert phases["git_version"]
    left = sorted(p.name for p in private.iterdir())
    assert not [name for name in left if "trace" in name], left


@needs_git
def test_an_auth_header_in_the_trace_reaches_no_log_and_no_result(tmp_path, origin):
    auth = _auth_value()
    git, _ = _header_git(tmp_path, auth)
    stream = io.StringIO()
    private = tmp_path / "private"
    (tmp_path / "logs").mkdir()
    result = gitops.shallow_clone(
        url=origin, ref=None, destination=tmp_path / "repo", private_dir=private,
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_logger(stream),
        git_binary=str(git),
    )
    assert result.phases["total_seconds"] > 0
    assert auth not in stream.getvalue()
    assert auth not in json.dumps(result.phases)
    assert "Authorization" not in stream.getvalue()
    for path in private.rglob("*"):
        if path.is_file():
            assert auth not in path.read_text(errors="replace"), path


# ---------------------------------------------------------------------------
# the worker's event
# ---------------------------------------------------------------------------


def _timed(db) -> list[dict]:
    return [
        event for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == CLONE_TIMED
    ]


@needs_git
def test_the_worker_writes_the_timings_once_per_attempt_even_after_a_retry(
    db, store, worker_factory, monkeypatch, tmp_path, origin, log_stream
):
    seed_attempt(db)
    auth = _auth_value()
    git, counter = _header_git(tmp_path, auth, failures=1)
    real_clone = gitops.shallow_clone
    monkeypatch.setattr(
        lifecycle, "shallow_clone", lambda **kwargs: real_clone(**kwargs, git_binary=str(git))
    )
    worker, _, _ = worker_factory(repository_url=origin)
    worker.forge_sleep = lambda _seconds: None

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert int(counter.read_text()) == 2, "the clone was not retried"

    (event,) = _timed(db)
    assert event["type"] == EventType.RUNNING.value
    assert event["attempt_id"] == "att_1"
    clone = event["detail"]["clone"]
    assert clone["tries"] == 2
    assert clone["seconds"] >= 0 and clone["total_seconds"] > 0
    assert clone["pinned"] is False

    everything = json.dumps(db.events("task_1"), default=str)
    assert auth not in everything
    assert auth not in log_stream.getvalue()


def test_a_task_without_a_repository_writes_no_clone_event(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    assert _timed(db) == []
