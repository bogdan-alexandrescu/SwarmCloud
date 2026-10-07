"""The access API: the caller's owners, their repositories, grants and their
verification (docs/onboarding.md §2.2, §2.4, §3.1-§3.2; #780, lane OB4).

Owner decisions of 2026-10-07 on #780 this builds: D1 (the GitHub App with
user access tokens), D6 (verification reads only), D7 (U1: a person acts
through their own slot only), D9 (a read grant is a read grant: SwarmCloud
enforces it, at submission in OB7 and in the worker in OB5 -- here it is
recorded), D10 (chooser A: owners, then the owner's repositories paged and
searched, each Read or Write with push ability shown before Write).

ACTING AS THE PERSON. Every GitHub read here is made with the caller's OWN
user access token, never the tenant token and never another member's.
swarm-api may not read the base slot the worker reads (terraform/bootstrap/
forge_user_slots.tf; `forgeapp`'s docstring), so it gets a usable token the
one way it may: it takes the connection's refresh lease, spends the refresh
token, stores BOTH new values in their slots exactly as the sweep does, and
holds the new access token in memory for the length of one request,
registered with the redaction filter. So each access request is one refresh
-- GitHub sets no limit on refreshes, and a request that would race the
sweep for the single-use refresh token is a 409 instead.

DOCUMENTS (§3.1). `forge_orgs/{tenant}__{user_hash}__{owner}` is an owner
the person enabled; `forge_grants/{tenant}__{user_hash}__{repo_id}` is a
repository they chose, `read` or `write`, with the checks the last verify
stored. Both carry `tenant_id`, `user` and `user_hash`, every read is
filtered on the tenant and the person (in the query and again here), and
another person's or another tenant's document is the same 404 as a missing
one (invariant 9). No field holds a value.

WHAT GITHUB IS ASKED (§2.2), each a read:

  * owners: `GET /user/installations` paged at 100 until a short page, and
    `GET /user/orgs` for orgs with no installation;
  * repositories: `GET /user/installations/{id}/repositories` -- one GitHub
    page per call without a search, and with one, up to MAX_PAGES pages
    filtered here and served a page at a time (GitHub has no search on this
    list); a typed `owner/repo` is read once with `GET /repos/{owner}/{repo}`;
  * verify: clone is the `upload-pack` advertisement; push (write grants)
    is the `receive-pack` advertisement plus `permissions.push`; pull request
    (write grants) is the installation's `pull_requests: write` plus the
    same push bit. D6's opt-in branch write test is not built here.

A READ THAT DID NOT COME BACK IS NOT AN ANSWER: a 5xx, a 429 or a network
error is FORGE_UNREACHABLE (503), a check it decides stays `unknown`, and
nothing is recorded as failed because of it.

NOT HERE. `ORG_APPROVAL_PENDING` (GitHub does not show a user token an
install request it is waiting on), the D5 fallback PAT, and the onboarding
checklist reading these documents (onboarding.py, OB1's) are other lanes'.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlencode

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Tenant, utcnow

from .errors import ApiError, Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed
from .forge import ForgeTokens, git_basic_headers, repository_from
from .forgeapp import (
    CONNECTIONS,
    GRANTS,
    REFRESH_FAILED,
    REVOKED,
    Caller,
    ForgeApp,
    HttpAnswer,
    HttpSend,
    _GitHub,
    _Refused,
    _Unanswered,
    connection_id_for,
    connection_to_api,
    refresh_suffix,
    urllib_send,
    user_hash,
)
from .gittokens import Scope, parse_sso_header, provider_suffix, redaction_literal
from .onboarding import recovery_copy
from .repositories import (
    SCOPE_USER,
    CredentialSource,
    HeldCredential,
    Repositories,
    RepositoryCreate,
    parse_repository,
    registration_record,
    repo_id_for,
    tenant_credential,
)
from .validation import _ISSUE_OWNER

log = logging.getLogger(__name__)

__all__ = [
    "AccessRefused", "AccessService", "CHECKS", "MODES", "NotConnected", "ORGS",
    "grant_id_for", "org_id_for",
]

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

ORGS = "forge_orgs"

API = "https://api.github.com"
_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "swarmcloud-swarm-api"}

MODES = ("read", "write")
CHECKS = ("clone", "push", "pull_request")
OK, MISSING, UNKNOWN, NOT_REQUIRED = "ok", "missing", "unknown", "not_required"

#: GitHub's page size for every list here, and the most pages one request
#: reads: 1,000 installations, orgs or repositories. Past that a person types
#: `owner/repo`, which the grant reads once either way.
PAGE_SIZE = 100
MAX_PAGES = 10

#: The longest search string. A search is a case-insensitive substring of
#: the repository's name, matched here, never sent to GitHub.
MAX_QUERY_CHARS = 100

#: The most grants or orgs one person's lists read: far above what a person
#: chooses, bounded so a list cannot grow a request without limit.
MAX_DOCS = 1000

#: Where an org owner installs the App or changes its repositories.
INSTALL_URL = "https://github.com/apps/{slug}/installations/new"
INSTALLATION_SETTINGS_URL = "https://github.com/settings/installations/{installation_id}"
SSO_URL = "https://github.com/orgs/{owner}/sso"

_OWNER = re.compile(rf"^{_ISSUE_OWNER}$")
_REPO_ID = re.compile(r"^repo_[0-9a-f]{16}$")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class AccessRefused(ApiError):
    """A §2.3 failure, served with its code and its recovery copy filled."""

    status_code = 409
    code = "access_refused"

    def __init__(self, failure_code: str, message: str, *, status: int | None = None,
                 owner: str = "", repo: str = "", url: str = "", login: str = "") -> None:
        super().__init__(message, detail={
            "failure_code": failure_code,
            "recovery": recovery_copy(failure_code, owner=owner, repo=repo, url=url,
                                      login=login),
            "url": url or None,
        })
        if status is not None:
            self.status_code = status


class NotConnected(ApiError):
    """The caller has no active GitHub connection in this tenant."""

    status_code = 409
    code = "github_not_connected"

    def __init__(self) -> None:
        super().__init__("you have not connected GitHub in this tenant: press Connect GitHub "
                         "(POST /v1/onboarding/github/authorize) first")


def _unreachable(what: str) -> AccessRefused:
    refused = AccessRefused("FORGE_UNREACHABLE", f"GitHub did not answer {what}", status=503)
    refused.code = "forge_unreachable"
    return refused


# --------------------------------------------------------------------------
# naming (§3.1)
# --------------------------------------------------------------------------

def org_id_for(tenant_id: str, email: str, owner: str) -> str:
    return f"{tenant_id}__{user_hash(email)}__{owner.lower()}"


def grant_id_for(tenant_id: str, email: str, repo_id: str) -> str:
    return f"{tenant_id}__{user_hash(email)}__{repo_id}"


def check_owner(owner: str) -> str:
    if not _OWNER.match(owner or ""):
        raise ValidationFailed("owner must be a GitHub user or organisation login")
    return owner


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def org_to_api(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner": doc.get("owner"),
        "owner_type": doc.get("owner_type"),
        "installation_id": doc.get("installation_id"),
        "repository_selection": doc.get("repository_selection"),
        "install_state": doc.get("install_state"),
        "sso": doc.get("sso"),
        "enabled": True,
        "enabled_at": _iso(doc.get("enabled_at")),
        "checked_at": _iso(doc.get("checked_at")),
    }


def grant_to_api(doc: dict[str, Any]) -> dict[str, Any]:
    checks = {}
    for name, check in (doc.get("checks") or {}).items():
        if isinstance(check, dict):
            checks[name] = {**check, "checked_at": _iso(check.get("checked_at"))}
    return {
        "repo_id": doc.get("repo_id"),
        "repository": doc.get("repository"),
        "owner": doc.get("owner"),
        "mode": doc.get("mode"),
        "can_push": doc.get("can_push"),
        "archived": doc.get("archived"),
        "granted_at": _iso(doc.get("granted_at")),
        "granted_by": doc.get("granted_by"),
        "checks": checks,
        "verified_at": _iso(doc.get("verified_at")),
    }


# --------------------------------------------------------------------------
# GitHub, as the person
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Owner:
    login: str
    owner_type: str
    installation_id: int | None = None
    repository_selection: str | None = None
    pull_requests: str | None = None


class _AsUser:
    """The reads one request makes with the person's token. Every answer
    that did not come back raises `_Unanswered`; the token is never in a
    message, and is registered with the redaction filter by the caller."""

    def __init__(self, github: _GitHub, token: str) -> None:
        self._github = github
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {**_API_HEADERS, "Authorization": f"Bearer {self._token}"}

    def get(self, path: str) -> HttpAnswer:
        return self._github._call("GET", API + path, self._headers(), None)

    def git(self, owner: str, repo: str, service: str) -> HttpAnswer:
        url = (f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}.git/"
               f"info/refs?service={service}")
        return self._github._call("GET", url, git_basic_headers(self._token), None)

    def installations(self) -> list[_Owner]:
        found: list[_Owner] = []
        for page in range(1, MAX_PAGES + 1):
            answer = self.get(f"/user/installations?per_page={PAGE_SIZE}&page={page}")
            data = answer.json()
            if answer.status != 200 or not isinstance(data, dict):
                raise _Refused(f"installations_http_{answer.status}")
            items = data.get("installations")
            items = items if isinstance(items, list) else []
            for item in items:
                owner = _installation_owner(item)
                if owner is not None:
                    found.append(owner)
            if len(items) < PAGE_SIZE:
                break
        return found

    def orgs(self) -> list[str] | None:
        """The orgs `GET /user/orgs` lists, or None when it answered no."""
        logins: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            answer = self.get(f"/user/orgs?per_page={PAGE_SIZE}&page={page}")
            data = answer.json()
            if answer.status != 200 or not isinstance(data, list):
                return None
            for item in data:
                login = item.get("login") if isinstance(item, dict) else None
                if isinstance(login, str) and _OWNER.match(login):
                    logins.append(login)
            if len(data) < PAGE_SIZE:
                break
        return logins

    def installation_page(self, installation_id: int, page: int) -> tuple[list[Any], int | None,
                                                                         HttpAnswer]:
        answer = self.get(f"/user/installations/{int(installation_id)}/repositories?"
                          + urlencode({"per_page": PAGE_SIZE, "page": page}))
        data = answer.json()
        if answer.status != 200 or not isinstance(data, dict):
            return [], None, answer
        items = data.get("repositories")
        total = data.get("total_count")
        return (items if isinstance(items, list) else [],
                total if isinstance(total, int) and not isinstance(total, bool) else None, answer)


def _installation_owner(item: Any) -> _Owner | None:
    if not isinstance(item, dict):
        return None
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    login, kind, inst_id = account.get("login"), account.get("type"), item.get("id")
    if not isinstance(login, str) or not _OWNER.match(login) \
            or not isinstance(inst_id, int) or isinstance(inst_id, bool):
        return None
    permissions = item.get("permissions") if isinstance(item.get("permissions"), dict) else {}
    selection = item.get("repository_selection")
    return _Owner(login=login, owner_type=kind if kind in ("User", "Organization") else "User",
                  installation_id=inst_id,
                  repository_selection=selection if selection in ("all", "selected") else None,
                  pull_requests=permissions.get("pull_requests")
                  if isinstance(permissions.get("pull_requests"), str) else None)


def _sso_owner(answer: HttpAnswer) -> str | None:
    """The org a 403's `X-GitHub-SSO: required` names, if it does."""
    if answer.status != 403:
        return None
    parsed = parse_sso_header(answer.headers.get("x-github-sso"))
    if parsed is None or parsed.get("mode") != "required":
        return None
    return parsed.get("org") or ""


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class AccessService:
    """The access routes' logic, over one Firestore, `ForgeApp`'s connection
    and slots, and one GitHub transport."""

    def __init__(self, db: Any, forge_app: ForgeApp, *, send: HttpSend = urllib_send,
                 now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._app = forge_app
        self._github = _GitHub(send)
        self._now = now

    # -- the connection and the person's token -------------------------------

    def connection(self, caller: Caller) -> dict[str, Any] | None:
        """The caller's connection document, or None. Another person's or
        another tenant's is None too."""
        snap = self._db.collection(CONNECTIONS).document(
            connection_id_for(caller.tenant_id, caller.key)).get()
        doc = snap.to_dict() if snap.exists else None
        if doc is None or doc.get("tenant_id") != caller.tenant_id \
                or doc.get("user") != caller.key:
            return None
        return doc

    def _require_active(self, caller: Caller) -> dict[str, Any]:
        doc = self.connection(caller)
        if doc is None or doc.get("state") == REVOKED:
            raise NotConnected()
        if doc.get("state") == REFRESH_FAILED:
            login = doc.get("forge_login") or ""
            raise AccessRefused("REFRESH_FAILED",
                                f"SwarmCloud's access as {login} has ended at GitHub; reconnect",
                                login=login)
        return doc

    @contextmanager
    def user_token(self, caller: Caller) -> Iterator[HeldCredential]:
        """A usable token of the caller's own connection, for one request.

        The refresh sequence is the sweep's (`ForgeApp._refresh_one`): the
        lease, the refresh, both values stored -- the refresh twin first --
        then the record. Only the outcome differs: the new access token is
        handed to the caller's `with` instead of being dropped.
        """
        doc = self._require_active(caller)
        app = self._app
        client_id = app._require_client_id()
        client_secret = app._client_secret()
        conn_id = str(doc["connection_id"])
        login = str(doc.get("forge_login") or "")
        suffix = provider_suffix(Scope.USER, user=caller.key)
        holder = uuid.uuid4().hex
        with ExitStack() as held:
            held.enter_context(redaction_literal(client_secret))
            current = app._take_lease(conn_id, holder)
            if current is None:
                self._require_active(caller)
                raise Conflict("a refresh of your GitHub token is running; try again in a "
                               "minute")
            now = self._now()
            refresh_expires = current.get("refresh_expires_at")
            if isinstance(refresh_expires, datetime) and refresh_expires <= now:
                app._mark_failed(current, holder, "the refresh token is past its expiry")
                raise AccessRefused("REFRESH_FAILED", "your GitHub authorisation has expired",
                                    login=login)
            try:
                refresh_token = app._slots.read_refresh(caller.tenant_id, refresh_suffix(suffix))
            except Exception as exc:
                app._finish(conn_id, holder, {})
                log.warning("access could not read the refresh slot connection=%s (%s)",
                            conn_id, type(exc).__name__)
                raise UpstreamUnavailable("your GitHub connection's slot could not be read "
                                          f"({type(exc).__name__}); try again") from None
            held.enter_context(redaction_literal(refresh_token))
            try:
                tokens = app._github.refresh(client_id, client_secret, refresh_token, now)
            except _Unanswered:
                app._finish(conn_id, holder, {})
                raise _unreachable("the token refresh") from None
            except _Refused as refused:
                app._mark_failed(current, holder, f"GitHub refused the refresh ({refused.error})")
                raise AccessRefused("REFRESH_FAILED",
                                    f"GitHub refused SwarmCloud's access as {login}",
                                    login=login) from None
            refresh_token = ""
            held.enter_context(redaction_literal(tokens.access))
            held.enter_context(redaction_literal(tokens.refresh))
            try:
                app._slots.add_version(caller.tenant_id, refresh_suffix(suffix), tokens.refresh)
                version = app._slots.add_version(caller.tenant_id, suffix, tokens.access)
            except Exception as exc:
                log.error("access could not store the refreshed tokens connection=%s (%s)",
                          conn_id, type(exc).__name__)
                app._mark_failed(current, holder, "the refreshed token could not be stored "
                                                  f"({type(exc).__name__})")
                raise UpstreamUnavailable("your refreshed GitHub token could not be stored; "
                                          "press Reconnect") from None
            app._finish(conn_id, holder, {"access_expires_at": tokens.access_expires_at,
                                          "refresh_expires_at": tokens.refresh_expires_at,
                                          "refreshed_at": now})
            token_id = current.get("token_id")
            if token_id:
                app._update_record(caller.tenant_id, token_id, {
                    "expires_at": tokens.access_expires_at, "rotated_at": now,
                    "secret_version": version or None, "verified_at": now})
            yield HeldCredential(SCOPE_USER, str(doc.get("secret_name") or ""), tokens.access,
                                 login=login)

    def credential_for(self, caller: Caller, tenant: Tenant,
                       tokens: ForgeTokens) -> CredentialSource:
        """What `register` and `readable` read with (§3.2, phase 3): the
        caller's own connection when it is active, the tenant token when the
        caller never connected or disconnected. A connection whose refresh
        failed is refused with REFRESH_FAILED rather than quietly answered
        with the tenant's reach, which is not what the person can do."""

        @contextmanager
        def held() -> Iterator[HeldCredential]:
            doc = self.connection(caller)
            if doc is None or doc.get("state") == REVOKED:
                with tenant_credential(tenant, tokens)() as tenant_held:
                    yield tenant_held
                return
            with self.user_token(caller) as user_held:
                yield user_held

        return held

    @contextmanager
    def _as_user(self, caller: Caller) -> Iterator[tuple[_AsUser, HeldCredential]]:
        with self.user_token(caller) as held:
            yield _AsUser(self._github, held.value), held

    # -- documents -----------------------------------------------------------

    def _mine(self, collection: str, caller: Caller) -> list[dict[str, Any]]:
        hashed = user_hash(caller.key)
        query = self._db.collection(collection).where(
            filter=FieldFilter("tenant_id", "==", caller.tenant_id)).where(
            filter=FieldFilter("user_hash", "==", hashed)).limit(MAX_DOCS)
        docs = [snap.to_dict() or {} for snap in query.stream()]
        return [d for d in docs if d.get("tenant_id") == caller.tenant_id
                and d.get("user") == caller.key]

    def _doc(self, collection: str, doc_id: str, caller: Caller) -> dict[str, Any] | None:
        snap = self._db.collection(collection).document(doc_id).get()
        doc = snap.to_dict() if snap.exists else None
        if doc is None or doc.get("tenant_id") != caller.tenant_id \
                or doc.get("user") != caller.key:
            return None
        return doc

    def _org(self, caller: Caller, owner: str) -> dict[str, Any] | None:
        return self._doc(ORGS, org_id_for(caller.tenant_id, caller.key, owner), caller)

    def _grant(self, caller: Caller, repo_id: str) -> dict[str, Any] | None:
        if not _REPO_ID.match(repo_id or ""):
            return None
        return self._doc(GRANTS, grant_id_for(caller.tenant_id, caller.key, repo_id), caller)

    def _install_url(self) -> str | None:
        slug = self._app.config.slug
        return INSTALL_URL.format(slug=quote(slug, safe="")) if slug else None

    # -- GET /v1/access --------------------------------------------------------

    def overview(self, caller: Caller) -> dict[str, Any]:
        """The Access page: connection, enabled owners, grants. No forge read."""
        doc = self.connection(caller)
        orgs = sorted(self._mine(ORGS, caller), key=lambda d: str(d.get("owner")))
        grants = sorted(self._mine(GRANTS, caller), key=lambda d: str(d.get("repository")))
        return {"connection": connection_to_api(doc) if doc is not None else None,
                "orgs": [org_to_api(d) for d in orgs],
                "grants": [grant_to_api(d) for d in grants]}

    # -- owners ----------------------------------------------------------------

    def owners(self, caller: Caller) -> dict[str, Any]:
        """Every owner the person reaches, each with its install state."""
        enabled = {str(d.get("owner")).lower(): d for d in self._mine(ORGS, caller)}
        with self._as_user(caller) as (github, held):
            own_login = held.login or ""
            try:
                installed = github.installations()
                orgs = github.orgs()
            except _Unanswered:
                raise _unreachable("the list of your installations") from None
            except _Refused as refused:
                raise AccessRefused("REFRESH_FAILED", "GitHub refused the list of your "
                                    f"installations ({refused.error})", login=own_login,
                                    status=502) from None
        by_owner: dict[str, dict[str, Any]] = {}

        def add(login: str, owner_type: str, inst: _Owner | None) -> None:
            key = login.lower()
            entry = by_owner.get(key)
            if entry is None or (inst is not None and entry["installation_id"] is None):
                doc = enabled.get(key) or {}
                by_owner[key] = {
                    "owner": login,
                    "owner_type": owner_type,
                    "installation_id": inst.installation_id if inst else None,
                    "repository_selection": inst.repository_selection if inst else None,
                    "install_state": "installed" if inst else "not_installed",
                    "sso": doc.get("sso") or "unknown",
                    "enabled": bool(doc) and inst is not None,
                    "install_url": None if inst else self._install_url(),
                }

        for inst in installed:
            add(inst.login, inst.owner_type, inst)
        if own_login and _OWNER.match(own_login):
            add(own_login, "User", None)
        for login in orgs or []:
            add(login, "Organization", None)
        rows = sorted(by_owner.values(), key=lambda e: (e["owner_type"] != "User",
                                                        e["owner"].lower()))
        log.info("access owners tenant=%s user_hash=%s owners=%d installed=%d",
                 caller.tenant_id, user_hash(caller.key), len(rows), len(installed))
        return {"owners": rows, "orgs_listed": orgs is not None,
                "install_url": self._install_url()}

    def enable(self, caller: Caller, owner: str) -> dict[str, Any]:
        """Enable an owner the App is installed on. Refused when it is not."""
        check_owner(owner)
        with self._as_user(caller) as (github, _held):
            try:
                installed = github.installations()
            except _Unanswered:
                raise _unreachable("the list of your installations") from None
            except _Refused as refused:
                raise AccessRefused("REFRESH_FAILED", "GitHub refused the list of your "
                                    f"installations ({refused.error})", status=502) from None
        match = next((i for i in installed if i.login.lower() == owner.lower()), None)
        if match is None:
            url = self._install_url() or ""
            refused = AccessRefused(
                "REPO_NOT_INSTALLED",
                f"SwarmCloud's GitHub App is not installed on {owner}: install it there "
                "(an org owner may have to), then enable it", owner=owner, url=url)
            refused.code = "not_installed"
            raise refused
        now = self._now()
        doc_id = org_id_for(caller.tenant_id, caller.key, match.login)
        previous = self._org(caller, match.login) or {}
        doc = {
            "tenant_id": caller.tenant_id,
            "user": caller.key,
            "user_hash": user_hash(caller.key),
            "owner": match.login.lower(),
            "owner_login": match.login,
            "owner_type": match.owner_type,
            "installation_id": match.installation_id,
            "repository_selection": match.repository_selection,
            "pull_requests": match.pull_requests,
            "install_state": "installed",
            "sso": previous.get("sso") or "unknown",
            "enabled_at": previous.get("enabled_at") or now,
            "enabled_by": caller.key,
            "checked_at": now,
        }
        self._db.collection(ORGS).document(doc_id).set(doc)
        log.info("access owner enabled tenant=%s user_hash=%s owner=%s installation=%s",
                 caller.tenant_id, user_hash(caller.key), doc["owner"], match.installation_id)
        return {"org": org_to_api(doc)}

    def disable(self, caller: Caller, owner: str) -> dict[str, Any]:
        """§2.4: delete the owner and every grant under it, in one write."""
        check_owner(owner)
        doc = self._org(caller, owner)
        if doc is None:
            raise NotFound(f"{owner} is not enabled for you in this tenant")
        grants = [g for g in self._mine(GRANTS, caller)
                  if str(g.get("owner") or "").lower() == owner.lower()]
        batch = self._db.batch()
        batch.delete(self._db.collection(ORGS).document(
            org_id_for(caller.tenant_id, caller.key, owner)))
        for grant in grants:
            batch.delete(self._db.collection(GRANTS).document(
                grant_id_for(caller.tenant_id, caller.key, str(grant["repo_id"]))))
        batch.commit()
        unregistered = [g["repo_id"] for g in grants
                        if self._unregister_if_last(caller.tenant_id, str(g["repo_id"]))]
        log.info("access owner disabled tenant=%s user_hash=%s owner=%s grants_deleted=%d",
                 caller.tenant_id, user_hash(caller.key), owner.lower(), len(grants))
        return {"owner": owner.lower(), "grants_deleted": len(grants),
                "unregistered": unregistered,
                # What GitHub still allows, said plainly (§2.4): the user's
                # token is not per org, so the installation stays until an
                # org owner removes it.
                "installation_settings_url": INSTALLATION_SETTINGS_URL.format(
                    installation_id=doc.get("installation_id"))
                if doc.get("installation_id") else None}

    # -- repositories ------------------------------------------------------------

    def repositories(self, caller: Caller, owner: str, *, page: int,
                     query: str | None) -> dict[str, Any]:
        """One page of the owner's installation's repositories, each with
        whether the tenant registered it and whether the person granted it."""
        check_owner(owner)
        if page < 1 or page > MAX_PAGES:
            raise ValidationFailed(f"page must be 1-{MAX_PAGES}")
        needle = (query or "").strip().lower()
        if len(needle) > MAX_QUERY_CHARS:
            raise ValidationFailed(f"q must be at most {MAX_QUERY_CHARS} characters")
        org = self._org(caller, owner)
        if org is None or not org.get("installation_id"):
            raise NotFound(f"{owner} is not enabled for you in this tenant: enable it first")
        installation_id = int(org["installation_id"])
        total: int | None = None
        capped = False
        with self._as_user(caller) as (github, _held):
            try:
                if not needle:
                    raw, total, answer = github.installation_page(installation_id, page)
                    self._refuse_listing(caller, answer, owner, installation_id)
                    window = raw
                    more = len(raw) >= PAGE_SIZE and page < MAX_PAGES
                    capped = len(raw) >= PAGE_SIZE and page == MAX_PAGES
                else:
                    matched: list[Any] = []
                    for gh_page in range(1, MAX_PAGES + 1):
                        raw, total, answer = github.installation_page(installation_id, gh_page)
                        self._refuse_listing(caller, answer, owner, installation_id)
                        matched += [r for r in raw if needle in _name(r)]
                        if len(raw) < PAGE_SIZE:
                            break
                    else:
                        capped = True
                    window = matched[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
                    more = len(matched) > page * PAGE_SIZE
            except _Unanswered:
                raise _unreachable(f"the list of {owner}'s repositories") from None
        entries = self._entries(caller, owner, window)
        self._db.collection(ORGS).document(org_id_for(caller.tenant_id, caller.key, owner)) \
            .update({"sso": "ok", "checked_at": self._now()})
        return {"owner": owner.lower(), "repositories": entries, "page": page,
                "per_page": PAGE_SIZE, "max_pages": MAX_PAGES,
                "next_page": page + 1 if more else None, "capped": capped,
                "q": needle or None, "total_count": None if needle else total}

    def _refuse_listing(self, caller: Caller, answer: HttpAnswer, owner: str,
                        installation_id: int) -> None:
        if answer.status == 200:
            return
        if _sso_owner(answer) is not None:
            # Evidence for the owners list: this org wants SSO for this person.
            self._db.collection(ORGS).document(org_id_for(caller.tenant_id, caller.key, owner)) \
                .update({"sso": "required", "checked_at": self._now()})
        raise self._refusal(answer, owner, "", installation_id)

    def _refusal(self, answer: HttpAnswer, owner: str, repo: str,
                 installation_id: int | None) -> AccessRefused:
        sso = _sso_owner(answer)
        if sso is not None:
            org = sso or owner
            return AccessRefused("SSO_NOT_AUTHORISED",
                                 f"{org} requires SAML single sign-on for this read",
                                 owner=org, url=SSO_URL.format(owner=quote(org, safe="")),
                                 status=403)
        url = (INSTALLATION_SETTINGS_URL.format(installation_id=installation_id)
               if installation_id else (self._install_url() or ""))
        what = f"{owner}/{repo}" if repo else f"{owner}'s repositories"
        return AccessRefused("REPO_NOT_INSTALLED",
                             f"GitHub answered HTTP {answer.status} for {what} through "
                             "SwarmCloud's installation", owner=owner, repo=repo or owner,
                             url=url, status=403)

    def _entries(self, caller: Caller, owner: str, raw: list[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in raw:
            read = repository_from(item)
            if read is None or read.owner.lower() != owner.lower():
                continue
            try:
                parse_repository(f"{read.owner}/{read.repo}")
            except ValueError:
                continue
            repo_id = repo_id_for(caller.tenant_id, read.owner, read.repo)
            rows.append({"repository": f"{read.owner}/{read.repo}", "owner": read.owner,
                         "repo": read.repo, "repo_id": repo_id, "visibility": read.visibility,
                         "archived": read.archived, "default_branch": read.default_branch or None,
                         "can_push": read.can_push})
        registered = Repositories(self._db, now=self._now).registered(
            caller.tenant_id, [r["repo_id"] for r in rows])
        grants = {g.get("repo_id"): g for g in self._mine(GRANTS, caller)}
        for row in rows:
            grant = grants.get(row["repo_id"])
            row["registered"] = row["repo_id"] in registered
            row["granted"] = grant is not None
            row["mode"] = grant.get("mode") if grant else None
        return rows

    # -- grants ------------------------------------------------------------------

    def grant(self, caller: Caller, tenant: Tenant, repo_id: str, *, repository: str,
              mode: str) -> dict[str, Any]:
        """§2.4 "Add a repository": read it once as the person, refuse what
        the mode cannot do, register it for the tenant if it is not yet,
        and record the grant."""
        try:
            owner, repo = parse_repository(repository)
        except ValueError as bad:
            raise ValidationFailed(str(bad)) from None
        if repo_id_for(caller.tenant_id, owner, repo) != repo_id:
            raise ValidationFailed("repo_id is not this tenant's id for that repository")
        org = self._org(caller, owner)
        if org is None:
            raise AccessRefused(
                "REPO_NOT_INSTALLED", f"{owner} is not enabled for you in this tenant: enable "
                "it under Access first", owner=owner, repo=f"{owner}/{repo}",
                url=self._install_url() or "")
        with self._as_user(caller) as (github, held):
            try:
                answer = github.get(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}")
            except _Unanswered:
                raise _unreachable(f"the read of {owner}/{repo}") from None
            # Names only: the credential, value and all, stays inside the `with`.
            secret_name, login = held.secret_name, held.login or ""
        if answer.status != 200:
            raise self._refusal(answer, owner, repo, org.get("installation_id"))
        read = repository_from(answer.json())
        if read is None or (read.owner.lower(), read.repo.lower()) != (owner.lower(),
                                                                       repo.lower()):
            raise UpstreamUnavailable(f"GitHub's answer for {owner}/{repo} is not that repository")
        if not read.can_read:
            raise self._refusal(HttpAnswer(403, {}), owner, repo, org.get("installation_id"))
        full = f"{read.owner}/{read.repo}"
        if mode == "write" and read.archived:
            raise AccessRefused("REPO_ARCHIVED", f"{full} is archived on GitHub", repo=full,
                                status=422)
        if mode == "write" and not read.can_push:
            raise AccessRefused("PERMISSION_MISSING",
                                f"GitHub does not let {login} push to {full}",
                                repo=full, login=login, status=422)
        now = self._now()
        store = Repositories(self._db, now=self._now)
        registration = store.find(caller.tenant_id, repo_id)
        created = False
        if registration is None:
            # The first grant registers it for the tenant (§2.4), read with
            # the person's own token, and says so.
            registration, created = store.create(registration_record(
                RepositoryCreate(repository=full), tenant, read, repo_id=repo_id,
                created_by=caller.key, at=now, token_scope=SCOPE_USER,
                secret_name=secret_name, registered_via="grant"))
        previous = self._grant(caller, repo_id) or {}
        doc = {
            "tenant_id": caller.tenant_id,
            "user": caller.key,
            "user_hash": user_hash(caller.key),
            "repo_id": repo_id,
            "repository": full,
            "owner": read.owner.lower(),
            "mode": mode,
            "can_push": read.can_push,
            "archived": read.archived,
            "granted_at": previous.get("granted_at") or now,
            "granted_by": caller.key,
            "updated_at": now,
            # A changed mode needs its own checks: kept only when unchanged.
            "checks": previous.get("checks") if previous.get("mode") == mode else {},
            "verified_at": previous.get("verified_at") if previous.get("mode") == mode else None,
        }
        self._db.collection(GRANTS).document(
            grant_id_for(caller.tenant_id, caller.key, repo_id)).set(doc)
        log.info("access grant tenant=%s user_hash=%s repo_id=%s mode=%s registered=%s",
                 caller.tenant_id, user_hash(caller.key), repo_id, mode,
                 "created" if created else "existing")
        return {"grant": grant_to_api(doc), "registered": created,
                "registration_repo_id": registration.get("repo_id")}

    def revoke(self, caller: Caller, repo_id: str) -> dict[str, Any]:
        """§2.4 "Remove a repository": the grant goes; the registration goes
        with the last grant, and only if a grant made it."""
        doc = self._grant(caller, repo_id)
        if doc is None:
            raise NotFound(f"you hold no grant on {repo_id[:64]!r} in this tenant")
        self._db.collection(GRANTS).document(
            grant_id_for(caller.tenant_id, caller.key, repo_id)).delete()
        unregistered = self._unregister_if_last(caller.tenant_id, repo_id)
        log.info("access grant revoked tenant=%s user_hash=%s repo_id=%s unregistered=%s",
                 caller.tenant_id, user_hash(caller.key), repo_id, unregistered)
        return {"repo_id": repo_id, "revoked": True, "unregistered": unregistered}

    def _unregister_if_last(self, tenant_id: str, repo_id: str) -> bool:
        holders = self._db.collection(GRANTS).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)).where(
            filter=FieldFilter("repo_id", "==", repo_id)).limit(1)
        if any((s.to_dict() or {}).get("tenant_id") == tenant_id for s in holders.stream()):
            return False
        store = Repositories(self._db, now=self._now)
        record = store.find(tenant_id, repo_id)
        if record is None or record.get("registered_via") != "grant":
            return False
        store.delete(tenant_id, repo_id)
        return True

    # -- verify ------------------------------------------------------------------

    def verify(self, caller: Caller, repo_id: str,
               checks: list[str] | None = None) -> dict[str, Any]:
        """§2.2 `access_verified` for one grant: clone, and for a write
        grant push and pull request. Reads only (D6)."""
        doc = self._grant(caller, repo_id)
        if doc is None:
            raise NotFound(f"you hold no grant on {repo_id[:64]!r} in this tenant")
        wanted = list(dict.fromkeys(checks or CHECKS))
        owner, repo = str(doc["repository"]).split("/", 1)
        write = doc.get("mode") == "write"
        org = self._org(caller, owner) or {}
        now = self._now()
        results: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []

        def put(name: str, state: str, code: str | None = None, **fill: str) -> None:
            results[name] = {"state": state, "code": code, "checked_at": now}
            if code is not None:
                failures.append({"check": name, "code": code,
                                 "copy": recovery_copy(code, **fill)})

        with self._as_user(caller) as (github, held):
            login = held.login or ""
            try:
                answer = github.get(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}")
            except _Unanswered:
                answer = None
            read = repository_from(answer.json()) if answer is not None \
                and answer.status == 200 else None
            if answer is None:
                for name in wanted:
                    put(name, UNKNOWN, "FORGE_UNREACHABLE")
            elif read is None:
                refusal = self._refusal(answer, owner, repo, org.get("installation_id"))
                code = refusal.detail["failure_code"]
                for name in wanted:
                    results[name] = {"state": MISSING, "code": code, "checked_at": now}
                failures.append({"check": "repository", "code": code,
                                 "copy": refusal.detail["recovery"],
                                 "url": refusal.detail.get("url")})
            else:
                clone_ok = self._advertised(github, owner, repo, "git-upload-pack")
                if "clone" in wanted:
                    self._put_git(put, "clone", clone_ok, repo=doc["repository"])
                push_ok: bool | None = None
                if write and ("push" in wanted or "pull_request" in wanted):
                    advertised = self._advertised(github, owner, repo, "git-receive-pack")
                    push_ok = None if advertised is None else (advertised and read.can_push)
                if "push" in wanted:
                    if not write:
                        put("push", NOT_REQUIRED)
                    else:
                        self._put_git(put, "push", push_ok, repo=doc["repository"], login=login,
                                      missing="PERMISSION_MISSING")
                if "pull_request" in wanted:
                    if not write:
                        put("pull_request", NOT_REQUIRED)
                    else:
                        try:
                            installed = github.installations()
                            inst = next((i for i in installed
                                         if i.login.lower() == owner.lower()), None)
                            pr_ok: bool | None = (inst is not None
                                                  and inst.pull_requests == "write")
                        except (_Unanswered, _Refused):
                            pr_ok = None
                        if push_ok is None or pr_ok is None:
                            put("pull_request", UNKNOWN, "FORGE_UNREACHABLE")
                        elif push_ok and pr_ok:
                            put("pull_request", OK)
                        else:
                            put("pull_request", MISSING, "PERMISSION_MISSING",
                                repo=doc["repository"], login=login)
        stored = {**(doc.get("checks") or {}), **results}
        update = {"checks": stored, "verified_at": now}
        self._db.collection(GRANTS).document(
            grant_id_for(caller.tenant_id, caller.key, repo_id)).update(update)
        doc.update(update)
        log.info("access verify tenant=%s user_hash=%s repo_id=%s %s", caller.tenant_id,
                 user_hash(caller.key), repo_id,
                 " ".join(f"{k}={v['state']}" for k, v in results.items()))
        return {"grant": grant_to_api(doc), "failures": failures,
                "passed": not failures and all(r["state"] in (OK, NOT_REQUIRED)
                                               for r in results.values())}

    @staticmethod
    def _advertised(github: _AsUser, owner: str, repo: str, service: str) -> bool | None:
        try:
            answer = github.git(owner, repo, service)
        except _Unanswered:
            return None
        return answer.status == 200

    @staticmethod
    def _put_git(put: Callable[..., None], name: str, ok: bool | None, *,
                 missing: str = "REPO_NOT_INSTALLED", **fill: str) -> None:
        if ok is None:
            put(name, UNKNOWN, "FORGE_UNREACHABLE")
        elif ok:
            put(name, OK)
        else:
            put(name, MISSING, missing, **fill)

    # -- the admin tenant view -----------------------------------------------------

    def members(self, tenant_id: str, *, is_admin: bool) -> dict[str, Any]:
        """Every member's connection state and grants in `tenant_id`. Never
        a value: the records hold none."""
        if not is_admin:
            raise Forbidden("the tenant's members view is for admins")
        people: dict[str, dict[str, Any]] = {}

        def person(email: str) -> dict[str, Any]:
            return people.setdefault(email, {"user": email, "connection": None, "orgs": [],
                                             "grants": []})

        for collection, key in ((CONNECTIONS, "connection"), (ORGS, "orgs"), (GRANTS, "grants")):
            query = self._db.collection(collection).where(
                filter=FieldFilter("tenant_id", "==", tenant_id)).limit(MAX_DOCS)
            for snap in query.stream():
                doc = snap.to_dict() or {}
                if doc.get("tenant_id") != tenant_id or not doc.get("user"):
                    continue
                row = person(str(doc["user"]))
                if key == "connection":
                    row["connection"] = connection_to_api(doc)
                elif key == "orgs":
                    row["orgs"].append(org_to_api(doc))
                else:
                    row["grants"].append(grant_to_api(doc))
        return {"members": [people[k] for k in sorted(people)]}


def _name(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    name = raw.get("full_name")
    return name.lower() if isinstance(name, str) else ""


def build_access(db: Any, forge_app: ForgeApp, *, now: Callable[[], datetime] = utcnow
                 ) -> AccessService:
    """Production wiring: urllib to GitHub. Builds no client."""
    return AccessService(db, forge_app, now=now)
