"""A park or a terminal write is recorded BEFORE the attempt's metrics are exported.

Measured on mock execution swarm-job-smoke-mock-sk8h9 (2026-10-06 06:05):
`_park_for_quota` ran checkpoint -> upload -> `_export_metrics` ->
update_quota_state -> control.park, and 53 s passed between the "attempt
resource usage" log line and the broker POST with nothing logged (across 24
other quota parks the same span was 1.3-3.1 s). The likely cause was the Cloud
Monitoring client's construction, which had no timeout. For those 53 s the task
was still LEASED and held its slot, invariant 3's unit of concurrency, for
telemetry.

Pinned here, through `Worker.run()` with spies on the control plane and the
exporter:

  * on a quota park, a successful finish and a failed finish, the broker
    update and the park/finish are written before the exporter is called;
  * an exporter that hangs does not delay the park at all, and does not hold
    the worker's exit beyond its own timeout;
  * an exporter that raises after the park cannot turn the PARKED task into a
    FAILED one through the crash path.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt

#: The writes that record how an attempt ended, and the broker update.
RECORDS = ("park", "finish", "fail_retryably", "park_awaiting_children", "update_quota_state")


def _spy(worker: Any, order: list[tuple[str, float]]) -> None:
    """Record each write when it RETURNS, and the export when it is CALLED."""
    for name in RECORDS:
        real = getattr(worker.control, name)

        def wrapped(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            out = _real(*args, **kwargs)
            order.append((_name, time.monotonic()))
            return out

        setattr(worker.control, name, wrapped)

    real_export = worker.metrics.export

    def export(*args: Any, **kwargs: Any) -> Any:
        order.append(("export", time.monotonic()))
        return real_export(*args, **kwargs)

    worker.metrics.export = export


def _names(order: list[tuple[str, float]]) -> list[str]:
    return [name for name, _ in order]


def _assert_recorded_before_export(order: list[tuple[str, float]], record: str) -> None:
    names = _names(order)
    assert "export" in names, f"the exporter was never called: {names}"
    assert record in names, f"{record} was never written: {names}"
    first_export = names.index("export")
    # EVERY record, not just the first: none of them may wait on telemetry.
    recorded = [i for i, name in enumerate(names) if name in RECORDS]
    assert recorded and max(recorded) < first_export, (
        f"metrics were exported before the attempt's end was recorded: {names}"
    )


def _quota_task(db: Any) -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "burn quota",
            "steps": 1,
            "sleep_seconds": 0.05,
            "quota_exhausted": True,
            "retry_after_seconds": 1800,
        },
        simulated={"provider": "anthropic"},
    )


def test_a_quota_park_and_its_broker_update_are_written_before_the_export(db, worker_factory):
    _quota_task(db)
    worker, _, exporter = worker_factory()
    order: list[tuple[str, float]] = []
    _spy(worker, order)

    assert worker.run() == ExitCode.PARKED

    names = _names(order)
    _assert_recorded_before_export(order, "park")
    assert "update_quota_state" in names, names
    assert names.index("update_quota_state") < names.index("park") < names.index("export")
    assert len(exporter.exports) == 1


def test_a_successful_finish_is_written_before_the_export(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "do the thing", "steps": 1, "sleep_seconds": 0.05})
    worker, _, exporter = worker_factory()
    order: list[tuple[str, float]] = []
    _spy(worker, order)

    assert worker.run() == ExitCode.OK

    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    _assert_recorded_before_export(order, "finish")
    assert len(exporter.exports) == 1


def test_a_failed_finish_is_written_before_the_export(db, worker_factory):
    seed_attempt(
        db,
        task_input={"prompt": "fail", "steps": 1, "sleep_seconds": 0.05, "fail": True},
    )
    worker, _, exporter = worker_factory()
    order: list[tuple[str, float]] = []
    _spy(worker, order)

    worker.run()

    assert db.doc("tasks/task_1")["state"] in (TaskState.FAILED.value, TaskState.READY.value)
    names = _names(order)
    record = "finish" if "finish" in names else "fail_retryably"
    _assert_recorded_before_export(order, record)
    assert len(exporter.exports) == 1


def test_a_hanging_cloud_exporter_does_not_delay_the_park(db, worker_factory):
    """The 53 s of 2026-10-06, replayed with a client that never comes back."""
    from agent_worker.metrics import (
        CloudMonitoringExporter,
        CompositeExporter,
        LoggingMetricsExporter,
    )

    released = threading.Event()

    class Hanging(CloudMonitoringExporter):
        def _build_client(self) -> Any:
            released.wait(60)
            raise RuntimeError("released by the test")

    _quota_task(db)
    worker, _, _ = worker_factory()
    bound = 0.5
    worker.metrics = CompositeExporter(
        [
            LoggingMetricsExporter(worker.log),
            Hanging("proj-x", "us-central1", worker.log, timeout_seconds=bound),
        ],
        worker.log,
    )
    order: list[tuple[str, float]] = []
    _spy(worker, order)

    try:
        started = time.monotonic()
        assert worker.run() == ExitCode.PARKED
        ended = time.monotonic()
    finally:
        released.set()

    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    _assert_recorded_before_export(order, "park")
    parked_at = dict(order)["park"]
    # The park never waited on the exporter: it was written before the
    # exporter was even called, so the hang is entirely after it ...
    assert parked_at <= dict(order)["export"]
    # ... and the hang holds the exit for no more than the exporter's bound
    # (plus the worker's own cleanup), never for the 60 s it would have hung.
    assert ended - parked_at < bound + 10, ended - parked_at
    assert ended - started < 50


def test_an_exporter_that_raises_after_the_park_leaves_the_task_parked(db, worker_factory, log_stream):
    class Raising:
        def export(self, usage: Any, labels: dict[str, str]) -> None:
            raise RuntimeError("monitoring is down")

    _quota_task(db)
    worker, _, _ = worker_factory()
    worker.metrics = Raising()

    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    assert "monitoring is down" in log_stream.getvalue()
