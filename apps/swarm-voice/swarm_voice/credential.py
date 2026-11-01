"""The OpenAI credential a GPT-Live session runs on (docs/design/voice-it.md §3.6).

THE OWNER'S CHOICE (BGD, 2026-11-01): "lets use the openai provider and a user
supplied open ai subscription" -- the voice runs on the person's own OpenAI
plan, the way the Claude pool runs on subscriptions, not on a platform API key.

WHAT OPENAI DOCUMENTS (read 2026-11-01, developers.openai.com):

* "Sign in with ChatGPT" lets a ChatGPT plan's holder authorise an app. Plan
  usage needs the `resource.invoke` and `chatgpt.tokens.use.direct` scopes for
  the resource `https://api.openai.com/v1`, plus `offline_access` for a refresh
  token (/siwc/quickstart). The access token is a one-hour bearer JWT; the
  refresh token lives 30 days and is REPLACED on every refresh
  (/siwc/token-sharing-open-source/token-reference). A refresh is a form POST to
  `https://auth.openai.com/api/accounts/oauth/token` with `grant_type`,
  `client_id`, `refresh_token` and `resource`, without `scope`, and refreshes
  of one session must be serialised "so two processes do not race a rotating
  token" (/siwc/token-sharing-open-source/profiles-and-sessions).
* Every page describes it for the Responses API, in a limited trial for
  selected partners, and no page says it covers `v1/live/sessions`; the Live
  pages show a project API key. Whether a plan's token opens a GPT-Live session
  is NOT VERIFIED, and the owner is asked in the pull request which credential
  to use. So the session code (live.py) runs against the `Credential`
  interface below, and `SubscriptionCredential` is the owner's choice built to
  OpenAI's documented token flow; no API-key fallback is built.

THE RULES THIS MODULE HOLDS (§3.6, invariant 9):

* The credential lives only in Secret Manager, in the PERSON'S OWN scope:
  secret `swarm-tenant-<personal tenant>-openai` of this deployment's project,
  the personal tenant being `swarm_common.identity.tenant_id_for_user` of the
  person swarm-api names. `check_scope` refuses any other secret -- another
  person's, a workspace's, another project's -- before anything is read.
* It is read by VERSION reference (`.../versions/<n>`), never `latest`, and held
  in memory only.
* A rotated refresh token is written back to the same secret as a new version;
  later reads start from the newest version swarm-voice holds or swarm-api
  names, whichever is newer.
* It is never logged, never put in an exception, never returned: errors carry
  an HTTP status only (an error body may quote the request), and `Bearer`'s
  repr is masked.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx
from swarm_common.identity import tenant_id_for_user

SECRET_MANAGER = "https://secretmanager.googleapis.com/v1/"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
#: The resource a plan's token is minted for (/siwc/quickstart).
RESOURCE = "https://api.openai.com/v1"
#: Refresh this long before the access token's own expiry, so a session create
#: never races the last seconds of a token.
REFRESH_EARLY_SECONDS = 120
#: What a stored credential says it is. The only kind built (module docstring).
KIND_SUBSCRIPTION = "chatgpt_subscription"

VERSION_REF = re.compile(
    r"projects/(?P<project>[a-z0-9][a-z0-9-]{0,61}[a-z0-9])"
    r"/secrets/(?P<secret>[A-Za-z0-9_-]{1,255})/versions/(?P<version>[1-9][0-9]{0,8})"
)


class CredentialError(RuntimeError):
    """The credential could not be read or refreshed. The message names a status, never a value."""


class CredentialRefused(PermissionError):
    """The reference is not a version of the person's own OpenAI secret."""


class Bearer:
    """An access token whose repr and str never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Bearer(<redacted>)"

    __str__ = __repr__


class Credential(Protocol):
    """What a Live session needs from a credential, whatever kind the owner picks."""

    async def bearer(self) -> Bearer:
        """A bearer that is valid now, refreshed first if it is about to expire."""
        ...

    async def refused(self) -> None:
        """OpenAI refused the last bearer: the next `bearer()` must not reuse it."""
        ...


class SecretStore(Protocol):
    async def access(self, version_ref: str) -> bytes: ...

    async def add_version(self, secret: str, payload: bytes) -> str:
        """Add a version to `projects/<p>/secrets/<s>`; return the new version's reference."""
        ...


def personal_secret_id(person: str) -> str:
    """The person's own OpenAI secret, as `TenantConfig.secret_name("openai")` names a tenant's."""
    return f"swarm-tenant-{tenant_id_for_user(person)}-openai"


@dataclass(frozen=True)
class VersionRef:
    project: str
    secret: str
    version: int

    @property
    def secret_name(self) -> str:
        return f"projects/{self.project}/secrets/{self.secret}"

    def __str__(self) -> str:
        return f"{self.secret_name}/versions/{self.version}"


def parse_ref(ref: str) -> VersionRef:
    match = VERSION_REF.fullmatch(ref.strip()) if isinstance(ref, str) else None
    if not match:
        raise CredentialRefused("the credential must be a Secret Manager version reference, "
                                "projects/<p>/secrets/<name>/versions/<n>")
    return VersionRef(match["project"], match["secret"], int(match["version"]))


def check_scope(ref: str, *, person: str, project: str) -> VersionRef:
    """The reference, parsed, if and only if it is a version of `person`'s own OpenAI
    secret in this deployment's project (invariant 9)."""
    parsed = parse_ref(ref)
    if parsed.project != project:
        raise CredentialRefused("the credential is not in this deployment's project")
    try:
        own = personal_secret_id(person)
    except ValueError:
        raise CredentialRefused("the person is not an email address") from None
    if parsed.secret != own:
        raise CredentialRefused("the credential is not the person's own OpenAI secret")
    return parsed


class SecretManagerStore:
    """Secret Manager over REST with the pod's own Workload Identity, as swarm-live
    reads its key. google-auth's session is synchronous, so each call runs in a thread."""

    def __init__(self) -> None:
        self._session = None

    def _authorized(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _access(self, version_ref: str) -> bytes:
        response = self._authorized().get(SECRET_MANAGER + version_ref + ":access", timeout=30)
        if response.status_code != 200:
            # The status only: an error body may quote the request.
            raise CredentialError(f"the credential's secret version could not be read (HTTP {response.status_code})")
        try:
            return base64.b64decode(response.json()["payload"]["data"])
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise CredentialError("Secret Manager returned no credential payload") from None

    def _add(self, secret: str, payload: bytes) -> str:
        body = {"payload": {"data": base64.b64encode(payload).decode("ascii")}}
        response = self._authorized().post(SECRET_MANAGER + secret + ":addVersion", json=body, timeout=30)
        if response.status_code != 200:
            raise CredentialError(f"the refreshed credential could not be stored (HTTP {response.status_code})")
        try:
            return str(response.json()["name"])
        except (KeyError, TypeError, ValueError):
            raise CredentialError("Secret Manager returned no version name") from None

    async def access(self, version_ref: str) -> bytes:
        return await asyncio.to_thread(self._access, version_ref)

    async def add_version(self, secret: str, payload: bytes) -> str:
        return await asyncio.to_thread(self._add, secret, payload)


class SubscriptionCredential:
    """A person's OpenAI plan through "Sign in with ChatGPT": the stored secret is
    `{"kind": "chatgpt_subscription", "client_id": ..., "refresh_token": ...}`; the
    one-hour access token is minted from it and held in memory only."""

    def __init__(self, ref: VersionRef, *, store: SecretStore, http: httpx.AsyncClient,
                 clock: Callable[[], float] = time.time, token_url: str = TOKEN_URL) -> None:
        self.ref = ref
        self._store = store
        self._http = http
        self._clock = clock
        self._token_url = token_url
        self._lock = asyncio.Lock()
        self._access: Bearer | None = None
        self._expires_at = 0.0

    async def bearer(self) -> Bearer:
        async with self._lock:  # one refresh at a time: the refresh token rotates
            if self._access is None or self._clock() >= self._expires_at - REFRESH_EARLY_SECONDS:
                await self._refresh()
            assert self._access is not None
            return self._access

    async def refused(self) -> None:
        async with self._lock:
            self._access = None

    async def newer(self, ref: VersionRef) -> None:
        """swarm-api named a version newer than the one held (the person stored a new
        credential): start from it, and drop the access token minted from the old one."""
        async with self._lock:
            if ref.version > self.ref.version:
                self.ref = ref
                self._access = None

    async def _stored(self) -> dict:
        raw = await self._store.access(str(self.ref))
        try:
            stored = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CredentialError("the stored OpenAI credential is not JSON") from None
        if not isinstance(stored, dict) or stored.get("kind") != KIND_SUBSCRIPTION:
            raise CredentialError(f"the stored OpenAI credential is not a {KIND_SUBSCRIPTION} credential")
        if not isinstance(stored.get("client_id"), str) or not isinstance(stored.get("refresh_token"), str):
            raise CredentialError("the stored OpenAI credential has no client_id or refresh_token")
        return stored

    async def _refresh(self) -> None:
        stored = await self._stored()
        try:
            response = await self._http.post(self._token_url, data={
                "grant_type": "refresh_token",
                "client_id": stored["client_id"],
                "refresh_token": stored["refresh_token"],
                "resource": RESOURCE,
            }, timeout=30)
        except httpx.HTTPError as exc:
            raise CredentialError(f"the OpenAI credential could not be refreshed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise CredentialError(f"the OpenAI credential could not be refreshed (HTTP {response.status_code})")
        try:
            token = response.json()
            access = token["access_token"]
            expires_in = float(token.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise CredentialError("OpenAI's token response had no access token") from None
        if not isinstance(access, str) or not access:
            raise CredentialError("OpenAI's token response had no access token")
        rotated = token.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != stored["refresh_token"]:
            # The old refresh token is spent the moment this one is issued: store the
            # new one before using the access token, or a restart would lose the grant.
            payload = json.dumps({**stored, "refresh_token": rotated}).encode()
            self.ref = parse_ref(await self._store.add_version(self.ref.secret_name, payload))
        self._access = Bearer(access)
        self._expires_at = self._clock() + expires_in


class CredentialFactory:
    """One credential per person's secret for the process's life, so every session of
    one person shares one refresh lock and the newest stored version."""

    def __init__(self, *, store: SecretStore, http: httpx.AsyncClient, project: str,
                 clock: Callable[[], float] = time.time, token_url: str = TOKEN_URL) -> None:
        self._store = store
        self._http = http
        self._project = project
        self._clock = clock
        self._token_url = token_url
        self._held: dict[str, SubscriptionCredential] = {}

    async def for_person(self, ref: str, *, person: str) -> SubscriptionCredential:
        parsed = check_scope(ref, person=person, project=self._project)
        held = self._held.get(parsed.secret_name)
        if held is None:
            held = SubscriptionCredential(parsed, store=self._store, http=self._http,
                                          clock=self._clock, token_url=self._token_url)
            self._held[parsed.secret_name] = held
        else:
            await held.newer(parsed)
        return held
