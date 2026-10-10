"""The SwarmCloud GitHub App's user authorisation: authorise, exchange,
refresh, disconnect (docs/onboarding.md §1.3, §3.1-§3.4; #780, lane OB3).

Owner decisions of 2026-10-07 on #780 this builds: D1, the GitHub App with
user access tokens; D2, swarm-api refreshes them on a Cloud Scheduler sweep,
behind an interface (`TokenRefresher`) a dedicated broker can take over; D3,
swarm-api creates each user's secret slot at onboarding.

THE FLOW. `authorize` mints a `state`, stores only its sha256 in
`forge_authorizations/{state_hash}` -- bound to the caller's tenant and
email, ten minutes, single use -- and answers GitHub's authorise URL. GitHub
sends the browser to the console's callback page, which posts `{state,
code}` to `exchange` with the user's own sign-in. The exchange spends the
state first (in a transaction, so a replay finds it used), then trades the
code for a user access token (8 hours) and a refresh token (6 months) with
the App's client secret, reads `GET /user` for the login, and writes the
access token to the user's slot `swarm-tenant-<t>-git-u-<hex>` and the
refresh token to its `-refresh` twin, creating both if absent. The
connection document and the slot's `git_tokens` record (kind `app_user`)
hold names, times and states; no field holds a value.

WHERE A VALUE MAY GO. The client secret is read from Secret Manager at
request time and sent to github.com's token endpoint and api.github.com's
grant endpoint, nowhere else. A user token goes from GitHub's answer into
Secret Manager and nowhere else -- never Firestore, a log line, an event or
a response. Every value is registered with the redaction filter
(`gittokens.redaction_literal`) for as long as it is in hand, so a record any
code logs meanwhile is masked before a handler writes it.

WHAT swarm-api MAY READ (terraform/bootstrap/forge_user_slots.tf). It creates
slots, adds versions to both, and reads both: the `-refresh` twin, which the
sweep spends, and -- owner decision 2026-10-08 -- the base slot, whose
current access token every request acting as the person REUSES
(`read_access`; `access.AccessService.user_token`).

WHY IT READS THE BASE SLOT NOW. Measured 2026-10-08 08:20Z: when swarm-api
could read only the twin, every call acting as the person refreshed, so the
owner's slot reached 35 versions within minutes -- and each refresh makes
GitHub end the access token it replaces, so a task holding the previous
version failed "the forge refused the credential (401: Bad credentials)".
Reading the access token adds no power: swarm-api already holds the refresh
token, which mints access tokens. So a request refreshes only when the token
is within `REUSE_WHILE_LEFT` of expiry, or GitHub answered 401, once, under
the refresh lease; in practice only the 15-minute sweep refreshes. The worker
re-reads the slot and retries once on a 401 for the rare token a sweep ends
under it (agent_worker.secrets.call_reading_again).

DISCONNECT revokes the user's authorisation of the App at GitHub
(`DELETE /applications/{client_id}/grant`, which ends every token of that
authorisation, refresh tokens included), then disables every enabled version
of both slots, deletes the caller's grants, and marks the connection and its
git token record revoked. The local half does not wait on GitHub: a revoke
GitHub did not answer still disconnects here, and the answer says so with
the page where the user can revoke it themselves.

A FALLBACK TOKEN FOR ONE OWNER (D5). An org whose admin will not install the
App is reached with a personal access token the person posts once to `POST
/v1/onboarding/github/token` (`store_owner_token`). It is probed first -- the
account, its orgs, one page of the owner's repositories and the ones the
person already chose there, SSO and the classic-token policy read from
GitHub's answers -- and stored only when it reaches the owner, as a version
of the person's per-owner slot `git-u-<16 hex of sha256(email|owner)>`
(`gittokens.owner_suffix`), created here like the App slot. That name is
`FORGE_CREDENTIAL`'s shape and under the bootstrap's `-git-u-` prefix, so no
contract or IAM change. The owner's `forge_orgs` document says `method: pat`;
removing the owner or disconnecting disables every version of the slot and
revokes its record (`revoke_owner_token`).

THE SWEEP ALSO RE-CHECKS INSTALL REQUESTS (§2.3 ORG_APPROVAL_PENDING). After
its refreshes, and inside the same time budget, the sweep calls the hook the
access service hangs on it (`on_sweep`; `access.AccessService.
recheck_requested`): for every person holding a `requested` owner, one `GET
/user/installations` with their own token, and an owner GitHub now lists
becomes installed and enabled. The sweep runs every 15 minutes
(terraform/modules/scheduler `forge_refresh_schedule`), which is the
interval §2.3's copy promises; the counts are in the report, never a value.

NOT CONFIGURED IS AN ANSWER. The App is registered by hand
(docs/runbooks/github-app.md); until its client id is in swarm-api's
environment and its client secret has a version, every route answers 503
`github_app_not_configured`, naming what is missing -- never a crash, and
never a call to GitHub. The one exception is the sweep with no active
connection: it has nothing to keep alive, so it answers 200 with
`configured: false` and the same sentence, and the scheduler job is not red
for an App nobody has connected to yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import time as _time
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urlencode, urlparse

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import utcnow

from .errors import ApiError, Conflict, NotFound, UpstreamUnavailable, ValidationFailed
from .forge import MAX_RESPONSE_BYTES, _OPENER
from .gittokens import (
    COLLECTION as TOKENS_COLLECTION,
    FORGE,
    PAT_KINDS,
    GitTokens,
    OwnerReach,
    Scope,
    TokenState,
    _hex16,
    _user_key,
    owner_record,
    owner_suffix,
    owner_token_id,
    probe_owner,
    provider_suffix,
    record_for_slot,
    redaction_literal,
    secret_name_for,
    token_kind,
)
from .onboarding import recovery_copy

log = logging.getLogger(__name__)

__all__ = [
    "AppConfig", "AppNotConfigured", "AuthorisationRefused", "ForgeApp", "HttpAnswer",
    "SlotUnreadable", "TokenRefresher", "connection_id_for", "slot_labels", "state_hash",
    "user_hash",
]

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PENDING_COLLECTION = "forge_authorizations"
CONNECTIONS = "forge_connections"
GRANTS = "forge_grants"
#: `forge_orgs/{tenant_id}__{user_hash}__{owner}`: an owner a person enabled,
#: through the App's installation or (D5) through a token for that owner.
ORGS = "forge_orgs"

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
#: The only hosts a value from this module is sent to.
HOSTS = frozenset({"github.com", "api.github.com"})

#: §2.3 AUTHORISATION_EXPIRED: "links last 10 minutes and work once".
STATE_TTL = timedelta(minutes=10)

#: What GitHub documents for an App's user tokens, used only when an answer
#: omits its own `expires_in`: 8 hours, and 6 months for the refresh token.
DEFAULT_ACCESS_TTL = timedelta(hours=8)
DEFAULT_REFRESH_TTL = timedelta(days=184)

#: Refresh a token with this much or less left. §3.3 step 3: the worker's
#: read finds a token at least two hours from expiry; the sweep runs every
#: 15 minutes (terraform/modules/scheduler/jobs.tf, forge_refresh), so a
#: token is taken on the tick before it would cross two hours, with a tick
#: spare for one the forge did not answer.
REFRESH_WHEN_LEFT = timedelta(hours=2, minutes=30)

#: A request acting as the person (access.py) reuses the current access
#: token while it has MORE than this left, and refreshes only at or under it
#: (owner decision 2026-10-08). Under the sweep's REFRESH_WHEN_LEFT, so the
#: sweep, ticking every 15 minutes, takes nearly every token first and a
#: request refreshes only one the sweep did not reach.
REUSE_WHILE_LEFT = timedelta(hours=2)

#: How long one refresher holds a connection (§3.1 `refresh_lease`): a
#: refresh token works once, so a second refresher must not spend it while
#: the first is writing the new one. Longer than one refresh takes, shorter
#: than a tick, so a crashed holder delays the next sweep by nothing.
LEASE_FOR = timedelta(minutes=2)

#: One sweep's bounds. The job's attempt deadline is 300 s; each refresh is
#: one GitHub POST and two Secret Manager writes. What is left over is the
#: next tick's, 15 minutes on.
MAX_CONNECTIONS = 2000
MAX_REFRESHES_PER_SWEEP = 200
SWEEP_BUDGET_SECONDS = 240.0

TIMEOUT_SECONDS = 10.0
_USER_AGENT = "swarmcloud-swarm-api"

#: The App's client secret slot (terraform/modules/secret_manager, `github_app`).
CLIENT_SECRET_SLOT = "swarm-github-app-client-secret"
#: The App's private key slot, the same module. Read only by the admin
#: self-check (`ForgeApp.check_app_key`), which signs an App JWT with it.
PRIVATE_KEY_SLOT = "swarm-github-app-private-key"
APP_URL = "https://api.github.com/app"
#: GitHub refuses an App JWT whose lifetime exceeds ten minutes; it is issued
#: 60 s in the past to absorb clock drift, and lives nine minutes from now.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540
RUNBOOK = "docs/runbooks/github-app.md"
#: Where a person revokes the authorisation themselves: a GitHub page.
AUTHORIZATIONS_PAGE = "https://github.com/settings/apps/authorizations"

SURFACES = ("console", "plugin")
METHOD_APP_USER = "app_user"
#: A `forge_orgs` document's `method` when the owner is reached through the
#: person's fallback token for it (D5), not the App.
METHOD_PAT = "pat"
#: The org's SAML sign-in page: what SSO_NOT_AUTHORISED's copy sends a person to.
SSO_URL = "https://github.com/orgs/{owner}/sso"
#: Where a person deletes a personal access token at GitHub.
TOKENS_PAGE = "https://github.com/settings/tokens"

ACTIVE = "active"
REFRESH_FAILED = "refresh_failed"
REVOKED = "revoked"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class AppNotConfigured(ApiError):
    """The App's client id or client secret is absent. Constant sentences
    that name what is missing, never a value."""

    status_code = 503
    code = "github_app_not_configured"

    def __init__(self, missing: str) -> None:
        super().__init__(
            f"the SwarmCloud GitHub App is not configured: {missing}. "
            f"An operator registers it from {RUNBOOK}."
        )


class AuthorisationRefused(ApiError):
    """A §2.3 failure, served with its code and its recovery copy."""

    status_code = 400
    code = "authorisation_refused"

    def __init__(self, failure_code: str, message: str, *, status: int | None = None,
                 **fill: str) -> None:
        super().__init__(message, detail={"failure_code": failure_code,
                                          "recovery": recovery_copy(failure_code, **fill)})
        if status is not None:
            self.status_code = status


def _unreachable(message: str) -> AuthorisationRefused:
    refused = AuthorisationRefused("FORGE_UNREACHABLE", message, status=503)
    refused.code = "forge_unreachable"
    return refused


class SlotUnreadable(Exception):
    """A slot read that did not return a value. Constant text naming the
    secret, never a value."""


# --------------------------------------------------------------------------
# naming (§3.1)
# --------------------------------------------------------------------------

def state_hash(state: str) -> str:
    """The id of a pending authorisation: the state itself is never stored."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def user_hash(email: str) -> str:
    """16 hex of sha256 of the lower-cased email, as `provider_suffix` hashes it."""
    return _hex16(_user_key(email))


def connection_id_for(tenant_id: str, email: str, forge: str = FORGE) -> str:
    """§3.1: `conn_` + 16 hex of tenant + user + forge."""
    return "conn_" + _hex16(tenant_id + _user_key(email) + forge)


#: A base user slot's provider suffix (`gittokens.provider_suffix` for a user).
_BASE_USER_SLOT = re.compile(r"git-u-[0-9a-f]{16}")


def refresh_suffix(suffix: str) -> str:
    return f"{suffix}-refresh"


def slot_labels(tenant_id: str, suffix: str) -> dict[str, str]:
    """A runtime-created slot's labels (§3.4 item 3, runbook "User slots
    outside Terraform"): `managed-by=swarm-api` and `swarm-tenant` so the
    tenant's slots are listable by label, and `tenant` because
    scripts/offboard-tenant.sh finds a tenant's secrets by `labels.tenant`."""
    return {"managed-by": "swarm-api", "swarm-tenant": tenant_id, "tenant": tenant_id,
            "provider": suffix}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


# --------------------------------------------------------------------------
# configuration and the App's secret
# --------------------------------------------------------------------------

def _app_setting(name: str, environ: Mapping[str, str] | None = None) -> str:
    """One of the App's public settings from the environment, stripped.

    A module-level helper that touches os.environ ON PURPOSE:
    scripts/lib/check-env-parity.sh counts a variable as read only through
    `os.environ` or such a helper, so reading it off an injected mapping hid
    these three reads and failed the parity check (#840). `environ` is the
    tests' injection point.
    """
    env = os.environ if environ is None else environ
    return (env.get(name) or "").strip()


@dataclass(frozen=True)
class AppConfig:
    """The App's PUBLIC settings (terraform/infra/github_app.tf): never a secret."""

    client_id: str = ""
    app_id: str = ""
    slug: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AppConfig":
        return cls(client_id=_app_setting("GITHUB_APP_CLIENT_ID", environ),
                   app_id=_app_setting("GITHUB_APP_ID", environ),
                   slug=_app_setting("GITHUB_APP_SLUG", environ))

    def missing(self) -> list[str]:
        return [] if self.client_id else ["GITHUB_APP_CLIENT_ID"]


class AppSecrets(Protocol):
    def client_secret(self) -> str: ...

    def private_key(self) -> str: ...


class SecretManagerAppSecrets:
    """Reads `swarm-github-app-client-secret`'s and `swarm-github-app-private-key`'s
    latest version, at request time, and only those. swarm-api is their one
    accessor (github_app_accessor). The client is built on first use, never
    at construction."""

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _latest(self, slot: str) -> str:
        from google.api_core import exceptions as gexc

        name = f"projects/{self._project_id}/secrets/{slot}/versions/latest"
        try:
            version = self._secret_client().access_secret_version(request={"name": name})
        except (gexc.NotFound, gexc.FailedPrecondition):
            raise AppNotConfigured(f"{slot} has no enabled version") from None
        except gexc.PermissionDenied:
            raise AppNotConfigured(f"swarm-api may not read {slot}") from None
        except Exception as exc:
            raise AppNotConfigured(
                f"{slot} could not be read ({type(exc).__name__})") from None
        try:
            value = bytes(version.payload.data).decode("utf-8").strip()
        except Exception:
            value = ""
        if not value:
            raise AppNotConfigured(f"{slot} is empty")
        return value

    def client_secret(self) -> str:
        return self._latest(CLIENT_SECRET_SLOT)

    def private_key(self) -> str:
        return self._latest(PRIVATE_KEY_SLOT)


# --------------------------------------------------------------------------
# the user's slots (D3)
# --------------------------------------------------------------------------

class UserSlots(Protocol):
    """What swarm-api does to a user's slots, by provider suffix.
    `read_access` reads a base user slot's current access token and nothing
    else; `read_refresh` reads a `-refresh` twin and nothing else."""

    def ensure(self, tenant_id: str, suffix: str) -> bool: ...

    def add_version(self, tenant_id: str, suffix: str, value: str) -> str: ...

    def read_access(self, tenant_id: str, suffix: str) -> str: ...

    def read_refresh(self, tenant_id: str, suffix: str) -> str: ...

    def disable(self, tenant_id: str, suffix: str) -> int: ...


class SecretManagerUserSlots:
    """The user slots in Secret Manager. Names go through the frozen
    `Tenant.secret_name` (`gittokens.secret_name_for`) from the caller's own
    tenant, so nothing here can name another tenant's secret, or anything
    that does not start `swarm-tenant-` -- which none of the other team's
    secrets in this project do."""

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _name(self, tenant_id: str, suffix: str) -> str:
        return f"projects/{self._project_id}/secrets/{secret_name_for(tenant_id, suffix)}"

    def ensure(self, tenant_id: str, suffix: str) -> bool:
        from google.api_core import exceptions as gexc

        try:
            self._secret_client().create_secret(request={
                "parent": f"projects/{self._project_id}",
                "secret_id": secret_name_for(tenant_id, suffix),
                "secret": {"replication": {"automatic": {}},
                           "labels": slot_labels(tenant_id, suffix)},
            })
            return True
        except gexc.AlreadyExists:
            return False

    def add_version(self, tenant_id: str, suffix: str, value: str) -> str:
        from google.cloud import secretmanager

        version = self._secret_client().add_secret_version(request={
            "parent": self._name(tenant_id, suffix),
            "payload": secretmanager.SecretPayload(data=value.encode("utf-8")),
        })
        return (getattr(version, "name", "") or "").rsplit("/", 1)[-1]

    def read_access(self, tenant_id: str, suffix: str) -> str:
        """The base user slot's latest version: the person's current access
        token. Only a `git-u-<16 hex>` slot, never its twin, never a tenant or
        repository slot."""
        if not _BASE_USER_SLOT.fullmatch(suffix or ""):
            raise SlotUnreadable("read_access reads a base user slot and nothing else")
        return self._latest(tenant_id, suffix)

    def read_refresh(self, tenant_id: str, suffix: str) -> str:
        if not suffix.endswith("-refresh"):
            raise SlotUnreadable("read_refresh reads a user slot's -refresh twin and nothing else")
        return self._latest(tenant_id, suffix)

    def _latest(self, tenant_id: str, suffix: str) -> str:
        from google.api_core import exceptions as gexc

        secret = secret_name_for(tenant_id, suffix)
        try:
            version = self._secret_client().access_secret_version(
                request={"name": f"{self._name(tenant_id, suffix)}/versions/latest"})
        except (gexc.NotFound, gexc.FailedPrecondition):
            raise SlotUnreadable(f"{secret} has no enabled version") from None
        except gexc.PermissionDenied:
            raise SlotUnreadable(f"swarm-api may not read {secret}") from None
        try:
            value = bytes(version.payload.data).decode("utf-8").strip()
        except Exception:
            value = ""
        if not value:
            raise SlotUnreadable(f"{secret} is empty")
        return value

    def disable(self, tenant_id: str, suffix: str) -> int:
        from google.api_core import exceptions as gexc

        client = self._secret_client()
        try:
            versions = list(client.list_secret_versions(
                request={"parent": self._name(tenant_id, suffix), "filter": "state:ENABLED"}))
        except gexc.NotFound:
            return 0
        for version in versions:
            client.disable_secret_version(request={"name": version.name})
        return len(versions)


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class HttpAnswer:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(default=b"", repr=False)

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


#: `(method, url, headers, body, timeout) -> HttpAnswer`; raises on a
#: transport failure. Injected by the tests; `urllib_send` in production.
HttpSend = Callable[[str, str, dict[str, str], "bytes | None", float], HttpAnswer]


class HostRefused(Exception):
    """A URL named a host a value may not go to. Constant text."""


def urllib_send(method: str, url: str, headers: dict[str, str], body: bytes | None,
                timeout: float) -> HttpAnswer:
    """One request to github.com or api.github.com over https, never
    following a redirect (a redirect would carry the body elsewhere)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in HOSTS \
            or parsed.port is not None or parsed.username is not None:
        raise HostRefused()
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return HttpAnswer(response.status, {k.lower(): v for k, v in response.headers.items()},
                              response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as answer:
        try:
            data = answer.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            data = b""
        items = answer.headers.items() if answer.headers is not None else ()
        return HttpAnswer(answer.code, {k.lower(): v for k, v in items}, data)


class _Unanswered(Exception):
    """GitHub did not answer: a network error, a 5xx or a 429. Constant text."""


class _Refused(Exception):
    """GitHub answered no. Carries GitHub's error code, never a value."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class _Tokens:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime


def _json_headers() -> dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": _USER_AGENT}


def _basic(client_id: str, client_secret: str) -> str:
    import base64

    pair = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    return f"Basic {pair}"


class _GitHub:
    """The calls the flow and the key self-check make. Every value it is
    handed or receives is already registered with the redaction filter by
    its caller."""

    def __init__(self, send: HttpSend, timeout: float = TIMEOUT_SECONDS) -> None:
        self._send = send
        self._timeout = timeout

    def _call(self, method: str, url: str, headers: dict[str, str],
              body: dict[str, Any] | None) -> HttpAnswer:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            answer = self._send(method, url, headers, data, self._timeout)
        except Exception as exc:
            raise _Unanswered(f"{urlparse(url).path}: no answer ({type(exc).__name__})") from None
        if answer.status >= 500 or answer.status == 429:
            raise _Unanswered(f"{urlparse(url).path}: GitHub answered HTTP {answer.status}")
        return answer

    def _token(self, body: dict[str, Any], now: datetime) -> _Tokens:
        answer = self._call("POST", TOKEN_URL, _json_headers(), body)
        data = answer.json()
        if answer.status != 200 or not isinstance(data, dict):
            raise _Refused(f"http_{answer.status}")
        if data.get("error"):
            # GitHub's code (bad_verification_code, bad_refresh_token, ...):
            # a fixed vocabulary, never a value.
            error = str(data.get("error"))
            raise _Refused(error if error.replace("_", "").isalnum() else "error")
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) \
                or not refresh:
            # An App without "Expire user authorization tokens" answers no
            # refresh token; the runbook turns it on, and this refuses rather
            # than store a token nobody can refresh or revoke from here.
            raise _Refused("no_refresh_token")
        return _Tokens(
            access=access,
            refresh=refresh,
            access_expires_at=now + _seconds(data.get("expires_in"), DEFAULT_ACCESS_TTL),
            refresh_expires_at=now + _seconds(data.get("refresh_token_expires_in"),
                                              DEFAULT_REFRESH_TTL),
        )

    def exchange(self, client_id: str, client_secret: str, code: str, now: datetime) -> _Tokens:
        return self._token({"client_id": client_id, "client_secret": client_secret,
                            "code": code}, now)

    def refresh(self, client_id: str, client_secret: str, refresh_token: str,
                now: datetime) -> _Tokens:
        return self._token({"client_id": client_id, "client_secret": client_secret,
                            "grant_type": "refresh_token", "refresh_token": refresh_token}, now)

    def user(self, access: str) -> tuple[str, int | None]:
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {access}",
                   "User-Agent": _USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        answer = self._call("GET", USER_URL, headers, None)
        data = answer.json()
        if answer.status != 200 or not isinstance(data, dict) \
                or not isinstance(data.get("login"), str):
            raise _Refused(f"user_http_{answer.status}")
        user_id = data.get("id")
        return data["login"], user_id if isinstance(user_id, int) else None

    def revoke_grant(self, client_id: str, client_secret: str, access: str) -> bool:
        """`DELETE /applications/{client_id}/grant`: ends the authorisation
        and every token of it. True when GitHub revoked it now; False when
        it says the token is already invalid (404, 422)."""
        url = f"https://api.github.com/applications/{quote(client_id, safe='')}/grant"
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": _basic(client_id, client_secret),
                   "Content-Type": "application/json", "User-Agent": _USER_AGENT,
                   "X-GitHub-Api-Version": "2022-11-28"}
        answer = self._call("DELETE", url, headers, {"access_token": access})
        if answer.status == 204:
            return True
        if answer.status in (404, 422):
            return False
        raise _Refused(f"revoke_http_{answer.status}")


    def app(self, jwt: str) -> HttpAnswer:
        """`GET /app` as the App itself: the one call an App JWT answers."""
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {jwt}",
                   "User-Agent": _USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        return self._call("GET", APP_URL, headers, None)


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _KeyUnusable(Exception):
    """The private key is not an RSA PEM this can sign with. Constant text:
    the cryptography error may quote the key's bytes."""


def app_jwt(app_id: str, private_key_pem: str, now: datetime) -> str:
    """A GitHub App JWT: RS256, `iss` the App id, `iat` a minute back, `exp`
    nine minutes on. Signed with `cryptography` (already swarm-api's, for
    childkey), so no JWT library is added for one header and one claim set."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except Exception:
        raise _KeyUnusable() from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise _KeyUnusable()
    issued = int(now.timestamp())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                separators=(",", ":")).encode("utf-8"))
    claims = _b64url(json.dumps({"iat": issued - JWT_BACKDATE_SECONDS,
                                 "exp": issued + JWT_LIFETIME_SECONDS, "iss": app_id},
                                separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{claims}".encode("ascii")
    try:
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise _KeyUnusable() from None
    return f"{header}.{claims}.{_b64url(signature)}"


#: A slug GitHub could have given an App: echoed back only when it looks like one.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


def _seconds(value: Any, default: timedelta) -> timedelta:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return timedelta(seconds=value)
    return default


# --------------------------------------------------------------------------
# the connection (§3.1)
# --------------------------------------------------------------------------

def connection_to_api(doc: dict[str, Any]) -> dict[str, Any]:
    """The connection as served: names, times and states. No field holds a value."""
    lease = doc.get("refresh_lease") or None
    return {
        "connection_id": doc.get("connection_id"),
        "forge": doc.get("forge"),
        "method": doc.get("method"),
        "forge_login": doc.get("forge_login"),
        "forge_user_id": doc.get("forge_user_id"),
        "token_id": doc.get("token_id"),
        "secret_name": doc.get("secret_name"),
        "state": doc.get("state"),
        "failure": doc.get("failure"),
        "access_expires_at": _iso(doc.get("access_expires_at")),
        "refresh_expires_at": _iso(doc.get("refresh_expires_at")),
        "refreshed_at": _iso(doc.get("refreshed_at")),
        "refreshing": lease is not None,
        "created_at": _iso(doc.get("created_at")),
        "connected_at": _iso(doc.get("connected_at")),
        "revoked_at": _iso(doc.get("revoked_at")),
    }


@dataclass
class RefreshReport:
    """One sweep, in counts and connection ids. Never a value."""

    considered: int = 0
    due: int = 0
    refreshed: int = 0
    failed: int = 0
    unreachable: int = 0
    leased_elsewhere: int = 0
    errors: list[str] = field(default_factory=list)
    out_of_time: bool = False
    #: False while the App is not configured and no connection is active.
    configured: bool = True
    message: str | None = None
    #: ORG_APPROVAL_PENDING's re-check (`ForgeApp.on_sweep`): the people
    #: holding a requested owner whose installations were read, the owners
    #: found installed and enabled, and the reads that did not come back or
    #: were refused.
    installs_rechecked: int = 0
    installs_found: int = 0
    installs_unreachable: int = 0
    installs_refused: int = 0

    def to_api(self) -> dict[str, Any]:
        return {"configured": self.configured, "message": self.message,
                "considered": self.considered, "due": self.due, "refreshed": self.refreshed,
                "failed": self.failed, "unreachable": self.unreachable,
                "leased_elsewhere": self.leased_elsewhere, "errors": list(self.errors),
                "out_of_time": self.out_of_time,
                "installs_rechecked": self.installs_rechecked,
                "installs_found": self.installs_found,
                "installs_unreachable": self.installs_unreachable,
                "installs_refused": self.installs_refused}


#: What the sweep calls after its refreshes, with the report to count into
#: and whether the sweep's time is spent: the access service's re-check of
#: install requests (`AccessService.recheck_requested`).
SweepHook = Callable[[RefreshReport, Callable[[], bool]], None]


class TokenRefresher(Protocol):
    """D2's seam: what the scheduler's tick calls. swarm-api implements it
    today (`ForgeApp`); a dedicated forge broker can take it over by
    answering the same sweep from its own identity."""

    def sweep(self) -> RefreshReport: ...


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Caller:
    """The VERIFIED caller and the tenant `tenant_scope` resolved."""

    email: str
    tenant_id: str

    @property
    def key(self) -> str:
        return _user_key(self.email)


class ForgeApp:
    """Authorise, exchange, refresh and disconnect, over one Firestore."""

    def __init__(
        self,
        db: Any,
        *,
        config: AppConfig,
        secrets: AppSecrets,
        slots: UserSlots,
        send: HttpSend = urllib_send,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = _time.monotonic,
        budget_seconds: float = SWEEP_BUDGET_SECONDS,
    ) -> None:
        self._db = db
        self.config = config
        self._secrets = secrets
        self._slots = slots
        self._send = send
        self._github = _GitHub(send)
        self._now = now
        self._clock = clock
        self._budget = budget_seconds
        self._after_sweep: SweepHook | None = None

    def on_sweep(self, hook: SweepHook) -> None:
        """Run `hook` at the end of every sweep, inside its time budget. The
        access service hangs its re-check of install requests here
        (ORG_APPROVAL_PENDING: §2.3's copy promises one every 15 minutes, the
        sweep's schedule). One hook: the latest service built over this
        connection store is the one that answers."""
        self._after_sweep = hook

    # -- configuration ------------------------------------------------------

    def _require_client_id(self) -> str:
        missing = self.config.missing()
        if missing:
            raise AppNotConfigured(f"{', '.join(missing)} is not set in swarm-api's environment")
        return self.config.client_id

    def _client_secret(self) -> str:
        """Read at request time, never cached, never logged, never returned."""
        self._require_client_id()
        return self._secrets.client_secret()

    # -- authorise ----------------------------------------------------------

    def authorize(self, caller: Caller, surface: str) -> dict[str, Any]:
        client_id = self._require_client_id()
        # Read and dropped: a user is not sent through GitHub to an exchange
        # that would then answer "not configured".
        with redaction_literal(self._client_secret()):
            pass
        now = self._now()
        state = _secrets.token_urlsafe(32)
        self._db.collection(PENDING_COLLECTION).document(state_hash(state)).set({
            "tenant_id": caller.tenant_id,
            "user": caller.key,
            "user_hash": user_hash(caller.key),
            "surface": surface,
            "created_at": now,
            "expires_at": now + STATE_TTL,
            "used_at": None,
        })
        log.info("github authorisation started tenant=%s user_hash=%s surface=%s",
                 caller.tenant_id, user_hash(caller.key), surface)
        url = AUTHORIZE_URL + "?" + urlencode({"client_id": client_id, "state": state})
        return {"authorize_url": url, "expires_in_seconds": int(STATE_TTL.total_seconds())}

    def _spend_state(self, caller: Caller, state: str) -> dict[str, Any]:
        """Mark the caller's pending authorisation used, once. Unknown,
        another caller's, used or expired are one answer, and another
        caller's is NOT spent: posting someone's state must not cancel it."""
        expired = AuthorisationRefused(
            "AUTHORISATION_EXPIRED",
            "this sign-in link is unknown, already used or older than 10 minutes")
        if not state:
            raise expired
        ref = self._db.collection(PENDING_COLLECTION).document(state_hash(state))
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != caller.tenant_id \
                    or data.get("user") != caller.key:
                return None
            if data.get("used_at") is not None:
                return None
            txn.update(ref, {"used_at": now})
            return data

        data = _apply(transaction)
        if data is None:
            raise expired
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= now:
            raise expired
        return data

    # -- exchange -----------------------------------------------------------

    def exchange(self, caller: Caller, *, state: str, code: str | None,
                 error: str | None) -> dict[str, Any]:
        client_id = self._require_client_id()
        client_secret = self._client_secret()
        with ExitStack() as held:
            held.enter_context(redaction_literal(client_secret))
            held.enter_context(redaction_literal(code or ""))
            self._spend_state(caller, state)
            if error:
                log.info("github authorisation ended at the callback tenant=%s user_hash=%s "
                         "error=%s", caller.tenant_id, user_hash(caller.key), error)
                if error == "access_denied":
                    raise AuthorisationRefused(
                        "AUTHORISATION_DENIED", "GitHub says the authorisation was cancelled")
                raise AuthorisationRefused(
                    "AUTHORISATION_EXPIRED", f"GitHub ended the authorisation ({error})")
            if not code:
                raise AuthorisationRefused("AUTHORISATION_EXPIRED",
                                           "the callback carried no code")
            now = self._now()
            try:
                tokens = self._github.exchange(client_id, client_secret, code, now)
            except _Unanswered as unanswered:
                log.warning("github code exchange unanswered tenant=%s user_hash=%s (%s)",
                            caller.tenant_id, user_hash(caller.key), unanswered)
                raise _unreachable("GitHub did not answer the code exchange") from None
            except _Refused as refused:
                log.info("github code exchange refused tenant=%s user_hash=%s error=%s",
                         caller.tenant_id, user_hash(caller.key), refused.error)
                raise AuthorisationRefused(
                    "AUTHORISATION_EXPIRED",
                    f"GitHub refused the authorisation code ({refused.error})") from None
            held.enter_context(redaction_literal(tokens.access))
            held.enter_context(redaction_literal(tokens.refresh))
            try:
                login, forge_user_id = self._github.user(tokens.access)
            except _Unanswered:
                raise _unreachable("GitHub did not answer GET /user") from None
            except _Refused as refused:
                raise AuthorisationRefused(
                    "AUTHORISATION_EXPIRED",
                    f"GitHub refused the new token's GET /user ({refused.error})") from None
            try:
                doc = self._store(caller, tokens, login=login, forge_user_id=forge_user_id,
                                  now=now)
            except ApiError:
                raise
            except Exception as exc:
                # Named by type, never by text: a client error's message may
                # quote the request it failed on.
                log.error("github token not stored tenant=%s user_hash=%s (%s)",
                          caller.tenant_id, user_hash(caller.key), type(exc).__name__)
                raise UpstreamUnavailable(
                    "the GitHub token could not be stored in your slot "
                    f"({type(exc).__name__}); press Connect GitHub to try again") from None
        log.info("github connected tenant=%s user_hash=%s connection=%s login=%s",
                 caller.tenant_id, user_hash(caller.key), doc["connection_id"], login)
        return {"connection": connection_to_api(doc)}

    def _store(self, caller: Caller, tokens: _Tokens, *, login: str,
               forge_user_id: int | None, now: datetime) -> dict[str, Any]:
        """Write the values to the slots (creating them, D3), then the
        records that name them. The refresh twin first: a crash between the
        two leaves a refreshable connection, never an access token with no
        way to renew or revoke it."""
        suffix = provider_suffix(Scope.USER, user=caller.key)
        twin = refresh_suffix(suffix)
        for slot in (twin, suffix):
            if self._slots.ensure(caller.tenant_id, slot):
                log.info("user slot created tenant=%s secret=%s", caller.tenant_id,
                         secret_name_for(caller.tenant_id, slot))
        self._slots.add_version(caller.tenant_id, twin, tokens.refresh)
        version = self._slots.add_version(caller.tenant_id, suffix, tokens.access)

        tokens_db = GitTokens(self._db, now=lambda: now)
        record, _ = tokens_db.register(record_for_slot(
            caller.tenant_id, Scope.USER, user=caller.key, registered_by=caller.key, now=now))
        self._db.collection(TOKENS_COLLECTION).document(record.token_id).update({
            "kind": METHOD_APP_USER,
            "forge_login": login,
            "expires_at": tokens.access_expires_at,
            "rotated_at": now,
            "secret_version": version or None,
            "state": TokenState.ACTIVE.value,
            # The exchange's `GET /user` is §2.2's probe for
            # `github_connected`: without this evidence the onboarding
            # checklist reads the step it just completed as stale.
            "verified_at": now,
            "probe_attempted_at": now,
            "probe_complete": True,
            "probe_error": None,
        })

        conn_id = connection_id_for(caller.tenant_id, caller.key)
        ref = self._db.collection(CONNECTIONS).document(conn_id)
        snap = ref.get()
        previous = snap.to_dict() if snap.exists else None
        if previous is not None and previous.get("tenant_id") != caller.tenant_id:
            # A connection id is a hash of the tenant: equal ids across
            # tenants would be a collision, refused rather than shared.
            raise NotFound("connection not found")
        doc = {
            "connection_id": conn_id,
            "tenant_id": caller.tenant_id,
            "user": caller.key,
            "user_hash": user_hash(caller.key),
            "forge": FORGE,
            "method": METHOD_APP_USER,
            "forge_login": login,
            "forge_user_id": forge_user_id,
            "token_id": record.token_id,
            "secret_name": record.secret_name,
            "access_expires_at": tokens.access_expires_at,
            "refresh_expires_at": tokens.refresh_expires_at,
            "refreshed_at": None,
            "refresh_lease": None,
            "state": ACTIVE,
            "failure": None,
            "created_at": (previous or {}).get("created_at") or now,
            "connected_at": now,
            "revoked_at": None,
            "revoked_by": None,
        }
        ref.set(doc)
        return doc

    # -- the refresh sweep (D2) ----------------------------------------------

    def sweep(self) -> RefreshReport:
        try:
            client_id = self._require_client_id()
            client_secret = self._client_secret()
        except AppNotConfigured as missing:
            # Not configured with no connection to keep alive is nothing
            # left undone: answered as such, with the reason, and the job
            # stays green. Not configured while a connection is active is
            # every such user's token running out within eight hours: 503,
            # so the job's failures show it.
            active = self._db.collection(CONNECTIONS).where(
                filter=FieldFilter("state", "==", ACTIVE)).limit(1)
            if any(True for _ in active.stream()):
                raise
            return RefreshReport(configured=False, message=missing.message)
        report = RefreshReport()
        started = self._clock()
        with redaction_literal(client_secret):
            query = self._db.collection(CONNECTIONS).where(
                filter=FieldFilter("state", "==", ACTIVE)).limit(MAX_CONNECTIONS)
            docs = [snap.to_dict() for snap in query.stream()]
            report.considered = len(docs)
            now = self._now()
            due = [d for d in docs if d.get("state") == ACTIVE and _due(d, now)]
            due.sort(key=lambda d: d.get("access_expires_at") or datetime.min.replace(
                tzinfo=now.tzinfo))
            report.due = len(due)
            for doc in due[:MAX_REFRESHES_PER_SWEEP]:
                if self._clock() - started > self._budget:
                    report.out_of_time = True
                    break
                self._refresh_one(doc, client_id, client_secret, report)
            if len(due) > MAX_REFRESHES_PER_SWEEP:
                report.out_of_time = True
        if self._after_sweep is not None and not report.out_of_time:
            try:
                self._after_sweep(report, lambda: self._clock() - started > self._budget)
            except Exception as exc:
                # The refreshes above are done and stored; a re-check that
                # broke is said, never allowed to fail them.
                report.errors.append(f"install re-check: {type(exc).__name__}")
                log.warning("forge refresh sweep install re-check failed (%s)",
                            type(exc).__name__)
        log.info("forge refresh sweep considered=%d due=%d refreshed=%d failed=%d "
                 "unreachable=%d leased_elsewhere=%d errors=%d installs_rechecked=%d "
                 "installs_found=%d", report.considered, report.due, report.refreshed,
                 report.failed, report.unreachable, report.leased_elsewhere, len(report.errors),
                 report.installs_rechecked, report.installs_found)
        return report

    def _take_lease(self, conn_id: str, holder: str) -> dict[str, Any] | None:
        """The connection, leased to `holder`; None when it is no longer
        active or another refresher's lease is still running."""
        ref = self._db.collection(CONNECTIONS).document(conn_id)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("state") != ACTIVE:
                return None
            lease = data.get("refresh_lease") or None
            if lease and lease.get("holder") != holder and isinstance(lease.get("until"),
                                                                      datetime) \
                    and lease["until"] > now:
                return None
            txn.update(ref, {"refresh_lease": {"holder": holder, "until": now + LEASE_FOR}})
            return data

        return _apply(transaction)

    def _finish(self, conn_id: str, holder: str, fields: dict[str, Any]) -> None:
        """Write `fields` and drop the lease, only while `holder` still holds it."""
        ref = self._db.collection(CONNECTIONS).document(conn_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None:
                return
            lease = data.get("refresh_lease") or {}
            if lease.get("holder") != holder:
                return
            txn.update(ref, {**fields, "refresh_lease": None})

        _apply(transaction)

    def _refresh_one(self, doc: dict[str, Any], client_id: str, client_secret: str,
                     report: RefreshReport) -> None:
        conn_id = str(doc.get("connection_id") or "")
        holder = uuid.uuid4().hex
        current = self._take_lease(conn_id, holder)
        if current is None:
            report.leased_elsewhere += 1
            return
        tenant_id, email = current["tenant_id"], current["user"]
        suffix = provider_suffix(Scope.USER, user=email)
        now = self._now()
        refresh_expires = current.get("refresh_expires_at")
        if isinstance(refresh_expires, datetime) and refresh_expires <= now:
            self._mark_failed(current, holder, "the refresh token is past its expiry")
            report.failed += 1
            return
        try:
            refresh_token = self._slots.read_refresh(tenant_id, refresh_suffix(suffix))
        except Exception as exc:
            # Not GitHub's refusal: the platform could not read its own slot.
            # Left active for the next tick, and named.
            reason = str(exc) if isinstance(exc, SlotUnreadable) else type(exc).__name__
            log.warning("forge refresh could not read the refresh slot connection=%s (%s)",
                        conn_id, reason)
            self._finish(conn_id, holder, {})
            report.errors.append(conn_id)
            return
        with redaction_literal(refresh_token):
            try:
                tokens = self._github.refresh(client_id, client_secret, refresh_token, now)
            except _Unanswered as unanswered:
                log.warning("forge refresh unanswered connection=%s (%s)", conn_id, unanswered)
                self._finish(conn_id, holder, {})
                report.unreachable += 1
                return
            except _Refused as refused:
                self._mark_failed(current, holder, f"GitHub refused the refresh ({refused.error})")
                report.failed += 1
                return
            refresh_token = ""
            with redaction_literal(tokens.access), redaction_literal(tokens.refresh):
                try:
                    self._slots.add_version(tenant_id, refresh_suffix(suffix), tokens.refresh)
                    version = self._slots.add_version(tenant_id, suffix, tokens.access)
                except Exception as exc:
                    # The old refresh token is spent and the new one is not
                    # stored: nothing can renew this connection now. Said so
                    # on the connection, so the user is asked to reconnect.
                    log.error("forge refresh could not store the new tokens connection=%s (%s)",
                              conn_id, type(exc).__name__)
                    self._mark_failed(current, holder,
                                      f"the refreshed token could not be stored "
                                      f"({type(exc).__name__})")
                    report.failed += 1
                    return
        self._finish(conn_id, holder, {
            "access_expires_at": tokens.access_expires_at,
            "refresh_expires_at": tokens.refresh_expires_at,
            "refreshed_at": now,
        })
        token_id = current.get("token_id")
        if token_id:
            # A refresh GitHub accepted is fresh evidence the authorisation
            # is live, so `github_connected` stays done between connections.
            self._update_record(tenant_id, token_id, {
                "expires_at": tokens.access_expires_at, "rotated_at": now,
                "secret_version": version or None, "verified_at": now})
        report.refreshed += 1
        log.info("forge token refreshed tenant=%s connection=%s", tenant_id, conn_id)

    def _mark_failed(self, current: dict[str, Any], holder: str, reason: str) -> None:
        login = current.get("forge_login") or ""
        self._finish(current["connection_id"], holder, {
            "state": REFRESH_FAILED,
            "failure": {"code": "REFRESH_FAILED",
                        "recovery": recovery_copy("REFRESH_FAILED", login=login)},
            "failed_at": self._now(),
        })
        token_id = current.get("token_id")
        if token_id:
            # The resolver passes an expired record over (gittokens.UNUSABLE).
            self._update_record(current["tenant_id"], token_id,
                                {"state": TokenState.EXPIRED.value}, unless_revoked=True)
        log.warning("forge refresh failed tenant=%s connection=%s: %s",
                    current["tenant_id"], current["connection_id"], reason)

    def _update_record(self, tenant_id: str, token_id: str, fields: dict[str, Any], *,
                       unless_revoked: bool = False) -> None:
        ref = self._db.collection(TOKENS_COLLECTION).document(token_id)
        snap = ref.get()
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            return
        if unless_revoked and data.get("state") == TokenState.REVOKED.value:
            return
        ref.update(fields)

    # -- disconnect ---------------------------------------------------------

    def disconnect(self, caller: Caller) -> dict[str, Any]:
        owner_docs = self.owner_token_docs(caller)
        conn_id = connection_id_for(caller.tenant_id, caller.key)
        ref = self._db.collection(CONNECTIONS).document(conn_id)
        snap = ref.get()
        doc = snap.to_dict() if snap.exists else None
        if doc is not None and (doc.get("tenant_id") != caller.tenant_id
                                or doc.get("user") != caller.key):
            doc = None
        if doc is None and owner_docs:
            # D5: a person who reached GitHub only through owner tokens has
            # no App authorisation to revoke, so the App need not be
            # configured for them to disconnect.
            return self._disconnect_tokens_only(caller, owner_docs)
        client_id = self._require_client_id()
        client_secret = self._client_secret()
        if doc is None:
            raise NotFound("you have no GitHub connection in this tenant")
        suffix = provider_suffix(Scope.USER, user=caller.key)
        holder = uuid.uuid4().hex
        github_revoked = False
        github_said = "already revoked: the connection was revoked before"
        if doc.get("state") != REVOKED:
            # The refresh lease, so the sweep does not spend the refresh
            # token this is about to spend -- nor this the one a running
            # refresh is replacing.
            if not self._lease_for_disconnect(conn_id, holder):
                raise Conflict("a refresh of this connection's token is running; "
                               "disconnect again in a minute")
            with redaction_literal(client_secret):
                github_revoked, github_said = self._revoke_at_github(
                    caller, doc, suffix, client_id, client_secret)
        disabled = {}
        for slot in (suffix, refresh_suffix(suffix)):
            try:
                disabled[secret_name_for(caller.tenant_id, slot)] = self._slots.disable(
                    caller.tenant_id, slot)
            except Exception as exc:
                log.error("user slot versions not disabled tenant=%s secret=%s (%s)",
                          caller.tenant_id, secret_name_for(caller.tenant_id, slot),
                          type(exc).__name__)
                disabled[secret_name_for(caller.tenant_id, slot)] = (
                    f"not disabled ({type(exc).__name__})")
        owners = self._revoke_owner_tokens(caller, owner_docs, disabled)
        grants = self._delete_grants(caller)
        now = self._now()
        fields = {"state": REVOKED, "refresh_lease": None, "failure": None}
        if doc.get("state") != REVOKED:
            fields.update({"revoked_at": now, "revoked_by": caller.key})
        ref.update(fields)
        token_id = doc.get("token_id")
        if token_id:
            GitTokens(self._db, now=lambda: now).revoke(
                caller.tenant_id, token_id, by=caller.key, allowed=lambda _record: None)
        doc.update(fields)
        log.info("github disconnected tenant=%s user_hash=%s connection=%s github_revoked=%s "
                 "grants_deleted=%d", caller.tenant_id, user_hash(caller.key), conn_id,
                 github_revoked, grants)
        return {"connection": connection_to_api(doc), "github_revoked": github_revoked,
                "github": github_said, "slot_versions_disabled": disabled,
                "grants_deleted": grants, "owner_tokens_revoked": owners}

    def _disconnect_tokens_only(self, caller: Caller,
                                owner_docs: list[dict[str, Any]]) -> dict[str, Any]:
        disabled: dict[str, Any] = {}
        owners = self._revoke_owner_tokens(caller, owner_docs, disabled)
        grants = self._delete_grants(caller)
        log.info("github disconnected tenant=%s user_hash=%s owner_tokens=%d grants_deleted=%d",
                 caller.tenant_id, user_hash(caller.key), len(owners), grants)
        return {"connection": None, "github_revoked": False,
                "github": ("no GitHub App connection to revoke; each owner token was disabled "
                           f"in SwarmCloud -- delete it at GitHub too, at {TOKENS_PAGE}"),
                "slot_versions_disabled": disabled, "grants_deleted": grants,
                "owner_tokens_revoked": owners}

    def _revoke_owner_tokens(self, caller: Caller, owner_docs: list[dict[str, Any]],
                             disabled: dict[str, Any]) -> list[str]:
        """Disable each owner token's slot, revoke its record and delete its
        `forge_orgs` document: the owner is no longer enabled through it."""
        owners: list[str] = []
        for org in owner_docs:
            owner = str(org.get("owner") or "").lower()
            try:
                disabled.update(self.revoke_owner_token(caller, owner))
            except UpstreamUnavailable:
                # The document stays, so disconnecting again finds and
                # disables the token; the answer says it was not.
                disabled[secret_name_for(caller.tenant_id, owner_suffix(caller.key, owner))] = \
                    "not disabled: disconnect again"
                continue
            self._db.collection(ORGS).document(
                f"{caller.tenant_id}__{user_hash(caller.key)}__{owner}").delete()
            owners.append(owner)
        return sorted(owners)

    def _lease_for_disconnect(self, conn_id: str, holder: str) -> bool:
        """Take the refresh lease whatever the connection's state; False
        while another holder's lease is still running."""
        ref = self._db.collection(CONNECTIONS).document(conn_id)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None:
                return False
            lease = data.get("refresh_lease") or None
            if lease and lease.get("holder") != holder and isinstance(lease.get("until"),
                                                                      datetime) \
                    and lease["until"] > now:
                return False
            txn.update(ref, {"refresh_lease": {"holder": holder, "until": now + LEASE_FOR}})
            return True

        return _apply(transaction)

    def _revoke_at_github(self, caller: Caller, doc: dict[str, Any], suffix: str,
                          client_id: str, client_secret: str) -> tuple[bool, str]:
        """Revoke the authorisation, with the person's CURRENT access token
        while the connection says it is unexpired -- no refresh, so no
        version is minted only to be revoked (owner decision 2026-10-08).
        When it has expired, cannot be read, or GitHub no longer knows it,
        refresh once and revoke with the token that minted; the grant's
        revocation ends the new refresh token too."""
        manual = f"revoke it yourself at {AUTHORIZATIONS_PAGE}"
        expires = doc.get("access_expires_at")
        if isinstance(expires, datetime) and expires > self._now():
            try:
                current = self._slots.read_access(caller.tenant_id, suffix)
            except Exception as exc:
                log.warning("disconnect could not read the access token tenant=%s (%s); "
                            "refreshing to revoke", caller.tenant_id, type(exc).__name__)
                current = ""
            if current:
                with redaction_literal(current):
                    try:
                        if self._github.revoke_grant(client_id, client_secret, current):
                            return True, ("revoked at GitHub: the authorisation and every "
                                          "token of it")
                    except _Unanswered:
                        return False, f"not revoked at GitHub: GitHub did not answer; {manual}"
                    except _Refused as refused:
                        return False, f"not revoked at GitHub ({refused.error}); {manual}"
                # GitHub no longer knew that token: a refresh may still hold
                # the authorisation, so revoke with the token it mints.
        try:
            refresh_token = self._slots.read_refresh(caller.tenant_id, refresh_suffix(suffix))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SlotUnreadable) else type(exc).__name__
            return False, f"not revoked at GitHub: the refresh token could not be read " \
                          f"({reason}); {manual}"
        with redaction_literal(refresh_token):
            try:
                tokens = self._github.refresh(client_id, client_secret, refresh_token,
                                              self._now())
            except _Unanswered:
                return False, f"not revoked at GitHub: GitHub did not answer; {manual}"
            except _Refused:
                return False, ("already ended at GitHub: it refused the refresh token, so "
                               "no token of this authorisation works")
        with redaction_literal(tokens.access), redaction_literal(tokens.refresh):
            try:
                revoked = self._github.revoke_grant(client_id, client_secret, tokens.access)
            except _Unanswered:
                return False, f"not revoked at GitHub: GitHub did not answer; {manual}"
            except _Refused as refused:
                return False, f"not revoked at GitHub ({refused.error}); {manual}"
        if revoked:
            return True, "revoked at GitHub: the authorisation and every token of it"
        return False, "already ended at GitHub: it no longer knew the token"

    def _delete_grants(self, caller: Caller) -> int:
        """§3.2: disconnect deletes the caller's grants, in their tenant only."""
        hashed = user_hash(caller.key)
        query = self._db.collection(GRANTS).where(
            filter=FieldFilter("tenant_id", "==", caller.tenant_id)).where(
            filter=FieldFilter("user_hash", "==", hashed)).limit(MAX_CONNECTIONS)
        count = 0
        for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get("tenant_id") != caller.tenant_id or data.get("user_hash") != hashed:
                continue
            snap.reference.delete()
            count += 1
        return count

    # -- D5: a fallback token for one owner ----------------------------------

    def owner_token_docs(self, caller: Caller) -> list[dict[str, Any]]:
        """The caller's owners enabled through a token, in their tenant only."""
        hashed = user_hash(caller.key)
        query = self._db.collection(ORGS).where(
            filter=FieldFilter("tenant_id", "==", caller.tenant_id)).where(
            filter=FieldFilter("user_hash", "==", hashed)).limit(MAX_CONNECTIONS)
        docs = [snap.to_dict() or {} for snap in query.stream()]
        return [d for d in docs if d.get("tenant_id") == caller.tenant_id
                and d.get("user") == caller.key and d.get("method") == METHOD_PAT]

    def _granted_under(self, caller: Caller, owner: str) -> list[tuple[str, str]]:
        """`(repo_id, owner/repo)` of the caller's grants under `owner`."""
        hashed = user_hash(caller.key)
        query = self._db.collection(GRANTS).where(
            filter=FieldFilter("tenant_id", "==", caller.tenant_id)).where(
            filter=FieldFilter("user_hash", "==", hashed)).limit(MAX_CONNECTIONS)
        rows = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get("tenant_id") != caller.tenant_id or data.get("user_hash") != hashed \
                    or str(data.get("owner") or "").lower() != owner:
                continue
            rows.append((str(data.get("repo_id")), str(data.get("repository"))))
        return sorted(rows)

    def _probe_send(self, url: str, headers: dict[str, str], timeout: float) -> HttpAnswer:
        """The probe's transport (`gittokens._Forge`) over this service's own."""
        return self._send("GET", url, headers, None, timeout)

    def store_owner_token(self, caller: Caller, owner: str, value: str) -> dict[str, Any]:
        """D5 (#780; §3.2 `POST /v1/onboarding/github/token`): probe a
        person's personal access token against one owner and, only when it
        reaches it, store it ONCE as a new version of their per-owner slot
        (created on first use, as D3 creates the App slot) and enable the
        owner through it. Answers the org document and the token's names,
        never the value.

        The probe runs BEFORE the value is stored, so a refused token never
        has an enabled version -- and the owner's earlier, working token, if
        there is one, stays exactly as it was."""
        kind = token_kind(value)
        if kind not in PAT_KINDS:
            raise ValidationFailed(
                "a fallback token is a personal access token: a fine-grained one "
                "(github_pat_...) or a classic one (ghp_...). Nothing was stored.",
                detail={"field": "token"})
        owner_key = owner.strip().lower()
        suffix = owner_suffix(caller.key, owner_key)
        ref = self._db.collection(ORGS).document(
            f"{caller.tenant_id}__{user_hash(caller.key)}__{owner_key}")
        snap = ref.get()
        previous = snap.to_dict() if snap.exists else None
        if previous is not None and (previous.get("tenant_id") != caller.tenant_id
                                     or previous.get("user") != caller.key):
            # The id is a hash of the tenant and the person: a document there
            # that names anyone else is a collision, refused, never shared.
            raise NotFound(f"{owner_key} not found")
        granted = self._granted_under(caller, owner_key)
        now = self._now()
        with redaction_literal(value):
            reach = probe_owner(value, owner, [name for _, name in granted],
                                send=self._probe_send, now=now)
            self._refuse_owner_token(caller, reach, kind)
            try:
                if self._slots.ensure(caller.tenant_id, suffix):
                    log.info("owner slot created tenant=%s secret=%s", caller.tenant_id,
                             secret_name_for(caller.tenant_id, suffix))
                version = self._slots.add_version(caller.tenant_id, suffix, value)
            except ApiError:
                raise
            except Exception as exc:
                log.error("owner token not stored tenant=%s user_hash=%s owner=%s (%s)",
                          caller.tenant_id, user_hash(caller.key), owner_key,
                          type(exc).__name__)
                raise UpstreamUnavailable(
                    f"the token for {owner_key} could not be stored ({type(exc).__name__}); "
                    "nothing changed, try again") from None
        try:
            doc, stored = self._record_owner_token(caller, owner, reach, kind, suffix,
                                                   version, granted, previous, now)
        except Exception as exc:
            # The value is stored and nothing may name it: disable the slot
            # again rather than leave an enabled version no document or
            # record finds (which no removal could then reach).
            log.error("owner token not recorded tenant=%s user_hash=%s owner=%s (%s); "
                      "disabling its slot", caller.tenant_id, user_hash(caller.key), owner_key,
                      type(exc).__name__)
            try:
                self._slots.disable(caller.tenant_id, suffix)
            except Exception as undo:
                log.error("owner slot versions not disabled tenant=%s secret=%s (%s)",
                          caller.tenant_id, secret_name_for(caller.tenant_id, suffix),
                          type(undo).__name__)
            if isinstance(exc, ApiError):
                raise
            raise UpstreamUnavailable(
                f"the token for {owner_key} could not be recorded ({type(exc).__name__}); its "
                "slot was disabled -- store it again") from None
        login = doc["forge_login"] or ""
        log.info("owner token stored tenant=%s user_hash=%s owner=%s secret=%s kind=%s "
                 "login=%s version=%s", caller.tenant_id, user_hash(caller.key), owner_key,
                 stored.secret_name, kind, login, version)
        return {"org": doc, "token": {
            "token_id": stored.token_id, "secret_name": stored.secret_name, "kind": kind,
            "forge_login": login or None, "state": stored.state.value,
            "expires_at": _iso(stored.expires_at)}}

    def _record_owner_token(self, caller: Caller, owner: str, reach: OwnerReach, kind: str,
                            suffix: str, version: str, granted: list[tuple[str, str]],
                            previous: dict[str, Any] | None, now: datetime
                            ) -> tuple[dict[str, Any], Any]:
        """The records that name a stored owner token: its `git_tokens`
        record, with the probe's evidence, and the owner's `forge_orgs`
        document. Names, times and states; no value."""
        owner_key = owner.strip().lower()
        result = reach.result
        result.read_value = True
        result.version = version or None
        tokens_db = GitTokens(self._db, now=lambda: now)
        record, _ = tokens_db.register(owner_record(
            caller.tenant_id, user=caller.key, owner=owner_key,
            repo_ids=[repo_id for repo_id, _ in granted], registered_by=caller.key, now=now))
        stored = tokens_db._store_probe(record, result, scoped=None)
        login = stored.forge_login or ""
        doc = {
            "tenant_id": caller.tenant_id,
            "user": caller.key,
            "user_hash": user_hash(caller.key),
            "owner": owner_key,
            "owner_login": owner.strip(),
            "owner_type": "User" if reach.own_account else "Organization",
            "installation_id": None,
            "repository_selection": None,
            "pull_requests": None,
            "install_state": "token",
            "sso": "ok",
            "method": METHOD_PAT,
            "token_kind": kind,
            "token_id": stored.token_id,
            "provider_suffix": suffix,
            "secret_name": stored.secret_name,
            "forge_login": login or None,
            "enabled_at": (previous or {}).get("enabled_at") or now,
            "enabled_by": caller.key,
            "checked_at": now,
        }
        self._db.collection(ORGS).document(
            f"{caller.tenant_id}__{user_hash(caller.key)}__{owner_key}").set(doc)
        return doc, stored

    def _refuse_owner_token(self, caller: Caller, reach: OwnerReach, kind: str) -> None:
        """Raise when the token does not reach the owner, with §2.3's code
        and copy where one names the cause. Nothing has been stored."""
        owner = reach.owner
        result = reach.result
        said = (f"tenant={caller.tenant_id} user_hash={user_hash(caller.key)} "
                f"owner={owner.lower()}")
        if result.rejected:
            log.info("owner token refused %s cause=rejected", said)
            raise ValidationFailed(
                "GitHub does not accept this token (HTTP 401 on GET /user): it is mistyped, "
                "expired or revoked. Nothing was stored.", detail={"field": "token"})
        if reach.unanswered or not result.account_complete:
            log.info("owner token unverified %s cause=unanswered", said)
            raise _unreachable("GitHub did not answer the token's check; nothing was stored")
        lowered = owner.lower()
        if lowered in {o.lower() for o in result.sso_required_orgs}:
            log.info("owner token refused %s cause=sso", said)
            raise AuthorisationRefused(
                "SSO_NOT_AUTHORISED",
                f"{owner} uses SAML single sign-on and this token is not authorised for it; "
                "nothing was stored", status=403, owner=owner,
                url=SSO_URL.format(owner=quote(owner, safe="")))
        if lowered in {o.lower() for o in result.classic_blocked_orgs}:
            log.info("owner token refused %s cause=classic_blocked", said)
            raise AuthorisationRefused(
                "CLASSIC_PAT_BLOCKED",
                f"{owner} does not accept classic personal access tokens; nothing was stored",
                status=403, owner=owner)
        reaches_nothing = reach.listing_status == 200 and reach.listed == 0
        if kind == "fine_grained_pat" and not reach.own_account \
                and (reach.hidden or reaches_nothing):
            # A fine-grained token an org has not approved yet sees only its
            # public repositories: the person's chosen repository answers
            # 404, or the org lists nothing at all (§2.3).
            log.info("owner token refused %s cause=fine_grained_pending", said)
            raise AuthorisationRefused(
                "FINE_GRAINED_PAT_PENDING",
                f"this fine-grained token cannot see {owner}'s private repositories yet; "
                "nothing was stored", status=403, owner=owner)
        if reach.listing_status != 200 or reaches_nothing:
            log.info("owner token refused %s cause=cannot_read status=%s", said,
                     reach.listing_status)
            raise ValidationFailed(
                f"this token cannot read {owner}'s repositories (GitHub answered HTTP "
                f"{reach.listing_status} and listed {reach.listed}): create a token whose "
                f"resource owner is {owner}, with access to the repositories you mean to "
                "choose. Nothing was stored.", detail={"field": "owner"})

    def revoke_owner_token(self, caller: Caller, owner: str) -> dict[str, Any]:
        """Disable every enabled version of the caller's slot for `owner` and
        mark its record revoked. The slot is named from the caller and the
        owner, never from a document. Answers `{secret name: versions
        disabled}`.

        A disable that fails RAISES (503), before the record is touched, and
        every caller calls this BEFORE it deletes or overwrites the owner's
        `forge_orgs` document: the document is what finds the slot again, so
        a failure keeps it, and a retry -- or a disconnect -- disables the
        token then, instead of leaving an enabled version nothing names."""
        suffix = owner_suffix(caller.key, owner)
        name = secret_name_for(caller.tenant_id, suffix)
        try:
            disabled = self._slots.disable(caller.tenant_id, suffix)
        except Exception as exc:
            log.error("owner slot versions not disabled tenant=%s secret=%s (%s)",
                      caller.tenant_id, name, type(exc).__name__)
            raise UpstreamUnavailable(
                f"your token for {owner.lower()} could not be disabled in {name} "
                f"({type(exc).__name__}); nothing was removed, try again") from None
        try:
            GitTokens(self._db, now=self._now).revoke(
                caller.tenant_id, owner_token_id(caller.tenant_id, caller.key, owner),
                by=caller.key, allowed=lambda _record: None)
        except NotFound:
            pass
        log.info("owner token revoked tenant=%s user_hash=%s owner=%s secret=%s disabled=%s",
                 caller.tenant_id, user_hash(caller.key), owner.lower(), name, disabled)
        return {name: disabled}

    # -- the private key's self-check (admin) ------------------------------

    def check_app_key(self) -> dict[str, Any]:
        """Prove the stored private key end to end: read it, sign an App JWT,
        and ask GitHub which App that is. Answers `{ok, app_id, slug, reason}`
        and nothing else; `reason` is one of a fixed set of words, never
        GitHub's body, the JWT or any of the key. `app_id` and `slug` are what
        GitHub answered, and only when it answered 200."""
        app_id, slug = self.config.app_id, self.config.slug
        if not (app_id.isascii() and app_id.isdigit()):
            return self._checked(False, reason="app_id_not_configured")
        if not slug:
            return self._checked(False, reason="app_slug_not_configured")
        try:
            pem = self._secrets.private_key()
        except AppNotConfigured:
            return self._checked(False, reason="private_key_missing")
        with ExitStack() as held:
            held.enter_context(redaction_literal(pem))
            try:
                jwt = app_jwt(app_id, pem, self._now())
            except _KeyUnusable:
                return self._checked(False, reason="private_key_malformed")
            held.enter_context(redaction_literal(jwt))
            try:
                answer = self._github.app(jwt)
            except _Unanswered:
                return self._checked(False, reason="github_unreachable")
        if answer.status == 401:
            return self._checked(False, reason="key_rejected")
        data = answer.json()
        if answer.status != 200 or not isinstance(data, dict):
            return self._checked(False, reason="github_refused")
        got_id = data.get("id") if isinstance(data.get("id"), int) else None
        got_slug = data.get("slug") if isinstance(data.get("slug"), str) \
            and _SLUG.match(data["slug"]) else None
        if got_id != int(app_id):
            return self._checked(False, app_id=got_id, slug=got_slug, reason="app_id_mismatch")
        if got_slug != slug:
            return self._checked(False, app_id=got_id, slug=got_slug, reason="app_slug_mismatch")
        return self._checked(True, app_id=got_id, slug=got_slug)

    @staticmethod
    def _checked(ok: bool, *, app_id: int | None = None, slug: str | None = None,
                 reason: str | None = None) -> dict[str, Any]:
        log.info("github app key self-check ok=%s reason=%s", ok, reason)
        return {"ok": ok, "app_id": app_id, "slug": slug, "reason": reason}



def _due(doc: dict[str, Any], now: datetime) -> bool:
    expires = doc.get("access_expires_at")
    return not isinstance(expires, datetime) or expires - now <= REFRESH_WHEN_LEFT


def build_forge_app(db: Any, project_id: str, *, now: Callable[[], datetime] = utcnow,
                    environ: Mapping[str, str] | None = None) -> ForgeApp:
    """Production wiring: the environment's App settings, Secret Manager for
    the client secret and the slots, urllib to GitHub. Builds no client."""
    return ForgeApp(db, config=AppConfig.from_env(environ),
                    secrets=SecretManagerAppSecrets(project_id),
                    slots=SecretManagerUserSlots(project_id), now=now)
