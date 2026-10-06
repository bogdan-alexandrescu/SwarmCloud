"""The Cloud Monitoring export builds a request the real library accepts.

Observer P12 (2026-10-06): every attempt logged "metrics export failed" -- 518
warnings in one day -- because `CloudMonitoringExporter.export` built its
monitored resource from `monitoring_v3.MonitoredResource`, which
google-cloud-monitoring does not export. The AttributeError was raised before
any request existed, so no series was ever written and the composite exporter
logged it on every attempt.

What is pinned here, against the real library classes and a fake client that
captures the request (no network, no credentials):

  * the request is built at all, from the installed library;
  * every series carries a `generic_task` resource with the five labels that
    type requires, filled from the attempt's labels;
  * an exporter error is logged once and swallowed: it never raises into the
    attempt, never logs per series or per write, and the RPC is bounded by a
    timeout so a hung endpoint cannot hold the attempt's exit.

Imports are inside the tests so each one fails on its own.
"""

from __future__ import annotations

import io
import json

LABELS = {
    "tenant_id": "eng",
    "runner_profile": "claude-code",
    "resource_class": "medium",
    "backend": "cloud_run",
    "attempt_id": "att_123",
    "task_id": "task_456",
}


class CapturingClient:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def create_time_series(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self._fail is not None:
            raise self._fail


def _logger():
    from agent_worker.logs import StructuredLogger

    stream = io.StringIO()
    return StructuredLogger(stream=stream), stream


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _usage(with_cpu: bool = True):
    from agent_worker.metrics import ResourceUsage

    if not with_cpu:
        return ResourceUsage(peak_rss_bytes=1024, peak_disk_bytes=2048, oom_near_miss=True)
    return ResourceUsage(
        peak_rss_bytes=1024,
        peak_disk_bytes=2048,
        oom_near_miss=False,
        cpu_seconds=3.25,
        peak_cpu_cores=1.5,
        mean_cpu_cores=0.8,
        cpu_source="cgroup",
    )


def test_the_request_carries_a_generic_task_resource_built_from_the_real_library():
    from google.cloud import monitoring_v3

    from agent_worker.metrics import METRIC_PREFIX, CloudMonitoringExporter

    log, stream = _logger()
    client = CapturingClient()
    CloudMonitoringExporter("proj-x", "europe-west1", log, client=client).export(
        _usage(), dict(LABELS)
    )

    # Memory/disk in one write, CPU in a second; nothing was logged as failed.
    assert len(client.calls) == 2
    assert not [l for l in _lines(stream) if l.get("severity") == "WARNING"]

    series = [s for call in client.calls for s in call["time_series"]]
    assert {s.metric.type for s in series} == {
        f"{METRIC_PREFIX}/{n}"
        for n in (
            "peak_rss_bytes",
            "peak_disk_bytes",
            "oom_near_miss",
            "cpu_time_ms",
            "peak_cpu_millicores",
            "mean_cpu_millicores",
        )
    }
    for call in client.calls:
        assert call["name"] == "projects/proj-x"
        # Bounded: a hung endpoint must not hold the attempt's exit.
        assert 0 < call["timeout"] <= 30
    for s in series:
        assert isinstance(s, monitoring_v3.TimeSeries)
        assert s.resource.type == "generic_task"
        assert dict(s.resource.labels) == {
            "project_id": "proj-x",
            "location": "europe-west1",
            "namespace": "eng",
            "job": "claude-code",
            "task_id": "att_123",
        }
        assert dict(s.metric.labels) == {
            "tenant_id": "eng",
            "runner_profile": "claude-code",
            "resource_class": "medium",
            "backend": "cloud_run",
        }
        # The series serialises: the request the client would send is valid.
        assert monitoring_v3.TimeSeries.serialize(s)

    by_type = {s.metric.type.rsplit("/", 1)[1]: s.points[0].value.int64_value for s in series}
    assert by_type["peak_rss_bytes"] == 1024
    assert by_type["cpu_time_ms"] == 3250
    assert by_type["peak_cpu_millicores"] == 1500


def test_missing_labels_fall_back_to_unknown_rather_than_an_empty_label():
    from agent_worker.metrics import CloudMonitoringExporter

    log, _ = _logger()
    client = CapturingClient()
    CloudMonitoringExporter("proj-x", "europe-west1", log, client=client).export(
        _usage(with_cpu=False), {}
    )
    assert len(client.calls) == 1
    resource = client.calls[0]["time_series"][0].resource
    assert resource.labels["namespace"] == "unknown"
    assert resource.labels["job"] == "unknown"
    assert resource.labels["task_id"] == "unknown"


def test_an_exporter_error_is_logged_once_and_swallowed():
    from agent_worker.metrics import CloudMonitoringExporter

    log, stream = _logger()
    client = CapturingClient(fail=RuntimeError("permission denied"))
    exporter = CloudMonitoringExporter("proj-x", "europe-west1", log, client=client)

    exporter.export(_usage(), dict(LABELS))  # must not raise
    exporter.export(_usage(), dict(LABELS))  # a retry by the crash path: still quiet

    warnings = [l for l in _lines(stream) if l.get("severity") == "WARNING"]
    assert len(warnings) == 1, warnings
    assert warnings[0]["message"] == "metrics export failed"
    assert "permission denied" in warnings[0]["error"]


def test_a_failing_client_constructor_is_swallowed_too():
    from agent_worker.metrics import CloudMonitoringExporter

    class Exploding(CloudMonitoringExporter):
        def _get_client(self):
            raise RuntimeError("no default credentials")

    log, stream = _logger()
    Exploding("proj-x", "europe-west1", log).export(_usage(), dict(LABELS))

    warnings = [l for l in _lines(stream) if l.get("severity") == "WARNING"]
    assert len(warnings) == 1
    assert "no default credentials" in warnings[0]["error"]


def test_the_composite_logs_a_cloud_failure_once_and_still_writes_the_log_line():
    from agent_worker.metrics import (
        CloudMonitoringExporter,
        CompositeExporter,
        LoggingMetricsExporter,
    )

    log, stream = _logger()
    client = CapturingClient(fail=RuntimeError("quota exceeded"))
    composite = CompositeExporter(
        [
            LoggingMetricsExporter(log),
            CloudMonitoringExporter("proj-x", "europe-west1", log, client=client),
        ],
        log,
    )
    composite.export(_usage(), dict(LABELS))

    lines = _lines(stream)
    assert [l["message"] for l in lines].count("attempt resource usage") == 1
    warnings = [l for l in lines if l.get("severity") == "WARNING"]
    assert len(warnings) == 1, warnings
