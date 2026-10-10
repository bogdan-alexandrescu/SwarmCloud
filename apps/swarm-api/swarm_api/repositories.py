"""Registered repositories: a per-tenant record (docs/repo-index.md §1, §6, lane RI1).

A repository was, until this module, a string on a task. A registration is the
smallest thing that can carry a schedule, a credential reference and a default
branch, so the repository index (lane RI2 onwards) has something to hang on.

WHAT A REGISTRATION IS, AND WHY EACH RULE:

  * PER TENANT. Tenant `eng` registering `example-org/example-api` says nothing
    about tenant `ops`, which gets its own record. `repo_id` hashes the tenant
    in, so the two never share a document, and every read compares the stored
    `tenant_id` with the caller's and answers a mismatch with the same 404 as a
    missing record -- the rule `issueruns.IssueRuns` keeps.
  * DETERMINISTIC ID. `repo_` + 16 hex of sha256(tenant + `github.com/` +
    lower-cased `owner/repo`): registering twice answers the existing record
    instead of making a second one, and an id cannot contain `/`.
  * ONE SPELLING WITH ISSUE RUNS. `owner` and `repo` pass `IssueRef`'s own
    patterns, and the derived `https://github.com/<owner>/<repo>` passes
    `check_repository_url`, so nothing the registry hands a task is a URL a
    task would have refused.
  * NO CREDENTIAL FIELD. The registration is read with the tenant's existing
    forge token, `swarm-tenant-<tenant>-git`, BY NAME. The record stores that
    name and what the read found (can it read, can it push); never the value.
    No request model here has a field a token could be sent in, and the token
    lives in one frame (`_read_with_token`) for the length of one read.
  * TOKEN RESOLUTION IS R2 (PICKS.md, 2026-10-05): a repository's own token,
    then the tenant's; a user's token is for attribution only. Repository
    tokens are git-tokens.md's registry (lanes GT1-GT5), which does not exist
    yet, so today R2 resolves to the tenant token every time, and the record
    says which scope it used (`access.token_scope`) so the console can show it.
  * THE CALLER'S OWN SLOT FIRST (docs/onboarding.md §3.2, #780 lane OB4).
    `register` and `readable` take a `CredentialSource`: the routes hand
    them `access.AccessService.credential_for`, which answers the caller's
    GitHub connection when they have an active one -- so a person registers
    and lists what THEY can reach -- and the tenant token otherwise, as
    before. A source yields a `HeldCredential` naming its scope and secret;
    the value lives inside the `with` and nowhere else. No source given is
    the tenant token, so every other caller is unchanged.
  * THE HARD STOPS' TWO FIELDS (docs/schedules.md §4.4, lane S11).
    `platform` marks a repository as the platform's own; its holds then need
    the owner. Only a platform admin may set or clear it -- clearing it would
    loosen the owner's holds -- and each change is an `admin_audit` entry
    written in the same transaction as the change, so the record and its
    audit cannot disagree. `hard_stop_paths` is the tenant's protected paths:
    a member may add patterns but never remove the four defaults, which are
    its floor, and what the hard stops read (`hard_stop_paths_of`) re-applies
    the floor rather than trusting a stored list to hold it. Nothing reads
    either field before lane S5.
  * `merge_policy` (WF-MERGE-API, 2026-10-10, part of #295): "off" or
    "on_merge_verdict", the default a workflow on this repository takes when
    its `metadata.merge` says nothing, and an issue run's default
    `auto_merge`. Never set, it defers to the platform's `merge_by_default`.
    Only a platform admin may set it -- it decides whether work merges on
    its own -- and each change is an `admin_audit` entry in the same
    transaction, as for `platform`.
  * OPT-IN CONTEXT, NOT A GATE. Nothing here changes what a task may run
    against; `allowed_profiles` narrows by NAME (invariant 10) and is not yet
    a submission check (repo-index.md §1, "How it relates to today").

Kept in its own collection, read and written here only -- not through
`store.py` or `codec.py` -- for the reason `issueruns` gives: the shape is not
the frozen contract's and must not leak into it.

INVARIANTS. A registration holds no capacity and creates no infrastructure
demand (invariant 1): it is a Firestore document. Index runs, which do, are
lane RI2's and go through the ordinary task path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Literal, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from .admins import AUDIT_COLLECTION
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .forge import (
    GIT_PROVIDER,
    PAGE_SIZE,
    ForgeReadError,
    ForgeTokens,
    GitHubIssues,
    IssueNoAccess,
    IssueNotFound,
    RepositoryRead,
    repository_from,
)
from .gittokens import GitTokenRecord, GitTokens, refusal_cause
from .validation import MERGE_POLICIES, _ISSUE_OWNER, _ISSUE_REPO, check_repository_url

log = logging.getLogger(__name__)

#: The collection, and the id prefix of its documents.
COLLECTION = "repositories"
REPO_ID_PREFIX = "repo_"
_REPO_ID = re.compile(r"^repo_[0-9a-f]{16}$")

#: The only forge in phase 1: the issue fetch, the preview and `read_open_work`
#: all read GitHub's API and nothing else. A second forge is a second client.
FORGE = "github"
FORGE_HOST = "github.com"

#: Who may work in a registered repository unless the tenant says otherwise:
#: the only enabled agent profile (repo-index.md §1).
DEFAULT_ALLOWED_PROFILES: tuple[str, ...] = ("claude-code",)

#: `GET /v1/repositories/readable` reads one GitHub page (forge.PAGE_SIZE) per
#: call and never past this page: at most 1,000 repositories. A token that
#: reads more is told `capped`, and the rest are registered by typing
#: `owner/repo`, which POST /v1/repositories takes either way. The console
#: renders a picker, not an inventory, and each page is one request with the
#: tenant's token the person is waiting on.
MAX_READABLE_PAGES = 10

# The index settings (repo-index.md §3.3): default and range, side by side.
INTERVAL_HOURS_DEFAULT = 24
INTERVAL_HOURS_RANGE = (1, 168)
#: `webhook` is phase 3 (RI8): no route verifies a push yet, so a registration
#: that asked for it would never be indexed on change. Refused until it exists.
ON_CHANGE_VALUES = ("poll", "off")
ON_CHANGE_DEFAULT = "poll"
MIN_CHANGE_INTERVAL_DEFAULT = 30
MIN_CHANGE_INTERVAL_RANGE = (10, 1440)
FULL_EVERY_DAYS_DEFAULT = 7
FULL_EVERY_DAYS_RANGE = (1, 30)

# The graph settings (repo-index.md §6.2, revised 2026-10-04). Stored with
# their defaults so lane RI9 reads a value rather than a gap; not patchable
# here, because nothing reads them until the graph exists.
GRAPH_DEPTH_DEFAULT = 3
GRAPH_MIN_CONFIDENCE_DEFAULT = 0.2

#: The protected paths every registration carries (docs/schedules.md §4.4),
#: and the floor of `hard_stop_paths`: a member may add patterns but cannot
#: remove these. Workflow files are refused at plan anyway (the forge
#: credential cannot push them); bootstrap and IAM files change who may do
#: what; CODEOWNERS changes who must review. Each is a change no automated
#: run should land unheld, so no tenant can opt out of holding it.
HARD_STOP_PATHS_DEFAULT: tuple[str, ...] = (
    ".github/workflows/**",
    "terraform/bootstrap/**",
    "**/iam*.tf",
    "CODEOWNERS",
)
#: Bounds on what a tenant may declare: enough for any real list, small
#: enough that matching every changed file against it at plan and at merge
#: (lane S5) stays cheap, and a pattern is a path, not a document.
HARD_STOP_PATHS_MAX = 64
HARD_STOP_PATH_MAX_CHARS = 256
#: A pattern relative to the repository root: no control character (a newline
#: would split one pattern into two in any log or comment that names it).
_HARD_STOP_PATH = re.compile(r"^[^\x00-\x1f\x7f]+$")

#: `admin_audit` actions for a change of `platform`.
AUDIT_PLATFORM_SET = "repository_platform_set"
AUDIT_PLATFORM_CLEARED = "repository_platform_cleared"
#: The `admin_audit` action for a change of `merge_policy`.
AUDIT_MERGE_POLICY_SET = "repository_merge_policy_set"

#: A branch name, conservatively: git's own ref rules (`git check-ref-format`)
#: narrowed to the characters a GitHub branch name is seen with. The value is
#: watched by the poll and handed to index runs as a ref, so it must never be
#: something a shell or a URL would read as more than one name.
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


class RepositoryNoAccess(ForgeReadError):
    """The tenant's token cannot read the repository. Names the secret, never its value."""

    status_code = 403
    code = "no_access"


# --------------------------------------------------------------------------
# which credential reads the forge (#780, lane OB4)
# --------------------------------------------------------------------------

#: `access.token_scope` values: the tenant's `-git` slot, or the caller's own
#: GitHub connection (their `git-u-<hex>` slot).
SCOPE_TENANT = "tenant"
SCOPE_USER = "user"


@dataclass(frozen=True)
class HeldCredential:
    """A forge credential in hand for one read: its scope and secret NAME,
    and the value, which no repr, log line or response carries."""

    scope: str
    secret_name: str
    value: str = field(repr=False)
    #: The GitHub login a user credential acts as; None for the tenant's.
    login: str | None = None


#: `() -> with ... as HeldCredential`: opened once per read, closed after it.
CredentialSource = Callable[[], AbstractContextManager[HeldCredential]]


def tenant_credential(tenant: Tenant, tokens: ForgeTokens) -> CredentialSource:
    """The tenant's `swarm-tenant-<t>-git`, read by name: today's credential."""

    @contextmanager
    def held() -> Iterator[HeldCredential]:
        value = tokens.token_for(tenant)
        try:
            yield HeldCredential(SCOPE_TENANT, tenant.secret_name(GIT_PROVIDER), value)
        finally:
            value = ""

    return held


# --------------------------------------------------------------------------
# the rules: a repository name, a branch, a profile list
# --------------------------------------------------------------------------

_OWNER_REPO = re.compile(rf"^({_ISSUE_OWNER})/({_ISSUE_REPO})$")


def parse_repository(value: str) -> tuple[str, str]:
    """`owner/repo` -> (owner, repo), by `IssueRef`'s own patterns.

    Raises ValueError. The value is never echoed: it is a caller's string.
    """
    matched = _OWNER_REPO.match(value or "")
    if matched is None:
        raise ValueError("repository must be owner/repo, as GitHub spells it")
    owner, repo = matched.group(1), matched.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if repo in ("", ".", "..") or repo.startswith("."):
        raise ValueError("repository must be owner/repo, as GitHub spells it")
    check_repository_url(repository_url(owner, repo))
    return owner, repo


def repository_url(owner: str, repo: str) -> str:
    return f"https://{FORGE_HOST}/{owner}/{repo}"


def repo_id_for(tenant_id: str, owner: str, repo: str) -> str:
    # The documented recipe: tenant + `github.com/` + lower-cased owner/repo,
    # so `Saga/Widgets` and `saga/widgets` are one registration.
    key = f"{tenant_id}{FORGE_HOST}/{f'{owner}/{repo}'.lower()}"
    return REPO_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def check_branch(value: str) -> str:
    if (
        not _BRANCH.match(value)
        or value.startswith(("-", "/", "."))
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "//" in value
        or "/." in value
    ):
        raise ValueError("default_branch must be a branch name (git check-ref-format)")
    return value


def check_profiles(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("allowed_profiles must name at least one runner profile")
    if len(set(value)) != len(value):
        raise ValueError("allowed_profiles names a runner profile twice")
    unknown = [name for name in value if name not in RUNNER_PROFILES]
    if unknown:
        raise ValueError(
            "allowed_profiles names runner profiles by their catalogue names; known: "
            + ", ".join(sorted(RUNNER_PROFILES))
        )
    return value


def check_hard_stop_paths(value: list[str]) -> list[str]:
    """`value` as stored: each pattern checked, repeats dropped, the four
    defaults required. A missing default is named, so the 422 says which."""
    for pattern in value:
        shown = pattern[:64]
        if not 1 <= len(pattern) <= HARD_STOP_PATH_MAX_CHARS:
            raise ValueError(
                f"hard_stop_paths entry {shown!r} must be 1-{HARD_STOP_PATH_MAX_CHARS} characters")
        if pattern != pattern.strip() or not _HARD_STOP_PATH.match(pattern):
            raise ValueError(
                f"hard_stop_paths entry {shown!r} has surrounding space or a control character")
        if pattern.startswith("/"):
            raise ValueError(
                f"hard_stop_paths entry {shown!r} must be relative to the repository root")
    paths = list(dict.fromkeys(value))
    missing = [default for default in HARD_STOP_PATHS_DEFAULT if default not in paths]
    if missing:
        raise ValueError(
            "hard_stop_paths cannot remove the defaults, which are its floor; missing: "
            + ", ".join(missing))
    return paths


def hard_stop_paths_of(data: Mapping[str, Any]) -> list[str]:
    """The protected paths a registration's hard stops read: the stored list,
    then any default it lacks. A registration stored before lane S11 has none
    and reads as the four defaults; a stored list is never trusted to hold
    the floor on its own."""
    stored = data.get("hard_stop_paths")
    paths = [p for p in stored if isinstance(p, str)] if isinstance(stored, list) else []
    return list(dict.fromkeys([*paths, *HARD_STOP_PATHS_DEFAULT]))


def platform_of(data: Mapping[str, Any]) -> bool:
    """Whether a registration is the platform's own. Only a stored `True` is:
    a missing or malformed value is the default, false."""
    return data.get("platform") is True


def merge_policy_of(data: Mapping[str, Any]) -> str | None:
    """A registration's `merge_policy`, or None when it set none. A stored
    value that is not one of `MERGE_POLICIES` reads as none: the platform
    default then decides, never a value this code does not know."""
    value = data.get("merge_policy")
    return value if value in MERGE_POLICIES else None


def registered_merge_policy(
    db: Any, now: Callable[[], datetime], tenant_id: str, owner: str, repo: str,
) -> str | None:
    """The `merge_policy` `tenant_id` registered `owner/repo` with, or None.

    The tenant's own registration only (`Repositories.find`), so another
    tenant's policy on the same repository never decides this tenant's
    default."""
    record = Repositories(db, now=now).find(tenant_id, repo_id_for(tenant_id, owner, repo))
    return merge_policy_of(record or {})


def _merge_policy_audit(
    txn: Any, db: Any, data: Mapping[str, Any], *, to: str, by: str, at: datetime,
) -> None:
    """One `admin_audit` entry for a change of `merge_policy`, inside `txn`."""
    ref = db.collection(AUDIT_COLLECTION).document()
    txn.set(ref, {
        "action": AUDIT_MERGE_POLICY_SET,
        "target_repo_id": data.get("repo_id"),
        "by": by,
        "at": at,
        "detail": {
            "tenant_id": data.get("tenant_id"),
            "repository": f"{data.get('owner')}/{data.get('repo')}",
            "merge_policy": {"from": merge_policy_of(data), "to": to},
        },
    })


def _platform_audit(
    txn: Any, db: Any, data: Mapping[str, Any], *, to: bool, by: str, at: datetime,
) -> None:
    """One `admin_audit` entry for a change of `platform`, inside `txn`, so the
    change and its audit commit together or not at all. Created, never
    updated (admins.py: the audit is append-only)."""
    ref = db.collection(AUDIT_COLLECTION).document()
    txn.set(ref, {
        "action": AUDIT_PLATFORM_SET if to else AUDIT_PLATFORM_CLEARED,
        "target_repo_id": data.get("repo_id"),
        "by": by,
        "at": at,
        "detail": {
            "tenant_id": data.get("tenant_id"),
            "repository": f"{data.get('owner')}/{data.get('repo')}",
            "platform": {"from": platform_of(data), "to": to},
        },
    })


# --------------------------------------------------------------------------
# the request bodies: no field a token, an image or a command could go in
# --------------------------------------------------------------------------

class _Body(BaseModel):
    # extra="forbid": a caller's `image`, `command` or `tenant_id` is refused,
    # not ignored (invariant 10; the tenant is the caller's, never the body's).
    model_config = ConfigDict(extra="forbid")


class IndexSettings(_Body):
    """The schedule and trigger of repo-index.md §3.3. The index POINTER fields
    (`current_sha`, `head_sha`, ...) are written by index runs, never a caller."""

    interval_hours: StrictInt | Literal["off"] | None = None
    on_change: Literal["poll", "off"] | None = None
    min_change_interval_minutes: StrictInt | None = Field(
        default=None, ge=MIN_CHANGE_INTERVAL_RANGE[0], le=MIN_CHANGE_INTERVAL_RANGE[1]
    )
    full_every_days: StrictInt | None = Field(
        default=None, ge=FULL_EVERY_DAYS_RANGE[0], le=FULL_EVERY_DAYS_RANGE[1]
    )
    paused: StrictBool | None = None

    @field_validator("interval_hours")
    @classmethod
    def _interval(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, int):
            low, high = INTERVAL_HOURS_RANGE
            if not low <= value <= high:
                raise ValueError(f"interval_hours must be {low}-{high}, or \"off\"")
        return value

    def changes(self) -> dict[str, Any]:
        return {name: value for name, value in self.model_dump().items() if value is not None}


class RepositoryCreate(_Body):
    repository: str = Field(min_length=3, max_length=141)
    forge: Literal["github"] = FORGE
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_profiles: list[str] | None = Field(default=None, max_length=len(RUNNER_PROFILES))
    index: IndexSettings | None = None
    #: A platform admin's only (the route refuses a member's with a 403).
    platform: StrictBool | None = None
    hard_stop_paths: list[str] | None = Field(default=None, max_length=HARD_STOP_PATHS_MAX)
    #: A platform admin's only, as `platform` is. Absent: the platform default.
    merge_policy: Literal["off", "on_merge_verdict"] | None = None

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        parse_repository(value)
        return value

    @field_validator("default_branch")
    @classmethod
    def _branch(cls, value: str | None) -> str | None:
        return None if value is None else check_branch(value)

    @field_validator("allowed_profiles")
    @classmethod
    def _profiles(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else check_profiles(value)

    @field_validator("hard_stop_paths")
    @classmethod
    def _hard_stops(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else check_hard_stop_paths(value)


class RepositoryPatch(_Body):
    """What repo-index.md §6.1 lets a PATCH change: schedule, trigger,
    `allowed_profiles`, `default_branch`, `paused`; and the hard stops'
    `platform` (a platform admin's only) and `hard_stop_paths`
    (docs/schedules.md §4.4); and `merge_policy` (a platform admin's only).
    Never the repository, the tenant or anything an index run writes."""

    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_profiles: list[str] | None = Field(default=None, max_length=len(RUNNER_PROFILES))
    paused: StrictBool | None = None
    index: IndexSettings | None = None
    platform: StrictBool | None = None
    hard_stop_paths: list[str] | None = Field(default=None, max_length=HARD_STOP_PATHS_MAX)
    merge_policy: Literal["off", "on_merge_verdict"] | None = None

    @field_validator("default_branch")
    @classmethod
    def _branch(cls, value: str | None) -> str | None:
        return None if value is None else check_branch(value)

    @field_validator("allowed_profiles")
    @classmethod
    def _profiles(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else check_profiles(value)

    @field_validator("hard_stop_paths")
    @classmethod
    def _hard_stops(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else check_hard_stop_paths(value)

    @model_validator(mode="after")
    def _changes_something(self) -> "RepositoryPatch":
        top = (self.default_branch, self.allowed_profiles, self.paused, self.platform,
               self.hard_stop_paths, self.merge_policy)
        if all(value is None for value in top) and (
            self.index is None or not self.index.changes()
        ):
            raise ValueError("a PATCH must change at least one setting")
        return self


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------

def _index_defaults() -> dict[str, Any]:
    return {
        "interval_hours": INTERVAL_HOURS_DEFAULT,
        "on_change": ON_CHANGE_DEFAULT,
        "min_change_interval_minutes": MIN_CHANGE_INTERVAL_DEFAULT,
        "full_every_days": FULL_EVERY_DAYS_DEFAULT,
        "paused": False,
        # Written by index runs and the poll (RI2, RI4); None until then.
        "current_sha": None,
        "current_digest": None,
        "last_indexed_at": None,
        "last_kind": None,
        "head_sha": None,
        "head_read_at": None,
        "behind_by": None,
        "etag": None,
        "pending_sha": None,
        "in_flight_task_id": None,
        "coverage": None,
    }


def _iso(moment: Any) -> Any:
    return moment.isoformat() if isinstance(moment, datetime) else moment


def to_api(data: Mapping[str, Any]) -> dict[str, Any]:
    """A stored registration as the API serves it. Every field is a name, a
    setting or a reading -- the stored document has no credential to omit."""
    access = dict(data.get("access") or {})
    if "read_at" in access:
        access["read_at"] = _iso(access["read_at"])
    index = dict(data.get("index") or {})
    for key in ("last_indexed_at", "head_read_at"):
        if key in index:
            index[key] = _iso(index[key])
    return {
        "repo_id": data.get("repo_id"),
        "tenant_id": data.get("tenant_id"),
        "forge": data.get("forge"),
        "owner": data.get("owner"),
        "repo": data.get("repo"),
        "repository": f"{data.get('owner')}/{data.get('repo')}",
        "repository_url": data.get("repository_url"),
        "default_branch": data.get("default_branch"),
        "default_branch_source": data.get("default_branch_source"),
        "visibility": data.get("visibility"),
        "archived": data.get("archived"),
        "allowed_profiles": list(data.get("allowed_profiles") or []),
        "access": access,
        "index": index,
        "graph": dict(data.get("graph") or {}),
        "selection_policy": dict(data.get("selection_policy") or {}),
        "platform": platform_of(data),
        "hard_stop_paths": hard_stop_paths_of(data),
        # Served so the console marks the floor without restating it.
        "hard_stop_paths_floor": list(HARD_STOP_PATHS_DEFAULT),
        # None: the platform's `merge_by_default` decides.
        "merge_policy": merge_policy_of(data),
        "created_by": data.get("created_by"),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


def _encode_cursor(repo_id: str) -> str:
    return base64.urlsafe_b64encode(repo_id.encode("ascii")).decode("ascii")


def _decode_cursor(token: str | None) -> str | None:
    if not token:
        return None
    try:
        repo_id = base64.urlsafe_b64decode(token.encode("ascii")).decode("ascii")
    except (ValueError, binascii.Error, UnicodeError):
        repo_id = ""
    if not _REPO_ID.match(repo_id):
        raise ValidationFailed("page_token is not a token this route issued")
    return repo_id


class Repositories:
    """The `repositories` collection. Every read is checked against the caller's tenant."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, repo_id: str) -> Any:
        return self._db.collection(COLLECTION).document(repo_id)

    @staticmethod
    def not_found(repo_id: str) -> NotFound:
        # One sentence for "no such registration" and "another tenant's".
        return NotFound(f"repository {repo_id!r} not found")

    def find(self, tenant_id: str, repo_id: str) -> dict[str, Any] | None:
        if not _REPO_ID.match(repo_id or ""):
            return None
        snap = self._ref(repo_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict()
        if data.get("tenant_id") != tenant_id:
            return None
        return data

    def token_evidence(self, tenant_id: str) -> GitTokenRecord | None:
        """The tenant token's record, for what its last probe read of the
        token's reach (#780): a refused registration names its likely cause
        from it. None when there is no record, or it cannot be read now --
        the refusal still stands, it just names no cause beyond the plain one."""
        try:
            return GitTokens(self._db, now=self._now).tenant_default(tenant_id)
        except Exception as exc:
            log.warning("repository register tenant=%s token evidence unreadable (%s)",
                        tenant_id, type(exc).__name__)
            return None

    def get(self, tenant_id: str, repo_id: str) -> dict[str, Any]:
        data = self.find(tenant_id, repo_id)
        if data is None:
            # The id is the caller's text: quoted by repr, bounded.
            raise self.not_found((repo_id or "")[:64])
        return data

    def create(
        self, record: dict[str, Any], *, platform_by: str | None = None,
        merge_policy_by: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Store `record` unless its id exists. (stored, created) -- in one
        transaction, so two registrations racing store one document.

        A record marked `platform` is stored only with the admin who marked
        it (`platform_by`, the route has checked the role), and its
        `admin_audit` entry is written in the same transaction."""
        if platform_of(record) and not platform_by:
            raise ValueError("a platform registration needs the admin who marked it, for the audit")
        if merge_policy_of(record) is not None and not merge_policy_by:
            raise ValueError("a registration with a merge_policy needs the admin who set it, for the audit")
        ref = self._ref(record["repo_id"])
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[dict[str, Any], bool]:
            snap = _snapshot(txn.get(ref))
            if snap.exists:
                existing = snap.to_dict()
                if existing.get("tenant_id") != record["tenant_id"]:
                    # sha256 of a key that contains the tenant: not reachable
                    # short of a collision, and never answered with the record.
                    raise Conflict("repository id collision; registration refused")
                return existing, False
            txn.set(ref, record)
            if platform_of(record) and platform_by:
                _platform_audit(txn, self._db, {**record, "platform": False}, to=True,
                                by=platform_by, at=self._now())
            policy = merge_policy_of(record)
            if policy is not None and merge_policy_by:
                _merge_policy_audit(txn, self._db, {**record, "merge_policy": None}, to=policy,
                                    by=merge_policy_by, at=self._now())
            return record, True

        return _apply(transaction)

    def list(
        self, tenant_id: str, *, limit: int, page_token: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """The tenant's registrations, by `repo_id`.

        Ordered by the document id because an equality filter ordered by
        `__name__` is served by Firestore's automatic single-field index: the
        list needs no composite index, so it cannot fail in an environment
        whose index set has not caught up. The console sorts its cards itself.
        """
        query = self._db.collection(COLLECTION).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        ).order_by("__name__")
        after = _decode_cursor(page_token)
        if after is not None:
            query = query.start_after({"__name__": after})
        rows = [snap.to_dict() for snap in query.limit(limit + 1).stream()]
        # The filter again, in the application, as `IssueRuns.list` does.
        rows = [row for row in rows if row.get("tenant_id") == tenant_id]
        next_token = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_token = _encode_cursor(rows[-1]["repo_id"])
        return rows, next_token

    def registered(self, tenant_id: str, repo_ids: list[str]) -> set[str]:
        """Which of `repo_ids` the tenant has registered: one batched read."""
        if not repo_ids:
            return set()
        refs = [self._ref(repo_id) for repo_id in dict.fromkeys(repo_ids)]
        found: set[str] = set()
        for snap in self._db.get_all(refs):
            if snap.exists and (snap.to_dict() or {}).get("tenant_id") == tenant_id:
                found.add(snap.id)
        return found

    def patch(
        self, tenant_id: str, repo_id: str, body: RepositoryPatch, *,
        platform_by: str | None = None, merge_policy_by: str | None = None,
    ) -> dict[str, Any]:
        """The settings a PATCH may change, in one transaction, tenant re-checked.

        `platform` changes only with `platform_by`, the admin the route
        checked, and its `admin_audit` entry is written in this transaction."""
        if body.platform is not None and not platform_by:
            raise Forbidden("only a platform admin may set or clear platform")
        if body.merge_policy is not None and not merge_policy_by:
            raise Forbidden("only a platform admin may set merge_policy")
        if not _REPO_ID.match(repo_id or ""):
            raise self.not_found((repo_id or "")[:64])
        ref = self._ref(repo_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != tenant_id:
                raise self.not_found(repo_id)
            index = dict(data.get("index") or {})
            if body.index is not None:
                index.update(body.index.changes())
            if body.paused is not None:
                index["paused"] = body.paused
            changes: dict[str, Any] = {"index": index, "updated_at": self._now()}
            if body.default_branch is not None:
                changes["default_branch"] = body.default_branch
                changes["default_branch_source"] = "override"
            if body.allowed_profiles is not None:
                changes["allowed_profiles"] = list(body.allowed_profiles)
            if body.hard_stop_paths is not None:
                changes["hard_stop_paths"] = list(body.hard_stop_paths)
            if body.platform is not None and body.platform != platform_of(data):
                changes["platform"] = body.platform
                _platform_audit(txn, self._db, data, to=body.platform,
                                by=str(platform_by), at=changes["updated_at"])
            if body.merge_policy is not None and body.merge_policy != merge_policy_of(data):
                changes["merge_policy"] = body.merge_policy
                _merge_policy_audit(txn, self._db, data, to=body.merge_policy,
                                    by=str(merge_policy_by), at=changes["updated_at"])
            txn.update(ref, changes)
            merged = dict(data)
            merged.update(changes)
            return merged

        return _apply(transaction)

    def delete(self, tenant_id: str, repo_id: str) -> None:
        """Unregister. Index objects are left to the artifact lifecycle (§6.1)."""
        if not _REPO_ID.match(repo_id or ""):
            raise self.not_found((repo_id or "")[:64])
        ref = self._ref(repo_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != tenant_id:
                raise self.not_found(repo_id)
            txn.delete(ref)

        _apply(transaction)


# --------------------------------------------------------------------------
# the forge reads, with the tenant's token
# --------------------------------------------------------------------------

def _no_access(secret_name: str, what: str) -> RepositoryNoAccess:
    return RepositoryNoAccess(
        f"the tenant's forge credential {secret_name} cannot read {what}: grant it read "
        "access to the repository, or store a token that has it with "
        "scripts/create-secrets.sh --stdin",
        detail={"secret_name": secret_name},
    )


#: §2.3's REPO_NOT_INSTALLED, the cause a user credential's refusal names:
#: a GitHub App user token reads only what the user can see AND an
#: installation covers, so a 404 is one or the other.
_USER_CANNOT_SEE = "REPO_NOT_INSTALLED"


def _user_refused(held: HeldCredential, what: str) -> RepositoryNoAccess:
    """A user credential's refusal: the person's own connection cannot read
    `what`. Names the login and the slot, never a value."""
    login = held.login or "your GitHub account"
    return RepositoryNoAccess(
        f"your GitHub connection as {login} cannot read {what}: SwarmCloud's GitHub App is "
        f"not installed for it, or {login} cannot see it. Add it to the installation (an "
        "org owner may have to), or type a repository you can read",
        detail={"secret_name": held.secret_name, "token_scope": SCOPE_USER,
                "cause": _USER_CANNOT_SEE, "org": None, "evidence_at": None},
    )


def _refused(secret_name: str, owner: str, repo: str,
             evidence: Callable[[], GitTokenRecord | None] | None) -> RepositoryNoAccess:
    """A 404/403 on `owner/repo`, naming its likely cause (docs/onboarding.md
    §2.3; #780): SSO not authorised for the org, the org refusing classic
    tokens, or the token's account not seeing the repository -- from what the
    token's last probe read (`gittokens.refusal_cause`). Additive: the code
    stays `no_access` and `detail` gains `cause`, `org` and `evidence_at`."""
    record = evidence() if evidence is not None else None
    cause = refusal_cause(owner, repo, record)
    message = f"the tenant's forge credential {secret_name} cannot read {owner}/{repo}. "
    message += cause["message"]
    if cause["code"] == "ACCOUNT_CANNOT_SEE":
        message += (" Grant it read access to the repository, or store a token that has it "
                    "with scripts/create-secrets.sh --stdin")
    return RepositoryNoAccess(message, detail={
        "secret_name": secret_name,
        "cause": cause["code"],
        "org": cause["org"],
        "evidence_at": cause["evidence_at"],
    })


def read_repository(
    owner: str, repo: str, tenant: Tenant, *, tokens: ForgeTokens, forge: GitHubIssues,
    evidence: Callable[[], GitTokenRecord | None] | None = None,
    credential: CredentialSource | None = None,
) -> RepositoryRead:
    return read_repository_as(owner, repo, tenant, tokens=tokens, forge=forge,
                              evidence=evidence, credential=credential)[0]


def read_repository_as(
    owner: str, repo: str, tenant: Tenant, *, tokens: ForgeTokens, forge: GitHubIssues,
    evidence: Callable[[], GitTokenRecord | None] | None = None,
    credential: CredentialSource | None = None,
) -> tuple[RepositoryRead, str, str]:
    """The registration's one forge read: `GET /repos/{owner}/{repo}`.

    A 404 is `no_access`, not `not_found`: GitHub answers 404 for a private
    repository the token cannot see, so all a 404 proves is that this token
    cannot read it -- which is the refusal registration makes. A 404 or 403
    names its likely cause from `evidence`, the tenant token's record, read
    only on a refusal. A user credential's refusal names REPO_NOT_INSTALLED.

    (read, token_scope, secret_name): which credential answered, by name.
    """
    what = f"{owner}/{repo}"
    source = credential or tenant_credential(tenant, tokens)
    with source() as held:
        try:
            read = forge.repository(owner, repo, held.value)
        except (IssueNotFound, IssueNoAccess):
            if held.scope == SCOPE_USER:
                raise _user_refused(held, what) from None
            raise _refused(held.secret_name, owner, repo, evidence) from None
        if not read.can_read:
            if held.scope == SCOPE_USER:
                raise _user_refused(held, what)
            raise _no_access(held.secret_name, what)
        return read, held.scope, held.secret_name


def register(
    body: RepositoryCreate,
    tenant: Tenant,
    *,
    created_by: str,
    store: Repositories,
    tokens: ForgeTokens,
    forge: GitHubIssues,
    now: Callable[[], datetime],
    credential: CredentialSource | None = None,
    platform_by: str | None = None,
    merge_policy_by: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Register `body.repository` for `tenant`. (record, created).

    An existing registration is answered as it is, without reading the forge:
    idempotent on `repo_id`; a change is a PATCH. The read is `credential`'s
    -- the caller's own connection when the route resolved one -- and the
    record names which scope and secret answered.
    """
    owner, repo = parse_repository(body.repository)
    repo_id = repo_id_for(tenant.tenant_id, owner, repo)
    existing = store.find(tenant.tenant_id, repo_id)
    if existing is not None:
        return existing, False
    read, scope, secret_name = read_repository_as(
        owner, repo, tenant, tokens=tokens, forge=forge, credential=credential,
        evidence=lambda: store.token_evidence(tenant.tenant_id))
    record = registration_record(body, tenant, read, repo_id=repo_id, created_by=created_by,
                                 at=now(), token_scope=scope, secret_name=secret_name)
    return store.create(record, platform_by=platform_by, merge_policy_by=merge_policy_by)


def registration_record(
    body: RepositoryCreate, tenant: Tenant, read: RepositoryRead, *, repo_id: str,
    created_by: str, at: datetime, token_scope: str, secret_name: str,
    registered_via: str | None = None,
) -> dict[str, Any]:
    """The registration document for `read`, GitHub's description of the
    repository. Shared by `register` and a grant's first registration
    (`access.AccessService.grant`), so both store one shape."""
    # GitHub's own spelling of the name; equal to the request's but for case.
    owner, repo = read.owner, read.repo
    try:
        parse_repository(f"{owner}/{repo}")
        branch = body.default_branch or check_branch(read.default_branch)
    except ValueError:
        raise ValidationFailed(
            f"GitHub describes {owner!r}/{repo!r} with a name or default branch this "
            "platform does not accept; register it with an explicit default_branch"
        ) from None
    index = _index_defaults()
    if body.index is not None:
        index.update(body.index.changes())
    record: dict[str, Any] = {
        "repo_id": repo_id,
        "tenant_id": tenant.tenant_id,
        "forge": FORGE,
        "owner": owner,
        "repo": repo,
        "repository_url": check_repository_url(repository_url(owner, repo)),
        "default_branch": branch,
        "default_branch_source": "override" if body.default_branch else "forge",
        "visibility": read.visibility,
        "archived": read.archived,
        "allowed_profiles": list(body.allowed_profiles or DEFAULT_ALLOWED_PROFILES),
        # WHICH token answered, by name, and what it could do. Never its value.
        "access": {
            "token_scope": token_scope,
            "secret_name": secret_name,
            "can_read": read.can_read,
            "can_push": read.can_push,
            "read_at": at,
        },
        "index": index,
        "graph": {
            "depth": GRAPH_DEPTH_DEFAULT,
            "min_confidence": GRAPH_MIN_CONFIDENCE_DEFAULT,
            "languages_enabled": None,
        },
        # Inherited from the tenant (repo-index.md §4.4). The picked policy,
        # P3 with X2, is not enabled here: §2.5 and RI14 put request B and the
        # carved `graph/` prefix before any merge gate reads the graph.
        "selection_policy": {"policy": None, "mode": None, "inherited_from_tenant": True},
        # docs/schedules.md §4.4. `platform` is true only when an admin's
        # request said so (the route checked the role, `create` audits it).
        "platform": body.platform is True,
        "hard_stop_paths": list(
            HARD_STOP_PATHS_DEFAULT if body.hard_stop_paths is None else body.hard_stop_paths),
        # None defers to the platform's `merge_by_default`; set only when an
        # admin's request said so (the route checked the role, `create` audits).
        "merge_policy": body.merge_policy,
        "created_by": created_by,
        "created_at": at,
        "updated_at": at,
    }
    if registered_via is not None:
        # `grant`: made by a person's first grant, so the last grant's
        # removal may unregister it (§2.4). A registration made any other
        # way is never deleted by a grant's removal.
        record["registered_via"] = registered_via
    return record


def readable(
    tenant: Tenant,
    page: int,
    *,
    store: Repositories,
    tokens: ForgeTokens,
    forge: GitHubIssues,
    credential: CredentialSource | None = None,
) -> dict[str, Any]:
    """One page of what the tenant's token can read, for Register C -- or,
    when the route resolved one, what the caller's own connection can read.

    PAGING. `page` is GitHub's page of `GET /user/repos`, PAGE_SIZE entries in
    full-name order, 1 to MAX_READABLE_PAGES; one forge request per call.
    `next_page` is the next page while this one was full and the cap is not
    reached. `capped` says the cap was reached with a full page: there may be
    more than the picker can list, and they are registered by name.

    Each entry says whether the tenant has registered it already; an entry
    whose name this platform would not accept is skipped and counted, never
    served half-checked.
    """
    if page < 1 or page > MAX_READABLE_PAGES:
        raise ValidationFailed(f"page must be 1-{MAX_READABLE_PAGES}")
    source = credential or tenant_credential(tenant, tokens)
    with source() as held:
        scope, secret_name = held.scope, held.secret_name
        try:
            listed = forge.readable_page(held.value, page)
        except (IssueNotFound, IssueNoAccess):
            if scope == SCOPE_USER:
                raise _user_refused(held, "the repositories it was granted") from None
            raise _no_access(secret_name, "the repositories it was granted") from None
    entries: list[dict[str, Any]] = []
    skipped = 0
    for raw in listed.entries:
        read = repository_from(raw)
        if read is None:
            skipped += 1
            continue
        try:
            owner, repo = parse_repository(f"{read.owner}/{read.repo}")
        except ValueError:
            skipped += 1
            continue
        if (owner, repo) != (read.owner, read.repo):
            skipped += 1
            continue
        entries.append({
            "repository": f"{owner}/{repo}",
            "owner": owner,
            "repo": repo,
            "visibility": read.visibility,
            "default_branch": read.default_branch or None,
            "archived": read.archived,
            "can_push": read.can_push,
            "can_admin": read.can_admin,
            "repo_id": repo_id_for(tenant.tenant_id, owner, repo),
        })
    registered = store.registered(tenant.tenant_id, [entry["repo_id"] for entry in entries])
    for entry in entries:
        entry["registered"] = entry["repo_id"] in registered
    at_cap = page == MAX_READABLE_PAGES
    return {
        "repositories": entries,
        "skipped": skipped,
        "page": page,
        "per_page": PAGE_SIZE,
        "max_pages": MAX_READABLE_PAGES,
        "next_page": page + 1 if listed.full and not at_cap else None,
        "capped": bool(listed.full and at_cap),
        "token_scope": scope,
        "secret_name": secret_name,
    }


__all__ = [
    "COLLECTION", "DEFAULT_ALLOWED_PROFILES", "FORGE", "MAX_READABLE_PAGES",
    "SCOPE_TENANT", "SCOPE_USER", "CredentialSource", "HeldCredential",
    "IndexSettings", "Repositories", "RepositoryCreate", "RepositoryNoAccess",
    "RepositoryPatch", "check_branch", "check_profiles", "parse_repository",
    "read_repository", "read_repository_as", "readable", "register",
    "registration_record", "repo_id_for", "repository_url", "tenant_credential",
    "to_api",
]
