"""The attempt graph ticket, verified (docs/design/graph-service.md §3.2-§3.3).

swarm-api mints it (`swarm_api.graph_ticket.mint_graph_ticket`, the
REFERENCE) for one attempt of one task of one tenant, pinning one repository,
one index commit and one manifest digest; the pod's shim presents it as the
bearer credential of every `POST /mcp`.

    <base64url(JSON claims)>.<base64url(HMAC-SHA256(key, DOMAIN || payload))>

Both halves base64url without padding; the claims are exactly TICKET_FIELDS;
the key is `swarm-graph-ticket-key`, its payload read as UTF-8 and stripped
(`mac_key`), as swarm-api reads SWARM_GRAPH_TICKET_KEY. Never swarm-egress-key,
swarm-typesafe-fwd-key or swarm-live-key: a compromise of one ticket kind's
key mints no other kind. This image carries no swarm-api, so the wire is
restated here; tests/unit/graphsvc/test_graphsvc_ticket.py holds it equal to
the reference, constant for constant and ticket for ticket.

The MAC is checked FIRST, in constant time against every key in the ring,
before a byte of the payload is parsed. Then (§3.3 step 2) every claim a GCS
object name is built from is checked against its exact shape -- the tenant
id, `repo_[0-9a-f]{16}`, a 40-hex commit, `sha256:<64 hex>` -- BEFORE any path
is built from it, so even a ticket a correct key MAC'd can never name a path
outside `tenants/<t>/repos/<r>/graph/`.

THE GENERATION IS FENCED AT MINTING, NOT HERE (§3.2). This service has no
Firestore access. A stale worker cannot renew (its next mint is a 409), and
its last ticket reads only the graph the attempt was already entitled to
read, for at most MAX_TICKET_SECONDS: the trade swarm-egress and
swarm-typesafe-fwd make too.

THE STATS TICKET (docs/design/graph-answer-cache.md §5.3) is the same wire
under the same key with its OWN domain, STATS_DOMAIN, so neither kind can stand
in for the other: an attempt ticket's MAC never verifies on the stats routes,
and a stats ticket's never on `/mcp`. swarm-api mints it (lane 3) for the
console's Cache section, pinning one tenant, one repository and the current and
previous manifest digests, for MAX_STATS_TICKET_SECONDS. Its claims are exactly
STATS_TICKET_FIELDS:

    {"tenant_id", "repo_id", "manifest_digest",
     "previous_manifest_digest" (null when there is no previous version), "exp"}

`json.dumps(claims, sort_keys=True, separators=(",", ":"))`, base64url, as the
attempt ticket's. It reads no object: it only selects which of this replica's
in-memory figures are returned, every one filtered on its tenant.

Nothing here puts a ticket, a key or any part of either in an exception.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

#: The MAC's domain; `swarm_api.graph_ticket.TICKET_DOMAIN` is the reference.
TICKET_DOMAIN = b"swarm-graph-ticket/v1\0"
TICKET_FIELDS = frozenset({"tenant_id", "repo_id", "task_id", "attempt_id", "generation",
                           "index_sha", "manifest_digest", "exp", "nonce"})
#: The longest life a ticket may claim (§3.2). swarm-api clamps what it mints
#: to this, so a ticket claiming more was not minted by a correct swarm-api.
MAX_TICKET_SECONDS = 900
#: The reference allows one second of rounding past MAX_TICKET_SECONDS; so
#: does this, and no more.
SKEW_SECONDS = 1
#: After a rotation the previous key verifies for a ticket's longest life and a
#: minute more (§3.2, swarm-egress's KeyRing): a ticket a not-yet-rolled
#: swarm-api minted just before the rotation lives out its life.
PREVIOUS_KEY_SECONDS = MAX_TICKET_SECONDS + 60
#: The stats ticket's domain (graph-answer-cache.md §5.3); swarm-api's mint
#: (lane 3) is held to it.
STATS_DOMAIN = b"swarm-graph-stats/v1\0"
STATS_TICKET_FIELDS = frozenset({"tenant_id", "repo_id", "manifest_digest",
                                 "previous_manifest_digest", "exp"})
#: A stats ticket is minted for one read of the console's 30 s refresh (§5.5),
#: so it lives a minute, not an attempt's quarter hour.
MAX_STATS_TICKET_SECONDS = 60
#: A ticket is a few hundred characters; anything far longer is not one.
MAX_TICKET_CHARS = 2048
#: What the 401 tells the caller (§10: `<expired | invalid>`); the finer
#: cause goes to the log only.
EXPIRED = "expired"
INVALID = "invalid"

#: A tenant id as `swarm_common.identity` mints it (a Kubernetes-safe slug).
_TENANT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
#: `swarm_api.repositories.repo_id_for`: `repo_` and 16 hex.
_REPO_ID = re.compile(r"repo_[0-9a-f]{16}")
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{16}")
_B64 = re.compile(r"[A-Za-z0-9_-]+")
#: Task and attempt ids are only logged, never part of a path or a key, so
#: they are held to "a short string" rather than to a shape swarm-api might
#: one day widen.
_MAX_ID_CHARS = 128


class TicketRefused(Exception):
    """A ticket that does not verify. `cause` is a short word for the log; the
    message never carries the ticket."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause

    @property
    def reason(self) -> str:
        """What the caller is told: `expired`, or `invalid` for every other cause."""
        return EXPIRED if self.cause == EXPIRED else INVALID


@dataclass(frozen=True)
class GraphTicket:
    """The claims of a verified ticket: the only source of every object name
    and every cache key this service builds (§3.3)."""

    tenant_id: str
    repo_id: str
    task_id: str
    attempt_id: str
    generation: int
    index_sha: str
    manifest_digest: str
    exp: int


def mac_key(payload: bytes) -> bytes:
    """The HMAC key from the secret's payload: UTF-8, surrounding whitespace
    stripped, as swarm-api reads SWARM_GRAPH_TICKET_KEY."""
    return payload.decode("utf-8").strip().encode("utf-8")


def _unb64(text: str) -> bytes:
    if not _B64.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, payload: str, domain: bytes = TICKET_DOMAIN) -> bytes:
    return hmac.new(key, domain + payload.encode("ascii"), hashlib.sha256).digest()


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _shaped(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _claims(keys: Sequence[bytes], raw: Any, domain: bytes) -> Any:
    """The parsed payload of `raw` when one of `keys` MAC'd it under `domain`;
    the MAC is checked before a byte of the payload is parsed."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TICKET_CHARS:
        raise TicketRefused("malformed")
    parts = raw.split(".")
    if len(parts) != 2 or not all(parts):
        raise TicketRefused("malformed")
    payload, mac = parts
    try:
        presented = _unb64(mac)
        _unb64(payload)
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    # Every key is compared, so the time taken does not say which one matched.
    matched = False
    for key in keys:
        if key and hmac.compare_digest(_mac(key, payload, domain), presented):
            matched = True
    if not matched:
        raise TicketRefused("bad_mac")
    try:
        claims = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    return claims


def verify_graph_ticket(keys: Sequence[bytes], raw: Any, *, now: float) -> GraphTicket:
    """The ticket `raw` names, when one of `keys` made it, it is live, and every
    claim a path is built from has its exact shape."""
    claims = _claims(keys, raw, TICKET_DOMAIN)
    if not isinstance(claims, dict) or set(claims) != TICKET_FIELDS:
        raise TicketRefused("malformed")
    if not (_count(claims["generation"]) and _count(claims["exp"])):
        raise TicketRefused("malformed")
    exp = claims["exp"]
    if exp <= now:
        raise TicketRefused(EXPIRED)
    if exp > now + MAX_TICKET_SECONDS + SKEW_SECONDS:
        raise TicketRefused("too_long")
    # §3.3 step 2: before any path is built from them.
    if not (_shaped(claims["tenant_id"], _TENANT) and _shaped(claims["repo_id"], _REPO_ID)
            and _shaped(claims["index_sha"], _SHA)
            and _shaped(claims["manifest_digest"], _DIGEST)
            and _shaped(claims["nonce"], _NONCE)):
        raise TicketRefused("claims_refused")
    for name in ("task_id", "attempt_id"):
        value = claims[name]
        if not isinstance(value, str) or not value or len(value) > _MAX_ID_CHARS:
            raise TicketRefused("claims_refused")
    return GraphTicket(tenant_id=claims["tenant_id"], repo_id=claims["repo_id"],
                       task_id=claims["task_id"], attempt_id=claims["attempt_id"],
                       generation=claims["generation"], index_sha=claims["index_sha"],
                       manifest_digest=claims["manifest_digest"], exp=exp)


@dataclass(frozen=True)
class StatsTicket:
    """The claims of a verified stats ticket: the one tenant and repository
    whose figures a stats read may return (§5.3, invariant 9)."""

    tenant_id: str
    repo_id: str
    manifest_digest: str
    previous_manifest_digest: str | None
    exp: int


def verify_stats_ticket(keys: Sequence[bytes], raw: Any, *, now: float) -> StatsTicket:
    """The stats ticket `raw` names, when one of `keys` made it under
    STATS_DOMAIN, it is live for at most MAX_STATS_TICKET_SECONDS, and every
    claim has its exact shape."""
    claims = _claims(keys, raw, STATS_DOMAIN)
    if not isinstance(claims, dict) or set(claims) != STATS_TICKET_FIELDS \
            or not _count(claims["exp"]):
        raise TicketRefused("malformed")
    exp = claims["exp"]
    if exp <= now:
        raise TicketRefused(EXPIRED)
    if exp > now + MAX_STATS_TICKET_SECONDS + SKEW_SECONDS:
        raise TicketRefused("too_long")
    previous = claims["previous_manifest_digest"]
    if not (_shaped(claims["tenant_id"], _TENANT) and _shaped(claims["repo_id"], _REPO_ID)
            and _shaped(claims["manifest_digest"], _DIGEST)
            and (previous is None or _shaped(previous, _DIGEST))):
        raise TicketRefused("claims_refused")
    return StatsTicket(tenant_id=claims["tenant_id"], repo_id=claims["repo_id"],
                       manifest_digest=claims["manifest_digest"],
                       previous_manifest_digest=previous, exp=exp)


def bearer(authorization: str | None) -> str | None:
    """The ticket of a bearer-scheme authorization header, or None."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


class KeyRing:
    """`swarm-graph-ticket-key`'s current value and, for PREVIOUS_KEY_SECONDS
    after a rotation, the previous one (§3.2)."""

    def __init__(self, current: bytes, *, clock: Callable[[], float] = time.time) -> None:
        if not current:
            raise ValueError("swarm-graph-ticket-key is empty")
        self._lock = threading.Lock()
        self._current = current
        self._previous: bytes | None = None
        self._previous_until = 0.0
        self._clock = clock

    def __repr__(self) -> str:
        return "KeyRing(<redacted>)"

    def rotate(self, new: bytes) -> bool:
        with self._lock:
            if not new or new == self._current:
                return False
            self._previous, self._current = self._current, new
            self._previous_until = self._clock() + PREVIOUS_KEY_SECONDS
            return True

    def keys(self) -> list[bytes]:
        with self._lock:
            if self._previous is not None and self._clock() < self._previous_until:
                return [self._current, self._previous]
            return [self._current]


__all__ = [
    "EXPIRED",
    "INVALID",
    "MAX_STATS_TICKET_SECONDS",
    "MAX_TICKET_SECONDS",
    "PREVIOUS_KEY_SECONDS",
    "STATS_DOMAIN",
    "STATS_TICKET_FIELDS",
    "TICKET_DOMAIN",
    "TICKET_FIELDS",
    "GraphTicket",
    "KeyRing",
    "StatsTicket",
    "TicketRefused",
    "bearer",
    "mac_key",
    "verify_graph_ticket",
    "verify_stats_ticket",
]
