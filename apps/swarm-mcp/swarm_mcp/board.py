"""The board: where each running workflow stands, and the order they are listed in.

Owner decisions, 2026-10-16 (#1437, BOARD-ORDER):

* ORDER. Every listing of running workflows -- `swarm_workflows`, `sc
  workflows`, `/sc attach --all` (its launch order, and which ones a cap leaves
  out) and `/sc check`'s attach -- puts the one CLOSEST TO FINISH first: steps
  done / steps total, descending; ties by the current step's age, descending
  (the step that has run longest first); then the id, so the order is total and
  two reads of the same state list the same order. It REPLACES the "newest
  first" half of the 2026-10-14 SC-ROWS order. The workspace blocks stay
  (`sc.group_by_workspace`): the blocks come in the order of each block's
  closest-to-finish entry, and this order holds inside each.
* MEDIANS are SHOWN, never used to order, and only because they are cheap: at
  most `MEDIAN_PAGES` reads per `MEDIAN_TTL_SECONDS` for the whole bridge,
  never a read per workflow per refresh.

A single task has no steps: it is ordered by the same key, as zero steps of
one done, so its own state age decides (the step that has run longest first).
"""

from __future__ import annotations

import statistics
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_common.states import TERMINAL_STATES

from .render import parse_time

#: What a step counts as FINISHED: a task in a terminal state, the rule
#: `sc._read_detail` already used to leave a step out of `current_steps` (and
#: the rollup uses to end a workflow). A step that failed under
#: `on_step_failure: continue` is terminal and so done; a step whose failure
#: ends the workflow ends its listing too. Read from the frozen contract.
FINISHED: frozenset[str] = frozenset(s.value for s in TERMINAL_STATES)

#: How far back the medians look: a week of SUCCEEDED tasks, so a profile's
#: median follows its recent prompts and images rather than its whole history.
MEDIAN_WINDOW_DAYS = 7
#: Rows per page and pages per read: `GET /v1/tasks?state=SUCCEEDED&view=summary`
#: is newest first, 100 rows a page, so 3 pages are the newest 300 successes --
#: enough for a median per (profile, step) a tenant runs often, and a bounded
#: cost for one that has thousands. `view=summary` leaves out input and
#: result_summary and cuts metadata to its title, the heavy fields; the timing
#: and the step stay.
MEDIAN_PAGE_SIZE = 100
MEDIAN_PAGES = 3
#: How long one read of the medians is reused, in this process: 15 minutes, so
#: the whole bridge costs at most MEDIAN_PAGES reads per 15 minutes however
#: many listings it answers (owner, 2026-10-16: "at most a few reads per 15
#: minutes for the whole bridge"). A FAILED read is remembered as long, for the
#: same bound: a deployment that refuses it is not asked again on every listing.
MEDIAN_TTL_SECONDS = 15 * 60

_lock = threading.Lock()
_cache: dict[tuple[Any, Any], tuple[float, dict[str, Any]]] = {}


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------


def _seconds(since: datetime | None, now: datetime) -> int | None:
    return max(0, int((now - since).total_seconds())) if since is not None else None


def progress(envelope: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Where one workflow stands, from the envelope `workflows.fetch` returns.

    `done` / `total` steps; `current` -- the unfinished steps whose parents
    have all finished (running, or free to run), each with its age; `ahead`
    -- the unfinished steps still waiting on an unfinished parent. A current
    step's age runs from its task's `started_at` (the last attempt's start);
    one not started yet counts from when its last parent finished
    (`completed_at`), else from its task's `created_at`. `age_basis` says
    which. `age_seconds` is the oldest current step's, the one the order reads.
    """
    from .workflows import step_rows

    rows = step_rows(envelope)
    tasks = {
        str(t["id"]): t for t in envelope.get("tasks") or [] if isinstance(t, dict) and t.get("id")
    }
    task_of_step = {row.get("step_id"): tasks.get(str(row.get("task_id"))) for row in rows}
    finished = {row.get("step_id") for row in rows if row.get("state") in FINISHED}
    current: list[dict[str, Any]] = []
    ahead: list[dict[str, Any]] = []
    for row in rows:
        if row.get("state") in FINISHED:
            continue
        parents = list(row.get("depends_on") or [])
        if any(parent not in finished for parent in parents):
            ahead.append({"step_id": row.get("step_id"), "runner_profile": row.get("runner_profile"),
                          "depends_on": parents})
            continue
        task = task_of_step.get(row.get("step_id")) or {}
        since, basis = parse_time(task.get("started_at")), "started_at"
        if since is None:
            ends = [parse_time((task_of_step.get(p) or {}).get("completed_at")) for p in parents]
            ends = [end for end in ends if end is not None]
            since, basis = (max(ends), "parents_completed_at") if ends else (
                parse_time(task.get("created_at")), "created_at")
        age = _seconds(since, now)
        current.append({
            "step_id": row.get("step_id"),
            "task_id": row.get("task_id"),
            "state": row.get("state"),
            "runner_profile": row.get("runner_profile"),
            "age_seconds": age,
            "age_basis": basis if age is not None else None,
        })
    total = len(rows)
    done = len(finished)
    ages = [step["age_seconds"] for step in current if step["age_seconds"] is not None]
    return {
        "done": done,
        "total": total,
        "fraction": round(done / total, 4) if total else 0.0,
        "current": current,
        "ahead": ahead,
        "age_seconds": max(ages) if ages else None,
    }


# --------------------------------------------------------------------------
# Order
# --------------------------------------------------------------------------


def _key(entry: dict[str, Any]) -> tuple[float, float, str]:
    """Closest to finish first. A workflow whose progress was not read sorts
    below every one that was (fraction -1): nobody has shown it is close. An
    entry with no progress at all -- a single task -- is zero steps done, and
    its own state age (`state_age_seconds`, else its age) decides."""
    p = entry.get("progress")
    if isinstance(p, dict):
        total = p.get("total") or 0
        fraction = (p.get("done") or 0) / total if total else 0.0
        age = p.get("age_seconds")
    elif "workflow_id" in entry:
        fraction, age = -1.0, entry.get("age_seconds")
    else:
        fraction = 0.0
        age = entry.get("state_age_seconds")
        age = entry.get("age_seconds") if age is None else age
    age_value = float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else -1.0
    ident = str(entry.get("workflow_id") or entry.get("task_id") or "")
    return (-fraction, -age_value, ident)


def order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`entries` closest to finish first: steps done / total descending, then
    the current step's age descending, then the id. A new list; the entries
    are the same objects."""
    return sorted(entries, key=_key)


# --------------------------------------------------------------------------
# Historical medians
# --------------------------------------------------------------------------


def forget_medians() -> None:
    """Drop every remembered read (tests; a bridge never needs to)."""
    with _lock:
        _cache.clear()


def _read_medians(client: Any, now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=MEDIAN_WINDOW_DAYS)
    durations: dict[tuple[str, str], list[float]] = {}
    token: str | None = None
    pages = 0
    rows = 0
    while pages < MEDIAN_PAGES:
        params = [("state", "SUCCEEDED"), ("view", "summary"), ("limit", str(MEDIAN_PAGE_SIZE))]
        if token:
            params.append(("page_token", token))
        data = client.request("GET", f"/v1/tasks?{urllib.parse.urlencode(params)}")
        pages += 1
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("GET /v1/tasks answered without a `tasks` field")
        older = False
        for task in data["tasks"]:
            if not isinstance(task, dict):
                continue
            rows += 1
            created = parse_time(task.get("created_at"))
            if created is not None and created < since:
                older = True
            started = parse_time(task.get("started_at"))
            completed = parse_time(task.get("completed_at"))
            profile, step = task.get("runner_profile"), task.get("step_id")
            if (started is None or completed is None or completed < since or completed < started
                    or not isinstance(profile, str) or not isinstance(step, str)):
                continue
            durations.setdefault((profile, step), []).append((completed - started).total_seconds())
        token = data.get("next_page_token") or None
        # Newest first by creation: a page that reached past the window ends
        # the walk, as does the last page.
        if token is None or older:
            break
    return {
        "by_step": {key: int(statistics.median(values)) for key, values in durations.items()},
        "window_days": MEDIAN_WINDOW_DAYS,
        "requests": pages,
        "tasks_read": rows,
        "read_at": now.isoformat(),
    }


def medians(client: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """p50 seconds per (runner_profile, step_id), from this tenant's SUCCEEDED
    step tasks of the last `MEDIAN_WINDOW_DAYS` days (`started_at` to
    `completed_at`), read at most `MEDIAN_PAGES` pages at a time and reused
    for `MEDIAN_TTL_SECONDS` per deployment and tenant. A read that fails
    comes back as `unavailable_because` with no medians -- never raised: a
    listing never fails for want of a median."""
    key = (getattr(client, "base_url", None), getattr(client, "tenant", None))
    now = now or datetime.now(timezone.utc)
    # Held across the read, so concurrent listings make ONE read between them.
    with _lock:
        held = _cache.get(key)
        if held is not None and time.monotonic() - held[0] < MEDIAN_TTL_SECONDS:
            return {**held[1], "reused": True}
        try:
            found = _read_medians(client, now)
        except Exception as exc:  # noqa: BLE001 - a median is never worth a failed listing
            found = {"by_step": {}, "window_days": MEDIAN_WINDOW_DAYS,
                     "unavailable_because": f"the historical medians could not be read: {exc}"}
        _cache[key] = (time.monotonic(), found)
        return found


def _remaining(
    step_id: Any, finish: dict[Any, int | None], ahead: dict[Any, dict[str, Any]], known: dict[tuple[str, str], int],
    seen: frozenset[Any] = frozenset(),
) -> int | None:
    """When a step still ahead is expected to finish, from now: its median
    after the latest of its unfinished parents. None when any is unknown."""
    if step_id in finish:
        return finish[step_id]
    step = ahead.get(step_id)
    if step is None or step_id in seen:
        return 0
    own = known.get((step.get("runner_profile"), step_id))
    parents = [_remaining(p, finish, ahead, known, seen | {step_id}) for p in step.get("depends_on") or []]
    result = None if own is None or any(p is None for p in parents) else own + max(parents, default=0)
    finish[step_id] = result
    return result


def annotate(entry: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """`entry["progress"]` with the medians shown beside it: each current step
    gains `median_seconds`; the progress gains `remaining_median_seconds` (the
    sum of the medians of the steps not started) and `eta_seconds` -- each
    current step's median minus its age, floored at 0, followed by the medians
    of the steps after it along the longest path, so two steps that run side
    by side are not counted twice. On a chain that is exactly the current
    step's remainder plus the sum. None wherever a step has no history."""
    p = entry.get("progress")
    if not isinstance(p, dict):
        return entry
    known = found.get("by_step") or {}
    finish: dict[Any, int | None] = {}
    for step in p.get("current") or []:
        median = known.get((step.get("runner_profile"), step.get("step_id")))
        step["median_seconds"] = median
        age = step.get("age_seconds")
        finish[step.get("step_id")] = (
            None if median is None or age is None else max(0, median - age)
        )
    ahead = {step.get("step_id"): step for step in p.get("ahead") or []}
    medians_ahead = [known.get((step.get("runner_profile"), sid)) for sid, step in ahead.items()]
    p["remaining_median_seconds"] = (
        None if any(m is None for m in medians_ahead) else sum(m for m in medians_ahead if m is not None)
    )
    ends = list(finish.values()) + [_remaining(sid, finish, ahead, known) for sid in ahead]
    p["eta_seconds"] = None if any(end is None for end in ends) else max(ends, default=0)
    return entry
