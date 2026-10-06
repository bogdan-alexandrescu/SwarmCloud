"""The Cloud Monitoring exporter is bounded: construction and every write time out.

Measured on mock execution swarm-job-smoke-mock-sk8h9 (2026-10-06 06:05): 53 s
with nothing logged between the "attempt resource usage" line and the next
write of a quota park, against 1.3-3.1 s across 24 other parks. The likely
cause was `CloudMonitoringExporter._get_client` building a
`MetricServiceClient` (credential discovery, a channel) with no timeout at all.

Pinned here, with no network and no credentials:

  * a client construction that never returns costs the caller the exporter's
    timeout, is logged once, and is swallowed;
  * a write that never returns costs the caller one timeout -- not one per
    write -- is logged once, and is swallowed;
  * every write carries the same bound as its RPC deadline;
  * the default bound is 5 s.
"""

from __future__ import annotations

import io
import json
import threading
import time
from typing import Any

from test_metrics_export_resource import LABELS, _usage


def _logger():
    from agent_worker.logs import StructuredLogger

    stream = io.StringIO()
    return StructuredLogger(stream=stream), stream


def _warnings(stream: io.StringIO) -> list[dict]:
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return [line for line in lines if line.get("severity") == "WARNING"]


def test_the_default_bound_is_five_seconds():
    from agent_worker.metrics import EXPORT_TIMEOUT_SECONDS, CloudMonitoringExporter

    log, _ = _logger()
    assert EXPORT_TIMEOUT_SECONDS == 5.0
    assert CloudMonitoringExporter("proj-x", "europe-west1", log).timeout_seconds == 5.0


def test_a_client_construction_that_hangs_costs_one_timeout_and_one_warning():
    from agent_worker.metrics import CloudMonitoringExporter

    released = threading.Event()

    class Hanging(CloudMonitoringExporter):
        def _build_client(self) -> Any:
            released.wait(60)
            raise RuntimeError("released by the test")

    log, stream = _logger()
    exporter = Hanging("proj-x", "europe-west1", log, timeout_seconds=0.3)
    try:
        started = time.monotonic()
        exporter.export(_usage(), dict(LABELS))  # must not raise
        elapsed = time.monotonic() - started
    finally:
        released.set()

    assert elapsed < 5, elapsed
    warnings = _warnings(stream)
    assert len(warnings) == 1, warnings
    assert warnings[0]["message"] == "metrics export failed"
    assert warnings[0]["write"] == "client"
    assert "0.3" in warnings[0]["error"]


def test_a_write_that_hangs_costs_one_timeout_not_one_per_write():
    from agent_worker.metrics import CloudMonitoringExporter

    released = threading.Event()

    class HangingClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def create_time_series(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)
            released.wait(60)

    log, stream = _logger()
    client = HangingClient()
    exporter = CloudMonitoringExporter(
        "proj-x", "europe-west1", log, client=client, timeout_seconds=0.3
    )
    try:
        started = time.monotonic()
        exporter.export(_usage(with_cpu=True), dict(LABELS))  # must not raise
        elapsed = time.monotonic() - started
    finally:
        released.set()

    assert elapsed < 0.3 * 2, elapsed
    # The memory write hung; the CPU write was not tried against the same
    # hung endpoint.
    assert len(client.calls) == 1
    assert client.calls[0]["timeout"] == 0.3
    warnings = _warnings(stream)
    assert len(warnings) == 1, warnings
    assert warnings[0]["write"] == "memory"
