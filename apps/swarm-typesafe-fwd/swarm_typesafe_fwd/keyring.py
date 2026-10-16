"""The MAC key ring and the TypeSafe key cache (docs/design/workspace-secrets.md §3.5).

KEY RING. `swarm-typesafe-fwd-key`'s current value and the previous one. main.py
re-reads `latest` every SECRET_POLL_SECONDS; when it changes, the previous key
stays in the ring UNTIL THE NEXT CHANGE (not for a fixed time), so a ticket a
not-yet-rolled swarm-api instance minted still verifies for its life and
longer. Rotation is: add the version, wait one poll, roll swarm-api, then
disable the old version by hand. A key two changes old is out of the ring.

KEY CACHE. A ticket names a secret by id and version; the value is read from
`projects/<project>/secrets/<secret_id>/versions/<version>`, where `project`
is the forwarder's own (SWARM_TYPESAFE_FWD_PROJECT) and NEVER a ticket's, so no
ticket reaches another project. The id and version were validated by
ticket.verify_ticket, so neither can carry a `/`. A value is held in memory for
at most VALUE_TTL_SECONDS: deleting a key (which destroys its versions) stops
it within a minute. Never written to a file, an environment variable or a log.

Neither class has a repr that carries a value.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

#: §3.5: "A value read is held in memory for at most 60 s".
VALUE_TTL_SECONDS = 60.0
#: A provider key is tens of characters; anything this long is not one.
MAX_VALUE_BYTES = 4096


class KeyUnreadable(Exception):
    """The named version could not be read. The message is a cause word; it
    never carries the name's value or Secret Manager's answer body."""


class KeyRing:
    def __init__(self, current: bytes) -> None:
        if not current:
            raise ValueError("swarm-typesafe-fwd-key is empty")
        self._lock = threading.Lock()
        self._current = current
        self._previous: bytes | None = None

    def __repr__(self) -> str:
        return "KeyRing(<redacted>)"

    def rotate(self, new: bytes) -> bool:
        with self._lock:
            if not new or new == self._current:
                return False
            self._previous, self._current = self._current, new
            return True

    def keys(self) -> list[bytes]:
        with self._lock:
            return [self._current] + ([self._previous] if self._previous else [])


class SecretCache:
    def __init__(self, access: Callable[[str], bytes], *, project: str,
                 clock: Callable[[], float] = time.time,
                 ttl: float = VALUE_TTL_SECONDS) -> None:
        if not project or "/" in project:
            raise ValueError("SWARM_TYPESAFE_FWD_PROJECT is a project id")
        self._access = access
        self._project = project
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str], tuple[str, float]] = {}

    def __repr__(self) -> str:
        return f"SecretCache(project={self._project!r}, cached={len(self._values)})"

    def name(self, secret_id: str, version: str) -> str:
        return f"projects/{self._project}/secrets/{secret_id}/versions/{version}"

    def value(self, secret_id: str, version: str) -> str:
        now = self._clock()
        with self._lock:
            for key in [k for k, (_, at) in self._values.items() if now - at >= self._ttl]:
                del self._values[key]
            held = self._values.get((secret_id, version))
        if held is not None:
            return held[0]
        try:
            raw = self._access(self.name(secret_id, version))
        except Exception:  # noqa: BLE001 - any failure is the same refusal
            raise KeyUnreadable("unreadable") from None
        if not isinstance(raw, bytes) or len(raw) > MAX_VALUE_BYTES:
            raise KeyUnreadable("invalid")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise KeyUnreadable("invalid") from None
        # A header value: one printable ASCII line, never a CR/LF that would
        # split the request the forwarder sends.
        if not value or not value.isascii() or not value.isprintable():
            raise KeyUnreadable("invalid")
        with self._lock:
            self._values[(secret_id, version)] = (value, now)
        return value

    def held(self) -> list[str]:
        """Every value in memory, for the redaction filter's literal list."""
        with self._lock:
            return [v for v, _ in self._values.values()]


__all__ = ["KeyRing", "KeyUnreadable", "SecretCache", "VALUE_TTL_SECONDS"]
