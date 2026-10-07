"""Per-run copies of `/sc:swarmcloud`'s script, each titled after its SwarmCloud workflow.

Owner, 2026-10-04: every SwarmCloud workflow run in Claude Code is titled
after its workflow -- since 2026-10-05 `SC · <name> · N steps`, at most 150
characters (the task panel shows ~28; Enter shows the whole) -- instead of
the fixed `swarmcloud` and its generic description. A workflow script's `meta` must be a
PURE LITERAL -- Claude Code reads the name before the script runs, and a
running script cannot rename itself -- so the name cannot be computed inside
run.js. Instead the bridge writes a copy of the plugin's run.js with
`meta.name` and `meta.description` filled in, and the session launches that
copy (`Workflow({scriptPath, args})`). Nothing else in the copy differs: the
two lines are replaced exactly, and a run.js whose first three lines are not
the shape this expects is refused rather than patched somewhere else.

The bridge finds run.js through `SWARM_SC_PLUGIN_ROOT`, which the plugin's
manifest sets to `${CLAUDE_PLUGIN_ROOT}`: the bridge itself runs from a `uv
tool` install of `apps/swarm-mcp` and has no plugin directory of its own.

`{attach: "all"}` is one run PER WORKFLOW (owner, 2026-10-04): a workflow
script cannot start a separate run -- `workflow()` nests the child inside the
caller's run, sharing its agent counter -- so the bridge writes one copy per
running workflow and the session launches each.

SINGLE TASKS (#830, owner 2026-10-07). A task sent with `swarm_dispatch`
belongs to no workflow, so it never had a row. `for_tasks` writes ONE copy
whose args are `{attach_tasks: [...]}`: run.js starts one `sc:task` row per
task in it, titled from the task's label. `for_all` adds that copy for the
caller's running single tasks, and `swarm_dispatch` hands one back for the
task ids it just sent, so a dispatched task gets a row without anyone asking.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from . import workflows
from .client import SwarmClient, SwarmError

PLUGIN_ROOT_ENV = "SWARM_SC_PLUGIN_ROOT"
RUN_SCRIPT = Path("workflows") / "run.js"

#: run.js's first three lines, as this module expects them.
META_OPEN = "export const meta = {"
NAME_LINE = "  name: 'swarmcloud',"
DESCRIPTION_OPEN = "  description: '"

#: How long the per-run description may be: one line in the permission
#: dialog and the workflow list, not a paragraph.
DESCRIPTION_CHARS = 300

#: The same cap run.js puts on `{attach: "all"}` (MAX_ATTACHED_WORKFLOWS):
#: every unfinished step is a live row, and past 10 workflows /workflows is
#: no longer readable. The rest are listed with the call that attaches each.
MAX_ATTACHED_WORKFLOWS = 10

#: The cap on single-task rows in one copy: each is an agent of its own in
#: the session for as long as its task runs, like a step row, and run.js
#: holds the same number (MAX_ATTACHED_TASKS). The rest are listed with the
#: command that shows them.
MAX_ATTACHED_TASKS = 10

#: Where the copies go: the system's temporary directory, one file per run.
RUN_DIR = "sc-swarmcloud-runs"
#: A per-run copy older than this is removed when the next one is written, so
#: the folder does not grow without bound. A week: Claude Code reads a copy at
#: launch and again only to resume that run, which happens within a session.
RUN_COPY_MAX_AGE_S = 7 * 24 * 3600


def template() -> str:
    root = os.environ.get(PLUGIN_ROOT_ENV, "").strip()
    if not root:
        raise SwarmError(
            f"{PLUGIN_ROOT_ENV} is not set, so this bridge cannot find the plugin's run.js to "
            "title a run with. The sc plugin's manifest sets it to ${CLAUDE_PLUGIN_ROOT}; a "
            "bridge started some other way has none. Launch the /sc:swarmcloud workflow by "
            "name instead -- its run is then titled `swarmcloud`. Nothing was written"
        )
    path = Path(root).expanduser() / RUN_SCRIPT
    if not path.is_file():
        raise SwarmError(f"{path} is not a file: {PLUGIN_ROOT_ENV} does not point at the sc plugin")
    return path.read_text(encoding="utf-8")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_script(title: str, description: str) -> Path:
    """A copy of run.js with `meta.name` = `title` and `meta.description` =
    `description`, as JSON string literals -- which are JavaScript string
    literals, so no title can end the literal early or add code."""
    lines = template().split("\n")
    if len(lines) < 3 or lines[0] != META_OPEN or lines[1] != NAME_LINE or not lines[2].startswith(DESCRIPTION_OPEN):
        raise SwarmError(
            "the plugin's run.js does not open with the meta name and description lines this "
            "bridge replaces, so no titled copy was written; the plugin and this bridge are "
            "from different releases"
        )
    lines[1] = "  name: " + json.dumps(title, ensure_ascii=False) + ","
    lines[2] = "  description: " + json.dumps(_clip(description, DESCRIPTION_CHARS), ensure_ascii=False) + ","
    folder = Path(tempfile.gettempdir()) / RUN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    _prune(folder)
    target = folder / f"swarmcloud-{secrets.token_hex(6)}.js"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _prune(folder: Path) -> None:
    """Remove this bridge's own run copies older than RUN_COPY_MAX_AGE_S. Only
    `swarmcloud-*.js` in its own folder; a file that vanishes or cannot be
    removed meanwhile is left alone, never an error for the launch."""
    cutoff = time.time() - RUN_COPY_MAX_AGE_S
    for old in folder.glob("swarmcloud-*.js"):
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def _launch(title: str, description: str, args: dict[str, Any] | None) -> dict[str, Any]:
    script = write_script(title, description)
    out: dict[str, Any] = {"title": title, "script_path": str(script)}
    if args is not None:
        out["args"] = args
    return out


def for_spec(document: dict[str, Any]) -> dict[str, Any]:
    """The titled copy for a spec about to be submitted. The workflow id is not
    known yet, so the title has none; the run's first row and its Result row
    carry it once SwarmCloud has answered."""
    spec = workflows.read_spec(document, where="the spec")
    steps = len(spec["steps"])
    name = workflows.title_name(title=spec["title"], label=spec["label"])
    title = workflows.workflow_title(name, steps)
    description = (
        f"{title}: each step runs in SwarmCloud and is shown here as one [SwarmCloud] row, "
        "by the console's stage, with its console link"
    )
    out = _launch(title, description, None)
    out["steps"] = steps
    out["launch_with"] = (
        "the Workflow tool with {scriptPath: <script_path>, args: <the same args /sc:swarmcloud "
        "takes -- {spec, spec_path} or {spec}>}"
    )
    return out


def _attach_launch(workflow_id: str, name: str, steps: int) -> dict[str, Any]:
    title = workflows.workflow_title(name, steps)
    description = (
        f"{title}: attached to SwarmCloud workflow {workflow_id}; each unfinished step is shown "
        "here as one [SwarmCloud] row, by the console's stage. Nothing is submitted"
    )
    out = _launch(title, description, {"attach": workflow_id, "title": name})
    out["workflow_id"] = workflow_id
    return out


def for_attach(client: SwarmClient, workflow_id: str) -> dict[str, Any]:
    """The titled copy for attaching one workflow: one read, for the name
    SwarmCloud stored and the step count."""
    envelope = workflows.fetch(client, workflow_id)
    stored = workflows.stored_names(envelope)
    name = workflows.title_name(stored=stored["title"] or stored["label"], workflow_id=workflow_id)
    out = _attach_launch(workflow_id, name, len(envelope["workflow"].get("steps") or []))
    out["launch_with"] = "the Workflow tool with {scriptPath: <script_path>, args: <args>}"
    return out


def _task_name(entry: dict[str, Any]) -> str:
    return workflows.title_name(stored=entry.get("label") or entry.get("title"), workflow_id=entry.get("task_id"))


def tasks_title(entries: list[dict[str, Any]]) -> str:
    """`SC · <label> · task` for one task, `SC · <first label> +N · N tasks`
    for several, at most workflows.TITLE_CHARS: the name is cut, never the
    count."""
    count = len(entries)
    name = _task_name(entries[0]) if entries else "tasks"
    if count > 1:
        name = f"{name} +{count - 1}"
    title = workflows.workflow_title(name, count)
    # workflow_title ends in ` · N step(s)`; a single task is a task.
    head, _, _ = title.rpartition(" · ")
    return head + (" · task" if count == 1 else f" · {count} tasks")


def for_tasks(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """ONE titled copy that starts an `sc:task` row per task in `entries` --
    each `{task_id, label?, console?}` -- up to MAX_ATTACHED_TASKS. The rest
    come back in `not_followed`. Reads nothing and submits nothing."""
    wanted = [e for e in entries if isinstance(e, dict) and e.get("task_id")]
    follow = wanted[:MAX_ATTACHED_TASKS]
    rows = []
    for entry in follow:
        row: dict[str, Any] = {"task_id": str(entry["task_id"]), "title": _task_name(entry)}
        if isinstance(entry.get("console"), str) and entry["console"].strip():
            row["console"] = entry["console"].strip()
        rows.append(row)
    title = tasks_title(follow)
    description = (
        f"{title}: each SwarmCloud single task is shown here as one [SwarmCloud] row, "
        "titled from its label, with its console link. Nothing is submitted"
    )
    out = _launch(title, description, {"attach_tasks": rows})
    out["task_ids"] = [row["task_id"] for row in rows]
    out["not_followed"] = [
        {"task_id": str(e["task_id"]), "label": e.get("label"), "follow_with": "swarm_follow"}
        for e in wanted[MAX_ATTACHED_TASKS:]
    ]
    out["launch_with"] = "the Workflow tool with {scriptPath: <script_path>, args: <args>}"
    return out


def for_all(client: SwarmClient) -> dict[str, Any]:
    """One titled copy per running workflow, newest first, up to the cap, and
    one more for the caller's running single tasks (#830)."""
    from . import sc

    # The single tasks first, and on their own: a failure to read them is
    # reported beside the workflows, never instead of them.
    try:
        singles: dict[str, Any] | None = sc.running_tasks(client)
        singles_error = None
    except SwarmError as exc:
        singles, singles_error = None, str(exc)
    listing = sc.running_workflows(client)
    entries = [e for e in listing.get("workflows") or [] if isinstance(e, dict) and e.get("workflow_id")]
    launches = []
    for entry in entries[:MAX_ATTACHED_WORKFLOWS]:
        workflow_id = str(entry["workflow_id"])
        name = workflows.title_name(stored=entry.get("title") or entry.get("label"), workflow_id=workflow_id)
        launches.append(_attach_launch(workflow_id, name, int(entry.get("steps_total") or 0)))
    not_followed = [
        {"workflow_id": e["workflow_id"], "title": e.get("title") or e.get("label"),
         "attach_with": f"/sc attach {e['workflow_id']}"}
        for e in entries[MAX_ATTACHED_WORKFLOWS:]
    ]
    out: dict[str, Any] = {
        "count": len(entries),
        "launches": launches,
        "not_followed": not_followed,
        "launch_with": (
            "one Workflow call per entry of `launches`, each {scriptPath: <script_path>, args: "
            "<args>}: one Claude Code run per SwarmCloud workflow"
        ),
    }
    if listing.get("complete") is False:
        out["complete"] = False
        out["incomplete_because"] = listing.get("incomplete_because")
    if singles_error is not None:
        out["single_tasks_error"] = singles_error
    elif singles and singles["tasks"]:
        task_launch = for_tasks(singles["tasks"])
        launches.append(task_launch)
        out["single_tasks"] = len(singles["tasks"])
        not_followed.extend(task_launch.pop("not_followed"))
        if singles.get("complete") is False:
            out["single_tasks_incomplete_because"] = singles.get("incomplete_because")
    else:
        out["single_tasks"] = 0
    out["launch_with"] = (
        "one Workflow call per entry of `launches`, each {scriptPath: <script_path>, args: "
        "<args>}: one Claude Code run per SwarmCloud workflow, and one for your running "
        "single tasks (args `attach_tasks`), with a row per task"
    )
    return out
