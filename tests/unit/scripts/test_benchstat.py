"""The benchmark engine's decisions, exhaustively.

WHY THIS FILE IS LONGER THAN THE THING IT TESTS
-----------------------------------------------
`scripts/benchstat.py` is where every benchmark verdict is made: whether a
number is a regression, whether a percentile is trustworthy, and -- the part
that matters most -- whether a measurement that never happened is allowed to
look like a fast one. The collectors around it are shell scripts that need a
deployed platform; this is the half that can be tested offline, so it is the
half that must be tested hard.

Each rule below is stated as the failure it prevents, because that is how these
get read in six months when somebody wants to relax one.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BENCHSTAT = ROOT / "scripts" / "benchstat.py"


def _load():
    spec = importlib.util.spec_from_file_location("benchstat", BENCHSTAT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bs = _load()


def summary_of(*values, metric="m", unit="ms", missing=0, reason="unreadable"):
    samples = [{"metric": metric, "unit": unit, "value": v} for v in values]
    samples += [
        {"metric": metric, "unit": unit, "value": None, "not_measured": reason}
        for _ in range(missing)
    ]
    return bs.summarize(samples)


def baseline_of(value, metric="m", unit="ms", n=10):
    return {"metrics": {metric: {"status": "ok", "p95": value, "unit": unit, "n": n}}}


def statuses(result):
    return [v["status"] for v in result["verdicts"]]


def assert_fails(result, *expected):
    """The status AND the consequence.

    Asserting only the status is not enough, and that is not a theoretical
    worry: a mutation that removed `missing_from_current` from `FAILING` --
    leaving the verdict correctly LABELLED but no longer failing the build --
    went UNCAUGHT by the first version of this file. The label is a report; the
    exit code is the gate. Every test that expects a failure checks both.
    """
    assert statuses(result) == list(expected), statuses(result)
    assert result["failed"] == len(expected), result


def assert_passes(result):
    assert result["failed"] == 0, result


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_nearest_rank_returns_a_value_that_was_actually_measured(self):
        """An interpolated p99 reports a latency no request ever experienced.

        That matters here because the percentile sits next to "and this was the
        slowest task"; a number in between two real ones cannot be chased.
        """
        values = [10.0, 20.0, 30.0]
        for q in (1, 25, 50, 75, 99, 100):
            assert bs.percentile(values, q) in values

    @pytest.mark.parametrize(
        "q,expected",
        [(10, 1.0), (50, 5.0), (90, 9.0), (95, 10.0), (99, 10.0), (100, 10.0)],
    )
    def test_known_ranks(self, q, expected):
        assert bs.percentile([float(i) for i in range(1, 11)], q) == expected

    def test_single_sample(self):
        assert bs.percentile([7.0], 99) == 7.0

    def test_empty_raises_rather_than_returning_zero(self):
        """Returning 0.0 here is the entire bug class this file exists for."""
        with pytest.raises(ValueError):
            bs.percentile([], 50)

    @pytest.mark.parametrize("q", [0, -1, 101, 1000])
    def test_out_of_range_q_raises(self, q):
        with pytest.raises(ValueError):
            bs.percentile([1.0], q)


# ---------------------------------------------------------------------------
# Rule 1 -- a failed measurement is not a fast one
# ---------------------------------------------------------------------------


class TestNotMeasured:
    def test_all_missing_has_no_percentile_at_all(self):
        s = summary_of(missing=5)["metrics"]["m"]
        assert s["status"] == "not_measured"
        assert s["n"] == 0
        assert s["missing"] == 5
        for field in ("p50", "p95", "p99", "min", "max"):
            assert field not in s, f"{field} must not exist when nothing was measured"

    def test_all_missing_fails_against_a_baseline(self):
        assert_fails(bs.compare(summary_of(missing=3), baseline_of(100.0)), "not_measured")

    def test_the_reason_survives_into_the_verdict(self):
        """An operator must not have to guess why a number is absent."""
        result = bs.compare(
            summary_of(missing=2, reason="HTTP 500 from /v1/providers"),
            baseline_of(100.0),
        )
        assert "HTTP 500 from /v1/providers" in result["verdicts"][0]["detail"]

    def test_missing_samples_do_not_drag_the_percentile_down(self):
        """The nulls must not be folded in as zeroes.

        This is the concrete arithmetic of the rule: nine 100ms samples and one
        failure is a p95 of 100, not of 90.
        """
        s = summary_of(*([100.0] * 9), missing=1)["metrics"]["m"]
        assert s["p50"] == 100.0
        assert s["p95"] == 100.0
        assert s["n"] == 9
        assert s["missing"] == 1

    def test_a_metric_with_no_samples_and_no_baseline_is_new_not_a_failure(self):
        result = bs.compare({"metrics": {"m": bs.summarize_values([], missing=0)}}, None)
        assert statuses(result) == ["new"]
        assert_passes(result)


class TestMissingFraction:
    def test_a_run_that_lost_a_third_of_its_samples_fails(self):
        assert_fails(
            bs.compare(summary_of(*([1.0] * 10), missing=5), baseline_of(1.0)),
            "too_many_missing",
        )

    def test_a_run_that_lost_one_in_ten_passes_at_the_default(self):
        result = bs.compare(summary_of(*([1.0] * 18), missing=2), baseline_of(1.0))
        assert statuses(result) == ["ok"]
        assert_passes(result)

    def test_the_limit_is_configurable_per_metric(self):
        current = summary_of(*([1.0] * 18), missing=2)
        thresholds = {"metrics": {"m": {"max_missing_fraction": 0.05}}}
        assert_fails(bs.compare(current, baseline_of(1.0), thresholds), "too_many_missing")

    def test_it_can_be_switched_off(self):
        current = summary_of(*([1.0] * 2), missing=8)
        thresholds = {"metrics": {"m": {"max_missing_fraction": None, "min_samples": 1}}}
        assert_passes(bs.compare(current, baseline_of(1.0), thresholds))

    def test_the_detail_names_the_count_and_the_reason(self):
        result = bs.compare(
            summary_of(*([1.0] * 5), missing=5, reason="the attempts could not be read"),
            baseline_of(1.0),
        )
        detail = result["verdicts"][0]["detail"]
        assert "5 of 10" in detail
        assert "the attempts could not be read" in detail


# ---------------------------------------------------------------------------
# Rule 2 -- no mean
# ---------------------------------------------------------------------------


class TestNoMean:
    def test_summaries_never_carry_a_mean(self):
        s = summary_of(1.0, 2.0, 1000.0)["metrics"]["m"]
        assert "mean" not in s
        assert "avg" not in s
        assert "average" not in s

    def test_the_tail_is_always_reported(self):
        s = summary_of(*([1.0] * 99), 1000.0)["metrics"]["m"]
        assert s["p50"] == 1.0
        assert s["max"] == 1000.0
        assert s["n"] == 100


# ---------------------------------------------------------------------------
# Rule 3 -- a percentile from two samples is a lie
# ---------------------------------------------------------------------------


class TestMinSamples:
    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_below_the_default_minimum_fails(self, n):
        assert_fails(
            bs.compare(summary_of(*([1.0] * n)), baseline_of(1.0)), "insufficient_samples"
        )

    def test_at_the_minimum_passes(self):
        assert_passes(bs.compare(summary_of(*([1.0] * 5)), baseline_of(1.0)))

    def test_a_gauge_can_lower_it_deliberately(self):
        thresholds = {"metrics": {"m": {"min_samples": 1}}}
        assert_passes(bs.compare(summary_of(1.0), baseline_of(1.0), thresholds))


# ---------------------------------------------------------------------------
# Rule 4 -- baselines
# ---------------------------------------------------------------------------


class TestBaselineComparison:
    def test_an_identical_run_passes(self):
        assert_passes(bs.compare(summary_of(*([100.0] * 10)), baseline_of(100.0)))

    def test_a_faster_run_passes(self):
        assert_passes(bs.compare(summary_of(*([10.0] * 10)), baseline_of(100.0)))

    @pytest.mark.parametrize("factor", [1.51, 2, 10, 100])
    def test_a_regression_beyond_the_threshold_fails(self, factor):
        current = summary_of(*([100.0 * factor] * 10))
        assert_fails(bs.compare(current, baseline_of(100.0)), "regressed")

    def test_just_inside_the_threshold_passes(self):
        assert_passes(bs.compare(summary_of(*([149.0] * 10)), baseline_of(100.0)))

    def test_the_ratio_is_reported_so_the_size_is_legible(self):
        result = bs.compare(summary_of(*([300.0] * 10)), baseline_of(100.0))
        assert result["verdicts"][0]["ratio"] == 3.0
        assert "3.0x" in result["verdicts"][0]["detail"]

    def test_a_metric_the_baseline_knows_and_the_run_does_not_fails(self):
        """The collector stopped running, or the path stopped executing.

        Both are exactly what a benchmark is for, and both would otherwise be
        silence.
        """
        assert_fails(
            bs.compare({"metrics": {}}, baseline_of(100.0, metric="gone")),
            "missing_from_current",
        )

    def test_a_metric_with_no_baseline_is_new_and_does_not_fail_the_build(self):
        """Otherwise no collector could ever be added without a red build."""
        result = bs.compare(summary_of(*([1.0] * 10)), {"metrics": {}})
        assert statuses(result) == ["new"]
        assert_passes(result)

    def test_a_zero_baseline_does_not_divide_by_zero(self):
        result = bs.compare(summary_of(*([1.0] * 10)), baseline_of(0.0))
        assert_fails(result, "regressed")
        assert result["verdicts"][0]["ratio"] is None

    def test_a_not_measured_baseline_is_not_compared_against(self):
        baseline = {"metrics": {"m": {"status": "not_measured", "n": 0, "missing": 4}}}
        result = bs.compare(summary_of(*([1.0] * 10)), baseline)
        assert statuses(result) == ["new"]
        assert_passes(result)


class TestAbsoluteMax:
    def test_over_the_absolute_limit_fails_even_with_no_baseline(self):
        thresholds = {"metrics": {"m": {"absolute_max": 10}}}
        assert_fails(bs.compare(summary_of(*([50.0] * 10)), None, thresholds), "over_absolute_max")

    def test_the_reconciler_backend_error_rule_is_zero_tolerance(self):
        """A pass that skipped a backend is FASTER and examines LESS.

        Every duration and count threshold rewards it; this is the only check
        that fails it, so it is worth asserting against the real thresholds
        file rather than a made-up one.
        """
        thresholds = json.loads((ROOT / "benchmarks" / "thresholds.json").read_text())
        clean = bs.summarize(
            [{"metric": "reconcile.backend_errors", "value": 0, "labels": {"dry_run": "false"}}]
        )
        assert_passes(bs.compare(clean, None, thresholds))

        skipped = bs.summarize(
            [{"metric": "reconcile.backend_errors", "value": 1, "labels": {"dry_run": "false"}}]
        )
        assert_fails(bs.compare(skipped, None, thresholds), "over_absolute_max")


class TestNegativeDurations:
    def test_a_negative_sample_is_counted_not_clamped(self):
        s = summary_of(-5.0, 1.0, 1.0)["metrics"]["m"]
        assert s["anomalies"] == 1
        assert s["min"] == -5.0, "clamping would hide clock skew as a fast dispatch"

    def test_a_negative_sample_fails_the_gate(self):
        current = summary_of(-5.0, *([1.0] * 9))
        assert_fails(bs.compare(current, baseline_of(1.0)), "anomalous_samples")

    def test_the_detail_names_clock_skew_rather_than_blaming_the_platform(self):
        result = bs.compare(summary_of(-5.0, *([1.0] * 9)), baseline_of(1.0))
        assert "skew" in result["verdicts"][0]["detail"]


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class TestThresholdResolution:
    def test_precedence_is_default_then_base_name_then_exact_key(self):
        thresholds = {
            "default": {"min_samples": 2},
            "metrics": {
                "m": {"min_samples": 3, "absolute_max": 99},
                "m{route=/x}": {"min_samples": 4},
            },
        }
        assert bs.threshold_for("m{route=/x}", thresholds)["min_samples"] == 4
        assert bs.threshold_for("m{route=/x}", thresholds)["absolute_max"] == 99
        assert bs.threshold_for("m{route=/y}", thresholds)["min_samples"] == 3
        assert bs.threshold_for("other", thresholds)["min_samples"] == 2

    def test_a_base_name_rule_reaches_label_values_nobody_listed(self):
        """Routes, profiles and screens are not known until the run produces them.

        A thresholds file keyed only by exact match would silently stop applying
        the moment a new label value appeared, which is the same as having no
        rule.
        """
        current = bs.summarize(
            [{"metric": "api.latency", "value": 900.0, "labels": {"route": "/v1/brand-new"}}] * 10
        )
        thresholds = {"metrics": {"api.latency": {"absolute_max": 300}}}
        assert_fails(bs.compare(current, None, thresholds), "over_absolute_max")

    def test_a_null_ratio_disables_the_ratio_gate(self):
        """Needed for a gauge that only grows and for throughput.

        The engine compares in one direction. A huge number would work and
        would read as an oversight; null states the intent.
        """
        current = summary_of(*([1000.0] * 10))
        thresholds = {"metrics": {"m": {"max_regression_ratio": None}}}
        assert_passes(bs.compare(current, baseline_of(1.0), thresholds))

    def test_the_shipped_thresholds_file_is_valid_and_every_rule_carries_a_reason(self):
        thresholds = json.loads((ROOT / "benchmarks" / "thresholds.json").read_text())
        assert thresholds["default"]["statistic"] == "p95"
        for name, rule in thresholds["metrics"].items():
            assert rule.get("_why"), f"{name} has a threshold with no stated reason"

    def test_every_metric_the_collectors_emit_is_covered_or_defaulted(self):
        """Not that every metric is listed -- that resolution never crashes."""
        thresholds = json.loads((ROOT / "benchmarks" / "thresholds.json").read_text())
        for name in (
            "api.latency{route=/v1/stats}",
            "api.ttfb{route=/readyz}",
            "dispatch.cold_start{backend=cloud_run,runner_profile=mock}",
            "dispatch.segment.ready_to_leased{backend=gke,runner_profile=codex}",
            "cost.usd_per_task{runner_profile=claude-code}",
            "ui.fetch{route=/v1/tasks,screen=#overview/now}",
            "reconcile.backend_errors{dry_run=false}",
        ):
            rule = bs.threshold_for(name, thresholds)
            assert "statistic" in rule and "min_samples" in rule


# ---------------------------------------------------------------------------
# Event decomposition
# ---------------------------------------------------------------------------


class TestSegments:
    REAL_RUN = [
        {"type": "submitted", "at": "2026-09-22T03:46:00Z"},
        {"type": "queued", "at": "2026-09-22T03:46:01Z"},
        {"type": "ready", "at": "2026-09-22T03:46:02Z"},
        {"type": "lease_acquired", "at": "2026-09-22T03:46:04Z"},
        {"type": "dispatched", "at": "2026-09-22T03:46:05Z"},
        {"type": "starting", "at": "2026-09-22T03:49:14Z"},
        {"type": "running", "at": "2026-09-22T03:49:14Z"},
        {"type": "succeeded", "at": "2026-09-22T03:49:32Z"},
    ]

    def by_metric(self, events, **kw):
        return {s["metric"]: s["value"] for s in bs.segment_samples(events, **kw)}

    def test_the_2026_09_22_run_decomposes_as_recorded(self):
        got = self.by_metric(self.REAL_RUN)
        assert got["dispatch.segment.dispatched_to_starting"] == 189.0
        assert got["dispatch.cold_start"] == 189.0
        assert got["dispatch.segment.ready_to_leased"] == 2.0
        assert got["dispatch.segment.starting_to_running"] == 0.0
        assert got["dispatch.total"] == 212.0

    def test_cold_start_is_not_the_sum_of_its_halves_by_construction(self):
        """Percentiles cannot be added, so the whole is measured separately."""
        names = [name for name, _, _ in bs.SEGMENTS]
        assert "dispatched_to_starting" not in bs.COLD_START[0]
        assert bs.COLD_START[1] == "dispatched" and bs.COLD_START[2] == "running"
        assert "dispatched_to_starting" in names and "starting_to_running" in names

    def test_a_missing_endpoint_produces_a_null_sample_not_no_sample(self):
        """A task that never reached RUNNING is not a fast cold start.

        An absent sample shrinks n and the run still passes. A null sample
        makes the metric not-measured and the run fails.
        """
        samples = bs.segment_samples([{"type": "submitted", "at": "2026-09-22T03:46:00Z"}])
        cold = [s for s in samples if s["metric"] == "dispatch.cold_start"]
        assert len(cold) == 1
        assert cold[0]["value"] is None
        assert "dispatched" in cold[0]["not_measured"]

    def test_every_segment_is_emitted_even_for_an_empty_event_list(self):
        samples = bs.segment_samples([])
        assert len(samples) == len(bs.SEGMENTS) + 2  # + cold_start + total
        assert all(s["value"] is None for s in samples)

    def test_the_earliest_event_wins_so_a_retry_is_not_measured_as_a_cold_start(self):
        events = list(self.REAL_RUN) + [
            {"type": "dispatched", "at": "2026-09-22T04:30:00Z"},
            {"type": "running", "at": "2026-09-22T04:40:00Z"},
        ]
        assert self.by_metric(events)["dispatch.cold_start"] == 189.0

    def test_the_first_terminal_event_ends_the_total(self):
        events = [
            {"type": "submitted", "at": "2026-09-22T03:46:00Z"},
            {"type": "failed", "at": "2026-09-22T03:47:00Z"},
            {"type": "succeeded", "at": "2026-09-22T03:50:00Z"},
        ]
        assert self.by_metric(events)["dispatch.total"] == 60.0

    def test_labels_travel_with_every_sample(self):
        labels = {"runner_profile": "claude-code", "backend": "cloud_run"}
        for sample in bs.segment_samples(self.REAL_RUN, labels=labels):
            assert sample["labels"] == labels

    def test_an_unparseable_timestamp_is_missing_not_zero(self):
        events = [
            {"type": "submitted", "at": "not a timestamp"},
            {"type": "succeeded", "at": "2026-09-22T03:50:00Z"},
        ]
        assert self.by_metric(events)["dispatch.total"] is None

    def test_clock_skew_survives_into_the_sample(self):
        events = [
            {"type": "dispatched", "at": "2026-09-22T03:46:05Z"},
            {"type": "running", "at": "2026-09-22T03:46:00Z"},
        ]
        assert self.by_metric(events)["dispatch.cold_start"] == -5.0

    def test_naive_timestamps_are_read_as_utc_rather_than_local(self):
        """A local-time reading would shift every segment by the TZ offset."""
        events = [
            {"type": "dispatched", "at": "2026-09-22T03:46:05"},
            {"type": "running", "at": "2026-09-22T03:49:14Z"},
        ]
        assert self.by_metric(events)["dispatch.cold_start"] == 189.0

    def test_the_event_names_are_the_frozen_contract_s_own(self):
        """The chain is restated here because the collectors run in an image
        with no swarm_common. This is the parity check for that restatement."""
        sys.path.insert(0, str(ROOT / "apps" / "common"))
        from swarm_common.states import EventType  # noqa: PLC0415

        members = {e.value for e in EventType}
        for name in bs.EVENT_CHAIN + bs.TERMINAL_EVENTS:
            assert name in members, f"{name} is not an EventType"
        for _, frm, to in bs.SEGMENTS:
            assert frm in members and to in members
        assert bs.COLD_START[1] in members and bs.COLD_START[2] in members


# ---------------------------------------------------------------------------
# Sample parsing and grouping
# ---------------------------------------------------------------------------


class TestSamples:
    def test_labels_are_part_of_the_metric_identity(self):
        """Merging a cold start on Cloud Run with one on GKE hides whichever
        backend regressed behind whichever did not."""
        s = bs.summarize(
            [
                {"metric": "m", "value": 1.0, "labels": {"backend": "cloud_run"}},
                {"metric": "m", "value": 500.0, "labels": {"backend": "gke"}},
            ]
        )
        assert set(s["metrics"]) == {"m{backend=cloud_run}", "m{backend=gke}"}

    def test_label_order_does_not_change_the_key(self):
        a = bs.metric_key({"metric": "m", "labels": {"b": "2", "a": "1"}})
        b = bs.metric_key({"metric": "m", "labels": {"a": "1", "b": "2"}})
        assert a == b == "m{a=1,b=2}"

    def test_an_empty_label_map_leaves_the_key_bare(self):
        assert bs.metric_key({"metric": "m", "labels": {}}) == "m"
        assert bs.metric_key({"metric": "m"}) == "m"

    def test_a_malformed_line_is_an_error_rather_than_a_skip(self):
        """Skipping would shrink n silently, which improves the run."""
        with pytest.raises(ValueError, match="line 2"):
            bs.parse_samples(['{"metric":"m","value":1}', "not json"])

    def test_a_line_without_a_metric_is_an_error(self):
        with pytest.raises(ValueError, match="no 'metric'"):
            bs.parse_samples(['{"value":1}'])

    def test_blank_lines_are_ignored(self):
        assert len(bs.parse_samples(['{"metric":"m","value":1}', "", "  "])) == 1

    def test_a_string_value_is_refused(self):
        """An unread shell variable arrives as "" and float("") explodes three
        frames away from the collector that produced it."""
        with pytest.raises(ValueError, match="must be a number or null"):
            bs.summarize([{"metric": "m", "value": ""}])

    def test_a_boolean_value_is_refused(self):
        with pytest.raises(ValueError, match="must be a number or null"):
            bs.summarize([{"metric": "m", "value": True}])


# ---------------------------------------------------------------------------
# Recording a baseline
# ---------------------------------------------------------------------------


class TestBaselineRecording:
    def run_cli(self, tmp_path, *args):
        return subprocess.run(
            [sys.executable, str(BENCHSTAT), *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    def test_a_not_measured_metric_is_excluded_and_named(self, tmp_path):
        """Recording it would bake the breakage in: every later run then gets
        `new` forever and the collector stays broken quietly."""
        current = tmp_path / "current.json"
        current.write_text(
            json.dumps(
                bs.summarize(
                    [
                        {"metric": "good", "value": 1.0},
                        {"metric": "broken", "value": None, "not_measured": "403"},
                    ]
                )
            )
        )
        out = tmp_path / "baseline.json"
        result = self.run_cli(tmp_path, "baseline", "--current", str(current), "--output", str(out))
        assert result.returncode == 0
        recorded = json.loads(out.read_text())
        assert set(recorded["metrics"]) == {"good"}
        assert recorded["excluded_not_measured"] == ["broken"]
        assert "broken" in result.stderr


# ---------------------------------------------------------------------------
# The CLI contract the shell depends on
# ---------------------------------------------------------------------------


class TestCli:
    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(BENCHSTAT), *args], capture_output=True, text=True, cwd=cwd
        )

    def test_self_test_passes(self):
        """The copy of these assertions that runs where pytest does not.

        The verify image has no pytest, so `scripts/bench.sh --self-test` is
        how the engine is proved in-VPC.
        """
        assert self.run_cli("self-test").returncode == 0

    def test_compare_exits_non_zero_on_a_regression(self, tmp_path):
        base = tmp_path / "b.json"
        cur = tmp_path / "c.json"
        base.write_text(json.dumps(bs.summarize([{"metric": "m", "value": 1.0}] * 10)))
        cur.write_text(json.dumps(bs.summarize([{"metric": "m", "value": 100.0}] * 10)))
        result = self.run_cli("compare", "--current", str(cur), "--baseline", str(base))
        assert result.returncode == 1
        assert "FAIL" in result.stdout

    def test_compare_exits_zero_when_everything_is_within_threshold(self, tmp_path):
        base = tmp_path / "b.json"
        base.write_text(json.dumps(bs.summarize([{"metric": "m", "value": 1.0}] * 10)))
        result = self.run_cli("compare", "--current", str(base), "--baseline", str(base))
        assert result.returncode == 0

    def test_a_missing_baseline_file_is_not_a_crash(self, tmp_path):
        """A first run has none, and it must report rather than explode."""
        cur = tmp_path / "c.json"
        cur.write_text(json.dumps(bs.summarize([{"metric": "m", "value": 1.0}] * 10)))
        result = self.run_cli(
            "compare", "--current", str(cur), "--baseline", str(tmp_path / "nope.json")
        )
        assert result.returncode == 0
        assert "new" in result.stdout

    def test_segments_reads_one_task_per_line(self, tmp_path):
        events = tmp_path / "events.jsonl"
        events.write_text(
            json.dumps(
                {
                    "labels": {"runner_profile": "mock"},
                    "events": [
                        {"type": "dispatched", "at": "2026-09-22T03:46:05Z"},
                        {"type": "running", "at": "2026-09-22T03:49:14Z"},
                    ],
                }
            )
            + "\n"
        )
        result = self.run_cli("segments", "--input", str(events))
        assert result.returncode == 0
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        cold = [r for r in rows if r["metric"] == "dispatch.cold_start"]
        assert cold[0]["value"] == 189.0
        assert cold[0]["labels"] == {"runner_profile": "mock"}

    def test_render_marks_failures_so_they_are_visible_in_a_log(self):
        result = bs.compare(summary_of(*([100.0] * 10)), baseline_of(1.0))
        text = bs.render(result)
        assert "FAIL" in text
        assert "1 failing" in text
