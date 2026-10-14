"""The Claude Code session a submission came from (SC-SESSION-SCOPE, owner
decision 2026-10-14).

The sc plugin's bridge tags everything it submits with the Claude Code session
that asked for it, so that `/sc check`, `/sc attach --all`, the SessionStart
hook and the watch consider only that session's work -- never another
session's in the same tenant. On a task and a workflow the tag is caller
metadata, `metadata.claude_session`, stored and passed through like any other
free-form key (the frozen `Task.metadata`; a workflow's metadata is written
onto every step task, the frozen `Workflow` having none). An issue run has no
metadata a caller writes, so `RunCreate.claude_session` carries it and the run
stores it.

WORK THE PLATFORM STARTS ON A TAGGED WORKFLOW'S BEHALF COPIES THE TAG: the
issue run's planner task, its compiled workflow, its CI-fix rounds and its
merge workflow (`issueruns`, `issueci`), and the merge step's conflict-fix and
CI-fix rounds (`mergewake`), so a session's own fix rounds count as its own.

The value is an identifier, not a credential, but it is caller-written, so
every route serves it through the task's masker (`codec.claude_session_of`).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: The metadata key, and the key every route serves it under.
KEY = "claude_session"

#: A Claude Code session id is a UUID; anything id-shaped is kept, so a
#: future format is not dropped. `schemas.RunCreate` holds the same pattern.
PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHAPE = re.compile(PATTERN)


def valid(value: Any) -> str | None:
    """`value` when it is a session id, else None."""
    return value if isinstance(value, str) and _SHAPE.match(value) else None


def of(metadata: Any) -> str | None:
    """The session a metadata map was tagged with, or None."""
    return valid(metadata.get(KEY)) if isinstance(metadata, Mapping) else None


def tag(value: Any) -> dict[str, str]:
    """`{claude_session: value}` to merge into metadata, or `{}`."""
    sid = valid(value)
    return {KEY: sid} if sid else {}


def inherit(metadata: Any) -> dict[str, str]:
    """The tag of a parent's metadata, for a child the platform starts."""
    return tag(of(metadata))
