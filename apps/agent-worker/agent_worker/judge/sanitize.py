"""What a Jev call may carry (context-quality.md §5.4), applied before every send.

The rules live in the shared package `swarm_jev.sanitize` (apps/jev-client;
docs/design/agent-quality/jev-contract.md section 13, lane 1), which swarm-api
installs too. This module hands out the package's OWN objects, not copies, and
adds the one thing only the worker has: its publish predicate
(`lifecycle._credential_in`, the one `agent_worker.publish_scan` runs), asked
at its STRICTEST tier (the path it is given is not a test path), so a string
the publish gate would refuse is dropped from every Jev call in this process,
as it was before the move.

Each name is bound by assignment rather than `from ... import` so that
docs/jev-data-flow.md's cites of this file still resolve to a definition.
"""

from __future__ import annotations

from swarm_jev import sanitize as _shared

from agent_worker.lifecycle import _credential_in

DENIED_NAME_GLOBS = _shared.DENIED_NAME_GLOBS
DENIED_DIRECTORIES = _shared.DENIED_DIRECTORIES
IDENTITY_KEYS = _shared.IDENTITY_KEYS
NEVER_SENT_KEYS = _shared.NEVER_SENT_KEYS
Sanitized = _shared.Sanitized
is_denied_path = _shared.is_denied_path
is_flagged = _shared.is_flagged
sanitize_state = _shared.sanitize_state
add_credential_check = _shared.add_credential_check
credential_checks = _shared.credential_checks
_PATH_KEYS = _shared._PATH_KEYS
_hard_stop_hit = _shared._hard_stop_hit
_Walker = _shared._Walker


def publish_gate_flags(text: str) -> bool:
    """The worker's publish predicate as a scan: True when the gate would
    refuse `text` at its strictest tier."""
    return _credential_in("", text) is not None


add_credential_check(publish_gate_flags)

__all__ = [
    "DENIED_DIRECTORIES", "DENIED_NAME_GLOBS", "IDENTITY_KEYS", "NEVER_SENT_KEYS",
    "Sanitized", "add_credential_check", "credential_checks", "is_denied_path",
    "is_flagged", "publish_gate_flags", "sanitize_state",
]
