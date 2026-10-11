"""Which GitHub permissions SwarmCloud needs, and which it holds: `GET
/v1/forge/permissions` (#1086, lane PERM-1; owner decision 2026-10-11,
design 2 "Org cards").

WHY IT EXISTS. On 2026-10-11 sagaxyz/ai-studio merges failed
`token_lacks_rights` because the App lacked Commit statuses: Read, and
nothing in the console said so until a merge had already failed. This read
answers, for the caller's tenant and before a run needs it:

  * THE CATALOGUE (`REQUIRED`): every repository permission SwarmCloud's
    runs use, the level they need, and in plain words what breaks without
    it -- the sentence the console shows next to a gap;
  * THE APP CARD: the App's own requested permissions (`GET /app`, with an
    App JWT) against the catalogue. A permission the App does not request
    cannot be granted by any org, so it is fixed on the App's permissions
    settings page -- a link sent to platform admins ONLY, and omitted from
    the response for anyone else rather than hidden by the console;
  * ONE ORG CARD per owner the tenant enabled through the App's
    installation (`forge_orgs`, `install_state: installed`, not a token),
    from `GET /app/installations`: the installation's held permissions
    against the catalogue. A permission the App requests and the
    installation does not hold is a request the org has not accepted; its
    fix is the installation's permission-update review page on GitHub;
  * A STATUS PER REGISTERED REPOSITORY: an installation-backed repository
    takes its org card's gaps (and, under an installation limited to
    selected repositories, one `GET /repos/{o}/{r}/installation` as the App
    says whether it is among them); a token-backed one reads what the token
    probe (`gittokens.probe_token`, stored in `git_token_checks`) measured
    for the token R2 resolves to there.

A FIX IS ALWAYS A LINK. GitHub has no API through which an App can widen
its own permissions or accept a request on an org's behalf: both are a
person on a GitHub settings page. So nothing here grants, accepts or
stores anything; every call is a read (`GET`), made as the App with a
JWT that never leaves `ForgeApp.as_app`, registered with the redaction
filter while it is in hand. The response holds names, levels, fixed reason
words and github.com URLs -- never a token, a JWT or a byte of GitHub's
body.

UNKNOWN IS NEVER OK. The App not configured, its key unreadable, GitHub not
answering, the time budget spent, a token never probed: each is `status:
unknown` with one of a fixed set of `reason` words, never a 500 and never
green. A permission is `ok` only when a read showed it held.

FRESHNESS. The console re-checks when its tab is shown again; a short
per-tenant cache (`CACHE_SECONDS`) answers those, and `?refresh=true` --
the console's Re-check, pressed after accepting on GitHub -- always reads
GitHub again.
"""

from __future__ import annotations

import logging
import re
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import utcnow

from .access import INSTALLED, requested, via_token
from .forgeapp import ORGS, AppUnavailable, ForgeApp, HttpAnswer, _Unanswered
from .gittokens import (
    CAPABILITIES,
    MISSING,
    OK,
    GitTokens,
    resolve_r2,
)
from .repositories import Repositories
from .validation import _ISSUE_OWNER

log = logging.getLogger(__name__)

__all__ = ["CACHE_SECONDS", "REQUIRED", "ForgePermissions", "gaps"]

# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------

#: What SwarmCloud's runs need on a repository, read from what they call.
#: Each entry's `breaks` is the sentence the console shows for a gap; keep it
#: a consequence a person recognises, not a permission name restated.
REQUIRED: tuple[Mapping[str, str], ...] = (
    {
        # Mandatory for every App; every REST read of a repository needs it.
        "permission": "metadata", "level": "read",
        "needed_for": "every read of the repository",
        "breaks": "SwarmCloud can't see the repository at all",
    },
    {
        # Clone is contents:read; a step pushing its branch, the merge (`PUT
        # /pulls/{n}/merge`) and its base update (`PUT .../update-branch`)
        # write contents (agent_worker/merge.py).
        "permission": "contents", "level": "write",
        "needed_for": "clone, push the step's branch, merge",
        "breaks": "steps can't push their branch, and merges can't land",
    },
    {
        # The worker opens and updates the task's pull request; the merge
        # step reads it and updates it onto the base.
        "permission": "pull_requests", "level": "write",
        "needed_for": "open and update the run's pull request",
        "breaks": "runs can't open or update their pull request",
    },
    {
        # merge.py reads `GET /commits/{sha}/check-runs` at the head to wait
        # for CI.
        "permission": "checks", "level": "read",
        "needed_for": "the merge step's wait for check runs",
        "breaks": "merges can't wait for checks",
    },
    {
        # merge.py reads `GET /commits/{sha}/status`, the combined commit
        # status. Missing on 2026-10-11: sagaxyz/ai-studio merges failed
        # token_lacks_rights on this read (#1086).
        "permission": "statuses", "level": "read",
        "needed_for": "the merge step's read of commit statuses",
        "breaks": "merges can't see CI, so they fail",
    },
    {
        # Issue runs comment on, and may close, their issue (#454, #646).
        "permission": "issues", "level": "write",
        "needed_for": "issue runs' comments and closing an issue",
        "breaks": "issue runs can't comment on their issue",
    },
)

#: GitHub's levels, in order. `admin` is answered for a few permissions and
#: covers write.
_RANK = {"read": 1, "write": 2, "admin": 3}

#: Which token-probe rows (`gittokens.CAPABILITIES`) measure which catalogue
#: entry. A permission is held by a token only when every row it maps to is
#: `ok`; `merge` and `workflow_dispatch` are not permissions and are left out.
TOKEN_READS: Mapping[str, tuple[str, ...]] = {
    "metadata": ("clone",),
    "contents": ("clone", "push"),
    "pull_requests": ("open_pull_requests",),
    "checks": ("read_checks",),
    "statuses": ("read_statuses",),
    "issues": ("read_issues", "close_issues"),
}

#: Where an org reviews and accepts the permissions the App asks for.
ORG_REVIEW_URL = ("https://github.com/organizations/{owner}/settings/installations/"
                  "{installation_id}/permissions/update")
USER_REVIEW_URL = "https://github.com/settings/installations/{installation_id}/permissions/update"
#: Where the App's owner changes what the App asks for. Admins only.
ORG_APP_URL = "https://github.com/organizations/{owner}/settings/apps/{slug}/permissions"
USER_APP_URL = "https://github.com/settings/apps/{slug}/permissions"
#: Where an org changes which repositories the installation covers.
ORG_INSTALLATION_URL = "https://github.com/organizations/{owner}/settings/installations/{installation_id}"
USER_INSTALLATION_URL = "https://github.com/settings/installations/{installation_id}"

#: How long a tenant's answer is reused when `refresh` is not asked for. The
#: console asks again every time its tab is shown; a minute keeps a person
#: flicking between tabs from spending GitHub's 5,000 App reads an hour,
#: and is short enough that nobody waits on a stale card -- Re-check
#: (`refresh=true`) skips it.
CACHE_SECONDS = 60.0
#: Tenants whose answer is held at once; past it the cache starts over.
MAX_CACHED_TENANTS = 500

#: Wall clock for one read, like the token probe's PROBE_BUDGET_SECONDS but
#: shorter: the console waits on it. Every GitHub call has its own 10 s
#: timeout (forgeapp.TIMEOUT_SECONDS); a call is not started past the budget.
BUDGET_SECONDS = 20.0
#: `GET /app/installations` pages (100 each). An App installed on more
#: accounts than this answers `installations_capped` for an org not found.
INSTALLATION_PAGE_SIZE = 100
MAX_INSTALLATION_PAGES = 5
#: Registered repositories read per tenant, and the per-repository App reads
#: (`GET /repos/{o}/{r}/installation`) one answer may make.
MAX_REPOSITORIES = 500
MAX_REPOSITORY_READS = 100
MAX_ORG_DOCS = 1000

OK_STATUS = "ok"
MISSING_STATUS = "missing"
UNKNOWN_STATUS = "unknown"

_OWNER = re.compile(rf"^{_ISSUE_OWNER}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


def _rank(level: Any) -> int:
    return _RANK.get(level, 0) if isinstance(level, str) else 0


def gaps(required: Iterable[Mapping[str, str]], held: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each entry of `required` that `held` (GitHub's `{permission: level}`)
    does not cover, write covering read. Pure: no network, no clock."""
    out = []
    for need in required:
        have = held.get(need["permission"])
        if _rank(have) < _rank(need["level"]):
            out.append({"permission": need["permission"], "level": need["level"],
                        "held": have if _rank(have) else None,
                        "needed_for": need["needed_for"], "breaks": need["breaks"]})
    return out


def _held(raw: Any) -> dict[str, str]:
    """GitHub's permissions object, levels only: anything else is dropped."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(k, str) and _rank(v)}


def _organization(owner_type: Any) -> bool:
    return owner_type != "User"


def review_url(owner: str, owner_type: Any, installation_id: int) -> str:
    if _organization(owner_type):
        return ORG_REVIEW_URL.format(owner=quote(owner, safe=""), installation_id=installation_id)
    return USER_REVIEW_URL.format(installation_id=installation_id)


def app_url(owner: str | None, owner_type: Any, slug: str) -> str:
    if owner and _organization(owner_type):
        return ORG_APP_URL.format(owner=quote(owner, safe=""), slug=quote(slug, safe=""))
    return USER_APP_URL.format(slug=quote(slug, safe=""))


def installation_url(owner: str, owner_type: Any, installation_id: int) -> str:
    if _organization(owner_type):
        return ORG_INSTALLATION_URL.format(owner=quote(owner, safe=""),
                                           installation_id=installation_id)
    return USER_INSTALLATION_URL.format(installation_id=installation_id)


# --------------------------------------------------------------------------
# what GitHub answered, as the App
# --------------------------------------------------------------------------

@dataclass
class _Installation:
    installation_id: int
    login: str
    owner_type: str
    held: dict[str, str]
    selection: str | None
    suspended: bool


@dataclass
class _AppView:
    """What the App's reads found. `reason` set means the App half is unknown."""

    reason: str | None = None
    slug: str | None = None
    owner: str | None = None
    owner_type: str | None = None
    requested: dict[str, str] = field(default_factory=dict)
    installations: dict[str, _Installation] = field(default_factory=dict)
    #: False when the listing stopped at MAX_INSTALLATION_PAGES.
    listing_complete: bool = True
    #: `(owner, repo)` lower-cased -> True (in the installation), False (not
    #: in it), or a reason word for a read that did not answer.
    repository_reads: dict[tuple[str, str], Any] = field(default_factory=dict)


def _installation(item: Any) -> _Installation | None:
    if not isinstance(item, dict):
        return None
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    login, inst_id = account.get("login"), item.get("id")
    if not isinstance(login, str) or not _OWNER.match(login) \
            or not isinstance(inst_id, int) or isinstance(inst_id, bool):
        return None
    selection = item.get("repository_selection")
    return _Installation(installation_id=inst_id, login=login,
                         owner_type="User" if account.get("type") == "User" else "Organization",
                         held=_held(item.get("permissions")),
                         selection=selection if selection in ("all", "selected") else None,
                         suspended=item.get("suspended_at") is not None)


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class ForgePermissions:
    """`GET /v1/forge/permissions` over one Firestore and the App."""

    def __init__(self, db: Any, forge_app: ForgeApp, *,
                 now: Callable[[], datetime] = utcnow,
                 clock: Callable[[], float] = _time.monotonic,
                 budget_seconds: float = BUDGET_SECONDS,
                 cache_seconds: float = CACHE_SECONDS) -> None:
        self._db = db
        self._app = forge_app
        self._now = now
        self._clock = clock
        self._budget = budget_seconds
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # -- the route's one entry ----------------------------------------------

    def read(self, tenant_id: str, *, is_admin: bool, refresh: bool = False) -> dict[str, Any]:
        """The tenant's cards and repository statuses. `refresh` reads GitHub
        again whatever the cache holds. The App's settings URL is in the
        answer only for an admin."""
        cached = False
        answer: dict[str, Any] | None = None
        if not refresh:
            with self._lock:
                hit = self._cache.get(tenant_id)
            if hit is not None and self._clock() - hit[0] < self._cache_seconds:
                answer, cached = hit[1], True
        if answer is None:
            answer = self._build(tenant_id)
            with self._lock:
                if len(self._cache) >= MAX_CACHED_TENANTS:
                    self._cache.clear()
                self._cache[tenant_id] = (self._clock(), answer)
        return _for_caller(answer, is_admin=is_admin, cached=cached)

    # -- one fresh read -------------------------------------------------------

    def _build(self, tenant_id: str) -> dict[str, Any]:
        started = self._clock()

        def out_of_time() -> bool:
            return self._clock() - started > self._budget

        orgs = self._enabled_orgs(tenant_id)
        token_owners = self._token_owners(tenant_id)
        repos = self._repositories(tenant_id)
        app_backed = [r for r in repos if r["owner"].lower() in orgs
                      and r["owner"].lower() not in token_owners]
        view = self._read_app(orgs, app_backed, out_of_time)

        app_card = self._app_card(view)
        org_cards = {owner: self._org_card(owner, doc, view)
                     for owner, doc in sorted(orgs.items())}
        token_view = _TokenView(self._db, tenant_id, self._now())
        rows = []
        for repo in repos:
            owner = repo["owner"].lower()
            if owner in org_cards and owner not in token_owners:
                row = self._app_repository(repo, org_cards[owner], view)
                org_cards[owner]["repositories"].append(repo["repo_id"])
            else:
                row = token_view.repository(repo)
            rows.append(row)
        answer = {
            "tenant_id": tenant_id,
            "checked_at": self._now().isoformat(),
            "required": [dict(need) for need in REQUIRED],
            "app": app_card,
            "orgs": list(org_cards.values()),
            "repositories": rows,
        }
        log.info("forge permissions tenant=%s app=%s reason=%s orgs=%d repositories=%d "
                 "missing=%d unknown=%d", tenant_id, app_card["status"], app_card["reason"],
                 len(org_cards), len(rows),
                 sum(1 for r in rows if r["status"] == MISSING_STATUS),
                 sum(1 for r in rows if r["status"] == UNKNOWN_STATUS))
        return answer

    # -- Firestore ------------------------------------------------------------

    def _org_docs(self, tenant_id: str) -> list[dict[str, Any]]:
        query = self._db.collection(ORGS).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)).limit(MAX_ORG_DOCS)
        docs = [snap.to_dict() or {} for snap in query.stream()]
        return [d for d in docs if d.get("tenant_id") == tenant_id]

    def _enabled_orgs(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        """Every owner any member of the tenant enabled through the App's
        installation: the tenant's runs reach its repositories through it,
        whoever enabled it. Keyed by the lower-cased owner."""
        found: dict[str, dict[str, Any]] = {}
        for doc in self._org_docs(tenant_id):
            if via_token(doc) or requested(doc) or doc.get("install_state") != INSTALLED:
                continue
            inst = doc.get("installation_id")
            owner = str(doc.get("owner_login") or doc.get("owner") or "")
            if not isinstance(inst, int) or isinstance(inst, bool) or not _OWNER.match(owner):
                continue
            found.setdefault(owner.lower(), doc)
        return found

    def _token_owners(self, tenant_id: str) -> set[str]:
        """Owners a member reaches through a fallback token (D5): their
        repositories run on that token, so they are token-backed here."""
        return {str(d.get("owner") or "").lower() for d in self._org_docs(tenant_id)
                if via_token(d)}

    def _repositories(self, tenant_id: str) -> list[dict[str, Any]]:
        rows, _ = Repositories(self._db, now=self._now).list(tenant_id, limit=MAX_REPOSITORIES)
        return [{"repo_id": r.get("repo_id"), "owner": str(r.get("owner") or ""),
                 "repo": str(r.get("repo") or "")} for r in rows
                if r.get("repo_id") and r.get("owner") and r.get("repo")]

    # -- GitHub, as the App ---------------------------------------------------

    def _read_app(self, orgs: Mapping[str, dict[str, Any]], app_backed: list[dict[str, Any]],
                  out_of_time: Callable[[], bool]) -> _AppView:
        view = _AppView()
        try:
            with self._app.as_app() as get:
                self._read_app_itself(view, get)
                if view.reason is None:
                    self._read_installations(view, get, out_of_time)
                if view.reason is None:
                    self._read_selected(view, get, app_backed, out_of_time)
        except AppUnavailable as unavailable:
            view.reason = unavailable.reason
        except _Unanswered:
            view.reason = "github_unreachable"
        return view

    @staticmethod
    def _read_app_itself(view: _AppView, get: Callable[[str], HttpAnswer]) -> None:
        answer = get("/app")
        data = answer.json()
        if answer.status == 401:
            view.reason = "key_rejected"
            return
        if answer.status != 200 or not isinstance(data, dict):
            view.reason = "github_refused"
            return
        slug = data.get("slug")
        view.slug = slug if isinstance(slug, str) and _SLUG.match(slug) else None
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        login = owner.get("login")
        view.owner = login if isinstance(login, str) and _OWNER.match(login) else None
        view.owner_type = "User" if owner.get("type") == "User" else "Organization"
        view.requested = _held(data.get("permissions"))

    @staticmethod
    def _read_installations(view: _AppView, get: Callable[[str], HttpAnswer],
                            out_of_time: Callable[[], bool]) -> None:
        for page in range(1, MAX_INSTALLATION_PAGES + 1):
            if out_of_time():
                view.reason = "out_of_time"
                return
            answer = get(f"/app/installations?per_page={INSTALLATION_PAGE_SIZE}&page={page}")
            data = answer.json()
            if answer.status != 200 or not isinstance(data, list):
                view.reason = "key_rejected" if answer.status == 401 else "github_refused"
                return
            for item in data:
                inst = _installation(item)
                if inst is not None:
                    view.installations.setdefault(inst.login.lower(), inst)
            if len(data) < INSTALLATION_PAGE_SIZE:
                return
        view.listing_complete = False

    @staticmethod
    def _read_selected(view: _AppView, get: Callable[[str], HttpAnswer],
                       app_backed: list[dict[str, Any]], out_of_time: Callable[[], bool]) -> None:
        """Under an installation limited to selected repositories, whether
        each registered repository is among them: `GET /repos/{o}/{r}/
        installation` as the App, 200 naming the installation or 404."""
        reads = 0
        for repo in app_backed:
            owner, name = repo["owner"].lower(), repo["repo"].lower()
            inst = view.installations.get(owner)
            if inst is None or inst.selection != "selected":
                continue
            if reads >= MAX_REPOSITORY_READS or out_of_time():
                view.repository_reads[(owner, name)] = "out_of_time"
                continue
            reads += 1
            try:
                answer = get(f"/repos/{quote(repo['owner'], safe='')}/"
                             f"{quote(repo['repo'], safe='')}/installation")
            except _Unanswered:
                view.repository_reads[(owner, name)] = "github_unreachable"
                continue
            data = answer.json()
            if answer.status == 200 and isinstance(data, dict):
                view.repository_reads[(owner, name)] = data.get("id") == inst.installation_id
            elif answer.status == 404:
                view.repository_reads[(owner, name)] = False
            else:
                view.repository_reads[(owner, name)] = "github_refused"

    # -- the cards ------------------------------------------------------------

    def _app_card(self, view: _AppView) -> dict[str, Any]:
        card: dict[str, Any] = {"slug": view.slug or (self._app.config.slug or None),
                                "owner": view.owner, "owner_type": view.owner_type,
                                "status": UNKNOWN_STATUS, "reason": view.reason, "gaps": []}
        if view.reason is not None:
            return card
        found = gaps(REQUIRED, view.requested)
        slug = card["slug"]
        url = app_url(view.owner, view.owner_type, slug) if slug else None
        card["gaps"] = [{**gap, "fix": "app", "fix_url": url} for gap in found]
        card["status"] = MISSING_STATUS if found else OK_STATUS
        card["permissions_url"] = url
        return card

    def _org_card(self, owner: str, doc: dict[str, Any], view: _AppView) -> dict[str, Any]:
        login = str(doc.get("owner_login") or doc.get("owner"))
        card: dict[str, Any] = {
            "owner": login, "owner_type": doc.get("owner_type"),
            "installation_id": doc.get("installation_id"),
            "repository_selection": doc.get("repository_selection"),
            "status": UNKNOWN_STATUS, "reason": view.reason, "gaps": [], "repositories": [],
        }
        if view.reason is not None:
            return card
        inst = view.installations.get(owner)
        if inst is None:
            card["reason"] = "app_not_installed" if view.listing_complete \
                else "installations_capped"
            card["status"] = MISSING_STATUS if view.listing_complete else UNKNOWN_STATUS
            return card
        card.update(owner=inst.login, owner_type=inst.owner_type,
                    installation_id=inst.installation_id, repository_selection=inst.selection)
        if inst.suspended:
            card.update(status=MISSING_STATUS, reason="installation_suspended",
                        installation_url=installation_url(inst.login, inst.owner_type,
                                                          inst.installation_id))
            return card
        slug = view.slug or self._app.config.slug
        found = []
        for gap in gaps(REQUIRED, inst.held):
            if _rank(view.requested.get(gap["permission"])) >= _rank(gap["level"]):
                # The App asks for it and the org has not accepted it.
                found.append({**gap, "fix": "accept",
                              "fix_url": review_url(inst.login, inst.owner_type,
                                                    inst.installation_id)})
            else:
                # Nobody can accept what the App does not ask for.
                found.append({**gap, "fix": "app",
                              "fix_url": app_url(view.owner, view.owner_type, slug)
                              if slug else None})
        card["gaps"] = found
        card["status"] = MISSING_STATUS if found else OK_STATUS
        return card

    @staticmethod
    def _app_repository(repo: dict[str, Any], card: dict[str, Any],
                        view: _AppView) -> dict[str, Any]:
        row = _repo_row(repo, "app")
        if card["status"] == UNKNOWN_STATUS:
            row["reason"] = card["reason"]
            return row
        if card["reason"] == "app_not_installed":
            row.update(status=MISSING_STATUS, reason="app_not_installed", problems=[{
                "permission": "installation", "level": None,
                "breaks": f"the App is not installed on {card['owner']}, so runs can't "
                          "reach this repository"}])
            return row
        if card["reason"] == "installation_suspended":
            row.update(status=MISSING_STATUS, reason="installation_suspended", problems=[{
                "permission": "installation", "level": None,
                "breaks": f"the App's installation on {card['owner']} is suspended, so runs "
                          "can't reach this repository"}])
            return row
        problems = [{"permission": g["permission"], "level": g["level"], "breaks": g["breaks"]}
                    for g in card["gaps"]]
        reach = view.repository_reads.get((repo["owner"].lower(), repo["repo"].lower()), True)
        if reach is False:
            problems.insert(0, {
                "permission": "repository_access", "level": None,
                "breaks": f"the App's installation on {card['owner']} does not include this "
                          "repository, so runs can't reach it"})
        if problems:
            row.update(status=MISSING_STATUS, problems=problems)
        elif reach is True:
            row["status"] = OK_STATUS
        else:
            row["reason"] = reach
        return row


def _repo_row(repo: dict[str, Any], source: str) -> dict[str, Any]:
    return {"repo_id": repo["repo_id"], "repository": f"{repo['owner']}/{repo['repo']}",
            "owner": repo["owner"], "source": source, "status": UNKNOWN_STATUS,
            "reason": None, "problems": []}


class _TokenView:
    """What the token probe measured for the token R2 resolves to on each
    token-backed repository. Reads Firestore only: the probe itself runs on
    registration, on "Verify now" and daily (gittokens §5.3)."""

    def __init__(self, db: Any, tenant_id: str, now: datetime) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._now = now
        self._records: list[Any] | None = None
        self._pairs: dict[tuple[str, str], dict[str, Any]] = {}

    def _load(self) -> list[Any]:
        if self._records is None:
            registry = GitTokens(self._db, now=lambda: self._now)
            self._records = registry.list(self._tenant_id)
            for doc in registry.pair_docs(self._tenant_id):
                self._pairs[(str(doc.get("token_id")), str(doc.get("repo_id")))] = doc
        return self._records

    def repository(self, repo: dict[str, Any]) -> dict[str, Any]:
        row = _repo_row(repo, "token")
        records = self._load()
        credential = resolve_r2(records, tenant_id=self._tenant_id, repo_id=repo["repo_id"],
                                user=None, now=self._now).credential
        if credential is None:
            row["reason"] = "no_token"
            return row
        row["token_id"] = credential.token_id
        pair = self._pairs.get((credential.token_id, repo["repo_id"]))
        if pair is None:
            row["reason"] = "not_probed"
            return row
        cells = pair.get("capabilities") if isinstance(pair.get("capabilities"), dict) else {}
        verified = pair.get("verified_at")
        row["probed_at"] = verified.isoformat() if isinstance(verified, datetime) else None
        problems, unread = [], []
        for need in REQUIRED:
            states = [((cells.get(cap) or {}).get("state")
                       if isinstance(cells.get(cap), dict) else None)
                      for cap in TOKEN_READS[need["permission"]] if cap in CAPABILITIES]
            if MISSING in states:
                problems.append({"permission": need["permission"], "level": need["level"],
                                 "breaks": need["breaks"]})
            elif not states or any(s != OK for s in states):
                unread.append(need["permission"])
        row["problems"] = problems
        row["unread"] = unread
        if problems:
            row["status"] = MISSING_STATUS
        elif unread:
            # A fine-grained token's pull-request grant is not readable, for
            # one: not measured is unknown, never ok.
            row["reason"] = "not_measured"
        else:
            row["status"] = OK_STATUS
        return row


def _for_caller(answer: dict[str, Any], *, is_admin: bool, cached: bool) -> dict[str, Any]:
    """The answer as this caller may see it: the App's settings URL is an
    admin's alone, so for anyone else it is taken out, not hidden."""
    out = dict(answer, cached=cached)
    app = dict(answer["app"])
    app["gaps"] = [dict(g) for g in app["gaps"]]
    orgs = [dict(card, gaps=[dict(g) for g in card["gaps"]],
                 repositories=list(card["repositories"])) for card in answer["orgs"]]
    if not is_admin:
        app.pop("permissions_url", None)
        for gap in app["gaps"]:
            gap.pop("fix_url", None)
        for card in orgs:
            for gap in card["gaps"]:
                if gap.get("fix") == "app":
                    gap.pop("fix_url", None)
    out["app"] = app
    out["orgs"] = orgs
    out["repositories"] = [dict(r) for r in answer["repositories"]]
    return out
