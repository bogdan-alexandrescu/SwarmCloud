"""What swarm-graph's answers cost, added up (#1840, #4030).

Two reports over data that already exists, both read-only:

  * THE COLD P95 PER PROMOTED VERSION (#1840; docs/design/graph-answer-cache.md
    §1.2-§1.3). The indexer, when a run asks it to, times its answer battery
    over the snapshot it has just written and puts `latency.json` beside the
    manifest (`graph/<sha>/latency.json`, never named IN the manifest, so the
    manifest digest never depends on a timing). `measurements` reads the newest
    `window` promoted versions (`index_versions`, which only promotion writes)
    and, for each, the version's recorded `answer_latency` when promotion
    copied one, else that file from the artifact store. A version with neither
    is unmeasured, never a zero. `decide` is the schedule type
    `graph-latency-report`'s whole rule: the newest measured run's p95 over its
    own `reference_ms` at or over `ratio_threshold` opens or comments one issue;
    the milliseconds are reported beside the ratio but never decide (the owner,
    Q3).
  * SEVEN DAYS OF GRAPH DELIVERY (#4030). Every attempt that reached the graph
    step records one `graph_ready` start-up mark on its task's events
    (agent_worker/graphstage.py `ready_detail`, a RUNNING event with
    `cause: graph_ready`): `graph.delivery` (`service`, `staged` or `none`) and
    `graph.reason`. An attempt whose workspace asked for the service and was
    staged instead says so in its reason, after `FALLBACK_SAID`.
    `delivery_report` counts a tenant's attempts of the window by delivery and
    the fallbacks by reason, per repository, and `served_latency` puts the
    replicas' merged served p50/p95 (routes/graph_cache.py) beside them against
    the 300 ms design target.

WHY A WORKSPACE IS A TENANT HERE. Every read is the schedule's or the caller's
own tenant's (invariant 9): attempts through `store.page_attempts(tenant)`,
re-filtered on `tenant_id`; tasks through `store.tasks_by_id(tenant)`; versions
under a registration `Repositories.find` found in that tenant; objects only
under `tenants/<tenant>/repos/<repo_id>/graph/`.

Nothing here touches a task's state, a lease or capacity (invariants 1-3), and
nothing logs or returns a credential: the marks hold numbers and fixed
sentences only.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter

from .errors import ApiError
from .objects import ObjectAbsent, ObjectUnreadable
from .repograph import graph_root
from .repoindex import VERSIONS_COLLECTION
from .repositories import COLLECTION as REPOSITORIES

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# The cold p95 per promoted version (#1840)
# --------------------------------------------------------------------------

#: The file the indexer writes beside the manifest, and its schema
#: (images/agent-runtime-indexer/repo-index/repo_graph_shards.py, `LATENCY_SCHEMA`).
LATENCY_FILE = "latency.json"
LATENCY_SCHEMA = "swarm.repo-graph-latency/v1"
#: A latency.json is a few KB; past this it is not one.
MAX_LATENCY_BYTES = 256 * 1024
#: The design target (KG4): answer p95 under 300 ms on the repository itself.
TARGET_MS = 300.0
#: §1.3 step 4: no measurement for this long, after one, is itself reported.
UNMEASURED_AFTER = timedelta(days=3)
SLOWEST_SHOWN = 5

OPEN_ISSUE = "open_issue"
COMMENT_OVER = "comment_over"
COMMENT_UNDER = "comment_under"
OPEN_UNMEASURED = "open_unmeasured"
COMMENT_UNMEASURED = "comment_unmeasured"
NOTHING = "nothing"


def latency_key(tenant_id: str, repo_id: str, commit_sha: str) -> str:
    return f"{graph_root(tenant_id, repo_id)}/{commit_sha}/{LATENCY_FILE}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return _time(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


@dataclass
class Measurement:
    """One promoted version's measurement: its median run (by p95)."""

    commit_sha: str
    recorded_at: datetime | None
    p50_ms: float | None
    p95_ms: float
    reference_ms: float
    symbols: int | None = None
    cpu_limit: float | None = None
    slowest: list[dict[str, Any]] = field(default_factory=list)
    source: str = LATENCY_FILE

    @property
    def ratio(self) -> float:
        return self.p95_ms / self.reference_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "p50_ms": self.p50_ms, "p95_ms": self.p95_ms, "reference_ms": self.reference_ms,
            "ratio": round(self.ratio, 2), "symbols": self.symbols, "cpu_limit": self.cpu_limit,
            "slowest": self.slowest, "source": self.source,
        }


def median_run(document: Any) -> dict[str, Any] | None:
    """The run whose p95 is the median of the document's runs, or None when
    the document is not a measurement (a missing reference cannot make a ratio)."""
    if not isinstance(document, Mapping):
        return None
    runs = []
    for run in document.get("runs") or []:
        if not isinstance(run, Mapping):
            continue
        p95, reference = _number(run.get("p95_ms")), _number(run.get("reference_ms"))
        if p95 is not None and reference:
            runs.append(run)
    if not runs:
        return None
    runs.sort(key=lambda run: float(run["p95_ms"]))
    return dict(runs[(len(runs) - 1) // 2])


def _slowest(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in run.get("slowest") or []:
        if isinstance(row, Mapping) and _number(row.get("ms")) is not None:
            out.append({"tool": str(row.get("tool") or "")[:40],
                        "symbol": str(row.get("symbol") or "")[:200],
                        "ms": float(row["ms"])})
    return out[:SLOWEST_SHOWN]


def measurement_of(commit_sha: str, recorded_at: datetime | None, document: Any, *,
                   source: str) -> Measurement | None:
    """A version's `Measurement` from latency.json's shape (or promotion's copy of it)."""
    if isinstance(document, Mapping) and document.get("state") == "unmeasured":
        return None
    run = median_run(document)
    if run is None:
        return None
    symbols = document.get("symbols") if isinstance(document, Mapping) else None
    return Measurement(
        commit_sha=commit_sha, recorded_at=recorded_at,
        p50_ms=_number(run.get("p50_ms")), p95_ms=float(run["p95_ms"]),
        reference_ms=float(run["reference_ms"]),
        symbols=symbols if isinstance(symbols, int) and not isinstance(symbols, bool) else None,
        cpu_limit=_number(document.get("cpu_limit")), slowest=_slowest(run), source=source,
    )


def _read_latency(reader: Any, key: str) -> tuple[Any, str | None]:
    """(the parsed latency.json, None) or (None, why it was not read)."""
    try:
        window = reader.read_range(key, offset=0, length=MAX_LATENCY_BYTES + 1)
    except ObjectAbsent:
        return None, "not_measured"
    except ObjectUnreadable:
        # Never the store's own words: a GCS error can quote a signed URL.
        return None, "unreadable"
    if window.total_bytes > MAX_LATENCY_BYTES:
        return None, "too_large"
    try:
        document = json.loads(window.data)
    except ValueError:
        return None, "malformed"
    if not isinstance(document, Mapping) or document.get("schema") != LATENCY_SCHEMA:
        return None, "malformed"
    return document, None


@dataclass
class Readings:
    """The window of one repository: newest version first."""

    versions: list[dict[str, Any]]
    measured: list[Measurement]
    unmeasured: list[dict[str, Any]]

    @property
    def newest(self) -> Measurement | None:
        return self.measured[0] if self.measured else None

    def as_dict(self) -> dict[str, Any]:
        return {"versions": len(self.versions),
                "measured": [m.as_dict() for m in self.measured],
                "unmeasured": self.unmeasured}


def measurements(db: Any, reader: Callable[[], Any] | None, tenant_id: str, repo_id: str,
                 window: int) -> Readings:
    """The newest `window` promoted versions of a registration the caller has
    already found in `tenant_id`, and the measurement of each that has one."""
    rows = [{"commit_sha": snap.id, **(snap.to_dict() or {})}
            for snap in db.collection(REPOSITORIES).document(repo_id)
            .collection(VERSIONS_COLLECTION).stream()]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda row: (_time(row.get("recorded_at")) or epoch, row["commit_sha"]),
              reverse=True)
    rows = rows[:window]
    measured: list[Measurement] = []
    unmeasured: list[dict[str, Any]] = []
    opened: Any = None
    for row in rows:
        sha = str(row["commit_sha"])
        at = _time(row.get("recorded_at"))
        recorded = row.get("answer_latency")
        found = measurement_of(sha, at, recorded, source="index_version") \
            if isinstance(recorded, Mapping) else None
        cause = None
        if found is None:
            if opened is None and reader is not None:
                try:
                    opened = reader()
                except ApiError:
                    opened = False
            if not opened:
                cause = "no_artifact_store"
            else:
                document, cause = _read_latency(opened, latency_key(tenant_id, repo_id, sha))
                if document is not None:
                    found = measurement_of(sha, at, document, source=LATENCY_FILE)
                    cause = None if found is not None else "malformed"
        if found is not None:
            measured.append(found)
        else:
            unmeasured.append({"commit_sha": sha, "cause": cause or "not_measured",
                               "recorded_at": at.isoformat() if at else None})
    return Readings(versions=rows, measured=measured, unmeasured=unmeasured)


# Markers: how the next firing finds what this one wrote (release-health's pattern).

def issue_marker(repo_id: str) -> str:
    return f"<!-- swarm:graph-latency:{repo_id} -->"


def over_marker(sha: str) -> str:
    return f"<!-- swarm:graph-latency over={sha} -->"


def under_marker(sha: str) -> str:
    return f"<!-- swarm:graph-latency under={sha} -->"


def unmeasured_marker(sha: str) -> str:
    """Keyed on the LAST measured version: a silence is reported once, however
    many firings it lasts, and a new silence after a new measurement again."""
    return f"<!-- swarm:graph-latency unmeasured-after={sha} -->"


def decide(readings: Readings, threshold: float, now: datetime, *,
           issue_open: bool, said: Callable[[str], bool]) -> tuple[str, str]:
    """(action, reason). `said(marker)` answers whether the open issue already
    carries the marker; it is only asked when an issue is open."""
    newest = readings.newest
    if newest is None:
        return NOTHING, "no promoted version in the window carries a measurement"
    # Silent only when versions were promoted since and none was measured: an
    # idle repository that was not indexed again has nothing new to measure.
    last = newest.recorded_at
    promoted_since = last is not None and any(
        (_time(row.get("recorded_at")) or last) > last for row in readings.versions)
    if last is not None and promoted_since and now - last >= UNMEASURED_AFTER:
        marker = unmeasured_marker(newest.commit_sha)
        if issue_open and said(marker):
            return NOTHING, "the silence since the last measurement is already reported"
        return (COMMENT_UNMEASURED if issue_open else OPEN_UNMEASURED,
                f"no measurement since {newest.recorded_at.date().isoformat()}")
    if newest.ratio >= threshold:
        if not issue_open:
            return OPEN_ISSUE, f"p95 is {newest.ratio:.1f}x the reference, at or over {threshold:g}x"
        if said(over_marker(newest.commit_sha)):
            return NOTHING, "the open issue already names this version"
        return COMMENT_OVER, f"p95 is {newest.ratio:.1f}x the reference at a new version"
    previous = readings.measured[1] if len(readings.measured) > 1 else None
    if issue_open and previous is not None and previous.ratio >= threshold:
        if said(under_marker(newest.commit_sha)):
            return NOTHING, "the open issue already says it is under again"
        return COMMENT_UNDER, f"p95 is {newest.ratio:.1f}x the reference, under {threshold:g}x again"
    return NOTHING, f"p95 is {newest.ratio:.1f}x the reference, under {threshold:g}x"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} ms"


def table(readings: Readings) -> str:
    lines = ["| version | recorded | symbols | p95 | reference | ratio |",
             "|---|---|---|---|---|---|"]
    for m in readings.measured:
        lines.append(f"| `{m.commit_sha[:12]}` | "
                     f"{m.recorded_at.date().isoformat() if m.recorded_at else '-'} | "
                     f"{m.symbols if m.symbols is not None else '-'} | {_ms(m.p95_ms)} | "
                     f"{m.reference_ms:.1f} ms | {m.ratio:.1f}x |")
    if readings.unmeasured:
        lines.append("")
        lines.append(f"{len(readings.unmeasured)} of the window's {len(readings.versions)} "
                     "versions carry no measurement.")
    return "\n".join(lines)


def slowest_lines(m: Measurement) -> str:
    if not m.slowest:
        return "The run named no slowest answers."
    return "\n".join(f"* `{row['tool']}` on `{row['symbol']}`: {row['ms']:.0f} ms"
                     for row in m.slowest)


def title(repository: str, m: Measurement) -> str:
    return (f"swarm-graph's cold answer p95 on {repository} reached {m.ratio:.1f}x the "
            f"reference workload ({m.p95_ms:.0f} ms) at {m.commit_sha[:12]}")[:200]


def unmeasured_title(repository: str, m: Measurement) -> str:
    since = m.recorded_at.date().isoformat() if m.recorded_at else "an unknown date"
    return f"swarm-graph's answer latency on {repository} has not been measured since {since}"[:200]


def issue_body(repo_id: str, repository: str, readings: Readings, threshold: float,
               action: str, schedule_id: str) -> str:
    m = readings.newest
    assert m is not None
    if action == OPEN_UNMEASURED:
        head = [unmeasured_marker(m.commit_sha), "",
                f"The newest measured version of `{repository}` is `{m.commit_sha[:12]}`; "
                f"no index since has carried a measurement for "
                f"{UNMEASURED_AFTER.days} days or more. A report that has stopped measuring "
                "is the failure this report exists to notice."]
    else:
        head = [over_marker(m.commit_sha), "",
                f"The cold answer p95 of `{repository}` at `{m.commit_sha[:12]}` is "
                f"{m.p95_ms:.0f} ms, {m.ratio:.1f}x the reference workload timed in the same "
                f"run, at or over the schedule's `ratio_threshold` of {threshold:g}x. "
                f"The design target is {TARGET_MS:.0f} ms (graph-answer-cache.md §1.1)."]
    return "\n".join([
        issue_marker(repo_id), *head, "", table(readings), "",
        "Slowest answers of the newest measured run:", "", slowest_lines(m), "",
        f"Written by schedule `{schedule_id}` (`graph-latency-report`). It comments when a "
        "new version is measured over the ratio and once when it is under again; it never "
        "closes this issue, a person does.",
    ])


def comment_body(readings: Readings, threshold: float, action: str) -> str:
    m = readings.newest
    assert m is not None
    if action == COMMENT_UNMEASURED:
        return "\n".join([unmeasured_marker(m.commit_sha), "",
                          f"No measurement since `{m.commit_sha[:12]}` "
                          f"({m.recorded_at.date().isoformat() if m.recorded_at else '-'})."])
    if action == COMMENT_UNDER:
        return "\n".join([under_marker(m.commit_sha), "",
                          f"Under again at `{m.commit_sha[:12]}`: p95 {m.p95_ms:.0f} ms, "
                          f"{m.ratio:.1f}x the reference, under {threshold:g}x. This issue stays "
                          "open until a person closes it."])
    return "\n".join([over_marker(m.commit_sha), "",
                      f"Still at or over {threshold:g}x at `{m.commit_sha[:12]}`: p95 "
                      f"{m.p95_ms:.0f} ms, {m.ratio:.1f}x the reference.", "",
                      table(readings), "", slowest_lines(m)])


# --------------------------------------------------------------------------
# Seven days of graph delivery (#4030)
# --------------------------------------------------------------------------

#: graphstage.READY_MARK, the cause of the RUNNING start-up mark. Restated,
#: not imported: swarm-api does not import the worker.
READY_MARK = "graph_ready"
RUNNING = "running"
DELIVERIES = ("service", "staged", "none")
#: What a staged attempt's reason says after it when the service was asked for
#: and not used (graphstage.stage: "; the graph service was not used: {why}").
FALLBACK_SAID = "; the graph service was not used: "
REPORT_DAYS = 7
#: Bounds of one report: attempts read and tasks whose events are read.
MAX_ATTEMPTS = 5000
MAX_TASKS = 2000
PAGE_SIZE = 500
REASON_CHARS = 160
#: Rows of one report, by fallbacks then attempts: the rest summed as one.
MAX_REPOSITORIES = 100
NO_REPOSITORY = "(no repository)"


def fallback_reason(detail: Mapping[str, Any]) -> str | None:
    """The fallback reason of one `graph_ready` mark's `graph`, or None."""
    if detail.get("delivery") != "staged":
        return None
    reason = detail.get("reason")
    if not isinstance(reason, str) or FALLBACK_SAID not in reason:
        return None
    why = reason.split(FALLBACK_SAID, 1)[1].strip()
    return why[:REASON_CHARS] or "unspecified"


def repository_of(task: Any) -> str:
    url = (getattr(task, "repository_url", None) or "") if task is not None else ""
    path = url.split("://", 1)[-1].split("/", 1)[-1].strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.lower() or NO_REPOSITORY


def _row() -> dict[str, Any]:
    return {"attempts": 0, "delivery": {d: 0 for d in DELIVERIES}, "fallbacks": 0,
            "fallback_reasons": {}}


def _add(row: dict[str, Any], delivery: str, reason: str | None) -> None:
    row["attempts"] += 1
    row["delivery"][delivery if delivery in DELIVERIES else "none"] += 1
    if reason is not None:
        row["fallbacks"] += 1
        row["fallback_reasons"][reason] = row["fallback_reasons"].get(reason, 0) + 1


def _close(row: dict[str, Any]) -> dict[str, Any]:
    wanted = row["delivery"]["service"] + row["fallbacks"]
    row["fallback_rate"] = round(row["fallbacks"] / wanted, 4) if wanted else None
    row["fallback_reasons"] = dict(sorted(row["fallback_reasons"].items(),
                                          key=lambda kv: (-kv[1], kv[0])))
    return row


def _marks(db: Any, task_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    query = (db.collection("tasks").document(task_id).collection("events")
             .where(filter=FieldFilter("type", "==", RUNNING)))
    out = []
    for snap in query.stream():
        event = snap.to_dict() or {}
        detail = event.get("detail")
        at = _time(event.get("at"))
        if not isinstance(detail, Mapping) or detail.get("cause") != READY_MARK:
            continue
        if at is None or not since <= at < until or not isinstance(detail.get("graph"), Mapping):
            continue
        out.append(event)
    return out


@dataclass
class AttemptsRead:
    """A tenant's attempts of a window and their tasks, read once and reused
    by every repository of one firing."""

    attempts: list[Any]
    tasks: Mapping[str, Any]
    truncated: bool


def read_attempts(store: Any, tenant_id: str, since: datetime, until: datetime) -> AttemptsRead:
    attempts: list[Any] = []
    token: str | None = None
    truncated = False
    while True:
        page = store.page_attempts(tenant_id, limit=min(PAGE_SIZE, MAX_ATTEMPTS - len(attempts)),
                                   page_token=token, since=since, until=until)
        # Filtered again here: an index mistake must not move the tenant boundary.
        attempts.extend(a for a in page.items if a.tenant_id == tenant_id)
        token = page.next_page_token
        if not token:
            break
        if len(attempts) >= MAX_ATTEMPTS:
            truncated = True
            break
    tasks = store.tasks_by_id(tenant_id, {a.task_id for a in attempts})
    return AttemptsRead(attempts=attempts, tasks=tasks, truncated=truncated)


def delivery_report(store: Any, db: Any, tenant_id: str, since: datetime, until: datetime,
                    *, repository: str | None = None,
                    read: AttemptsRead | None = None) -> dict[str, Any]:
    """`tenant_id`'s attempts created in [since, until) counted by how the graph
    reached them, and the fallbacks by reason, per repository (`owner/repo`).

    One mark per attempt (the worker writes it once; a second is not counted).
    `repository` narrows to one; `read` is `read_attempts`' answer for the same
    tenant and window when the caller already has it. An attempt with no mark
    (it ended before the graph step, or ran before the mark existed) is counted
    in `unmarked`.
    """
    read = read if read is not None else read_attempts(store, tenant_id, since, until)
    attempts, tasks, truncated = read.attempts, read.tasks, read.truncated
    wanted = repository.lower() if repository else None
    by_task: dict[str, list[Any]] = {}
    for attempt in attempts:
        task = tasks.get(attempt.task_id)
        if task is None or (wanted is not None and repository_of(task) != wanted):
            continue
        by_task.setdefault(attempt.task_id, []).append(attempt)
    task_ids = sorted(by_task)
    if len(task_ids) > MAX_TASKS:
        task_ids, truncated = task_ids[:MAX_TASKS], True
    rows: dict[str, dict[str, Any]] = {}
    total, unmarked = _row(), 0
    for task_id in task_ids:
        ids = {a.attempt_id for a in by_task[task_id]}
        seen: set[str] = set()
        for event in sorted(_marks(db, task_id, since, until), key=lambda e: str(e.get("at"))):
            attempt_id = event.get("attempt_id")
            if attempt_id not in ids or attempt_id in seen:
                continue
            seen.add(attempt_id)
            graph = event["detail"]["graph"]
            delivery = str(graph.get("delivery") or "none")
            reason = fallback_reason(graph)
            name = repository_of(tasks[task_id])
            _add(rows.setdefault(name, _row()), delivery, reason)
            _add(total, delivery, reason)
        unmarked += len(ids - seen)
    ranked = sorted(rows.items(), key=lambda kv: (-kv[1]["fallbacks"], -kv[1]["attempts"], kv[0]))
    shown = [{"repository": name, **_close(row)} for name, row in ranked[:MAX_REPOSITORIES]]
    return {
        "tenant_id": tenant_id, "since": since.isoformat(), "until": until.isoformat(),
        "attempts_read": sum(len(v) for v in by_task.values()), "unmarked": unmarked,
        "truncated": truncated, "total": _close(total), "repositories": shown,
        "repositories_not_shown": max(0, len(ranked) - MAX_REPOSITORIES),
    }


def served_latency(facts_latency: Any) -> dict[str, Any]:
    """The replicas' merged served answer latency (graph_cache's
    `facts.answer_latency`) held against `TARGET_MS`; not read stays not read."""
    if not isinstance(facts_latency, Mapping) or facts_latency.get("state") != "read":
        reason = facts_latency.get("reason") if isinstance(facts_latency, Mapping) else None
        return {"state": "not_read", "reason": reason or "not_served", "target_ms": TARGET_MS}
    p50, p95 = _number(facts_latency.get("p50_ms")), _number(facts_latency.get("p95_ms"))
    return {
        "state": "read", "count": facts_latency.get("count"),
        "window_hours": facts_latency.get("window_hours"),
        "p50_ms": p50, "p95_ms": p95, "exact": facts_latency.get("exact"),
        "target_ms": TARGET_MS,
        "p95_over_target": None if p95 is None else p95 >= TARGET_MS,
    }


def read_served_latency(ctx: Any, tenant_id: str, repo_id: str, reader: Any) -> dict[str, Any]:
    """`served_latency` of one registered repository, through the Cache
    section's one stats call (routes/graph_cache.py). Never raises for
    swarm-graph not answering: that is `not_read`, as it is there."""
    from .routes.graph_cache import graph_cache  # the route module imports this one

    body = graph_cache(ctx, tenant_id, repo_id, shards=False, page=1, reader=reader)
    return served_latency((body.get("facts") or {}).get("answer_latency"))


def window_until(now: datetime, days: int = REPORT_DAYS) -> tuple[datetime, datetime]:
    return now - timedelta(days=days), now


__all__: Sequence[str] = (
    "COMMENT_OVER", "COMMENT_UNDER", "COMMENT_UNMEASURED", "FALLBACK_SAID", "LATENCY_FILE",
    "LATENCY_SCHEMA", "Measurement", "NOTHING", "OPEN_ISSUE", "OPEN_UNMEASURED", "READY_MARK",
    "AttemptsRead", "Readings", "TARGET_MS", "comment_body", "decide", "delivery_report", "fallback_reason",
    "issue_body", "issue_marker", "latency_key", "measurement_of", "measurements", "median_run",
    "over_marker", "read_attempts", "read_served_latency", "served_latency", "title", "under_marker",
    "unmeasured_marker", "unmeasured_title", "window_until",
)
