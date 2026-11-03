"""A Voice it pool account, leased from the quota broker for one voice session.

THE OWNER'S CHOICE (2026-11-02): the ChatGPT plan account is reserved for Voice
it through a separate "Voice it pool" in the quota broker, and every voice
session runs on an account leased from it -- "pool only, wait for it". With no
free account the session is refused with a reason and nothing starts; there is
no fallback to the person's own sign-in, which nothing in swarm-voice reads.

THE BROKER'S ROUTES (artifacts interface, apps/quota-broker):

* `POST /v1/voice/lease` `{"person", "voice_session_id", "provider": "openai"}`
  -> `{"account_id", "assignment_id", "label", "display_name", "expires_at",
  "credential": {"access_token", "account_id"}, "reason": ""}`, or, refused,
  `{"account_id": null, ..., "credential": null, "reason": <why>,
  "next_reset_at"}`.
* `POST /v1/voice/lease/{account_id}/renew` `{"assignment_id",
  "voice_session_id"}` -> `{"expires_at", "credential": {...}}` with a FRESH
  access token; 404/409 when the hold is gone.
* `POST /v1/voice/lease/{account_id}/release` (same body) -> `{"released"}`,
  idempotent.

The hold's TTL is ten minutes and swarm-voice renews every four
(`RENEW_SECONDS`), so a swarm-voice that dies leaves a hold that expires on its
own; nothing has to clean up after it.

AUTHENTICATION IS THE METADATA SERVER'S ID TOKEN, minted for the broker's
audience, exactly as agent_worker/accountlease.py `fetch_identity_token` mints
it: the broker identifies swarm-voice by the service-account email in that
token (its VOICE_SERVICE_ACCOUNTS), and swarm-voice names no tenant. The helper
is mirrored here rather than imported because swarm-voice's image and
pyproject do not carry the agent-worker package, and it is async (httpx) where
the worker's is blocking urllib.

NOTHING SECRET LEAVES. The access token is held in a `Bearer` (masked repr),
never logged, never put in an exception: errors carry a status and a route.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from .credential import Bearer, CredentialError

log = logging.getLogger("swarm_voice.accountlease")

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

PROVIDER = "openai"
#: The broker's voice hold lives ten minutes; renewing every four leaves two
#: renewals' slack before it lapses, and each renewal brings a fresh token.
RENEW_SECONDS = 240.0
_TIMEOUT = 10.0

#: The refusals the broker gives, and what swarm-voice answers swarm-api with.
NOT_IN_POOL = "not_in_voice_pool"
NONE_REGISTERED = "no_voice_account_registered"
NONE_AVAILABLE = "no_voice_account_available"

TokenFetcher = Callable[[str], Awaitable[str]]


class VoicePoolRefused(Exception):
    """The broker leased no account: `reason` is one of the three refusals above."""

    def __init__(self, reason: str, next_reset_at: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.next_reset_at = next_reset_at


class HoldGone(CredentialError):
    """The broker no longer holds this session's lease (expired or released)."""


def _credential(body: Any) -> Bearer:
    cred = body.get("credential") if isinstance(body, dict) else None
    token = cred.get("access_token") if isinstance(cred, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialError("the quota broker's voice lease carried no credential")
    account = cred.get("account_id")
    return Bearer(token, account if isinstance(account, str) and account else None)


class VoiceBroker:
    """The three Voice it pool routes, over one shared HTTP client."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, *, audience: str | None = None,
                 token_fetcher: TokenFetcher | None = None,
                 clock: Callable[[], float] = time.monotonic, renew_seconds: float = RENEW_SECONDS) -> None:
        if not base_url:
            raise RuntimeError("QUOTA_BROKER_URL is not set: no Voice it pool account could be leased")
        self._http = http
        self._base = base_url.rstrip("/")
        # Cloud Run checks `aud` against the service URL unless a custom
        # audience is configured; the broker sets one (QUOTA_BROKER_AUDIENCE).
        self._audience = (audience or self._base).rstrip("/")
        self._fetch_token = token_fetcher or self._metadata_token
        self._clock = clock
        self.renew_seconds = renew_seconds

    async def _metadata_token(self, audience: str) -> str:
        """A Google-signed ID token for `audience`, from the metadata server."""
        try:
            response = await self._http.get(
                METADATA_IDENTITY_URL, params={"audience": audience, "format": "full"},
                headers={"Metadata-Flavor": "Google"}, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise CredentialError(f"no workload identity token available ({type(exc).__name__})") from None
        token = response.text.strip() if response.status_code == 200 else ""
        if not token:
            raise CredentialError(f"no workload identity token available (HTTP {response.status_code})")
        return token

    async def _post(self, path: str, payload: dict) -> tuple[int, Any]:
        token = await self._fetch_token(self._audience)
        try:
            response = await self._http.post(
                f"{self._base}{path}", json=payload, timeout=_TIMEOUT,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise CredentialError(f"the quota broker could not be reached ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    async def lease(self, *, person: str, voice_session_id: str) -> "PoolCredential":
        """Lease a Voice it pool account for `person`'s session. Raises
        VoicePoolRefused with the broker's reason, or CredentialError."""
        status, body = await self._post("/v1/voice/lease", {
            "person": person, "voice_session_id": voice_session_id, "provider": PROVIDER})
        if status != 200 or not isinstance(body, dict):
            # The status only: an error body may quote the request.
            raise CredentialError(f"the quota broker refused the voice lease (HTTP {status})")
        if not body.get("account_id") or not body.get("assignment_id"):
            reason = str(body.get("reason") or NONE_AVAILABLE)
            nxt = body.get("next_reset_at")
            raise VoicePoolRefused(reason, nxt if isinstance(nxt, str) else None)
        bearer = _credential(body)
        return PoolCredential(self, account_id=str(body["account_id"]), assignment_id=str(body["assignment_id"]),
                              voice_session_id=voice_session_id, bearer=bearer,
                              label=str(body.get("label") or ""))

    async def renew(self, account_id: str, assignment_id: str, voice_session_id: str) -> Bearer:
        status, body = await self._post(f"/v1/voice/lease/{quote(account_id, safe='')}/renew", {
            "assignment_id": assignment_id, "voice_session_id": voice_session_id})
        if status in (404, 409):
            raise HoldGone(f"the Voice it pool hold is gone (HTTP {status})")
        if status != 200:
            raise CredentialError(f"the quota broker refused the voice renewal (HTTP {status})")
        return _credential(body)

    async def release(self, account_id: str, assignment_id: str, voice_session_id: str) -> bool:
        status, body = await self._post(f"/v1/voice/lease/{quote(account_id, safe='')}/release", {
            "assignment_id": assignment_id, "voice_session_id": voice_session_id})
        if status != 200:
            raise CredentialError(f"the quota broker refused the voice release (HTTP {status})")
        return bool(isinstance(body, dict) and body.get("released"))

    def now(self) -> float:
        return self._clock()


class PoolCredential:
    """One session's lease, as a `credential.Credential`: the broker's access token,
    renewed every `RENEW_SECONDS` (each renewal returns a fresh token), released
    when the session ends."""

    def __init__(self, broker: VoiceBroker, *, account_id: str, assignment_id: str,
                 voice_session_id: str, bearer: Bearer, label: str = "") -> None:
        self._broker = broker
        self.account_id = account_id
        self.assignment_id = assignment_id
        self.voice_session_id = voice_session_id
        self.label = label
        self._bearer: Bearer | None = bearer
        self._renew_at = broker.now() + broker.renew_seconds
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.released = False
        self.lost = False

    def __repr__(self) -> str:
        return f"PoolCredential(account_id={self.account_id!r}, voice_session_id={self.voice_session_id!r})"

    async def bearer(self) -> Bearer:
        async with self._lock:
            if self.released or self.lost:
                raise CredentialError("this session's Voice it pool lease has ended")
            if self._bearer is None or self._broker.now() >= self._renew_at:
                await self._renew()
            assert self._bearer is not None
            return self._bearer

    async def refused(self) -> None:
        async with self._lock:
            self._bearer = None

    async def renew(self) -> None:
        async with self._lock:
            if not self.released and not self.lost:
                await self._renew()

    async def _renew(self) -> None:
        try:
            self._bearer = await self._broker.renew(self.account_id, self.assignment_id, self.voice_session_id)
        except HoldGone:
            self.lost = True
            self._bearer = None
            raise
        self._renew_at = self._broker.now() + self._broker.renew_seconds

    def start_renewing(self) -> None:
        """Renew in the background for as long as the session holds the lease."""
        if self._task is None and not self.released:
            self._task = asyncio.create_task(self._renew_loop(), name=f"voice-lease-{self.voice_session_id}")

    async def _renew_loop(self) -> None:
        while not self.released and not self.lost:
            await asyncio.sleep(self._broker.renew_seconds)
            try:
                await self.renew()
            except HoldGone:
                log.warning("voice session %s: the Voice it pool hold on %s is gone",
                            self.voice_session_id, self.account_id)
                return
            except CredentialError as exc:
                # Retried on the next tick; the hold's TTL is longer than two ticks.
                log.warning("voice session %s: lease renewal failed: %s", self.voice_session_id, exc)

    async def release(self) -> None:
        """Give the account back. Idempotent; a failure is logged, never raised: the
        broker's hold then expires on its own TTL."""
        if self.released:
            return
        self.released = True
        self._bearer = None
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await self._broker.release(self.account_id, self.assignment_id, self.voice_session_id)
        except CredentialError as exc:
            log.warning("voice session %s: lease release failed (the hold expires on its own): %s",
                        self.voice_session_id, exc)


def build_broker(http: httpx.AsyncClient) -> VoiceBroker:
    """The broker client from `QUOTA_BROKER_URL` and `QUOTA_BROKER_AUDIENCE`."""
    url = os.environ.get("QUOTA_BROKER_URL", "").strip()
    audience = os.environ.get("QUOTA_BROKER_AUDIENCE", "").strip() or None
    return VoiceBroker(http, url, audience=audience)
