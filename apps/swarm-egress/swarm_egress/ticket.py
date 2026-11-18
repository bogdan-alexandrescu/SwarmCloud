"""The attempt egress ticket (docs/browser-egress.md §2.2 hop 3, §3.1).

swarm-api mints it (`swarm_api.egress.mint_egress_ticket`) for one attempt of
one task of one tenant, at one fencing generation, with an expiry; the gate's
local forwarder puts it on every CONNECT as `Proxy-Authorization: Bearer
<ticket>`, and swarm-egress verifies it here before it dials the vendor.

    <base64url(JSON claims)>.<base64url(HMAC-SHA256(key, DOMAIN || payload))>

The claims are exactly TICKET_FIELDS. The key is `swarm-egress-key`, a
platform secret of its own (terraform/infra/egress.tf): never swarm-live-key,
so a process that can verify live tickets cannot mint egress ones, and the
reverse. The MAC's domain differs from the live ticket's as well, so even one
key value would never make one ticket verify as the other.

Not one-use: a ticket is presented on every CONNECT for its life, and the
worker renews it before it expires (§2.5). What keeps a copied ticket small
is its scope (one attempt), its life (at most MAX_TICKET_SECONDS) and where it
works (only inside the VPC, only from a labelled pod: the NetworkPolicies).

Nothing here puts a ticket, or any part of one, in an exception message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: The MAC's domain. `swarm_api.egress.EGRESS_TICKET_DOMAIN` restates it; a
#: test holds the two equal.
TICKET_DOMAIN = b"swarm-egress-ticket/v1\0"
TICKET_FIELDS = frozenset({"tenant_id", "task_id", "attempt_id", "generation", "exp", "nonce"})
#: The longest life swarm-egress accepts. swarm-api clamps what it mints to
#: this (`swarm_api.egress.MAX_TICKET_SECONDS`), so a ticket claiming more was
#: not minted by a correct swarm-api. Fifteen minutes: long enough that the
#: worker's renewal is not a hot loop, short enough that a ticket taken from a
#: compromised renderer stops working soon after its attempt does.
MAX_TICKET_SECONDS = 900
#: Clock skew allowed between swarm-api (Cloud Run) and this pod.
SKEW_SECONDS = 60
MAX_TICKET_CHARS = 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class TicketRefused(Exception):
    """A ticket that does not verify. `cause` is a short word for the log;
    the message never contains the ticket."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True)
class Ticket:
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int
    exp: int


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, payload: str) -> bytes:
    return hmac.new(key, TICKET_DOMAIN + payload.encode("ascii"), hashlib.sha256).digest()


def _signed(key: bytes, body: Mapping[str, Any]) -> str:
    payload = _b64(json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_b64(_mac(key, payload))}"


def mint_ticket(key: bytes, *, tenant_id: str, task_id: str, attempt_id: str,
                generation: int, exp: int, nonce: str | None = None) -> str:
    """A ticket; swarm-api's copy is the one that runs in production. This one
    exists for tests and for an operator's smoke check."""
    return _signed(key, {"tenant_id": tenant_id, "task_id": task_id, "attempt_id": attempt_id,
                         "generation": generation, "exp": exp,
                         "nonce": nonce or secrets.token_hex(8)})


def verify_ticket(keys: Sequence[bytes], raw: Any, *, now: float) -> Ticket:
    """The ticket `raw` names, when one of `keys` made it and it is live."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TICKET_CHARS:
        raise TicketRefused("malformed")
    parts = raw.split(".")
    if len(parts) != 2 or not all(parts):
        raise TicketRefused("malformed")
    payload, mac = parts
    try:
        presented = _unb64(mac)
        payload.encode("ascii")
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    if not any(key and hmac.compare_digest(_mac(key, payload), presented) for key in keys):
        raise TicketRefused("bad_mac")
    try:
        claims = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    if not isinstance(claims, dict) or set(claims) != TICKET_FIELDS:
        raise TicketRefused("malformed")
    for name in ("tenant_id", "task_id", "attempt_id"):
        value = claims[name]
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise TicketRefused("malformed")
    generation, exp = claims["generation"], claims["exp"]
    for value in (generation, exp):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TicketRefused("malformed")
    if exp <= now:
        raise TicketRefused("expired")
    if exp > now + MAX_TICKET_SECONDS + SKEW_SECONDS:
        raise TicketRefused("too_long")
    return Ticket(tenant_id=claims["tenant_id"], task_id=claims["task_id"],
                  attempt_id=claims["attempt_id"], generation=generation, exp=exp)


__all__ = ["MAX_TICKET_SECONDS", "TICKET_DOMAIN", "TICKET_FIELDS", "Ticket", "TicketRefused",
           "mint_ticket", "verify_ticket"]
