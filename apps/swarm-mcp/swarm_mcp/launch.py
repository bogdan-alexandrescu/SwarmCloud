"""Per-run copies of `/sc:swarmcloud`'s script, each titled after its SwarmCloud workflow.

Owner, 2026-10-04: every SwarmCloud workflow run in Claude Code is titled
`SwarmCloud · <name> · N steps`, at most 100 characters, instead of the fixed
`swarmcloud` and its generic description. A workflow script's `meta` must be a
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


def for_all(client: SwarmClient) -> dict[str, Any]:
    """One titled copy per running workflow, newest first, up to the cap."""
    from . import sc

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
    return out
