#!/usr/bin/env python3
"""The live browser, measured: start-up, the viewer's stream, the hand-off (LIVE-MEASURE).

Owner decision 2026-10-13: Phase 1 of the live-browser plan is "measure", and
whether to keep warm pods is decided from these numbers. `scripts/live-report.sh`
does the reading (it needs gcloud and the API); everything that decides a
number lives here and reads files, so it is unit-tested with no credentials
(tests/unit/scripts/test_live_report.py). The shell decides nothing.

WHAT IT READS, all for the last `--hours`:

* GATE LINES (`--gate`): `gcloud logging read --format json` entries of the
  display sidecar's `live_viewer_metrics` line (agent_worker/livegate/rfb.py
  `ViewerMetrics`), from OUR cluster only. The task is the pod's `swarm-task`
  label (scheduler.dispatch sets it to the sanitized task id).
* API LINES (`--api`): swarm-api's `live_handoff_funnel` and
  `live_ready_first_frame` lines (swarm_api/handoff.py).
* PER TASK (`--tasks-dir`): `<task>.startup.json`, the body of
  `GET /v1/tasks/{id}/startup`, and `<task>.actions.json`, the body of
  `GET /v1/tasks/{id}/live/actions` (the relay's action log).

THE RULES, each the failure it prevents:

* A CONNECTION IS COUNTED ONCE. The gate logs every minute and at close; the
  close line (`final`) is the connection's, and a connection with no close line
  (its pod was killed) is read from its latest line.
* THE RECORDER IS NOT A PERSON. `role=recorder` connections are shown in their
  own column and kept out of the stream targets.
* PERCENTILES ARE NOT AVERAGED. Input-to-frame is pooled from the gate's
  histogram, whose 200 ms bound is the target, so the target is read exactly.
* NO DATA IS NOT A PASS. A target with nothing to judge reads NO DATA, and the
  exit status says so; reading nothing at all exits 2.

Exit codes: 0 every target PASS, 1 any FAIL, 3 no FAIL but some NO DATA,
2 nothing read or bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchstat import percentile  # noqa: E402  (nearest-rank, stdlib-only)

#: The gate's line and swarm-api's two (rfb.VIEWER_METRICS_EVENT,
#: handoff.FUNNEL_EVENT, handoff.READY_FRAME_EVENT); a test holds them equal.
GATE_EVENT = "live_viewer_metrics"
FUNNEL_EVENT = "live_handoff_funnel"
READY_EVENT = "live_ready_first_frame"

#: THE PHASE-2 TARGETS (owner decision 2026-10-13).
TARGET_FPS = 10.0
TARGET_INPUT_P95_MS = 200
TARGET_READY_P90_WARM_S = 20.0
TARGET_READY_P90_COLD_S = 60.0
TARGET_EXPIRY_PRESENT = 0.30

#: COLD OR WARM. A start-up is cold when Autopilot had to bring up a node for
#: the pod (dispatched -> pod_scheduled at least `COLD_NODE_SECONDS`) or the
#: image was pulled rather than found on the node (image_pulling ->
#: image_pulled at least `COLD_PULL_SECONDS`). A node that already exists
#: schedules a pod in seconds and a cached image "pulls" in under one; a new
#: node takes a minute or more and the browser image's pull tens of seconds,
#: so the thresholds sit well between the two.
COLD_NODE_SECONDS = 30.0
COLD_PULL_SECONDS = 15.0

#: Google's rate-limit page (docs/browser-egress.md §2): a URL on a Google
#: host whose path begins /sorry.
_GOOGLE_HOST = re.compile(r"^(www\.)?google\.[a-z.]{2,12}$")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def task_from_label(label: Any) -> str | None:
    """The pod's `swarm-task` label back to the task id: `task-<hex>` is
    `task_<hex>` (sanitize_name turned the one underscore into a dash)."""
    if not isinstance(label, str) or not re.fullmatch(r"task-[a-z0-9]+", label):
        return None
    return "task_" + label[len("task-"):]


def _entries(path: Path | None) -> list[dict[str, Any]]:
    """`gcloud logging read --format json` output: a JSON array of entries."""
    if path is None or not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of log entries")
    return [e for e in data if isinstance(e, dict)]


def _payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("jsonPayload")
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Reading.


def viewer_lines(entries: Iterable[dict[str, Any]], *, cutoff: datetime | None = None
                 ) -> list[dict[str, Any]]:
    """One line per connection: its close line, else its latest. Each gets
    `task_id` (from the pod label) and `pod`."""
    best: dict[tuple[str, str], tuple[tuple[int, datetime], dict[str, Any]]] = {}
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for entry in entries:
        line = _payload(entry)
        if line.get("event") != GATE_EVENT or not isinstance(line.get("conn"), str):
            continue
        at = parse_time(entry.get("timestamp")) or floor
        if cutoff is not None and at != floor and at < cutoff:
            continue
        resource = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
        rlabels = resource.get("labels") if isinstance(resource.get("labels"), dict) else {}
        labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
        pod = str(rlabels.get("pod_name") or "")
        key = (pod, line["conn"])
        rank = (1 if line.get("final") else 0, at)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, {**line, "pod": pod,
                                "task_id": task_from_label(labels.get("k8s-pod/swarm-task"))})
    return [v[1] for v in best.values()]


def api_lines(entries: Iterable[dict[str, Any]], event: str, *, cutoff: datetime | None = None
              ) -> list[dict[str, Any]]:
    """swarm-api's lines of `event`, one per hand-off (or per resumed attempt)."""
    seen: set[tuple[Any, ...]] = set()
    out = []
    for entry in entries:
        line = _payload(entry)
        if line.get("event") != event:
            continue
        at = parse_time(entry.get("timestamp"))
        if cutoff is not None and at is not None and at < cutoff:
            continue
        key = (line.get("task_id"), line.get("attempt_id"), line.get("request_id"), event)
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def task_ids(gate: list[dict[str, Any]], funnels: list[dict[str, Any]],
             readies: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for line in [*gate, *funnels, *readies]:
        task = line.get("task_id")
        if isinstance(task, str) and task and task not in ids:
            ids.append(task)
    return ids


def _json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def startup_of(body: dict[str, Any] | None) -> dict[str, Any]:
    """`GET /tasks/{id}/startup` as seconds: start-up (dispatched -> first
    frame), node allocation, pull, and cold/warm. None where a step is missing."""
    out: dict[str, Any] = {"attempt_id": None, "startup_s": None, "node_s": None,
                           "pull_s": None, "class": "unknown"}
    if not body:
        return out
    out["attempt_id"] = body.get("attempt_id")
    at: dict[str, datetime] = {}
    for step in body.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("step"), str):
            moment = parse_time(step.get("at"))
            if moment is not None and step["step"] not in at:
                at[step["step"]] = moment

    def between(a: str, b: str) -> float | None:
        if a in at and b in at:
            return round(max(0.0, (at[b] - at[a]).total_seconds()), 1)
        return None

    out["startup_s"] = between("dispatched", "first_frame")
    out["node_s"] = between("dispatched", "pod_scheduled")
    out["pull_s"] = between("image_pulling", "image_pulled")
    if out["node_s"] is None:
        return out
    cold = out["node_s"] >= COLD_NODE_SECONDS or (out["pull_s"] or 0.0) >= COLD_PULL_SECONDS
    out["class"] = "cold" if cold else "warm"
    return out


def sorry_hits(body: dict[str, Any] | None) -> tuple[int, int]:
    """(Google /sorry hits, Google navigations) in an action log."""
    hits = navigations = 0
    for action in (body or {}).get("actions") or []:
        url = action.get("url") if isinstance(action, dict) else None
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if not _GOOGLE_HOST.match((parts.hostname or "").lower()):
            continue
        navigations += 1
        if parts.path.startswith("/sorry"):
            hits += 1
    return hits, navigations


# ---------------------------------------------------------------------------
# Arithmetic.


def _sum(lines: Iterable[dict[str, Any]], key: str) -> float:
    return sum(v for v in (line.get(key) for line in lines)
               if isinstance(v, (int, float)) and not isinstance(v, bool))


def pooled_fps(lines: list[dict[str, Any]]) -> float | None:
    changing = _sum(lines, "changing_seconds")
    return round(_sum(lines, "updates_while_changing") / changing, 2) if changing else None


def pooled_hist(lines: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    """The input-to-frame histograms added. Lines with other bounds are skipped."""
    bounds: list[int] = []
    total: list[int] = []
    for line in lines:
        b, h = line.get("input_to_frame_bounds_ms"), line.get("input_to_frame_hist")
        if not isinstance(b, list) or not isinstance(h, list) or len(h) != len(b) + 1:
            continue
        if not bounds:
            bounds, total = list(b), [0] * len(h)
        if b != bounds:
            continue
        total = [t + int(x) for t, x in zip(total, h)]
    return bounds, total


def hist_p95_bound(bounds: list[int], hist: list[int]) -> str | None:
    """The bucket p95 falls in, as `<=N` ms or `>N`; None with no sample."""
    n = sum(hist)
    if not n:
        return None
    rank, seen = -(-95 * n // 100), 0
    for i, count in enumerate(hist):
        seen += count
        if seen >= rank:
            return f"<={bounds[i]}" if i < len(bounds) else f">{bounds[-1]}"
    return None


def verdict(ok: bool | None) -> str:
    return "NO DATA" if ok is None else ("PASS" if ok else "FAIL")


def build(gate: list[dict[str, Any]], funnels: list[dict[str, Any]], readies: list[dict[str, Any]],
          tasks_dir: Path | None, listed: Iterable[str] = ()) -> dict[str, Any]:
    """The report. `listed`: browser tasks the API listed in the window, so a
    task whose browser never showed anyone a frame still has its row."""
    viewers = [line for line in gate if line.get("role") != "recorder"]
    ids = task_ids(gate, funnels, readies)
    ids += [t for t in listed if t and t not in ids]
    starts: dict[str, dict[str, Any]] = {}
    sorry: dict[str, tuple[int, int]] = {}
    for task in ids:
        if tasks_dir is not None:
            starts[task] = startup_of(_json_file(tasks_dir / f"{task}.startup.json"))
            sorry[task] = sorry_hits(_json_file(tasks_dir / f"{task}.actions.json"))
        else:
            starts[task], sorry[task] = startup_of(None), (0, 0)

    rows = []
    for task in ids:
        mine = [v for v in viewers if v.get("task_id") == task]
        recorders = [v for v in gate if v.get("task_id") == task and v.get("role") == "recorder"]
        bounds, hist = pooled_hist(mine)
        handoffs = [f for f in funnels if f.get("task_id") == task]
        ready = [r.get("seconds") for r in readies if r.get("task_id") == task]
        rows.append({
            "task_id": task,
            **{k: starts[task][k] for k in ("class", "startup_s", "node_s", "pull_s")},
            "viewers": len(mine),
            "recorders": len(recorders),
            "fps_while_changing": pooled_fps(mine),
            "longest_gap_ms": max((v.get("longest_gap_ms") or 0 for v in mine), default=None),
            "blank_s": round(_sum(mine, "blank_ms") / 1000, 1) if mine else None,
            "input_p95": hist_p95_bound(bounds, hist),
            "keys": int(_sum(mine, "keys")),
            "keys_read": int(_sum(mine, "keys_read")),
            "handoffs": len(handoffs),
            "expired": sum(1 for f in handoffs if f.get("outcome") == "expired"),
            "ready_to_frame_s": [s for s in ready if isinstance(s, (int, float))],
            "sorry": sorry[task][0],
            "google": sorry[task][1],
        })

    # The funnel, over every hand-off.
    reached = {name: [f[name] for f in funnels if isinstance(f.get(name), (int, float))]
               for name in ("first_viewer_s", "control_taken_s", "first_input_s")}
    funnel = {
        "requested": len(funnels),
        **{name: {"n": len(v), "p50_s": percentile(sorted(v), 50) if v else None}
           for name, v in reached.items()},
        "outcomes": {o: sum(1 for f in funnels if f.get("outcome") == o)
                     for o in sorted({str(f.get("outcome")) for f in funnels})},
    }

    # The targets.
    fps = pooled_fps(viewers)
    bounds, hist = pooled_hist(viewers)
    samples = sum(hist)
    within = sum(hist[: bounds.index(TARGET_INPUT_P95_MS) + 1]) if (
        samples and TARGET_INPUT_P95_MS in bounds) else 0
    keys, keys_read = int(_sum(viewers, "keys")), int(_sum(viewers, "keys_read"))
    by_attempt = {(t, s["attempt_id"]): s["class"] for t, s in starts.items()}
    ready_by_class: dict[str, list[float]] = {"warm": [], "cold": [], "unknown": []}
    for line in readies:
        seconds = line.get("seconds")
        if isinstance(seconds, (int, float)):
            cls = by_attempt.get((line.get("task_id"), line.get("attempt_id")), "unknown")
            ready_by_class[cls].append(float(seconds))
    present = [f for f in funnels if f.get("person_present") is True]
    expired_present = sum(1 for f in present if f.get("outcome") == "expired")

    def p90(values: list[float]) -> float | None:
        return percentile(sorted(values), 90) if values else None

    warm90, cold90 = p90(ready_by_class["warm"]), p90(ready_by_class["cold"])
    targets = [
        {"target": f"viewer fps while the page changes >= {TARGET_FPS:g}",
         "value": fps, "n": len(viewers),
         "verdict": verdict(None if fps is None else fps >= TARGET_FPS)},
        {"target": f"input_to_frame p95 < {TARGET_INPUT_P95_MS} ms",
         "value": hist_p95_bound(bounds, hist), "n": samples,
         "verdict": verdict(None if not samples else within * 100 >= 95 * samples)},
        {"target": "every key read in control reached the page (100%)",
         "value": f"{keys}/{keys_read}" if keys_read else None, "n": keys_read,
         "verdict": verdict(None if not keys_read else keys >= keys_read)},
        {"target": f"I'm ready -> visible p90 < {TARGET_READY_P90_WARM_S:g} s (warm)",
         "value": warm90, "n": len(ready_by_class["warm"]),
         "verdict": verdict(None if warm90 is None else warm90 < TARGET_READY_P90_WARM_S)},
        {"target": f"I'm ready -> visible p90 < {TARGET_READY_P90_COLD_S:g} s (cold)",
         "value": cold90, "n": len(ready_by_class["cold"]),
         "verdict": verdict(None if cold90 is None else cold90 < TARGET_READY_P90_COLD_S)},
        {"target": f"hand-off expiry < {TARGET_EXPIRY_PRESENT:.0%} when a person is present",
         "value": round(expired_present / len(present), 3) if present else None, "n": len(present),
         "verdict": verdict(None if not present else
                            expired_present < TARGET_EXPIRY_PRESENT * len(present))},
    ]
    return {
        "read": {"viewer_connections": len(viewers), "recorder_connections": len(gate) - len(viewers),
                 "handoffs": len(funnels), "ready_measurements": len(readies), "tasks": len(ids),
                 "ready_unclassified": len(ready_by_class["unknown"])},
        "tasks": rows,
        "funnel": funnel,
        "targets": targets,
    }


def exit_code(report: dict[str, Any]) -> int:
    read = report["read"]
    if not (read["viewer_connections"] or read["recorder_connections"] or read["handoffs"]
            or read["ready_measurements"]):
        return 2
    verdicts = [t["verdict"] for t in report["targets"]]
    if "FAIL" in verdicts:
        return 1
    return 3 if "NO DATA" in verdicts else 0


# ---------------------------------------------------------------------------
# Printing.


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ",".join(_cell(v) for v in value) or "-"
    return str(value)


def render(report: dict[str, Any], hours: float) -> str:
    out = [f"Live browser, last {hours:g} h: {report['read']['tasks']} task(s), "
           f"{report['read']['viewer_connections']} viewer and "
           f"{report['read']['recorder_connections']} recorder connection(s), "
           f"{report['read']['handoffs']} hand-off(s), "
           f"{report['read']['ready_measurements']} ready->frame measurement(s)", ""]
    columns = [("task", "task_id"), ("start", "class"), ("startup_s", "startup_s"),
               ("node_s", "node_s"), ("pull_s", "pull_s"), ("viewers", "viewers"),
               ("fps", "fps_while_changing"), ("gap_ms", "longest_gap_ms"), ("blank_s", "blank_s"),
               ("in->frame_p95", "input_p95"), ("keys", "keys"), ("keys_read", "keys_read"),
               ("handoffs", "handoffs"), ("expired", "expired"),
               ("ready->frame_s", "ready_to_frame_s"), ("sorry", "sorry"), ("google", "google")]
    table = [[name for name, _ in columns]]
    for row in report["tasks"]:
        table.append([_cell(row.get(key)) for _, key in columns])
    widths = [max(len(r[i]) for r in table) for i in range(len(columns))]
    for r in table:
        out.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    f = report["funnel"]
    out += ["", "Hand-off funnel (seconds from the request):",
            f"  requested       {f['requested']}"]
    for name, label in (("first_viewer_s", "viewer joined"), ("control_taken_s", "control taken"),
                        ("first_input_s", "first input")):
        out.append(f"  {label:<15} {f[name]['n']}  p50 {_cell(f[name]['p50_s'])} s")
    out.append("  ended           " + (", ".join(f"{k} {v}" for k, v in f["outcomes"].items()) or "-"))
    if report["read"]["ready_unclassified"]:
        out.append(f"  ({report['read']['ready_unclassified']} ready->frame measurement(s) whose "
                   "attempt's start-up was not read: not judged warm or cold)")
    out += ["", "Phase-2 targets:"]
    for t in report["targets"]:
        out.append(f"  {t['verdict']:<8} {t['target']}: {_cell(t['value'])} (n={t['n']})")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The listing helpers the shell walks.


def window_lines(page: dict[str, Any], *, hours: float, now: str | None = None) -> list[str]:
    """`task <id>` per task created inside the window, then `older` or `next <token>`."""
    at = parse_time(now) if now else datetime.now(timezone.utc)
    assert at is not None
    cutoff = at - timedelta(hours=hours)
    lines: list[str] = []
    for task in page.get("tasks") or []:
        created = parse_time(task.get("created_at"))
        if created is not None and created < cutoff:
            lines.append("older")
            return lines
        if task.get("id"):
            lines.append(f"task {task['id']}")
    token = page.get("next_page_token")
    if token:
        lines.append(f"next {quote(str(token), safe='')}")
    return lines


def live_profiles() -> list[str]:
    try:
        from swarm_common.profiles import RUNNER_PROFILES
    except ImportError:  # pragma: no cover - run outside the project's venv
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "common"))
        from swarm_common.profiles import RUNNER_PROFILES
    return sorted(name for name, p in RUNNER_PROFILES.items() if getattr(p, "live_browser", False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_report.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profiles", help="the live-browser runner profiles, one per line")
    w = sub.add_parser("window", help="a GET /tasks page on stdin -> tasks inside the window")
    w.add_argument("--hours", type=float, required=True)
    w.add_argument("--now")
    for name in ("tasks", "report"):
        p = sub.add_parser(name)
        p.add_argument("--gate", type=Path)
        p.add_argument("--api", type=Path)
        p.add_argument("--hours", type=float, default=24.0)
        p.add_argument("--now")
        if name == "report":
            p.add_argument("--tasks-dir", type=Path)
            p.add_argument("--listed", type=Path, help="browser task ids the API listed, one per line")
            p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "profiles":
        print("\n".join(live_profiles()))
        return 0
    if args.cmd == "window":
        print("\n".join(window_lines(json.loads(sys.stdin.read() or "{}"), hours=args.hours,
                                     now=args.now)))
        return 0
    try:
        now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=args.hours) if now else None
        gate_entries, api_entries = _entries(args.gate), _entries(args.api)
    except ValueError as exc:
        print(f"live_report: {exc}", file=sys.stderr)
        return 2
    gate = viewer_lines(gate_entries, cutoff=cutoff)
    funnels = api_lines(api_entries, FUNNEL_EVENT, cutoff=cutoff)
    readies = api_lines(api_entries, READY_EVENT, cutoff=cutoff)
    if args.cmd == "tasks":
        print("\n".join(task_ids(gate, funnels, readies)))
        return 0
    listed = args.listed.read_text().split() if args.listed and args.listed.exists() else []
    report = build(gate, funnels, readies, args.tasks_dir, listed)
    code = exit_code(report)
    if args.json:
        print(json.dumps({**report, "exit": code}, indent=2, sort_keys=True))
    else:
        print(render(report, args.hours))
        if code == 2:
            print("\nNOTHING READ: no gate metrics line and no hand-off line in the window. "
                  "That is not a pass.")
    return code


if __name__ == "__main__":
    sys.exit(main())
