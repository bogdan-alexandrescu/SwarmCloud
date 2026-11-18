"""`/sc watch`: a recurring `/sc check`, and the per-project preference that
re-arms it in the next session (SC-WATCH, owner request 2026-10-14).

THE SCHEDULER IS CLAUDE CODE'S. A plugin skill may call the session's own
CronCreate tool: it enqueues a prompt at each cron match while the session is
idle. Its jobs are SESSION-ONLY -- gone when Claude Code exits, and a
recurring job expires after 7 days -- so the plugin keeps the one fact that
must outlive a session, "this project wants a watch every N minutes", in a
small file of its own, and the SessionStart hook (plugin/hooks/
session-start.sh) reads it and tells the next session to run
`/sc watch <N>m` again. This module parses the interval, spells the cron,
and reads and writes that file; it schedules nothing itself and reads
nothing from SwarmCloud.

THE INTERVAL: default 30 minutes, at least 5 (owner), at most a day. A cron
expression cannot say "every 45 minutes", so an interval is rounded to the
nearest one it can say -- a divisor of 60 below an hour, a divisor of 24
hours above -- and the answer says it was. The minute is never :00 or :30:
CronCreate's own guidance, because every session asking for "every 30
minutes" would otherwise land on the API at the same instant.

THE FILE: `<config_dir>/sc-watch/<sha256 of the project directory>.json`,
under the bridge's own per-user directory (`config.config_dir`), never in the
repository. The project is the git top level of the session's directory
(CLAUDE_PROJECT_DIR when Claude Code sets it, else the bridge's cwd), with
symlinks resolved, so a session opened in a subdirectory is the same
project. The hook derives the same key the same way, in bash
(`tests/unit/mcp/test_sc_watch.py` holds the two together).

IT ARMS ITSELF (SC-AUTO-WATCH, owner request 2026-10-14): "/sc watch should
automatically be set if there is at least one workflow being worked on in sc
that is attached here or if and when we start a new workflow". Every tool
reply that puts SwarmCloud work in front of the session -- a spec launched or
submitted, an issue run whose workflow exists, an attach -- carries
`arm_watch` (`arm_call`): the CronCreate call at the saved interval, else the
default, which the skill makes after CronList shows no `/sc check` job. So the
file now has THREE states: absent (never set: arm at the default when work is
attached), an interval (arm at it), and `"watch": "off"` -- an explicit `/sc
watch off` is SAVED rather than forgotten, because forgetting it would let the
next attach arm the watch the developer just turned off. It wins until `/sc
watch` again overwrites it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config
from .client import SwarmError

DEFAULT_INTERVAL_MINUTES = 30
#: The owner's floor: a check reads the API, and a watch is not a poll loop.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60

#: What each firing runs.
CHECK_PROMPT = "/sc check"
STATE_DIR = "sc-watch"
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"
#: How the hook hands the remembered interval to `sc workflows
#: --session-start`, so one context names both the attach and the re-arm.
#: `off` there is the explicit `/sc watch off`: the context then asks for no
#: arming at all.
REARM_ENV = "SWARM_SC_WATCH_MINUTES"
HANDED_OFF = "off"

#: The intervals a cron expression can say evenly.
_MINUTE_STEPS = (5, 6, 10, 12, 15, 20, 30)
_HOUR_STEPS = (1, 2, 3, 4, 6, 8, 12, 24)
_SAYABLE = _MINUTE_STEPS + tuple(h * 60 for h in _HOUR_STEPS)

#: The minute every expression starts from: off :00 and :30.
_OFFSET = 7

_INTERVAL = re.compile(
    r"^\s*(?:(?P<h>\d+)\s*(?:h|hr|hrs|hour|hours))?\s*(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes)?)?\s*$",
    re.IGNORECASE,
)


def parse_interval(text: Any) -> dict[str, Any]:
    """`{minutes, asked, clamped?, rounded?}` for `30m`, `30`, `1h`, `1h30m`, ...

    Empty or None is the default. Below MIN_INTERVAL_MINUTES is held to it
    (`clamped`), above a day to a day; the result is then the nearest
    interval a cron expression says evenly (`rounded`, ties to the longer).
    Anything else is refused: a typo is not an interval."""
    asked = "" if text is None else str(text).strip()
    if not asked:
        return {"minutes": DEFAULT_INTERVAL_MINUTES, "asked": None}
    match = _INTERVAL.match(asked)
    if not match or (match.group("h") is None and match.group("m") is None):
        raise SwarmError(
            f"`{asked}` is not an interval: give minutes (`30m`, `30`) or hours (`1h`, `1h30m`). "
            f"The default is {DEFAULT_INTERVAL_MINUTES}m, the shortest {MIN_INTERVAL_MINUTES}m. "
            "Nothing was scheduled"
        )
    minutes = int(match.group("h") or 0) * 60 + int(match.group("m") or 0)
    out: dict[str, Any] = {"asked": asked}
    held = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, minutes))
    if held != minutes:
        out["clamped"] = (
            f"{minutes} min is below the {MIN_INTERVAL_MINUTES}-minute minimum" if minutes < held
            else f"{minutes} min is above the one-day maximum"
        )
    sayable = min(_SAYABLE, key=lambda s: (abs(s - held), -s))
    if sayable != held:
        out["rounded"] = f"{held} min is not a cron interval; the nearest is {sayable} min"
    out["minutes"] = sayable
    return out


def cron_for(minutes: int) -> str:
    """A 5-field cron expression firing every `minutes` (one of `_SAYABLE`),
    as explicit lists -- no `a-b/n` steps, which not every parser reads."""
    if minutes not in _SAYABLE:
        raise SwarmError(f"{minutes} min is not a cron interval; one of {list(_SAYABLE)}")
    if minutes < 60:
        start = _OFFSET % minutes
        return f"{','.join(str(m) for m in range(start, 60, minutes))} * * * *"
    hours = minutes // 60
    if hours == 1:
        return f"{_OFFSET} * * * *"
    return f"{_OFFSET} {','.join(str(h) for h in range(0, 24, hours))} * * *"


def interval_text(minutes: int) -> str:
    return f"{minutes}m"


# --------------------------------------------------------------------------
# The per-project preference
# --------------------------------------------------------------------------


def project_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The project a watch belongs to: the git top level of CLAUDE_PROJECT_DIR
    (else this process's cwd), symlinks resolved."""
    environ = os.environ if environ is None else environ
    start = Path(environ.get(PROJECT_DIR_ENV, "").strip() or os.getcwd())
    try:
        got = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        top = got.stdout.strip() if got.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        top = ""
    return Path(top or start).resolve()


def project_key(project: Path) -> str:
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()


def state_path(project: Path, environ: Mapping[str, str] | None = None) -> Path:
    return config.config_dir(environ) / STATE_DIR / f"{project_key(project)}.json"


def read(project: Path, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The saved preference, or None (never set, turned off, or unreadable)."""
    try:
        data = json.loads(state_path(project, environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("interval_minutes"), int):
        return None
    return data


def save(project: Path, minutes: int, environ: Mapping[str, str] | None = None) -> Path:
    """Write the preference atomically. ONE LINE, `"interval_minutes": N`
    first, so the hook reads it with sed rather than a JSON parser."""
    path = state_path(project, environ)
    _write(path, json.dumps({
        "interval_minutes": minutes,
        "project": str(project),
        "enabled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }))
    return path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sc-watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save_off(project: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Record an explicit `/sc watch off`. ONE LINE, `"watch": "off"` first and
    no `interval_minutes`, so the hook's sed finds no interval and its grep
    finds the off."""
    path = state_path(project, environ)
    _write(path, json.dumps({
        "watch": HANDED_OFF,
        "project": str(project),
        "disabled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }))
    return path


def preference(project: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """`"on"` (an interval is saved), `"off"` (an explicit `/sc watch off`), or
    None (never set, or unreadable)."""
    if read(project, environ) is not None:
        return "on"
    try:
        data = json.loads(state_path(project, environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return HANDED_OFF if isinstance(data, dict) and data.get("watch") == HANDED_OFF else None


def arm_call(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The CronCreate call that arms this project's watch, or None when the
    project's watch is explicitly off. At the saved interval, else the
    default. It is an instruction, not a schedule: only the session can see
    its own jobs, so `unless` tells it to look first and never make a second."""
    project = project_dir(environ)
    if preference(project, environ) == HANDED_OFF:
        return None
    saved = read(project, environ)
    minutes = int(saved["interval_minutes"]) if saved else DEFAULT_INTERVAL_MINUTES
    if minutes not in _SAYABLE:
        minutes = DEFAULT_INTERVAL_MINUTES
    return {
        "unless": (
            "CronList first: if a job whose prompt is `/sc check` exists, the watch is already "
            "armed in this session -- make neither call, never a second job"
        ),
        "save_with": {"tool": "swarm_check_schedule", "args": {"interval": interval_text(minutes)}},
        "tool": "CronCreate",
        "args": {"cron": cron_for(minutes), "prompt": CHECK_PROMPT, "recurring": True},
        "then_say": f"the watch is armed: a `/sc check` every {minutes} minutes (`/sc watch off` stops it)",
    }


#: What a reply carries beside `arm_watch: null` when the project turned the
#: watch off, so the session says why it armed nothing.
OFF_NOTE = "the watch is off for this project (`/sc watch off`); `/sc watch` turns it on again"


def arm_fields(armed: bool, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`{arm_watch}` for a reply: the call when `armed` (work was attached or
    started) and the project's watch is not off; null otherwise, with
    `arm_watch_off` saying it was the preference."""
    if not armed:
        return {"arm_watch": None}
    call = arm_call(environ)
    return {"arm_watch": call} if call is not None else {"arm_watch": None, "arm_watch_off": OFF_NOTE}


def rearm_minutes(environ: Mapping[str, str] | None = None) -> int | None:
    """The interval the hook handed over in REARM_ENV, or None."""
    environ = os.environ if environ is None else environ
    text = environ.get(REARM_ENV, "").strip()
    if not text.isdigit():
        return None
    minutes = int(text)
    return minutes if minutes in _SAYABLE else None


def handed_off(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the hook said this project's watch is explicitly off."""
    environ = os.environ if environ is None else environ
    return environ.get(REARM_ENV, "").strip() == HANDED_OFF


def arm_sentence() -> str:
    """What the SessionStart context asks when work runs and no watch was
    ever set for the project (SC-AUTO-WATCH)."""
    return (
        "Then arm the sc plugin's watch for this project, since SwarmCloud work of yours is "
        f"running: CronList first, and unless a job whose prompt is `{CHECK_PROMPT}` already "
        f"exists, run `/sc watch` (a `{CHECK_PROMPT}` every {DEFAULT_INTERVAL_MINUTES} minutes; "
        "`/sc watch off` turns it off for this project)."
    )


def rearm_sentence(minutes: int) -> str:
    return (
        f"The sc plugin's watch is on for this project: a `/sc check` every {minutes} minutes. "
        "Claude Code's scheduled prompts end with each session, so run "
        f"`/sc watch {interval_text(minutes)}` now to re-arm it (`/sc watch off` turns it off)."
    )


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


def tool(args: dict[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`swarm_check_schedule`: `{interval}` turns the watch on (and saves it), `{off:
    true}` turns it off, neither reports it. Schedules nothing: the reply
    hands the session the CronCreate call to make, and the `/loop` command
    that does the same where CronCreate is not available."""
    project = project_dir(environ)
    off = args.get("off")
    if off is not None and not isinstance(off, bool):
        raise SwarmError("swarm_check_schedule: `off` is true or false. Nothing was changed")
    if off and args.get("interval") is not None:
        raise SwarmError("swarm_check_schedule takes `interval` OR `off: true`, not both. Nothing was changed")
    if off:
        was_on = read(project, environ) is not None
        # Saved, not forgotten: an attach after this must not arm it again.
        save_off(project, environ)
        return {
            "watch": "off",
            "project": str(project),
            "was_on": was_on,
            "saved": "off until `/sc watch` again: no attach or run arms it in the meantime",
            "cancel_with": (
                "CronList, then CronDelete {id} for every job whose prompt is `/sc check`; a "
                "`/loop ... /sc check` started by hand ends with Esc"
            ),
        }
    if args.get("interval") is None and "interval" not in args:
        saved = read(project, environ)
        if saved is None:
            explicitly_off = preference(project, environ) == HANDED_OFF
            return {"watch": "off", "project": str(project),
                    "explicitly_off": explicitly_off,
                    "note": ("turned off with `/sc watch off`: nothing arms it until `/sc watch`" if explicitly_off
                             else "never set: the next attach or run arms it at the default"),
                    "turn_on_with": f"/sc watch [interval] (default {interval_text(DEFAULT_INTERVAL_MINUTES)})"}
        minutes = int(saved["interval_minutes"])
        return {"watch": "on", "project": str(project), "interval_minutes": minutes,
                "cron": cron_for(minutes) if minutes in _SAYABLE else None,
                "enabled_at": saved.get("enabled_at"),
                "note": "saved for this project; whether a job is armed in THIS session is CronList's to say"}
    parsed = parse_interval(args.get("interval"))
    minutes = parsed["minutes"]
    path = save(project, minutes, environ)
    out: dict[str, Any] = {
        "watch": "on",
        "project": str(project),
        "interval_minutes": minutes,
        "cron": cron_for(minutes),
        "prompt": CHECK_PROMPT,
        "arm_with": {"tool": "CronCreate",
                     "args": {"cron": cron_for(minutes), "prompt": CHECK_PROMPT, "recurring": True}},
        "replace_first": "CronList, then CronDelete {id} for any job whose prompt is `/sc check`, so one watch runs",
        "without_cron": f"/loop {interval_text(minutes)} {CHECK_PROMPT}",
        "saved_to": str(path),
        "expires": "CronCreate's recurring jobs end with the session and after 7 days; the next session is "
                   "told to re-arm it by the plugin's SessionStart hook",
    }
    for key in ("asked", "clamped", "rounded"):
        if parsed.get(key):
            out[key] = parsed[key]
    return out
