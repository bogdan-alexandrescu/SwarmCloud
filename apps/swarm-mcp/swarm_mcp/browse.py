"""`swarm_browse`: the four compact reads the sc plugin's SwarmCloud pane
makes, one MCP call per click (SC-BROWSER-BUILD, #2318; BGD picked proposal
B, Two columns, on 2026-10-25, PR 2328).

THE PANE SPENDS NO MODEL TOKENS. plugin/hooks/pane.tsx calls this tool
directly, over the engine's own connection (`$.mcp.call`), when the person
opens the pane, opens something in it, or presses Refresh -- never on a
timer. So each level is ONE read shaped for exactly what that level draws,
and nothing else: a fatter answer costs nobody tokens but makes every click
slower, and the existing tools were each shaped for a model reader.

* `workflows` (Level 1): this Claude Code session's workflows -- finished
  ones too, so a workflow that ended while nobody looked is still listed --
  each with its state as the server derived it, its steps (id, parents,
  state: the pane groups them into the console's stages itself, with
  `levelsOf`), its duration, tokens and cost; and this session's running
  single tasks. `swarm_workflows` lists the whole tenant's RUNNING
  workflows with no tokens and no parents, which is not this level. With
  `project: true` (SC-BROWSER-FOLLOWUP, BGD 2026-10-25: session start tracks
  the project's running work), also every workflow and single task still
  RUNNING that clones this checkout's origin repository, whichever session
  started it, marked `this_session: false`; the pane asks for it after
  session start, and nothing else does.
* `stages` (Level 2): one workflow, read fresh, in the same row shape.
* `agents` (Level 3): one stage's tasks: name, state and a one-line note.
* `agent` (Level 4): one task: state, attempts, duration, cost, tokens,
  last error, pull request, artifact names, console link.

HONEST FIGURES. A figure nobody recorded is null with a `*_because`, never 0
(a mock task costs nothing on purpose; an unparsed run costs an unknown
amount). Tokens are every kind reported -- input, output, cache reads and
writes -- summed, as the console's Tokens cell sums them (#322); a
workflow's are its FINISHED steps' (`result_summary.runner.usage`), the
console's borrowing rule (`finishedResultOf`): an unfinished task's result
summary is an earlier attempt's.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from . import session, workflows
from .client import TERMINAL, SwarmClient, SwarmError
from .follow import console_link
from .patches import describe_task
from .progress import _artifacts, _derived_totals, _duration, clip, questions_count
from .render import parse_time

LEVELS = ("workflows", "stages", "agents", "agent")
#: This session's workflows, newest first: one page. A session that started
#: more than this many is told so (`more_because`), never silently cut.
SESSION_PAGE = 50
#: Detail reads at once, as `sc.running_workflows` reads them.
DETAIL_CONCURRENCY = 8
#: A stage's tasks read at once.
TASK_CONCURRENCY = 8
#: The most task ids one `agents` read takes: the widest stage the console draws.
MAX_TASKS = 200
TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
NOT_RECORDED = "not recorded"


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _money(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_tokens(task: dict[str, Any]) -> int | None:
    """Every token kind a task's result summary reported, summed; None when it reported none."""
    runner = (task.get("result_summary") or {}).get("runner") if isinstance(task.get("result_summary"), dict) else None
    usage = runner.get("usage") if isinstance(runner, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = [_count(usage.get(kind)) for kind in TOKEN_KINDS]
    reported = [c for c in counts if c is not None]
    return sum(reported) if reported else None


def attempt_tokens(attempts: list[dict[str, Any]]) -> int | None:
    """Every token kind every attempt recorded, summed; None when none recorded any."""
    reported = [c for a in attempts for c in (_count(a.get(kind)) for kind in TOKEN_KINDS) if c is not None]
    return sum(reported) if reported else None


def workflow_row(envelope: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One workflow as Levels 1 and 2 draw it."""
    workflow = envelope["workflow"]
    names = workflows.stored_names(envelope)
    tasks = {str(t.get("id")): t for t in envelope.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
    derived = workflows.state_of(workflow)
    steps = []
    for row in workflows.step_rows(envelope):
        step: dict[str, Any] = {"step_id": row.get("step_id"), "task_id": row.get("task_id"),
                                "depends_on": list(row.get("depends_on") or []), "state": row.get("state")}
        if row.get("park_reason"):
            step["park_reason"] = row["park_reason"]
        phase = _park_phase(tasks.get(str(row.get("task_id"))))
        if phase:
            step["park_phase"] = phase
        steps.append(step)
    out: dict[str, Any] = {
        "workflow_id": workflow.get("workflow_id"),
        "name": workflows.title_name(title=names["title"], label=names["label"],
                                     workflow_id=workflow.get("workflow_id")),
        "state": derived.get("state"),
        "steps": steps,
    }
    if derived.get("state") is None:
        out["state_because"] = clip(derived.get("state_unavailable_because") or "not derived", 200)
    finished = [tasks[s["task_id"]] for s in steps if s.get("task_id") in tasks and s.get("state") in TERMINAL]
    figures = [usage_tokens(t) for t in finished]
    reported = [f for f in figures if f is not None]
    out["tokens"] = sum(reported) if reported else None
    if out["tokens"] is None:
        out["tokens_because"] = "no finished step reported tokens yet" if not finished else "no step reported tokens"
    elif len(finished) < len(steps):
        out["tokens_note"] = "finished steps only"
    costs = [_money(t.get("cost_usd_total")) for t in tasks.values()]
    out["cost_usd"] = round(sum(c for c in costs if c is not None), 2) if any(c is not None for c in costs) else None
    created = parse_time(workflow.get("created_at"))
    if all(s.get("state") in TERMINAL for s in steps) and steps:
        ends = [parse_time(tasks[s["task_id"]].get("completed_at")) for s in steps if s.get("task_id") in tasks]
        ended = max((e for e in ends if e is not None), default=None)
    else:
        ended = now
    out["duration_s"] = int((ended - created).total_seconds()) if created and ended and ended >= created else None
    if out["duration_s"] is None:
        out["duration_because"] = NOT_RECORDED
    # SC-BAND: what the band counts and notices -- the pull requests its
    # succeeded steps opened, and the owner questions its steps' agents asked
    # (the worker's count in each task's result summary; no artifact read).
    pulls: list[str] = []
    for step in steps:
        task = tasks.get(str(step.get("task_id")))
        if task and step.get("state") == "SUCCEEDED":
            pull = describe_task(task).get("pull_request")
            if pull and pull not in pulls:
                pulls.append(pull)
    out["pull_requests"] = pulls
    out["owner_questions"] = sum(questions_count(task) for task in tasks.values())
    link = console_link(workflow)
    if link is not None:
        out["console"] = link
    return out


def _park_phase(task: dict[str, Any] | None) -> str | None:
    """Why a HUMAN_REQUIRED task holds, as the API serves it (`blocked_by[].park_phase`,
    else `waiting.detail.park_phase`): `owner_questions` is a merge held by them.
    An AWAITING_INPUT park (a control_plane step waiting for a member's answer)
    is never a merge held on owner questions, so it has no park phase here."""
    if not task or task.get("park_reason") != "HUMAN_REQUIRED":
        return None
    for block in task.get("blocked_by") or []:
        if isinstance(block, dict) and isinstance(block.get("park_phase"), str):
            return block["park_phase"]
    waiting = task.get("waiting") if isinstance(task.get("waiting"), dict) else {}
    detail = waiting.get("detail") if isinstance(waiting.get("detail"), dict) else {}
    return detail.get("park_phase") if isinstance(detail.get("park_phase"), str) else None


def _owned(envelope: dict[str, Any], sid: str) -> bool:
    owner = session.owner_of(envelope["workflow"]) or session.owner_of_tasks(envelope.get("tasks") or [])
    return session.ownership(owner, sid) == "own"


def _clones(envelope: dict[str, Any], repository: str) -> bool:
    """Does any of the workflow's step tasks clone `repository` (`host/owner/repo`)?"""
    from .check import repo_key

    return any(isinstance(t, dict) and repo_key(t.get("repository_url")) == repository
               for t in envelope.get("tasks") or [])


def workflows_level(client: SwarmClient, sid: str | None, now: datetime | None = None,
                    project: bool = False) -> dict[str, Any]:
    """Level 1: this session's workflows and running single tasks -- and with
    `project`, this project's running ones whichever session started them."""
    from .check import repo_key
    from .sc import mark_session, project_key, running_tasks, walk_running_workflows

    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"level": "workflows", "read_at": _stamp(now), "session": session.describe(sid)}
    repository = project_key() if project else None
    if project:
        out["project"] = {"repository": repository}
        if repository is None:
            out["project"]["note"] = ("this checkout has no origin remote naming a repository, so only "
                                      "this session's work is listed")
    if not sid and repository is None:
        out.update(workflows=[], single_tasks=[],
                   scope_because="this Claude Code session's id is not known to the bridge, so its work "
                                 "cannot be told from anyone else's; /sc workflows lists the tenant's")
        return out
    ids: list[str] = []
    if sid:
        page = client.workflows(claude_session=sid, limit=SESSION_PAGE)
        ids = [str(w["workflow_id"]) for w in page["workflows"] if isinstance(w, dict) and w.get("workflow_id")]
        if page.get("next_page_token"):
            out["more_because"] = f"this session started more than {SESSION_PAGE} workflows; the newest are listed"
    theirs: list[str] = []
    if repository is not None:
        # The project's RUNNING workflows, asked of the API by repository and
        # checked again below: a route older than the filter ignores it.
        try:
            walk = walk_running_workflows(client, repository=repository)
        except SwarmError as exc:
            out["project"]["error"] = clip(str(exc), 200)
        else:
            theirs = [str(w["workflow_id"]) for w in walk["workflows"]
                      if w.get("workflow_id") and str(w["workflow_id"]) not in ids]
            if walk.get("incomplete_because"):
                out["project"]["incomplete_because"] = walk["incomplete_because"]

    def detail(item: tuple[str, bool]) -> dict[str, Any] | None:
        workflow_id, listed_as_mine = item
        try:
            envelope = workflows.fetch(client, workflow_id)
        except SwarmError as exc:
            return {"workflow_id": workflow_id, "name": workflow_id, "state": None, "steps": [],
                    "read_error": clip(str(exc), 200)} if listed_as_mine else None
        # Checked AGAIN: a route older than the session filter ignores it.
        if sid and _owned(envelope, sid):
            return workflow_row(envelope, now)
        if repository is not None and _clones(envelope, repository):
            row = workflow_row(envelope, now)
            if row.get("state") not in TERMINAL:
                return {**row, "this_session": False}
        return None

    items = [(i, True) for i in ids] + [(i, False) for i in theirs]
    rows: list[dict[str, Any] | None] = []
    if items:
        with ThreadPoolExecutor(max_workers=min(DETAIL_CONCURRENCY, len(items))) as pool:
            rows = list(pool.map(detail, items))
    out["workflows"] = [row for row in rows if row is not None]

    try:
        singles = running_tasks(client, now=now)
    except SwarmError as exc:
        out["single_tasks"] = []
        out["single_tasks_error"] = clip(str(exc), 200)
    else:
        kept = [t for t in mark_session(singles["tasks"], sid)
                if t.get("this_session")
                or (repository is not None and repo_key(t.get("repository_url")) == repository)]
        out["single_tasks"] = [
            {key: t.get(key) for key in ("task_id", "label", "state", "age_seconds", "console", "this_session")
             if key in t}
            for t in kept
        ]
    return out


def stages_level(client: SwarmClient, workflow_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Level 2: one workflow, read fresh."""
    now = now or datetime.now(timezone.utc)
    return {"level": "stages", "read_at": _stamp(now),
            "workflow": workflow_row(workflows.fetch(client, workflow_id), now)}


def _task_note(task: dict[str, Any]) -> str:
    """The one line Level 3 shows after an agent's state."""
    from .compact import waits_for

    state = task.get("state")
    if state == "SUCCEEDED":
        pr = describe_task(task).get("pull_request")
        return f"PR {_pr_number(pr)}" if pr else "succeeded"
    if state in TERMINAL:
        return clip(" ".join(str(task.get("last_error") or state.lower()).split()), 60)
    if state == "PARKED" and task.get("park_reason") in ("PROVIDER_QUOTA_EXHAUSTED", "PROVIDER_COOLDOWN",
                                                         "DEPENDENCY_INCOMPLETE", "CI_PENDING"):
        return f"{waits_for(task)}, holds nothing"
    if state in ("LEASED", "DISPATCHED", "STARTING", "RUNNING"):
        count, cap = _count(task.get("attempt_count")), _count(task.get("max_attempts"))
        return f"attempt {count} of {cap}" if count and cap else "running"
    return f"waits: {waits_for(task)}"


def _pr_number(url: Any) -> str:
    text = str(url)
    at = text.rfind("/pull/")
    return "#" + text[at + 6:].split("/")[0] if at >= 0 else text


def agents_level(client: SwarmClient, task_ids: list[str], now: datetime | None = None) -> dict[str, Any]:
    """Level 3: one stage's tasks, each its name, state and note."""
    now = now or datetime.now(timezone.utc)
    if not task_ids:
        raise SwarmError("`task_ids` names the stage's tasks: at least one. Nothing was read")
    if len(task_ids) > MAX_TASKS:
        raise SwarmError(f"`task_ids` holds {len(task_ids)}; one stage read takes at most {MAX_TASKS}")

    def one(task_id: str) -> dict[str, Any]:
        try:
            task = client.task(task_id)
        except SwarmError as exc:
            return {"task_id": task_id, "state": None, "note": clip(f"not read: {exc}", 80)}
        row = {"task_id": task_id, "step_id": task.get("step_id"), "state": task.get("state"),
               "note": _task_note(task)}
        if task.get("park_reason"):
            row["park_reason"] = task["park_reason"]
        return row

    with ThreadPoolExecutor(max_workers=min(TASK_CONCURRENCY, len(task_ids))) as pool:
        agents = list(pool.map(one, [str(t) for t in task_ids]))
    return {"level": "agents", "read_at": _stamp(now), "agents": agents}


def agent_level(client: SwarmClient, task_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Level 4: one task's details."""
    now = now or datetime.now(timezone.utc)
    task = client.task(task_id)
    state = task.get("state")
    try:
        attempts = client.attempts(task_id)
        attempts_error = None
    except SwarmError as exc:
        attempts, attempts_error = [], clip(str(exc), 160)
    out: dict[str, Any] = {
        "level": "agent", "read_at": _stamp(now), "task_id": task_id,
        "step_id": task.get("step_id"), "workflow_id": task.get("workflow_id"),
        "state": state, "park_reason": task.get("park_reason"),
        "attempts": _count(task.get("attempt_count")), "max_attempts": _count(task.get("max_attempts")),
    }
    if state in TERMINAL:
        out.update(_duration(task))
    else:
        started = parse_time(task.get("started_at"))
        out["duration_s"] = int((now - started).total_seconds()) if started else None
        out["duration_basis"] = "so far" if started else NOT_RECORDED
    served = _money(task.get("cost_usd_total"))
    out["cost_usd"] = served if served is not None else _derived_totals(attempts)["cost_usd_total"] if attempts else None
    out["tokens"] = attempt_tokens(attempts)
    if out["tokens"] is None and state in TERMINAL:
        out["tokens"] = usage_tokens(task)
    if out["tokens"] is None:
        out["tokens_because"] = attempts_error and f"attempts not read: {attempts_error}" or NOT_RECORDED
    out["last_error"] = clip(" ".join(str(task["last_error"]).split()), 300) if task.get("last_error") else None
    out["pr_url"] = describe_task(task).get("pull_request")
    out["artifacts"] = [a["name"] for a in _artifacts(task)]
    link = console_link(task)
    if link is not None:
        out["console"] = link
    return out


def read(client: SwarmClient, args: dict[str, Any]) -> dict[str, Any]:
    """`swarm_browse`'s answer for `args["level"]`."""
    level = args.get("level")
    if level not in LEVELS:
        raise SwarmError(f"`level` is one of {list(LEVELS)}; it was {level!r}. Nothing was read")
    if level == "workflows":
        return workflows_level(client, session.current(), project=args.get("project") is True)
    if level == "stages":
        workflow_id = args.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise SwarmError("`level: \"stages\"` takes `workflow_id`. Nothing was read")
        return stages_level(client, workflow_id.strip())
    if level == "agents":
        ids = args.get("task_ids")
        if not isinstance(ids, list) or not all(isinstance(t, str) and t.strip() for t in ids):
            raise SwarmError("`level: \"agents\"` takes `task_ids`, the stage's task ids. Nothing was read")
        return agents_level(client, [t.strip() for t in ids])
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise SwarmError("`level: \"agent\"` takes `task_id`. Nothing was read")
    return agent_level(client, task_id.strip())
