#!/usr/bin/env python3
"""The benchmark measurement engine: samples in, percentiles and a verdict out.

WHY THIS IS PYTHON AND NOT MORE BASH
------------------------------------
Every collector in this repository is a shell script, and that is right: they
have to run inside a Cloud Run job with nothing but curl and jq. But the part
that decides whether a number is a REGRESSION is the part that has to be tested
exhaustively and offline, and a percentile computed in `sed -n "${k}p"` cannot
be unit-tested the way `make test` needs. So the split is:

    collectors (bash)   measure, and append one JSON line per sample
    this file (python)  summarise, compare against a baseline, and fail

The collectors never decide anything. They cannot report a pass.

THE FOUR RULES THIS FILE EXISTS TO ENFORCE
------------------------------------------
1. A FAILED MEASUREMENT IS NOT A FAST ONE.
   This is the rule benchmarks break more often than anything else, and it is
   the same bug this repository has produced over and over in another costume:
   an unread value falling into a comparison that is true for every bound. A
   timing that could not be taken arrives here as `{"value": null,
   "not_measured": "<reason>"}`. It is counted in `missing`, it is never
   counted as 0, and a metric whose samples are ALL missing is reported as
   `not_measured` and FAILS the gate. It does not silently become the fastest
   run ever recorded.

2. NO MEAN. p50/p95/p99 and n, always.
   There is deliberately no `mean` field in the summary. A mean hides the tail,
   and the one number on this platform that most needs its tail read -- Cloud
   Run cold start, three minutes against eighteen seconds of agent -- is
   exactly the number a mean over a warm pool would erase. A field nobody may
   gate on is a field somebody will quote, so it is not emitted at all.

3. A PERCENTILE FROM TWO SAMPLES IS A LIE.
   `min_samples` is part of the threshold, and a metric below it fails as
   `insufficient_samples` rather than passing on a p99 that is just the maximum
   of a pair.

4. A BASELINE METRIC THAT VANISHES IS A FAILURE, NOT A SILENCE.
   If the baseline knows about `dispatch.cold_start` and this run produced no
   such metric at all, the collector broke or the path stopped executing. Both
   are the thing benchmarks are for. That is `missing_from_current`, and it
   fails.

NEGATIVE DURATIONS ARE REPORTED, NEVER CLAMPED.
Segment timings come from event timestamps written by different processes on
different machines. Clock skew produces a negative segment, and `max(0, x)`
would turn a real distributed-systems problem into a suspiciously fast
dispatch. A negative sample is kept, counted in `anomalies`, and fails the gate
under `anomalous_samples`.

Usage
-----
    benchstat.py summarize --input SAMPLES.jsonl [--environment dev]
                           [--output SUMMARY.json]
    benchstat.py segments  --input EVENTS.jsonl  [--output SAMPLES.jsonl]
    benchstat.py compare   --current SUMMARY.json --baseline BASELINE.json
                           [--thresholds THRESHOLDS.json] [--json]
    benchstat.py baseline  --current SUMMARY.json --output BASELINE.json
    benchstat.py self-test

`compare` exits 0 only when every verdict is `ok` or `new`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = 1

#: The lifecycle chain, in the one order the state machine permits. Restated
#: from `swarm_common.states.EventType` -- which is FROZEN and cannot be
#: imported here, because the collectors run in a Cloud Run job whose image has
#: no swarm_common. `check-contract-parity.sh` covers shell and jq restatements
#: of the contract; `tests/unit/scripts/test_benchstat.py` covers this one, by
#: importing the real enum and asserting every name below is a member of it.
EVENT_CHAIN: tuple[str, ...] = (
    "submitted",
    "queued",
    "ready",
    "lease_acquired",
    "dispatched",
    "starting",
    "running",
)

#: Terminal events, any of which ends the chain.
TERMINAL_EVENTS: tuple[str, ...] = ("succeeded", "failed", "cancelled", "dead_lettered")

#: Segment name -> (from_event, to_event). Named for the OWNER of the delay,
#: because "dispatch is slow" is three different teams' problem depending on
#: which of these moved.
SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("submit_to_queued", "submitted", "queued"),
    ("queued_to_ready", "queued", "ready"),
    # Admission: the scheduler's all-or-nothing multi-pool reservation.
    ("ready_to_leased", "ready", "lease_acquired"),
    # Dispatch: the control plane creating the execution.
    ("leased_to_dispatched", "lease_acquired", "dispatched"),
    # COLD START, first half: the backend scheduling a container and pulling an
    # image. This is the 3m09s. Nothing else on the platform measures it.
    ("dispatched_to_starting", "dispatched", "starting"),
    ("starting_to_running", "starting", "running"),
)

#: Cold start as one number, for the per-profile/per-backend threshold. Kept
#: SEPARATE from its two halves rather than derived at read time, so a baseline
#: can carry a threshold on the whole without anyone having to add two
#: percentiles together -- which is not a valid operation on percentiles.
COLD_START = ("dispatch.cold_start", "dispatched", "running")


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile over an ALREADY SORTED sequence.

    Nearest-rank, not interpolated: every value this returns is a measurement
    that actually happened. An interpolated p99 over eleven samples reports a
    latency no request ever experienced, which is the wrong thing to show an
    operator next to "and here is the task that was slowest".

    Rank is `ceil(q/100 * n)`, clamped to [1, n].
    """
    if not values:
        raise ValueError("percentile of an empty sequence; the caller must handle n=0")
    if not 0 < q <= 100:
        raise ValueError(f"percentile q must be in (0, 100], got {q}")
    rank = math.ceil(q / 100.0 * len(values))
    return values[max(1, min(rank, len(values))) - 1]


def summarize_values(
    values: Iterable[float], *, missing: int = 0, unit: str = ""
) -> dict[str, Any]:
    """One metric's distribution, or an honest statement that there isn't one."""
    kept = sorted(float(v) for v in values)
    anomalies = sum(1 for v in kept if v < 0)
    if not kept:
        # n=0. There is no p50. Saying so is the whole point of this branch:
        # returning zeros here is the bug this file was written to prevent.
        return {
            "unit": unit,
            "n": 0,
            "missing": int(missing),
            "anomalies": 0,
            "status": "not_measured" if missing else "no_samples",
        }
    return {
        "unit": unit,
        "n": len(kept),
        "missing": int(missing),
        "anomalies": anomalies,
        "min": kept[0],
        "p50": percentile(kept, 50),
        "p95": percentile(kept, 95),
        "p99": percentile(kept, 99),
        "max": kept[-1],
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# Samples -> summary
# ---------------------------------------------------------------------------


def parse_samples(lines: Iterable[str]) -> list[dict[str, Any]]:
    """JSONL in, sample dicts out. A malformed line is an error, not a skip."""
    out: list[dict[str, Any]] = []
    for lineno, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno} is not JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {lineno} is not a JSON object")
        if "metric" not in obj:
            raise ValueError(f"line {lineno} has no 'metric'")
        out.append(obj)
    return out


def summarize(samples: Iterable[dict[str, Any]], *, environment: str = "") -> dict[str, Any]:
    """Group samples by metric (plus its labels) and summarise each group."""
    grouped: dict[str, dict[str, Any]] = {}
    for sample in samples:
        key = metric_key(sample)
        bucket = grouped.setdefault(
            key,
            {"unit": sample.get("unit", ""), "values": [], "missing": 0, "reasons": []},
        )
        value = sample.get("value", None)
        if value is None:
            bucket["missing"] += 1
            reason = sample.get("not_measured") or "no reason given"
            if reason not in bucket["reasons"]:
                bucket["reasons"].append(str(reason))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # A string that looks like a number is the shape an unquoted shell
            # variable arrives in when the read failed and jq wrote "". Refuse
            # it rather than let float("") explode three frames away.
            raise ValueError(f"metric {key}: value must be a number or null, got {value!r}")
        bucket["values"].append(float(value))

    metrics: dict[str, Any] = {}
    for key, bucket in sorted(grouped.items()):
        entry = summarize_values(
            bucket["values"], missing=bucket["missing"], unit=bucket["unit"]
        )
        if bucket["reasons"]:
            entry["not_measured_reasons"] = bucket["reasons"]
        metrics[key] = entry

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": environment,
        "metrics": metrics,
    }


def metric_key(sample: dict[str, Any]) -> str:
    """`metric{label=value,...}` with labels sorted, so the key is stable.

    Labels are part of the identity, not decoration: `dispatch.cold_start` for
    `claude-code` on Cloud Run and for `mock` on GKE are different numbers with
    different owners, and averaging them together is how a cold-start
    regression in one backend disappears.
    """
    name = str(sample["metric"])
    labels = sample.get("labels") or {}
    if not isinstance(labels, dict) or not labels:
        return name
    inner = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{inner}}}"


# ---------------------------------------------------------------------------
# Event streams -> segment samples
# ---------------------------------------------------------------------------


def _epoch(value: Any) -> float | None:
    """ISO-8601 (or an epoch number) to seconds. Returns None, never 0."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def first_event_times(events: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Earliest timestamp per event type.

    EARLIEST, not latest: a retried task re-enters READY, acquires a second
    lease and dispatches again, and the question these segments answer is "how
    long did the first attempt take to get moving". Taking the max would
    measure the retry and call it a cold start.
    """
    out: dict[str, float] = {}
    for event in events:
        etype = event.get("type")
        if not isinstance(etype, str):
            continue
        at = _epoch(event.get("at"))
        if at is None:
            continue
        if etype not in out or at < out[etype]:
            out[etype] = at
    return out


def segment_samples(
    events: Iterable[dict[str, Any]], *, labels: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """One sample per segment for one task, including the ones that are missing.

    A segment whose endpoint event never happened produces a sample with
    `value: null` and a reason naming the event, NOT the absence of a sample.
    The difference matters: an absent sample makes n smaller and the run still
    passes; a null sample makes the metric `not_measured` and the run fails.
    A task that never reached RUNNING is not a task with a fast cold start.
    """
    labels = dict(labels or {})
    times = first_event_times(events)
    samples: list[dict[str, Any]] = []

    def emit(metric: str, frm: str, to: str) -> None:
        a, b = times.get(frm), times.get(to)
        if a is None or b is None:
            absent = frm if a is None else to
            samples.append(
                {
                    "metric": metric,
                    "unit": "s",
                    "value": None,
                    "not_measured": f"no '{absent}' event on this task",
                    "labels": labels,
                }
            )
            return
        samples.append(
            {"metric": metric, "unit": "s", "value": b - a, "labels": labels}
        )

    for name, frm, to in SEGMENTS:
        emit(f"dispatch.segment.{name}", frm, to)
    emit(*COLD_START)

    terminal = [times[t] for t in TERMINAL_EVENTS if t in times]
    start = times.get("submitted")
    if start is None or not terminal:
        absent = "submitted" if start is None else "a terminal"
        samples.append(
            {
                "metric": "dispatch.total",
                "unit": "s",
                "value": None,
                "not_measured": f"no '{absent}' event on this task",
                "labels": labels,
            }
        )
    else:
        samples.append(
            {
                "metric": "dispatch.total",
                "unit": "s",
                "value": min(terminal) - start,
                "labels": labels,
            }
        )
    return samples


# ---------------------------------------------------------------------------
# Baselines and thresholds
# ---------------------------------------------------------------------------

#: Applied to any metric the thresholds file does not name.
#:
#: 1.5 is not a principled constant, it is a starting point chosen so the first
#: baselines on this platform do not fail on ordinary Cloud Run variance -- a
#: cold start measured at 189s and again at 240s is the same platform, not a
#: regression. Tighten it per metric in the thresholds file as real variance
#: becomes known; that is what the per-metric overrides are for.
DEFAULT_THRESHOLD: dict[str, Any] = {
    "statistic": "p95",
    "max_regression_ratio": 1.5,
    "absolute_max": None,
    "min_samples": 5,
    # A run where a fifth of the samples could not be taken is not a run whose
    # p95 means anything. Without this, a collector that loses 40% of its
    # measurements to 403s reports a healthy percentile over the 60% that
    # happened to work -- and the ones that failed are exactly the ones most
    # likely to have been slow. The default is deliberately loose (one in five)
    # so ordinary flakiness does not fail a build; tighten it per metric.
    "max_missing_fraction": 0.2,
}

#: Verdicts that fail the gate. `new` does not: a metric the baseline has never
#: seen has nothing to regress against, and failing on it would mean no
#: collector could ever be added without a red build.
FAILING = frozenset(
    {
        "regressed",
        "not_measured",
        "missing_from_current",
        "insufficient_samples",
        "anomalous_samples",
        "over_absolute_max",
        "too_many_missing",
    }
)


def base_name(name: str) -> str:
    """`m{a=b}` -> `m`. The metric without its labels."""
    return name.split("{", 1)[0]


def threshold_for(name: str, thresholds: dict[str, Any] | None) -> dict[str, Any]:
    """default <- base metric name <- exact key, in that order of precedence.

    THE BASE-NAME LAYER IS WHAT MAKES THIS FILE WRITABLE BY HAND. Labels are
    part of a metric's identity, so keys look like
    `api.latency{route=/v1/stats}` and `reconcile.backend_errors{dry_run=false}`
    -- and a thresholds file keyed only by exact match would need one entry per
    label combination, including for labels whose values are not known until
    the run produces them (every route, every runner profile, every screen).
    Nobody maintains that, so the rules would silently stop applying to new
    label values, which is the same failure as having no rules.
    """
    merged = dict(DEFAULT_THRESHOLD)
    if not thresholds:
        return merged
    merged.update(thresholds.get("default") or {})
    metrics = thresholds.get("metrics") or {}
    merged.update(metrics.get(base_name(name)) or {})
    merged.update(metrics.get(name) or {})
    return merged


def compare(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verdict per metric. See FAILING for which ones are a red build."""
    cur_metrics = (current or {}).get("metrics") or {}
    base_metrics = (baseline or {}).get("metrics") or {}
    verdicts: list[dict[str, Any]] = []

    for name in sorted(set(cur_metrics) | set(base_metrics)):
        rule = threshold_for(name, thresholds)
        stat = rule.get("statistic") or "p95"
        cur = cur_metrics.get(name)
        base = base_metrics.get(name)

        if cur is None:
            # Rule 4. The baseline knows this metric and this run produced no
            # sample at all -- not even a null one. The collector did not run,
            # or the code path it measures stopped executing.
            verdicts.append(
                {
                    "metric": name,
                    "status": "missing_from_current",
                    "statistic": stat,
                    "detail": "the baseline has this metric and this run produced no samples for it",
                }
            )
            continue

        entry: dict[str, Any] = {"metric": name, "statistic": stat, "unit": cur.get("unit", "")}

        if cur.get("status") != "ok":
            # Rule 1. Every sample was missing.
            entry["status"] = "not_measured" if cur.get("missing") else "no_samples"
            entry["n"] = cur.get("n", 0)
            entry["missing"] = cur.get("missing", 0)
            reasons = cur.get("not_measured_reasons")
            entry["detail"] = (
                "; ".join(reasons) if reasons else "no samples and no reason recorded"
            )
            # `no_samples` with nothing missing means the collector emitted
            # nothing at all for it, which is the same failure as rule 4.
            if entry["status"] == "no_samples":
                entry["status"] = "missing_from_current" if base else "new"
            verdicts.append(entry)
            continue

        value = cur.get(stat)
        entry.update(
            {"n": cur["n"], "missing": cur.get("missing", 0), "value": value}
        )

        if cur.get("anomalies"):
            # Rule: negative durations are reported, never clamped.
            entry["status"] = "anomalous_samples"
            entry["anomalies"] = cur["anomalies"]
            entry["detail"] = (
                f"{cur['anomalies']} sample(s) are negative. A duration cannot be "
                "negative; this is clock skew between the writers of the two events, "
                "and it is not a fast measurement."
            )
            verdicts.append(entry)
            continue

        missing = int(cur.get("missing", 0))
        total = cur["n"] + missing
        max_missing = rule.get("max_missing_fraction")
        if max_missing is not None and total > 0:
            fraction = missing / total
            if fraction > float(max_missing):
                entry["status"] = "too_many_missing"
                entry["missing_fraction"] = round(fraction, 4)
                entry["detail"] = (
                    f"{missing} of {total} samples could not be taken "
                    f"({fraction:.0%}), over the {float(max_missing):.0%} limit. "
                    "The percentile below is computed only over the ones that "
                    "worked, and the ones that failed are the likelier to have "
                    "been slow."
                )
                reasons = cur.get("not_measured_reasons")
                if reasons:
                    entry["detail"] += " Reasons: " + "; ".join(reasons)
                verdicts.append(entry)
                continue

        min_samples = int(rule.get("min_samples") or 0)
        if cur["n"] < min_samples:
            # Rule 3.
            entry["status"] = "insufficient_samples"
            entry["detail"] = (
                f"n={cur['n']} is below min_samples={min_samples}; "
                f"a {stat} from that many samples is not a percentile"
            )
            verdicts.append(entry)
            continue

        absolute_max = rule.get("absolute_max")
        if absolute_max is not None and value is not None and value > float(absolute_max):
            entry["status"] = "over_absolute_max"
            entry["limit"] = float(absolute_max)
            entry["detail"] = f"{stat}={value} exceeds the absolute limit {absolute_max}"
            verdicts.append(entry)
            continue

        if base is None or base.get("status") != "ok" or base.get(stat) is None:
            entry["status"] = "new"
            entry["detail"] = "no comparable baseline; recorded for the next run"
            verdicts.append(entry)
            continue

        base_value = float(base[stat])
        entry["baseline"] = base_value
        ratio = float("inf") if base_value == 0 else value / base_value
        entry["ratio"] = None if ratio == float("inf") else round(ratio, 4)

        # `"max_regression_ratio": null` means DO NOT GATE ON THE RATIO, and it
        # is a real setting rather than an omission. Two metrics here need it:
        # a gauge that only ever grows (`api.stats.history_tasks`), and a
        # throughput where HIGHER IS BETTER (`admission.throughput_per_s`) --
        # this engine compares in one direction, so a ratio rule there would
        # fail a scheduler that sped up and pass one that slowed down. Writing
        # a huge number instead would work and would be a lie about intent, and
        # the next person would tighten it.
        raw_limit = rule.get("max_regression_ratio", DEFAULT_THRESHOLD["max_regression_ratio"])
        if raw_limit is None:
            entry["status"] = "ok"
            entry["detail"] = "ratio not gated for this metric (see benchmarks/thresholds.json)"
            verdicts.append(entry)
            continue
        limit = float(raw_limit)
        if ratio > limit:
            entry["status"] = "regressed"
            entry["detail"] = (
                f"{stat} {value}{cur.get('unit','')} vs baseline {base_value}"
                f"{cur.get('unit','')} = {entry['ratio']}x, over the {limit}x threshold"
            )
        else:
            entry["status"] = "ok"
        verdicts.append(entry)

    failed = [v for v in verdicts if v["status"] in FAILING]
    return {
        "schema": SCHEMA,
        "environment": current.get("environment", ""),
        "compared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_generated_at": (baseline or {}).get("generated_at"),
        "verdicts": verdicts,
        "failed": len(failed),
        "passed": len(verdicts) - len(failed),
    }


def render(result: dict[str, Any]) -> str:
    lines = []
    width = max((len(v["metric"]) for v in result["verdicts"]), default=10)
    for v in result["verdicts"]:
        mark = "FAIL" if v["status"] in FAILING else "ok  "
        value = v.get("value")
        shown = "--" if value is None else f"{value:g}{v.get('unit','')}"
        base = v.get("baseline")
        against = "" if base is None else f"  (baseline {base:g}{v.get('unit','')})"
        lines.append(
            f"{mark}  {v['metric']:<{width}}  {v.get('statistic','')}={shown}"
            f"  n={v.get('n', 0)}  missing={v.get('missing', 0)}{against}"
        )
        if v.get("detail"):
            lines.append(f"      {v['status']}: {v['detail']}")
    lines.append("")
    lines.append(f"{result['passed']} ok, {result['failed']} failing")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text())


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    else:
        sys.stdout.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchstat.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("summarize", help="samples.jsonl -> summary.json")
    p.add_argument("--input", required=True)
    p.add_argument("--environment", default="")
    p.add_argument("--output")

    p = sub.add_parser("segments", help="events.jsonl -> segment samples.jsonl")
    p.add_argument("--input", required=True)
    p.add_argument("--output")

    p = sub.add_parser("compare", help="summary vs baseline -> verdict")
    p.add_argument("--current", required=True)
    p.add_argument("--baseline")
    p.add_argument("--thresholds")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output")

    p = sub.add_parser("baseline", help="record a summary as the new baseline")
    p.add_argument("--current", required=True)
    p.add_argument("--output", required=True)

    sub.add_parser("self-test", help="the engine's own assertions, offline")

    args = parser.parse_args(argv)

    if args.command == "summarize":
        samples = parse_samples(Path(args.input).read_text().splitlines())
        out = summarize(samples, environment=args.environment)
        _write(args.output, json.dumps(out, indent=2, sort_keys=True) + "\n")
        return 0

    if args.command == "segments":
        out_lines: list[str] = []
        for raw in Path(args.input).read_text().splitlines():
            text = raw.strip()
            if not text:
                continue
            doc = json.loads(text)
            labels = {
                k: str(v)
                for k, v in (doc.get("labels") or {}).items()
                if v not in (None, "")
            }
            for sample in segment_samples(doc.get("events") or [], labels=labels):
                out_lines.append(json.dumps(sample, sort_keys=True))
        _write(args.output, "\n".join(out_lines) + ("\n" if out_lines else ""))
        return 0

    if args.command == "compare":
        current = _read_json(args.current)
        baseline = _read_json(args.baseline) if args.baseline and Path(args.baseline).exists() else None
        thresholds = _read_json(args.thresholds) if args.thresholds and Path(args.thresholds).exists() else None
        result = compare(current, baseline, thresholds)
        text = (
            json.dumps(result, indent=2, sort_keys=True) + "\n"
            if args.json
            else render(result) + "\n"
        )
        _write(args.output, text)
        if args.output:
            sys.stdout.write(render(result) + "\n")
        return 1 if result["failed"] else 0

    if args.command == "baseline":
        current = _read_json(args.current)
        usable = {
            k: v for k, v in (current.get("metrics") or {}).items() if v.get("status") == "ok"
        }
        dropped = sorted(set(current.get("metrics") or {}) - set(usable))
        out = dict(current, metrics=usable, recorded_from=current.get("generated_at"))
        if dropped:
            # Recording a not-measured metric as a baseline would bake the
            # failure in: the next run compares against a metric with no value
            # and gets `new` forever, so the collector stays broken quietly.
            out["excluded_not_measured"] = dropped
        _write(args.output, json.dumps(out, indent=2, sort_keys=True) + "\n")
        sys.stderr.write(
            f"baseline: {len(usable)} metric(s) recorded"
            + (f", {len(dropped)} excluded as not measured: {', '.join(dropped)}" if dropped else "")
            + "\n"
        )
        return 0

    if args.command == "self-test":
        return _self_test()

    parser.error(f"unknown command {args.command}")
    return 2


def _self_test() -> int:
    """The assertions that must hold for any verdict this file produces.

    Duplicated in tests/unit/scripts/test_benchstat.py with far more cases.
    This copy exists so `scripts/bench.sh --self-test` can prove the engine on
    a machine with no pytest -- the Cloud Run verify image is exactly that
    machine.
    """
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    check("p50 nearest-rank", percentile(values, 50) == 5.0)
    check("p95 nearest-rank", percentile(values, 95) == 10.0)
    check("p99 nearest-rank", percentile(values, 99) == 10.0)

    # Rule 1: all-missing is not fast.
    s = summarize([{"metric": "m", "value": None, "not_measured": "read failed"}])
    check("all-missing is not_measured", s["metrics"]["m"]["status"] == "not_measured")
    check("all-missing has no p50", "p50" not in s["metrics"]["m"])
    v = compare(s, {"metrics": {"m": {"status": "ok", "p95": 1.0, "unit": "s", "n": 9}}})
    check("not_measured fails the gate", v["failed"] == 1)

    # Rule 2: no mean anywhere.
    ok = summarize([{"metric": "m", "value": float(i), "unit": "s"} for i in range(1, 21)])
    check("no mean is emitted", "mean" not in ok["metrics"]["m"])

    # Rule 3: a percentile from two samples is refused.
    two = summarize([{"metric": "m", "value": 1.0}, {"metric": "m", "value": 2.0}])
    v = compare(two, {"metrics": {"m": {"status": "ok", "p95": 2.0, "n": 9}}})
    check("insufficient_samples fails", v["verdicts"][0]["status"] == "insufficient_samples")

    # Rule 4: a baseline metric with no current samples fails.
    v = compare(
        {"metrics": {}}, {"metrics": {"gone": {"status": "ok", "p95": 1.0, "n": 9}}}
    )
    check("missing_from_current fails", v["verdicts"][0]["status"] == "missing_from_current")

    # A real regression is caught, and an equal run is not.
    base = summarize([{"metric": "m", "value": 100.0, "unit": "ms"} for _ in range(10)])
    slow = summarize([{"metric": "m", "value": 300.0, "unit": "ms"} for _ in range(10)])
    check("3x is a regression", compare(slow, base)["failed"] == 1)
    check("an equal run passes", compare(base, base)["failed"] == 0)

    # Negative durations are reported, not clamped.
    skew = summarize([{"metric": "m", "value": -3.0}] + [{"metric": "m", "value": 1.0}] * 9)
    v = compare(skew, base)
    check("negative samples fail", v["verdicts"][0]["status"] == "anomalous_samples")

    # A missing endpoint event is a null sample, not an absent one.
    seg = segment_samples([{"type": "submitted", "at": "2026-09-22T03:46:05Z"}])
    cold = [x for x in seg if x["metric"] == "dispatch.cold_start"]
    check("cold start with no events is null", len(cold) == 1 and cold[0]["value"] is None)

    # The measurement from the task description, end to end.
    real = segment_samples(
        [
            {"type": "submitted", "at": "2026-09-22T03:46:00Z"},
            {"type": "dispatched", "at": "2026-09-22T03:46:05Z"},
            {"type": "starting", "at": "2026-09-22T03:49:14Z"},
            {"type": "running", "at": "2026-09-22T03:49:14Z"},
            {"type": "succeeded", "at": "2026-09-22T03:49:32Z"},
        ]
    )
    by = {x["metric"]: x["value"] for x in real}
    check("cold start is 189s", by["dispatch.cold_start"] == 189.0)
    check("total is 212s", by["dispatch.total"] == 212.0)

    # A run that lost a third of its samples does not pass on the rest.
    holed = summarize(
        [{"metric": "m", "value": 1.0}] * 10
        + [{"metric": "m", "value": None, "not_measured": "HTTP 500"}] * 5
    )
    v = compare(holed, base)
    check("too_many_missing fails", v["verdicts"][0]["status"] == "too_many_missing")

    # A threshold written against the bare metric name reaches every label set.
    labelled = summarize(
        [{"metric": "m", "value": 5.0, "labels": {"route": "/x"}} for _ in range(10)]
    )
    v = compare(labelled, None, {"metrics": {"m": {"absolute_max": 1.0}}})
    check("base-name thresholds apply to labelled metrics",
          v["verdicts"][0]["status"] == "over_absolute_max")

    for name in EVENT_CHAIN + TERMINAL_EVENTS:
        check(f"event name {name} is lower case", name == name.lower())

    if failures:
        for name in failures:
            sys.stderr.write(f"FAIL {name}\n")
        sys.stderr.write(f"benchstat self-test: {len(failures)} assertion(s) failed\n")
        return 1
    sys.stderr.write("benchstat self-test: all assertions passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
