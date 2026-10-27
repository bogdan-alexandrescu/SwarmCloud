"""The per-project build board: one local HTML page of this project's SwarmCloud work.

Owner requirements and decisions, 2026-10-20 (#1773; lane BOARD-CORE):

* PER PROJECT, LOCAL, NEVER COMMITTED. The page is
  `<project>/.claude/build-board/index.html`, beside a `.gitignore` of `*`, so
  git ignores the folder without the repository's own `.gitignore` being
  edited -- the rule `.sc-swarmcloud-runs/` already follows (`launch`). It
  belongs to no Claude account or profile: a claudeswitch rotation changes
  nothing, because nothing is published anywhere.
* SCOPE: every workflow and single task THE CALLER submitted against THIS
  project's repository (its `origin`, keyed by `check.repo_key`), from any
  Claude Code session or account -- not only this session's, and not
  teammates'. In-flight work always shows; finished work for
  `HISTORY_DAYS` days.
* GROUPING: by the issue each lane names -- a step's `issue` input, else a
  `(... #N)` in its title -- labelled with the issue's title where SwarmCloud
  has it, else the number alone; "No issue" for the rest. No hand-kept map.
* WAITING ON SOMEONE lists exactly three kinds, from what SwarmCloud knows:
  owner questions (the recommended option marked); steps waiting for a
  person (AWAITING_INPUT, and HUMAN_REQUIRED parks), each with its
  `/sc answer WF STEP`; merges refused (`check.MERGE_CODES`) and CI parks
  over `CI_PARK_MINUTES`. A failed step in general is NOT listed.
* AN OWNER QUESTION WAITS only while its lane is open (owner decision
  2026-10-27, "Yes, narrow it"; #1812): a finished step's question, in a
  workflow that has neither finished nor merged (a merge step SUCCEEDED),
  and that no answer recorded on any step of the lane answers -- the task's
  `owner_answers`, which `swarm_task_answer` and the console write. A
  finished single task is a finished lane, so its questions never wait. A
  question several steps asked is listed ONCE per lane, naming every step
  that asked it, and "Questions that could not be read" is listed only when
  no step of the lane read any. Before this, a board of weeks of merged
  lanes counted 1,054 "needs you", nearly all old questions, many twice.
* NOTHING IS READ FROM GITHUB. Every read is a GET this bridge already makes
  elsewhere; `swarm_check` reads a refused PR's state from GitHub, the board
  does not, so a refused merge stays listed for as long as its lane does.

WHERE AN ISSUE'S TITLE COMES FROM. The worker reads an issue into `issue.md`
in the workspace -- never into the artifacts -- and records its title nowhere
the API serves. SwarmCloud has it in two places: an issue run's compiled
workflow is titled `<owner>/<repo>#<n>: <issue title>`
(`issueruns.workflow_title`), and `GET /v1/runs` serves each run's
`issue_read.title`. Both are read; a failed runs read leaves the number alone.

WHAT IS CACHED, AND WHY. A finished task's questions and pull request never
change, nor does a finished lane: `.claude/build-board/cache.json` keeps them,
so a refresh re-reads only what is still moving. Nothing else is kept there.

A FAILED READ leaves the previous page in place (the page is replaced by a
rename, never rewritten in place) and is recorded beside it; the next page
that is written says in its banner when the refresh before it failed and why.

NO SECRET REACHES THE PAGE. Only fields the API serves masked are drawn --
titles and labels (`metadata`), questions (the artifacts route, redacted),
states, times, refusal codes -- never a step's input or prompt, and never an
environment value. Every string is escaped; a console link is drawn only when
it is an http(s) URL, as served (`follow.console_link`), never built.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from swarm_common.states import TERMINAL_STATES

from . import awaitinput, checkout, progress, workflows
from .check import EVENT_PAGE, MERGE_CODES, MERGE_PROFILE, _entered, _refusal_code, pull_request_link, repo_key
from .client import SwarmClient, SwarmError, task_id_of
from .follow import console_link
from .invocation import terminal_command
from .render import parse_time

#: Where the board lives, under the project directory.
BOARD_DIR = Path(".claude") / "build-board"
PAGE_NAME = "index.html"
CACHE_NAME = "cache.json"
#: The last refresh that failed, read into the next page's banner and then removed.
FAILURE_NAME = "last-failure.json"
#: 2 since #1812 (2026-10-27): a version-1 cache holds finished lanes with the
#: owner questions they listed before a merged or finished lane stopped waiting,
#: and `collect()` reuses a cached finished lane verbatim, so those questions
#: would stay on the board for the whole HISTORY_DAYS window. A new version
#: drops the old cache and every lane is read once under the new rule.
CACHE_VERSION = 2

#: Finished work stays on the board this long (owner decision 4, #1773).
HISTORY_DAYS = 30
#: How far past the window a listing is paged by creation time. The listings
#: are newest CREATED first, and a lane is kept by when it ENDED: a workflow
#: created 34 days ago that finished 29 days ago belongs on the board. Seven
#: days is longer than any lane has run; a lane older than that is left out.
LISTING_MARGIN_DAYS = 7
#: A merge step's CI park is listed past this many minutes (owner decision 3),
#: the same line `/sc check` flags it at.
CI_PARK_MINUTES = 90
#: Workflow envelopes read at once, as `sc.WORKFLOW_DETAIL_CONCURRENCY` reads them.
DETAIL_CONCURRENCY = 8
#: One page of each listing. The API clamps it to its own maximum.
PAGE_SIZE = 50
TASK_PAGE_SIZE = 100
#: Issue runs read for their titles: one page, newest first. A title missing
#: here is the number alone, never a failed board.
RUN_PAGE_SIZE = 100

_TERMINAL = frozenset(s.value for s in TERMINAL_STATES)
_QUEUED = frozenset({"QUEUED", "READY"})

#: The states a lane is shown in (owner's board), and the pill each wears.
QUEUED, RUNNING, IN_CI, REFUSED = "Queued", "Running", "In CI", "Red CI or refused"
MERGED, SUCCEEDED, FAILED, CANCELLED = "Merged", "Succeeded", "Failed", "Cancelled"
DONE_STATES = frozenset({MERGED, SUCCEEDED})
_PILL = {QUEUED: "queued", RUNNING: "running", IN_CI: "ci", REFUSED: "refused",
         MERGED: "done", SUCCEEDED: "done", FAILED: "failed", CANCELLED: "cancelled"}

NO_ISSUE = "No issue"
#: `(#12)`, `(LANE; part of #12)`: the issue a title names, in its last parenthesis.
_TITLE_ISSUE = re.compile(r"\(([^()]*?)#([1-9][0-9]{0,8})\)\s*$")
#: An issue run's workflow title: `<owner>/<repo>#<n>: <issue title>`.
_RUN_TITLE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#([1-9][0-9]{0,8}):\s*(.+)$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    at = parse_time(value)
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if at else None


def _clock(value: Any) -> str:
    at = parse_time(value)
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if at else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        number = int(value.strip().lstrip("#"))
        return number if number > 0 else None
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# Reading one task
# --------------------------------------------------------------------------


def pull_request_of(task: dict[str, Any]) -> int | None:
    """The PR a step or task names: the one it published (`result_summary.git.
    pull_request`), else a merge step's (`result_summary.pull_request`), else
    the one its input names."""
    summary = _mapping(task.get("result_summary"))
    published = _int(_mapping(_mapping(summary.get("git")).get("pull_request")).get("number"))
    if published:
        return published
    return _int(summary.get("pull_request")) or _int(_mapping(task.get("input")).get("pull_request"))


def issue_of(task_input: Any, title: Any) -> int | None:
    """The issue a lane names: its `issue` input, else `(... #N)` ending its title."""
    issue = _mapping(task_input).get("issue")
    number = _int(_mapping(issue).get("number")) if isinstance(issue, dict) else _int(issue)
    if number:
        return number
    found = _TITLE_ISSUE.search(str(title or ""))
    return int(found.group(2)) if found else None


def _run_title(title: Any, key: str) -> tuple[int, str] | None:
    """`(number, issue title)` from an issue run's workflow title in THIS repository."""
    found = _RUN_TITLE.match(str(title or "").strip())
    if not found or repo_key(f"{found.group(1)}/{found.group(2)}") != key:
        return None
    return int(found.group(3)), found.group(4).strip()


class _Reader:
    """What one refresh reads past the listings, with the cache in front of it."""

    def __init__(self, client: SwarmClient, cache: dict[str, Any], now: datetime) -> None:
        self.client = client
        self.cache = cache
        self.now = now

    def finished(self, task: dict[str, Any], *, questions: bool = True) -> dict[str, Any]:
        """`{pull_request, questions, unavailable?}` of a FINISHED task, cached.

        `questions=False` reads nothing: a finished lane's questions wait on no
        one, so they are not fetched, and that answer is not cached."""
        tid = str(task_id_of(task) or "")
        kept = self.cache["tasks"].get(tid)
        if kept is not None:
            return kept
        if not questions:
            return {"pull_request": pull_request_of(task), "questions": [],
                    "ended": _iso(task.get("completed_at"))}
        asked = progress.owner_questions(self.client, task)
        entry: dict[str, Any] = {"pull_request": pull_request_of(task),
                                 "questions": asked.get("questions") or [],
                                 "ended": _iso(task.get("completed_at"))}
        if "questions_unavailable_because" in asked:
            # NOT CACHED: a read that failed is tried again on the next refresh.
            return {**entry, "unavailable": asked["questions_unavailable_because"]}
        self.cache["tasks"][tid] = entry
        return entry

    def parked_since(self, task: dict[str, Any]) -> str | None:
        """When a PARKED task entered PARKED, from its newest events (`check._entered`)."""
        query = urllib.parse.urlencode({"order": "desc", "limit": EVENT_PAGE})
        data = self.client.request("GET", f"/v1/tasks/{task_id_of(task)}/events?{query}")
        events = [e for e in (data.get("events") if isinstance(data, dict) else None) or [] if isinstance(e, dict)]
        return _iso(_entered(events, "PARKED", parse_time(task.get("created_at")))["since"])


# --------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------


def _answered(task: dict[str, Any], asked: list[dict[str, Any]]) -> set[str]:
    """The texts of the questions `task`'s recorded answers answer."""
    texts = set()
    for answer in progress.owner_answers(task):
        if answer.get("question"):
            texts.add(str(answer["question"]))
        index = answer.get("question_index")
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(asked):
            texts.add(str(asked[index].get("question") or ""))
    return texts


def _questions(lane: dict[str, Any], read: list[tuple[Any, dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    """An open lane's unanswered owner questions, once each, from its finished
    steps' `(step_id, task, reader.finished(task))`."""
    answered: set[str] = set()
    for _, task, done in read:
        answered |= _answered(task, done.get("questions") or [])
    shown: dict[str, tuple[dict[str, Any], list[str]]] = {}
    unread: list[str] = []
    since: list[str] = []
    for step, task, done in read:
        name = f"step {step}" if step else None
        if done.get("unavailable"):
            unread.append(f"{name}: {done['unavailable']}" if name else str(done["unavailable"]))
            since.append(done.get("ended") or "")
        for asked in done.get("questions") or []:
            text = str(asked.get("question") or "(no question text)")
            if text in answered:
                continue
            if text in shown:
                if name and name not in shown[text][1]:
                    shown[text][1].append(name)
                continue
            recommended = asked.get("recommended")
            shown[text] = ({
                "kind": "question", "who": "Owner", "item": text,
                "since": done.get("ended"), "unlocks": "a decision this lane left to the owner",
                "context": asked.get("context") or "",
                "options": [{"label": str(o.get("label") or ""), "recommended": bool(recommended)
                             and o.get("label") == recommended} for o in asked.get("options") or []],
                "next": None,
            }, [name] if name else [])
    items = []
    for item, steps in shown.values():
        context = item.pop("context")
        where = lane["label"] + (" · " + ", ".join(steps) if steps else "")
        items.append({**item, "detail": where + (f": {context}" if context else "")})
    if unread and not any(done.get("questions") for _, _, done in read):
        items.append({"kind": "question", "who": "Owner", "item": "Questions that could not be read",
                      "since": min((s for s in since if s), default=None),
                      "unlocks": "a decision this lane left to the owner",
                      "detail": f"{lane['label']} · " + "; ".join(unread), "options": [], "next": None})
    return items


def _dependants(steps: list[dict[str, Any]], step_id: Any) -> list[str]:
    return [str(s.get("step_id")) for s in steps if step_id in (s.get("depends_on") or [])]


def _unlocks(steps: list[dict[str, Any]], step_id: Any, lane: dict[str, Any]) -> str:
    after = _dependants(steps, step_id)
    return ("step " + ", ".join(after)) if after else f"the end of {lane['label']}"


def _step_waits(reader: _Reader, lane: dict[str, Any], steps: list[dict[str, Any]],
                step: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    """The waiting items one step's task raises (the three kinds, never a plain failure)."""
    wid, sid = lane["id"], step.get("step_id")
    state, park = task.get("state"), task.get("park_reason")
    is_merge = step.get("runner_profile") == MERGE_PROFILE
    items: list[dict[str, Any]] = []
    if state == "PARKED" and park == "AWAITING_INPUT":
        opened = awaitinput.window(task).get("opened_at")
        items.append({"kind": "awaiting_input", "who": f"A member of {task.get('tenant_id') or 'the workspace'}",
                      "item": f"Step {sid} of {lane['label']} waits for an answer",
                      "since": _iso(opened) or _iso(task.get("created_at")),
                      "unlocks": _unlocks(steps, sid, lane), "detail": "AWAITING_INPUT",
                      "next": f"/sc answer {wid} {sid}"})
    if state == "PARKED" and park == "HUMAN_REQUIRED":
        items.append({"kind": "human_required", "who": "A person",
                      "item": f"Step {sid} of {lane['label']} is parked until a person promotes it",
                      "since": reader.parked_since(task), "unlocks": _unlocks(steps, sid, lane),
                      "detail": "HUMAN_REQUIRED", "next": f"/sc answer {wid} {sid}"})
    if is_merge and state == "FAILED":
        code, message = _refusal_code(task)
        if code in MERGE_CODES:
            number = pull_request_of(task)
            items.append({"kind": "merge_refused", "who": "You",
                          "item": f"The merge of PR #{number} was refused" if number else "A merge was refused",
                          "since": _iso(task.get("completed_at")), "unlocks": f"{lane['label']} merging",
                          "detail": code + (f": {message}" if message else ""), "next": f"/sc status {wid}"})
    if is_merge and state == "PARKED" and park == "CI_PENDING":
        since = reader.parked_since(task)
        at = parse_time(since)
        if at is not None and (reader.now - at).total_seconds() > CI_PARK_MINUTES * 60:
            number = pull_request_of(task)
            minutes = int((reader.now - at).total_seconds() // 60)
            items.append({"kind": "ci_pending", "who": "CI",
                          "item": (f"PR #{number}" if number else "A merge") + f" has waited on CI for {minutes} min",
                          "since": since, "unlocks": f"{lane['label']} merging",
                          "detail": f"CI_PENDING over {CI_PARK_MINUTES} min", "next": f"/sc status {wid}"})
    for item in items:
        item["lane"] = wid
    return items


def _workflow_state(state: Any, tasks: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    merges = [t for s, t in tasks if s.get("runner_profile") == MERGE_PROFILE]
    if state == "CANCELLED":
        return CANCELLED
    if any(t.get("state") == "FAILED" and _refusal_code(t)[0] in MERGE_CODES for t in merges):
        return REFUSED
    if state == "SUCCEEDED":
        return MERGED if any(t.get("state") == "SUCCEEDED" for t in merges) else SUCCEEDED
    if state in _TERMINAL:
        return FAILED
    if any(t.get("state") == "PARKED" and t.get("park_reason") == "CI_PENDING" for t in merges):
        return IN_CI
    if state in _QUEUED or all(t.get("state") in _QUEUED for _, t in tasks):
        return QUEUED
    return RUNNING


def workflow_lane(reader: _Reader, row: dict[str, Any], envelope: dict[str, Any], key: str) -> dict[str, Any] | None:
    """One workflow's lane, or None when its steps clone another repository."""
    doc = envelope.get("workflow") or {}
    by_id = {str(t.get("id") or task_id_of(t)): t for t in envelope.get("tasks") or [] if isinstance(t, dict)}
    if by_id and key not in {repo_key(t.get("repository_url")) for t in by_id.values()}:
        return None
    steps = [s for s in doc.get("steps") or row.get("steps") or [] if isinstance(s, dict)]
    paired = [(s, by_id[str(s.get("task_id"))]) for s in steps if str(s.get("task_id")) in by_id]
    names = workflows.stored_names(envelope)
    wid = str(row.get("workflow_id"))
    label = names.get("title") or names.get("label") or wid
    state = doc.get("state") or row.get("state")
    finished = state in _TERMINAL
    # Its owner questions wait only while it is open: neither finished nor merged (#1812).
    asking = not finished and not any(s.get("runner_profile") == MERGE_PROFILE and t.get("state") == "SUCCEEDED"
                                      for s, t in paired)
    issue = next((n for n in (issue_of(t.get("input"), None) for _, t in paired) if n), None) or issue_of(None, label)
    titled = _run_title(names.get("title"), key)
    pr = None
    for _, task in paired:
        pr = ((reader.finished(task, questions=asking)["pull_request"] if task.get("state") in _TERMINAL else None)
              or pull_request_of(task))
        if pr:
            break
    started = [parse_time(t.get("started_at")) for _, t in paired]
    ended = [parse_time(t.get("completed_at")) for _, t in paired]
    lane: dict[str, Any] = {
        "id": wid, "kind": "workflow", "label": label, "state": _workflow_state(state, paired),
        "finished": finished, "pull_request": pr, "console": console_link(doc) or console_link(row),
        "created": _iso(row.get("created_at") or doc.get("created_at")),
        "started": _iso(min((s for s in started if s), default=None)),
        "ended": _iso(max((e for e in ended if e), default=None)) if finished else None,
        "issue": issue, "issue_title": titled[1] if titled and titled[0] == issue else None,
    }
    read = [(step.get("step_id"), task, reader.finished(task)) for step, task in paired
            if asking and task.get("state") in _TERMINAL]
    waiting = _questions(lane, read)
    for item in waiting:
        item["lane"] = wid
    for step, task in paired:
        waiting += _step_waits(reader, lane, steps, step, task)
    lane["waiting"] = waiting
    return lane


def _task_state(state: Any) -> str:
    if state in _QUEUED:
        return QUEUED
    return {"SUCCEEDED": SUCCEEDED, "FAILED": FAILED, "DEAD_LETTERED": FAILED,
            "CANCELLED": CANCELLED}.get(str(state), RUNNING)


def task_lane(reader: _Reader, task: dict[str, Any]) -> dict[str, Any]:
    """A single task's lane: a lane of its own (owner decision 1)."""
    meta = _mapping(task.get("metadata"))
    tid = str(task_id_of(task))
    label = workflows._flat(meta.get("title")) or workflows._flat(meta.get("unit")) or tid
    finished = task.get("state") in _TERMINAL
    # A finished single task is a finished lane: its questions wait on no one (#1812).
    done = reader.finished(task, questions=False) if finished else None
    lane: dict[str, Any] = {
        "id": tid, "kind": "task", "label": label, "state": _task_state(task.get("state")),
        "finished": finished, "pull_request": (done or {}).get("pull_request") or pull_request_of(task),
        "console": console_link(task), "created": _iso(task.get("created_at")),
        "started": _iso(task.get("started_at")), "ended": _iso(task.get("completed_at")) if finished else None,
        "issue": issue_of(task.get("input"), label), "issue_title": None,
    }
    waiting: list[dict[str, Any]] = []
    if task.get("state") == "PARKED" and task.get("park_reason") == "HUMAN_REQUIRED":
        waiting.append({"kind": "human_required", "who": "A person",
                        "item": f"{label} is parked until a person promotes it",
                        "since": reader.parked_since(task), "unlocks": f"the end of {label}",
                        "detail": "HUMAN_REQUIRED", "next": terminal_command(f"sc task {tid}")})
    for item in waiting:
        item["lane"] = tid
    lane["waiting"] = waiting
    return lane


# --------------------------------------------------------------------------
# Listings
# --------------------------------------------------------------------------


def _caller(client: SwarmClient) -> str:
    """The caller's verified email, which `submitted_by` holds (`/v1/tenants/me`)."""
    me = client.request("GET", "/v1/tenants/me")
    principal = _mapping(_mapping(me).get("principal"))
    email = principal.get("email") if principal else _mapping(me).get("email")
    if not isinstance(email, str) or not email.strip():
        raise SwarmError("GET /v1/tenants/me named no email, so the caller's own work cannot be told apart")
    return email.strip().lower()


def _workflow_rows(client: SwarmClient, key: str, horizon: datetime) -> list[dict[str, Any]]:
    """Every running workflow of the repository, every page; and the history,
    newest first, until a page reaches past `horizon`. A route older than
    CHECK-FILTER ignores the repository filter, so `workflow_lane` checks each
    workflow's steps' repository_url either way."""
    from .sc import ACTIVE_WORKFLOW_STATES

    rows: dict[str, dict[str, Any]] = {}
    for active in (True, False):
        token: str | None = None
        seen: set[str] = set()
        while True:
            page = client.workflows(states=ACTIVE_WORKFLOW_STATES if active else (), active=active,
                                    limit=PAGE_SIZE, page_token=token, repository=key)
            listed = [w for w in page["workflows"] if isinstance(w, dict) and w.get("workflow_id")]
            for row in listed:
                rows.setdefault(str(row["workflow_id"]), row)
            token = page.get("next_page_token") or None
            created = [parse_time(w.get("created_at")) for w in listed]
            if not active and created and all(c is not None and c < horizon for c in created):
                break
            if token is None or token in seen:
                break
            seen.add(token)
    return list(rows.values())


def _task_pages(client: SwarmClient, params: list[tuple[str, str]], horizon: datetime | None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    while True:
        query = urllib.parse.urlencode(params + ([("page_token", token)] if token else []))
        data = client.request("GET", f"/v1/tasks?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise SwarmError("GET /v1/tasks answered without a `tasks` list")
        listed = [t for t in data["tasks"] if isinstance(t, dict)]
        found += listed
        token = data.get("next_page_token") or None
        created = [parse_time(t.get("created_at")) for t in listed]
        if horizon is not None and created and all(c is not None and c < horizon for c in created):
            break
        if token is None or token in seen:
            break
        seen.add(token)
    return found


def _single_tasks(client: SwarmClient, key: str, horizon: datetime) -> list[dict[str, Any]]:
    """The caller's single tasks in this repository: every unfinished one, by
    state (`sc._live_states`), and the history newest first to `horizon`.
    `submitted_by=me` is the route's own filter; the repository and "in no
    workflow, from no parent" are checked here, the route has no filter for them."""
    from .sc import _live_states

    base = [("submitted_by", "me"), ("limit", str(TASK_PAGE_SIZE))]
    rows: dict[str, dict[str, Any]] = {}
    for state in _live_states():
        for task in _task_pages(client, [("state", state), *base], None):
            rows.setdefault(str(task_id_of(task)), task)
    for task in _task_pages(client, base, horizon):
        rows.setdefault(str(task_id_of(task)), task)
    return [t for tid, t in rows.items() if tid and tid != "None" and not t.get("workflow_id")
            and not t.get("parent_task_id") and repo_key(t.get("repository_url")) == key]


def _issue_titles(client: SwarmClient, key: str) -> tuple[dict[int, str], str | None]:
    """`({number: title}, why not read)` from one page of issue runs."""
    try:
        data = client.request("GET", f"/v1/runs?{urllib.parse.urlencode({'view': 'summary', 'limit': RUN_PAGE_SIZE})}")
    except SwarmError as exc:
        return {}, f"issue titles from the issue runs were not read ({exc}); those issues show their number"
    titles: dict[int, str] = {}
    for run in _mapping(data).get("runs") or []:
        issue = _mapping(_mapping(run).get("issue"))
        number = _int(issue.get("number"))
        title = _mapping(run.get("issue_read")).get("title")
        where = issue.get("repository_url") or (f"{issue.get('owner')}/{issue.get('repo')}" if issue.get("owner") else None)
        if number and isinstance(title, str) and title.strip() and repo_key(where) == key:
            titles.setdefault(number, " ".join(title.split()))
    return titles, None


# --------------------------------------------------------------------------
# Collect
# --------------------------------------------------------------------------


def _fresh_cache(cache: dict[str, Any], key: str) -> None:
    if cache.get("version") != CACHE_VERSION or cache.get("repository") != key:
        cache.clear()
        cache.update({"version": CACHE_VERSION, "repository": key})
    for part in ("tasks", "lanes"):
        if not isinstance(cache.get(part), dict):
            cache[part] = {}


def _counts(lanes: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "lanes": len(lanes),
        "done": sum(1 for lane in lanes if lane["state"] in DONE_STATES),
        "in_flight": sum(1 for lane in lanes if not lane["finished"] and lane["state"] != QUEUED),
        "queued": sum(1 for lane in lanes if lane["state"] == QUEUED),
    }


def _groups(lanes: list[dict[str, Any]], titles: dict[int, str]) -> list[dict[str, Any]]:
    grouped: dict[int | None, list[dict[str, Any]]] = {}
    for lane in lanes:
        grouped.setdefault(lane["issue"], []).append(lane)
    out = []
    for issue in sorted((i for i in grouped if i is not None), reverse=True) + ([None] if None in grouped else []):
        members = grouped[issue]
        counts = _counts(members)
        out.append({"issue": issue, "title": NO_ISSUE if issue is None else titles.get(issue),
                    "lanes": [lane["id"] for lane in members], "done": counts["done"], "total": len(members),
                    "in_flight": counts["in_flight"], "queued": counts["queued"]})
    return out


def collect(client: SwarmClient, *, repository: str, now: datetime, cache: dict[str, Any]) -> dict[str, Any]:
    """Everything the page draws, for the project whose origin is `repository`.

    READ-ONLY: GETs only. Raises SwarmError when a listing, the caller or a
    workflow cannot be read -- the caller keeps the previous page then. A
    missing issue title is a note, never a failure. `cache` is updated with
    what never changes once finished, and pruned past the window."""
    key = repo_key(repository)
    if key is None:
        raise SwarmError(f"{repository!r} names no repository, so there is no project to build a board for")
    _fresh_cache(cache, key)
    cutoff = now - timedelta(days=HISTORY_DAYS)
    horizon = cutoff - timedelta(days=LISTING_MARGIN_DAYS)
    reader = _Reader(client, cache, now)
    me = _caller(client)
    rows = _workflow_rows(client, key, horizon)
    mine = [r for r in rows if str(r.get("submitted_by") or "").strip().lower() == me]

    lanes: list[dict[str, Any]] = []
    to_read: list[dict[str, Any]] = []
    for row in mine:
        wid = str(row["workflow_id"])
        created = parse_time(row.get("created_at"))
        if row.get("state") in _TERMINAL:
            if wid in cache["lanes"]:
                lanes.append(cache["lanes"][wid])
                continue
            if created is not None and created < horizon:
                continue
        to_read.append(row)

    def fetch(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return row, workflows.fetch(client, str(row["workflow_id"]))

    with ThreadPoolExecutor(max_workers=max(1, min(DETAIL_CONCURRENCY, len(to_read)))) as pool:
        envelopes = list(pool.map(fetch, to_read))
    for row, envelope in envelopes:
        lane = workflow_lane(reader, row, envelope, key)
        if lane is not None:
            lanes.append(lane)

    for task in _single_tasks(client, key, horizon):
        tid = str(task_id_of(task))
        lanes.append(cache["lanes"][tid] if tid in cache["lanes"] and task.get("state") in _TERMINAL
                     else task_lane(reader, task))

    lanes = [lane for lane in lanes
             if not lane["finished"] or (parse_time(lane.get("ended") or lane.get("created")) or now) >= cutoff]
    # Only a finished lane is cached, and a finished lane reads no questions
    # (#1812), so none holds a failed read: an open lane's is retried because
    # `_Reader.finished` never caches one.
    cache["lanes"] = {lane["id"]: lane for lane in lanes if lane["finished"]}
    cache["tasks"] = {tid: entry for tid, entry in cache["tasks"].items()
                      if (parse_time(entry.get("ended")) or now) >= cutoff}

    titles, why = _issue_titles(client, key)
    for lane in lanes:
        if lane["issue"] and lane.get("issue_title"):
            titles.setdefault(lane["issue"], lane["issue_title"])
    lanes.sort(key=lambda lane: (lane["finished"], -(parse_time(lane.get("started") or lane.get("created"))
                                                     or now).timestamp(), lane["id"]))
    waiting = sorted((item for lane in lanes for item in lane["waiting"]),
                     key=lambda item: (parse_time(item.get("since")) or now, item["lane"], item["item"]))
    counts = {**_counts(lanes), "needs_you": len(waiting)}
    return {"repository": key, "lanes": lanes, "groups": _groups(lanes, titles), "waiting": waiting,
            "counts": counts, "notes": [why] if why else []}


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url: Any, text: str) -> str:
    """`<a>` to an http(s) URL as served, else the text alone: never a `javascript:` link."""
    if isinstance(url, str) and urllib.parse.urlsplit(url).scheme in ("http", "https"):
        return f'<a href="{_e(url)}" rel="noopener noreferrer">{text}</a>'
    return text


def _time(value: Any) -> str:
    at = _iso(value)
    if not at:
        return '<span class="muted">—</span>'
    return f'<time datetime="{_e(at)}" title="{_e(_clock(at))}">{_e(_clock(at))}</time>'


_CSS = """
:root {
  --bg: #f7f7f8; --card: #ffffff; --text: #1d1d22; --muted: #6b6b76; --line: #e3e3e8;
  --accent: #3b5bdb; --bar: #e9ecf5; --bar-fill: #2f9e44;
  --queued: #868e96; --running: #1c7ed6; --ci: #f08c00; --refused: #e03131;
  --done: #2f9e44; --failed: #c92a2a; --cancelled: #5c5f66; --banner: #fff3bf; --banner-text: #5c3c00;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141417; --card: #1e1e23; --text: #e8e8ee; --muted: #9a9aa6; --line: #33333b;
    --accent: #91a7ff; --bar: #2b2f3a; --bar-fill: #51cf66;
    --queued: #adb5bd; --running: #4dabf7; --ci: #ffa94d; --refused: #ff6b6b;
    --done: #51cf66; --failed: #ff8787; --cancelled: #909296; --banner: #3d3200; --banner-text: #ffe8a1;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; overflow-x: hidden; }
body { background: var(--bg); color: var(--text); font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 16px 12px 40px; }
h1 { font-size: 20px; margin: 0 0 4px; overflow-wrap: anywhere; }
h2 { font-size: 16px; margin: 0 0 10px; }
a { color: var(--accent); }
.muted, .written { color: var(--muted); }
.banner { background: var(--banner); color: var(--banner-text); border-radius: 8px; padding: 10px 12px; margin: 12px 0; overflow-wrap: anywhere; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 8px; margin: 14px 0; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; }
.tile-n { font-size: 22px; }
.tile-label { color: var(--muted); font-size: 12px; }
.card { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; margin: 14px 0; max-width: 100%; }
table { border-collapse: collapse; width: 100%; min-width: 560px; }
th, td { text-align: left; vertical-align: top; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: 12px; white-space: nowrap; }
td { overflow-wrap: anywhere; }
td.lane { min-width: 220px; }
code { font-size: 12px; white-space: nowrap; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; color: #fff; white-space: nowrap; }
.pill.queued { background: var(--queued); } .pill.running { background: var(--running); }
.pill.ci { background: var(--ci); } .pill.refused { background: var(--refused); }
.pill.done { background: var(--done); } .pill.failed { background: var(--failed); }
.pill.cancelled { background: var(--cancelled); }
.bar { background: var(--bar); border-radius: 4px; height: 8px; min-width: 80px; margin-top: 4px; }
.bar > span { display: block; height: 8px; border-radius: 4px; background: var(--bar-fill); }
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 8px; }
.controls input[type=search] { flex: 1 1 180px; min-width: 0; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--bg); color: var(--text); }
ul.options { margin: 4px 0 0; padding-left: 18px; }
.empty { color: var(--muted); }
"""

_JS = """
(function () {
  function local(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString();
  }
  var written = document.getElementById('written');
  if (written) { written.textContent = local(written.getAttribute('datetime')); }
  var times = document.querySelectorAll('td time[datetime]');
  for (var i = 0; i < times.length; i++) { times[i].textContent = local(times[i].getAttribute('datetime')); }
  var filter = document.getElementById('lane-filter');
  var hide = document.getElementById('hide-finished');
  function apply() {
    var needle = (filter.value || '').toLowerCase();
    var rows = document.querySelectorAll('#lanes tbody tr');
    for (var j = 0; j < rows.length; j++) {
      var row = rows[j];
      var hidden = (hide.checked && row.getAttribute('data-finished') === '1') ||
        (needle && row.textContent.toLowerCase().indexOf(needle) < 0);
      row.hidden = hidden;
    }
  }
  filter.addEventListener('input', apply);
  hide.addEventListener('change', apply);
  apply();
})();
"""


def _waiting_rows(model: dict[str, Any], labels: dict[str, str]) -> str:
    if not model["waiting"]:
        return '<p class="empty">Nothing is waiting on anyone.</p>'
    rows = []
    for item in model["waiting"]:
        options = "".join(f"<li>{_e(o['label'])}{' ★' if o['recommended'] else ''}</li>"
                          for o in item.get("options") or [])
        detail = _e(item.get("detail"))
        if options:
            detail += f'<ul class="options">{options}</ul>'
        if item.get("next"):
            detail += f"<div><code>{_e(item['next'])}</code></div>"
        rows.append(
            f"<tr><td>{_e(item['who'])}</td>"
            f"<td>{_e(item['item'])}<div class=\"muted\">{_e(labels.get(item['lane'], item['lane']))}</div></td>"
            f"<td>{_time(item.get('since'))}</td><td>{_e(item.get('unlocks'))}</td><td>{detail}</td></tr>")
    return ("<table><thead><tr><th>Who</th><th>Item</th><th>Since</th><th>What it unlocks</th>"
            "<th>Detail / next command</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _feature_rows(model: dict[str, Any]) -> str:
    if not model["groups"]:
        return '<p class="empty">No lanes yet.</p>'
    rows = []
    for group in model["groups"]:
        issue = group["issue"]
        feature = group["title"] or (f"#{issue}" if issue else NO_ISSUE)
        share = int(round(100 * group["done"] / group["total"])) if group["total"] else 0
        rows.append(
            f"<tr><td>{_e(feature)}</td><td>{_e(f'#{issue}') if issue else '—'}</td>"
            f"<td>{group['done']}/{group['total']}<div class=\"bar\"><span style=\"width:{share}%\"></span></div></td>"
            f"<td>{group['in_flight']}</td><td>{group['queued']}</td></tr>")
    return ("<table><thead><tr><th>Feature</th><th>Issue</th><th>Done/Total</th><th>In flight</th>"
            "<th>Queued</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _lane_rows(model: dict[str, Any], origin: str, features: dict[Any, str]) -> str:
    rows = []
    for lane in model["lanes"]:
        pr = lane.get("pull_request")
        pr_cell = _link(pull_request_link(origin, pr), f"#{pr}") if pr else "—"
        rows.append(
            f"<tr data-finished=\"{1 if lane['finished'] else 0}\">"
            f"<td class=\"lane\">{_link(lane.get('console'), _e(lane['label']))}"
            f"<div class=\"muted\"><code>{_e(lane['id'])}</code></div></td>"
            f"<td>{_e(features.get(lane['issue'], NO_ISSUE))}</td>"
            f"<td><span class=\"pill {_PILL.get(lane['state'], 'queued')}\">{_e(lane['state'])}</span></td>"
            f"<td>{pr_cell}</td><td>{_time(lane.get('started'))}</td><td>{_time(lane.get('ended'))}</td></tr>")
    body = "".join(rows) or '<tr><td colspan="6" class="empty">No lanes yet.</td></tr>'
    return ("<table id=\"lanes\"><thead><tr><th>Lane</th><th>Feature</th><th>State</th><th>PR</th>"
            "<th>Started</th><th>Ended</th></tr></thead><tbody>" + body + "</tbody></table>")


def render(model: dict[str, Any], *, project_name: str, origin: str, written_at: datetime,
           banner: str | None = None) -> str:
    """The whole page: one self-contained HTML document, every string escaped."""
    title = f"SwarmCloud build board · {project_name}"
    stamp = _iso(written_at) or ""
    counts = model["counts"]
    labels = {lane["id"]: lane["label"] for lane in model["lanes"]}
    features = {g["issue"]: (g["title"] or f"#{g['issue']}") if g["issue"] else NO_ISSUE for g in model["groups"]}
    tiles = "".join(
        f'<div class="tile"><b class="tile-n">{n}</b><span class="tile-label">{label}</span></div>'
        for n, label in ((counts["lanes"], "lanes"), (counts["done"], "merged/done"),
                         (counts["in_flight"], "in flight"), (counts["queued"], "queued"),
                         (counts["needs_you"], "needs you")))
    notes = "".join(f'<p class="muted">{_e(n)}</p>' for n in model.get("notes") or [])
    banner_html = f'<div class="banner" role="status">{_e(banner)}</div>' if banner else ""
    utc = (parse_time(stamp) or written_at).strftime("%H:%M UTC")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"color-scheme\" content=\"light dark\">"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{_e(title)}</h1>"
        f"<div class=\"written\">written <time id=\"written\" datetime=\"{_e(stamp)}\">{_e(_clock(stamp))}</time>"
        f" · {_e(utc)} · <code>{_e(model.get('repository'))}</code></div>"
        f"{banner_html}{notes}<div class=\"tiles\">{tiles}</div>"
        f"<section class=\"card\"><h2>Waiting on someone</h2>{_waiting_rows(model, labels)}</section>"
        f"<section class=\"card\"><h2>By feature</h2>{_feature_rows(model)}</section>"
        "<section class=\"card\"><h2>Every lane</h2><div class=\"controls\">"
        "<input type=\"search\" id=\"lane-filter\" placeholder=\"Filter lanes\" aria-label=\"Filter lanes\">"
        "<label><input type=\"checkbox\" id=\"hide-finished\" checked> hide finished</label></div>"
        f"{_lane_rows(model, origin, features)}</section>"
        f"</main><script>{_JS}</script></body></html>\n"
    )


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def folder_of(project: Path) -> Path:
    return Path(project) / BOARD_DIR


def _prepared(folder: Path) -> None:
    """The folder, with its `.gitignore` of `*`: git ignores everything in it,
    and the repository's own `.gitignore` is never touched."""
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != "*\n":
        _atomic(ignore, "*\n")


def _atomic(path: Path, text: str) -> None:
    """Write a temp file beside `path`, then rename it over `path`: a reader
    sees the old file or the new one, never half of either."""
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def write(project: Path, page: str) -> Path:
    """`<project>/.claude/build-board/index.html`, atomically."""
    folder = folder_of(project)
    _prepared(folder)
    path = folder / PAGE_NAME
    _atomic(path, page)
    return path


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# Refresh, and opening the page
# --------------------------------------------------------------------------


def _project() -> Path | None:
    """The session's project directory, as `launch` resolves it."""
    from .launch import _project as project_of

    found = project_of()
    return Path(found[0]) if found else None


def _origin(project: Path) -> str | None:
    got = checkout._git(project, "remote", "get-url", "origin")
    url = checkout._out(got) if got.returncode == 0 else ""
    return url if repo_key(url) else None


def _banner(failed: dict[str, Any]) -> str | None:
    if not failed.get("why"):
        return None
    return (f"The refresh at {_clock(failed.get('at'))} failed: {failed.get('why')}. "
            "This page is from the next refresh, which succeeded.")


def refresh(client: SwarmClient, *, project: Path | None = None) -> dict[str, Any]:
    """Read, render and write the board: `{path, written_at, counts, waiting}`,
    or `{error, path}` -- `path` the page left in place, or None."""
    project = project or _project()
    if project is None:
        return {"error": ("no project directory: neither CLAUDE_PROJECT_DIR nor a git work tree "
                          "around this process names one, so there is no board to write"), "path": None}
    folder = folder_of(project)
    page = folder / PAGE_NAME
    kept = str(page) if page.is_file() else None
    origin = _origin(project)
    if origin is None:
        return {"error": (f"{project} has no `origin` remote naming a repository; the board shows "
                          "the work submitted against the project's repository"), "path": kept}
    now = _now()
    cache = _load(folder / CACHE_NAME)
    try:
        model = collect(client, repository=origin, now=now, cache=cache)
        page_text = render(model, project_name=project.name, origin=origin, written_at=now,
                           banner=_banner(_load(folder / FAILURE_NAME)))
        path = write(project, page_text)
        _atomic(folder / CACHE_NAME, json.dumps(cache, sort_keys=True))
    except (SwarmError, OSError, KeyError, TypeError, ValueError) as exc:
        # A malformed listing (KeyError, TypeError) or a failed write (OSError)
        # leaves the previous page in place exactly as a failed read does.
        why = str(exc) if isinstance(exc, (SwarmError, OSError)) else f"a malformed answer ({type(exc).__name__}: {exc})"
        try:
            _prepared(folder)
            _atomic(folder / FAILURE_NAME, json.dumps({"at": _iso(now), "why": why}))
        except OSError:
            pass
        return {"error": f"the build board was not refreshed: {why}. The previous page stays", "path": kept}
    (folder / FAILURE_NAME).unlink(missing_ok=True)
    return {"path": str(path), "written_at": _iso(now), "counts": model["counts"], "waiting": len(model["waiting"])}


def open_page(path: str | Path) -> str | None:
    """Open the page in the default browser: `open` on macOS, `xdg-open`
    elsewhere. None when it opened; else why not."""
    argv = ["open" if sys.platform == "darwin" else "xdg-open", str(path)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{argv[0]} could not be run ({type(exc).__name__}); open {path} yourself"
    if done.returncode != 0:
        return f"{argv[0]} exited {done.returncode}; open {path} yourself"
    return None


# --------------------------------------------------------------------------
# Keeping itself current (BOARD-WIRE, part of #1773)
# --------------------------------------------------------------------------

#: A row's follow that sees a state change refreshes the board at most this
#: often per project. Rows follow every task of every running workflow, and a
#: busy project changes state many times a minute; each refresh is a listing
#: plus one read per moving lane, so a refresh per change would spend the
#: bridge's 20/s on the board. A change inside the window is written when the
#: window closes, never dropped: the last change of a run is the one a reader
#: most wants to see.
FOLLOW_REFRESH_SECONDS = 120.0

#: The tools after whose SUCCESS the board is refreshed unthrottled (owner
#: requirement: "updated automatically when work is dispatched"): every
#: submission (`swarm_workflow`, `merge_pr` included; `swarm_dispatch`, one
#: task or `tasks`; `swarm_run_issue`, whose planner is work submitted), the
#: launch that submits or attaches, and `swarm_check`. A refused call raises
#: before this is consulted, so it refreshes nothing.
ALWAYS_AFTER = frozenset({"swarm_workflow", "swarm_dispatch", "swarm_workflow_launch",
                          "swarm_run_issue", "swarm_check"})


def _log(message: str) -> None:
    """Into the bridge's stderr, which the host keeps in its MCP log and
    never shows as a tool's answer."""
    print(f"swarm-mcp: build board: {message}", file=sys.stderr)


def _refresh_project(client: Any, project: Path) -> dict[str, Any]:
    return refresh(client, project=project)


def _thread(run: Callable[[], None]) -> None:
    threading.Thread(target=run, name="build-board", daemon=True).start()


def _timer(seconds: float, run: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, run)
    timer.daemon = True
    timer.start()


class AutoRefresh:
    """The board's automatic refresh: in the background, coalesced, throttled
    only for follows, and never able to fail or delay the tool behind it.

    ONE REFRESH AT A TIME. A trigger while one runs is remembered, and however
    many arrive they cost ONE more refresh once it ends -- the running one may
    have read before the submission that triggered them. A submission's
    refresh (`submitted`) is never throttled; a follow's (`followed`) runs only
    when the project's last refresh, of either kind, is `FOLLOW_REFRESH_SECONDS`
    old, else once at the end of that window, unless a refresh started since
    the deferral covers it. A daemon thread does the work: a refresh cut short
    when the bridge exits leaves the previous page, because the page is
    replaced by a rename (`write`).

    Every failure -- a raise, or a refresh that answered `error` -- is logged
    and forgotten; the next trigger tries again.
    """

    def __init__(
        self,
        *,
        refresh: Callable[[Any, Path], dict[str, Any]] | None = None,
        project: Callable[[], Path | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        start: Callable[[Callable[[], None]], None] = _thread,
        after: Callable[[float, Callable[[], None]], None] = _timer,
    ) -> None:
        self._refresh = refresh or _refresh_project
        self._project = project or (lambda: _project())
        self._clock = clock
        self._start = start
        self._after = after
        self._lock = threading.Lock()
        self._busy = False
        #: 0 nothing pending, 1 a follow's, 2 a submission's: the stronger wins.
        self._pending = 0
        self._client: Any = None
        self._last: dict[str, float] = {}
        self._deferred: dict[str, float] = {}
        self._states: dict[str, str] = {}

    def submitted(self, client: Any) -> None:
        self._request(client, 2)

    def followed(self, client: Any, reply: str) -> None:
        """A follow's reply: a refresh, throttled, when a task's state moved
        since this bridge last saw it, or when the reply names a transition
        itself (`compact.watch_progress`). A task seen for the first time is
        no change: the submission's own refresh already showed it."""
        try:
            found = json.loads(reply)
        except (TypeError, ValueError):
            return
        if not isinstance(found, dict):
            return
        changed = bool(found.get("transitions"))
        tasks = found.get("tasks")
        with self._lock:
            for task in tasks if isinstance(tasks, list) else []:
                if not isinstance(task, dict) or not isinstance(task.get("state"), str):
                    continue
                task_id = str(task.get("task_id") or "")
                before = self._states.get(task_id)
                if before is not None and before != task["state"]:
                    changed = True
                self._states[task_id] = task["state"]
        if changed:
            self._request(client, 1)

    def _request(self, client: Any, level: int) -> None:
        with self._lock:
            self._client = client
            self._pending = max(self._pending, level)
            if self._busy:
                return
            self._busy = True
        try:
            self._start(self._drain)
        except Exception as exc:  # noqa: BLE001 - no thread is no refresh, never a failed tool
            with self._lock:
                self._busy = False
            _log(f"the refresh could not start ({type(exc).__name__}: {exc})")

    def _drain(self) -> None:
        while True:
            with self._lock:
                level, self._pending = self._pending, 0
                client = self._client
                if level == 0:
                    self._busy = False
                    return
            try:
                self._once(client, forced=level == 2)
            except Exception as exc:  # noqa: BLE001 - logged and forgotten; the next trigger tries again
                _log(f"the refresh failed ({type(exc).__name__}: {exc})")

    def _once(self, client: Any, *, forced: bool) -> None:
        project = self._project()
        if project is None:
            _log("no project directory, so no board to refresh")
            return
        key = str(project)
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if not forced and last is not None and now - last < FOLLOW_REFRESH_SECONDS:
                if key not in self._deferred:
                    self._deferred[key] = now
                    self._after(FOLLOW_REFRESH_SECONDS - (now - last), lambda: self._fire(client, key))
                return
            self._last[key] = now
        got = self._refresh(client, project)
        if isinstance(got, dict) and got.get("error"):
            _log(str(got["error"]))

    def _fire(self, client: Any, key: str) -> None:
        """The end of a follow's window: refresh, unless one started after
        the change was deferred and so already shows it."""
        with self._lock:
            deferred = self._deferred.pop(key, None)
            last = self._last.get(key)
            if deferred is None or (last is not None and last > deferred):
                return
        self._request(client, 1)


#: The bridge's one refresher: one per process, as the bridge serves one project.
AUTO = AutoRefresh()


def after_tool(client: Any, name: str, reply: str) -> None:
    """What a tool's successful answer does to the board: called by the
    bridge after every tool call that did not raise."""
    if name in ALWAYS_AFTER:
        AUTO.submitted(client)
    elif name == "swarm_follow":
        AUTO.followed(client, reply)
