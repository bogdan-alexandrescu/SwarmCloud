#!/usr/bin/env python3
"""Where a GKE start-up's seconds go, and whether a browser pod fits a smaller class (START-MEASURE).

The owner, 2026-10-13 (docs/browser-startup-speed.md §9, decisions 3 and 6):
warm placeholder slots and a smaller `browser` class are both decided AFTER
they are measured. This file is that measurement's arithmetic.
`scripts/startup-measure.sh` does the reading -- it needs a credential, the
API and a kubectl pointed at `swarm-autopilot` -- and everything that decides
a number lives here, taking what was read on stdin and in a file, so it is
unit-tested with no credentials (tests/unit/scripts/test_startup_measure.py).
The shell decides nothing.

THE START-UP INPUT is a stream of JSON documents, each a response body exactly
as the API served it: `GET /v1/tasks` pages (for each task's `runner_profile`
and `resource_class`) and `GET /v1/tasks/{id}/events` pages. One START is one
attempt that has a `dispatched` event and at least one `startup_progress`
step. Its steps are read the way `swarm_api.startup` reads them: at the
generation its earliest `dispatched` was written under (or an event that names
none), each step at its EARLIEST time -- so a fenced worker's late write or a
second attempt's pod is never this start-up (invariant 5).

THE SEGMENTS, each the pair of steps docs/browser-startup-speed.md §1 names:

  * `node_wait` -- `dispatched` -> `pod_scheduled`. On Autopilot mostly the wait
    for a new node, because a pod is bound only once its node exists.
  * `pull`      -- `image_pulling` -> `image_pulled`: the image pull plus one
    container create (`reconciler.startsteps` says why that is the nearest
    the pod's status can date it).
  * `browser`   -- `container_started` -> `first_frame`: after the worker
    container started, until the live stream's first frame.
  * `total`     -- `dispatched` -> `first_frame`.

A start is COLD when its node wait is OVER `--cold-seconds` (10 s, the doc's
§7 line between "a node was there" and "a node was made"), WARM when it is at
or under, and neither when its pod was never scheduled -- such a start is in
`n` but not in the cold share's denominator.

THE POD INPUT (`--top FILE`) is `kubectl top pods -A --containers --no-headers`
output, one block per sample under a `# sample <time>` line. A pod's use in one
sample is the sum of its containers; its peak is the largest of those sums.
The verdict compares the largest pod peak with the CANDIDATE class (default
`standard`, 4 vCPU / 8 GiB) times `HEADROOM`. Both classes are read from the
frozen catalogue (`swarm_common.profiles.RESOURCE_CLASSES`), never restated.

THE RULES, each the failure it prevents:

* Percentiles are nearest-rank (`benchstat.percentile`): every number printed
  is a start that happened.
* A segment no start reached is `-`, never 0.
* Reading no start-up exits 2; sampling no pod is NOT_MEASURED and exits 1,
  never FITS (CLAUDE.md's empty-output rule).

Exit codes: 0 both parts measured (or no `--top` asked for), 1 a pod sample was
asked for and none was read, 2 no start-up read or bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchstat import percentile  # noqa: E402  (nearest-rank, stdlib-only)

try:
    from swarm_common.profiles import RESOURCE_CLASSES
except ImportError:  # pragma: no cover - run outside the project's venv
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "common"))
    from swarm_common.profiles import RESOURCE_CLASSES

UNKNOWN = "unknown"
DEFAULT_SINCE = "7d"
#: docs/browser-startup-speed.md §7: "`pod_scheduled - dispatched` over 10 s
#: means cold". A node that already had room binds the pod in a second or two.
DEFAULT_COLD_SECONDS = 10.0
#: The class being sized, and the one it would shrink to (4 vCPU / 8 GiB).
DEFAULT_CLASS = "browser"
DEFAULT_CANDIDATE = "standard"
#: The share of the candidate's limits a measured peak may use. Not 1.0:
#: requests == limits (invariant 7), so there is no burst above the limit, a
#: memory overshoot is an OOM kill, and `kubectl top` samples miss whatever
#: peak fell between two samples. 20 % is that margin.
HEADROOM = 0.8

DISPATCHED = "dispatched"
STARTUP_PROGRESS = "startup_progress"

#: Segment name -> (from step, to step), in the order the table prints them.
SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("node_wait", DISPATCHED, "pod_scheduled"),
    ("pull", "image_pulling", "image_pulled"),
    ("browser", "container_started", "first_frame"),
    ("total", DISPATCHED, "first_frame"),
)
STARTS = ("all", "cold", "warm")

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


# ---------------------------------------------------------------------------
# Reading.


def parse_duration(text: str) -> int:
    """`30m`, `36h`, `7d` -> seconds."""
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(f"not a duration: {text!r} (expected e.g. 36h, 7d)")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cutoff_for(since: str, now: str | None) -> datetime:
    at = parse_time(now) if now else datetime.now(timezone.utc)
    if at is None:
        raise ValueError(f"not a timestamp: {now!r}")
    return at - timedelta(seconds=parse_duration(since))


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
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise ValueError(f"unreadable JSON at character {index}: {exc.msg}") from None
        if isinstance(value, list):
            docs.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            docs.append(value)


def _read(docs: Iterable[dict[str, Any]]) -> tuple[dict[str, tuple[str, str]], dict[str, dict[str, Any]]]:
    """Each task's (profile, class), and every event once (pages overlap)."""
    labels: dict[str, tuple[str, str]] = {}
    events: dict[str, dict[str, Any]] = {}
    for doc in docs:
        for task in doc.get("tasks") or []:
            if isinstance(task, dict) and task.get("id"):
                labels[str(task["id"])] = (
                    str(task.get("runner_profile") or UNKNOWN),
                    str(task.get("resource_class") or UNKNOWN),
                )
        task_id = doc.get("task_id")
        for index, event in enumerate(doc.get("events") or []):
            if not isinstance(event, dict):
                continue
            event.setdefault("task_id", task_id)
            key = str(event.get("event_id") or f"{task_id}:{len(events)}:{index}")
            events[key] = event
    return labels, events


def _step_of(event: dict[str, Any]) -> str | None:
    kind = event.get("type")
    if kind == STARTUP_PROGRESS:
        detail = event.get("detail")
        step = detail.get("step") if isinstance(detail, dict) else None
        return step if isinstance(step, str) and step else None
    return DISPATCHED if kind == DISPATCHED else None


def attempt_times(events: list[dict[str, Any]]) -> dict[str, datetime]:
    """One attempt's steps at the dispatching generation, each at its earliest time."""
    dispatched = [
        (at, e) for e in events if e.get("type") == DISPATCHED and (at := parse_time(e.get("at")))
    ]
    if not dispatched:
        return {}
    generation = min(dispatched, key=lambda pair: pair[0])[1].get("generation")
    times: dict[str, datetime] = {}
    for event in events:
        step = _step_of(event)
        at = parse_time(event.get("at"))
        stamped = event.get("generation")
        if step is None or at is None:
            continue
        if generation is not None and stamped is not None and stamped != generation:
            continue
        if step not in times or at < times[step]:
            times[step] = at
    return times


def collect(docs: Iterable[dict[str, Any]], cutoff: datetime) -> dict[str, Any]:
    """Every start inside the window, labelled, and what was visited on the way."""
    labels, events = _read(docs)
    by_attempt: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events.values():
        attempt_id = event.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id:
            by_attempt.setdefault((str(event.get("task_id")), attempt_id), []).append(event)
    starts: list[dict[str, Any]] = []
    visited = {
        "tasks": len({task for task, _ in by_attempt} | set(labels)),
        "attempts": 0,
        "attempts_without_steps": 0,
        "outside_window": 0,
    }
    for (task_id, attempt_id), attempt_events in sorted(by_attempt.items()):
        times = attempt_times(attempt_events)
        if DISPATCHED not in times:
            continue
        visited["attempts"] += 1
        if times[DISPATCHED] < cutoff:
            visited["outside_window"] += 1
            continue
        if len(times) == 1:
            # Dispatched, and no pod or browser step: a Cloud Run attempt, or
            # one that ended before its pod was read. Not a start to measure.
            visited["attempts_without_steps"] += 1
            continue
        profile, rc = labels.get(task_id, (UNKNOWN, UNKNOWN))
        starts.append(
            {"task_id": task_id, "attempt_id": attempt_id, "runner_profile": profile,
             "resource_class": rc, "times": times}
        )
    return {"starts": starts, "visited": visited}


# ---------------------------------------------------------------------------
# Arithmetic.


def segment_seconds(times: dict[str, datetime], start: str, end: str) -> float | None:
    if start in times and end in times:
        return (times[end] - times[start]).total_seconds()
    return None


def summarize(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None}
    return {"n": len(ordered), "p50": percentile(ordered, 50), "p95": percentile(ordered, 95)}


def start_kind(times: dict[str, datetime], cold_seconds: float) -> str | None:
    wait = segment_seconds(times, DISPATCHED, "pod_scheduled")
    if wait is None:
        return None
    return "cold" if wait > cold_seconds else "warm"


def build_rows(starts: list[dict[str, Any]], cold_seconds: float) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in starts:
        groups.setdefault((s["runner_profile"], s["resource_class"]), []).append(s)
    rows: list[dict[str, Any]] = []
    for (profile, rc), members in sorted(groups.items()):
        kinds = [start_kind(s["times"], cold_seconds) for s in members]
        cold = kinds.count("cold")
        classified = cold + kinds.count("warm")
        for start in STARTS:
            chosen = [s for s, k in zip(members, kinds) if start == "all" or k == start]
            row: dict[str, Any] = {
                "runner_profile": profile,
                "resource_class": rc,
                "start": start,
                "n": len(chosen),
                "segments": {
                    name: summarize(
                        [v for s in chosen if (v := segment_seconds(s["times"], a, b)) is not None]
                    )
                    for name, a, b in SEGMENTS
                },
            }
            if start == "all":
                row["cold"] = cold
                row["classified"] = classified
                row["cold_share"] = cold / classified if classified else None
            rows.append(row)
    return rows


_CPU = re.compile(r"^(\d+(?:\.\d+)?)([numk]?)$")
_CPU_SCALE = {"": 1.0, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3}
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T|k)?$")
_MEMORY_SCALE = {
    None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
}


def parse_cpu(text: str) -> float:
    """A Kubernetes CPU quantity (`250m`, `2`, `1500000000n`) -> cores."""
    match = _CPU.match(text.strip())
    if not match:
        raise ValueError(f"not a CPU quantity: {text!r}")
    return float(match.group(1)) * _CPU_SCALE[match.group(2)]


def parse_memory(text: str) -> int:
    """A Kubernetes memory quantity (`512Mi`, `2Gi`, `1G`, `4096`) -> bytes."""
    match = _MEMORY.match(text.strip())
    if not match:
        raise ValueError(f"not a memory quantity: {text!r}")
    return int(float(match.group(1)) * _MEMORY_SCALE[match.group(2)])


def pod_usage(text: str) -> dict[str, Any]:
    """Peaks from `kubectl top pods -A --containers --no-headers`, one `# sample` block per sample."""
    samples: list[dict[str, dict[str, tuple[float, int]]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# sample"):
            samples.append({})
            continue
        if line.startswith("#"):
            continue
        cols = line.split()
        if len(cols) == 5:
            namespace, pod, container, cpu, memory = cols
        elif len(cols) == 4:
            namespace, (pod, container, cpu, memory) = "", cols
        else:
            raise ValueError(f"not a `kubectl top --containers` line: {line!r}")
        if not samples:
            samples.append({})
        samples[-1].setdefault(f"{namespace}/{pod}", {})[container] = (parse_cpu(cpu), parse_memory(memory))
    pods: set[str] = set()
    totals_cpu: list[float] = []
    totals_memory: list[int] = []
    containers: dict[str, dict[str, Any]] = {}
    for sample in samples:
        for pod, by_container in sample.items():
            pods.add(pod)
            totals_cpu.append(sum(c for c, _ in by_container.values()))
            totals_memory.append(sum(m for _, m in by_container.values()))
            for name, (cpu, memory) in by_container.items():
                peak = containers.setdefault(name, {"peak_cpu": 0.0, "peak_memory_bytes": 0})
                peak["peak_cpu"] = max(peak["peak_cpu"], cpu)
                peak["peak_memory_bytes"] = max(peak["peak_memory_bytes"], memory)
    return {
        "samples": len(samples),
        "pods": len(pods),
        "pod_samples": len(totals_cpu),
        "peak_cpu": max(totals_cpu) if totals_cpu else None,
        "p95_cpu": percentile(sorted(totals_cpu), 95) if totals_cpu else None,
        "peak_memory_bytes": max(totals_memory) if totals_memory else None,
        "p95_memory_bytes": percentile(sorted(totals_memory), 95) if totals_memory else None,
        "containers": containers,
    }


def _class(name: str) -> dict[str, Any]:
    try:
        rc = RESOURCE_CLASSES[name]
    except KeyError:
        raise ValueError(f"unknown resource class {name!r} (one of {', '.join(sorted(RESOURCE_CLASSES))})") from None
    return {"name": rc.name, "cpu": float(rc.cpu), "memory_gib": rc.memory_gib}


def sizing(usage: dict[str, Any], *, measured: str, candidate: str) -> dict[str, Any]:
    current, target = _class(measured), _class(candidate)
    out: dict[str, Any] = {**usage, "current": current, "candidate": target, "headroom": HEADROOM}
    if not usage["pod_samples"]:
        out["verdict"] = "NOT_MEASURED"
        out["reason"] = (
            f"no `{measured}` pod was running in {usage['samples']} sample(s); "
            "sample again while browser agents are running"
        )
        return out
    cpu_bar = target["cpu"] * HEADROOM
    memory_bar = target["memory_gib"] * HEADROOM * 1024**3
    over: list[str] = []
    if usage["peak_cpu"] > cpu_bar:
        over.append(f"peak CPU {usage['peak_cpu']:.2f} cores > {cpu_bar:.2f} ({HEADROOM:.0%} of {target['cpu']:g})")
    if usage["peak_memory_bytes"] > memory_bar:
        over.append(
            f"peak memory {_gib(usage['peak_memory_bytes'])} GiB > {memory_bar / 1024**3:.2f} "
            f"({HEADROOM:.0%} of {target['memory_gib']})"
        )
    if over:
        out["verdict"] = "DOES_NOT_FIT"
        out["reason"] = "; ".join(over)
    else:
        out["verdict"] = "FITS"
        out["reason"] = (
            f"peak {usage['peak_cpu']:.2f} cores and {_gib(usage['peak_memory_bytes'])} GiB, both under "
            f"{HEADROOM:.0%} of {target['cpu']:g} vCPU / {target['memory_gib']} GiB"
        )
    return out


def build_report(
    docs: Iterable[dict[str, Any]],
    *,
    since: str = DEFAULT_SINCE,
    now: str | None = None,
    cold_seconds: float = DEFAULT_COLD_SECONDS,
    top_text: str | None = None,
    measured_class: str = DEFAULT_CLASS,
    candidate_class: str = DEFAULT_CANDIDATE,
) -> dict[str, Any]:
    cutoff = cutoff_for(since, now)
    read = collect(docs, cutoff)
    result: dict[str, Any] = {
        "since": since,
        "cutoff": cutoff.isoformat(),
        "cold_seconds": cold_seconds,
        "rows": build_rows(read["starts"], cold_seconds),
        "visited": read["visited"],
        "starts": len(read["starts"]),
        "sizing": None,
    }
    if top_text is not None:
        result["sizing"] = sizing(pod_usage(top_text), measured=measured_class, candidate=candidate_class)
    return result


# ---------------------------------------------------------------------------
# Rendering.


def _s(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _gib(value: int | None) -> str:
    return "-" if value is None else f"{value / 1024**3:.2f}"


def _cell(summary: dict[str, Any]) -> str:
    if not summary["n"]:
        return "-"
    return f"{_s(summary['p50'])}/{_s(summary['p95'])} n={summary['n']}"


def _warm_slot_lines(rows: list[dict[str, Any]], cold_seconds: float) -> list[str]:
    lines: list[str] = []
    by_key = {(r["runner_profile"], r["resource_class"], r["start"]): r for r in rows}
    for r in rows:
        if r["start"] != "all":
            continue
        key = (r["runner_profile"], r["resource_class"])
        if not r["classified"]:
            lines.append(f"  {key[0]} ({key[1]}): no start reached pod_scheduled; cold share not measured")
            continue
        cold = by_key[(*key, "cold")]
        share = f"{r['cold']}/{r['classified']} ({r['cold_share']:.0%})"
        if not r["cold"]:
            lines.append(f"  {key[0]} ({key[1]}): {share} cold; a warm slot would have saved nothing measured")
            continue
        wait, pull = cold["segments"]["node_wait"], cold["segments"]["pull"]
        lines.append(
            f"  {key[0]} ({key[1]}): {share} cold (node wait > {cold_seconds:g} s); a warm slot "
            f"removes a cold start's node wait, p50/p95 {_s(wait['p50'])}/{_s(wait['p95'])} s, "
            f"and with the image on the node its pull, p50/p95 {_s(pull['p50'])}/{_s(pull['p95'])} s"
        )
    return lines


def render(result: dict[str, Any]) -> str:
    seg_names = [name for name, _, _ in SEGMENTS]
    header = f"{'profile':<24} {'class':<10} {'start':<5} {'n':>4} {'cold':>11}  " + "  ".join(
        f"{name + ' p50/p95 s':<24}" for name in seg_names
    )
    lines = [
        f"start-up by runner_profile and resource class (since {result['since']}, from {result['cutoff']}; "
        f"cold = node wait > {result['cold_seconds']:g} s; nearest-rank)",
        header.rstrip(),
    ]
    for r in result["rows"]:
        cold = f"{r['cold']}/{r['classified']} ({r['cold_share']:.0%})" if r.get("cold_share") is not None else ""
        if r["start"] == "all" and r.get("cold_share") is None:
            cold = "-"
        cells = "  ".join(f"{_cell(r['segments'][name]):<24}" for name in seg_names)
        lines.append(
            f"{r['runner_profile']:<24} {r['resource_class']:<10} {r['start']:<5} {r['n']:>4} {cold:>11}  {cells}".rstrip()
        )
    v = result["visited"]
    lines.append(
        f"visited: {v['tasks']} task(s), {v['attempts']} dispatched attempt(s); "
        f"{result['starts']} start(s) in the window, {v['attempts_without_steps']} without a startup step, "
        f"{v['outside_window']} outside it"
    )
    lines.append("")
    lines.append("verdict")
    lines.append("warm slots:")
    lines.extend(_warm_slot_lines(result["rows"], result["cold_seconds"]))
    size = result["sizing"]
    if size is not None:
        cand, cur = size["candidate"], size["current"]
        lines.append(
            f"{cand['cpu']:g} vCPU / {cand['memory_gib']} GiB (`{cand['name']}`) for `{cur['name']}` pods "
            f"(now {cur['cpu']:g} vCPU / {cur['memory_gib']} GiB): {size['verdict']} -- {size['reason']}"
        )
        if size["pod_samples"]:
            lines.append(
                f"  {size['pods']} pod(s), {size['samples']} sample(s): pod CPU peak/p95 "
                f"{size['peak_cpu']:.2f}/{size['p95_cpu']:.2f} cores, memory peak/p95 "
                f"{_gib(size['peak_memory_bytes'])}/{_gib(size['p95_memory_bytes'])} GiB"
            )
            for name, peak in sorted(size["containers"].items()):
                lines.append(
                    f"  container {name}: peak {peak['peak_cpu']:.2f} cores, {_gib(peak['peak_memory_bytes'])} GiB"
                )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Paging helpers for the shell script, which parses no JSON itself.


def window_lines(page: dict[str, Any], *, since: str, now: str | None = None) -> list[str]:
    """`task <id>` per task inside the window, then `older` or `next <token>`.

    The task list is newest first, so the first task older than the window
    ends the walk. A row with no readable `created_at` is kept: its attempts
    are filtered by their own `dispatched` time anyway.
    """
    cutoff = cutoff_for(since, now)
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

    rep = sub.add_parser("report", help="API responses on stdin (and --top) -> the table and the verdict")
    rep.add_argument("--since", default=DEFAULT_SINCE)
    rep.add_argument("--cold-seconds", type=float, default=DEFAULT_COLD_SECONDS)
    rep.add_argument("--top", default=None, help="file of `kubectl top` samples")
    rep.add_argument("--class", dest="measured_class", default=DEFAULT_CLASS)
    rep.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    rep.add_argument("--json", action="store_true")
    rep.add_argument("--now", default=None, help="ISO timestamp the window ends at (tests)")

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
        top_text = Path(args.top).read_text() if args.top else None
        result = build_report(
            read_documents(text),
            since=args.since,
            now=args.now,
            cold_seconds=args.cold_seconds,
            top_text=top_text,
            measured_class=args.measured_class,
            candidate_class=args.candidate,
        )
    except (ValueError, OSError) as exc:
        print(f"startup_measure: {exc}", file=sys.stderr)
        return 2

    if not result["starts"]:
        v = result["visited"]
        print(
            f"startup_measure: no startup_progress step read in the window "
            f"({v['tasks']} task(s), {v['attempts']} dispatched attempt(s) visited)",
            file=sys.stderr,
        )
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result))
    size = result["sizing"]
    return 1 if size is not None and size["verdict"] == "NOT_MEASURED" else 0


if __name__ == "__main__":
    sys.exit(main())
