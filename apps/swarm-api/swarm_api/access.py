"""The access API: the caller's owners, their repositories, grants and their
verification (docs/onboarding.md §2.2, §2.4, §3.1-§3.2; #780, lane OB4).

Owner decisions of 2026-10-07 on #780 this builds: D1 (the GitHub App with
user access tokens), D6 (verification reads only, with one opt-in branch
write test per repository), D7 (U1: a person acts
through their own slot only), D9 (a read grant is a read grant: SwarmCloud
enforces it, at submission in OB7 and in the worker in OB5 -- here it is
recorded), D10 (chooser A: owners, then the owner's repositories paged and
searched, each Read or Write with push ability shown before Write).

ACTING AS THE PERSON. Every GitHub read here is made with the caller's OWN
user access token, never the tenant token and never another member's, held
in memory for the length of one request and registered with the redaction
filter.

IT REUSES THE CURRENT TOKEN (owner decision 2026-10-08). The request reads
the base slot's latest version -- the token the worker reads too -- and uses
it while the connection says it has more than `forgeapp.REUSE_WHILE_LEFT`
(2 hours) left. It refreshes only when the token is within that of expiry,
or when GitHub answers 401; then once, under the connection's refresh lease,
storing both new values exactly as the sweep does, and the read is retried
once. MEASURED 2026-10-08 08:20Z, before this: every request refreshed, the
owner's slot reached 35 versions in minutes, and since each refresh makes
GitHub end the access token it replaces, a task holding the previous version
failed 401. A refresh another holder is running is never raced: the request
uses the still-valid current token, or answers 409 when there is none; a
refresh another holder finished meanwhile is adopted, not repeated. Until
the bootstrap grant that lets swarm-api read base slots is applied, the read
is refused and the request refreshes, as it did before.

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
    same push bit. Each of these is a read.

THE ONE WRITE (D6 b). `push_test` is never in the default check set; it
runs only when the caller names it, and is refused on a read grant before
any GitHub call (D9: SwarmCloud enforces read). With the token `_as_owner`
picks for the owner -- the App user token, or the person's fallback token
for an owner reached through one (D5) -- it reads the default branch head
(`GET /repos/{o}/{r}/git/ref/heads/{branch}`), creates
`refs/heads/swarmcloud/onboarding-check-<16 hex nonce>` there
(`POST /repos/{o}/{r}/git/refs`) and deletes it in a `finally`. A 403 or 404
on create is `missing` with PERMISSION_MISSING copy; a 5xx, 429 or network
error is FORGE_UNREACHABLE and `unknown`, never `missing`, and the delete is
still tried, since an unanswered create may have been made. A delete that
fails answers the branch name with `leftover: true` so the person can delete
it, and logs the branch, never the token. Nothing else in verify writes.

A READ THAT DID NOT COME BACK IS NOT AN ANSWER: a 5xx, a 429 or a network
error is FORGE_UNREACHABLE (503), a check it decides stays `unknown`, and
nothing is recorded as failed because of it.

AN OWNER REACHED THROUGH A TOKEN (D5). An org whose admin will not install
the App is enabled by the person's fallback token for it
(`ForgeApp.store_owner_token`): its `forge_orgs` document says `method: pat`.
Everything here that reads under that owner -- its repositories, a grant, a
verify -- reads with THAT token, from the person's per-owner slot
(`gittokens.owner_suffix`), never the App's user token; the owners list
shows it from its document with no GitHub read. Removing the owner disables
every version of that slot and revokes its record (`ForgeApp.revoke_owner_token`).
A token's pull-request ability is not readable (a fine-grained token's
permissions are not exposed), so verify passes it on the push it needs, as
the probe does (`gittokens.FINE_GRAINED_UNKNOWN`).

AN INSTALL SOMEONE ELSE HAS TO APPROVE (§2.3 ORG_APPROVAL_PENDING). GitHub
does not show a user access token the install request it is waiting on, so
SwarmCloud records it: `request_install` (`POST /v1/access/orgs/{owner}/
install-request`, called by the console and the CLI when the person opens
the App's install page for an org they do not own) writes their
`forge_orgs` document with `install_state: requested` and `requested_at`,
and asks GitHub nothing. A requested owner is NOT an enabled one: it has no
installation id, so nothing under it is listed, granted or verified, and
Access lists it under `requested`, not `orgs`. While `GET
/user/installations` does not list it, the owners list and the checklist say
ORG_APPROVAL_PENDING with §2.3's copy. As soon as the installation appears
-- on the next owners read, the next checklist read (`installations` reads
GitHub whenever a request is pending, whatever else is enabled), or the
15-minute refresh sweep (`recheck_requested`, the re-check the copy
promises) -- the document becomes the one `enable` writes, `installed` and
enabled, keeping `requested_at`. Removing the owner withdraws the request.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlencode

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Tenant, utcnow

from .errors import ApiError, Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed
from .forge import ForgeTokens, git_basic_headers, repository_from
from .forgeapp import (
    ACTIVE,
    CONNECTIONS,
    GRANTS,
    METHOD_PAT,
    ORGS,
    REFRESH_FAILED,
    REUSE_WHILE_LEFT,
    REVOKED,
    SSO_URL,
    Caller,
    ForgeApp,
    HttpAnswer,
    HttpSend,
    RefreshReport,
    _GitHub,
    _Refused,
    _Unanswered,
    connection_id_for,
    connection_to_api,
    refresh_suffix,
    urllib_send,
    user_hash,
)
from .gittokens import (
    _CLASSIC_BLOCKED,
    Scope,
    owner_suffix,
    parse_sso_header,
    provider_suffix,
    redaction_literal,
    secret_name_for,
)
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
    "AccessRefused", "AccessService", "CHECKS", "MODES", "NotConnected", "OPT_IN_CHECKS",
    "ORGS", "PUSH_TEST", "grant_id_for", "org_id_for",
]

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

API = "https://api.github.com"
_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "swarmcloud-swarm-api"}

MODES = ("read", "write")
CHECKS = ("clone", "push", "pull_request")

#: D6 (b): the one check that writes. Never in the default set; it runs only
#: when the caller names it, and only on a write grant.
PUSH_TEST = "push_test"
OPT_IN_CHECKS = (PUSH_TEST,)

#: The branch the push test creates and deletes, `<16 hex>` a fresh nonce so
#: two tests, or a leftover from an earlier one, never collide.
PUSH_TEST_PREFIX = "swarmcloud/onboarding-check-"
OK, MISSING, UNKNOWN, NOT_REQUIRED = "ok", "missing", "unknown", "not_required"

#: `forge_orgs.install_state` (§3.1). `requested` is an install the person
#: asked an org's owners for (ORG_APPROVAL_PENDING): recorded, never enabled,
#: until GitHub lists the installation and it becomes `installed`.
INSTALLED = "installed"
REQUESTED = "requested"
APPROVAL_PENDING = "ORG_APPROVAL_PENDING"

#: The people whose pending requests one refresh sweep re-reads at GitHub,
#: one `GET /user/installations` each. Past it the sweep says `out_of_time`
#: and the next tick, 15 minutes on, takes the rest.
MAX_RECHECKS_PER_SWEEP = 200

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


def via_token(org: dict[str, Any] | None) -> bool:
    """True when the owner is enabled through the person's fallback token (D5)."""
    return bool(org) and org.get("method") == METHOD_PAT


def requested(org: dict[str, Any] | None) -> bool:
    """True for an install the person asked an org's owners for and GitHub
    has not shown yet: recorded, not enabled (§2.3 ORG_APPROVAL_PENDING)."""
    return bool(org) and org.get("install_state") == REQUESTED


def pending_copy(owner: str) -> dict[str, Any]:
    """ORG_APPROVAL_PENDING's code and §2.3 copy for `owner`."""
    return {"code": APPROVAL_PENDING, "copy": recovery_copy(APPROVAL_PENDING, owner=owner)}


def org_to_api(doc: dict[str, Any]) -> dict[str, Any]:
    pending = requested(doc)
    row = {
        "owner": doc.get("owner"),
        "method": METHOD_PAT if via_token(doc) else "app",
        "owner_type": doc.get("owner_type"),
        "installation_id": doc.get("installation_id"),
        "repository_selection": doc.get("repository_selection"),
        "install_state": doc.get("install_state"),
        "sso": doc.get("sso"),
        "enabled": not pending,
        "enabled_at": _iso(doc.get("enabled_at")),
        "requested_at": _iso(doc.get("requested_at")),
        "checked_at": _iso(doc.get("checked_at")),
    }
    if pending:
        row.update(pending_copy(str(doc.get("owner_login") or doc.get("owner") or "")))
    return row


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
    message, and is registered with the redaction filter by the caller.

    A 401 is GitHub saying the token in hand has ended -- a refresh elsewhere
    replaced it. `renew` is asked ONCE per request for a usable one, and the
    read is made again with it; a second 401 is the answer."""

    def __init__(self, github: _GitHub, token: str,
                 renew: Callable[[], str | None] | None = None) -> None:
        self._github = github
        self._token = token
        self._renew = renew

    def _send(self, make: Callable[[str], HttpAnswer]) -> HttpAnswer:
        answer = make(self._token)
        if answer.status == 401 and self._renew is not None:
            renew, self._renew = self._renew, None
            fresh = renew()
            if fresh:
                self._token = fresh
                answer = make(fresh)
        return answer

    def get(self, path: str) -> HttpAnswer:
        return self.api("GET", path)

    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> HttpAnswer:
        return self._send(lambda token: self._github._call(
            method, API + path, {**_API_HEADERS, "Authorization": f"Bearer {token}"}, body))

    def git(self, owner: str, repo: str, service: str) -> HttpAnswer:
        url = (f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}.git/"
               f"info/refs?service={service}")
        return self._send(lambda token: self._github._call(
            "GET", url, git_basic_headers(token), None))

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

    def owner_page(self, owner: str, page: int, *, own_account: bool
                   ) -> tuple[list[Any], int | None, HttpAnswer]:
        """One page of what a token reaches under `owner` (D5): the person's
        own repositories, or an org's. GitHub answers a list, with no total."""
        if own_account:
            path = "/user/repos?" + urlencode({"affiliation": "owner", "per_page": PAGE_SIZE,
                                               "page": page})
        else:
            path = (f"/orgs/{quote(owner, safe='')}/repos?"
                    + urlencode({"type": "all", "per_page": PAGE_SIZE, "page": page}))
        answer = self.get(path)
        data = answer.json()
        if answer.status != 200 or not isinstance(data, list):
            return [], None, answer
        return data, None, answer

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

def _left(doc: dict[str, Any], now: datetime) -> timedelta | None:
    """How long the connection's access token has left, or None when unknown."""
    expires = doc.get("access_expires_at")
    return expires - now if isinstance(expires, datetime) else None


def _moved(seen: dict[str, Any], current: dict[str, Any]) -> bool:
    """True when another holder refreshed the connection since `seen` was read."""
    return any(seen.get(name) != current.get(name)
               for name in ("refreshed_at", "access_expires_at"))


class _UserSession:
    """One request's hold on the person's token (owner decision 2026-10-08).

    `open` reads the base slot's current access token and uses it while it
    has more than REUSE_WHILE_LEFT left; otherwise, or when the slot cannot
    be read, it refreshes. `renew` is the 401 path: one refresh, never two.
    Every value is registered with the redaction filter on `stack`, which
    the caller closes when the request ends.
    """

    def __init__(self, service: "AccessService", caller: Caller, stack: ExitStack) -> None:
        self._service = service
        self._app = service._app
        self._caller = caller
        self._stack = stack
        self._doc = service._require_active(caller)
        self._suffix = provider_suffix(Scope.USER, user=caller.key)
        self._login = str(self._doc.get("forge_login") or "")
        self._token = ""
        self._renewed = False

    def held(self, token: str) -> HeldCredential:
        return HeldCredential(SCOPE_USER, str(self._doc.get("secret_name") or ""), token,
                              login=self._login)

    def _hold(self, value: str) -> str:
        if value:
            self._stack.enter_context(redaction_literal(value))
        return value

    def _read_access(self) -> str:
        """The base slot's latest version, or "" when it cannot be read --
        before the bootstrap grant is applied, for one -- named by type only."""
        try:
            return self._hold(self._app._slots.read_access(self._caller.tenant_id,
                                                           self._suffix))
        except Exception as exc:
            log.warning("access could not read the current token connection=%s (%s)",
                        self._doc.get("connection_id"), type(exc).__name__)
            return ""

    def open(self) -> str:
        left = _left(self._doc, self._service._now())
        current = self._read_access() if left is not None and left > timedelta(0) else ""
        if current and left is not None and left > REUSE_WHILE_LEFT:
            self._token = current
        else:
            self._token = self._refresh(fallback=current, refused="")
        return self._token

    def renew(self) -> str | None:
        """GitHub answered 401 to the token in hand: one usable replacement
        for this request, or None when one was already asked for."""
        if self._renewed:
            return None
        self._renewed = True
        self._token = self._refresh(fallback="", refused=self._token)
        return self._token

    def _refresh(self, *, fallback: str, refused: str) -> str:
        """A usable token by way of the refresh lease. `fallback` is a still
        valid current token to use while another holder refreshes; `refused`
        the token GitHub just answered 401 to, which is never handed back.

        The refresh sequence is the sweep's (`ForgeApp._refresh_one`): the
        lease, the refresh, both values stored -- the refresh twin first --
        then the record. Only the outcome differs: the new access token is
        handed to the request instead of being dropped.
        """
        service, app, caller = self._service, self._app, self._caller
        conn_id = str(self._doc["connection_id"])
        login = self._login
        suffix = self._suffix
        holder = uuid.uuid4().hex
        current = app._take_lease(conn_id, holder)
        if current is None:
            service._require_active(caller)
            if fallback:
                # Another holder is refreshing a token that still works:
                # use it, and leave the refresh to them.
                return fallback
            if refused:
                latest = self._read_access()
                if latest and latest != refused:
                    return latest
            raise Conflict("a refresh of your GitHub token is running; try again in a "
                           "minute")
        now = service._now()
        left = _left(current, now)
        if _moved(self._doc, current) and left is not None and left > REUSE_WHILE_LEFT:
            # Another holder refreshed since this request read the
            # connection: its token is the current one. Not refreshed again.
            latest = self._read_access()
            if latest and latest != refused:
                app._finish(conn_id, holder, {})
                self._doc = current
                return latest
        client_id = app._require_client_id()
        client_secret = self._hold(app._client_secret())
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
        self._hold(refresh_token)
        try:
            tokens = app._github.refresh(client_id, client_secret, refresh_token, now)
        except _Unanswered:
            app._finish(conn_id, holder, {})
            raise _unreachable("the token refresh") from None
        except _Refused as refused_refresh:
            app._mark_failed(current, holder,
                             f"GitHub refused the refresh ({refused_refresh.error})")
            raise AccessRefused("REFRESH_FAILED",
                                f"GitHub refused SwarmCloud's access as {login}",
                                login=login) from None
        refresh_token = ""
        self._hold(tokens.access)
        self._hold(tokens.refresh)
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
        fields = {"access_expires_at": tokens.access_expires_at,
                  "refresh_expires_at": tokens.refresh_expires_at, "refreshed_at": now}
        app._finish(conn_id, holder, fields)
        self._doc = {**current, **fields, "refresh_lease": None}
        token_id = current.get("token_id")
        if token_id:
            app._update_record(caller.tenant_id, token_id, {
                "expires_at": tokens.access_expires_at, "rotated_at": now,
                "secret_version": version or None, "verified_at": now})
        log.info("access refreshed the GitHub token tenant=%s connection=%s",
                 caller.tenant_id, conn_id)
        return tokens.access


class AccessService:
    """The access routes' logic, over one Firestore, `ForgeApp`'s connection
    and slots, and one GitHub transport."""

    def __init__(self, db: Any, forge_app: ForgeApp, *, send: HttpSend = urllib_send,
                 now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._app = forge_app
        self._github = _GitHub(send)
        self._now = now
        # ORG_APPROVAL_PENDING's re-check rides the 15-minute refresh sweep.
        forge_app.on_sweep(self.recheck_requested)

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
        """A usable token of the caller's own connection, for one request:
        the current one while it is fresh, a refresh only when it is near
        expiry (`_UserSession`)."""
        with ExitStack() as stack:
            session = _UserSession(self, caller, stack)
            yield session.held(session.open())

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
        with ExitStack() as stack:
            session = _UserSession(self, caller, stack)
            token = session.open()
            yield _AsUser(self._github, token, renew=session.renew), session.held(token)

    @contextmanager
    def _as_owner(self, caller: Caller, org: dict[str, Any] | None
                  ) -> Iterator[tuple[_AsUser, HeldCredential]]:
        """The reads under one owner: with the person's token for it when the
        owner is enabled through one (D5), else with their App user token.
        The slot is named from the caller and the owner, never a document."""
        if not via_token(org):
            with self._as_user(caller) as pair:
                yield pair
            return
        owner = str(org.get("owner") or "")
        suffix = owner_suffix(caller.key, owner)
        with ExitStack() as stack:
            try:
                token = self._app._slots.read_access(caller.tenant_id, suffix)
            except Exception as exc:
                log.warning("access could not read the owner token tenant=%s user_hash=%s "
                            "owner=%s (%s)", caller.tenant_id, user_hash(caller.key), owner,
                            type(exc).__name__)
                raise UpstreamUnavailable(
                    f"your token for {owner} could not be read ({type(exc).__name__}); store "
                    f"it again with `uv run sc setup token --owner {owner}`") from None
            stack.enter_context(redaction_literal(token))
            yield (_AsUser(self._github, token),
                   HeldCredential(SCOPE_USER, secret_name_for(caller.tenant_id, suffix), token,
                                  login=str(org.get("forge_login") or "")))

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
        """The Access page: connection, enabled owners, the installs the
        person asked for and GitHub has not shown yet, grants. No forge read."""
        doc = self.connection(caller)
        orgs = sorted(self._mine(ORGS, caller), key=lambda d: str(d.get("owner")))
        grants = sorted(self._mine(GRANTS, caller), key=lambda d: str(d.get("repository")))
        return {"connection": connection_to_api(doc) if doc is not None else None,
                "orgs": [org_to_api(d) for d in orgs if not requested(d)],
                "requested": [org_to_api(d) for d in orgs if requested(d)],
                "grants": [grant_to_api(d) for d in grants]}

    # -- owners ----------------------------------------------------------------

    def owners(self, caller: Caller) -> dict[str, Any]:
        """Every owner the person reaches, each with its install state. An
        owner they asked to install that GitHub now lists is marked installed
        and enabled here, on this read; one it does not list yet is
        `requested`, with ORG_APPROVAL_PENDING's copy."""
        mine = {str(d.get("owner")).lower(): d for d in self._mine(ORGS, caller)}
        asked = {key: doc for key, doc in mine.items() if requested(doc)}
        enabled = {key: doc for key, doc in mine.items() if not requested(doc)}
        tokens = {key: doc for key, doc in enabled.items() if via_token(doc)}
        connection = self.connection(caller)
        if tokens and (connection is None or connection.get("state") == REVOKED):
            # D5: a person who reaches GitHub only through owner tokens has no
            # App token to list installations with; their owners are the ones
            # their tokens enabled.
            rows = sorted((_token_row(doc) for doc in tokens.values()),
                          key=lambda e: (e["owner_type"] != "User", e["owner"].lower()))
            return {"owners": rows, "orgs_listed": False, "install_url": self._install_url()}
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
        flipped = self._mark_installed(caller, asked, installed)
        enabled.update(flipped)
        asked = {key: doc for key, doc in asked.items() if key not in flipped}
        by_owner: dict[str, dict[str, Any]] = {}

        def add(login: str, owner_type: str, inst: _Owner | None) -> None:
            key = login.lower()
            entry = by_owner.get(key)
            if entry is None or (inst is not None and entry["installation_id"] is None):
                doc = enabled.get(key) or {}
                pending = asked.get(key) if inst is None else None
                row = {
                    "owner": login,
                    "owner_type": owner_type,
                    "installation_id": inst.installation_id if inst else None,
                    "repository_selection": inst.repository_selection if inst else None,
                    "install_state": INSTALLED if inst else (
                        REQUESTED if pending else "not_installed"),
                    "sso": doc.get("sso") or "unknown",
                    "enabled": bool(doc) and inst is not None and not via_token(doc),
                    "install_url": None if inst else self._install_url(),
                    "method": "app",
                }
                if pending is not None:
                    row["requested_at"] = _iso(pending.get("requested_at"))
                    row.update(pending_copy(login))
                by_owner[key] = row

        for inst in installed:
            add(inst.login, inst.owner_type, inst)
        if own_login and _OWNER.match(own_login):
            add(own_login, "User", None)
        for login in orgs or []:
            add(login, "Organization", None)
        for doc in asked.values():
            # Asked for, and not among the person's orgs GitHub listed (an
            # org that hides membership): still shown, still pending.
            add(str(doc.get("owner_login") or doc.get("owner")),
                str(doc.get("owner_type") or "Organization"), None)
        for key, doc in tokens.items():
            # Enabled through the person's token for it: what that token
            # reached when it was stored, never the App's view of the owner.
            by_owner[key] = _token_row(doc)
        rows = sorted(by_owner.values(), key=lambda e: (e["owner_type"] != "User",
                                                        e["owner"].lower()))
        log.info("access owners tenant=%s user_hash=%s owners=%d installed=%d",
                 caller.tenant_id, user_hash(caller.key), len(rows), len(installed))
        return {"owners": rows, "orgs_listed": orgs is not None,
                "install_url": self._install_url()}

    def installations(self, caller: Caller) -> dict[str, Any]:
        """Whether the App is installed anywhere the person reaches: the
        onboarding checklist's `app_installed` (#780, 2026-10-08).

        An owner the person enabled was installed when it was enabled
        (`enable` refuses any other), so its document answers with no GitHub
        read. Otherwise this is one `owners` read -- one refresh, as on every
        Access visit. A read that did not come back, a refresh running
        elsewhere (409) or a refused one is `read: False`, never "installed
        nowhere": the step stays in progress rather than sending someone to
        install an App they already installed."""
        docs = self._mine(ORGS, caller)
        installed = sorted(str(d.get("owner_login") or d.get("owner"))
                           for d in docs if d.get("install_state") == INSTALLED)
        asked = sorted((_requested_row(d) for d in docs if requested(d)),
                       key=lambda r: r["owner"].lower())
        enabled_answer = {"read": True, "source": "enabled", "installed": installed,
                          "not_installed": [], "requested": asked,
                          "install_url": self._install_url()}
        if installed and not asked:
            return enabled_answer
        # No enabled owner, or an install request pending: GitHub is read, so
        # an approved request is marked installed on this read (§2.3).
        try:
            found = self.owners(caller)
        except ApiError as refused:
            log.info("access installations unread tenant=%s user_hash=%s code=%s",
                     caller.tenant_id, user_hash(caller.key), refused.code)
            if installed:
                # The enabled owners still answer app_installed; the requests
                # stay pending, as recorded, until a read comes back.
                return enabled_answer
            return {"read": False, "source": "github", "error": refused.code,
                    "unreachable": refused.status_code == 503, "requested": asked,
                    "install_url": self._install_url()}
        rows = found["owners"]
        return {"read": True, "source": "github",
                "installed": [r["owner"] for r in rows if r["install_state"] == INSTALLED],
                "not_installed": [r["owner"] for r in rows
                                  if r["install_state"] != INSTALLED],
                "requested": [{"owner": r["owner"], "requested_at": r.get("requested_at")}
                              for r in rows if r["install_state"] == REQUESTED],
                "install_url": found["install_url"]}

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
        doc_id = org_id_for(caller.tenant_id, caller.key, match.login)
        previous = self._org(caller, match.login) or {}
        doc = self._installed_doc(caller, match, previous)
        if via_token(previous):
            # The App now reaches the owner: the person's token for it is no
            # longer read by anything, so it is not left enabled. BEFORE the
            # document is overwritten: a disable that fails keeps it (503).
            self._app.revoke_owner_token(caller, match.login)
        self._db.collection(ORGS).document(doc_id).set(doc)
        log.info("access owner enabled tenant=%s user_hash=%s owner=%s installation=%s",
                 caller.tenant_id, user_hash(caller.key), doc["owner"], match.installation_id)
        return {"org": org_to_api(doc)}

    def _installed_doc(self, caller: Caller, match: _Owner,
                       previous: dict[str, Any]) -> dict[str, Any]:
        """The enabled owner's document, as `enable` writes it and as an
        approved install request becomes. `requested_at` is kept."""
        now = self._now()
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
            "install_state": INSTALLED,
            "sso": previous.get("sso") or "unknown",
            "enabled_at": previous.get("enabled_at") or now,
            "enabled_by": caller.key,
            "checked_at": now,
        }
        if previous.get("requested_at") is not None:
            doc["requested_at"] = previous["requested_at"]
        return doc

    def _mark_installed(self, caller: Caller, asked: dict[str, dict[str, Any]],
                        installed: list[_Owner]) -> dict[str, dict[str, Any]]:
        """Every requested owner GitHub now lists among the person's
        installations, written installed and enabled. Answers them by key."""
        flipped: dict[str, dict[str, Any]] = {}
        for inst in installed:
            key = inst.login.lower()
            previous = asked.get(key)
            if previous is None or key in flipped:
                continue
            doc = self._installed_doc(caller, inst, previous)
            self._db.collection(ORGS).document(
                org_id_for(caller.tenant_id, caller.key, inst.login)).set(doc)
            flipped[key] = doc
            log.info("access install request approved tenant=%s user_hash=%s owner=%s "
                     "installation=%s", caller.tenant_id, user_hash(caller.key), key,
                     inst.installation_id)
        return flipped

    # -- install requests (§2.3 ORG_APPROVAL_PENDING) ----------------------------

    def request_install(self, caller: Caller, owner: str) -> dict[str, Any]:
        """Record that the person asked `owner`'s owners to install the App.
        No GitHub call: GitHub shows a user token no pending request, which is
        why it is recorded here. An owner already enabled -- installed, or
        through the person's token -- is answered as it is and not touched."""
        check_owner(owner)
        self._require_active(caller)
        previous = self._org(caller, owner)
        if previous is not None and not requested(previous):
            return {"org": org_to_api(previous), "recorded": False,
                    "install_url": self._install_url()}
        now = self._now()
        doc = {
            "tenant_id": caller.tenant_id,
            "user": caller.key,
            "user_hash": user_hash(caller.key),
            "owner": owner.lower(),
            "owner_login": owner,
            "owner_type": "Organization",
            "installation_id": None,
            "repository_selection": None,
            "install_state": REQUESTED,
            "sso": (previous or {}).get("sso") or "unknown",
            "requested_at": (previous or {}).get("requested_at") or now,
            "requested_by": caller.key,
            "checked_at": None,
        }
        self._db.collection(ORGS).document(
            org_id_for(caller.tenant_id, caller.key, owner)).set(doc)
        log.info("access install requested tenant=%s user_hash=%s owner=%s",
                 caller.tenant_id, user_hash(caller.key), owner.lower())
        return {"org": org_to_api(doc), "recorded": True, "install_url": self._install_url()}

    def recheck_requested(self, report: RefreshReport, out_of_time: Callable[[], bool]) -> None:
        """The refresh sweep's re-check (`ForgeApp.on_sweep`): for each person
        holding a requested owner, one `GET /user/installations` with their
        OWN token, and every requested owner it lists is marked installed and
        enabled. A read that did not come back leaves the request as it was.
        Each document is the person's, in its own tenant: the person is named
        from the document's tenant and user, and `_mine`-style checks drop one
        whose hash does not match its user (invariant 9)."""
        query = self._db.collection(ORGS).where(
            filter=FieldFilter("install_state", "==", REQUESTED)).limit(MAX_DOCS)
        people: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for snap in query.stream():
            doc = snap.to_dict() or {}
            tenant_id, user = doc.get("tenant_id"), doc.get("user")
            if not requested(doc) or not isinstance(tenant_id, str) \
                    or not isinstance(user, str) or doc.get("user_hash") != user_hash(user):
                continue
            people.setdefault((tenant_id, user), {})[str(doc.get("owner")).lower()] = doc
        for (tenant_id, user), asked in sorted(people.items())[:MAX_RECHECKS_PER_SWEEP]:
            if out_of_time():
                report.out_of_time = True
                break
            caller = Caller(email=user, tenant_id=tenant_id)
            connection = self.connection(caller)
            if connection is None or connection.get("state") != ACTIVE:
                continue
            report.installs_rechecked += 1
            try:
                with self._as_user(caller) as (github, _held):
                    installed = github.installations()
            except _Unanswered:
                report.installs_unreachable += 1
                continue
            except (_Refused, ApiError) as refused:
                report.installs_refused += 1
                log.info("access install re-check refused tenant=%s user_hash=%s (%s)",
                         tenant_id, user_hash(user), type(refused).__name__)
                continue
            flipped = self._mark_installed(caller, asked, installed)
            report.installs_found += len(flipped)
            now = self._now()
            for key in asked:
                if key not in flipped:
                    self._db.collection(ORGS).document(
                        org_id_for(tenant_id, user, key)).update({"checked_at": now})
        if len(people) > MAX_RECHECKS_PER_SWEEP:
            report.out_of_time = True

    def disable(self, caller: Caller, owner: str) -> dict[str, Any]:
        """§2.4: delete the owner and every grant under it, in one write. For
        a requested owner, that withdraws the request."""
        check_owner(owner)
        doc = self._org(caller, owner)
        if doc is None:
            raise NotFound(f"{owner} is not enabled for you in this tenant")
        grants = [g for g in self._mine(GRANTS, caller)
                  if str(g.get("owner") or "").lower() == owner.lower()]
        # D5: an owner enabled through the person's token loses the token too,
        # every version of it, so removing the org revokes SwarmCloud's access.
        # BEFORE the document goes: a disable that fails raises 503 and keeps
        # it, so removing the owner again still finds the token.
        slot_versions = self._app.revoke_owner_token(caller, owner) if via_token(doc) else {}
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
                "unregistered": unregistered, "token_revoked": via_token(doc),
                "slot_versions_disabled": slot_versions,
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
        if org is None or not (org.get("installation_id") or via_token(org)):
            raise NotFound(f"{owner} is not enabled for you in this tenant: enable it first")
        token = via_token(org)
        installation_id = 0 if token else int(org["installation_id"])
        own_account = org.get("owner_type") == "User"
        total: int | None = None
        capped = False

        def page_of(github: _AsUser, n: int) -> tuple[list[Any], int | None, HttpAnswer]:
            if token:
                return github.owner_page(owner, n, own_account=own_account)
            return github.installation_page(installation_id, n)

        def refuse(answer: HttpAnswer) -> None:
            if token:
                if answer.status != 200:
                    raise self._owner_refusal(answer, owner, "", org)
                return
            self._refuse_listing(caller, answer, owner, installation_id)

        with self._as_owner(caller, org) as (github, _held):
            try:
                if not needle:
                    raw, total, answer = page_of(github, page)
                    refuse(answer)
                    window = raw
                    more = len(raw) >= PAGE_SIZE and page < MAX_PAGES
                    capped = len(raw) >= PAGE_SIZE and page == MAX_PAGES
                else:
                    matched: list[Any] = []
                    for gh_page in range(1, MAX_PAGES + 1):
                        raw, total, answer = page_of(github, gh_page)
                        refuse(answer)
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

    def _owner_refusal(self, answer: HttpAnswer, owner: str, repo: str,
                       org: dict[str, Any] | None) -> AccessRefused:
        """`_refusal` for an owner reached through the App; for one reached
        through the person's token (D5), §2.3's token causes instead: SSO,
        the classic-token policy, a fine-grained token not yet approved, and
        otherwise the token's own lack of access to what was read."""
        if not via_token(org):
            return self._refusal(answer, owner, repo, (org or {}).get("installation_id"))
        sso = _sso_owner(answer)
        if sso is not None:
            return self._refusal(answer, owner, repo, None)
        data = answer.json()
        message = data.get("message") if isinstance(data, dict) else None
        if answer.status == 403 and isinstance(message, str) and _CLASSIC_BLOCKED.search(message):
            return AccessRefused("CLASSIC_PAT_BLOCKED",
                                 f"{owner} does not accept classic personal access tokens",
                                 owner=owner, status=403)
        what = f"{owner}/{repo}" if repo else f"{owner}'s repositories"
        if (org or {}).get("token_kind") == "fine_grained_pat" \
                and (org or {}).get("owner_type") != "User":
            return AccessRefused("FINE_GRAINED_PAT_PENDING",
                                 f"GitHub answered HTTP {answer.status} for {what} to your "
                                 f"fine-grained token for {owner}", owner=owner, status=403)
        return AccessRefused("PERMISSION_MISSING",
                             f"GitHub answered HTTP {answer.status} for {what} to your token "
                             f"for {owner}", repo=what, login=str((org or {}).get("forge_login")
                                                                  or ""), status=403)

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
        if org is None or requested(org):
            raise AccessRefused(
                "REPO_NOT_INSTALLED", f"{owner} is not enabled for you in this tenant: enable "
                "it under Access first", owner=owner, repo=f"{owner}/{repo}",
                url=self._install_url() or "")
        with self._as_owner(caller, org) as (github, held):
            try:
                answer = github.get(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}")
            except _Unanswered:
                raise _unreachable(f"the read of {owner}/{repo}") from None
            # Names only: the credential, value and all, stays inside the `with`.
            secret_name, login = held.secret_name, held.login or ""
        if answer.status != 200:
            raise self._owner_refusal(answer, owner, repo, org)
        read = repository_from(answer.json())
        if read is None or (read.owner.lower(), read.repo.lower()) != (owner.lower(),
                                                                       repo.lower()):
            raise UpstreamUnavailable(f"GitHub's answer for {owner}/{repo} is not that repository")
        if not read.can_read:
            raise self._owner_refusal(HttpAnswer(403, {}), owner, repo, org)
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
        grant push and pull request. Reads only (D6), unless the caller names
        `push_test`: then, on a write grant only, one branch is created and
        deleted (`_push_test`)."""
        doc = self._grant(caller, repo_id)
        if doc is None:
            raise NotFound(f"you hold no grant on {repo_id[:64]!r} in this tenant")
        wanted = list(dict.fromkeys(checks or CHECKS))
        owner, repo = str(doc["repository"]).split("/", 1)
        write = doc.get("mode") == "write"
        if PUSH_TEST in wanted and not write:
            # D9: SwarmCloud enforces a read grant, so it never pushes where
            # the person chose read -- not even a test branch. Refused before
            # the token is opened: nothing reaches GitHub. Not a switch: the
            # request let through would be the push D9 forbids.
            raise ValidationFailed(
                f"push_test writes a branch, and your grant on {doc['repository']} is read: "
                "SwarmCloud enforces read. Change the grant to write first.")
        org = self._org(caller, owner) or {}
        now = self._now()
        results: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []

        def put(name: str, state: str, code: str | None = None, **fill: str) -> None:
            results[name] = {"state": state, "code": code, "checked_at": now}
            if code is not None:
                failures.append({"check": name, "code": code,
                                 "copy": recovery_copy(code, **fill)})

        token = via_token(org)
        pushed: dict[str, Any] | None = None
        with self._as_owner(caller, org) as (github, held):
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
                refusal = self._owner_refusal(answer, owner, repo, org)
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
                        pr_ok: bool | None
                        if token:
                            # A token that pushes opens a pull request; its
                            # pull-request permission is not readable (D5).
                            pr_ok = True
                        else:
                            try:
                                installed = github.installations()
                                inst = next((i for i in installed
                                             if i.login.lower() == owner.lower()), None)
                                pr_ok = inst is not None and inst.pull_requests == "write"
                            except (_Unanswered, _Refused):
                                pr_ok = None
                        if push_ok is None or pr_ok is None:
                            put("pull_request", UNKNOWN, "FORGE_UNREACHABLE")
                        elif push_ok and pr_ok:
                            put("pull_request", OK)
                        else:
                            put("pull_request", MISSING, "PERMISSION_MISSING",
                                repo=doc["repository"], login=login)
                if PUSH_TEST in wanted:
                    pushed = self._push_test(caller, repo_id, github, owner, repo,
                                             read.default_branch, now, results, failures,
                                             login=login)
        stored = {**(doc.get("checks") or {}), **results}
        update = {"checks": stored, "verified_at": now}
        self._db.collection(GRANTS).document(
            grant_id_for(caller.tenant_id, caller.key, repo_id)).update(update)
        doc.update(update)
        log.info("access verify tenant=%s user_hash=%s repo_id=%s %s", caller.tenant_id,
                 user_hash(caller.key), repo_id,
                 " ".join(f"{k}={v['state']}" for k, v in results.items()))
        answer_body = {"grant": grant_to_api(doc), "failures": failures,
                       "passed": not failures and all(r["state"] in (OK, NOT_REQUIRED)
                                                      for r in results.values())}
        if pushed is not None:
            answer_body["push_test"] = pushed
        return answer_body

    def _push_test(self, caller: Caller, repo_id: str, github: _AsUser, owner: str,
                   repo: str, default_branch: str, now: datetime,
                   results: dict[str, dict[str, Any]], failures: list[dict[str, Any]], *,
                   login: str) -> dict[str, Any] | None:
        """D6 (b): the one write verify makes, with the token `_as_owner`
        picked for the owner. Reads the default branch head, creates
        `refs/heads/swarmcloud/onboarding-check-<nonce>` there, and deletes
        it in a `finally`. Records `checks.push_test`; answers the branch and
        whether it was left behind, or None when nothing was created.

        A 403 or 404 on create is `missing` (PERMISSION_MISSING); a 5xx, a
        429 or no answer is FORGE_UNREACHABLE and `unknown`, never `missing`.
        A delete that fails leaves the push proven (`ok`) and answers
        `leftover: true` with the branch, so the person can delete it."""
        full = f"{owner}/{repo}"
        base = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git"

        def record(state: str, code: str | None = None, copy: str | None = None,
                   **extra: Any) -> None:
            results[PUSH_TEST] = {"state": state, "code": code, "checked_at": now, **extra}
            if code is not None or copy is not None:
                failures.append({"check": PUSH_TEST, "code": code,
                                 "copy": copy or recovery_copy(code or "", repo=full,
                                                               login=login)})

        if not default_branch:
            record(UNKNOWN, copy=f"{full} has no default branch to test a push from; push a "
                                 "first commit, then run the push test again.")
            return None
        try:
            head = github.get(f"{base}/ref/heads/{quote(default_branch, safe='/')}")
        except _Unanswered:
            record(UNKNOWN, "FORGE_UNREACHABLE")
            return None
        data = head.json()
        target = data.get("object") if isinstance(data, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if head.status in (403, 404):
            record(MISSING, "PERMISSION_MISSING")
            return None
        if head.status != 200 or not isinstance(sha, str) or not re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            record(UNKNOWN, copy=f"GitHub answered HTTP {head.status} for the head of "
                                 f"{full}'s {default_branch}, so there is no commit to test "
                                 "a push from; press Re-check to try again.")
            return None
        branch = PUSH_TEST_PREFIX + secrets.token_hex(8)
        try:
            created = github.api("POST", f"{base}/refs",
                                 {"ref": f"refs/heads/{branch}", "sha": sha})
        except _Unanswered:
            # Not answered is not refused, and may still have been made: the
            # delete below is tried all the same.
            created = None
        # A create GitHub did not answer may still have been made, so its
        # delete is tried too; only a refused create is known to leave nothing.
        maybe_made = created is None or created.status == 201
        deleted = False
        try:
            if created is None:
                record(UNKNOWN, "FORGE_UNREACHABLE")
            elif created.status == 201:
                record(OK)
            elif created.status in (403, 404):
                record(MISSING, "PERMISSION_MISSING")
            else:
                record(UNKNOWN, copy=f"GitHub answered HTTP {created.status} to the test "
                                     f"branch in {full}; press Re-check to try again.")
        finally:
            if maybe_made:
                deleted = self._delete_branch(caller, repo_id, github, base, branch,
                                              must_exist=created is not None)
        if not maybe_made or (created is None and deleted):
            return None
        results[PUSH_TEST].update({"branch": branch, "leftover": not deleted})
        return {"branch": branch, "leftover": not deleted}

    @staticmethod
    def _delete_branch(caller: Caller, repo_id: str, github: _AsUser, base: str, branch: str,
                       *, must_exist: bool) -> bool:
        """Delete the push test's branch: True when GitHub says it is gone.
        Logged by name and status, never with the token."""
        try:
            answer = github.api("DELETE", f"{base}/refs/heads/{quote(branch, safe='/')}")
            status: str = str(answer.status)
            gone = answer.status == 204 or (not must_exist and answer.status in (404, 422))
        except _Unanswered as exc:
            status, gone = type(exc).__name__, False
        if not gone:
            log.warning("access push_test left its branch tenant=%s user_hash=%s repo_id=%s "
                        "branch=%s delete=%s", caller.tenant_id, user_hash(caller.key),
                        repo_id, branch, status)
        return gone

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


def _requested_row(doc: dict[str, Any]) -> dict[str, Any]:
    return {"owner": str(doc.get("owner_login") or doc.get("owner")),
            "requested_at": _iso(doc.get("requested_at"))}


def _token_row(doc: dict[str, Any]) -> dict[str, Any]:
    """An owner enabled through the person's token (D5), as the owners list
    shows it: from its document, which the token's probe wrote."""
    return {
        "owner": str(doc.get("owner_login") or doc.get("owner")),
        "owner_type": doc.get("owner_type") or "Organization",
        "installation_id": None,
        "repository_selection": None,
        "install_state": doc.get("install_state") or "token",
        "sso": doc.get("sso") or "unknown",
        "enabled": True,
        "install_url": None,
        "method": METHOD_PAT,
    }


def _name(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    name = raw.get("full_name")
    return name.lower() if isinstance(name, str) else ""


def build_access(db: Any, forge_app: ForgeApp, *, now: Callable[[], datetime] = utcnow
                 ) -> AccessService:
    """Production wiring: urllib to GitHub. Builds no client."""
    return AccessService(db, forge_app, now=now)
