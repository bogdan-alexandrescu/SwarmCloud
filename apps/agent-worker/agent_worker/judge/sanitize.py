"""What a Jev call may carry (context-quality.md §5.4), applied before every send.

In order, over every string in the state, wherever it sits:

1. **Path deny.** An object whose `path` (or `file`) is a secret-shaped path
   or one of the repository's `hard_stop_paths` keeps its identity only --
   `path`, `symbol`, `i` and the like. Its content is never read into a call.
2. **Credential scan, then DROP.** The worker's publish predicate
   (`lifecycle._credential_in`, the one `agent_worker.publish_scan` runs) and
   `swarm_redaction.redact`'s rules. A hit removes the whole string from its
   object or list. It is not masked: a masked key's neighbourhood can still
   identify it, and a half-sent excerpt is worth less than none.
3. **The task's registered secrets** (`redact.scrub_text`'s values): a string
   holding one is dropped the same way.
4. **Never sent:** author identities and emails, environment variables, the
   agent's transcript. Their keys are removed wherever they appear.

The credential scan is asked at its STRICTEST tier (the path it is given is
not a test path). The publish gate tiers by path because a test fixture must
be committable; nothing here needs to be sent, so a placeholder that only a
test path would excuse is dropped too.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

from swarm_redaction.rules import redact as _rules_redact

from agent_worker.lifecycle import _credential_in

#: §5.4 step 1: file names whose content is never read.
DENIED_NAME_GLOBS = (".env", ".env*", "*.pem", "*.key", "*.p12", "*.tfvars", "*.tfstate*")
#: §5.4 step 1: nothing under a directory with one of these names.
DENIED_DIRECTORIES = frozenset({"secrets", "credentials"})
#: The keys an object keeps when its path is denied: names, never content.
IDENTITY_KEYS = frozenset({"i", "kind", "path", "file", "symbol", "name", "line", "step_id"})
#: §5.4 step 5: never sent, at any depth.
NEVER_SENT_KEYS = frozenset({
    "author", "authors", "email", "emails", "user", "login",
    "env", "environ", "environment", "transcript",
})
_PATH_KEYS = ("path", "file")


@dataclass(frozen=True)
class Sanitized:
    """The state to send (None when nothing survived), and what was removed."""

    state: Any
    #: Strings dropped by the credential scan or a registered secret.
    dropped: int = 0
    #: Objects reduced to their identity by a denied path.
    denied: int = 0


def _hard_stop_hit(path: str, pattern: str) -> bool:
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def is_denied_path(path: str, hard_stop_paths: Iterable[str] = ()) -> bool:
    """Whether a file's CONTENT may never be sent; its name still may."""
    clean = path.strip()
    while clean.startswith("./"):
        clean = clean[2:]
    parts = PurePosixPath(clean).parts
    if not parts:
        return False
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, glob) for glob in DENIED_NAME_GLOBS):
        return True
    if any(part in DENIED_DIRECTORIES for part in parts[:-1]):
        return True
    return any(_hard_stop_hit(clean, p) for p in hard_stop_paths)


def is_flagged(text: str, secrets: Iterable[str] = ()) -> bool:
    """Whether `text` must not be sent at all: it holds a registered secret or
    something credential-shaped."""
    if not text:
        return False
    for secret in secrets:
        if secret and secret in text:
            return True
    if _credential_in("", text) is not None:
        return True
    return _rules_redact(text).count > 0


class _Walker:
    def __init__(self, hard_stop_paths: Iterable[str], secrets: Iterable[str]) -> None:
        self.hard_stop = tuple(p for p in hard_stop_paths if isinstance(p, str))
        self.secrets = tuple(s for s in secrets if isinstance(s, str) and s)
        self.dropped = 0
        self.denied = 0

    _DROP = object()

    def value(self, value: Any) -> Any:
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            if is_flagged(value, self.secrets):
                self.dropped += 1
                return self._DROP
            return value
        if isinstance(value, dict):
            return self.mapping(value)
        if isinstance(value, (list, tuple)):
            out = [self.value(v) for v in value]
            return [v for v in out if v is not self._DROP]
        return self._DROP  # nothing that is not JSON is sent

    def mapping(self, value: dict) -> dict:
        keys = [k for k in value if isinstance(k, str) and k.lower() not in NEVER_SENT_KEYS]
        denied = any(
            isinstance(value.get(k), str) and is_denied_path(value[k], self.hard_stop)
            for k in _PATH_KEYS
        )
        if denied:
            self.denied += 1
            keys = [k for k in keys if k in IDENTITY_KEYS]
        out: dict[str, Any] = {}
        for key in keys:
            if is_flagged(key, self.secrets):
                self.dropped += 1
                continue
            kept = self.value(value[key])
            if kept is not self._DROP:
                out[key] = kept
        return out


def sanitize_state(state: Any, *, hard_stop_paths: Iterable[str] = (),
                   secrets: Iterable[str] = ()) -> Sanitized:
    """`state` with §5.4 applied. A string that parses as a JSON object or list
    (the agent's `jev_decide` may pass either) is sanitised as that structure."""
    if isinstance(state, str):
        try:
            parsed = json.loads(state)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            state = parsed
    walker = _Walker(hard_stop_paths, secrets)
    out = walker.value(state)
    return Sanitized(None if out is _Walker._DROP else out, walker.dropped, walker.denied)
