#!/usr/bin/env python3
"""The platform's scorecard: what agent work costs and how fast it lands, per week.

Owner-approved improvement plan, 2026-11-23: every later change (cheaper
model classes for reviews, a stricter pre-publish gate, a shorter merge-train
wait) is judged on measured numbers, and this is what measures them.
`scripts/scorecard.sh` does the reading (Firestore, the API, gh) into files;
everything that decides a number lives here and reads those files, so it is
unit-tested with no credentials, no emulator and no network
(tests/unit/scripts/test_scorecard.py). Standard library only: it runs with
the system interpreter or uv. docs/scorecard.md explains each measure.

SUBCOMMANDS (the shell calls each; nothing here touches the network):

* `window`     the window and its weekly buckets, as `since until` (UTC ISO);
* `fs-query`   the body of one Firestore `runQuery` page on `tasks`: created_at
               in the window, a field mask (`FIELD_MASK`), ordered by
               created_at then document name, after the cursor file's values;
* `fs-decode`  one `runQuery` answer appended to the records file as plain JSON
               lines; prints `rows N` and rewrites the cursor file (or removes
               it when the page was the last);
* `reviews`    the review and re-review tasks whose verdict.json to read, at
               most `--max`, as `task_id tenant_id` lines (the tenant id is
               only ever an `X-Swarm-Tenant` header, never printed by the shell);
* `report`     the table (or `--json`).

THE RULES, each the failure it prevents:

* NOT READ IS NOT ZERO. Every measure is a value or `not read: <why>`. A
  source that failed, was switched off or had nothing to measure says so; a
  0 would be read as a measurement nobody made (CLAUDE.md, rule zero).
* NO WORKSPACE NAME IS PRINTED. Tenants are counted only as `main workspace`,
  `acceptance` or `personal` (`workspace_class`); no tenant id, workflow id,
  model-free text or comment body reaches the output.
* ONE ROLE FUNCTION. A task's role (build, review, fix, ...) is
  `role_for_step(step_id)` and nothing else.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: The task fields read from Firestore, and nothing else: a field mask keeps
#: the read small and keeps prompts, inputs and logs out of the extract.
FIELD_MASK = (
    "state",
    "runner_profile",
    "model",
    "model_class",
    "tenant_id",
    "workflow_id",
    "step_id",
    "created_at",
    "started_at",
    "completed_at",
    "attempt_count",
    "end_cause",
    "attempt_totals.cost_usd_total",
    "result_summary.runner.usage",
    "result_summary.runner.output.prepublish",
    "result_summary.duration_seconds",
    "result_summary.git.publish_reason",
    "result_summary.git.pull_request.number",
)

#: Rows per Firestore page. 300 masked task documents is well under the
#: 10 MiB answer limit and keeps the number of round trips small.
PAGE_SIZE = 300

#: Runner profiles that run no agent (apps/common/swarm_common/profiles.py
#: RUNNER_PROFILES): the merge step, the verdict poster, the indexer, a wait
#: for a person, the fix-loop driver and the mock runner. Their tasks are
#: counted for the merge-step measure, never as agent tasks.
NON_AGENT_PROFILES = frozenset({"merge", "post-verdict", "indexer", "await-input", "fix-loop", "mock"})
MERGE_PROFILE = "merge"
#: swarm_common.models.EndCause.MERGE_REFUSED: a condition for merging was not met.
MERGE_REFUSED = "merge_refused"

TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
SUCCEEDED = "SUCCEEDED"

#: The roles cost is shared across, in the order the table prints them.
ROLES = ("build", "review", "fix", "re-review", "ci-fix", "conflict-fix", "planner", "other")

#: A task this many turns long or longer is "long": the share of tasks past
#: it, and their share of cost, is what a turn cap would act on.
LONG_TURNS = 100

#: The acceptance suite's tenant when none is named (scripts/acceptance/config.sh ACC_TENANT).
DEFAULT_ACCEPTANCE_TENANTS = ("smoke",)
#: swarm_common.identity.tenant_id_for_user: a person's own tenant is `u-<slug>`.
PERSONAL_PREFIX = "u-"
WORKSPACE_CLASSES = ("main workspace", "acceptance", "personal")

#: The merge train's ejection comment, both writers (swarm_api.mergetrain and
#: scripts/merge-train.sh) start their bold first line with this.
EJECTED_MARKER = "Ejected from the merge train"
#: merge-train.sh lists the failing checks as `- [name](url)`.
_FAILED_CHECK = re.compile(r"^\s*[-*]\s+\[([^\]]{1,120})\]\(", re.M)
#: swarm_api.mergetrain names its reason in parentheses after the bold line:
#: `**Ejected from the merge train of `main`** (reason): detail`.
_EJECT_REASON = re.compile(r"Ejected from the merge train[^*]*\*\*\s*\(([a-z0-9_ -]{1,60})\)", re.I)

#: The verdicts a review file can carry (swarm_api.validation.REVIEW_VERDICTS
#: and reviewgate.SKIPPED_VERDICT), read as verdictpublish.parse_verdict reads them.
REVIEW_VERDICTS = ("MERGE", "NOT_YET")
SKIPPED_VERDICT = "SKIPPED"
VERDICT_FILE = "verdict.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class NotRead:
    """A value that could not be read, and why. Printed as `not read: <why>`."""

    def __init__(self, why: str) -> None:
        self.why = why

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NotRead({self.why!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, NotRead) and other.why == self.why


def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 timestamp (Firestore's, GitHub's) as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Firestore gives nanoseconds; fromisoformat takes at most microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def number(value: Any) -> float | None:
    """A JSON number as a float; booleans and anything else are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def dig(record: Mapping[str, Any], *path: str) -> Any:
    cur: Any = record
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (scripts/benchstat.py's rule): always a value that happened."""
    ordered = sorted(values)
    rank = math.ceil(q / 100.0 * len(ordered))
    return ordered[max(1, min(rank, len(ordered))) - 1]


def median_p90(values: Sequence[float], what: str) -> Any:
    if not values:
        return NotRead(f"no {what} in the week")
    return {"median": percentile(values, 50), "p90": percentile(values, 90), "n": len(values)}


def share(part: float, whole: float, what: str) -> Any:
    if whole <= 0:
        return NotRead(f"no {what} in the week")
    return round(100.0 * part / whole, 1)


# ---------------------------------------------------------------------------
# The documented classifications
# ---------------------------------------------------------------------------


def role_for_step(step_id: Any) -> str:
    """The role of a workflow step, from its step_id. The ONE place a role is decided.

    Step ids are lower-cased and split on anything that is not a letter or a
    digit, so a lane prefix (`lane-12-review`) or a round suffix
    (`re-review:2`, `review-fix:1`, `ci-fix:1`) does not change the role.
    Checked in this order, first match wins:

      conflict-fix  `conflict` then `fix` (swarm_api.mergewake.CONFLICT_FIX_STEP)
      ci-fix        `ci` then `fix` (swarm_api.cifix's rounds)
      re-review     `re-review` / `rereview` (swarm_api.fixloop.RE_REVIEW_STEP)
      fix           any `fix` part (`review-fix`, `fix`)
      review        any `review` part (`review`, `security-review`)
      planner       `plan` or `planner`
      build         `build`, `implement`, `impl`
      other         anything else, and a task with no step_id
    """
    if not isinstance(step_id, str) or not step_id.strip():
        return "other"
    parts = [p for p in re.split(r"[^a-z0-9]+", step_id.lower()) if p]
    pairs = list(zip(parts, parts[1:]))
    if ("conflict", "fix") in pairs:
        return "conflict-fix"
    if ("ci", "fix") in pairs:
        return "ci-fix"
    if ("re", "review") in pairs or "rereview" in parts:
        return "re-review"
    if "fix" in parts:
        return "fix"
    if "review" in parts:
        return "review"
    if "plan" in parts or "planner" in parts:
        return "planner"
    if {"build", "implement", "impl"} & set(parts):
        return "build"
    return "other"


def workspace_class(tenant_id: Any, acceptance: Iterable[str]) -> str:
    """`acceptance` for the acceptance suite's tenants, `personal` for a
    person's own tenant (`u-...`), `main workspace` for every group tenant.
    The tenant id itself is never printed."""
    text = tenant_id if isinstance(tenant_id, str) else ""
    if text in set(acceptance):
        return "acceptance"
    if text.startswith(PERSONAL_PREFIX):
        return "personal"
    return "main workspace"


def is_agent_task(record: Mapping[str, Any]) -> bool:
    return str(record.get("runner_profile") or "") not in NON_AGENT_PROFILES


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def week_buckets(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """Seven-day buckets ending at `until`, oldest first; the oldest may be short."""
    out: list[tuple[datetime, datetime]] = []
    end = until
    while end > since:
        start = max(since, end - timedelta(days=7))
        out.append((start, end))
        end = start
    return list(reversed(out))


def resolve_window(since: str | None, until: str | None, weeks: int | None,
                   now: datetime | None = None) -> tuple[datetime, datetime]:
    end = parse_time(until) if until else (now or datetime.now(timezone.utc))
    if end is None:
        raise ValueError(f"--until is not a timestamp: {until!r}")
    if since:
        start = parse_time(since)
        if start is None:
            raise ValueError(f"--since is not a timestamp: {since!r}")
    else:
        start = end - timedelta(days=7 * (weeks or 1))
    if start >= end:
        raise ValueError("--since must be before --until")
    return start, end


def bucket_of(moment: datetime | None, buckets: Sequence[tuple[datetime, datetime]]) -> int | None:
    if moment is None:
        return None
    for i, (start, end) in enumerate(buckets):
        if start <= moment < end:
            return i
    return None


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------


def fs_query_body(since: datetime, until: datetime, cursor: Mapping[str, Any] | None,
                  page_size: int = PAGE_SIZE) -> dict[str, Any]:
    def bound(op: str, moment: datetime) -> dict[str, Any]:
        return {"fieldFilter": {"field": {"fieldPath": "created_at"}, "op": op,
                                "value": {"timestampValue": iso(moment)}}}

    query: dict[str, Any] = {
        "from": [{"collectionId": "tasks"}],
        "select": {"fields": [{"fieldPath": f} for f in FIELD_MASK]},
        "where": {"compositeFilter": {"op": "AND", "filters": [
            bound("GREATER_THAN_OR_EQUAL", since), bound("LESS_THAN", until)]}},
        # created_at then the document name: a cursor on created_at alone
        # would skip a task sharing its timestamp with the page's last.
        "orderBy": [{"field": {"fieldPath": "created_at"}, "direction": "ASCENDING"},
                    {"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
        "limit": page_size,
    }
    if cursor:
        query["startAt"] = {"before": False, "values": [
            cursor["created_at"], {"referenceValue": cursor["name"]}]}
    return {"structuredQuery": query}


def fs_value(value: Any) -> Any:
    """Firestore's typed REST value as plain JSON (common.sh FS_JQ's `fv`)."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "timestampValue", "booleanValue", "doubleValue", "referenceValue"):
        if key in value:
            return value[key]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        return [fs_value(v) for v in value["arrayValue"].get("values") or []]
    if "mapValue" in value:
        return {k: fs_value(v) for k, v in (value["mapValue"].get("fields") or {}).items()}
    return None


def fs_decode(answer: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """A runQuery answer as records, and the cursor after its last document."""
    rows = answer if isinstance(answer, list) else []
    records: list[dict[str, Any]] = []
    cursor: dict[str, Any] | None = None
    for row in rows:
        doc = row.get("document") if isinstance(row, dict) else None
        if not isinstance(doc, dict):
            continue
        fields = doc.get("fields") or {}
        record = {"id": str(doc.get("name", "")).rsplit("/", 1)[-1]}
        record.update({k: fs_value(v) for k, v in fields.items()})
        records.append(record)
        cursor = {"created_at": fields.get("created_at"), "name": doc.get("name")}
    return records, cursor


# ---------------------------------------------------------------------------
# Inputs read from files
# ---------------------------------------------------------------------------


def read_records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            out.append(item)
    return out


def read_verdict_text(text: str) -> str | None:
    """MERGE, NOT_YET or SKIPPED from a verdict file's text, or None when the
    platform would refuse it (swarm_api.verdictpublish.parse_verdict's rules)."""
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("verdict"), str):
        return None
    verdict = document["verdict"].strip().upper()
    if verdict == SKIPPED_VERDICT:
        reason = document.get("skipped")
        return SKIPPED_VERDICT if isinstance(reason, str) and reason.strip() else None
    return verdict if verdict in REVIEW_VERDICTS else None


def read_verdicts(directory: Path | None) -> tuple[dict[str, str], Any]:
    """`<task>.json` (the API's `/artifacts/content` answer) per sampled task,
    and `meta.json` ({candidates, sampled, cap}). Returns {task: verdict} with
    `unreadable` / `missing` for a file that names no verdict, and the meta."""
    if directory is None:
        return {}, NotRead("verdicts were not read (--no-verdicts)")
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        return {}, NotRead("no verdict sample was taken")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for path in sorted(directory.glob("task_*.json")):
        task = path.stem
        if path.stat().st_size == 0:
            out[task] = "missing"
            continue
        try:
            answer = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            out[task] = "unreadable"
            continue
        content = answer.get("content") if isinstance(answer, dict) else None
        verdict = read_verdict_text(content) if isinstance(content, str) else None
        out[task] = verdict or "unreadable"
    return out, meta


def read_github(directory: Path | None) -> dict[str, Any]:
    """The gh pages the shell saved: `<source>-<n>.json` and `status.tsv`
    (`source<TAB>ok` or `source<TAB><why>`), plus `calls` (the count made)."""
    sources = ("comments", "merged", "release", "accept", "prci")
    if directory is None:
        return {s: NotRead("GitHub was not read (--no-github)") for s in sources} | {"calls": 0}
    status: dict[str, str] = {}
    status_path = directory / "status.tsv"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                key, value = line.split("\t", 1)
                status[key] = value
    out: dict[str, Any] = {}
    for source in sources:
        state = status.get(source)
        if state != "ok":
            out[source] = NotRead(state or "not fetched")
            continue
        items: list[Any] = []
        for path in sorted(directory.glob(f"{source}-*.json"),
                           key=lambda p: int(p.stem.rsplit("-", 1)[-1])):
            page = json.loads(path.read_text(encoding="utf-8") or "null")
            if isinstance(page, list):
                items.extend(page)
            elif isinstance(page, dict):
                items.extend(page.get("items") or page.get("workflow_runs") or [])
        out[source] = items
    calls_path = directory / "calls"
    out["calls"] = int(calls_path.read_text().strip() or 0) if calls_path.is_file() else 0
    return out


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def task_cost(record: Mapping[str, Any]) -> float | None:
    """attempt_totals.cost_usd_total (every attempt), else the runner's
    usage.total_cost_usd (the last attempt), else None."""
    total = number(dig(record, "attempt_totals", "cost_usd_total"))
    if total is not None:
        return total
    return number(dig(record, "result_summary", "runner", "usage", "total_cost_usd"))


def usage(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = dig(record, "result_summary", "runner", "usage")
    return value if isinstance(value, Mapping) else {}


def seconds_between(a: Any, b: Any) -> float | None:
    start, end = parse_time(a), parse_time(b)
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def wall_seconds(record: Mapping[str, Any]) -> float | None:
    value = number(dig(record, "result_summary", "duration_seconds"))
    if value is not None:
        return value
    return seconds_between(record.get("started_at"), record.get("completed_at"))


def gate_outcome(record: Mapping[str, Any]) -> tuple[bool, int] | None:
    """(ended at the budget, guards left to CI) for a task whose pre-publish
    gate ran (`result_summary.runner.output.prepublish`), else None.

    At the budget: the gate was skipped or its second turn skipped for budget,
    or a check timed out, was skipped for budget or left guards to CI."""
    gate = dig(record, "result_summary", "runner", "output", "prepublish")
    if not isinstance(gate, Mapping) or not gate:
        return None
    at_budget = gate.get("skipped") == "budget" or gate.get("second_turn_skipped") == "budget"
    left = 0
    for round_ in gate.get("rounds") or []:
        for outcome in round_ if isinstance(round_, list) else [round_]:
            if not isinstance(outcome, Mapping):
                continue
            names = outcome.get("left_to_ci") or []
            left += len(names) if isinstance(names, list) else 0
            if outcome.get("timed_out") or names or (
                    outcome.get("status") == "skipped" and "budget" in str(outcome.get("reason") or "")):
                at_budget = True
    return at_budget, left


def eject_cause(body: str) -> str:
    """The failing checks an ejection comment names, else its reason, else `unnamed`."""
    checks = _FAILED_CHECK.findall(body)
    if checks:
        return ", ".join(sorted({c.strip() for c in checks}))
    reason = _EJECT_REASON.search(body)
    if reason:
        return reason.group(1).strip()
    return "unnamed"


def run_seconds(run: Mapping[str, Any]) -> float | None:
    return seconds_between(run.get("run_started_at") or run.get("created_at"), run.get("updated_at"))


def build_report(records: Sequence[Mapping[str, Any]], buckets: Sequence[tuple[datetime, datetime]],
                 verdicts: Mapping[str, str], verdict_meta: Any, github: Mapping[str, Any],
                 acceptance: Sequence[str], records_source: str) -> dict[str, Any]:
    weeks: list[dict[str, Any]] = [
        {"since": iso(s), "until": iso(e), "label": f"{s:%Y-%m-%d}..{e:%m-%d}", "measures": {}}
        for s, e in buckets]
    by_week: list[list[Mapping[str, Any]]] = [[] for _ in buckets]
    for record in records:
        i = bucket_of(parse_time(record.get("created_at")), buckets)
        if i is not None:
            by_week[i].append(record)

    # Merged pull requests, from GitHub; the workflow links through
    # result_summary.git.pull_request.number.
    merged_at: dict[int, datetime] = {}
    merged_src = github.get("merged")
    if isinstance(merged_src, list):
        for item in merged_src:
            when = parse_time(dig(item, "pull_request", "merged_at") or item.get("closed_at"))
            if isinstance(item.get("number"), int) and when is not None:
                merged_at[item["number"]] = when
    workflows = workflow_rows(records, merged_at)

    for i, week in enumerate(weeks):
        m = week["measures"]
        tasks = [r for r in by_week[i] if is_agent_task(r)]
        merges = [r for r in by_week[i] if str(r.get("runner_profile") or "") == MERGE_PROFILE]
        start, end = buckets[i]
        merged_prs: Any = (sum(1 for t in merged_at.values() if start <= t < end)
                           if isinstance(merged_src, list) else merged_src)
        task_measures(m, tasks, merged_prs)
        m["merge_steps_refused_pct"] = share(
            sum(1 for r in merges if r.get("end_cause") == MERGE_REFUSED),
            sum(1 for r in merges if r.get("state") in TERMINAL_STATES), "ended merge steps")
        workflow_measures(m, [w for w in workflows if start <= w["merged"] < end])
        github_measures(m, github, start, end, merged_prs)
        m["review_verdicts_by_model_class"] = verdict_measure(tasks, verdicts, verdict_meta)
        counts = {c: 0 for c in WORKSPACE_CLASSES}
        for r in tasks:
            counts[workspace_class(r.get("tenant_id"), acceptance)] += 1
        m["agent_tasks_by_workspace"] = counts

    sampled: Any = verdict_meta
    if isinstance(verdict_meta, Mapping):
        sampled = {k: verdict_meta.get(k) for k in ("candidates", "sampled", "cap")}
    return {
        "window": {"since": weeks[0]["since"], "until": weeks[-1]["until"]},
        "sources": {
            "task_records": {"source": records_source, "read": len(records)},
            "verdicts": sampled,
            "github_calls": github.get("calls", 0),
        },
        "weeks": weeks,
    }


def task_measures(m: dict[str, Any], tasks: Sequence[Mapping[str, Any]], merged_prs: Any) -> None:
    m["agent_tasks"] = len(tasks)
    ended = [r for r in tasks if r.get("state") in TERMINAL_STATES]
    m["succeeded_pct"] = share(sum(1 for r in ended if r.get("state") == SUCCEEDED), len(ended),
                               "ended agent tasks")

    costed = [(r, task_cost(r)) for r in tasks]
    costs = [(r, c) for r, c in costed if c is not None]
    total_cost = sum(c for _, c in costs)
    tokens = {"cache_read": "cache_read_input_tokens", "cache_creation": "cache_creation_input_tokens",
              "output": "output_tokens", "input": "input_tokens"}
    with_usage = [usage(r) for r in tasks if usage(r)]
    if costs:
        m["cost_usd"] = round(total_cost, 2)
        m["tasks_without_cost"] = len(tasks) - len(costs)
    else:
        m["cost_usd"] = NotRead("no agent task in the week carries a cost")
    if with_usage:
        m["tokens"] = {k: int(sum(number(u.get(f)) or 0 for u in with_usage)) for k, f in tokens.items()}
    else:
        m["tokens"] = NotRead("no agent task in the week carries runner usage")

    if not isinstance(merged_prs, int):
        why = merged_prs.why if isinstance(merged_prs, NotRead) else "merged pull requests not read"
        m["cost_usd_per_merged_pr"] = NotRead(why)
        m["tokens_per_merged_pr"] = NotRead(why)
    elif merged_prs == 0:
        m["cost_usd_per_merged_pr"] = NotRead("no pull request merged in the week")
        m["tokens_per_merged_pr"] = NotRead("no pull request merged in the week")
    else:
        m["cost_usd_per_merged_pr"] = (round(total_cost / merged_prs, 2) if costs
                                       else NotRead("no agent task in the week carries a cost"))
        tok = m["tokens"]
        m["tokens_per_merged_pr"] = ({k: int(v / merged_prs) for k, v in tok.items() if k != "input"}
                                     if isinstance(tok, dict) else tok)

    if costs and total_cost > 0:
        by_role = {role: 0.0 for role in ROLES}
        by_model: dict[str, float] = {}
        for r, c in costs:
            by_role[role_for_step(r.get("step_id"))] += c
            model = str(r.get("model") or r.get("model_class") or "unrecorded")
            by_model[model] = by_model.get(model, 0.0) + c
        m["cost_share_by_role_pct"] = {k: round(100 * v / total_cost, 1) for k, v in by_role.items()}
        m["cost_share_by_model_pct"] = {k: round(100 * v / total_cost, 1)
                                        for k, v in sorted(by_model.items(), key=lambda kv: -kv[1])}
    else:
        why = "no agent task in the week carries a cost" if not costs else "the week's cost is 0"
        m["cost_share_by_role_pct"] = NotRead(why)
        m["cost_share_by_model_pct"] = NotRead(why)

    turned = [(r, number(usage(r).get("num_turns"))) for r in tasks]
    turned = [(r, t) for r, t in turned if t is not None and t > 0]
    m["turns"] = median_p90([t for _, t in turned], "agent task with a turn count")
    per_turn = []
    for r, t in turned:
        u = usage(r)
        context = sum(number(u.get(f)) or 0 for f in
                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if context > 0:
            per_turn.append(round(context / t))
    m["context_tokens_per_turn"] = median_p90(per_turn, "agent task with tokens and turns")
    long_tasks = [r for r, t in turned if t >= LONG_TURNS]
    m["over_100_turns_pct"] = share(len(long_tasks), len(turned), "agent task with a turn count")
    long_cost = sum(task_cost(r) or 0 for r in long_tasks)
    turned_cost = sum(task_cost(r) or 0 for r, _ in turned)
    m["over_100_turns_cost_pct"] = share(long_cost, turned_cost, "cost on tasks with a turn count")

    model_ms = 0.0
    wall_s = 0.0
    for r in tasks:
        api_ms, wall = number(usage(r).get("duration_api_ms")), wall_seconds(r)
        if api_ms is not None and wall:
            model_ms += api_ms
            wall_s += wall
    if wall_s > 0:
        m["model_time_vs_wall_clock"] = {"model_h": round(model_ms / 3_600_000, 2),
                                         "wall_h": round(wall_s / 3600, 2),
                                         "model_pct": round(100 * (model_ms / 1000) / wall_s, 1)}
    else:
        m["model_time_vs_wall_clock"] = NotRead("no agent task with both model time and wall-clock")

    standalone = [s for r in tasks if not r.get("workflow_id")
                  for s in [seconds_between(r.get("created_at"), r.get("started_at"))] if s is not None]
    steps = [s for r in tasks if r.get("workflow_id")
             for s in [seconds_between(r.get("created_at"), r.get("started_at"))] if s is not None]
    m["queue_wait_s"] = median_p90(standalone, "started task outside a workflow")
    m["step_created_to_started_s"] = median_p90(steps, "started workflow step")

    gates = [g for g in (gate_outcome(r) for r in tasks) if g is not None]
    m["gate_runs"] = len(gates) if gates else NotRead("no task in the week records a pre-publish gate")
    m["gate_at_budget_pct"] = share(sum(1 for b, _ in gates if b), len(gates), "pre-publish gate run")
    m["gate_guards_left_to_ci"] = (sum(n for _, n in gates) if gates
                                   else NotRead("no task in the week records a pre-publish gate"))
    m["unpublished_with_reason"] = sum(1 for r in tasks if dig(r, "result_summary", "git", "publish_reason"))


def workflow_rows(records: Sequence[Mapping[str, Any]], merged_at: Mapping[int, datetime]) -> list[dict[str, Any]]:
    """Per merged workflow: submitted (its first task's created_at), merged
    (its pull request's merged_at from GitHub, else its merge step's
    completed_at when that step SUCCEEDED) and the last review or fix's end."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        if r.get("workflow_id"):
            grouped.setdefault(str(r["workflow_id"]), []).append(r)
    rows = []
    for tasks in grouped.values():
        submitted = min((t for t in (parse_time(r.get("created_at")) for r in tasks) if t), default=None)
        merged = None
        for r in tasks:
            pr = dig(r, "result_summary", "git", "pull_request", "number")
            if isinstance(pr, int) and pr in merged_at:
                merged = max(merged, merged_at[pr]) if merged else merged_at[pr]
        if merged is None:
            done = [parse_time(r.get("completed_at")) for r in tasks
                    if r.get("runner_profile") == MERGE_PROFILE and r.get("state") == SUCCEEDED]
            merged = max((d for d in done if d), default=None)
        if submitted is None or merged is None:
            continue
        last = max((t for t in (parse_time(r.get("completed_at")) for r in tasks
                                if role_for_step(r.get("step_id")) in
                                ("review", "fix", "re-review", "ci-fix", "conflict-fix")) if t and t <= merged),
                   default=None)
        rows.append({"submitted": submitted, "merged": merged, "last_review_or_fix": last})
    return rows


def workflow_measures(m: dict[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    m["workflows_merged"] = len(rows)
    m["workflow_submit_to_merged_h"] = median_p90(
        [round((w["merged"] - w["submitted"]).total_seconds() / 3600, 2) for w in rows],
        "merged workflow")
    m["workflow_last_review_or_fix_to_merged_h"] = median_p90(
        [round((w["merged"] - w["last_review_or_fix"]).total_seconds() / 3600, 2)
         for w in rows if w["last_review_or_fix"]], "merged workflow with a review or fix step")


def _in(item: Mapping[str, Any], key: str, start: datetime, end: datetime) -> bool:
    when = parse_time(item.get(key))
    return when is not None and start <= when < end


def github_measures(m: dict[str, Any], github: Mapping[str, Any], start: datetime, end: datetime,
                    merged_prs: Any) -> None:
    m["merged_prs"] = merged_prs
    comments = github.get("comments")
    if isinstance(comments, list):
        ejections = [c for c in comments if _in(c, "created_at", start, end)
                     and EJECTED_MARKER in str(c.get("body") or "")]
        causes: dict[str, int] = {}
        for c in ejections:
            cause = eject_cause(str(c.get("body") or ""))
            causes[cause] = causes.get(cause, 0) + 1
        m["train_ejections"] = len(ejections)
        m["train_ejections_by_cause"] = dict(sorted(causes.items(), key=lambda kv: -kv[1])) or {}
        if isinstance(merged_prs, int) and merged_prs > 0:
            m["train_ejections_per_merged_pr"] = round(len(ejections) / merged_prs, 2)
        else:
            m["train_ejections_per_merged_pr"] = (merged_prs if isinstance(merged_prs, NotRead)
                                                  else NotRead("no pull request merged in the week"))
    else:
        for key in ("train_ejections", "train_ejections_by_cause", "train_ejections_per_merged_pr"):
            m[key] = comments

    for source, label in (("release", "release"), ("accept", "acceptance")):
        runs = github.get(source)
        if not isinstance(runs, list):
            m[f"{label}_runs"] = runs
            m[f"{label}_success_pct"] = runs
            m[f"{label}_duration_min"] = runs
            continue
        week = [r for r in runs if _in(r, "created_at", start, end) and r.get("status") == "completed"]
        decided = [r for r in week if r.get("conclusion") in ("success", "failure", "timed_out")]
        m[f"{label}_runs"] = len(week)
        m[f"{label}_success_pct"] = share(sum(1 for r in decided if r.get("conclusion") == "success"),
                                          len(decided), f"decided {label} run")
        m[f"{label}_duration_min"] = median_p90(
            [round(s / 60, 1) for s in (run_seconds(r) for r in week) if s is not None],
            f"completed {label} run")

    prci = github.get("prci")
    if isinstance(prci, list):
        week = [r for r in prci if _in(r, "created_at", start, end) and r.get("status") == "completed"]
        m["pr_ci_duration_min"] = median_p90(
            [round(s / 60, 1) for s in (run_seconds(r) for r in week) if s is not None],
            "completed pull-request CI run")
    else:
        m["pr_ci_duration_min"] = prci


def verdict_measure(tasks: Sequence[Mapping[str, Any]], verdicts: Mapping[str, str], meta: Any) -> Any:
    if isinstance(meta, NotRead):
        return meta
    counts: dict[str, dict[str, int]] = {}
    for r in tasks:
        verdict = verdicts.get(str(r.get("id")))
        if verdict is None:
            continue
        cls = str(r.get("model_class") or "unset")
        row = counts.setdefault(cls, {"MERGE": 0, "NOT_YET": 0, "SKIPPED": 0, "unreadable": 0})
        row[verdict if verdict in row else "unreadable"] += 1
    if not counts:
        return NotRead("no sampled review verdict in the week")
    out = {}
    for cls, row in sorted(counts.items()):
        decided = row["MERGE"] + row["NOT_YET"]
        out[cls] = dict(row, not_yet_pct=(round(100 * row["NOT_YET"] / decided, 1) if decided
                                          else NotRead("no MERGE or NOT_YET verdict")))
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: The table's rows, in order: (measure key, label).
ROWS = (
    ("agent_tasks", "agent tasks"),
    ("agent_tasks_by_workspace", "agent tasks by workspace"),
    ("succeeded_pct", "succeeded %"),
    ("cost_usd", "cost USD"),
    ("tokens", "tokens"),
    ("merged_prs", "merged pull requests"),
    ("cost_usd_per_merged_pr", "cost USD per merged PR"),
    ("tokens_per_merged_pr", "tokens per merged PR"),
    ("cost_share_by_role_pct", "cost share by role %"),
    ("cost_share_by_model_pct", "cost share by model %"),
    ("turns", "turns"),
    ("context_tokens_per_turn", "context tokens per turn"),
    ("over_100_turns_pct", "tasks over 100 turns %"),
    ("over_100_turns_cost_pct", "  their share of cost %"),
    ("model_time_vs_wall_clock", "model time vs wall-clock"),
    ("queue_wait_s", "queue wait s (no workflow)"),
    ("step_created_to_started_s", "step created->started s"),
    ("workflows_merged", "workflows merged"),
    ("workflow_submit_to_merged_h", "submit->merged h"),
    ("workflow_last_review_or_fix_to_merged_h", "last review/fix->merged h"),
    ("merge_steps_refused_pct", "merge steps refused %"),
    ("train_ejections", "train ejections"),
    ("train_ejections_per_merged_pr", "ejections per merged PR"),
    ("train_ejections_by_cause", "ejections by cause"),
    ("review_verdicts_by_model_class", "review verdicts by model class"),
    ("release_runs", "release runs"),
    ("release_success_pct", "release success %"),
    ("release_duration_min", "release duration min"),
    ("acceptance_runs", "acceptance runs"),
    ("acceptance_success_pct", "acceptance pass %"),
    ("acceptance_duration_min", "acceptance duration min"),
    ("pr_ci_duration_min", "PR CI duration min"),
    ("gate_runs", "pre-publish gate runs"),
    ("gate_at_budget_pct", "gate ended at budget %"),
    ("gate_guards_left_to_ci", "guards left to CI"),
    ("unpublished_with_reason", "not published (reason recorded)"),
)


def render_value(value: Any) -> str:
    if isinstance(value, NotRead):
        return f"not read: {value.why}"
    if value is None:
        return "not read: no value"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict):
        if set(value) == {"median", "p90", "n"}:
            return f"median {value['median']:g}, p90 {value['p90']:g} (n={value['n']})"
        if not value:
            return "none"
        return "; ".join(f"{k} {render_value(v)}" if not isinstance(v, dict)
                         else f"{k}: " + ", ".join(f"{kk} {render_value(vv)}" for kk, vv in v.items())
                         for k, v in value.items())
    return str(value)


def render_text(report: Mapping[str, Any]) -> str:
    lines = [f"Scorecard {report['window']['since']} .. {report['window']['until']} (read-only)"]
    src = report["sources"]
    lines.append(f"  task records: {src['task_records']['read']} read from {src['task_records']['source']}")
    verdicts = src["verdicts"]
    lines.append("  review verdicts: " + (render_value(verdicts) if isinstance(verdicts, NotRead) else
                 f"{verdicts.get('sampled')} sampled of {verdicts.get('candidates')} (cap {verdicts.get('cap')})"))
    lines.append(f"  GitHub calls made: {src['github_calls']}")
    for week in report["weeks"]:
        lines.append("")
        lines.append(f"== week {week['label']} ==")
        width = max(len(label) for _, label in ROWS)
        for key, label in ROWS:
            lines.append(f"  {label.ljust(width)}  {render_value(week['measures'].get(key, NotRead('not computed')))}")
    return "\n".join(lines) + "\n"


def to_json(value: Any) -> Any:
    if isinstance(value, NotRead):
        return {"not_read": value.why}
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _window_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--weeks", type=int)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("window")
    _window_args(w)
    q = sub.add_parser("fs-query")
    _window_args(q)
    q.add_argument("--cursor", type=Path, required=True)
    d = sub.add_parser("fs-decode")
    d.add_argument("--page", type=Path, required=True)
    d.add_argument("--records", type=Path, required=True)
    d.add_argument("--cursor", type=Path, required=True)
    v = sub.add_parser("reviews")
    v.add_argument("--records", type=Path, required=True)
    v.add_argument("--max", type=int, required=True)
    r = sub.add_parser("report")
    _window_args(r)
    r.add_argument("--records", type=Path, required=True)
    r.add_argument("--records-source", default="a file")
    r.add_argument("--verdicts-dir", type=Path)
    r.add_argument("--github-dir", type=Path)
    r.add_argument("--acceptance-tenant", action="append", default=[])
    r.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd in ("window", "fs-query", "report"):
        try:
            since, until = resolve_window(args.since, args.until, args.weeks)
        except ValueError as exc:
            print(f"scorecard: {exc}", file=sys.stderr)
            return 2
    if args.cmd == "window":
        print(iso(since), iso(until))
        return 0
    if args.cmd == "fs-query":
        cursor = json.loads(args.cursor.read_text()) if args.cursor.is_file() else None
        print(json.dumps(fs_query_body(since, until, cursor)))
        return 0
    if args.cmd == "fs-decode":
        answer = json.loads(args.page.read_text(encoding="utf-8") or "[]")
        records, cursor = fs_decode(answer)
        with args.records.open("a", encoding="utf-8") as out:
            for record in records:
                out.write(json.dumps(record, sort_keys=True) + "\n")
        if cursor is not None and len(records) >= PAGE_SIZE:
            args.cursor.write_text(json.dumps(cursor))
        elif args.cursor.exists():
            args.cursor.unlink()
        print(f"rows {len(records)}")
        return 0
    if args.cmd == "reviews":
        records = read_records(args.records)
        wanted = [rec for rec in records
                  if role_for_step(rec.get("step_id")) in ("review", "re-review")
                  and rec.get("state") == SUCCEEDED and is_agent_task(rec)
                  and re.fullmatch(r"task_[A-Za-z0-9]+", str(rec.get("id") or ""))
                  and re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(rec.get("tenant_id") or ""))]
        # Newest first, so a cap keeps the most recent reviews.
        wanted.sort(key=lambda rec: str(rec.get("created_at") or ""), reverse=True)
        print(f"candidates {len(wanted)}")
        for rec in wanted[: max(args.max, 0)]:
            print(f"task {rec['id']} {rec['tenant_id']}")
        return 0

    records = read_records(args.records)
    verdicts, meta = read_verdicts(args.verdicts_dir)
    github = read_github(args.github_dir)
    acceptance = tuple(args.acceptance_tenant) or DEFAULT_ACCEPTANCE_TENANTS
    report = build_report(records, week_buckets(since, until), verdicts, meta, github, acceptance,
                          args.records_source)
    if args.json:
        print(json.dumps(to_json(report), indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
