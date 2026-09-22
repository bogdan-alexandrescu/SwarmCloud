"""The benchmark collectors, driven end to end, offline.

WHAT THIS COVERS THAT tests/unit CANNOT
---------------------------------------
`tests/unit/scripts/test_benchstat.py` proves the engine's arithmetic. It says
nothing about whether `scripts/bench-api.sh` actually feeds it the right
numbers -- and "both ends built, the seam between them never executed" is the
shape of every defect this repository spent three days finding.

So these run the REAL scripts, with the REAL curl, against a real HTTP server
on localhost whose latency the test controls to the millisecond. The whole
chain executes: argument parsing, the readiness gate, concurrent bursts,
curl's `-w` timing fields, the JSONL samples, the percentile engine, the
baseline comparison and the exit code.

Nothing here needs credentials, a cloud project or an emulator:

  * `API_URL` is honoured by `common.sh::api_url` ahead of everything else.
  * `SWARM_ID_TOKEN` is honoured by `common.sh::id_token` ahead of gcloud.
  * `SWARM_ENV_FILE=/dev/null` keeps the operator's own .env out of it.

THE POINT OF THE DELAY INJECTION
--------------------------------
A benchmark that cannot fail is not a benchmark. Each test here mutates the
thing under measurement -- adds 400ms to one route, makes one route answer 500
-- and asserts that the gate catches it, on that metric and not on its
neighbours. A regression detector that fires on everything is as useless as one
that fires on nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH_API = ROOT / "scripts" / "bench-api.sh"
BENCH_UI = ROOT / "scripts" / "bench-ui.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("curl") is None,
    reason="the collectors need jq and curl",
)

#: Every route answers with this baseline cost.
#:
#: 50ms, not 2ms, and the difference is a real finding rather than a fudge.
#: A RATIO THRESHOLD IS MEANINGLESS WHEN THE BASELINE IS AT THE NOISE FLOOR.
#: With a 2ms baseline, the ordinary jitter of spawning curl and scheduling a
#: thread is itself more than 1.5x, so the untouched routes in the delay test
#: failed the gate perfectly correctly -- they really had gone from 2.0ms to
#: 3.4ms -- and the assertion that a regression detector must not fire on its
#: neighbours went red about one run in three.
#:
#: That is the same trap in production: a 1.5x rule on a route that answers in
#: 3ms will fire forever, and the fix there is the same as the fix here -- put
#: the measurement somewhere the signal is large against the noise, or gate on
#: an absolute limit instead of a ratio. docs/benchmarks.md says so.
#:
#: At 50ms the +400ms injection is still a ~9x regression and a few
#: milliseconds of scheduler jitter is a few percent.
BASE_MS = 50.0

_BODIES = {
    "/readyz": {"status": "ok"},
    "/v1/stats": {
        "tenant_id": "bench",
        "tasks_by_state": {"SUCCEEDED": 1200, "QUEUED": 30},
        "platform_tasks_by_state": {"SUCCEEDED": 4000, "QUEUED": 90, "RUNNING": 10},
    },
    "/v1/capacity": {"pools": [{"name": "global", "active": 0}], "pools_complete": True},
    "/v1/providers": {"providers": {}},
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Set per-server by _serve().
    delay_ms = 0.0
    delay_route = ""
    fail_route = ""

    def do_GET(self):  # noqa: N802
        route = self.path.split("?", 1)[0]
        time.sleep(BASE_MS / 1000.0)
        if self.delay_ms and route == self.delay_route:
            time.sleep(self.delay_ms / 1000.0)
        if self.fail_route and route == self.fail_route:
            self._send(500, {"code": "injected", "message": "injected failure"})
            return
        body = _BODIES.get(route)
        if body is None:
            self._send(404, {"code": "not_found", "message": route})
            return
        self._send(200, body)

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # noqa: ARG002
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, **attrs):
        port = _free_port()
        handler = type("_H", (_Handler,), attrs)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


#: The threshold this fixture is gated with, and why it is not the shipped 1.5x.
#:
#: These tests compare two separate runs of a real process, with real curl, on
#: a laptop or a CI runner that is doing other things at the same time. The
#: signal being tested is the INJECTED 400ms -- roughly nine times the 50ms
#: baseline. The noise is whatever else the machine was doing, which on a busy
#: box moved a 50ms route to 80ms and failed the "an unchanged platform passes"
#: assertion outright.
#:
#: Three times is chosen so that the noise cannot reach it and the injection
#: cannot avoid it. That is the same judgement docs/benchmarks.md asks anyone
#: setting a real threshold to make: size it against the measurement's own
#: variance, and say so. Loosening it to hide a genuine regression would need
#: a factor above nine, which nobody would write without noticing.
#:
#: The SHIPPED thresholds in benchmarks/thresholds.json are asserted against
#: separately, in tests/unit/scripts/test_benchstat.py, where the inputs are
#: exact numbers and nothing is timed.
FIXTURE_THRESHOLDS = {
    "default": {"statistic": "p95", "max_regression_ratio": 3.0, "min_samples": 5},
}


def _env(url: str, baseline: Path, build: Path) -> dict:
    thresholds = build / "thresholds.json"
    thresholds.write_text(json.dumps(FIXTURE_THRESHOLDS))
    env = dict(os.environ)
    env.update(
        {
            "API_URL": url,
            # A placeholder. The fake server never looks at it; what matters is
            # that `id_token` takes this branch instead of shelling to gcloud,
            # so the suite needs no credentials.
            "SWARM_ID_TOKEN": "offline-test-token",
            "SWARM_ENV_FILE": "/dev/null",
            "ENVIRONMENT": "benchtest",
            "NO_COLOR": "1",
            "BENCH_BASELINE": str(baseline),
            "BENCH_THRESHOLDS": str(thresholds),
            "HTTP_TIMEOUT": "15",
        }
    )
    return env


def _run(script: Path, *args, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=300
    )


ROUTES = ("--route", "/readyz", "--route", "/v1/capacity", "--route", "/v1/stats")


class TestApiCollector:
    """scripts/bench-api.sh, against a server whose latency the test sets."""

    def test_a_healthy_run_records_a_baseline_and_passes_against_it(self, tmp_path):
        server = _Server()
        baseline = tmp_path / "baseline.json"
        env = _env(server.url, baseline, tmp_path)
        try:
            record = dict(env, BENCH_RECORD_BASELINE="1")
            first = _run(BENCH_API, "--rounds", "5", "--concurrency", "2", *ROUTES, env=record)
            assert first.returncode == 0, first.stderr[-3000:]
            assert baseline.exists()

            recorded = json.loads(baseline.read_text())
            assert "api.latency{route=/v1/stats}" in recorded["metrics"]
            # Rule 2: the shape the collectors hand over never carries a mean.
            for entry in recorded["metrics"].values():
                assert "mean" not in entry
                assert entry["n"] >= 5

            second = _run(BENCH_API, "--rounds", "5", "--concurrency", "2", *ROUTES, env=env)
            assert second.returncode == 0, second.stderr[-3000:]
            assert "0 failing" in second.stdout
        finally:
            server.close()

    def test_an_injected_400ms_delay_fails_the_gate_on_that_route_alone(self, tmp_path):
        """The proof that these benchmarks can fail.

        400ms on top of a 50ms baseline is a ~9x regression on one route,
        against a 3x threshold. The gate must catch it, and must NOT catch the
        routes beside it -- a detector that fires on everything is as useless
        as one that fires on nothing.
        """
        baseline = tmp_path / "baseline.json"

        fast = _Server()
        env = _env(fast.url, baseline, tmp_path)
        try:
            recorded = _run(
                BENCH_API,
                "--rounds", "5", "--concurrency", "2", *ROUTES,
                env=dict(env, BENCH_RECORD_BASELINE="1"),
            )
            assert recorded.returncode == 0, recorded.stderr[-3000:]
        finally:
            fast.close()

        slow = _Server(delay_ms=400.0, delay_route="/v1/stats")
        env = _env(slow.url, baseline, tmp_path)
        try:
            result = _run(BENCH_API, "--rounds", "5", "--concurrency", "2", *ROUTES, env=env)
        finally:
            slow.close()

        assert result.returncode == 1, "the injected delay did not fail the gate"
        lines = result.stdout.splitlines()
        failed = [ln for ln in lines if ln.startswith("FAIL")]
        assert failed, result.stdout

        assert any("api.latency{route=/v1/stats}" in ln for ln in failed)
        # Untouched routes stay green.
        assert not any("route=/readyz}" in ln for ln in failed), result.stdout
        assert not any("route=/v1/capacity}" in ln for ln in failed), result.stdout
        assert "regressed" in result.stdout

    def test_the_delay_also_moves_the_history_normalised_figure(self, tmp_path):
        """`api.stats.ms_per_1k_history` is the metric that separates a real
        /v1/stats regression from a collection that simply grew.

        The history size is identical between the two runs here, so the
        normalised figure must move with the latency -- which is what makes it
        usable as the gated one.
        """
        baseline = tmp_path / "baseline.json"
        fast = _Server()
        env = _env(fast.url, baseline, tmp_path)
        try:
            _run(
                BENCH_API, "--rounds", "5", "--concurrency", "1", "--route", "/v1/stats",
                env=dict(env, BENCH_RECORD_BASELINE="1"),
            )
        finally:
            fast.close()

        recorded = json.loads(baseline.read_text())
        history_key = "api.stats.history_tasks{route=/v1/stats,scope=platform}"
        assert history_key in recorded["metrics"]
        assert recorded["metrics"][history_key]["p50"] == 4100

        slow = _Server(delay_ms=400.0, delay_route="/v1/stats")
        env = _env(slow.url, baseline, tmp_path)
        try:
            result = _run(
                BENCH_API, "--rounds", "5", "--concurrency", "1", "--route", "/v1/stats", env=env
            )
        finally:
            slow.close()

        assert result.returncode == 1
        failed = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL")]
        assert any("api.stats.ms_per_1k_history" in ln for ln in failed), result.stdout
        # The history count itself did not change, so it must not be reported
        # as a regression.
        assert not any("api.stats.history_tasks" in ln for ln in failed), result.stdout

    def test_a_500_is_recorded_as_not_measured_rather_than_as_a_fast_request(self, tmp_path):
        """The single most important assertion in this file.

        An error answers in a couple of milliseconds. If the collector timed it
        like any other response, a totally broken route would set a new record
        on every run and the gate would go green as the platform failed.
        """
        baseline = tmp_path / "baseline.json"
        fast = _Server()
        env = _env(fast.url, baseline, tmp_path)
        try:
            _run(
                BENCH_API, "--rounds", "5", "--concurrency", "2",
                "--route", "/readyz", "--route", "/v1/capacity",
                env=dict(env, BENCH_RECORD_BASELINE="1"),
            )
        finally:
            fast.close()

        broken = _Server(fail_route="/v1/capacity")
        env = _env(broken.url, baseline, tmp_path)
        try:
            result = _run(
                BENCH_API, "--rounds", "5", "--concurrency", "2",
                "--route", "/readyz", "--route", "/v1/capacity", env=env,
            )
        finally:
            broken.close()

        assert result.returncode == 1
        assert "not_measured" in result.stdout
        assert "HTTP 500" in result.stdout
        failed = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL")]
        assert any("route=/v1/capacity}" in ln for ln in failed)
        # And it did NOT get a percentile.
        capacity_lines = [ln for ln in failed if "api.latency{route=/v1/capacity}" in ln]
        assert capacity_lines and "p95=--" in capacity_lines[0], result.stdout

    def test_an_unready_api_stops_the_run_instead_of_being_measured(self, tmp_path):
        """A readiness failure must not arrive as a page of odd percentiles.

        The assertion that matters is that NOTHING WAS MEASURED, not merely
        that the exit code is non-zero. A mutation replacing the gate's `die`
        with a `warn` still exits non-zero -- the samples all come back
        not-measured and the gate fails for a different reason -- and still
        prints the same sentence. Both of those assertions passed against a
        collector whose readiness gate had been removed. What separates the two
        worlds is whether the suite went on to time the routes.
        """
        broken = _Server(fail_route="/readyz")
        env = _env(broken.url, tmp_path / "b.json", tmp_path)
        try:
            result = _run(
                BENCH_API, "--rounds", "5", "--route", "/readyz", "--route", "/v1/capacity",
                env=env,
            )
        finally:
            broken.close()
        assert result.returncode != 0
        assert "readyz answered HTTP 500" in result.stderr
        assert "not 2xx" in result.stderr, "the gate did not refuse; it only warned"
        # It stopped before measuring anything: no verdict table, no metrics.
        assert "api.latency" not in result.stdout, result.stdout
        assert "failing" not in result.stdout, result.stdout


class TestBenchlibGuards:
    """`bench_sample`'s refusal to record a value it cannot read as a number.

    This guard is the mechanical form of the whole suite's honesty rule, and it
    is reachable only from shell -- so it is exercised here directly rather
    than through a collector. It was UNCAUGHT by the first version of this file:
    every collector happened to feed it well-formed numbers, so removing the
    guard changed nothing any test looked at.
    """

    def _drive(self, tmp_path, body: str) -> Path:
        script = tmp_path / "drive.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'source "{ROOT}/scripts/lib/common.sh"\n'
            f'source "{ROOT}/scripts/lib/benchlib.sh"\n'
            "load_env\n"
            "bench_init guardtest\n"
            f"{body}\n"
            'cp "${BENCH_SAMPLES}" "$1"\n'
        )
        script.chmod(0o755)
        out = tmp_path / "samples.jsonl"
        env = dict(os.environ)
        env.update(
            {"SWARM_ENV_FILE": "/dev/null", "ENVIRONMENT": "guardtest", "NO_COLOR": "1"}
        )
        result = subprocess.run(
            [str(script), str(out)], capture_output=True, text=True, env=env,
            cwd=str(ROOT), timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return out

    def _rows(self, path: Path):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_a_number_is_recorded_as_a_number(self, tmp_path):
        rows = self._rows(self._drive(tmp_path, 'bench_sample m ms 12.5 "{}"'))
        assert rows == [{"metric": "m", "unit": "ms", "value": 12.5, "labels": {}}]

    @pytest.mark.parametrize("bad", ['""', '"   "', '"n/a"', '"12ms"', '"1.2.3"', '"null"'])
    def test_a_value_that_is_not_a_number_becomes_a_not_measured_sample(self, tmp_path, bad):
        """An unread shell variable arrives as the empty string.

        Recording it as 0 would make a failed read the fastest measurement in
        the run -- the same shape as `[[ "" -le N ]]` being true for every
        bound, which is how testlib.sh once printed PASS having read nothing.
        """
        rows = self._rows(self._drive(tmp_path, f'bench_sample m ms {bad} "{{}}"'))
        assert len(rows) == 1
        assert rows[0]["value"] is None, f"{bad} was recorded as {rows[0]['value']}"
        assert "not a number" in rows[0]["not_measured"]

    def test_a_negative_number_is_kept_because_clock_skew_is_real(self, tmp_path):
        rows = self._rows(self._drive(tmp_path, 'bench_sample m s -3 "{}"'))
        assert rows[0]["value"] == -3

    def test_bench_missing_records_the_reason_verbatim(self, tmp_path):
        rows = self._rows(
            self._drive(tmp_path, 'bench_missing m ms "the pool could not be read" "{}"')
        )
        assert rows[0]["value"] is None
        assert rows[0]["not_measured"] == "the pool could not be read"

    def test_labels_round_trip_through_bench_label(self, tmp_path):
        rows = self._rows(
            self._drive(
                tmp_path,
                'labels="$(bench_label route /v1/stats scope platform)"\n'
                'bench_sample m ms 1 "${labels}"',
            )
        )
        assert rows[0]["labels"] == {"route": "/v1/stats", "scope": "platform"}

    def test_an_unreachable_api_is_a_transport_failure_not_a_slow_one(self, tmp_path):
        port = _free_port()  # nothing is listening on it
        env = _env(f"http://127.0.0.1:{port}", tmp_path / "b.json", tmp_path)
        result = _run(BENCH_API, "--rounds", "2", "--route", "/readyz", env=env)
        assert result.returncode != 0
        assert "transport failure" in result.stderr


class TestUiCollector:
    """scripts/bench-ui.sh's capture-to-samples half, with no browser.

    `--from-json` exists for exactly this: the browser half needs a daemon and
    a running UI, the translation half does not, and the translation half is
    where the honesty rules live.
    """

    def _capture(self, tmp_path, *rows) -> Path:
        path = tmp_path / "capture.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return path

    def _env(self, tmp_path, baseline="baseline.json"):
        env = dict(os.environ)
        env.update(
            {
                "SWARM_ENV_FILE": "/dev/null",
                "ENVIRONMENT": "benchtest",
                "NO_COLOR": "1",
                "BENCH_BASELINE": str(tmp_path / baseline),
                "BENCH_THRESHOLDS": str(tmp_path / "no-such-thresholds.json"),
            }
        )
        return env

    def test_a_live_capture_becomes_per_route_samples(self, tmp_path):
        rows = []
        for i in range(6):
            rows.append(
                {
                    "screen": "#overview/now",
                    "ok": True,
                    "nonce": f"n{i}",
                    "nav": {"ttfb_ms": 4.2, "dom_content_loaded_ms": 35.9, "load_ms": 41.2},
                    "first_contentful_paint_ms": 76,
                    "api": [
                        {"route": "/v1/stats", "duration": 1700.0 + i, "ttfb": 1690.0},
                        {"route": "/v1/capacity", "duration": 29.1 + i, "ttfb": 14.0},
                    ],
                }
            )
        capture = self._capture(tmp_path, *rows)
        env = dict(self._env(tmp_path), BENCH_RECORD_BASELINE="1")
        result = _run(BENCH_UI, "--from-json", str(capture), env=env)
        assert result.returncode == 0, result.stderr[-3000:]

        recorded = json.loads((tmp_path / "baseline.json").read_text())
        assert "ui.fetch{route=/v1/stats,screen=#overview/now}" in recorded["metrics"]
        assert "ui.fetch{route=/v1/capacity,screen=#overview/now}" in recorded["metrics"]
        assert "ui.paint.first_contentful{screen=#overview/now}" in recorded["metrics"]
        # The slow route keeps its own identity rather than being averaged in
        # with the fast one on the same screen.
        slow = recorded["metrics"]["ui.fetch{route=/v1/stats,screen=#overview/now}"]
        fast = recorded["metrics"]["ui.fetch{route=/v1/capacity,screen=#overview/now}"]
        assert slow["p95"] > 1000 and fast["p95"] < 100

    def test_a_fixture_run_reports_its_api_metrics_as_not_measured(self, tmp_path):
        """A fixture build makes no network request at all.

        Omitting the API metrics there would make a fixture run indistinguishable
        from a very fast live one -- the single most likely way this collector
        could lie.
        """
        rows = [
            {
                "screen": "#overview/now",
                "ok": True,
                "nonce": f"n{i}",
                "nav": {"ttfb_ms": 4.2, "dom_content_loaded_ms": 35.9, "load_ms": 41.2},
                "first_contentful_paint_ms": 76,
                "api": [],
            }
            for i in range(6)
        ]
        capture = self._capture(tmp_path, *rows)
        result = _run(BENCH_UI, "--from-json", str(capture), env=self._env(tmp_path))
        assert result.returncode == 1
        assert "issued no same-origin /v1 request" in result.stdout
        # The render metrics are still measured honestly.
        assert "ui.paint.first_contentful{screen=#overview/now}" in result.stdout

    def test_a_failed_probe_is_not_a_screen_that_loaded_instantly(self, tmp_path):
        rows = [
            {"screen": "#overview/now", "ok": False, "reason": "origin guard: this tab is null"}
            for _ in range(6)
        ]
        capture = self._capture(tmp_path, *rows)
        result = _run(BENCH_UI, "--from-json", str(capture), env=self._env(tmp_path))
        assert result.returncode == 1
        assert "origin guard" in result.stdout
        assert "p95=--" in result.stdout

    def test_an_empty_capture_is_refused_rather_than_reported_as_clean(self, tmp_path):
        capture = tmp_path / "empty.jsonl"
        capture.write_text("")
        result = _run(BENCH_UI, "--from-json", str(capture), env=self._env(tmp_path))
        assert result.returncode != 0
        assert "nothing was measured" in result.stderr


class TestBenchOrchestrator:
    def test_the_self_test_runs_offline_and_passes(self):
        """The same check `make test` runs. Asserted here too so a failure
        names the benchmark suite rather than appearing as a bare Makefile
        step."""
        env = dict(os.environ, SWARM_ENV_FILE="/dev/null", NO_COLOR="1")
        result = _run(ROOT / "scripts" / "bench.sh", "--self-test", env=env)
        assert result.returncode == 0, result.stderr[-3000:]
        assert "benchmark self-test passed" in result.stderr

    def test_an_unknown_suite_is_refused_by_name(self):
        env = dict(os.environ, SWARM_ENV_FILE="/dev/null", NO_COLOR="1")
        result = _run(ROOT / "scripts" / "bench.sh", "--suites", "nonsense", env=env)
        assert result.returncode != 0
        assert "unknown suite" in result.stderr
