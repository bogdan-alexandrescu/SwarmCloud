"""The egress probe at worker start (#721 (a)).

MEASURED 2026-10-06 (#721): 96 % of a clone's time was one TCP connect to
GitHub, stalling 4-71 s on values that double like the SYN retransmission
backoff. The probe opens a fresh connection every ~1 s from process start, the
clone waits for it (bounded), and the attempt records when the path opened.

Pinned here, with no network: every connect is a stand-in.

* the probe answers on the Nth try and says so, with the timing;
* a probe that never answers stops at its cap, and does not block the clone;
* the clone waits for the clone host's answer, and only for that;
* a worker run writes ONE `egress_ready` mark with the numbers, and leaves no
  probe thread running once startup is over;
* the targets: github.com always, the clone host when it is elsewhere, none
  for a repository with no network host.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

from agent_worker import egress, gitops, lifecycle
from agent_worker.errors import ExitCode
from agent_worker.gitops import CloneResult
from agent_worker.logs import build_logger
from swarm_common.states import EventType

from conftest import seed_attempt

GITHUB = ("github.com", 443)
EGRESS_READY = "egress_ready"


class _Conn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Clock:
    """A clock each connect moves on by one second, as a 1 s timeout would."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _answers_on(n: int, clock: _Clock | None = None):
    calls: list[tuple] = []
    conns: list[_Conn] = []

    def connect(target, timeout=None):
        calls.append((target, timeout))
        if clock is not None:
            clock.now += 1.0
        if len(calls) < n:
            raise ConnectionRefusedError("dropped")
        conn = _Conn()
        conns.append(conn)
        return conn

    return connect, calls, conns


def _never_answers(clock: _Clock | None = None):
    calls: list[tuple] = []

    def connect(target, timeout=None):
        calls.append((target, timeout))
        if clock is not None:
            clock.now += 1.0
        raise TimeoutError("timed out")

    return connect, calls


def _logger():
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="eng", generation=1,
        runner_profile="mock", stream=io.StringIO(),
    )


def _probe_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "egress-probe" and t.is_alive()]


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


def test_the_probe_answers_on_the_nth_try_and_records_when():
    clock = _Clock()
    connect, calls, conns = _answers_on(4, clock)
    probe = egress.EgressProbe([GITHUB], connect=connect, clock=clock, interval_seconds=0.0)
    probe.start()
    assert probe.wait(GITHUB, timeout=5) is True
    probe.stop()

    result = probe.result()
    assert result["probe_attempts"] == 4
    # Four connects of one second each, from the probe's start.
    assert result["egress_ready_seconds"] == pytest.approx(4.0)
    assert result["ended"] == "ready"
    assert result["targets"][0]["ready"] is True
    # A fresh connection each try, a 1 s timeout, and the answered one closed.
    assert [c[0] for c in calls] == [GITHUB] * 4
    assert {c[1] for c in calls} == {egress.EGRESS_CONNECT_TIMEOUT_SECONDS}
    assert len(conns) == 1 and conns[0].closed
    assert not probe.running


def test_a_probe_that_never_answers_stops_at_its_cap():
    clock = _Clock()
    connect, calls = _never_answers(clock)
    probe = egress.EgressProbe(
        [GITHUB], connect=connect, clock=clock, cap_seconds=5.0, interval_seconds=0.0,
    )
    probe.start()
    started = time.monotonic()
    assert probe.wait(GITHUB, timeout=10) is False
    assert time.monotonic() - started < 5, "the wait outlived a probe that had ended"
    probe.stop()

    result = probe.result()
    assert result["ended"] == "cap"
    assert result["egress_ready_seconds"] is None
    assert result["probe_attempts"] == len(calls) == 5
    # The exception's class only: an error's text never reaches the event.
    assert result["targets"][0]["last_error"] == "TimeoutError"
    assert not probe.running


def test_the_cap_is_above_the_longest_stall_measured():
    # #721: the longest connect stall over three days was 78 s.
    assert egress.EGRESS_PROBE_CAP_SECONDS > 78
    assert egress.EGRESS_CONNECT_TIMEOUT_SECONDS == 1.0
    assert egress.EGRESS_PROBE_INTERVAL_SECONDS == 1.0


def test_stop_ends_a_running_probe_and_joins_its_thread():
    connect, _ = _never_answers()
    probe = egress.EgressProbe([GITHUB], connect=connect, interval_seconds=0.05)
    probe.start()
    assert probe.running
    probe.stop()
    assert not probe.running
    assert probe.result()["ended"] == "stopped"
    assert _probe_threads() == []


def test_the_targets_are_github_and_the_clone_host_when_elsewhere():
    assert egress.probe_targets("https://github.com/acme/widgets.git") == [GITHUB]
    assert egress.probe_targets("https://gitlab.example.com/acme/widgets.git") == [
        GITHUB, ("gitlab.example.com", 443),
    ]
    assert egress.probe_targets("https://git.example.com:8443/a/b.git") == [
        GITHUB, ("git.example.com", 8443),
    ]
    assert egress.probe_targets("git@git.example.com:a/b.git") == [
        GITHUB, ("git.example.com", 22),
    ]
    assert egress.probe_targets("file:///srv/origin.git") == []
    assert egress.probe_targets(None) == []


def test_each_target_is_probed_until_it_answers():
    tries: dict[tuple, int] = {}

    def connect(target, timeout=None):
        tries[target] = tries.get(target, 0) + 1
        if target == GITHUB or tries[target] >= 3:
            return _Conn()
        raise OSError("dropped")

    other = ("git.example.com", 443)
    probe = egress.EgressProbe([GITHUB, other], connect=connect, interval_seconds=0.0)
    probe.start()
    assert probe.wait(other, timeout=5) is True
    probe.stop()
    by_host = {t["host"]: t for t in probe.result()["targets"]}
    assert by_host["github.com"]["probe_attempts"] == 1
    assert by_host["git.example.com"]["probe_attempts"] == 3


# ---------------------------------------------------------------------------
# the clone's wait
# ---------------------------------------------------------------------------


def test_the_clone_waits_for_the_probe_then_goes_on():
    gate = threading.Event()

    def connect(target, timeout=None):
        if gate.is_set():
            return _Conn()
        raise OSError("dropped")

    probe = egress.EgressProbe([GITHUB], connect=connect, interval_seconds=0.02)
    probe.start()
    threading.Timer(0.3, gate.set).start()
    started = time.monotonic()
    ready = gitops.await_egress(probe, "https://github.com/acme/widgets.git", _logger())
    waited = time.monotonic() - started
    probe.stop()
    assert ready is True
    assert waited >= 0.25, "the clone did not wait for the probe"


def test_a_failing_probe_does_not_block_the_clone():
    clock = _Clock()
    connect, _ = _never_answers(clock)
    probe = egress.EgressProbe(
        [GITHUB], connect=connect, clock=clock, cap_seconds=3.0, interval_seconds=0.0,
    )
    probe.start()
    started = time.monotonic()
    assert gitops.await_egress(probe, "https://github.com/acme/widgets.git", _logger()) is False
    assert time.monotonic() - started < 5
    probe.stop()


def test_the_clone_does_not_wait_for_a_host_nobody_probed():
    connect, _ = _never_answers()
    probe = egress.EgressProbe([GITHUB], connect=connect, interval_seconds=0.05)
    probe.start()
    started = time.monotonic()
    assert gitops.await_egress(probe, "https://git.example.com/a/b.git", _logger()) is False
    assert gitops.await_egress(probe, "file:///srv/origin.git", _logger()) is False
    assert gitops.await_egress(None, "https://github.com/a/b.git", _logger()) is False
    assert time.monotonic() - started < 0.5
    probe.stop()


def test_shallow_clone_waits_for_the_probe_before_git_runs(tmp_path: Path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        gitops, "await_egress", lambda probe, url, logger: order.append("egress") or True
    )

    def steps(*_args, **_kwargs):
        order.append("git")
        return 0.01

    monkeypatch.setattr(gitops, "_run_git_steps", steps)
    monkeypatch.setattr(gitops, "_read_head", lambda *a, **k: "a" * 40)
    probe = egress.EgressProbe([GITHUB])
    gitops.shallow_clone(
        url="https://github.com/acme/widgets.git", ref=None, destination=tmp_path / "repo",
        private_dir=tmp_path / "private", logs_dir=tmp_path, timeout_seconds=10,
        logger=_logger(), egress=probe,
    )
    assert order == ["egress", "git"]


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


def _marks(db) -> list[dict]:
    return [
        event for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == EGRESS_READY
    ]


def _fake_clone(seen: dict):
    def clone(**kwargs):
        probe = kwargs.get("egress")
        seen["egress"] = probe
        seen["answered_before_clone"] = gitops.await_egress(probe, kwargs["url"], _logger())
        return CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="a" * 40, duration_seconds=0.1,
        )

    return clone


def test_the_worker_probes_at_start_and_records_egress_ready(
    db, worker_factory, monkeypatch
):
    seed_attempt(db)
    seen: dict = {}
    monkeypatch.setattr(lifecycle, "shallow_clone", _fake_clone(seen))
    worker, _, _ = worker_factory(repository_url="https://github.com/acme/widgets.git")
    connect, calls, _ = _answers_on(3)
    worker.egress_connect = connect
    worker.egress_interval_seconds = 0.0

    assert worker.run() == ExitCode.OK

    assert isinstance(seen["egress"], egress.EgressProbe)
    assert seen["answered_before_clone"] is True
    (event,) = _marks(db)
    assert event["type"] == EventType.RUNNING.value
    assert event["attempt_id"] == "att_1"
    detail = event["detail"]["egress"]
    assert detail["probe_attempts"] == 3
    assert isinstance(detail["egress_ready_seconds"], float)
    assert detail["egress_ready_seconds"] >= 0
    assert detail["ended"] == "ready"
    assert len(calls) == 3
    # No probe thread outlives startup.
    assert _probe_threads() == []


def test_a_worker_whose_probe_never_answers_still_clones_and_leaves_no_thread(
    db, worker_factory, monkeypatch
):
    seed_attempt(db)
    seen: dict = {}
    monkeypatch.setattr(lifecycle, "shallow_clone", _fake_clone(seen))
    worker, _, _ = worker_factory(repository_url="https://github.com/acme/widgets.git")
    connect, _ = _never_answers()
    worker.egress_connect = connect
    worker.egress_interval_seconds = 0.01
    worker.egress_cap_seconds = 0.2

    assert worker.run() == ExitCode.OK

    assert seen["answered_before_clone"] is False
    (event,) = _marks(db)
    detail = event["detail"]["egress"]
    assert detail["egress_ready_seconds"] is None
    assert detail["probe_attempts"] >= 1
    assert _probe_threads() == []


def test_a_task_without_a_network_repository_starts_no_probe(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()

    def refuse(*_args, **_kwargs):
        raise AssertionError("a task with nothing to clone probed the network")

    worker.egress_connect = refuse
    assert worker.run() == ExitCode.OK
    assert _marks(db) == []
    assert _probe_threads() == []
