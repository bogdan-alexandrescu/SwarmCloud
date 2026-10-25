"""`/sc layout rows|pane`: how this project follows SwarmCloud work in Claude
Code, remembered per project (SC-BROWSER-BUILD, #2318; BGD picked proposal B
on 2026-10-25, PR 2328).

TWO LAYOUTS.

* `rows` -- today's: /sc:swarmcloud shows each workflow in /workflows as the
  plugin's `rows` setting says (`summary`, one sc:summary row per workflow,
  or `per-step`, a row per unfinished step). Every row is a model agent that
  polls for as long as its work runs.
* `pane` -- the SwarmCloud pane beside the chat (plugin/hooks/pane.tsx), which
  reads SwarmCloud with one direct MCP call per click and spends no model
  tokens, plus ONE quiet /workflows row per workflow (an sc:finish agent)
  whose only job is to notice the finish: it holds each follow until the
  workflow's STAGE changes (`compact.watch_stage`), so a quiet stage is a
  call every thirty minutes, not one per step change.

`rows` is the default: a project that never chose keeps what it had.

THE FILE: `<config_dir>/sc-layout/<sha256 of the project directory>.json`,
beside `/sc watch`'s preference and keyed the same way (`watch.project_dir`,
`watch.project_key`), never in the repository. A saved choice outranks the
plugin's `layout` option (SWARM_PLUGIN_LAYOUT), so `/sc layout` or the pane's
Layout control switches without restarting the session: the bridge reads the
file at every launch, and the pane reads it through `swarm_layout`.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config, watch
from .client import SwarmError

ROWS = "rows"
PANE = "pane"
LAYOUTS = (ROWS, PANE)
#: Today's layout, unless the project or the plugin option chose the pane.
DEFAULT = ROWS
STATE_DIR = "sc-layout"
#: The plugin's `layout` option (`userConfig.layout` in plugin.json).
OPTION_ENV = config.plugin_env("layout")


def check(value: Any) -> str:
    """`rows` or `pane`, from what a person typed; anything else is refused."""
    given = value.strip().lower() if isinstance(value, str) else ""
    if given not in LAYOUTS:
        raise SwarmError(
            f"`{value}` is not a layout: `rows` (today's /workflows rows) or `pane` (the "
            "SwarmCloud pane plus one row per workflow). Nothing was saved"
        )
    return given


def state_path(project: Path, environ: Mapping[str, str] | None = None) -> Path:
    return config.config_dir(environ) / STATE_DIR / f"{watch.project_key(project)}.json"


def saved(project: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """The project's saved layout, or None (never set, or unreadable)."""
    try:
        data = json.loads(state_path(project, environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("layout") if isinstance(data, dict) else None
    return value if value in LAYOUTS else None


def option(environ: Mapping[str, str] | None = None) -> str | None:
    """The plugin's `layout` option, or None when unset, unexpanded or not a layout."""
    environ = os.environ if environ is None else environ
    given = (environ.get(OPTION_ENV) or "").strip().lower()
    return given if given in LAYOUTS else None


def current(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`{layout, source, project}`: the saved choice, else the option, else the default."""
    project = watch.project_dir(environ)
    chosen = saved(project, environ)
    if chosen is not None:
        return {"layout": chosen, "source": "saved for this project", "project": str(project)}
    chosen = option(environ)
    if chosen is not None:
        return {"layout": chosen, "source": "the plugin's layout option", "project": str(project)}
    return {"layout": DEFAULT, "source": "the default", "project": str(project)}


def save(value: Any, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Save `value` for this project, atomically; answers as `current` does."""
    chosen = check(value)
    project = watch.project_dir(environ)
    path = state_path(project, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sc-layout-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "layout": chosen,
                "project": str(project),
                "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {"layout": chosen, "source": "saved for this project", "project": str(project)}


def row_mode(rows: str | None, environ: Mapping[str, str] | None = None) -> str:
    """The row mode a launch writes: `rows` when the caller named one, else
    `pane` (config.ROWS_PANE) when this project's layout is the pane, else the
    plugin's `rows` setting (`config.rows_mode`)."""
    if rows:
        return rows
    if current(environ)["layout"] == PANE:
        return config.ROWS_PANE
    return config.rows_mode(environ)


#: What a layout answer says about rows already running.
SWITCH_NOTE = (
    "Rows already running in /workflows keep going until they end; the next launch or "
    "`/sc attach --all` starts rows in the new layout. Nothing in SwarmCloud was touched"
)
