"""Which Claude Code session this bridge serves, and which SwarmCloud work is
that session's own (SC-SESSION-SCOPE, owner decision 2026-10-14).

"check on the set time interval to see if there are any tasks or workflows
running in swarmcloud that belong to this agent or claude code session that
should be attached and are not. It should filter any potential workflow that
is not owned by this claude session. Even if using the same tenant or
workspace in swarmcloud, only the tasks related to the work this claude code
session is doing should pass as valid candidates to attach if not attached
already. Same goes for watching and checking for staleness."

THE SESSION ID. Claude Code starts each MCP server, and each hook, with
`CLAUDE_CODE_SESSION_ID` in its environment -- measured 2026-10-14 on Claude
Code 2.1.283: the swarm-mcp process (`uv run ... swarm-mcp` and its python
child) carried the same value as the Bash tool and as the name of the
session's transcript, `<id>.jsonl`. /compact keeps the session and its id, and
`claude --resume <id>` continues that same transcript under that same id; a
new `claude` gets a new one. So the environment is the source, read on every
call rather than once (a test, or a hook, sets it per call).

ONE CASE THE ENVIRONMENT CANNOT SEE: `/clear` (and an in-process `/resume`)
moves the session to another id while the MCP server keeps running with the
environment it was started with. The plugin's SessionStart hook fires on each
of those with the session's CURRENT id on stdin (`session_id`), so it records
`{session_id}` under `<config_dir>/sc-session/<sha256 of the id this process
was started with>.json`, and `current()` prefers that record: the bridge and
the hook inherit the same starting id from the one Claude Code process, so
each finds the other's record and no other session's. A record whose key is
not this process's starting id is never read.

`SWARM_SC_SESSION_ID` is the hook handing its stdin id straight to the CLI it
runs (`sc workflows --session-start`), which wins over both: that process
serves exactly that hook firing.

No id known (a Claude Code too old to set the variable, or `sc` run outside
one) is NOT "every session": nothing is then this session's, every automatic
candidate list is empty, and the answer says why. Untagged work -- submitted
before this change, or by another client -- is never a candidate either.

THE TAG. Every submission the bridge makes carries the id as
`metadata.claude_session`: on a task, and on a workflow (whose metadata swarm-
api writes onto every step task, the frozen `Workflow` having none of its
own). An issue run takes it as `RunCreate.claude_session` and swarm-api copies
it onto the planner task, the compiled workflow and every CI-fix and merge
workflow the run starts; the merge step's conflict-fix and CI-fix rounds copy
their merge task's. swarm-api serves it back as `claude_session` on a task
row, a workflow row and a run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Iterable, Mapping

from . import config

#: What Claude Code sets in every MCP server's and hook's environment.
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
#: What the SessionStart hook hands the CLI it runs: its stdin `session_id`.
HOOK_ENV = "SWARM_SC_SESSION_ID"
#: The metadata key every submission carries, and the key swarm-api serves.
METADATA_KEY = "claude_session"
#: Where the hook records the current id for a process's starting id.
STATE_DIR = "sc-session"

#: A Claude Code session id is a UUID; anything id-shaped is accepted, so a
#: future format is not refused, and anything else is not an id.
_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def _valid(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text if _SHAPE.match(text) else None


def record_path(started_as: str, environ: Mapping[str, str] | None = None):
    key = hashlib.sha256(started_as.encode("utf-8")).hexdigest()
    return config.config_dir(environ) / STATE_DIR / f"{key}.json"


def _recorded(started_as: str, environ: Mapping[str, str] | None) -> str | None:
    try:
        data = json.loads(record_path(started_as, environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _valid(data.get("session_id")) if isinstance(data, dict) else None


def current(environ: Mapping[str, str] | None = None) -> str | None:
    """This session's id, or None when it cannot be known."""
    environ = os.environ if environ is None else environ
    handed = _valid(environ.get(HOOK_ENV))
    if handed:
        return handed
    started_as = _valid(environ.get(SESSION_ENV))
    if not started_as:
        return None
    return _recorded(started_as, environ) or started_as


def source(environ: Mapping[str, str] | None = None) -> str:
    """Where `current()` read the id from, for an answer to say."""
    environ = os.environ if environ is None else environ
    if _valid(environ.get(HOOK_ENV)):
        return "the SessionStart hook's session_id"
    started_as = _valid(environ.get(SESSION_ENV))
    if not started_as:
        return "unknown"
    if _recorded(started_as, environ):
        return f"the SessionStart hook's record for {SESSION_ENV}"
    return SESSION_ENV


def tag(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`{claude_session: <id>}` for a submission's metadata, or `{}`."""
    sid = current(environ)
    return {METADATA_KEY: sid} if sid else {}


# --------------------------------------------------------------------------
# Whose work is it
# --------------------------------------------------------------------------


def owner_of(doc: Any) -> str | None:
    """The session a task, workflow, run or listing entry was tagged with.

    swarm-api serves it top-level (`claude_session`); a task row from an older
    API still has it inside `metadata`, as stored."""
    if not isinstance(doc, Mapping):
        return None
    top = _valid(doc.get(METADATA_KEY))
    if top:
        return top
    metadata = doc.get("metadata")
    return _valid(metadata.get(METADATA_KEY)) if isinstance(metadata, Mapping) else None


def owner_of_tasks(tasks: Iterable[Any]) -> str | None:
    """A workflow's tag, read off its step tasks: every one carries the
    workflow's metadata, so the first tagged one answers."""
    for task in tasks:
        found = owner_of(task)
        if found:
            return found
    return None


def ownership(owner: str | None, sid: str | None) -> str:
    """`own`, `other_session` or `untagged`."""
    if not owner:
        return "untagged"
    return "own" if sid and owner == sid else "other_session"


#: Said beside an empty candidate list when the bridge knows no session id.
UNKNOWN_NOTE = (
    f"this bridge does not know which Claude Code session it serves ({SESSION_ENV} is not set), "
    "so no SwarmCloud work counts as this session's and none is a candidate; `/sc attach <wf_id>` "
    "still attaches any workflow by hand"
)


def note(sid: str | None) -> str:
    """One line on what the scope was, for every scoped answer."""
    if not sid:
        return UNKNOWN_NOTE
    return (
        f"only work this Claude Code session started (tagged `{METADATA_KEY}` {sid}) is a candidate; "
        "other sessions' work in the same tenant, and work submitted before tagging existed, are "
        "left out"
    )


def describe(sid: str | None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`{session_id, read_from, scope}` for an answer."""
    return {"session_id": sid, "read_from": source(environ), "scope": note(sid)}


def counts() -> dict[str, int]:
    return {"other_session": 0, "untagged": 0}


def attached_note(owner: str | None, sid: str | None) -> str | None:
    """What a hand attach says about a workflow that is not this session's."""
    kind = ownership(owner, sid)
    if kind == "own":
        return None
    if kind == "other_session":
        return (f"this workflow is NOT this session's: it was started by Claude Code session {owner}. "
                "Attached because you asked by id; `/sc check` and `/sc attach --all` leave it out")
    return ("this workflow is NOT this session's: it carries no Claude Code session tag (submitted "
            "before tagging existed, or by another client). Attached because you asked by id; "
            "`/sc check` and `/sc attach --all` leave it out")
