"""The generation check retries on a schedule before a worker gives up with 69 (#198).

WHAT HAPPENED. On 2026-09-25 execution swarm-job-eng-mock-9ngvq (task
task_5254e8f1cb31446ca20b) started its worker at 20:37:26Z, passed the DNS
preflight in 28 ms, and then could not reach Firestore at all:

    503 failed to connect to all addresses; last error: FAILED_PRECONDITION:
    ipv6:[2607:f8b0:4001:c00::5f]:443 ... Network is unreachable

It spent the generation check's whole 30 s budget in one silent retry and
exited 69 at 20:37:56Z. The other 119 executions of the previous 14 days
(measured from their "startup phase" log lines) made the same read in at most
0.43 s, though every one of them was given the same IPv6 address as well as an
IPv4 one. "Failed to connect to ALL addresses" means the IPv4 address failed
too; the IPv6 error is only the last one gRPC kept. Google documents the cause
for Direct VPC egress: "You might experience connection establishment delays
of a minute or more on instance startup when using Direct VPC egress."
(docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc, read 2026-09-25.)

So the read now gets the DNS preflight's own treatment: attempts at fixed
start times that span more than a minute, one WARNING per failed attempt, and
the verdict only after the last. The loop is the preflight's loop, not a copy
of it (`startup.run_on_schedule`).

WHAT IS NOT RETRIED. A refusal (78) would be refused again. A fence (70) and
a tenant mismatch (79) are answers about ownership. Each still ends the
attempt on the first read, as before.

HOW THE TESTS WERE MADE RED FIRST. The worker's schedule is driven by a fake
clock set on `Worker.startup_clock` and `Worker.startup_sleep`, attributes the
old worker never reads. So on main every behavioural test here fails on the
exit code or on the number of reads, not on an import.
"""

from __future__ import annotations

import io
import json
import signal
from typing import Any

import pytest
from fakes import ExplodingChildProcess, FakeDocumentRef, FakeFirestore

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt

#: The worker's exits, restated: the numbers are the contract with whoever
#: reads the execution's exit status.
UNAVAILABLE = 69
FENCED = 70
CANNOT_START = 78
INTERRUPTED = 143

#: The documented delay these retries exist to outlast: Direct VPC egress can
#: take "a minute or more" to pass traffic after an instance starts.
DIRECT_VPC_STARTUP_DELAY_SECONDS = 60


class FakeTime:
    """A clock that moves only when the worker sleeps, so a test never waits."""

    def __init__(self) -> None:
        self.now = 5_000.0
        self.slept: list[float] = []
        self.on_sleep: Any = None

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(float(seconds), 3))
        if self.on_sleep is not None:
            self.on_sleep()
        self.now += float(seconds)


class _Exited(BaseException):
    """Stands in for `os._exit`, so the test process survives the worker's exit."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _hard_exit(code: int) -> None:
    raise _Exited(code)


def _records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


def _unavailable() -> BaseException:
    from google.api_core import exceptions as core

    # What the incident's read raised once its budget was spent.
    return core.RetryError(
        "Timeout of 30.0s exceeded, last exception: 503 failed to connect to all addresses",
        cause=core.ServiceUnavailable(
            "failed to connect to all addresses; last error: FAILED_PRECONDITION: "
            "ipv6:%5B2607:f8b0:4001:c00::5f%5D:443: connect failed: Network is unreachable"
        ),
    )


def _task_reads_fail(monkeypatch, db: FakeFirestore, *, failures: int | None,
                     error: Any = _unavailable) -> list[int]:
    """Make the first `failures` reads of the task raise (None: every read).

    Returns, per failed read, how many writes the worker had made by then.
    """
    original = FakeDocumentRef.get
    writes_at_failure: list[int] = []

    def get(self: FakeDocumentRef, *args: Any, **kwargs: Any) -> Any:
        if self.path == "tasks/task_1" and (failures is None or len(writes_at_failure) < failures):
            writes_at_failure.append(len(db.writes))
            raise error()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FakeDocumentRef, "get", get)
    return writes_at_failure


def _scheduled(worker: Any) -> FakeTime:
    fake = FakeTime()
    worker.startup_clock = fake.clock
    worker.startup_sleep = fake.sleep
    return fake


def _schedule() -> tuple[float, ...]:
    from agent_worker import startup

    return tuple(startup.CONTROL_PLANE_READ_SCHEDULE_SECONDS)


def _gaps(schedule: tuple[float, ...]) -> list[float]:
    """The sleeps between attempts that each fail at once."""
    return [round(later - earlier, 3) for earlier, later in zip(schedule, schedule[1:])]


# ---------------------------------------------------------------------------
# an outage that ends inside the schedule costs nothing
# ---------------------------------------------------------------------------


def test_an_outage_that_ends_before_the_last_attempt_does_not_cost_the_attempt(
    db, worker_factory, log_stream, monkeypatch
):
    """The incident, survived: the first two reads fail, the third reaches Firestore."""
    seed_attempt(db)
    worker, _, _ = worker_factory()
    fake = _scheduled(worker)
    failed = _task_reads_fail(monkeypatch, db, failures=2)

    assert worker.run() == ExitCode.OK

    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert len(failed) == 2
    # Nothing was written while the control plane could not be read: the
    # generation check had not passed, so nothing was this attempt's to write.
    assert failed == [0, 0], failed
    schedule = _schedule()
    assert fake.slept == _gaps(schedule)[:2], fake.slept

    warned = [
        r for r in _records(log_stream)
        if r["severity"] == "WARNING" and r.get("phase") == "validate_generation"
    ]
    assert [r.get("attempt") for r in warned] == [1, 2], warned
    for record in warned:
        assert record["attempts"] == len(schedule), record
        assert "ServiceUnavailable" in record["cause"] or "RetryError" in record["error_type"], record
        assert record["retry_in_seconds"] > 0, record


# ---------------------------------------------------------------------------
# an outage that outlasts every attempt
# ---------------------------------------------------------------------------


def test_an_outage_that_outlasts_every_attempt_exits_69_having_written_nothing(
    db, worker_factory, log_stream, monkeypatch
):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    fake = _scheduled(worker)
    failed = _task_reads_fail(monkeypatch, db, failures=None)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    assert worker.run() == UNAVAILABLE

    schedule = _schedule()
    assert len(failed) == len(schedule), f"{len(failed)} reads for {len(schedule)} attempts"
    assert fake.slept == _gaps(schedule), fake.slept
    assert db.writes == [], db.writes
    errors = [r for r in _records(log_stream) if r["severity"] == "ERROR"]
    last = errors[-1]
    assert last["phase"] == "validate_generation", last
    assert last["exit_code"] == UNAVAILABLE, last
    assert last["attempts"] == len(schedule), last
    # The verdict comes after the last attempt, not after the first.
    assert last["seconds"] >= schedule[-1], last


def test_a_sigterm_between_two_attempts_exits_at_once_having_written_nothing(
    db, worker_factory, log_stream, monkeypatch
):
    """The wait between attempts is still inside the window where nothing is written."""
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker.phases.hard_exit = _hard_exit
    fake = _scheduled(worker)
    _task_reads_fail(monkeypatch, db, failures=None)

    def sigterm() -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), handler
        handler(signal.SIGTERM, None)

    fake.on_sleep = sigterm

    with pytest.raises(_Exited) as exited:
        worker.run()

    assert exited.value.code == INTERRUPTED
    assert len(fake.slept) == 1, fake.slept
    assert db.writes == [], db.writes
    said = [r for r in _records(log_stream) if "SIGTERM" in r["message"]]
    assert said and said[-1]["phase"] == "validate_generation", said


# ---------------------------------------------------------------------------
# what is not retried
# ---------------------------------------------------------------------------


def test_a_refusal_ends_startup_on_the_first_read(db, worker_factory, monkeypatch):
    """PERMISSION_DENIED would be refused again: 78 at once, no wait."""
    from google.api_core import exceptions as core

    seed_attempt(db)
    worker, _, _ = worker_factory()
    fake = _scheduled(worker)
    failed = _task_reads_fail(
        monkeypatch, db, failures=None,
        error=lambda: core.PermissionDenied("Missing or insufficient permissions."),
    )

    assert worker.run() == CANNOT_START
    assert len(failed) == 1
    assert fake.slept == []


def test_a_fenced_attempt_is_not_retried(db, worker_factory):
    """A newer generation owns the task. Asking again cannot change that."""
    seed_attempt(db, task_generation=2)
    worker, _, _ = worker_factory()
    fake = _scheduled(worker)

    assert worker.run() == FENCED
    assert fake.slept == []


# ---------------------------------------------------------------------------
# the schedule
# ---------------------------------------------------------------------------


def test_the_retries_outlast_the_documented_direct_vpc_delay_and_stay_bounded():
    """Starts at 0, keeps asking past a minute, and gives its verdict within two.

    The worst case is every try hanging for its whole call timeout, so each
    attempt costs the full startup budget plus one call. The bound keeps the
    verdict well inside the lease's 300 s dispatch deadline for the cold starts
    measured on 2026-09-25 (dispatch to first line: 103 s and 195 s).
    """
    from agent_worker import startup

    schedule = _schedule()
    assert schedule[0] == 0
    assert list(schedule) == sorted(schedule)
    assert schedule[-1] >= DIRECT_VPC_STARTUP_DELAY_SECONDS
    per_attempt = startup.FIRESTORE_STARTUP_RETRY_SECONDS + startup.FIRESTORE_STARTUP_CALL_SECONDS
    end = 0.0
    for start in schedule:
        end = max(start, end) + per_attempt
    assert startup.CONTROL_PLANE_READ_WINDOW_SECONDS == end
    assert end <= 120, end


def test_the_dns_preflight_and_the_control_plane_read_share_one_retry_loop(
    db, worker_factory, monkeypatch
):
    """Owner's instruction for #198: extend the preflight's pattern, do not duplicate it."""
    from google.api_core import exceptions as core

    from agent_worker import startup

    real = getattr(startup, "run_on_schedule", None)
    schedules: list[tuple[float, ...]] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        schedules.append(tuple(kwargs.get("schedule") or ()))
        assert real is not None
        return real(*args, **kwargs)

    monkeypatch.setattr(startup, "run_on_schedule", recording, raising=False)

    fake = FakeTime()
    startup.dns_preflight_with_retries(
        ["firestore.googleapis.com"],
        resolve=lambda host: [(2, 1, 6, "", ("10.0.0.7", 443))],
        sleep=fake.sleep,
        clock=fake.clock,
    )
    seed_attempt(db)
    worker, _, _ = worker_factory()
    _scheduled(worker)
    _task_reads_fail(monkeypatch, db, failures=None, error=lambda: core.PermissionDenied("no"))
    worker.run()

    assert schedules == [
        tuple(startup.DNS_PREFLIGHT_SCHEDULE_SECONDS),
        tuple(startup.CONTROL_PLANE_READ_SCHEDULE_SECONDS),
    ], schedules
