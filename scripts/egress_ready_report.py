#!/usr/bin/env python3
"""How long a new worker waited for its path to the forge, per backend (#939).

Every attempt that clones writes two startup marks on its task's events
(`agent_worker.lifecycle._mark_egress_ready` and `_mark_clone_timed`): a
RUNNING event whose `detail.cause` is `egress_ready`, with `detail.egress` the
probe's `EgressProbe.result()`, and one whose cause is `clone_timed`, with
`detail.clone.seconds`. This file reads them as the API serves them and
prints, per backend, n, n_none, p50, p90 and max of `egress_ready_seconds`,
with the clone's p50 beside it for context, and a verdict against #939's bar:
Cloud Run p50 under 5 s over at least 30 steps.

WHY THE SPLIT. `scripts/egress-ready-report.sh` does the reading (it needs a
credential and the API); everything that decides a number lives here and takes
the API's responses on stdin, so it is unit-tested with no credentials
(tests/unit/scripts/test_egress_ready_report.py). The shell decides nothing.

THE INPUT is a stream of JSON documents, each one response body exactly as
the API served it: `GET /v1/tasks/{id}/events` pages (`task_id`, `events`)
and `GET /v1/tasks/{id}/attempts` (`task_id`, `attempts`). The task document
carries no backend; the ATTEMPT does (`Attempt.backend`, frozen in
swarm_common.models), so a mark's backend is its `attempt_id`'s.

THE RULES, each the failure it prevents:

* A STEP THAT NEVER CONNECTED IS NOT A FAST ONE. `egress_ready_seconds` is
  None when nothing answered within the probe's cap. It is counted in n and
  n_none, and kept out of the table's percentiles (which describe the steps
  that did connect) -- but the verdict's p50 counts it as slower than any
  measurement, so steps that never opened cannot carry a pass.
* TOO FEW STEPS IS NOT A PASS. Under `--min-n` Cloud Run marks the verdict is
  FAIL, `insufficient n`.
* AN UNKNOWN BACKEND IS COUNTED, NOT DROPPED. A mark whose attempt is not
  listed, or names a backend this file does not know, goes under `unknown`.
* READING NOTHING IS NOT A RESULT. No mark read exits 2 with `no egress_ready
  marks read`, never 0 (CLAUDE.md's empty-output rule).

CLONE BUNDLES (#940), `bundle-report`. The same marks answer #940's
acceptance (docs/clone-bundles.md §8): `clone_timed`'s `clone.source` says
where the commit came from (`bundle`, `bundle+delta`, `forge`; none from a
worker older than #940, read as `unknown`) and `clone.total_seconds` is what
the step waited, the bundle's download included. The verdict is PASS only
when at least `--min-n` steps have `source: bundle`, their p50 is under 5 s
with a clone that did not land counted slower than any that did, and the
fallback was exercised and held: at least one `forge` step whose bundle
missed (`bundle.hit` false, `miss_reason` `miss`, `no_key` or
`bundle_error`), every one of them `ok`. A fallback that never ran is not one
that works. The pinned sha being the commit checked out is not in the mark;
tests/unit/worker/test_clone_bundle_git.py holds it.

Exit codes: 0 PASS (or NOT_JUDGED when `--backend` excludes Cloud Run), 1
FAIL, 2 nothing read or bad input.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchstat import percentile  # noqa: E402  (nearest-rank, stdlib-only)

try:
    from swarm_common.profiles import Backend
except ImportError:  # pragma: no cover - run outside the project's venv
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "common"))
    from swarm_common.profiles import Backend

#: The two backends an attempt actually runs on. `AUTO` is a profile's
#: request, resolved before dispatch, so an attempt naming it is `unknown`.
KNOWN_BACKENDS = (Backend.CLOUD_RUN_JOB.value, Backend.GKE_AUTOPILOT.value)
UNKNOWN = "unknown"

#: #939's acceptance bar.
BAR_BACKEND = Backend.CLOUD_RUN_JOB.value
BAR_P50_SECONDS = 5.0
DEFAULT_MIN_N = 30
DEFAULT_SINCE = "7d"

_ALIASES = {
    "cloud-run": Backend.CLOUD_RUN_JOB.value,
    "cloudrun": Backend.CLOUD_RUN_JOB.value,
    "cloud_run": Backend.CLOUD_RUN_JOB.value,
    "gke": Backend.GKE_AUTOPILOT.value,
}

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """`30m`, `36h`, `7d` -> seconds."""
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(f"not a duration: {text!r} (expected e.g. 30m, 36h, 7d)")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cutoff_for(since: str | None, now: str | None) -> datetime | None:
    if since is None:
        return None
    at = parse_time(now) if now else datetime.now(timezone.utc)
    if at is None:
        raise ValueError(f"not a timestamp: {now!r}")
    return at - timedelta(seconds=parse_duration(since))


def normalize_backend(value: Any) -> str:
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in KNOWN_BACKENDS:
            return upper
    return UNKNOWN


def backend_option(value: str) -> str:
    resolved = _ALIASES.get(value.strip().lower(), value.strip().upper())
    if resolved not in KNOWN_BACKENDS and resolved != UNKNOWN.upper():
        raise argparse.ArgumentTypeError(
            f"unknown backend {value!r} (one of {', '.join(KNOWN_BACKENDS)}, cloud-run, gke, unknown)"
        )
    return UNKNOWN if resolved == UNKNOWN.upper() else resolved


# ---------------------------------------------------------------------------
# Reading.


def read_documents(text: str) -> list[dict[str, Any]]:
    """Every JSON document in a stream of concatenated response bodies."""
    decoder = json.JSONDecoder()
    docs: list[dict[str, Any]] = []
    index, end = 0, len(text)
    while True:
        while index < end and text[index].isspace():
            index += 1
        if index >= end:
            return docs
        value, index = decoder.raw_decode(text, index)
        if isinstance(value, list):
            docs.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            docs.append(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _read(docs: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict[str, Any]], set[str]]:
    """Each attempt's backend, every event once (pages overlap), and the tasks seen."""
    backends: dict[str, str] = {}
    events: dict[str, dict[str, Any]] = {}
    tasks: set[str] = set()
    for doc in docs:
        task_id = doc.get("task_id")
        if isinstance(task_id, str):
            tasks.add(task_id)
        for attempt in doc.get("attempts") or []:
            if isinstance(attempt, dict) and attempt.get("attempt_id"):
                backends[str(attempt["attempt_id"])] = normalize_backend(attempt.get("backend"))
        for index, event in enumerate(doc.get("events") or []):
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("task_id"), str):
                tasks.add(event["task_id"])
            key = str(event.get("event_id") or f"{task_id}:{len(events)}:{index}")
            events[key] = event
    return backends, events, tasks


def _startup_mark(event: dict[str, Any], cutoff: datetime | None) -> dict[str, Any] | None:
    """The detail of a RUNNING startup mark inside the window, else None."""
    detail = event.get("detail")
    if str(event.get("type") or "").upper() != "RUNNING" or not isinstance(detail, dict):
        return None
    at = parse_time(event.get("at"))
    if cutoff is not None and at is not None and at < cutoff:
        return None
    return detail


def collect(docs: Iterable[dict[str, Any]], cutoff: datetime | None) -> dict[str, Any]:
    """Marks by backend, and how much was visited."""
    backends, events, tasks = _read(docs)
    egress: dict[str, list[float | None]] = {}
    clone: dict[str, list[float]] = {}
    visited = {"tasks": len(tasks), "egress_ready_marks": 0, "clone_timed_marks": 0}
    for event in events.values():
        detail = _startup_mark(event, cutoff)
        cause = detail.get("cause") if detail is not None else None
        if detail is None or cause not in ("egress_ready", "clone_timed"):
            continue
        backend = backends.get(str(event.get("attempt_id")), UNKNOWN)
        if cause == "egress_ready":
            probe = detail.get("egress")
            if not isinstance(probe, dict):
                continue
            visited["egress_ready_marks"] += 1
            egress.setdefault(backend, []).append(_number(probe.get("egress_ready_seconds")))
        else:
            figures = detail.get("clone")
            seconds = _number(figures.get("seconds")) if isinstance(figures, dict) else None
            visited["clone_timed_marks"] += 1
            if seconds is not None:
                clone.setdefault(backend, []).append(seconds)
    return {"egress": egress, "clone": clone, "visited": visited}


# ---------------------------------------------------------------------------
# Arithmetic.


def summarize(backend: str, values: list[float | None], clone: list[float]) -> dict[str, Any]:
    measured = sorted(v for v in values if v is not None)
    # The verdict's p50: a step that never connected sorts after every one that did.
    with_none = measured + [math.inf] * (len(values) - len(measured))
    clone_sorted = sorted(clone)
    p50_all = percentile(with_none, 50) if with_none else None
    return {
        "backend": backend,
        "n": len(values),
        "n_none": len(values) - len(measured),
        "p50": percentile(measured, 50) if measured else None,
        "p90": percentile(measured, 90) if measured else None,
        "max": measured[-1] if measured else None,
        # None, not Infinity, when the median step never connected: JSON has no inf.
        "p50_counting_none": None if p50_all is None or math.isinf(p50_all) else p50_all,
        "clone_p50": percentile(clone_sorted, 50) if clone_sorted else None,
        "clone_n": len(clone_sorted),
    }


def verdict(rows: list[dict[str, Any]], *, min_n: int, judged: bool) -> dict[str, Any]:
    if not judged:
        return {"result": "NOT_JUDGED", "reason": f"--backend excludes {BAR_BACKEND}"}
    bar = next((r for r in rows if r["backend"] == BAR_BACKEND), None)
    n = bar["n"] if bar else 0
    if n < min_n:
        return {"result": "FAIL", "reason": f"insufficient n ({n} {BAR_BACKEND} marks < {min_n})"}
    p50 = bar["p50_counting_none"]
    if p50 is None or p50 >= BAR_P50_SECONDS:
        shown = "no connect" if p50 is None else f"{p50:.2f} s"
        return {
            "result": "FAIL",
            "reason": f"{BAR_BACKEND} p50 {shown} >= {BAR_P50_SECONDS:g} s (n={n}, n_none={bar['n_none']})",
        }
    return {
        "result": "PASS",
        "reason": f"{BAR_BACKEND} p50 {p50:.2f} s < {BAR_P50_SECONDS:g} s (n={n} >= {min_n})",
    }


def build_report(
    docs: Iterable[dict[str, Any]],
    *,
    since: str | None = DEFAULT_SINCE,
    now: str | None = None,
    min_n: int = DEFAULT_MIN_N,
    backend: str | None = None,
) -> dict[str, Any]:
    cutoff = cutoff_for(since, now)
    marks = collect(docs, cutoff)
    names = [b for b in (*KNOWN_BACKENDS, UNKNOWN) if b in marks["egress"]]
    if backend is not None:
        names = [b for b in names if b == backend]
    rows = [summarize(b, marks["egress"][b], marks["clone"].get(b, [])) for b in names]
    judged = backend is None or backend == BAR_BACKEND
    return {
        "since": since,
        "cutoff": cutoff.isoformat() if cutoff else None,
        "min_n": min_n,
        "rows": rows,
        "visited": marks["visited"],
        "verdict": verdict(rows, min_n=min_n, judged=judged),
    }


# ---------------------------------------------------------------------------
# Clone bundles (#940).

#: Where a clone's commit came from (`clone_timed`'s `clone.source`).
BUNDLE_SOURCES = ("bundle", "bundle+delta", "forge")
#: A bundle that was looked for and not used: the fallback #940 must keep working.
FALLBACK_MISSES = ("miss", "no_key", "bundle_error")


def collect_clones(docs: Iterable[dict[str, Any]], cutoff: datetime | None) -> dict[str, Any]:
    """Each `clone_timed` mark's source, wait, outcome and bundle miss."""
    _, events, tasks = _read(docs)
    clones: list[dict[str, Any]] = []
    for event in events.values():
        detail = _startup_mark(event, cutoff)
        if detail is None or detail.get("cause") != "clone_timed":
            continue
        figures = detail.get("clone")
        figures = figures if isinstance(figures, dict) else {}
        bundle = figures.get("bundle")
        bundle = bundle if isinstance(bundle, dict) else {}
        source = figures.get("source")
        total = _number(figures.get("total_seconds"))
        clones.append(
            {
                "source": source if source in BUNDLE_SOURCES else UNKNOWN,
                "seconds": total if total is not None else _number(figures.get("seconds")),
                "ok": figures.get("ok") is True,
                "fallback": bundle.get("hit") is False and bundle.get("miss_reason") in FALLBACK_MISSES,
            }
        )
    return {"clones": clones, "visited": {"tasks": len(tasks), "clone_timed_marks": len(clones)}}


def summarize_source(source: str, clones: list[dict[str, Any]]) -> dict[str, Any]:
    landed = sorted(c["seconds"] for c in clones if c["ok"] and c["seconds"] is not None)
    # The verdict's p50: a clone that did not land sorts after every one that did.
    with_failed = landed + [math.inf] * (len(clones) - len(landed))
    p50_all = percentile(with_failed, 50) if with_failed else None
    return {
        "source": source,
        "n": len(clones),
        "n_failed": len(clones) - len(landed),
        "p50": percentile(landed, 50) if landed else None,
        "p90": percentile(landed, 90) if landed else None,
        "max": landed[-1] if landed else None,
        "p50_counting_failed": None if p50_all is None or math.isinf(p50_all) else p50_all,
    }


def bundle_verdict(rows: list[dict[str, Any]], fallback: dict[str, int], *, min_n: int) -> dict[str, Any]:
    bar = next((r for r in rows if r["source"] == "bundle"), None)
    n = bar["n"] if bar else 0
    if n < min_n:
        return {"result": "FAIL", "reason": f"insufficient n ({n} bundle clones < {min_n})"}
    p50 = bar["p50_counting_failed"]
    if p50 is None or p50 >= BAR_P50_SECONDS:
        shown = "no clone landed" if p50 is None else f"{p50:.2f} s"
        return {
            "result": "FAIL",
            "reason": f"bundle p50 {shown} >= {BAR_P50_SECONDS:g} s (n={n}, n_failed={bar['n_failed']})",
        }
    if fallback["n"] == 0:
        return {"result": "FAIL", "reason": "the fallback was not exercised: no forge clone after a bundle miss"}
    if fallback["n_ok"] < fallback["n"]:
        failed = fallback["n"] - fallback["n_ok"]
        return {"result": "FAIL", "reason": f"{failed} of {fallback['n']} fallback clones did not land"}
    return {
        "result": "PASS",
        "reason": (
            f"bundle p50 {p50:.2f} s < {BAR_P50_SECONDS:g} s (n={n} >= {min_n}); "
            f"fallback held ({fallback['n_ok']}/{fallback['n']})"
        ),
    }


def build_bundle_report(
    docs: Iterable[dict[str, Any]],
    *,
    since: str | None = DEFAULT_SINCE,
    now: str | None = None,
    min_n: int = DEFAULT_MIN_N,
) -> dict[str, Any]:
    cutoff = cutoff_for(since, now)
    read = collect_clones(docs, cutoff)
    clones = read["clones"]
    rows = [
        summarize_source(source, [c for c in clones if c["source"] == source])
        for source in (*BUNDLE_SOURCES, UNKNOWN)
        if any(c["source"] == source for c in clones)
    ]
    misses = [c for c in clones if c["source"] == "forge" and c["fallback"]]
    fallback = {"n": len(misses), "n_ok": sum(1 for c in misses if c["ok"])}
    return {
        "since": since,
        "cutoff": cutoff.isoformat() if cutoff else None,
        "min_n": min_n,
        "rows": rows,
        "fallback": fallback,
        "visited": read["visited"],
        "verdict": bundle_verdict(rows, fallback, min_n=min_n),
    }


def render_bundles(result: dict[str, Any]) -> str:
    lines = [
        f"clone_timed total_seconds by source (since {result['since']}, from {result['cutoff']})",
        f"{'source':<13} {'n':>5} {'n_failed':>9} {'p50':>8} {'p90':>8} {'max':>8}",
    ]
    for r in result["rows"]:
        lines.append(
            f"{r['source']:<13} {r['n']:>5} {r['n_failed']:>9} {_cell(r['p50']):>8} "
            f"{_cell(r['p90']):>8} {_cell(r['max']):>8}"
        )
    fallback, visited = result["fallback"], result["visited"]
    lines.append(f"fallback (forge after a bundle miss): {fallback['n_ok']}/{fallback['n']} landed")
    lines.append(f"visited: {visited['tasks']} task(s), {visited['clone_timed_marks']} clone_timed mark(s)")
    lines.append(f"verdict: {result['verdict']['result']} -- {result['verdict']['reason']}")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def render(result: dict[str, Any]) -> str:
    lines = [
        f"egress_ready_seconds by backend (since {result['since']}, from {result['cutoff']})",
        f"{'backend':<15} {'n':>5} {'n_none':>7} {'p50':>8} {'p90':>8} {'max':>8} {'clone_p50':>10}",
    ]
    for r in result["rows"]:
        lines.append(
            f"{r['backend']:<15} {r['n']:>5} {r['n_none']:>7} {_cell(r['p50']):>8} "
            f"{_cell(r['p90']):>8} {_cell(r['max']):>8} {_cell(r['clone_p50']):>10}"
        )
    v = result["visited"]
    lines.append(
        f"visited: {v['tasks']} task(s), {v['egress_ready_marks']} egress_ready mark(s), "
        f"{v['clone_timed_marks']} clone_timed mark(s)"
    )
    lines.append(f"verdict: {result['verdict']['result']} -- {result['verdict']['reason']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Paging helpers for the shell script, which parses no JSON itself.


def window_lines(page: dict[str, Any], *, since: str, now: str | None = None) -> list[str]:
    """`task <id>` per task inside the window, then `older` or `next <token>`.

    The task list is newest first, so the first task older than the window
    ends the walk. A row with no readable `created_at` is kept: dropping it
    would hide a mark, and its events are filtered by their own `at` anyway.
    """
    cutoff = cutoff_for(since, now)
    lines: list[str] = []
    for task in page.get("tasks") or []:
        created = parse_time(task.get("created_at"))
        if cutoff is not None and created is not None and created < cutoff:
            lines.append("older")
            return lines
        if task.get("id"):
            lines.append(f"task {task['id']}")
    token = page.get("next_page_token")
    if token:
        lines.append(f"next {quote(str(token), safe='')}")
    return lines


def next_token(page: dict[str, Any]) -> str:
    token = page.get("next_page_token")
    return quote(str(token), safe="") if token else ""


def _single(text: str) -> dict[str, Any]:
    docs = read_documents(text)
    if len(docs) != 1:
        raise ValueError(f"expected one JSON object on stdin, read {len(docs)}")
    return docs[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("report", help="API responses on stdin -> the table and the verdict")
    rep.add_argument("--since", default=DEFAULT_SINCE)
    rep.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    rep.add_argument("--backend", type=backend_option, default=None)
    rep.add_argument("--json", action="store_true")
    rep.add_argument("--now", default=None, help="ISO timestamp the window ends at (tests)")

    bun = sub.add_parser("bundle-report", help="API responses on stdin -> #940's clone-bundle verdict")
    bun.add_argument("--since", default=DEFAULT_SINCE)
    bun.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    bun.add_argument("--json", action="store_true")
    bun.add_argument("--now", default=None, help="ISO timestamp the window ends at (tests)")

    win = sub.add_parser("window", help="one GET /v1/tasks page on stdin -> task ids in the window")
    win.add_argument("--since", default=DEFAULT_SINCE)
    win.add_argument("--now", default=None)

    sub.add_parser("next-token", help="one page on stdin -> its url-encoded next_page_token")

    args = parser.parse_args(argv)
    text = sys.stdin.read()
    try:
        if args.command == "window":
            for line in window_lines(_single(text), since=args.since, now=args.now):
                print(line)
            return 0
        if args.command == "next-token":
            print(next_token(_single(text)))
            return 0
        if args.command == "bundle-report":
            bundles = build_bundle_report(
                read_documents(text), since=args.since, now=args.now, min_n=args.min_n
            )
            return _bundle_main(bundles, as_json=args.json)
        result = build_report(
            read_documents(text),
            since=args.since,
            now=args.now,
            min_n=args.min_n,
            backend=args.backend,
        )
    except ValueError as exc:
        print(f"egress_ready_report: {exc}", file=sys.stderr)
        return 2

    if not any(r["n"] for r in result["rows"]):
        print(
            f"egress_ready_report: no egress_ready marks read "
            f"({result['visited']['tasks']} task(s) visited)",
            file=sys.stderr,
        )
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result))
    return 1 if result["verdict"]["result"] == "FAIL" else 0


def _bundle_main(result: dict[str, Any], *, as_json: bool) -> int:
    if not result["visited"]["clone_timed_marks"]:
        print(
            f"egress_ready_report: no clone_timed marks read "
            f"({result['visited']['tasks']} task(s) visited)",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, default=str) if as_json else render_bundles(result))
    return 1 if result["verdict"]["result"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
