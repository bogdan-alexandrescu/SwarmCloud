"""The attempt egress ticket, the worker's half (docs/browser-egress.md §2.5; lane
EGRESS-GATE, part of #1030).

swarm-api mints the ticket (`POST /v1/attempts/{attempt_id}/egress-ticket`,
`swarm_api.egress`) for the worker, proven by the attempt key in this heap
under its own signing purpose; the gate's local forwarder presents it to
swarm-egress on every CONNECT (`livegate/egress.py`). The worker is the only
holder of the attempt key, so it asks, hands the ticket to the gate on
ctl.sock (`egress_ticket`) and renews it before it expires.

WHEN. Only when the gate says its browser is proxied (`status` -> `egress.route
== "proxy"`): the dispatcher's decision, made once per attempt and written in
the sidecar's environment, is read from the gate rather than restated here.
A direct browser is never asked about egress at all.

FAIL CLOSED (owner decision Q7). When no ticket can be had -- swarm-api
refuses (the switch was turned off, the attempt is fenced, the deployment has
no key), the worker has no attempt key or cannot protect its heap -- the
worker tells the gate `{"failed": cause}`, and the gate keeps refusing every
CONNECT and puts a page on screen saying why. The browser is NEVER sent
direct. A transient failure (swarm-api unreachable, a 5xx, a 429) is tried
again with a backoff until the ticket in hand expires, after which the gate
refuses CONNECTs on its own (`ticket_expired`).

THE AGENT IS TOLD in its prompt (`notice`, via `handoff.EGRESS_NOTICE_NAME`)
and the attempt's events carry the cause (`handoff.HandoffPath`'s note).

NOTHING HERE LOGS a ticket, a signature or a key. Log lines carry codes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

#: `swarm_api.egress.EGRESS_TICKET_PURPOSE`, restated (this image does not
#: carry swarm-api); tests/unit/worker/test_egress_gate_worker.py holds them equal.
EGRESS_TICKET_PURPOSE = "swarm-egress-ticket-request/v1"

#: Renew once this fraction of the ticket's life is left: a 600 s ticket is
#: renewed at 200 s left, which leaves several worker polls and a backoff or
#: two for a swarm-api blip before the gate would refuse anything.
RENEW_FRACTION = 1 / 3
#: A transient failure is tried again after this, doubling to the max.
RETRY_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0

#: The gate's states (`livegate.egress.LocalForwarder`).
PROXY = "proxy"

#: Why no ticket can be had, as the gate and the agent are told it.
NO_ATTEMPT_KEY = "no_attempt_key"
WORKER_UNPROTECTED = "worker_unprotected"
MINT_REFUSED = "mint_refused"
MINT_FAILED = "mint_failed"
FORWARDER_UNREACHABLE = "forwarder_unreachable"

#: swarm-api's codes that say no ticket will come for this attempt. Each new
#: egress refusal ships switched off under an established code (`conflict`,
#: `forbidden`; `swarm_api.refusals`), so those count as the same.
_PERMANENT = {
    "egress_off": "egress_off",
    "egress_fenced": "egress_fenced",
    "egress_unproven": MINT_REFUSED,
    "egress_unauthenticated": MINT_REFUSED,
    "egress_unavailable": "egress_unavailable",
    "conflict": MINT_REFUSED,
    "forbidden": MINT_REFUSED,
}

_NOTICE = """\
# The browser's internet access is stopped

This attempt's live browser is set to reach websites only through SwarmCloud's
egress proxy, and that path is unavailable ({cause}: {why}). SwarmCloud does
not fall back to a direct connection, so pages will not load in the browser
(you will see connection errors such as ERR_TUNNEL_CONNECTION_FAILED). Do not
keep retrying page loads: finish what you can without the browser and say in
your answer that browsing was unavailable, naming `{cause}`.
"""

_WHY = {
    "egress_off": "SwarmCloud's switch for proxied browsing is off for this attempt",
    "egress_fenced": "this attempt is no longer the task's current one",
    "egress_unavailable": "this deployment has no egress key",
    MINT_REFUSED: "SwarmCloud refused this attempt an egress ticket",
    MINT_FAILED: "SwarmCloud could not be asked for an egress ticket",
    NO_ATTEMPT_KEY: "the worker has no attempt key to ask with",
    WORKER_UNPROTECTED: "the worker cannot protect a ticket in its memory",
    FORWARDER_UNREACHABLE: "the egress forwarder cannot be reached",
}


class TicketRefused(Exception):
    """No ticket: `cause` is a word, `retryable` whether asking again may help."""

    def __init__(self, cause: str, *, retryable: bool) -> None:
        super().__init__(cause)
        self.cause = cause
        self.retryable = retryable


def mint(cfg: Any, key: Any, api: Any, *, now: float) -> tuple[str, int]:
    """Ask swarm-api for this attempt's egress ticket: `(ticket, expires_in)`.

    The body is `/live-session`'s; the signature is under EGRESS_TICKET_PURPOSE.
    """
    from .handoff import sign_headers

    body = json.dumps(
        {
            "tenant_id": cfg.tenant_id,
            "task_id": cfg.task_id,
            "attempt_id": cfg.attempt_id,
            "lease_id": cfg.lease_id,
            "generation": cfg.generation,
        }
    ).encode("utf-8")
    path = f"/v1/attempts/{quote(cfg.attempt_id, safe='')}/egress-ticket"
    try:
        headers = sign_headers(key, EGRESS_TICKET_PURPOSE, "POST", path, body, now=now)
        _, payload = api.call("POST", path, body=body, headers=headers)
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        if code in _PERMANENT:
            raise TicketRefused(_PERMANENT[code], retryable=False) from None
        retryable = getattr(exc, "retryable", None)
        status = getattr(exc, "status", None)
        if retryable is False and isinstance(status, int) and 400 <= status < 500:
            raise TicketRefused(MINT_REFUSED, retryable=False) from None
        raise TicketRefused(MINT_FAILED, retryable=True) from None
    payload = payload if isinstance(payload, Mapping) else {}
    ticket = payload.get("ticket")
    seconds = payload.get("expires_in")
    if payload.get("route") != PROXY or not isinstance(ticket, str) or not ticket \
            or isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise TicketRefused(MINT_FAILED, retryable=True)
    return ticket, seconds


def notice(cause: str) -> str:
    """What the agent is told when its browser has no egress."""
    return _NOTICE.format(cause=cause, why=_WHY.get(cause, "the egress path is unavailable"))


class BrowserEgress:
    """One attempt's egress ticket upkeep. Every method never raises."""

    def __init__(self, cfg: Any, *, logger: Any, wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.log = logger
        self._wall = wall
        #: Whether the gate's browser is proxied; None until the gate is asked.
        self.proxied: bool | None = None
        #: `ready` with a ticket handed over, `failed` with `cause`, or None.
        self.state: str | None = None
        self.cause: str | None = None
        self.renewals = 0
        self._renew_at = 0.0
        self._expires_at = 0.0
        self._retry_at = 0.0
        self._backoff = RETRY_SECONDS
        #: A permanent refusal: nothing more is asked this attempt.
        self._ended = False

    def wanted(self, gate: Any) -> bool:
        """Whether the gate's browser is proxied, asked once."""
        if self.proxied is None:
            try:
                status = gate.call("status", {})
            except Exception:  # a gate that cannot answer gives no ticket anywhere
                return False
            egress = status.get("egress") if isinstance(status, Mapping) else None
            self.proxied = isinstance(egress, Mapping) and egress.get("route") == PROXY
        return bool(self.proxied)

    def start(self, gate: Any, key: Any, api: Any) -> str | None:
        """The first ticket, before the agent exists. The cause when there is none."""
        if not self.wanted(gate):
            return None
        self._ask(gate, key, api)
        return self.cause if self.state == "failed" else None

    def unavailable(self, gate: Any, cause: str) -> str | None:
        """No ticket can be asked for (no key, an unprotected heap): the gate refuses all."""
        if gate is None or not self.wanted(gate):
            return None
        self._ended = True
        self._fail(gate, cause)
        return cause

    def keep(self, gate: Any, key: Any, api: Any) -> str | None:
        """Renew the ticket when it is due; on a poll. A new failure's cause, or None."""
        if not self.proxied or self._ended or gate is None or key is None or api is None:
            return None
        now = self._wall()
        if now < self._renew_at or now < self._retry_at:
            return None
        before = (self.state, self.cause)
        self._ask(gate, key, api)
        if self.state == "failed" and (self.state, self.cause) != before:
            return self.cause
        return None

    def notice(self) -> str | None:
        """The agent's notice while egress is failed, else None."""
        if self.proxied and self.state == "failed" and self.cause:
            return notice(self.cause)
        return None

    # -- internals -----------------------------------------------------------------

    def _ask(self, gate: Any, key: Any, api: Any) -> None:
        now = self._wall()
        try:
            ticket, seconds = mint(self.cfg, key, api, now=now)
        except TicketRefused as refused:
            if refused.retryable and now < self._expires_at:
                # The ticket in hand still works: ask again later, change nothing.
                self._retry_later(refused.cause)
                return
            if refused.retryable:
                self._retry_later(refused.cause)
            else:
                self._ended = True
            self._fail(gate, refused.cause)
            return
        try:
            answer = gate.call("egress_ticket", {"ticket": ticket, "expires_in": seconds})
        except Exception as exc:
            self.log.warning("the gate did not take the egress ticket", error=type(exc).__name__)
            self._retry_later(type(exc).__name__)
            return
        finally:
            ticket = ""
        self._expires_at = now + seconds
        self._renew_at = now + seconds * (1 - RENEW_FRACTION)
        self._retry_at = 0.0
        self._backoff = RETRY_SECONDS
        if isinstance(answer, Mapping) and answer.get("egress") == "failed":
            # The gate holds the ticket but cannot reach swarm-egress: its
            # CONNECTs fail closed; the next renewal probes again.
            self.state, self.cause = "failed", str(answer.get("cause") or FORWARDER_UNREACHABLE)
            self.log.warning("browser egress unavailable; the browser stays proxied and fails closed",
                             cause=self.cause)
            return
        if self.state == "ready":
            self.renewals += 1
        self.state, self.cause = "ready", None
        self.log.info("browser egress ticket handed to the gate", expires_in=seconds,
                      renewals=self.renewals)

    def _fail(self, gate: Any, cause: str) -> None:
        self.state, self.cause = "failed", cause
        self.log.warning("browser egress unavailable; the browser stays proxied and fails closed",
                         cause=cause)
        try:
            gate.call("egress_ticket", {"failed": cause})
        except Exception as exc:  # the gate refuses CONNECTs without a ticket either way
            self.log.warning("the gate was not told egress failed", error=type(exc).__name__)

    def _retry_later(self, cause: str) -> None:
        self._retry_at = self._wall() + self._backoff
        self.log.warning("browser egress ticket not renewed; trying again", code=cause,
                         retry_seconds=int(self._backoff))
        self._backoff = min(self._backoff * 2, RETRY_MAX_SECONDS)


__all__ = ["EGRESS_TICKET_PURPOSE", "BrowserEgress", "TicketRefused", "mint", "notice"]
