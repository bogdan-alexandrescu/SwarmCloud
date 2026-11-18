#!/usr/bin/env python3
"""The index canary: how the repository index's runs went over a window.

The owner's agent quality program (the owner, 2026-11-03) proves the landed
incremental carry before relying on it. On 2026-11-03 SwarmCloud's 222 index
runs of a week were every one a full ~42-minute rebuild, and nothing said so
until someone counted by hand. This report counts: it reads the index-runs
read's responses and prints

* the full-versus-incremental ratio, by what each run BUILT (the index's own
  `kind`), with what it was submitted as beside it;
* the reasons for every full rebuild, most frequent first;
* per phase -- stage_base, extraction, language servers, judgment, check,
  upload, sweep, promotion -- n, p50, p90 and max, over every measured run
  and apart for full and incremental builds;
* lag behind main: from the run's queueing (when swarm-api saw its commit at
  the head) to its promotion, and how many promoted indexes were still at
  the head then;
* the files reused (carried from the base) and recomputed.

Each run's figures are `run_metrics` in apps/swarm-api/swarm_api/repoindex.py,
stored on the run when it settles and served by
`GET /v1/repositories/{repo_id}/index/runs` as `metrics`.

THE INPUT is a stream of JSON documents on stdin (or the files named), each
one that route's body exactly as served (`repo_id`, `runs`), one a
repository, read with `?limit=50` (the most it serves):

    scripts/index_canary_report.py runs-repo_a.json runs-repo_b.json --window-hours 168

This file reads no credential and calls nothing: whoever reads the route
saves its bodies, and everything that decides a number is here, unit-tested
with no credentials (tests/unit/scripts/test_index_canary_report.py).

THE RULES, each the failure it prevents:

* AN UNKNOWN IS NOT ZERO. A phase a run did not reach, a run recorded before
  the metrics existed, a language-server pass whose servers measured nothing:
  counted in `unknown`, kept out of the percentiles, never read as 0 s.
* EVERY RUN COSTS. A failed, cancelled or refused run is in the phase
  figures: its pod ran. Only the build ratio is over runs that built.
* A CUT WINDOW SAYS SO. swarm-api keeps a repository's newest KEPT_RUNS run
  records (50); when a repository's response holds that many and its oldest
  is inside the window, the report says the window is cut there.
* READING NOTHING IS NOT A RESULT. No run read, or none in the window, exits
  2 (CLAUDE.md's empty-output rule), never 0.

Exit codes: 0 a report was printed, 2 nothing read or bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchstat import percentile  # noqa: E402  (nearest-rank, stdlib-only)

#: `repoindex.METRIC_PHASE_NAMES`, in the order a run goes through them.
PHASES = ("stage_base", "extraction", "language_servers", "judgment", "check", "upload",
          "sweep", "promotion")
#: `repoindex.KEPT_RUNS`: the run records swarm-api keeps a repository.
KEPT_RUNS = 50
#: The default window: a week, the span the 2026-11-03 count covered.
WINDOW_HOURS = 168
#: How many rebuild reasons are printed; the rest are counted as `other`.
REASONS_SHOWN = 12
_SHA = re.compile(r"\b[0-9a-f]{40}\b")


class InputError(ValueError):
    """The input is not the index-runs read's responses."""


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def documents(text: str) -> list[Any]:
    """Every JSON document in `text`, one after another (`jq` output, or
    several responses concatenated)."""
    decoder, out, at = json.JSONDecoder(), [], 0
    while True:
        while at < len(text) and text[at].isspace():
            at += 1
        if at >= len(text):
            return out
        try:
            value, at = decoder.raw_decode(text, at)
        except ValueError as bad:
            raise InputError(f"not JSON at character {at}: {bad}") from None
        out.append(value)


def responses(docs: Iterable[Any]) -> list[dict[str, Any]]:
    """The index-runs responses in `docs`; anything else is refused."""
    out = []
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):
            raise InputError("each document is a GET .../index/runs body, with a `runs` list")
        out.append(doc)
    return out


def _moment(run: dict[str, Any]) -> datetime | None:
    return parse_time(run.get("ended_at")) or parse_time(run.get("queued_at"))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def stats(values: Sequence[float | None]) -> dict[str, Any]:
    """n, unknown, p50, p90 and max of `values`; None marks an unknown."""
    known = sorted(v for v in values if v is not None)
    out: dict[str, Any] = {"n": len(known), "unknown": len(values) - len(known)}
    if known:
        out.update(p50=percentile(known, 50), p90=percentile(known, 90), max=known[-1])
    else:
        out.update(p50=None, p90=None, max=None)
    return out


def _reason(text: Any) -> str:
    said = text if isinstance(text, str) and text.strip() else "no reason recorded"
    return _SHA.sub("<sha>", said.strip())[:160]


def report(docs: Sequence[dict[str, Any]], *, now: datetime,
           window: timedelta) -> dict[str, Any]:
    """The canary's figures over the runs that ended (or were queued) in the window."""
    start = now - window
    runs: list[dict[str, Any]] = []
    cut: list[dict[str, Any]] = []
    seen: set[str] = set()
    read = 0
    for doc in docs:
        rows = [r for r in doc["runs"] if isinstance(r, dict)]
        read += len(rows)
        times = [m for m in (_moment(r) for r in rows) if m is not None]
        if len(rows) >= KEPT_RUNS and times and min(times) > start:
            cut.append({"repo_id": doc.get("repo_id"), "from": min(times).isoformat()})
        for row in rows:
            moment = _moment(row)
            key = str(row.get("task_id"))
            if moment is None or not start <= moment <= now or key in seen:
                continue
            seen.add(key)
            runs.append(row)
    measured = [r for r in runs if isinstance(r.get("metrics"), dict)]
    built: dict[str, int] = {"full": 0, "incremental": 0}
    requested: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for run in measured:
        metrics = run["metrics"]
        kind = metrics.get("built_kind")
        asked = str(metrics.get("requested_kind") or run.get("kind") or "unknown")
        requested[asked] = requested.get(asked, 0) + 1
        if kind in built:
            built[kind] += 1
        if kind == "full":
            why = _reason(metrics.get("rebuild_reason"))
            reasons[why] = reasons.get(why, 0) + 1
    states: dict[str, int] = {}
    for run in runs:
        state = str(run.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1

    def phases_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {name: stats([_number((r["metrics"].get("phases") or {}).get(name))
                             for r in rows]) for name in PHASES}

    promoted = [r for r in measured
                if (r.get("promotion") or {}).get("outcome") == "promoted"]
    files = [r["metrics"].get("files") for r in measured if r["metrics"].get("built_kind")]
    known_files = [f for f in files if isinstance(f, dict)]

    def total(key: str) -> int | None:
        values = [f.get(key) for f in known_files]
        if not values or any(not isinstance(v, int) or isinstance(v, bool) for v in values):
            return None
        return sum(values)

    ordered = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    shown = ordered[:REASONS_SHOWN]
    other = sum(count for _, count in ordered[REASONS_SHOWN:])
    builds = built["full"] + built["incremental"]
    return {
        "window": {"from": start.isoformat(), "to": now.isoformat()},
        "read": read,
        "runs": len(runs),
        "measured": len(measured),
        "unmeasured": len(runs) - len(measured),
        "states": dict(sorted(states.items())),
        "requested": dict(sorted(requested.items())),
        "built": built,
        "incremental_share": round(built["incremental"] / builds, 3) if builds else None,
        "rebuild_reasons": [{"reason": why, "runs": count} for why, count in shown]
        + ([{"reason": "other", "runs": other}] if other else []),
        "phases": phases_of(measured),
        "phases_by_kind": {
            kind: phases_of([r for r in measured if r["metrics"].get("built_kind") == kind])
            for kind in ("full", "incremental")
        },
        "lag_seconds": stats([_number(r["metrics"].get("lag_seconds")) for r in promoted]),
        "promoted": len(promoted),
        "promoted_at_head": sum(1 for r in promoted if r["metrics"].get("at_head") is True),
        "promoted_head_unknown": sum(1 for r in promoted if r["metrics"].get("at_head") is None),
        "files": {
            "runs_known": len(known_files),
            "runs_unknown": len(files) - len(known_files),
            "parsed": total("parsed"), "reused": total("reused"),
            "recomputed": total("recomputed"),
        },
        "cut_by_run_retention": cut,
    }


def _s(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}s" if value >= 10 else f"{value:.1f}s"


def render(figures: dict[str, Any]) -> str:
    lines = [
        f"index canary {figures['window']['from']} .. {figures['window']['to']}",
        f"runs in window: {figures['runs']} of {figures['read']} read; "
        f"measured {figures['measured']}, recorded before the metrics {figures['unmeasured']}",
        "states: " + (", ".join(f"{k} {v}" for k, v in figures["states"].items()) or "-"),
    ]
    built = figures["built"]
    share = figures["incremental_share"]
    lines.append(
        f"built: full {built['full']}, incremental {built['incremental']}"
        + (f" (incremental {share:.0%})" if share is not None else " (none built)")
        + "; submitted as: "
        + (", ".join(f"{k} {v}" for k, v in figures["requested"].items()) or "-"))
    lines.append("full rebuilds, by reason:")
    lines.extend(f"  {row['runs']:>4}  {row['reason']}" for row in figures["rebuild_reasons"])
    if not figures["rebuild_reasons"]:
        lines.append("     -  no full rebuild")
    lines.append(f"{'phase':<17}{'n':>5}{'unknown':>9}{'p50':>8}{'p90':>8}{'max':>8}"
                 f"{'full p50':>10}{'incr p50':>10}")
    for name in PHASES:
        row = figures["phases"][name]
        full = figures["phases_by_kind"]["full"][name]["p50"]
        incr = figures["phases_by_kind"]["incremental"][name]["p50"]
        lines.append(f"{name:<17}{row['n']:>5}{row['unknown']:>9}{_s(row['p50']):>8}"
                     f"{_s(row['p90']):>8}{_s(row['max']):>8}{_s(full):>10}{_s(incr):>10}")
    lag = figures["lag_seconds"]
    lines.append(
        f"lag behind main (queued to promoted): n {lag['n']}, unknown {lag['unknown']}, "
        f"p50 {_s(lag['p50'])}, p90 {_s(lag['p90'])}, max {_s(lag['max'])}; "
        f"{figures['promoted_at_head']} of {figures['promoted']} promoted at the head"
        + (f", {figures['promoted_head_unknown']} unknown" if figures["promoted_head_unknown"]
           else ""))
    files = figures["files"]
    lines.append(
        f"files: reused {files['reused'] if files['reused'] is not None else 'unknown'}, "
        f"recomputed {files['recomputed'] if files['recomputed'] is not None else 'unknown'} "
        f"of {files['parsed'] if files['parsed'] is not None else 'unknown'} parsed, "
        f"over {files['runs_known']} runs ({files['runs_unknown']} runs did not say)")
    for row in figures["cut_by_run_retention"]:
        lines.append(f"CUT: {row['repo_id']} keeps its newest {KEPT_RUNS} run records; "
                     f"this window covers it only from {row['from']}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="index-runs responses (default: stdin)")
    parser.add_argument("--window-hours", type=float, default=WINDOW_HOURS)
    parser.add_argument("--now", help="the window's end, ISO 8601 (default: now)")
    parser.add_argument("--json", action="store_true", help="print the figures as JSON")
    args = parser.parse_args(argv)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None or args.window_hours <= 0:
        print("index canary: --now is an ISO 8601 time and --window-hours is positive",
              file=sys.stderr)
        return 2
    try:
        text = "".join(Path(f).read_text(encoding="utf-8") for f in args.files) \
            if args.files else sys.stdin.read()
        docs = responses(documents(text))
    except (OSError, InputError) as bad:
        print(f"index canary: {bad}", file=sys.stderr)
        return 2
    figures = report(docs, now=now, window=timedelta(hours=args.window_hours))
    if figures["read"] == 0:
        print("index canary: no index run read", file=sys.stderr)
        return 2
    if figures["runs"] == 0:
        print(f"index canary: {figures['read']} runs read, none in the window", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(figures, indent=2) + "\n" if args.json else render(figures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
