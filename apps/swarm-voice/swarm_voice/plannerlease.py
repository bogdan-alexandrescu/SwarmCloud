"""The planner's Claude subscription account, leased from the quota broker for
ONE delegation turn (docs/design/voice-it.md §3.6).

THE OWNER'S CHOICE (2 November 2026): "we have a pool of account for
anthropic, lets use one of the accounts there". The planner runs on a Claude
subscription account from the person's own workspace pool, through Claude Code
(claudeturn.py), the way a worker runs an agent: one lease per turn, so the
account counts one agent while the turn runs and none after it.

THE BROKER'S ROUTES (apps/quota-broker; the worker's are
agent_worker/accountlease.py's):

* `POST /v1/accounts/assign` `{"provider": "anthropic", "holder":
  "voice-planner", "person", "voice_session_id"}` -> `{"account_id",
  "assignment_id", "credential": {"access_token"}, "reason": "",
  "next_reset_at"}`, or, with no account free, a 200 with `account_id: null`
  and the broker's `reason`, exactly as it answers a worker. The broker takes
  the tenant from `person` (the person's own workspace, never one the body
  names) only for a caller in its VOICE_SERVICE_ACCOUNTS, stamps the hold with
  the voice session and counts it on the account like a task's.
* `POST /v1/voice/lease/{account_id}/renew` and `.../release`
  `{"assignment_id", "voice_session_id"}`: the voice hold routes, which keep
  or give back any hold stamped with that voice session. A renewal's
  credential is not read: the turn keeps the token it started with.

A FULL POOL IS A REFUSAL, NEVER A WAIT: `PoolFull` carries the broker's reason
and the voice says it. A swarm-voice that dies holds nothing for long: the
hold lapses on the broker's TTL, which a turn (planner.TURN_SECONDS) never
outlives without a renewal.

AUTHENTICATION is accountlease.VoiceBroker's: the metadata server's ID token for
the broker's audience. NOTHING SECRET LEAVES: the access token is held in a
`Bearer` (masked repr), errors carry a status and a route, never a body.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import quote

from .accountlease import VoiceBroker
from .credential import Bearer, CredentialError

log = logging.getLogger("swarm_voice.plannerlease")

PROVIDER = "anthropic"
#: Who holds the lease, as the broker records it on the hold.
HOLDER = "voice-planner"
#: The broker's voice hold lives ten minutes; a turn renews every four.
RENEW_SECONDS = 240.0

#: The broker's refusals of an assign (quota_broker.accounts.Unavailable).
NO_ACCOUNTS_REGISTERED = "no_accounts_registered"
NO_ACCOUNT_AVAILABLE = "no_account_available"


class PoolFull(Exception):
    """The broker leased no account: `reason` is its refusal."""

    def __init__(self, reason: str, next_reset_at: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.next_reset_at = next_reset_at


class LeaseFailed(CredentialError):
    """The broker could not be reached or refused the call; names a status only."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class PlannerLease:
    """One turn's hold. `bearer` reveals the token only on request."""

    account_id: str
    assignment_id: str
    voice_session_id: str
    bearer: Bearer
    released: bool = False

    def __repr__(self) -> str:
        return f"PlannerLease(account_id={self.account_id!r}, voice_session_id={self.voice_session_id!r})"


class PlannerAccounts:
    """Assign, renew and release one turn's account, over VoiceBroker's client."""

    def __init__(self, broker: VoiceBroker, *, renew_seconds: float = RENEW_SECONDS) -> None:
        self._broker = broker
        self.renew_seconds = renew_seconds

    async def assign(self, *, person: str, voice_session_id: str) -> PlannerLease:
        status, body = await self._broker.call("/v1/accounts/assign", {
            "provider": PROVIDER, "holder": HOLDER, "person": person,
            "voice_session_id": voice_session_id})
        if status != 200 or not isinstance(body, dict):
            # The status only: an error body may quote the request.
            raise LeaseFailed(f"the quota broker refused the planner's account lease (HTTP {status})", status)
        if not body.get("account_id") or not body.get("assignment_id"):
            nxt = body.get("next_reset_at")
            raise PoolFull(str(body.get("reason") or NO_ACCOUNT_AVAILABLE), nxt if isinstance(nxt, str) else None)
        cred = body.get("credential")
        token = cred.get("access_token") if isinstance(cred, dict) else None
        lease = PlannerLease(account_id=str(body["account_id"]), assignment_id=str(body["assignment_id"]),
                             voice_session_id=voice_session_id, bearer=Bearer(token or ""))
        if not isinstance(token, str) or not token:
            # Held but unusable: give it back at once, so the account does not
            # count an agent that never ran.
            await self.release(lease)
            raise LeaseFailed("the quota broker's planner lease carried no credential")
        return lease

    def _hold_path(self, lease: PlannerLease, verb: str) -> str:
        return f"/v1/voice/lease/{quote(lease.account_id, safe='')}/{verb}"

    def _hold_body(self, lease: PlannerLease) -> dict[str, Any]:
        return {"assignment_id": lease.assignment_id, "voice_session_id": lease.voice_session_id}

    async def renew(self, lease: PlannerLease) -> None:
        status, _ = await self._broker.call(self._hold_path(lease, "renew"), self._hold_body(lease))
        if status != 200:
            raise LeaseFailed(f"the quota broker refused the planner's renewal (HTTP {status})", status)

    async def release(self, lease: PlannerLease) -> None:
        """Give the account back. Idempotent; a failure is logged, never raised:
        the hold then lapses on the broker's TTL."""
        if lease.released:
            return
        lease.released = True
        try:
            status, _ = await self._broker.call(self._hold_path(lease, "release"), self._hold_body(lease))
        except CredentialError as exc:
            log.warning("voice session %s: planner lease release failed (the hold expires on its own): %s",
                        lease.voice_session_id, exc)
            return
        if status != 200:
            log.warning("voice session %s: planner lease release answered HTTP %s (the hold expires on "
                        "its own)", lease.voice_session_id, status)

    async def _renew_loop(self, lease: PlannerLease) -> None:
        while not lease.released:
            await asyncio.sleep(self.renew_seconds)
            try:
                await self.renew(lease)
            except CredentialError as exc:
                log.warning("voice session %s: planner lease renewal failed: %s", lease.voice_session_id, exc)

    @contextlib.asynccontextmanager
    async def lease(self, *, person: str, voice_session_id: str) -> AsyncIterator[PlannerLease]:
        """Hold one account for the body of the `async with`, renewed while it
        runs and released in a finally, whatever the turn did."""
        held = await self.assign(person=person, voice_session_id=voice_session_id)
        renewer = asyncio.create_task(self._renew_loop(held), name=f"planner-lease-{voice_session_id}")
        try:
            yield held
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewer
            await self.release(held)
