"""A live worker's heartbeat outlives a CPU-saturated agent and a checkpoint (#426).

On 2026-10-01 a claude-code worker compiling OpenTofu, TFLint and Trivy from
source on every core went 98 s without a heartbeat across a checkpoint and was
reclaimed as `lost_worker` against the reconciler's 90 s grace while it was
alive and working. #286/#288 had already put a thread under the archive. What
was left, and what each test below holds:

1. THE BEAT DID NOT FIT THE GRACE. 30 s beats against a 90 s grace: the third
   beat is due on the grace itself, so two missed beats plus any latency on the
   third is a reclaim. The worker's beat is now derived from the grace so two
   missed beats and a whole interval for the third still fit, and the grace and
   the beat are stated together (`agent_worker.config`).
2. ONE BEAT COULD RETRY FOR 90 s. A beat retried for `0.75 x` the lease
   extension -- the whole grace -- before giving way to the next one. It is
   now held to one beat interval.
3. THE THREAD'S FIRST BEAT WAS COUNTED FROM THE CHECKPOINT'S START, not from
   the last beat that landed, so a checkpoint begun just before the loop's next
   beat added a whole interval of silence.
4. THE THREAD COVERED ONLY THE ARCHIVE. The owner check and the start event
   before it, and the pointer's transaction after it, ran with nothing beating.
5. THE AGENT COMPETED WITH THE HEARTBEAT AT THE SAME PRIORITY. The runner
   child, and everything it forks, now runs at a lower priority (`nice`) than
   the worker. A process may lower its own priority without a capability but
   not raise it, so "the heartbeat above the agent" is reached by lowering the
   agent, not by raising the worker.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from agent_worker import config as config_mod
from agent_worker import lifecycle as lifecycle_mod
from agent_worker.config import WorkerConfig
from agent_worker.control import ControlPlane
from agent_worker.errors import ConfigError, ExitCode
from agent_worker.logs import build_logger
from agent_worker.procman import ChildProcess
from swarm_common.states import EventType

from conftest import seed_attempt
import fakes


# ---------------------------------------------------------------------------
# 1. the beat fits inside the grace with two missed beats to spare
# ---------------------------------------------------------------------------


def _from_env(monkeypatch, **env: str) -> WorkerConfig:
    for name in ("HEARTBEAT_INTERVAL_SECONDS", "HEARTBEAT_GRACE_SECONDS", "RUNNER_NICENESS"):
        monkeypatch.delenv(name, raising=False)
    base = {
        "TASK_ID": "task_1",
        "ATTEMPT_ID": "att_1",
        "LEASE_ID": "lease_1",
        "TENANT_ID": "eng",
        "GENERATION": "1",
        "RUNNER_PROFILE": "mock",
        "PROJECT_ID": "swarm-test",
    }
    base.update(env)
    for name, value in base.items():
        monkeypatch.setenv(name, value)
    return WorkerConfig.from_env()


def test_the_heartbeat_interval_leaves_two_missed_beats_inside_the_grace(monkeypatch):
    """At the platform's defaults (30 s interval, 90 s grace) the worker beat
    every 30 s: the third beat was due ON the grace, so two missed beats and a
    few seconds of latency were a reclaim. MUTATION: take the interval from
    the platform setting unchanged and this reads 30."""
    cfg = _from_env(monkeypatch)
    grace = config_mod.heartbeat_grace_seconds(30)  # the platform's default interval
    beat = cfg.heartbeat_interval_seconds
    missed = config_mod.MISSED_BEATS_TOLERATED
    assert missed == 2
    assert grace == 90
    # The (missed + 1)th beat is due inside the grace with a whole interval of
    # room left for it to land.
    assert (missed + 1) * beat + beat <= grace, (
        f"a {beat} s beat against a {grace} s grace: two missed beats are a reclaim"
    )
    assert beat == 22


def test_a_grace_with_room_to_spare_keeps_the_configured_interval(monkeypatch):
    """The control: the interval is shortened only when the grace needs it. A
    deployment whose grace is 200 s keeps its 30 s beat."""
    cfg = _from_env(monkeypatch, HEARTBEAT_GRACE_SECONDS="200")
    assert cfg.heartbeat_interval_seconds == 30


def test_a_longer_platform_interval_still_fits_its_own_grace(monkeypatch):
    """HEARTBEAT_INTERVAL_SECONDS=40 makes the reconciler's grace 120 s
    (`max(90, 3 x interval)`): the worker beats every 30 s, not 40."""
    cfg = _from_env(monkeypatch, HEARTBEAT_INTERVAL_SECONDS="40", LEASE_TIMEOUT_SECONDS="160")
    assert config_mod.heartbeat_grace_seconds(40) == 120
    assert cfg.heartbeat_interval_seconds == 30


def test_a_grace_too_short_for_any_beat_is_refused(monkeypatch):
    with pytest.raises(ConfigError, match="grace"):
        _from_env(monkeypatch, HEARTBEAT_GRACE_SECONDS="3")


def test_the_worker_reads_the_grace_the_reconciler_reclaims_by(monkeypatch):
    """The grace is the reconciler's (`ReconcilerConfig.heartbeat_grace_seconds`);
    the worker restates its default formula, and this holds the two equal for
    the platform's intervals and for an override both of them read."""
    from reconciler.config import ReconcilerConfig
    from swarm_common.config import Settings

    for interval in (10, 30, 40, 60):
        monkeypatch.delenv("HEARTBEAT_GRACE_SECONDS", raising=False)
        settings = Settings(project_id="swarm-test", heartbeat_interval_seconds=interval,
                            lease_timeout_seconds=max(120, 4 * interval))
        reconciler = ReconcilerConfig.from_env(settings)
        assert config_mod.heartbeat_grace_seconds(interval) == reconciler.heartbeat_grace_seconds
    monkeypatch.setenv("HEARTBEAT_GRACE_SECONDS", "150")
    settings = Settings(project_id="swarm-test")
    assert config_mod.heartbeat_grace_seconds(30) == ReconcilerConfig.from_env(
        settings
    ).heartbeat_grace_seconds == 150


# ---------------------------------------------------------------------------
# 2. one beat's retries give way to the next beat
# ---------------------------------------------------------------------------


def _control(**kwargs) -> ControlPlane:
    return ControlPlane(
        fakes.FakeFirestore(), task_id="task_1", attempt_id="att_1", lease_id="lease_1",
        tenant_id="eng", generation=1,
        logger=build_logger(task_id="t", attempt_id="a", tenant_id="eng", generation=1,
                            runner_profile="mock"),
        heartbeat_extension_seconds=120, **kwargs,
    )


def test_a_heartbeat_retries_for_no_longer_than_one_beat_interval():
    """A beat retried for 90 s -- the whole grace -- before the next one could
    start. Held to the beat interval, a beat that cannot land gives way to the
    next on time. The control: with no interval given, the #70 budget stands."""
    assert _control().call_options("heartbeat")["retry"].timeout == 90
    assert _control(heartbeat_interval_seconds=22).call_options("heartbeat")["retry"].timeout == 22
    # Only the heartbeat's budget moves.
    assert _control(heartbeat_interval_seconds=22).call_options("poll")["retry"].timeout == 30


# ---------------------------------------------------------------------------
# 3. the thread's first beat is due from the last beat, not from its own start
# ---------------------------------------------------------------------------


def _count_beats(worker):
    beats: list[float] = []
    real = worker._heartbeat

    def counted():
        beats.append(time.monotonic())
        return real()

    worker._heartbeat = counted  # type: ignore[method-assign]
    return beats


def test_the_first_beat_during_a_checkpoint_is_due_from_the_last_beat(db, worker_factory):
    """The loop beats at t0 and starts a checkpoint 1.4 s later, on a 2 s beat.
    The thread's first beat is due at t0 + 2 s. It used to wait 2 s from its
    own start -- t0 + 3.4 s, 1.7 intervals of silence."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(heartbeat_interval_seconds=2)
    last = time.monotonic()
    worker._heartbeat()
    beats = _count_beats(worker)
    time.sleep(1.4)
    with worker._heartbeat_meanwhile("checkpoint (test)"):
        time.sleep(2.2)
    assert beats, "no beat at all while the block ran"
    assert beats[0] - last <= 2.0 + 0.4, (
        f"the first beat came {beats[0] - last:.2f} s after the last one, on a 2 s beat"
    )


def test_a_beat_that_cannot_reach_the_control_plane_does_not_end_the_beating(
    db, worker_factory
):
    """Held to one interval, a beat that cannot land is one of the missed
    beats the interval is sized for; the thread asks for the next on time. It
    used to end the thread, leaving the rest of the checkpoint unbeaten. The
    control: a fence still ends it (the fenced test in
    test_checkpoint_tool_caches.py), and here a beat after the failures lands."""
    from google.api_core import exceptions as core_exceptions

    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(heartbeat_interval_seconds=1)
    real = worker.control.heartbeat
    calls: list[str] = []

    def flaky():
        if len(calls) < 2:
            calls.append("unreachable")
            raise core_exceptions.ServiceUnavailable("firestore is unreachable")
        calls.append("landed")
        return real()

    worker.control.heartbeat = flaky  # type: ignore[method-assign]
    before = db.doc("leases/lease_1").get("heartbeat_at")
    with worker._heartbeat_meanwhile("checkpoint (test)"):
        time.sleep(3.6)
    assert calls[:3] == ["unreachable", "unreachable", "landed"], calls
    assert db.doc("leases/lease_1").get("heartbeat_at") != before


# ---------------------------------------------------------------------------
# 4. the whole checkpoint is covered, not only its archive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slow_call", ["ensure_owner", "emit", "record_checkpoint"])
def test_the_lease_is_heartbeaten_through_the_whole_checkpoint(db, worker_factory, slow_call):
    """The owner check, the start event and the pointer's transaction are
    Firestore calls with budgets of 30-60 s each, and they ran with nothing
    beating. Each is held here for 2.5 beat intervals; the lease's
    `heartbeat_at` must move while it is."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=1, checkpoint_interval_seconds=60, control_poll_seconds=60,
    )
    real = getattr(worker.control, slow_call)
    seen: list[tuple[object, object]] = []

    def slow(*args, **kwargs):
        is_checkpoint_call = slow_call != "emit" or args[0] == EventType.CHECKPOINT_STARTED
        if is_checkpoint_call and not seen:
            before = db.doc("leases/lease_1").get("heartbeat_at")
            time.sleep(2.5)
            seen.append((before, db.doc("leases/lease_1").get("heartbeat_at")))
        return real(*args, **kwargs)

    setattr(worker.control, slow_call, slow)
    worker.run()
    assert seen, f"no checkpoint reached {slow_call}"
    before, after = seen[0]
    assert after is not None and after != before, (
        f"{slow_call} held the lease's heartbeat for 2.5 intervals during a checkpoint"
    )


# ---------------------------------------------------------------------------
# 5. the agent runs below the worker's priority
# ---------------------------------------------------------------------------

_REPORTS_ITS_NICENESS = (
    "import os, subprocess, sys, time\n"
    "time.sleep(0.5)\n"
    "print('child', os.nice(0), flush=True)\n"
    "subprocess.run([sys.executable, '-c', 'import os; print(\"grandchild\", os.nice(0), flush=True)'])\n"
)


def _niceness_of(tmp_path, log_stream, **kwargs) -> dict[str, int]:
    logger = build_logger(task_id="t", attempt_id="a", tenant_id="eng", generation=1,
                          runner_profile="mock", stream=log_stream)
    child = ChildProcess(
        [sys.executable, "-c", _REPORTS_ITS_NICENESS],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log", stderr_path=tmp_path / "err.log",
        max_stdout_bytes=4096, max_stderr_bytes=4096, logger=logger, **kwargs,
    )
    child.start()
    assert child.wait(30) == 0, (tmp_path / "err.log").read_text()
    child.finish()
    seen = {}
    for line in (tmp_path / "out.log").read_text().splitlines():
        who, value = line.split()
        seen[who] = int(value)
    return seen


def test_the_runner_child_and_what_it_forks_run_below_the_worker(tmp_path, log_stream):
    """A compiler on every core competed with the heartbeat at the same
    priority. The child is started at `niceness` above the worker's, and a
    process it forks inherits it. The control: the worker's own niceness does
    not move, and a child started with 0 runs at the worker's."""
    base = os.nice(0)
    lowered = _niceness_of(tmp_path, log_stream, niceness=10)
    assert os.nice(0) == base, "the worker lowered its own priority, not the child's"
    expected = min(19, base + 10)
    assert lowered == {"child": expected, "grandchild": expected}
    (tmp_path / "out.log").unlink()
    assert _niceness_of(tmp_path, log_stream, niceness=0) == {"child": base, "grandchild": base}


def test_the_worker_starts_its_runner_at_the_configured_niceness(db, worker_factory, monkeypatch):
    """The lifecycle's runner child is the one that matters: it is the agent.
    Measured from the outside, on the live child, right after it starts."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    measured: list[int] = []

    class Measured(ChildProcess):
        def start(self) -> None:
            super().start()
            measured.append(os.getpriority(os.PRIO_PROCESS, self.pid) - os.nice(0))

    monkeypatch.setattr(lifecycle_mod, "ChildProcess", Measured)
    worker, cfg, _ = worker_factory()
    assert cfg.runner_niceness == 10
    assert worker.run() == ExitCode.OK
    assert measured, "the runner child was never started"
    assert measured[0] == min(19, os.nice(0) + 10) - os.nice(0)


def test_the_runner_niceness_defaults_to_ten_and_is_bounded(monkeypatch):
    assert _from_env(monkeypatch).runner_niceness == 10
    assert _from_env(monkeypatch, RUNNER_NICENESS="0").runner_niceness == 0
    with pytest.raises(ConfigError, match="niceness"):
        _from_env(monkeypatch, RUNNER_NICENESS="20")
    with pytest.raises(ConfigError, match="niceness"):
        _from_env(monkeypatch, RUNNER_NICENESS="-1")
