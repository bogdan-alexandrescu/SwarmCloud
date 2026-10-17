"""GitHub App installation tokens for swarm-api's background reads (#1483, QUOTA-INSTALL).

WHY. On 2026-10-17 every GitHub call SwarmCloud made for the eng and
u-bogdan workspaces went out as one person: both workspaces connect through
the App's USER token for bogdan-alexandrescu (`method: app_user`), and the
`-git` slot `ForgeTokens.token_for` reads is that token. The merge wake's CI
reads alone (mergewake.py, one park every `issueci.CI_READ_SECONDS`) cost
~3,450 calls an hour, the stranded-PR sweep ~200-400 more; the person's
5,000/h ran out, push tests read as missing permissions and ten lane
attempts failed at publish. An INSTALLATION token is the App acting as
itself, on its own per-installation quota, so a background read spends
nothing of a person's.

WHAT THIS IS. `InstallationTokens.token_for_repo(tenant, owner, repo)`:

  1. the owner's installation, from the tenant's OWN `forge_orgs` documents
     (access.py writes one per enabled owner: `install_state: installed`
     with the `installation_id` GitHub listed). An owner reached through a
     person's fallback token (`method: pat`, no installation) has none, and
     the answer is None: the caller keeps today's token. Another tenant's
     enabled owner is never this tenant's installation (invariant 9).
  2. `POST /app/installations/{id}/access_tokens`, with the App JWT
     `ForgeApp.signed_app_jwt` signs, scoped to the ONE repository being
     read (`repositories`) and to READ permissions only (`READ_PERMISSIONS`):
     the token can read that repository's pulls, checks and statuses and
     can write nothing.
  3. held in memory until `REFRESH_BEFORE` before GitHub's `expires_at`
     (tokens live an hour), so a park read every 30 s mints about once an
     hour per repository.

A MINT THAT FAILS falls back: the answer is None, the caller reads with
today's token, the failure is logged ONCE per repository (its type and the
HTTP status, never a value) until a mint succeeds again, and the repository
is not minted again for `FAILURE_HOLD`, so an App missing a permission is
not asked every 30 s.

WHAT NEVER LEAVES. The JWT and the minted token are registered with the
redaction filter while in hand; neither is logged, persisted, put in an
exception's text or returned anywhere but to the caller that reads with it.

WRITES STAY THE PERSON'S. Nothing here is used to open a pull request,
comment, re-run a job, merge, or read as the person: only the background
reads that switched (mergewake `_read`, issueci's CI read, the stranded-PR
sweep, the repository index's head poll) ask for one.

WHOSE QUOTA (QUOTA-THROTTLE). `read_credential` answers the token AND the
identity its quota is kept under in `forge_limits.QUOTAS`,
`installation:<id>`: the caller reads inside `forge_limits.acting_as(that)`
so every answer updates the installation's quota, and asks
`QUOTAS.defer(that)` before a background read. A mint's own answer is the
App JWT's quota (`forge_limits.APP_CREDENTIAL`).

`FORGE_INSTALLATION_READS` (settings `forge_installation_reads`, ON by
default) turns the switch off: off, `build_context` wires no source and every
read is today's.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from google.cloud.firestore_v1.base_query import FieldFilter

from . import forge_limits
from .gittokens import redaction_literal

log = logging.getLogger(__name__)

__all__ = [
    "FAILURE_HOLD", "InstallationSource", "InstallationTokens", "READ_PERMISSIONS",
    "REFRESH_BEFORE", "build_installation_tokens", "read_credential", "read_token",
]

#: The collection access.py writes an enabled owner to (forgeapp.ORGS).
ORGS = "forge_orgs"
#: access.INSTALLED: an owner the App is installed on and the person enabled.
INSTALLED = "installed"
#: forgeapp.METHOD_PAT: an owner reached through a person's token, not the App.
METHOD_PAT = "pat"
#: A tenant holds a handful of enabled owners; more than this is not read.
MAX_ORG_DOCS = 200

API = "https://api.github.com"
#: What a background read needs and nothing more: the pull request, its
#: base branch's rules (metadata), check runs, commit statuses and the
#: default branch's head (contents). GitHub refuses a mint (422) that asks
#: for a permission the installation did not grant; that is a fallback.
READ_PERMISSIONS: Mapping[str, str] = {
    "metadata": "read",
    "contents": "read",
    "pull_requests": "read",
    "checks": "read",
    "statuses": "read",
}
#: A token is used until this long before GitHub's `expires_at` (tokens
#: live an hour), so none is sent that could expire mid-read.
REFRESH_BEFORE = 300.0
#: After a failed mint the repository is read with today's token for this
#: long before the App is asked again.
FAILURE_HOLD = 300.0
TIMEOUT_SECONDS = 10.0


class InstallationSource(Protocol):
    def token_for_repo(self, tenant_id: str, owner: str, repo: str) -> str | None: ...


@dataclass
class _Held:
    token: str = field(repr=False)
    use_until: float


class _MintFailed(Exception):
    """A mint that produced no token. Constant text: a kind and a status."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status


def _expiry(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class InstallationTokens:
    """Mints and holds one read-only installation token per (installation, repository)."""

    def __init__(
        self,
        db: Any,
        *,
        app_jwt: Callable[[], str],
        send: Callable[..., Any],
        clock: Callable[[], float] = time.time,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._db = db
        self._app_jwt = app_jwt
        self._send = send
        self._clock = clock
        self._timeout = timeout
        self._held: dict[tuple[int, str], _Held] = {}
        self._failed_until: dict[tuple[str, str], float] = {}
        self._logged: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    # -- which installation --------------------------------------------------

    def installation_for(self, tenant_id: str, owner: str) -> int | None:
        """The installation of `owner` THIS tenant enabled, or None."""
        wanted = owner.lower()
        query = (self._db.collection(ORGS)
                 .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                 .limit(MAX_ORG_DOCS))
        for snap in query.stream():
            doc = snap.to_dict() or {}
            # The query filtered on it; asked again (invariant 9).
            if doc.get("tenant_id") != tenant_id or str(doc.get("owner") or "").lower() != wanted:
                continue
            if doc.get("method") == METHOD_PAT or doc.get("install_state") != INSTALLED:
                continue
            installation = doc.get("installation_id")
            if isinstance(installation, int) and not isinstance(installation, bool) \
                    and installation > 0:
                return installation
        return None

    # -- the token -----------------------------------------------------------

    def token_for_repo(self, tenant_id: str, owner: str, repo: str) -> str | None:
        """A read-only token for `owner/repo` as the App, or None: no
        installation for the owner, or the mint failed (logged once)."""
        found = self.credential_for_repo(tenant_id, owner, repo)
        return found[0] if found is not None else None

    def credential_for_repo(self, tenant_id: str, owner: str,
                            repo: str) -> tuple[str, int] | None:
        """`token_for_repo`'s token and the installation it acts as, or None."""
        key = (owner.lower(), repo.lower())
        now = self._clock()
        with self._lock:
            if self._failed_until.get(key, 0.0) > now:
                return None
        try:
            installation = self.installation_for(tenant_id, owner)
        except Exception as exc:
            self._failed(key, _MintFailed(f"installation lookup {type(exc).__name__}"), now)
            return None
        if installation is None:
            return None
        slot = (installation, key[1])
        with self._lock:
            held = self._held.get(slot)
            if held is not None and held.use_until > now:
                return held.token, installation
        try:
            token, expires = self._mint(installation, repo)
        except _MintFailed as failed:
            self._failed(key, failed, now)
            return None
        except Exception as exc:
            self._failed(key, _MintFailed(type(exc).__name__), now)
            return None
        with self._lock:
            self._held[slot] = _Held(token=token, use_until=expires - REFRESH_BEFORE)
            self._failed_until.pop(key, None)
            recovered = key in self._logged
            self._logged.discard(key)
        if recovered:
            log.info("installation token minted again owner=%s repo=%s installation=%s",
                     key[0], key[1], installation)
        return token, installation

    def _failed(self, key: tuple[str, str], failed: _MintFailed, now: float) -> None:
        with self._lock:
            self._failed_until[key] = now + FAILURE_HOLD
            first = key not in self._logged
            self._logged.add(key)
        if first:
            log.warning("installation token not minted owner=%s repo=%s (%s%s); "
                        "reading with the tenant's token", key[0], key[1], failed.kind,
                        "" if failed.status is None else f" HTTP {failed.status}")

    def _mint(self, installation: int, repo: str) -> tuple[str, float]:
        try:
            jwt = self._app_jwt()
        except Exception as exc:
            raise _MintFailed(f"app jwt {type(exc).__name__}") from None
        with redaction_literal(jwt):
            headers = {"Accept": "application/vnd.github+json",
                       "Authorization": f"Bearer {jwt}",
                       "Content-Type": "application/json",
                       "User-Agent": "swarmcloud-swarm-api",
                       "X-GitHub-Api-Version": "2022-11-28"}
            body = json.dumps({"repositories": [repo],
                               "permissions": dict(READ_PERMISSIONS)}).encode("utf-8")
            try:
                answer = self._send("POST", f"{API}/app/installations/{int(installation)}"
                                    "/access_tokens", headers, body, self._timeout)
            except Exception as exc:
                # The type only: a transport's message can quote the request.
                raise _MintFailed(f"no answer {type(exc).__name__}") from None
            finally:
                headers.clear()
                jwt = ""
        # The mint spends the App JWT's quota, not the installation's.
        forge_limits.QUOTAS.observe_answer(answer, credential=forge_limits.APP_CREDENTIAL)
        status = getattr(answer, "status", None)
        if status != 201:
            raise _MintFailed("refused", status if isinstance(status, int) else None)
        try:
            data = answer.json()
        except Exception:
            data = None
        token = data.get("token") if isinstance(data, dict) else None
        expires = _expiry(data.get("expires_at")) if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or expires is None:
            raise _MintFailed("unreadable answer", status)
        return token, expires


def read_token(source: InstallationSource | None, tenant_id: str, owner: str,
               repo: str) -> str:
    """The installation token a background read of `owner/repo` uses, or ""
    when there is none and the caller keeps today's token. Never raises."""
    if source is None or not owner or not repo:
        return ""
    try:
        return source.token_for_repo(tenant_id, owner, repo) or ""
    except Exception as exc:
        log.warning("installation token source failed owner=%s repo=%s (%s)",
                    owner.lower(), repo.lower(), type(exc).__name__)
        return ""


def read_credential(source: InstallationSource | None, tenant_id: str, owner: str,
                    repo: str) -> tuple[str, str]:
    """`read_token`, with the identity its quota is kept under: ("", "")
    when there is no installation token. A source that cannot name its
    installation (`token_for_repo` only) is keyed by the owner it serves."""
    if source is None or not owner or not repo:
        return "", ""
    named = getattr(source, "credential_for_repo", None)
    try:
        if callable(named):
            found = named(tenant_id, owner, repo)
            if found is None or not found[0]:
                return "", ""
            return found[0], forge_limits.installation_credential(int(found[1]))
        token = source.token_for_repo(tenant_id, owner, repo) or ""
    except Exception as exc:
        log.warning("installation token source failed owner=%s repo=%s (%s)",
                    owner.lower(), repo.lower(), type(exc).__name__)
        return "", ""
    return (token, forge_limits.installation_credential(f"owner/{owner.lower()}")) if token \
        else ("", "")


def build_installation_tokens(db: Any, project_id: str, *,
                              now: Callable[[], datetime] | None = None) -> InstallationTokens:
    """Production wiring over the App `forgeapp.build_forge_app` describes:
    its JWT (`signed_app_jwt`, the private key swarm-api already reads for the
    key self-check and forgeperms, `github_app_accessor`) and its transport
    (urllib, github.com hosts only, no redirects). Builds no client."""
    from swarm_common.models import utcnow

    from .forgeapp import build_forge_app

    app = build_forge_app(db, project_id, now=now or utcnow)
    return InstallationTokens(db, app_jwt=app.signed_app_jwt, send=app.send)
