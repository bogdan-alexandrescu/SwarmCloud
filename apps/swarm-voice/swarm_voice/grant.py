"""The Voice it session grant, as swarm-voice holds it (docs/design/voice-it.md
§3.6; swarm-api's side is swarm_api/voice/grant.py).

swarm-api hands a session's grant to swarm-voice in the internal start call,
beside the session's project id. swarm-voice keeps it in memory, on the live
session, and nowhere else: no file, no log line, no response, no exception
message, and it is never sent anywhere but swarm-api. It presents it on each
call it makes to swarm-api as the session's person (`headers`), beside its own
Google ID token, and drops it when the session ends.
"""

from __future__ import annotations

import re

GRANT_HEADER = "X-Swarm-Voice-Grant"
SESSION_HEADER = "X-Swarm-Voice-Session"

#: swarm-api mints `secrets.token_urlsafe(32)`: 43 URL-safe characters.
GRANT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PROJECT_RE = re.compile(r"^vp_[0-9a-f]{32}$")
SESSION_RE = re.compile(r"^vs_[0-9a-f]{32}$")


class GrantInvalid(ValueError):
    """The start call's grant or ids are not ones swarm-api mints. The message
    never carries the value."""


class SessionGrant:
    """One session's grant. Its repr and str name the session, never the grant."""

    __slots__ = ("project_id", "session_id", "_value")

    def __init__(self, *, project_id: str, session_id: str, value: str) -> None:
        if not PROJECT_RE.match(project_id or "") or not SESSION_RE.match(session_id or ""):
            raise GrantInvalid("the session grant names no Voice it project and session")
        if not GRANT_RE.match(value or ""):
            raise GrantInvalid("the session grant is not one swarm-api mints")
        self.project_id = project_id
        self.session_id = session_id
        self._value = value

    def headers(self) -> dict[str, str]:
        """The two headers a call to swarm-api presents beside swarm-voice's ID token."""
        return {SESSION_HEADER: f"{self.project_id}/{self.session_id}", GRANT_HEADER: self._value}

    def __repr__(self) -> str:
        return f"SessionGrant(project_id={self.project_id!r}, session_id={self.session_id!r})"

    __str__ = __repr__
