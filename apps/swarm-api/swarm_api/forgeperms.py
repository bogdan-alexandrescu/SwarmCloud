"""GitHub permissions health: what the platform needs, and who withholds it (#1086, PERM-1).

    GET /v1/forge/permissions[?refresh=true]   the caller's tenant's answer

On 2026-10-11 sagaxyz/ai-studio's merges failed with `token_lacks_rights`
because the App lacked `statuses: read`, and nothing in the console said so.
This read says it before a run fails, in four parts:

  * `required` -- `REQUIRED` below, the one table of what the platform's steps
    need. Nothing else restates it: the console, the docs' prose and the
    capability derivation all read this table.
  * `app` -- what the App itself requests (`GET /app` with the App JWT
    `ForgeApp` signs), what of `required` it does not, and `settings_url`,
    the page where an App owner changes it. `settings_url` is served to
    platform admins only (`serve`): no member can change the App, and a
    button to a page they cannot open is noise.
  * `orgs` -- one entry per installation that covers a registered repository:
    what it granted (`GET /app/installations/{id}`), what of `required` that
    lacks, and `pending_update`, true when the App requests a permission the
    installation has not accepted -- the 2026-10-11 state. `review_url` is
    the org's (or account's) page to accept it. GitHub lets nobody but the
    org's owner accept, so a fix is always a link.
  * `repositories` -- one per registration: `credential` "app" when the App
    is installed on it (`GET /repos/{o}/{r}/installation`), "token" when not,
    and what each capability can do. An App-backed repository's capabilities
    derive from its installation's grant. A token-backed one is probed with
    two read-only GETs on the tenant's own `-git` token: the repository (its
    role) and one combined-status read on the default branch's head.

WORKFLOW FILES (#2029, WF-APP-TOKEN). A repository whose registration has
`workflow_files` switched on is pushed to with an App token that needs
`workflows: write` (BGD's decision 4: the App requests "Workflows: Read and
write", each installation's owner accepts it). That requirement,
`WORKFLOW_FILES_REQUIREMENT`, is checked for a switched-on registration ONLY,
and its row carries the capability `workflow_files`: false with the gap in
`blocked_by` when the installation has not granted it (the console's
"Workflows: not granted"), or when no installation covers the repository at
all. Every other repository reads exactly as before: `REQUIRED`, the App's
and the orgs' `missing`, unchanged.

TRUE / FALSE / NULL. A capability is null when it was not measured -- GitHub
did not answer, or no cheap read can tell (a fine-grained token's
`pull_requests: write` is not readable without writing) -- and never false
for that reason. False is a measured refusal; `blocked_by` names it.

ONE ORG FAILING IS ONE ENTRY'S `error`, never a 500 for the whole answer:
the read is for the moment something is wrong, so it must not fail with it.

READ-ONLY. Every call is a GET. No installation token is minted (that is a
POST); the App JWT reads `/app`, `/app/installations/{id}` and
`/repos/{o}/{r}/installation`, the three calls a JWT answers. The JWT and the
tenant token are registered with the redaction filter while in hand, and no
response, log line or cache entry carries either.

CACHED per tenant for `CACHE_SECONDS`; `refresh=true` reads again. The cache
holds the admin form; `serve` drops `settings_url` per caller, so a member
served from an admin's read still never sees it.

QUOTA (QUOTA-THROTTLE, #1483). Every answer updates `forge_limits.QUOTAS`:
the App JWT's reads as `app`, the tenant token's probes as the tenant's
`-git` slot. A rate-limited 403 or 429 is GitHub not answering -- null,
never a measured refusal. The cached read, which the console makes on its
own, is background: when the cache is stale and the tenant token's quota is
under `forge_quota_defer_percent` (or held), the last answer is served
again (`deferred: true`), or, with none, the token probes are not made and
their rows say why. `refresh=true` is a person pressing Refresh and is never
deferred.

INVARIANTS. A read holds no capacity and creates no infrastructure demand
(invariant 1); it stores nothing in Firestore.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Query, Request

from swarm_common.models import Tenant

from .auth import AuthContext
from .deps import AppContext, current_auth, get_context, tenant_scope
from .errors import ApiError
from .forge import NoForgeCredential
from . import forge_limits
from .forgeapp import APP_URL, AppNotConfigured, ForgeApp, HttpAnswer, HttpSend
from .gittokens import redaction_literal, token_kind
from .repositories import Repositories, workflow_files_of

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# what the platform needs: the one table
# --------------------------------------------------------------------------

#: The capabilities a repository row reports, in the order the console shows.
CAPABILITIES = ("push", "pull_request", "merge", "ci", "issues", "actions")


@dataclass(frozen=True)
class Requirement:
    #: GitHub's permission name, as `GET /app` and an installation spell it.
    permission: str
    #: The least access that does: "read" or "write".
    access: str
    #: What breaks without it, in the words the console shows.
    needed_for: str
    #: The repository capabilities it gates (`CAPABILITIES`).
    capabilities: tuple[str, ...]

    def to_api(self) -> dict[str, Any]:
        return {"permission": self.permission, "access": self.access,
                "needed_for": self.needed_for, "capabilities": list(self.capabilities)}


REQUIRED: tuple[Requirement, ...] = (
    # contents: write -- the worker's `git push` of a task branch
    # (agent_worker/gitops.py), and merge.py's PUT /pulls/{n}/merge, which
    # writes the base branch.
    Requirement("contents", "write", "runs can't push their branch, and merges can't land",
                ("push", "merge")),
    # pull_requests: write -- forgewrite.py opens and updates pull requests
    # (PULLS_WRITE), post_verdict.py comments a verdict, merge.py merges.
    Requirement("pull_requests", "write", "runs can't open or update pull requests, or merge them",
                ("pull_request", "merge")),
    # statuses: read -- merge.py reads /commits/{sha}/status before it
    # merges; without it every merge fails `token_lacks_rights` (#1086).
    Requirement("statuses", "read", "merges can't see CI, so they fail",
                ("merge", "ci")),
    # checks: read -- merge.py reads /commits/{sha}/check-runs to wait for
    # CI; issueci.py reads a run's checks for CI fixes.
    Requirement("checks", "read", "merges and CI fixes can't see check runs",
                ("merge", "ci")),
    # issues: write -- issue runs read and comment on their issue
    # (issueruns, issuecomments, agent_worker/issue.py) and the observer
    # files and updates its findings.
    Requirement("issues", "write", "issue runs and the observer can't read or comment on issues",
                ("issues",)),
    # actions: read -- the ci-flake-hunter and release-health schedules read
    # workflow runs and their jobs (schedtypes/).
    Requirement("actions", "read", "ci-flake-hunter and release-health can't read workflow runs",
                ("actions",)),
)

#: What a registration with `workflow_files` on needs besides `REQUIRED`
#: (#2029): GitHub refuses a push touching .github/workflows/ from a token
#: without it. Never part of `REQUIRED`, so no other repository reports it.
WORKFLOW_FILES_REQUIREMENT = Requirement(
    "workflows", "write",
    "workflow-file pushes fall back to workflow-lines.patch (Workflows: not granted)",
    ("workflow_files",))

#: What writing a GitHub Actions secret needs besides `REQUIRED`
#: (SSC-ACTIONS-API, part of #2158; secrets-scopes.md §3.1 and §11 Q3/Q4):
#: the existing App gains "Secrets: Read and write" and "Environments: Read
#: and write" (docs/runbooks/github-app.md, "Writing GitHub Actions
#: secrets"), each installation's owner accepts them, and until then every
#: write is refused with the installation's `review_url`. Reading an
#: environment's branch rule is Actions: read, already in `REQUIRED`.
#: Never `administration`: no environment is created or changed (Q4). Not
#: part of `REQUIRED`, so no repository's existing capabilities change.
ACTIONS_SECRETS_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("secrets", "write",
                "SwarmCloud can't write the repository's GitHub Actions secrets",
                ("actions_secrets",)),
    Requirement("environments", "write",
                "SwarmCloud can't write a GitHub Actions environment's secrets",
                ("actions_secrets",)),
)

#: GitHub's access levels, least first. Anything else (absent) is none.
_RANK = {"read": 1, "write": 2, "admin": 3}

#: How long a tenant's answer is served again before GitHub is read anew.
CACHE_SECONDS = 60.0
#: Per GitHub call. A slow org must not hold the whole answer for long.
TIMEOUT_SECONDS = 8.0
#: Registrations read per page while listing the tenant's repositories.
_PAGE = 200

API_HOST = "api.github.com"
_USER_AGENT = "swarmcloud-swarm-api"


def _rank(access: Any) -> int:
    return _RANK.get(access, 0) if isinstance(access, str) else 0


def missing(granted: Mapping[str, Any] | None,
            required: Iterable[Requirement] = REQUIRED) -> list[dict[str, Any]]:
    """Each requirement `granted` does not meet, with what it holds instead."""
    held = granted or {}
    return [{**req.to_api(), "granted": held.get(req.permission)
             if isinstance(held.get(req.permission), str) else None}
            for req in required if _rank(held.get(req.permission)) < _rank(req.access)]


def pending(requested: Mapping[str, Any] | None,
            granted: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Each permission the App requests above what the installation accepted."""
    held = granted or {}
    return [{"permission": name, "requested": access,
             "granted": held.get(name) if isinstance(held.get(name), str) else None}
            for name, access in sorted((requested or {}).items())
            if _rank(held.get(name)) < _rank(access)]


def _permissions(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def app_settings_url(slug: str, owner: str | None = None, owner_type: str | None = None) -> str:
    """Where an App's owner changes what it requests."""
    if owner and owner_type == "Organization":
        return (f"https://github.com/organizations/{quote(owner, safe='')}/settings/apps/"
                f"{quote(slug, safe='')}/permissions")
    return f"https://github.com/settings/apps/{quote(slug, safe='')}/permissions"


def review_url(account: str, installation_id: int, account_type: str | None) -> str:
    """Where an org's owner (or a user, for their own account) accepts a
    permission update: GitHub's own review form for the installation."""
    if account_type == "User":
        return f"https://github.com/settings/installations/{int(installation_id)}/permissions/update"
    return (f"https://github.com/organizations/{quote(account, safe='')}/settings/installations/"
            f"{int(installation_id)}/permissions/update")


# --------------------------------------------------------------------------
# GitHub, read-only
# --------------------------------------------------------------------------

class _Unanswered(Exception):
    """GitHub did not answer: a transport error, a 5xx or a 429. Constant text."""


class _Reader:
    """GETs to api.github.com with one bearer value, never anything else."""

    def __init__(self, send: HttpSend, timeout: float = TIMEOUT_SECONDS) -> None:
        self._send = send
        self._timeout = timeout

    def get(self, path: str, bearer: str, credential: str = forge_limits.APP_CREDENTIAL,
            ) -> HttpAnswer:
        url = f"https://{API_HOST}{path}"
        if urlparse(url).hostname != API_HOST:  # pragma: no cover - built from constants
            raise _Unanswered("refused host")
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {bearer}",
                   "User-Agent": _USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        try:
            answer = self._send("GET", url, headers, None, self._timeout)
        except Exception as exc:
            raise _Unanswered(f"no answer ({type(exc).__name__})") from None
        limited = forge_limits.QUOTAS.observe_answer(answer, credential=credential)
        if limited is not None:
            # GitHub limiting requests is not GitHub refusing them (#1483).
            when = forge_limits.until(limited)
            raise _Unanswered(f"GitHub is limiting requests (HTTP {answer.status})"
                              + (f" until {when}" if when else ""))
        if answer.status >= 500 or answer.status == 429:
            raise _Unanswered(f"GitHub answered HTTP {answer.status}")
        return answer


def _repo_path(owner: str, repo: str, rest: str = "") -> str:
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}{rest}"


@dataclass(frozen=True)
class Registered:
    """A registration's names: all this read needs of one."""

    repo_id: str
    owner: str
    repo: str
    default_branch: str | None = None
    #: The registration's `workflow_files` switch (#2029).
    workflow_files: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def _capabilities_from(granted: Mapping[str, Any], *, workflow_files: bool = False,
                       ) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    gaps = missing(granted)
    capabilities = CAPABILITIES
    if workflow_files:
        # #2029: checked only where the switch is on.
        gaps += missing(granted, (WORKFLOW_FILES_REQUIREMENT,))
        capabilities = (*CAPABILITIES, *WORKFLOW_FILES_REQUIREMENT.capabilities)
    blocked = {cap for gap in gaps for cap in gap["capabilities"]}
    return {cap: cap not in blocked for cap in capabilities}, gaps


def _none() -> dict[str, None]:
    return {cap: None for cap in CAPABILITIES}


def _token_gap(capabilities: list[str], reason: str) -> dict[str, Any]:
    """A token's measured refusal, in `blocked_by`'s shape: no permission
    name, because a token's grant is not readable from GitHub."""
    return {"permission": None, "access": None, "granted": None,
            "needed_for": reason, "capabilities": capabilities}


def probe_token(reader: _Reader, token: str, r: Registered,
                credential: str = "") -> dict[str, Any]:
    """Two read-only GETs: the repository (the token's role on it) and the
    combined status of the default branch's head. Null wherever neither
    tells; false only where GitHub refused. `credential`: whose quota the
    answers update (the tenant's `-git` slot)."""
    try:
        answer = reader.get(_repo_path(r.owner, r.repo), token, credential)
    except _Unanswered as exc:
        return {"capabilities": _none(), "blocked_by": [], "error": f"repository read: {exc}"}
    if answer.status in (401, 403, 404):
        what = ("GitHub does not accept the tenant token" if answer.status == 401
                else "the tenant token cannot read the repository")
        return {"capabilities": {cap: False for cap in CAPABILITIES},
                "blocked_by": [_token_gap(list(CAPABILITIES), f"{what} (HTTP {answer.status})")],
                "error": None}
    data = answer.json()
    if answer.status != 200 or not isinstance(data, dict):
        return {"capabilities": _none(), "blocked_by": [],
                "error": f"repository read: GitHub answered HTTP {answer.status}"}
    role = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    push = any(role.get(name) is True for name in ("push", "maintain", "admin"))
    triage = push or role.get("triage") is True
    branch = data.get("default_branch") if isinstance(data.get("default_branch"), str) \
        and data.get("default_branch") else (r.default_branch or "main")
    ci: bool | None
    try:
        status = reader.get(_repo_path(r.owner, r.repo, f"/commits/{quote(branch, safe='')}/status"),
                            token, credential)
        # 404 with the repository readable is a missing branch, not a refusal.
        ci = True if status.status == 200 else False if status.status in (401, 403) else None
    except _Unanswered:
        ci = None
    classic = token_kind(token) == "classic_pat"
    # A classic token's `repo` scope covers every write its role allows; a
    # fine-grained token's per-permission grant is not readable, so what
    # the role allows is the most this can say -- null, never a guess.
    caps: dict[str, bool | None] = {
        "push": push,
        "pull_request": False if not push else (True if classic else None),
        "merge": False if (not push or ci is False) else (True if classic and ci else None),
        "ci": ci,
        "issues": False if not triage else (True if classic else None),
        "actions": None,
    }
    blocked: list[dict[str, Any]] = []
    if not push:
        blocked.append(_token_gap(["push", "pull_request", "merge"],
                                  "the token's role on the repository does not allow push"))
    if ci is False:
        blocked.append(_token_gap(["ci", "merge"], "the token cannot read commit statuses, "
                                  "so merges can't see CI"))
    if not triage:
        blocked.append(_token_gap(["issues"], "the token's role cannot triage issues"))
    return {"capabilities": caps, "blocked_by": blocked, "error": None}


# --------------------------------------------------------------------------
# the read
# --------------------------------------------------------------------------

class ForgePermissions:
    """The tenant's permissions answer, cached for `CACHE_SECONDS`."""

    def __init__(
        self,
        *,
        app_jwt: Callable[[], str] | None,
        app_slug: str = "",
        send: HttpSend,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_SECONDS,
    ) -> None:
        self._app_jwt = app_jwt
        self._app_slug = app_slug
        self._reader = _Reader(send)
        self._clock = clock
        self._ttl = ttl
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # -- the cache ------------------------------------------------------------

    def read(
        self,
        tenant_id: str,
        *,
        repositories: Callable[[], list[Registered]],
        token: Callable[[], str],
        now: datetime,
        is_admin: bool,
        refresh: bool = False,
        defer_percent: float | None = None,
    ) -> dict[str, Any]:
        """`defer_percent`: the console's own read is background, and waits
        under this percent of the tenant token's quota; None (a person's
        `refresh=true`) never waits."""
        at = self._clock()
        with self._lock:
            held = None if refresh else self._cache.get(tenant_id)
        if held is not None and at - held[0] < self._ttl:
            return serve(held[1], is_admin=is_admin, cached=True)
        deferral = None
        if not refresh and defer_percent is not None:
            deferral = forge_limits.QUOTAS.defer(forge_limits.tenant_credential(tenant_id),
                                                 defer_percent, now=now)
        if deferral is not None:
            forge_limits.log_deferred("forge permissions", tenant_id, deferral)
            if held is not None:
                # The last answer again, said to be old, rather than spending
                # what is left of the tenant token's quota.
                return {**serve(held[1], is_admin=is_admin, cached=True), "deferred": True}
        answer = self.compute(tenant_id, repositories(), token=token, now=now,
                              deferral=deferral)
        with self._lock:
            self._cache[tenant_id] = (at, answer)
        return serve(answer, is_admin=is_admin, cached=False)

    # -- one fresh answer -----------------------------------------------------

    def compute(self, tenant_id: str, repositories: list[Registered], *,
                token: Callable[[], str], now: datetime,
                deferral: forge_limits.Deferral | None = None) -> dict[str, Any]:
        with ExitStack() as held:
            jwt, app = self._app(held)
            requested = app.get("requested")
            lookups = self._installations(jwt, repositories)
            orgs = self._orgs(jwt, lookups, requested)
            rows = self._repositories(held, repositories, lookups, orgs, token,
                                      tenant_id=tenant_id, deferral=deferral)
        log.info("forge permissions tenant=%s repositories=%d orgs=%d app_missing=%s",
                 tenant_id, len(rows), len(orgs), len(app.get("missing") or []))
        return {
            "tenant_id": tenant_id,
            "checked_at": now.isoformat(),
            "required": [req.to_api() for req in REQUIRED],
            # #2029: what a `workflow_files` registration needs besides.
            "workflow_files_required": WORKFLOW_FILES_REQUIREMENT.to_api(),
            "app": app,
            "orgs": list(orgs.values()),
            "repositories": rows,
        }

    def _app(self, held: ExitStack) -> tuple[str | None, dict[str, Any]]:
        app: dict[str, Any] = {"configured": False, "slug": self._app_slug or None,
                               "granted": None, "requested": None, "missing": None,
                               "settings_url": None, "error": None}
        if self._app_jwt is None:
            app["error"] = "the GitHub App is not configured in swarm-api"
            return None, app
        try:
            jwt = self._app_jwt()
        except AppNotConfigured as exc:
            app["error"] = exc.message
            return None, app
        held.enter_context(redaction_literal(jwt))
        app["configured"] = True
        try:
            answer = self._reader.get(urlparse(APP_URL).path, jwt)
        except _Unanswered as exc:
            app["error"] = f"GET /app: {exc}"
            return jwt, app
        data = answer.json()
        if answer.status != 200 or not isinstance(data, dict):
            app["error"] = f"GET /app: GitHub answered HTTP {answer.status}"
            return jwt, app
        requested = _permissions(data.get("permissions"))
        slug = data.get("slug") if isinstance(data.get("slug"), str) else self._app_slug
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        app.update(slug=slug or None, granted=requested, requested=requested,
                   missing=missing(requested))
        if slug:
            app["settings_url"] = app_settings_url(
                slug, owner.get("login") if isinstance(owner.get("login"), str) else None,
                owner.get("type") if isinstance(owner.get("type"), str) else None)
        return jwt, app

    def _installations(self, jwt: str | None,
                       repositories: list[Registered]) -> dict[str, dict[str, Any]]:
        """repo_id -> {installation, account, account_type} or {none} or {error}."""
        found: dict[str, dict[str, Any]] = {}
        for r in repositories:
            if jwt is None:
                # No App to ask: every repository is the tenant token's.
                found[r.repo_id] = {"installation": None}
                continue
            try:
                answer = self._reader.get(_repo_path(r.owner, r.repo, "/installation"), jwt)
            except _Unanswered as exc:
                found[r.repo_id] = {"error": f"installation lookup: {exc}"}
                continue
            data = answer.json()
            if answer.status == 404:
                found[r.repo_id] = {"installation": None}
            elif answer.status == 200 and isinstance(data, dict) and isinstance(data.get("id"), int):
                account = data.get("account") if isinstance(data.get("account"), dict) else {}
                login = account.get("login") if isinstance(account.get("login"), str) else r.owner
                kind = account.get("type") if isinstance(account.get("type"), str) else None
                found[r.repo_id] = {"installation": int(data["id"]), "account": login,
                                    "account_type": kind}
            else:
                found[r.repo_id] = {"error": "installation lookup: GitHub answered "
                                             f"HTTP {answer.status}"}
        return found

    def _orgs(self, jwt: str | None, lookups: Mapping[str, Mapping[str, Any]],
              requested: Mapping[str, Any] | None) -> dict[int, dict[str, Any]]:
        orgs: dict[int, dict[str, Any]] = {}
        for lookup in lookups.values():
            inst = lookup.get("installation")
            if not isinstance(inst, int) or inst in orgs or jwt is None:
                continue
            entry: dict[str, Any] = {
                "org": lookup["account"], "account_type": lookup.get("account_type"),
                "installation_id": inst, "granted": None, "missing": None,
                "pending_update": None, "pending": None,
                "review_url": review_url(lookup["account"], inst, lookup.get("account_type")),
                "error": None,
            }
            orgs[inst] = entry
            try:
                answer = self._reader.get(f"/app/installations/{inst}", jwt)
            except _Unanswered as exc:
                entry["error"] = f"installation read: {exc}"
                continue
            data = answer.json()
            if answer.status != 200 or not isinstance(data, dict):
                entry["error"] = f"installation read: GitHub answered HTTP {answer.status}"
                continue
            granted = _permissions(data.get("permissions"))
            entry["granted"] = granted
            entry["missing"] = missing(granted)
            if requested is not None:
                gaps = pending(requested, granted)
                entry["pending"] = gaps
                entry["pending_update"] = bool(gaps)
        return orgs

    def _repositories(self, held: ExitStack, repositories: list[Registered],
                      lookups: Mapping[str, Mapping[str, Any]],
                      orgs: Mapping[int, Mapping[str, Any]],
                      token: Callable[[], str], *, tenant_id: str = "",
                      deferral: forge_limits.Deferral | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        credential = forge_limits.tenant_credential(tenant_id) if tenant_id else ""
        value: str | None = None
        token_error: str | None = None
        for r in repositories:
            lookup = lookups.get(r.repo_id) or {}
            row: dict[str, Any] = {"repo_id": r.repo_id, "repo": r.full_name,
                                   "credential": None, "installation_id": None,
                                   "capabilities": _none(), "blocked_by": [], "error": None}
            rows.append(row)
            if "error" in lookup:
                # Neither "app" nor "token": which credential acts here is
                # itself what GitHub did not answer.
                row["error"] = lookup["error"]
                continue
            inst = lookup.get("installation")
            if isinstance(inst, int):
                row["credential"] = "app"
                row["installation_id"] = inst
                org = orgs.get(inst) or {}
                if org.get("granted") is None:
                    row["error"] = org.get("error") or "the installation was not read"
                    continue
                row["capabilities"], row["blocked_by"] = _capabilities_from(
                    org["granted"], workflow_files=r.workflow_files)
                continue
            row["credential"] = "token"
            if deferral is not None:
                # Not measured, so null: never false for a read not made.
                row["error"] = ("not read: the tenant token's GitHub quota is low until "
                                + deferral.until.astimezone(timezone.utc).strftime("%H:%M UTC"))
                continue
            if value is None and token_error is None:
                try:
                    value = token()
                except ApiError as exc:
                    token_error = exc.message
                except Exception as exc:
                    token_error = f"the tenant token could not be read ({type(exc).__name__})"
                if value:
                    held.enter_context(redaction_literal(value))
            if token_error is not None or not value:
                row["error"] = token_error or "the tenant token is empty"
                continue
            row.update(probe_token(self._reader, value, r, credential))
            if r.workflow_files:
                _no_app_for_workflows(row)
        return rows


def _no_app_for_workflows(row: dict[str, Any]) -> None:
    """A switched-on registration no installation covers (#2029): its
    workflow-file pushes have no App token to mint, a measured refusal."""
    row["capabilities"] = {**(row.get("capabilities") or {}), "workflow_files": False}
    row["blocked_by"] = [*(row.get("blocked_by") or []), _token_gap(
        list(WORKFLOW_FILES_REQUIREMENT.capabilities),
        "the GitHub App is not installed on the repository, so workflow-file pushes fall "
        "back to workflow-lines.patch")]


def serve(answer: Mapping[str, Any], *, is_admin: bool, cached: bool) -> dict[str, Any]:
    """`answer` for one caller: `settings_url` only for a platform admin."""
    app = dict(answer.get("app") or {})
    if not is_admin:
        app["settings_url"] = None
    return {**answer, "app": app, "cached": cached}


def build_forge_permissions(forge_app: ForgeApp) -> ForgePermissions:
    """Over the App `create_app` already holds: its JWT, its slug, its transport."""
    return ForgePermissions(app_jwt=forge_app.signed_app_jwt, app_slug=forge_app.config.slug,
                            send=forge_app.send)


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

router = APIRouter(prefix="/v1/forge", tags=["forge"])


def get_forge_permissions(request: Request) -> ForgePermissions:
    """The service `create_app` hung on the app; a test injects one over fakes."""
    return request.app.state.forge_perms


def _registered(ctx: AppContext, tenant_id: str) -> list[Registered]:
    store = Repositories(ctx.db, now=ctx.now)
    found: list[Registered] = []
    page: str | None = None
    while True:
        rows, page = store.list(tenant_id, limit=_PAGE, page_token=page)
        for row in rows:
            if isinstance(row.get("owner"), str) and isinstance(row.get("repo"), str):
                found.append(Registered(repo_id=str(row.get("repo_id")), owner=row["owner"],
                                        repo=row["repo"],
                                        default_branch=row.get("default_branch"),
                                        workflow_files=workflow_files_of(row)))
        if not page:
            return found


@router.get("/permissions")
def get_permissions(
    refresh: bool = Query(default=False),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: ForgePermissions = Depends(get_forge_permissions),
) -> dict:
    # The secret is named from the tenant id through the frozen
    # `Tenant.secret_name`; a tenant with no Firestore document yet is named
    # the same way (routes/repositories.py `_tenant`).
    tenant = ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id, kind="group", principal=auth.tenant_principal or auth.email,
        created_at=ctx.now())

    def token() -> str:
        if ctx.forge_tokens is None:
            raise NoForgeCredential("swarm-api has no forge token reader")
        return ctx.forge_tokens.token_for(tenant)

    return service.read(tenant_id, repositories=lambda: _registered(ctx, tenant_id),
                        token=token, now=ctx.now(), is_admin=auth.is_admin, refresh=refresh,
                        defer_percent=forge_limits.defer_percent(ctx))
