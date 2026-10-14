#!/usr/bin/env python3
"""The context-quality baseline: the §2.6 metrics of docs/design/context-quality.md
(lane CTXQ-M), computed from what scripts/quality-baseline.sh recorded.

    quality_metrics.py select        --limit N --pages FILE      the lane window, as JSON
    quality_metrics.py windows       --since ISO --until ISO --hours H
    quality_metrics.py issue-run-prs --pages FILE --since ISO    PRs whose closing comment is needed
    quality_metrics.py report        --dir DIR [--json] [--operator-author NAME]...

WHY. The design measured a baseline once, by hand (§2.1, §9), and every later
phase is judged against it: the replay (§6.1), the A/B (§6.2) and the
default-on thresholds (§6.3). A baseline nobody can re-measure is a number
nobody can compare against, so this module is the definition, and the shell
script only fetches.

PURE. Every function takes what was recorded and returns numbers; nothing here
reads the network. tests/unit/scripts/test_quality_metrics.py holds each rule
that moves a figure, with a control.

THE DEFINITIONS, and the choices behind each one:

  M1-M3  A red run is a completed `pull_request` run of application, terraform
         or security with `conclusion == failure`, joined to a lane on its head
         branch. Cancelled, skipped and unfinished runs are DROPPED, not
         counted green: a newer push supersedes a cancelled run, so it says
         nothing about the change (the 0.52 of an earlier count included them).
         First-pass is the lane's earliest completed `application` run; a lane
         with none is unknown, never green.
  M4-M5  The verdict block the worker appends (`Review verdict: **X**`,
         agent_worker.verdict.pull_request_lines). Findings are the `- ` lines
         of the fenced block after it, plus its "N more finding(s)" line. A
         lane is NOT_YET or MERGE when any verdict in its body says so.
  M6     A hand fix-up is a non-merge commit on the PR head, outside main,
         authored by an operator identity (`--operator-author`, default the
         design's `bogdan-alexandrescu`). A merge from main is never one, even
         under the operator's name: it brings main in, it does not fix the lane.
  M7     An issue labelled `bug`, or a comment on a wave epic, citing `#N` of a
         lane, created between its merge and 14 days after it.
  M8     A PR whose body carries `<!-- swarmcloud-issue-run:`: merged, open,
         closed as a duplicate (its CLOSING comment -- the last one at or before
         the close -- says duplicate or superseded), or closed otherwise.
  M9     swarm-api's runs (`GET /v1/runs`): an edit is a PLANNED -> PLANNED move
         in the run's history; approved and rejected are moves to APPROVED and
         REJECTED; NOT_READY is the state. Without a credential it is NOT
         MEASURED, which is not zero.
  M10    Fix rounds are `## Continuation (...)` headings, each distinct heading
         counted ONCE (a body re-appended by a retry repeats it). A repeated fix
         is a fix commit -- one made after the lane's first red run -- that
         edits a file the lane had not touched before that run, when another
         lane's fix commit already edited it: the time-bomb fixture fixed four
         times by four lanes (§2.4) is exactly that shape.
  M11    Graph test reach from swarm-api's impact answer at the PR's base: the
         share of changed NON-TEST symbols that are not in `unmapped`. An
         answer whose `unmapped` list was cut is a floor, and is left out.
  M12    Each lane's task (`swarm/task_<id>`): `cost_usd_total` and
         `duration_s_total` from `GET /v1/tasks/{id}`. A null cost is not $0,
         and any `cost_incomplete` makes the sum a floor.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "swarm.quality-baseline/v1"

#: The workflows whose pull_request runs are a lane's CI (§2.1).
WORKFLOWS = ("application", "terraform", "security")
#: The conclusions that say something about the change. Anything else --
#: cancelled, skipped, neutral, null (not finished) -- is dropped.
COUNTED_CONCLUSIONS = ("success", "failure")
LANE_PREFIX = "swarm/"
#: The design's operator identity (§2.1 "Hand fix-up"). The worker commits as
#: `bogdan`, and commits brought in from main carry `Bogdan Alexandrescu`.
DEFAULT_OPERATORS = ("bogdan-alexandrescu",)
#: How long after a merge a bug citing the lane counts as escaped (M7).
ESCAPE_DAYS = 14
ISSUE_RUN_MARKER = "<!-- swarmcloud-issue-run:"
#: A closing comment can land a moment after the close it explains.
CLOSE_SLACK = timedelta(minutes=2)

_VERDICT = re.compile(r"^Review verdict: \*\*([A-Z_]+)\*\*", re.MULTILINE)
_MORE = re.compile(r"^(\d+) more finding\(s\)", re.MULTILINE)
_CONTINUATION = re.compile(r"^## Continuation \(.*$", re.MULTILINE)
_DUPLICATE = re.compile(r"\b(duplicate[sd]?|superseded|supersedes)\b", re.IGNORECASE)
_TASK = re.compile(r"^swarm/(task_[A-Za-z0-9]+)")
_REF = re.compile(r"(?<![\w/])#(\d+)\b")


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------

def _at(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _workflow(run: Mapping[str, Any]) -> str:
    path = str(run.get("path") or "")
    if path:
        return Path(path.split("@", 1)[0]).stem
    return str(run.get("name") or "").lower()


def _head(pr: Mapping[str, Any]) -> str:
    return str((pr.get("head") or {}).get("ref") or "")


def _number(value: Any) -> int:
    return int(value)


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

def select_lanes(prs: Iterable[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The `limit` newest merged `swarm/*` PRs, oldest first, each once."""
    seen: dict[int, dict[str, Any]] = {}
    for pr in prs:
        if pr.get("merged_at") and _head(pr).startswith(LANE_PREFIX):
            seen[_number(pr["number"])] = dict(pr)
    newest = sorted(seen, reverse=True)[:limit]
    return [seen[n] for n in sorted(newest)]


def run_windows(since: datetime, until: datetime, hours: int) -> list[str]:
    """`created=` ranges for the runs list. The API answers at most 1,000 runs
    per query, so the window is read in bounded pieces (§9)."""
    out = []
    step = timedelta(hours=hours)
    start = since
    while start < until:
        end = min(start + step - timedelta(seconds=1), until)
        out.append(f"{_iso(start)}..{_iso(end)}")
        start = start + step
    return out


def task_id_of(pr: Mapping[str, Any]) -> str | None:
    match = _TASK.match(_head(pr))
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# M1-M3: CI
# --------------------------------------------------------------------------

def counted_runs(runs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    by_id: dict[Any, Mapping[str, Any]] = {}
    for run in runs:
        if run.get("event") != "pull_request" or _workflow(run) not in WORKFLOWS:
            continue
        if run.get("conclusion") not in COUNTED_CONCLUSIONS:
            continue
        by_id[run.get("id")] = run
    return list(by_id.values())


def ci_metrics(lanes: Sequence[Mapping[str, Any]], runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    counted = counted_runs(runs)
    by_branch: dict[str, list[Mapping[str, Any]]] = {}
    for run in counted:
        by_branch.setdefault(str(run.get("head_branch") or ""), []).append(run)
    per_pr = []
    for pr in lanes:
        mine = by_branch.get(_head(pr), [])
        red = [r for r in mine if r.get("conclusion") == "failure"]
        app = sorted((r for r in mine if _workflow(r) == "application"),
                     key=lambda r: (str(r.get("created_at") or ""), str(r.get("id"))))
        first_red = min((str(r.get("created_at") or "") for r in red), default=None)
        per_pr.append({
            "number": _number(pr["number"]),
            "runs": len(mine),
            "red_runs": len(red),
            "red_by_workflow": {w: sum(1 for r in red if _workflow(r) == w) for w in WORKFLOWS},
            "first_pass": (app[0].get("conclusion") == "success") if app else None,
            "first_red_at": first_red,
        })
    measured = [p for p in per_pr if p["first_pass"] is not None]
    return {
        "runs_counted": sum(p["runs"] for p in per_pr),
        "m1_red_runs": sum(p["red_runs"] for p in per_pr),
        "m1_red_by_workflow": {w: sum(p["red_by_workflow"][w] for p in per_pr) for w in WORKFLOWS},
        "m2_red_prs": sum(1 for p in per_pr if p["red_runs"]),
        "m3_first_pass": sum(1 for p in measured if p["first_pass"]),
        "m3_measured": len(measured),
        "per_pr": per_pr,
    }


# --------------------------------------------------------------------------
# M4-M5: the review verdict
# --------------------------------------------------------------------------

def parse_verdicts(body: str | None) -> list[dict[str, Any]]:
    """Each verdict block in a PR body, with how many findings it carried."""
    text = body or ""
    marks = list(_VERDICT.finditer(text))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[mark.end():end]
        findings = 0
        fence = block.find("Findings, as the review wrote them:")
        if fence >= 0:
            opened = block.find("```", fence)
            closed = block.find("```", opened + 3) if opened >= 0 else -1
            if opened >= 0 and closed > opened:
                inner = block[opened:closed].splitlines()[1:]
                findings = sum(1 for line in inner if line.startswith("- "))
                more = _MORE.search(block[closed:])
                if more:
                    findings += int(more.group(1))
        out.append({"verdict": mark.group(1), "findings": findings})
    return out


def review_metrics(lanes: Sequence[Mapping[str, Any]], ci: Mapping[str, Any]) -> dict[str, Any]:
    red = {p["number"] for p in ci["per_pr"] if p["red_runs"]}
    not_yet, merge, findings, reviewed = [], set(), 0, 0
    for pr in lanes:
        verdicts = parse_verdicts(pr.get("body"))
        if verdicts:
            reviewed += 1
        number = _number(pr["number"])
        if any(v["verdict"] == "NOT_YET" for v in verdicts):
            not_yet.append(number)
            findings += sum(v["findings"] for v in verdicts if v["verdict"] == "NOT_YET")
        if any(v["verdict"] == "MERGE" for v in verdicts):
            merge.add(number)
    return {
        "reviewed_prs": reviewed,
        "m4_not_yet_prs": len(not_yet),
        "m4_not_yet": not_yet,
        "m4_findings": findings,
        "m4_findings_per_not_yet": round(findings / len(not_yet), 2) if not_yet else None,
        "m5_red_prs": len(red),
        "m5_red_after_merge": len(red & merge),
    }


# --------------------------------------------------------------------------
# M6: hand fix-up
# --------------------------------------------------------------------------

def parse_git_log(text: str) -> list[dict[str, Any]]:
    """`git log --format=%x1e%H%x1f%P%x1f%an%x1f%cI --name-only` as commits."""
    commits = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        header, _, rest = record.partition("\n")
        fields = header.split("\x1f")
        if len(fields) < 4:
            continue
        sha, parents, author, date = fields[:4]
        commits.append({
            "sha": sha.strip(),
            "parents": parents.split(),
            "author": author,
            "date": date.strip(),
            "files": [line.strip() for line in rest.splitlines() if line.strip()],
        })
    return commits


def hand_fixups(commits: Iterable[Mapping[str, Any]], operators: Sequence[str]) -> list[Mapping[str, Any]]:
    names = set(operators)
    return [c for c in commits if len(c.get("parents") or ()) < 2 and c.get("author") in names]


def hand_fixup_metrics(lanes: Sequence[Mapping[str, Any]], commits: Mapping[int, list] | None,
                       operators: Sequence[str]) -> dict[str, Any]:
    commits = commits or {}
    read = [p for p in lanes if _number(p["number"]) in commits]
    per = {_number(p["number"]): hand_fixups(commits[_number(p["number"])], operators) for p in read}
    return {
        "prs_read": len(read),
        "prs_unread": [_number(p["number"]) for p in lanes if _number(p["number"]) not in commits],
        "commits_read": sum(len(commits[_number(p["number"])]) for p in read),
        "m6_prs": sum(1 for v in per.values() if v),
        "m6_commits": sum(len(v) for v in per.values()),
        "m6_hand_fixed": sorted(n for n, v in per.items() if v),
        "operators": list(operators),
    }


# --------------------------------------------------------------------------
# M7: escaped defects
# --------------------------------------------------------------------------

def escaped_defects(lanes: Sequence[Mapping[str, Any]], issues: Iterable[Mapping[str, Any]] | None,
                    epic_comments: Iterable[Mapping[str, Any]] | None,
                    days: int = ESCAPE_DAYS) -> dict[str, Any]:
    merged = {_number(p["number"]): _at(p.get("merged_at")) for p in lanes}
    sources: list[tuple[str, str, datetime | None]] = []
    issues_read = 0
    for issue in issues or ():
        if "pull_request" in issue:
            continue
        labels = {str(label.get("name") if isinstance(label, Mapping) else label)
                  for label in issue.get("labels") or ()}
        if "bug" not in labels:
            continue
        issues_read += 1
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        sources.append((f"issue #{issue.get('number')}", text, _at(issue.get("created_at"))))
    comments_read = 0
    for comment in epic_comments or ():
        comments_read += 1
        sources.append((f"epic-comment #{comment.get('issue')}", str(comment.get("body") or ""),
                        _at(comment.get("created_at"))))
    items = []
    for source, text, created in sources:
        if created is None:
            continue
        for ref in sorted({int(m) for m in _REF.findall(text)}):
            at = merged.get(ref)
            if at is not None and at <= created <= at + timedelta(days=days):
                items.append({"source": source, "pr": ref})
    return {
        "bug_issues_read": issues_read,
        "epic_comments_read": comments_read,
        "m7_escaped": len(items),
        "prs_with_escape": len({i["pr"] for i in items}),
        "items": items,
        "days": days,
    }


# --------------------------------------------------------------------------
# M8: issue-run outcomes
# --------------------------------------------------------------------------

def issue_run_prs(prs: Iterable[Mapping[str, Any]], since: str) -> list[dict[str, Any]]:
    start = _at(since)
    seen: dict[int, dict[str, Any]] = {}
    for pr in prs:
        if ISSUE_RUN_MARKER not in str(pr.get("body") or ""):
            continue
        created = _at(pr.get("created_at"))
        if start is not None and created is not None and created < start:
            continue
        seen[_number(pr["number"])] = dict(pr)
    return [seen[n] for n in sorted(seen)]


def needs_closing_comment(pr: Mapping[str, Any]) -> bool:
    return pr.get("state") == "closed" and not pr.get("merged_at")


def issue_run_outcomes(prs: Iterable[Mapping[str, Any]], comments: Mapping[int, list] | None,
                       since: str) -> dict[str, Any]:
    comments = comments or {}
    out = {"merged": 0, "open": 0, "closed_duplicate": 0, "closed_other": 0}
    unread, duplicates = [], []
    runs = issue_run_prs(prs, since)
    for pr in runs:
        number = _number(pr["number"])
        if pr.get("merged_at"):
            out["merged"] += 1
        elif pr.get("state") == "open":
            out["open"] += 1
        elif number not in comments:
            unread.append(number)
            out["closed_other"] += 1
        else:
            closed = _at(pr.get("closed_at"))
            before = [c for c in comments[number]
                      if closed is None or (_at(c.get("created_at")) or closed) <= closed + CLOSE_SLACK]
            before.sort(key=lambda c: str(c.get("created_at") or ""))
            closing = str(before[-1].get("body") or "") if before else ""
            if _DUPLICATE.search(closing):
                out["closed_duplicate"] += 1
                duplicates.append(number)
            else:
                out["closed_other"] += 1
    return {"m8_issue_run_prs": len(runs), **out, "duplicates": duplicates,
            "closed_comment_unread": unread}


# --------------------------------------------------------------------------
# M9: plans (swarm-api)
# --------------------------------------------------------------------------

def plan_metrics(runs: Iterable[Mapping[str, Any]] | None, since: str) -> dict[str, Any]:
    if runs is None:
        return {"measured": False}
    start = _at(since)
    by_id: dict[str, Mapping[str, Any]] = {}
    for run in runs:
        created = _at(run.get("created_at"))
        if start is not None and created is not None and created < start:
            continue
        by_id[str(run.get("id"))] = run
    approved = edits = edited = rejected = not_ready = 0
    for run in by_id.values():
        moves = [(h.get("from"), h.get("to")) for h in run.get("history") or () if isinstance(h, Mapping)]
        n = sum(1 for a, b in moves if a == "PLANNED" and b == "PLANNED")
        edits += n
        edited += 1 if n else 0
        approved += 1 if any(b == "APPROVED" for _a, b in moves) else 0
        rejected += 1 if (run.get("state") == "REJECTED" or any(b == "REJECTED" for _a, b in moves)) else 0
        not_ready += 1 if run.get("state") == "NOT_READY" else 0
    return {"m9_runs": len(by_id), "approved": approved, "edits": edits, "runs_edited": edited,
            "rejected": rejected, "not_ready": not_ready, "measured": True}


# --------------------------------------------------------------------------
# M10: fix rounds
# --------------------------------------------------------------------------

def continuations(body: str | None) -> int:
    return len({m.group(0).strip() for m in _CONTINUATION.finditer(body or "")})


def fix_round_metrics(lanes: Sequence[Mapping[str, Any]], ci: Mapping[str, Any],
                      commits: Mapping[int, list] | None) -> dict[str, Any]:
    commits = commits or {}
    first_red = {p["number"]: _at(p["first_red_at"]) for p in ci["per_pr"]}
    counts = {_number(p["number"]): continuations(p.get("body")) for p in lanes}
    # (when, pr, file) for every file a fix commit edited outside the lane's own change
    fixes: list[tuple[datetime, int, str]] = []
    with_fixes = 0
    for pr in lanes:
        number = _number(pr["number"])
        red_at = first_red.get(number)
        if red_at is None or number not in commits:
            continue
        own: set[str] = set()
        mine = []
        for c in commits[number]:
            if len(c.get("parents") or ()) >= 2:
                continue
            at = _at(c.get("date"))
            if at is None:
                continue
            if at <= red_at:
                own.update(c.get("files") or ())
            else:
                mine.append((at, c))
        if mine:
            with_fixes += 1
        for at, c in mine:
            for f in c.get("files") or ():
                if f not in own:
                    fixes.append((at, number, f))
    fixes.sort()
    first: dict[str, int] = {}
    repeats = []
    reported: set[tuple[int, str]] = set()
    for _at_, number, f in fixes:
        earlier = first.setdefault(f, number)
        if earlier != number and (number, f) not in reported:
            reported.add((number, f))
            repeats.append({"pr": number, "file": f, "first_fixed_in": earlier})
    return {
        "m10_continuations": sum(counts.values()),
        "prs_continued": sum(1 for v in counts.values() if v),
        "prs_with_fix_commits": with_fixes,
        "m10_repeats": repeats,
    }


# --------------------------------------------------------------------------
# M11: graph test reach (swarm-api impact)
# --------------------------------------------------------------------------

def test_reach(impacts: Mapping[int, Mapping[str, Any]] | None) -> dict[str, Any]:
    if impacts is None:
        return {"measured": False}
    symbols = reached = 0
    measured, empty, cut, stale = [], [], [], []
    for number in sorted(impacts):
        answer = impacts[number]
        if answer.get("stale"):
            stale.append(number)
        changed = [c for c in answer.get("changed") or () if c.get("kind") != "test"]
        if "unmapped" in (answer.get("lists_cut") or ()):
            cut.append(number)
            continue
        if not changed:
            empty.append(number)
            continue
        ids = {c.get("id") for c in changed}
        missed = {u.get("symbol") for u in answer.get("unmapped") or () if u.get("symbol") in ids}
        symbols += len(ids)
        reached += len(ids - missed)
        measured.append(number)
    return {
        "measured": True,
        "m11_symbols": symbols,
        "m11_reached": reached,
        "m11_reach": round(reached / symbols, 4) if symbols else None,
        "prs_measured": len(measured),
        "prs_without_symbols": empty,
        "prs_cut": cut,
        "stale_index": stale,
    }


# --------------------------------------------------------------------------
# M12: cost and wall time (swarm-api tasks)
# --------------------------------------------------------------------------

def cost_metrics(lanes: Sequence[Mapping[str, Any]], tasks: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    if tasks is None:
        return {"measured": False}
    costs, walls, without, unread = [], [], [], []
    floor = False
    for pr in lanes:
        task_id = task_id_of(pr)
        if task_id is None:
            continue
        task = tasks.get(task_id)
        if task is None:
            unread.append(task_id)
            continue
        cost = task.get("cost_usd_total")
        if cost is None:
            without.append(task_id)
        else:
            costs.append(float(cost))
        if task.get("cost_incomplete") or cost is None:
            floor = True
        if task.get("duration_s_total") is not None:
            walls.append(float(task["duration_s_total"]))
    return {
        "measured": True,
        "m12_cost_usd": round(sum(costs), 4) if costs else None,
        "median_cost_usd": round(statistics.median(costs), 4) if costs else None,
        "cost_is_floor": floor,
        "tasks_with_cost": len(costs),
        "tasks_without_cost": without,
        "tasks_unread": unread,
        "m12_wall_s": round(sum(walls)) if walls else None,
        "median_wall_s": round(statistics.median(walls)) if walls else None,
    }


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def build_report(bundle: Mapping[str, Any], operators: Sequence[str] = DEFAULT_OPERATORS) -> dict[str, Any]:
    lanes = list(bundle.get("lanes") or [])
    runs = list(bundle.get("runs") or [])
    since = min((str(p.get("created_at") or "") for p in lanes), default="") or "1970-01-01T00:00:00Z"
    ci = ci_metrics(lanes, runs)
    report = {
        "schema": SCHEMA,
        "window": {
            "lanes": len(lanes),
            "first_pr": min((_number(p["number"]) for p in lanes), default=None),
            "last_pr": max((_number(p["number"]) for p in lanes), default=None),
            "since": since,
            "until": max((str(p.get("created_at") or "") for p in lanes), default=None),
        },
        "visited": {
            "lanes": len(lanes),
            "runs_read": len(runs),
            "runs_counted": ci["runs_counted"],
        },
        "ci": ci,
        "review": review_metrics(lanes, ci),
        "hand_fixup": hand_fixup_metrics(lanes, bundle.get("commits"), operators),
        "escaped": escaped_defects(lanes, bundle.get("issues"), bundle.get("epic_comments")),
        "issue_runs": issue_run_outcomes(list(bundle.get("all_prs") or lanes),
                                         bundle.get("issue_run_comments"),
                                         bundle.get("issue_runs_since") or since),
        "plans": plan_metrics(bundle.get("api_runs"), since),
        "fix_rounds": fix_round_metrics(lanes, ci, bundle.get("commits")),
        "test_reach": test_reach(bundle.get("impacts")),
        "cost": cost_metrics(lanes, bundle.get("tasks")),
    }
    hand = report["hand_fixup"]
    report["visited"].update({"commits_read": hand["commits_read"], "heads_read": hand["prs_read"]})
    red = {p["number"] for p in ci["per_pr"] if p["red_runs"]}
    after = red | set(report["review"]["m4_not_yet"]) | set(hand["m6_hand_fixed"])
    report["needed_anything"] = len(after)
    return report


def _or_na(value: Any) -> str:
    """A figure, or `n/a` when there was nothing to compute it from. 0 stays 0."""
    return "n/a" if value is None else str(value)


def _share(n: int, d: int) -> str:
    return f"{n} / {d} = {100 * n / d:.1f}%" if d else f"{n} / 0"


def render(report: Mapping[str, Any]) -> list[str]:
    w, v, ci = report["window"], report["visited"], report["ci"]
    n = w["lanes"]
    review, hand, esc = report["review"], report["hand_fixup"], report["escaped"]
    runs, plans, fix = report["issue_runs"], report["plans"], report["fix_rounds"]
    reach, cost = report["test_reach"], report["cost"]
    by_wf = ", ".join(f"{k} {c}" for k, c in ci["m1_red_by_workflow"].items())
    lines = [
        f"Window: {n} merged lane PR(s), #{w['first_pr']}-#{w['last_pr']}, created {w['since']} to {w['until']}",
        f"Visited: {v['lanes']} PR(s), {v['runs_read']} run(s) read, {v['runs_counted']} counted, "
        f"{v['heads_read']} head(s) with {v['commits_read']} commit(s)",
        "",
        f"M1  red runs per PR            {ci['m1_red_runs'] / n if n else 0:.2f} ({ci['m1_red_runs']} red / {n}; {by_wf})",
        f"M2  PRs with a red run         {_share(ci['m2_red_prs'], n)}",
        f"M3  first-pass application CI  {_share(ci['m3_first_pass'], ci['m3_measured'])} "
        f"({n - ci['m3_measured']} PR(s) with no application run)",
        f"M4  review NOT_YET             {_share(review['m4_not_yet_prs'], n)}, "
        f"{review['m4_findings']} finding(s), {_or_na(review['m4_findings_per_not_yet'])} per NOT_YET",
        f"M5  red after a MERGE verdict  {review['m5_red_after_merge']} / {review['m5_red_prs']} red PR(s)",
        f"M6  hand fix-up                {_share(hand['m6_prs'], hand['prs_read'])} "
        f"({hand['m6_commits']} commit(s); author {', '.join(hand['operators'])})",
    ]
    if hand["prs_unread"]:
        lines.append(f"      heads not read: {len(hand['prs_unread'])} -- not counted either way")
    lines += [
        f"    needed anything after the agent (red, NOT_YET or hand): {_share(report['needed_anything'], n)}",
        f"M7  escaped defects            {esc['m7_escaped']} citation(s) of {esc['prs_with_escape']} PR(s) "
        f"within {esc['days']} days ({esc['bug_issues_read']} bug issue(s), "
        f"{esc['epic_comments_read']} epic comment(s) read)",
    ]
    lines += [f"      {i['source']} cites #{i['pr']}" for i in esc["items"]]
    lines.append(
        f"M8  issue-run PRs              {runs['m8_issue_run_prs']}: {runs['merged']} merged, "
        f"{runs['open']} open, {runs['closed_duplicate']} closed as duplicate, "
        f"{runs['closed_other']} closed otherwise")
    if runs["closed_comment_unread"]:
        lines.append(f"      closing comment not read for {len(runs['closed_comment_unread'])}: counted as otherwise")
    if plans["measured"]:
        lines.append(
            f"M9  plans                      {plans['m9_runs']} run(s): {plans['approved']} approved, "
            f"{plans['edits']} edit(s) on {plans['runs_edited']} run(s), {plans['rejected']} rejected, "
            f"{_share(plans['not_ready'], plans['m9_runs'])} NOT_READY")
    else:
        lines.append("M9  plans                      not measured (needs --api and an operator credential)")
    lines.append(
        f"M10 fix rounds                 {fix['m10_continuations']} continuation(s) on "
        f"{fix['prs_continued']} PR(s); {fix['prs_with_fix_commits']} PR(s) with a fix commit after red; "
        f"{len(fix['m10_repeats'])} repeated fix(es)")
    lines += [f"      #{r['pr']} re-fixed {r['file']} (first fixed in #{r['first_fixed_in']})"
              for r in fix["m10_repeats"]]
    if reach["measured"]:
        share = f"{100 * reach['m11_reach']:.1f}%" if reach["m11_reach"] is not None else "n/a"
        lines.append(
            f"M11 changed-symbol test reach  {reach['m11_reached']} / {reach['m11_symbols']} = {share} "
            f"over {reach['prs_measured']} PR(s); {len(reach['prs_cut'])} cut (floor, left out), "
            f"{len(reach['prs_without_symbols'])} with no changed symbol, "
            f"{len(reach['stale_index'])} on a stale index")
    else:
        lines.append("M11 changed-symbol test reach  not measured (needs --api and --repo-id)")
    if cost["measured"]:
        floor = "at least " if cost["cost_is_floor"] else ""
        total = f"${cost['m12_cost_usd']:.2f}" if cost["m12_cost_usd"] is not None else "no cost recorded"
        lines.append(
            f"M12 lane cost and wall time    {floor}{total} over {cost['tasks_with_cost']} task(s) "
            f"(median ${_or_na(cost['median_cost_usd'])}), {_or_na(cost['m12_wall_s'])} s total; "
            f"{len(cost['tasks_without_cost'])} with no cost, {len(cost['tasks_unread'])} not readable")
    else:
        lines.append("M12 lane cost and wall time    not measured (needs --api and an operator credential)")
    return lines


# --------------------------------------------------------------------------
# reading a recorded directory
# --------------------------------------------------------------------------

def _json_stream(path: Path) -> list[Any]:
    """One or more JSON documents in one file (arrays are flattened)."""
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    out: list[Any] = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        value, i = decoder.raw_decode(text, i)
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


def _optional(path: Path) -> list[Any] | None:
    return _json_stream(path) if path.exists() else None


def load_dir(root: Path) -> dict[str, Any]:
    bundle: dict[str, Any] = {"lanes": _json_stream(root / "lanes.json")}
    for key, name in (("runs", "runs.json"), ("all_prs", "prs.json"), ("issues", "bugs.json"),
                      ("epic_comments", "epic-comments.json")):
        value = _optional(root / name)
        if value is not None:
            bundle[key] = value
    if (root / "commits").is_dir():
        bundle["commits"] = {int(p.stem): parse_git_log(p.read_text(encoding="utf-8"))
                             for p in (root / "commits").glob("*.log") if p.stem.isdigit()}
    if (root / "comments").is_dir():
        bundle["issue_run_comments"] = {int(p.stem): _json_stream(p)
                                        for p in (root / "comments").glob("*.json") if p.stem.isdigit()}
    api_runs = _optional(root / "api-runs.json")
    if api_runs is not None:
        bundle["api_runs"] = [r for page in api_runs for r in (page.get("runs") or [])]
    tasks = _optional(root / "tasks.json")
    if tasks is not None:
        bundle["tasks"] = {str(t["task"]["id"]): t["task"] for t in tasks
                           if isinstance(t, dict) and isinstance(t.get("task"), dict)}
    if (root / "impact").is_dir():
        bundle["impacts"] = {int(p.stem): json.loads(p.read_text(encoding="utf-8"))
                             for p in (root / "impact").glob("*.json") if p.stem.isdigit()}
    since = root / "issue-runs-since.txt"
    if since.exists():
        bundle["issue_runs_since"] = since.read_text(encoding="utf-8").strip()
    return bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_select = sub.add_parser("select")
    p_select.add_argument("--limit", type=int, required=True)
    p_select.add_argument("--pages", type=Path, required=True)
    p_windows = sub.add_parser("windows")
    p_windows.add_argument("--since", required=True)
    p_windows.add_argument("--until", required=True)
    p_windows.add_argument("--hours", type=int, default=6)
    p_runs = sub.add_parser("issue-run-prs")
    p_runs.add_argument("--pages", type=Path, required=True)
    p_runs.add_argument("--since", required=True)
    p_report = sub.add_parser("report")
    p_report.add_argument("--dir", type=Path, required=True)
    p_report.add_argument("--json", action="store_true")
    p_report.add_argument("--operator-author", action="append", default=[])
    args = parser.parse_args(argv)

    if args.command == "select":
        json.dump(select_lanes(_json_stream(args.pages), args.limit), sys.stdout)
        sys.stdout.write("\n")
        return 0
    if args.command == "windows":
        since, until = _at(args.since), _at(args.until)
        if since is None or until is None or args.hours < 1:
            print("windows: --since and --until must be ISO 8601 and --hours positive", file=sys.stderr)
            return 2
        print("\n".join(run_windows(since, until, args.hours)))
        return 0
    if args.command == "issue-run-prs":
        for pr in issue_run_prs(_json_stream(args.pages), args.since):
            if needs_closing_comment(pr):
                print(pr["number"])
        return 0

    bundle = load_dir(args.dir)
    if not bundle["lanes"]:
        print("report: no lane PR was recorded, so there is nothing to measure", file=sys.stderr)
        return 2
    report = build_report(bundle, tuple(args.operator_author) or DEFAULT_OPERATORS)
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print("\n".join(render(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
