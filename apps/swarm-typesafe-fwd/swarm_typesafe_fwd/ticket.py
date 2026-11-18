"""The attempt TypeSafe ticket, verified (docs/design/workspace-secrets.md §3.5).

swarm-api mints it (`swarm_api.typesafe_ticket.mint_typesafe_ticket`, the
REFERENCE) for one attempt of one task of one tenant, naming the secret whose
value this forwarder spends; the agent pod presents it as the bearer
credential of every `POST /v1/systemone`.

    <base64url(JSON claims)>.<base64url(HMAC-SHA256(key, DOMAIN || payload))>

Both halves base64url without padding; the claims are exactly TICKET_FIELDS;
the key is `swarm-typesafe-fwd-key`, its payload read as UTF-8 and stripped
(mac_key), never swarm-egress-key or swarm-live-key. This image carries no
swarm-api, so the wire is restated here; tests/unit/typesafe_fwd/
test_typesafe_fwd_ticket.py holds it equal to the reference, byte for byte.

The MAC is checked FIRST, in constant time (hmac.compare_digest) against every
key in the ring, before a byte of the payload is parsed. Then, as defence in
depth behind it, the claims must name a secret this forwarder may read for
that tenant: `scope: tenant` names exactly `swarm-tenant-<tenant_id>-typesafe`,
`scope: user` a `swarm-tenant-u-<user>-typesafe`, and the version is digits or
`latest`. A ticket for tenant A naming tenant B's key is refused here even if
a key that MAC'd it were somehow the right one.

Nothing here puts a ticket, a key or any part of either in an exception.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

#: The MAC's domain; `swarm_api.typesafe_ticket.TICKET_DOMAIN` is the reference.
TICKET_DOMAIN = b"swarm-typesafe-ticket/v1\0"
TICKET_FIELDS = frozenset({"tenant_id", "task_id", "attempt_id", "generation", "scope",
                           "secret_id", "secret_version", "exp", "nonce"})
#: The longest life a ticket may claim (§3.5). swarm-api clamps what it mints
#: to this, so a ticket claiming more was not minted by a correct swarm-api.
MAX_TICKET_SECONDS = 900
#: The reference allows one second of rounding past MAX_TICKET_SECONDS; so
#: does this, and no more: swarm-api and this pod read one clock source (NTP).
SKEW_SECONDS = 1
#: A ticket is a few hundred characters; anything far longer is not one.
MAX_TICKET_CHARS = 2048
SCOPES = ("tenant", "user")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_TENANT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_USER_SECRET = re.compile(r"swarm-tenant-u-[a-z0-9][a-z0-9-]{0,62}-typesafe")
_VERSION = re.compile(r"[0-9]{1,19}|latest")
_NONCE = re.compile(r"[0-9a-f]{16}")
_B64 = re.compile(r"[A-Za-z0-9_-]+")


class TicketRefused(Exception):
    """A ticket that does not verify. `cause` is a short word for the log; the
    message never carries the ticket."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True)
class Ticket:
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int
    scope: str
    secret_id: str
    secret_version: str
    exp: int


def mac_key(payload: bytes) -> bytes:
    """The HMAC key from the secret's payload: UTF-8, surrounding whitespace
    stripped, as swarm-api reads SWARM_TYPESAFE_FWD_KEY."""
    return payload.decode("utf-8").strip().encode("utf-8")


def _unb64(text: str) -> bytes:
    if not _B64.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, payload: str) -> bytes:
    return hmac.new(key, TICKET_DOMAIN + payload.encode("ascii"), hashlib.sha256).digest()


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def verify_ticket(keys: Sequence[bytes], raw: Any, *, now: float) -> Ticket:
    """The ticket `raw` names, when one of `keys` made it, it is live, and it
    names a secret this forwarder may read for its tenant."""
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
        if key and hmac.compare_digest(_mac(key, payload), presented):
            matched = True
    if not matched:
        raise TicketRefused("bad_mac")
    try:
        claims = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    if not isinstance(claims, dict) or set(claims) != TICKET_FIELDS:
        raise TicketRefused("malformed")
    for name in ("task_id", "attempt_id"):
        if not isinstance(claims[name], str) or not _ID.fullmatch(claims[name]):
            raise TicketRefused("malformed")
    tenant_id = claims["tenant_id"]
    if not isinstance(tenant_id, str) or not _TENANT.fullmatch(tenant_id):
        raise TicketRefused("malformed")
    if not isinstance(claims["nonce"], str) or not _NONCE.fullmatch(claims["nonce"]):
        raise TicketRefused("malformed")
    generation, exp = claims["generation"], claims["exp"]
    if not _int(generation) or not _int(exp):
        raise TicketRefused("malformed")
    if exp <= now:
        raise TicketRefused("expired")
    if exp > now + MAX_TICKET_SECONDS + SKEW_SECONDS:
        raise TicketRefused("too_long")
    scope, secret_id, version = claims["scope"], claims["secret_id"], claims["secret_version"]
    if scope not in SCOPES or not isinstance(secret_id, str) or not isinstance(version, str):
        raise TicketRefused("secret_refused")
    if scope == "tenant" and secret_id != f"swarm-tenant-{tenant_id}-typesafe":
        raise TicketRefused("secret_refused")
    if scope == "user" and not _USER_SECRET.fullmatch(secret_id):
        raise TicketRefused("secret_refused")
    if not _VERSION.fullmatch(version):
        raise TicketRefused("secret_refused")
    return Ticket(tenant_id=tenant_id, task_id=claims["task_id"],
                  attempt_id=claims["attempt_id"], generation=generation, scope=scope,
                  secret_id=secret_id, secret_version=version, exp=exp)


__all__ = ["MAX_TICKET_SECONDS", "TICKET_DOMAIN", "TICKET_FIELDS", "Ticket", "TicketRefused",
           "mac_key", "verify_ticket"]
