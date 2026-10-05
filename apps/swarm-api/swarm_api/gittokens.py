"""The git token registry: which forge-token slots a tenant has, never their values.

docs/git-tokens.md is the design; lane GT1 builds §1 (the record), §2 (the
slot names), §4.1 (registration by the command line) and the R2 resolution
order the operator picked on 2026-10-05 (docs/web-ui/mockups/PICKS.md).

A RECORD DESCRIBES ONE SECRET MANAGER SLOT AND NEVER HOLDS A VALUE. There is
no field for one, no route that accepts one (Git tokens B: no console paste
box) and nothing here reads Secret Manager. A value enters only through
`scripts/create-secrets.sh --stdin` (CLAUDE.md, owner rule 2026-09-25), and
`store_command` is the exact line for a slot. `last4` is served when an
earlier rotation recorded it and is never derived here.

THREE SCOPES, EACH NAMED THROUGH THE FROZEN `Tenant.secret_name`:

    tenant      git                  swarm-tenant-<tenant>-git (today's slot)
    repository  git-r-<16 hex>       the hex of the repository's repo_id
    user        git-u-<16 hex>       sha256 of the lower-cased email: an email
                                     is hashed, not spelled, because a secret's
                                     name shows in audit logs and plans

THE TENANT DEFAULT IS NOT REGISTERED BY ANYONE: a tenant whose document lists
the `git` credential already has `swarm-tenant-<tenant>-git`, and the first
read creates its record (`ensure_tenant_default`). A revoked default is left
revoked; only registering the slot again makes it usable.

The collection is this module's own, like `issue_runs`, and every read checks
the record's tenant against the caller's: another tenant's token id is the
same 404 as a missing one.

`resolve_r2` is the resolution order as a pure function. Nothing calls it on
the dispatch path yet: using a repository or user token in a task needs a
field on the frozen `Task` (docs/git-tokens.md §8, request E), which is lane
GT5's.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant, utcnow
from swarm_common.states import ParkReason

from .errors import NotFound, ValidationFailed
from .forge import GIT_PROVIDER

log = logging.getLogger(__name__)

COLLECTION = "git_tokens"

#: The only forge phase 1 knows. A record says so rather than leaving it
#: implied, so a second forge is a new value and not a reinterpretation.
FORGE = "github"

#: What `kind` may say once the probe (lane GT2) has read it; None until then.
TOKEN_KINDS = ("fine_grained_pat", "classic_pat", "app_installation")

#: Who `registered_by` names for the tenant default, which nobody registers:
#: it is created from the slot the tenant document already lists.
SYSTEM = "swarm-api"

#: A list never pages: a tenant has one record per registered repository and
#: per member who registered one, plus its default. This bounds a read anyway.
MAX_RECORDS = 1000

REPO_ID = re.compile(r"^repo_[0-9a-f]{16}$")
TOKEN_ID = re.compile(r"^tok_[0-9a-f]{16}$")
#: owner/repo on github.com: the characters GitHub allows in either half.
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Scope(str, Enum):
    TENANT = "tenant"
    REPOSITORY = "repository"
    USER = "user"


class TokenState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    #: Registered and not yet probed. The resolver USES it: refusing on "not
    #: measured" would refuse every slot until lane GT2's probe exists, the
    #: same reason §3.2 uses a capability the probe calls `unknown`.
    UNVERIFIED = "unverified"


#: The states the resolver passes over (§3.2).
UNUSABLE = frozenset({TokenState.EXPIRED, TokenState.REVOKED})


# --------------------------------------------------------------------------
# naming (§1, §2)
# --------------------------------------------------------------------------

def _hex16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def repo_id_for(tenant_id: str, repository: str) -> str:
    """docs/repo-index.md §1: `repo_` + 16 hex of sha256(tenant + host + owner/repo)."""
    return "repo_" + _hex16(tenant_id + "github.com/" + repository.strip().lower())


def _user_key(email: str) -> str:
    return email.strip().lower()


def provider_suffix(scope: Scope | str, *, repo_id: str | None = None,
                    user: str | None = None) -> str:
    """The `--provider` a slot is stored under; `Tenant.secret_name` adds the rest."""
    scope = Scope(scope)
    if scope is Scope.TENANT:
        return GIT_PROVIDER
    if scope is Scope.REPOSITORY:
        if not repo_id or not REPO_ID.match(repo_id):
            raise ValidationFailed("a repository slot needs a repo_id of the form repo_<16 hex>",
                                   detail={"field": "repo_id"})
        return f"{GIT_PROVIDER}-r-{repo_id.removeprefix('repo_')}"
    if not user:
        raise ValidationFailed("a user slot needs the user's email", detail={"field": "user"})
    return f"{GIT_PROVIDER}-u-{_hex16(_user_key(user))}"


def slot_key(scope: Scope | str, *, repo_id: str | None = None, user: str | None = None) -> str:
    """What distinguishes one slot of a scope from another: nothing for the
    tenant default, the repo_id, or the lower-cased email."""
    scope = Scope(scope)
    if scope is Scope.TENANT:
        return ""
    if scope is Scope.REPOSITORY:
        return repo_id or ""
    return _user_key(user or "")


def token_id_for(tenant_id: str, scope: Scope | str, key: str) -> str:
    """§1: `tok_` + 16 hex of sha256(tenant_id + scope + key), so registering a
    slot twice names one record."""
    return "tok_" + _hex16(tenant_id + Scope(scope).value + key)


def secret_name_for(tenant_id: str, suffix: str) -> str:
    # Through the frozen spelling, never restated: a Tenant needs only its id
    # for this, and building one here keeps a single definition of the name.
    return Tenant(tenant_id=tenant_id, kind="group", principal="",
                  created_at=utcnow()).secret_name(suffix)


def store_command(tenant_id: str, suffix: str) -> str:
    """The one way a value reaches a slot (§4.1). `--stdin`, so the value is
    never on a command line, in a file or in this API."""
    return f"scripts/create-secrets.sh --tenant {tenant_id} --provider {suffix} --stdin"


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class GitTokenRecord:
    """`git_tokens/{token_id}`, docs/git-tokens.md §1. No field holds a value."""

    token_id: str
    tenant_id: str
    scope: Scope
    provider_suffix: str
    secret_name: str
    registered_by: str
    registered_at: datetime
    #: repository: the one repo_id. user: [] for every repository the user's
    #: dispatches touch, or the list it is narrowed to. tenant: [].
    repo_ids: list[str] = field(default_factory=list)
    #: The email; user scope only.
    user: str | None = None
    forge: str = FORGE
    kind: str | None = None
    #: The account the token acts as (`GET /user`), read by the probe (GT2).
    forge_login: str | None = None
    #: Recorded from the value in memory at a rotation, never computed here.
    last4: str | None = None
    expires_at: datetime | None = None
    rotated_at: datetime | None = None
    verified_at: datetime | None = None
    state: TokenState = TokenState.UNVERIFIED
    revoked_by: str | None = None
    revoked_at: datetime | None = None

    def to_firestore(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "tenant_id": self.tenant_id,
            "scope": self.scope.value,
            "repo_ids": list(self.repo_ids),
            "user": self.user,
            "provider_suffix": self.provider_suffix,
            "secret_name": self.secret_name,
            "forge": self.forge,
            "kind": self.kind,
            "forge_login": self.forge_login,
            "last4": self.last4,
            "expires_at": self.expires_at,
            "registered_by": self.registered_by,
            "registered_at": self.registered_at,
            "rotated_at": self.rotated_at,
            "verified_at": self.verified_at,
            "state": self.state.value,
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> "GitTokenRecord":
        last4 = data.get("last4")
        return cls(
            token_id=data["token_id"],
            tenant_id=data["tenant_id"],
            scope=Scope(data["scope"]),
            provider_suffix=data["provider_suffix"],
            secret_name=data["secret_name"],
            registered_by=data.get("registered_by") or "",
            registered_at=data["registered_at"],
            repo_ids=list(data.get("repo_ids") or []),
            user=data.get("user"),
            forge=data.get("forge") or FORGE,
            kind=data.get("kind"),
            forge_login=data.get("forge_login"),
            # Four characters at most, whatever the document says: a field
            # written wrongly must not become a way to serve more of a value.
            last4=str(last4)[-4:] if last4 else None,
            expires_at=data.get("expires_at"),
            rotated_at=data.get("rotated_at"),
            verified_at=data.get("verified_at"),
            state=TokenState(data.get("state") or TokenState.UNVERIFIED.value),
            revoked_by=data.get("revoked_by"),
            revoked_at=data.get("revoked_at"),
        )

    def to_api(self) -> dict[str, Any]:
        body = self.to_firestore()
        for key in ("expires_at", "registered_at", "rotated_at", "verified_at", "revoked_at"):
            body[key] = _iso(body[key])
        body["last4"] = self.last4
        body["store_command"] = store_command(self.tenant_id, self.provider_suffix)
        return body


def record_for_slot(
    tenant_id: str,
    scope: Scope | str,
    *,
    repo_id: str | None = None,
    user: str | None = None,
    repo_ids: Iterable[str] | None = None,
    registered_by: str,
    now: datetime,
) -> GitTokenRecord:
    """A fresh, unverified record for one slot. Pure: names and nothing else."""
    scope = Scope(scope)
    suffix = provider_suffix(scope, repo_id=repo_id, user=user)
    if scope is Scope.REPOSITORY:
        covered = [repo_id or ""]
    elif scope is Scope.USER:
        covered = sorted(set(repo_ids or ()))
        bad = [r for r in covered if not REPO_ID.match(r)]
        if bad:
            raise ValidationFailed("repo_ids must each be repo_<16 hex>",
                                   detail={"field": "repo_ids"})
    else:
        covered = []
    return GitTokenRecord(
        token_id=token_id_for(tenant_id, scope, slot_key(scope, repo_id=repo_id, user=user)),
        tenant_id=tenant_id,
        scope=scope,
        provider_suffix=suffix,
        secret_name=secret_name_for(tenant_id, suffix),
        registered_by=registered_by,
        registered_at=now,
        repo_ids=covered,
        user=_user_key(user) if scope is Scope.USER and user else None,
    )


# --------------------------------------------------------------------------
# the collection
# --------------------------------------------------------------------------

class GitTokens:
    """The `git_tokens` collection. Every read is checked against the caller's tenant."""

    def __init__(self, db: Any, *, now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._now = now

    def _ref(self, token_id: str) -> Any:
        return self._db.collection(COLLECTION).document(token_id)

    @staticmethod
    def _not_found(token_id: str) -> NotFound:
        # One sentence for "no such token" and "another tenant's token".
        return NotFound(f"git token {token_id!r} not found")

    def ensure_tenant_default(self, tenant: Tenant | None) -> GitTokenRecord | None:
        """Create the tenant default's record from the `-git` slot the tenant
        document already lists, once. None when the tenant lists no such slot
        or the record already exists (in whatever state, revoked included)."""
        if tenant is None or GIT_PROVIDER not in (tenant.credentials or []):
            return None
        record = record_for_slot(tenant.tenant_id, Scope.TENANT,
                                 registered_by=SYSTEM, now=self._now())
        ref = self._ref(record.token_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _create(txn: Any) -> GitTokenRecord | None:
            if _snapshot(txn.get(ref)).exists:
                return None
            txn.set(ref, record.to_firestore())
            return record

        created = _create(transaction)
        if created is not None:
            log.info("git token default created tenant=%s token_id=%s secret=%s",
                     tenant.tenant_id, record.token_id, record.secret_name)
        return created

    def get(self, tenant_id: str, token_id: str) -> GitTokenRecord:
        if not TOKEN_ID.match(token_id or ""):
            raise self._not_found(token_id)
        snap = self._ref(token_id).get()
        if not snap.exists:
            raise self._not_found(token_id)
        data = snap.to_dict()
        if data.get("tenant_id") != tenant_id:
            raise self._not_found(token_id)
        return GitTokenRecord.from_firestore(data)

    def list(self, tenant_id: str) -> list[GitTokenRecord]:
        """The tenant's records: the default first, then repositories, then users."""
        query = self._db.collection(COLLECTION).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        ).limit(MAX_RECORDS)
        rows = [GitTokenRecord.from_firestore(snap.to_dict()) for snap in query.stream()]
        # The filter again, in the application, whatever the engine returned.
        rows = [row for row in rows if row.tenant_id == tenant_id]
        order = {Scope.TENANT: 0, Scope.REPOSITORY: 1, Scope.USER: 2}
        rows.sort(key=lambda r: (order[r.scope], r.registered_at, r.token_id))
        return rows

    def register(self, record: GitTokenRecord) -> tuple[GitTokenRecord, bool]:
        """Create the slot's record, or make an existing one usable again.

        Idempotent on the slot. A record that exists keeps what is known about
        the slot (kind, login, last4, expiry, last verified) and returns to
        `unverified` only if it had been revoked; True when it was created.
        """
        ref = self._ref(record.token_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[GitTokenRecord, bool]:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                txn.set(ref, record.to_firestore())
                return record, True
            existing = GitTokenRecord.from_firestore(snap.to_dict())
            if existing.tenant_id != record.tenant_id:
                # A token id is a hash of the tenant; equal ids across tenants
                # would be a collision, and is refused rather than shared.
                raise self._not_found(record.token_id)
            if record.scope is Scope.USER and existing.repo_ids != record.repo_ids:
                existing.repo_ids = record.repo_ids
                txn.update(ref, {"repo_ids": record.repo_ids})
            if existing.state is TokenState.REVOKED:
                existing.state = TokenState.UNVERIFIED
                existing.registered_by = record.registered_by
                existing.registered_at = record.registered_at
                existing.revoked_by = None
                existing.revoked_at = None
                txn.update(ref, {
                    "state": existing.state.value,
                    "registered_by": existing.registered_by,
                    "registered_at": existing.registered_at,
                    "revoked_by": None,
                    "revoked_at": None,
                })
            return existing, False

        result, created = _apply(transaction)
        log.info("git token slot registered tenant=%s token_id=%s scope=%s secret=%s created=%s",
                 result.tenant_id, result.token_id, result.scope.value, result.secret_name,
                 created)
        return result, created

    def revoke(self, tenant_id: str, token_id: str, *, by: str,
               allowed: Callable[[GitTokenRecord], None]) -> GitTokenRecord:
        """Mark the record revoked, after `allowed` (which raises) has seen it.

        The secret's versions are NOT disabled here: swarm-api holds no grant
        that can, and is not given one for this (S1 keeps it unprivileged).
        The resolver passes a revoked record over, which is what stops it
        being used; the route says so and names the secret.
        """
        if not TOKEN_ID.match(token_id or ""):
            raise self._not_found(token_id)
        ref = self._ref(token_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> GitTokenRecord:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != tenant_id:
                raise self._not_found(token_id)
            record = GitTokenRecord.from_firestore(data)
            allowed(record)
            if record.state is not TokenState.REVOKED:
                record.state = TokenState.REVOKED
                record.revoked_by = by
                record.revoked_at = self._now()
                txn.update(ref, {"state": record.state.value, "revoked_by": by,
                                 "revoked_at": record.revoked_at})
            return record

        record = _apply(transaction)
        log.info("git token revoked tenant=%s token_id=%s scope=%s secret=%s",
                 tenant_id, token_id, record.scope.value, record.secret_name)
        return record


# --------------------------------------------------------------------------
# resolution: R2 (§3.1, picked 2026-10-05)
# --------------------------------------------------------------------------

#: Actions whose author shows on the pull request: the user's token, when
#: there is one, under R2. Every other action -- clone, push, merge -- uses the
#: repository's or the tenant's token, never a person's (§3.3).
AUTHORED_ACTIONS = frozenset({"open_pull_request", "comment"})
CREDENTIAL_ACTIONS = frozenset({"clone", "push", "merge"})


@dataclass(frozen=True)
class Resolution:
    """Which record each forge action uses for one task. Names, never values."""

    credential: GitTokenRecord | None
    author: GitTokenRecord | None
    #: Every scope passed over and why, by name (§3.2): `repo: expired
    #: 2026-09-30`, `tenant: none registered`, `user: revoked`.
    tried: tuple[str, ...] = ()

    @property
    def park_reason(self) -> ParkReason | None:
        """CREDENTIAL_MISSING when nothing can clone: the existing park, at no cost."""
        return ParkReason.CREDENTIAL_MISSING if self.credential is None else None

    def for_action(self, action: str) -> GitTokenRecord | None:
        if action in AUTHORED_ACTIONS:
            return self.author
        if action in CREDENTIAL_ACTIONS:
            return self.credential
        raise ValueError(f"unknown forge action {action!r}")

    def to_api(self) -> dict[str, Any]:
        def name(record: GitTokenRecord | None) -> dict[str, str] | None:
            if record is None:
                return None
            return {"token_id": record.token_id, "scope": record.scope.value,
                    "secret_name": record.secret_name}

        return {
            "credential": name(self.credential),
            "author": name(self.author),
            "park_reason": self.park_reason.value if self.park_reason else None,
            "tried": list(self.tried),
            "order": "R2",
        }


def _passed_over(record: GitTokenRecord, now: datetime) -> str | None:
    """Why a record cannot be used now, or None when it can."""
    if record.state is TokenState.REVOKED:
        return TokenState.REVOKED.value
    if record.expires_at is not None and record.expires_at <= now:
        return f"{TokenState.EXPIRED.value} {record.expires_at.date().isoformat()}"
    if record.state is TokenState.EXPIRED:
        return TokenState.EXPIRED.value
    return None


def _pick(candidates: list[GitTokenRecord], label: str, now: datetime,
          tried: list[str]) -> GitTokenRecord | None:
    if not candidates:
        tried.append(f"{label}: none registered")
        return None
    reasons = []
    for record in sorted(candidates, key=lambda r: r.token_id):
        reason = _passed_over(record, now)
        if reason is None:
            return record
        reasons.append(reason)
    tried.append(f"{label}: {', '.join(reasons)}")
    return None


def resolve_r2(
    records: Iterable[GitTokenRecord],
    *,
    tenant_id: str,
    repo_id: str,
    user: str | None,
    now: datetime,
) -> Resolution:
    """R2: the repository's token, else the tenant default, for clone, push and
    merge; the dispatching user's token, if it covers the repository, for the
    actions whose author is visible, else the same credential.

    Pure. Only the task's OWN tenant's records are considered, whatever the
    caller passed, so a record from another tenant can never resolve. `user`
    is the task's `submitted_by`, never a field a caller sets (invariant 10).
    """
    own = [r for r in records if r.tenant_id == tenant_id]
    tried: list[str] = []
    repo = _pick([r for r in own if r.scope is Scope.REPOSITORY and repo_id in r.repo_ids],
                 "repo", now, tried)
    credential = repo or _pick([r for r in own if r.scope is Scope.TENANT], "tenant", now, tried)
    mine: GitTokenRecord | None = None
    if user:
        key = _user_key(user)
        mine = _pick(
            [r for r in own if r.scope is Scope.USER and r.user == key
             and (not r.repo_ids or repo_id in r.repo_ids)],
            "user", now, tried,
        )
    return Resolution(credential=credential, author=mine or credential, tried=tuple(tried))
