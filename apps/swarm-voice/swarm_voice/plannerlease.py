"""The planner's Claude subscription account, leased from the quota broker for
ONE delegation turn (docs/design/voice-it.md §3.6), and own speech's fast
front's, leased for the WHOLE session (docs/design/voice-it-own-speech.md
§14.1): the same routes, told apart by `holder`.

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

THE FALLBACK ORDER (docs/design/llm-providers.md §5.3). The assign says it
can run on a key (`key_tier`), so the broker walks the person's workspace's
plans, then the Anthropic keys shared with Voice it, and names the `tier` it
granted; there is no tier 3 (Q2): with both spent the planner refuses, as it
does today. A lease on a key hands its token over as `claudeturn.ApiKey`, so
the turn's Claude Code is given ANTHROPIC_API_KEY in place of the plan's
token. `move` gives a spent account back and assigns again with it excluded,
which is the walk minus that account.

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

from .accountlease import TIER_PLAN, VoiceBroker, tier_of
from .claudeturn import ApiKey
from .credential import Bearer, CredentialError

log = logging.getLogger("swarm_voice.plannerlease")

PROVIDER = "anthropic"
#: Who holds the lease, as the broker records it on the hold: the planner, per
#: delegation turn, or the fast front (claudefront.py), for the session.
HOLDER = "voice-planner"
FRONT_HOLDER = "voice-front"
HOLDERS = (HOLDER, FRONT_HOLDER)
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
    #: The tier the broker granted (accountlease.TIERS), None when it named none.
    tier: str | None = None
    #: The accounts this turn already left as spent, which a move passes over.
    excluded: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"PlannerLease(account_id={self.account_id!r}, voice_session_id={self.voice_session_id!r})"


class PlannerAccounts:
    """Assign, renew and release one holder's account, over VoiceBroker's client."""

    def __init__(self, broker: VoiceBroker, *, holder: str = HOLDER,
                 renew_seconds: float = RENEW_SECONDS) -> None:
        if holder not in HOLDERS:
            raise ValueError(f"holder is one of {', '.join(HOLDERS)}")
        self._broker = broker
        self.holder = holder
        self.renew_seconds = renew_seconds

    async def assign(self, *, person: str, voice_session_id: str,
                     exclude: tuple[str, ...] = ()) -> PlannerLease:
        """Hold one account. `exclude` names accounts this session already
        held and could not use (the broker never offers them again)."""
        payload: dict[str, Any] = {"provider": PROVIDER, "holder": self.holder, "person": person,
                                   "voice_session_id": voice_session_id,
                                   # claudeturn.py sets ANTHROPIC_API_KEY for a key (§5.3).
                                   "key_tier": True}
        if exclude:
            payload["exclude"] = list(exclude)
        status, body = await self._broker.call("/v1/accounts/assign", payload)
        if status != 200 or not isinstance(body, dict):
            # The status only: an error body may quote the request.
            raise LeaseFailed(f"the quota broker refused the planner's account lease (HTTP {status})", status)
        if not body.get("account_id") or not body.get("assignment_id"):
            nxt = body.get("next_reset_at")
            raise PoolFull(str(body.get("reason") or NO_ACCOUNT_AVAILABLE), nxt if isinstance(nxt, str) else None)
        cred = body.get("credential")
        token = cred.get("access_token") if isinstance(cred, dict) else None
        tier = tier_of(body)
        # Anything but a plan is a key: its token is Claude Code's ANTHROPIC_API_KEY.
        value = ApiKey(token) if isinstance(token, str) and token and tier not in (None, TIER_PLAN) else token
        lease = PlannerLease(account_id=str(body["account_id"]), assignment_id=str(body["assignment_id"]),
                             voice_session_id=voice_session_id, bearer=Bearer(value or ""), tier=tier,
                             excluded=tuple(exclude))
        if not isinstance(token, str) or not token:
            # Held but unusable: give it back at once, so the account does not
            # count an agent that never ran.
            await self.release(lease)
            raise LeaseFailed("the quota broker's planner lease carried no credential")
        return lease

    async def move(self, spent: PlannerLease, *, person: str) -> PlannerLease:
        """The next tier for a turn whose account is spent: `spent` given back,
        then an assign with it, and every account the turn left before it,
        excluded (§5.3), so a turn never bounces back onto a spent account. Raises PoolFull when nothing is
        left; the planner's chain has no tier 3 (Q2)."""
        await self.release(spent)
        moved = await self.assign(person=person, voice_session_id=spent.voice_session_id,
                                  exclude=(*spent.excluded, spent.account_id))
        log.info("voice session %s: the %s moved from the %s tier to the %s tier", spent.voice_session_id,
                 self.holder, spent.tier, moved.tier)
        return moved

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
    async def lease(self, *, person: str, voice_session_id: str) -> AsyncIterator["PlannerHold"]:
        """Hold one account for the body of the `async with`, renewed while it
        runs and released in a finally, whatever the turn did. A turn whose
        account is spent moves the hold on (`PlannerHold.move`); whichever
        account holds it at the end is the one released."""
        hold = PlannerHold(self, person, await self.assign(person=person, voice_session_id=voice_session_id))
        try:
            yield hold
        finally:
            await hold.close()


class PlannerHold:
    """The account one turn holds now, renewed while it is held. `move` takes
    the turn to the next tier (§5.3); the renewal follows the account."""

    def __init__(self, accounts: PlannerAccounts, person: str, held: PlannerLease) -> None:
        self._accounts = accounts
        self._person = person
        self.current = held
        self._renewer = self._renew(held)

    def _renew(self, held: PlannerLease) -> asyncio.Task:
        return asyncio.create_task(self._accounts._renew_loop(held),  # noqa: SLF001
                                   name=f"planner-lease-{held.voice_session_id}")

    @property
    def bearer(self) -> Bearer:
        return self.current.bearer

    @property
    def tier(self) -> str | None:
        return self.current.tier

    @property
    def account_id(self) -> str:
        return self.current.account_id

    async def _stop(self) -> None:
        self._renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._renewer

    async def move(self) -> PlannerLease:
        """Give the spent account back and hold the next tier's. Raises PoolFull
        when the chain has nothing left (no tier 3, Q2): the spent account is
        given back either way."""
        await self._stop()
        self.current = await self._accounts.move(self.current, person=self._person)
        self._renewer = self._renew(self.current)
        return self.current

    async def close(self) -> None:
        await self._stop()
        await self._accounts.release(self.current)

    def __repr__(self) -> str:
        return f"PlannerHold({self.current!r})"
