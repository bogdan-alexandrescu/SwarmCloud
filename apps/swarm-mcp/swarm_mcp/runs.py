"""Issue runs (#454), reachable from a terminal and a Claude Code session.

`POST /v1/runs` reads an issue and the repository's open work, and a planner
task writes a plan. The plan waits -- PLANNED, holding no task, workflow or
lease (invariant 1) -- until a person approves it, and then compiles into one
workflow whose pull request's CI the API reads and fixes. This module is what
`sc` and the MCP server share about that: how a run and its plan are shown,
which workflow a run is running now, and how a run is followed.

THE DIGEST IS THE APPROVAL'S EVIDENCE. `plan:approve` takes the digest of the
plan the caller was SHOWN and refuses with `plan_changed` when the plan is no
longer that one. So every surface here shows the plan and its digest first and
approves with THAT digest -- never a read-and-approve in one breath, which
would approve whatever the plan had become with nobody having read it.

EVERYTHING SHOWN IS THE API'S, AS SERVED. The plan, the issue and anything read
from GitHub are data: nothing here turns them into a profile, an image or a
command (invariant 10). The run's `error` and `failure_excerpt` were redacted
by the API before they were stored (`swarm_api.redaction`), and no field this
module prints carries a credential. No console link is built here; a run
carries none, and its workflow's served link is shown by the workflow read.

THIS MODULE DOES NOT ADVANCE A RUN. A read does, on the server
(`routes/runs.py`); following a run is reading it again.
"""

from __future__ import annotations

import time
from typing import Any, Callable, TextIO

from . import render, workflows
from .client import SwarmClient, SwarmError
from .invocation import terminal_command
from .render import Style

#: How long `sc run --follow` waits between reads. A run moves on the scale of
#: a planner's or a step's minutes, and every read advances it server-side, so
#: polling faster buys nothing but load.
FOLLOW_INTERVAL_SECONDS = 10

#: The states in which a run runs a workflow that a session can attach to.
#: RUNNING runs the compiled plan; FIXING runs a CI fix round's continuation.
ATTACHABLE = frozenset({"RUNNING", "FIXING"})


def active_workflow_id(run: dict[str, Any]) -> str | None:
    """The workflow `run` is running now, or None.

    RUNNING: the compiled plan's. FIXING: the newest CI fix round's. Any other
    state runs no workflow -- PLANNING runs the planner task, CHECKING is the
    API reading CI, and a finished run runs nothing.
    """
    state = run.get("state")
    if state == "FIXING":
        rounds = [w for w in run.get("ci_fix_workflows") or [] if isinstance(w, str) and w]
        return rounds[-1] if rounds else None
    if state == "RUNNING":
        workflow_id = run.get("workflow_id")
        return workflow_id if isinstance(workflow_id, str) and workflow_id else None
    return None


def attach_with(run: dict[str, Any]) -> str | None:
    """`/sc attach <wf_id>` for a run whose workflow is running, else None (#448)."""
    workflow_id = active_workflow_id(run)
    return f"/sc attach {workflow_id}" if workflow_id else None


def waits_for_a_person(run: dict[str, Any]) -> bool:
    """A `required` run whose plan is waiting: nothing moves it but an approval."""
    return run.get("state") == "PLANNED" and run.get("plan_approval") != "auto"


def _issue_text(run: dict[str, Any]) -> str:
    issue = run.get("issue")
    if isinstance(issue, dict):
        return str(issue.get("ref") or issue.get("url") or "")
    return str(issue or "")


def _approve_command(run: dict[str, Any]) -> str:
    return terminal_command(f"sc plan approve {run.get('id')}")


# --------------------------------------------------------------------------
# Showing a run
# --------------------------------------------------------------------------

def run_lines(run: dict[str, Any], style: Style) -> list[str]:
    """The run: where it is, what it is running, and what it ended as."""
    dash = style.dash
    state = str(run.get("state") or dash)
    tone = {"DONE": "good", "FAILED": "bad", "REJECTED": "warn", "CANCELLED": "warn"}.get(state)
    lines = [
        render.section("run", f"{run.get('id')}{style.sep}{_issue_text(run)}", style),
        f"  state         {style.paint(state, tone)}",
        f"  plan          {run.get('plan_approval') or dash}"
        + (f"{style.sep}revision {run['plan_revision']}" if run.get("plan_revision") else ""),
        # VISIBLE BUT DISABLED (#454): the API refuses auto-merge until #295.
        f"  auto-merge    {'requested' if run.get('auto_merge') else 'off'}"
        f"{style.sep}refused by the API until #295",
        f"  fix rounds    {run.get('ci_fix_round') or 0} of {run.get('fix_rounds') or dash}",
    ]
    if run.get("planner_task_id"):
        lines.append(f"  planner       {run['planner_task_id']}")
    if run.get("workflow_id"):
        lines.append(f"  workflow      {run['workflow_id']}")
    for number, workflow_id in enumerate(run.get("ci_fix_workflows") or [], start=1):
        lines.append(f"  fix round {number}   {workflow_id}")
    pull = run.get("pull_request")
    if isinstance(pull, dict) and (pull.get("url") or pull.get("number")):
        lines.append(f"  pull request  {pull.get('url') or '#' + str(pull.get('number'))}")
        if pull.get("head_sha"):
            lines.append(f"  head          {pull['head_sha']}")
        if pull.get("checks"):
            lines.append(f"  checks        {pull['checks']}")
    if run.get("green_sha"):
        lines.append(f"  green at      {run['green_sha']}")
    if run.get("requirements_met") is not None:
        met = "every planned requirement met: Closes" if run["requirements_met"] else "part of"
        lines.append(f"  keyword       {met} {_issue_text(run)}")
        for missing in run.get("requirements_unmet") or []:
            lines.extend(render.detail(f"unmet: {missing}", style, indent=4, tone=None))
    if run.get("approved_by"):
        lines.append(f"  approved by   {run['approved_by']} at {run.get('approved_at') or dash}")
    if run.get("rejected_by"):
        why = f": {run['rejection_reason']}" if run.get("rejection_reason") else ""
        lines.append(f"  rejected by   {run['rejected_by']}{why}")
    if run.get("error"):
        lines.append("  error")
        lines.extend(render.detail(str(run["error"]), style, indent=4, tone="bad"))
    if run.get("failure_excerpt"):
        lines.append("  failing CI (redacted by the API)")
        for raw in str(run["failure_excerpt"]).splitlines()[-20:]:
            lines.append(style.paint("    " + raw, "dim"))
    hint = attach_with(run)
    if hint:
        lines.append(f"  live rows     {hint}")
    if waits_for_a_person(run):
        lines.append(
            f"  waiting for your approval: {_approve_command(run)}  (holds no capacity until then)"
        )
    return lines


def plan_lines(run: dict[str, Any], style: Style) -> list[str]:
    """The plan, whole, with the digest an approval of it must send.

    The step PROMPTS are printed in full: they are what will run with the
    tenant's forge token, so they are what is being approved.
    """
    plan = run.get("plan")
    if not isinstance(plan, dict):
        return [
            render.section("plan", "", style),
            f"  no plan yet: the run is {run.get('state')}",
        ]
    steps = [s for s in plan.get("steps") or [] if isinstance(s, dict)]
    head = f"{len(steps)} step{'s' if len(steps) != 1 else ''}"
    if plan.get("mode"):
        head += f"{style.sep}{plan['mode']}"
    if plan.get("estimate"):
        head += f"{style.sep}{plan['estimate']}"
    lines = [render.section("plan", head, style)]
    lines.extend(render.detail(str(plan.get("summary") or ""), style, indent=2, tone=None))
    requirements = plan.get("requirements") or []
    if requirements:
        lines.append("  requirements")
        for number, requirement in enumerate(requirements, start=1):
            lines.extend(render.detail(f"{number}. {requirement}", style, indent=4, tone=None))
    overlaps = [o for o in plan.get("overlaps") or [] if isinstance(o, dict)]
    if overlaps:
        lines.append("  overlaps with work in flight")
        for overlap in overlaps:
            lines.extend(render.detail(
                f"{overlap.get('kind')} {overlap.get('ref')}: {overlap.get('note')}",
                style, indent=4, tone="warn",
            ))
    for step in steps:
        title = f"  {style.marker} {step.get('step_id')}  {step.get('title') or ''}"
        if step.get("estimate"):
            title += f"{style.sep}{step['estimate']}"
        lines.append(style.paint(title, "bold"))
        if step.get("files"):
            lines.extend(render.detail("files: " + ", ".join(map(str, step["files"])), style, indent=4, tone=None))
        for test in step.get("tests") or []:
            lines.extend(render.detail(f"test: {test}", style, indent=4, tone=None))
        lines.extend(render.detail(str(step.get("prompt") or ""), style, indent=4))
    lines.append(f"  digest  {run.get('plan_digest') or style.dash}")
    return lines


def runs_lines(listing: dict[str, Any], style: Style) -> list[str]:
    rows = [r for r in listing.get("runs") or [] if isinstance(r, dict)]
    lines = [render.section("runs", f"{len(rows)} shown", style)]
    if not rows:
        lines.append("  none")
    for run in rows:
        pull = run.get("pull_request") if isinstance(run.get("pull_request"), dict) else {}
        tail = f"  {pull['url']}" if pull.get("url") else ""
        lines.append(
            f"  {run.get('id')}  {_issue_text(run)}  {run.get('state')}  "
            f"{run.get('updated_at') or style.dash}{tail}"
        )
    if listing.get("next_page_token"):
        lines.append(f"  more: --page-token {listing['next_page_token']}")
    return lines


# --------------------------------------------------------------------------
# The steps of the workflow a run is running
# --------------------------------------------------------------------------

def step_report(client: SwarmClient, run: dict[str, Any]) -> dict[str, Any] | None:
    """`workflows.report` for the run's active workflow, or None when it runs none."""
    workflow_id = active_workflow_id(run)
    if not workflow_id:
        return None
    return workflows.report(workflows.fetch(client, workflow_id))


def step_lines(report: dict[str, Any] | None, style: Style) -> list[str]:
    if not report:
        return []
    state = report.get("state") or style.dash
    lines = [render.section("steps", f"{report.get('workflow_id')}{style.sep}{state}", style)]
    for row in report.get("steps") or []:
        lines.append(f"  {row.get('step_id')}  {row.get('state') or style.dash}  {row.get('task_id') or ''}")
    return lines


# --------------------------------------------------------------------------
# Following a run
# --------------------------------------------------------------------------

def follow(
    client: SwarmClient,
    run_id: str,
    out: TextIO,
    style: Style,
    *,
    interval: float = FOLLOW_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    max_reads: int | None = None,
) -> dict[str, Any]:
    """Read the run until it finishes or waits for a person; print what moved.

    One line when the run's state changes, and one row per compiled step when
    that step's state changes -- `swarm tail`'s shape, timestamped by the
    reader's clock because a run carries no event times. Stops at a `required`
    run's PLANNED (only a person moves it, and following would poll for ever)
    and at a finished run. Each read advances the run on the server.
    """
    last_state: str | None = None
    seen_steps: dict[tuple[str, str], str | None] = {}
    planner_state: str | None = None
    reads = 0
    while True:
        run = client.run(run_id)
        reads += 1
        now = time.strftime("%H:%M:%SZ", time.gmtime())
        state = str(run.get("state"))
        if state != last_state:
            line = f"{now}  run {run_id}  {state}"
            pull = run.get("pull_request") if isinstance(run.get("pull_request"), dict) else {}
            if pull.get("url"):
                line += f"  {pull['url']}"
            out.write(line + "\n")
            if run.get("error") and run.get("terminal"):
                out.write("\n".join(render.detail(str(run["error"]), style, indent=2)) + "\n")
            last_state = state
        if state == "PLANNING" and run.get("planner_task_id"):
            try:
                task_state = client.task(str(run["planner_task_id"])).get("state")
            except SwarmError as exc:
                task_state = f"not read: {exc}"
            if task_state != planner_state:
                out.write(f"{now}    planner {run['planner_task_id']}  {task_state}\n")
                planner_state = task_state
        workflow_id = active_workflow_id(run)
        if workflow_id:
            try:
                report = step_report(client, run) or {}
            except SwarmError as exc:
                out.write(f"{now}    workflow {workflow_id} not read: {exc}\n")
                report = {}
            for row in report.get("steps") or []:
                key = (workflow_id, str(row.get("step_id")))
                if key not in seen_steps or seen_steps[key] != row.get("state"):
                    out.write(
                        f"{now}    {row.get('step_id')}  {row.get('state') or style.dash}"
                        f"  {row.get('task_id') or ''}\n"
                    )
                    seen_steps[key] = row.get("state")
        if run.get("terminal"):
            return run
        if waits_for_a_person(run):
            out.write(
                f"{now}  the plan is waiting for your approval: "
                f"{terminal_command(f'sc plan show {run_id}')}, then {_approve_command(run)}\n"
            )
            return run
        if max_reads is not None and reads >= max_reads:
            return run
        out.flush()
        sleep(interval)


def summary(client: SwarmClient, run: dict[str, Any]) -> dict[str, Any]:
    """What the MCP tools answer: the run as served, its steps, and what to do next."""
    out: dict[str, Any] = {"run": run}
    hint = attach_with(run)
    if hint:
        out["attach_with"] = hint
        try:
            out["steps"] = step_report(client, run)
        except SwarmError as exc:
            out["steps_unread_because"] = str(exc)
    if waits_for_a_person(run):
        out["next"] = (
            "show the developer this plan and its plan_digest, and approve only with "
            "swarm_plan_approve and THAT digest once they say so; or swarm_plan_edit / "
            "swarm_plan_reject. The run holds no capacity while it waits."
        )
    elif not run.get("terminal"):
        out["next"] = "read it again with swarm_run; each read advances it"
    return out
