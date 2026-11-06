"""What a Jev call may carry (context-quality.md §5.4), applied before every send.

The rules live in the shared package `swarm_jev.sanitize` (apps/jev-client;
docs/design/agent-quality/jev-contract.md section 13, lane 1), which swarm-api
installs too. This module hands out the package's OWN objects, not copies.
The credential check the shared scan asks is the publish gate's own
(`swarm_redaction.credentials`, which `lifecycle._credential_in` is), at its
STRICTEST tier, so a string the publish gate would refuse is dropped from
every Jev call -- the worker's and swarm-api's alike (the owner, 4 November
2026). The worker no longer adds it at import: it is there by default.

Each name is bound by assignment rather than `from ... import` so that
docs/jev-data-flow.md's cites of this file still resolve to a definition.
"""

from __future__ import annotations

from swarm_jev import sanitize as _shared

DENIED_NAME_GLOBS = _shared.DENIED_NAME_GLOBS
DENIED_DIRECTORIES = _shared.DENIED_DIRECTORIES
IDENTITY_KEYS = _shared.IDENTITY_KEYS
NEVER_SENT_KEYS = _shared.NEVER_SENT_KEYS
Sanitized = _shared.Sanitized
is_denied_path = _shared.is_denied_path
is_flagged = _shared.is_flagged
sanitize_state = _shared.sanitize_state
is_credential = _shared.is_credential
_PATH_KEYS = _shared._PATH_KEYS
_hard_stop_hit = _shared._hard_stop_hit
_Walker = _shared._Walker

__all__ = [
    "DENIED_DIRECTORIES", "DENIED_NAME_GLOBS", "IDENTITY_KEYS", "NEVER_SENT_KEYS",
    "Sanitized", "is_credential", "is_denied_path", "is_flagged", "sanitize_state",
]
