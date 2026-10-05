"""The git token registry: which forge-token slots a tenant has, never their values.

docs/git-tokens.md is the design; lane GT1 builds §1 (the record), §2 (the
slot names), §4.1 (registration by the command line) and the R2 resolution
order the operator picked on 2026-10-05 (docs/web-ui/mockups/PICKS.md).

A RECORD DESCRIBES ONE SECRET MANAGER SLOT AND NEVER HOLDS A VALUE. There is
no field for one and no route that accepts one (Git tokens B: no console
paste box). A value enters only through `scripts/create-secrets.sh --stdin`
(CLAUDE.md, owner rule 2026-09-25), and `store_command` is the exact line
for a slot. The one read of a value is the probe's (lane GT2a, §5 below):
it holds the value in a local for one probe, registers it with the
redaction filter first, and keeps `last4` -- the only part of a value ever
stored, recorded from memory at registration or rotation (§1).

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
import json as _json
import logging
import re
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable
from urllib.parse import quote as _quote

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant, utcnow
from swarm_common.states import ParkReason

from . import forge as _forge
from .errors import Conflict, NotFound, ValidationFailed
from .forge import GIT_PROVIDER
from .redaction import redact

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
    #: Registered and not yet completely probed (no value stored yet, or the
    #: forge did not answer). The resolver USES it: refusing on "not measured"
    #: would refuse a slot for a forge outage, the same reason §3.2 uses a
    #: capability the probe calls `unknown`.
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
    #: repo_id -> "owner/repo" for the repositories this slot is known to
    #: cover by name (a repo_id is a one-way hash): what an unscoped verify
    #: probes. Names, never values.
    repositories: dict[str, str] = field(default_factory=dict)
    #: The Secret Manager version the last probe read: a change is a rotation.
    secret_version: str | None = None
    #: The last probe attempt, complete or not (§5.3). `verified_at` above is
    #: the last COMPLETE one.
    probe_attempted_at: datetime | None = None
    probe_complete: bool | None = None
    probe_error: str | None = None
    rate_remaining: int | None = None

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
            "repositories": dict(self.repositories),
            "secret_version": self.secret_version,
            "probe_attempted_at": self.probe_attempted_at,
            "probe_complete": self.probe_complete,
            "probe_error": self.probe_error,
            "rate_remaining": self.rate_remaining,
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
            repositories={str(k): str(v) for k, v in (data.get("repositories") or {}).items()},
            secret_version=data.get("secret_version"),
            probe_attempted_at=data.get("probe_attempted_at"),
            probe_complete=data.get("probe_complete"),
            probe_error=data.get("probe_error"),
            rate_remaining=data.get("rate_remaining"),
        )

    def to_api(self, pair_docs: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
        """The record as served, with its probe summary (§5, §6: the
        permission matrix reads `probe.repositories`). `pair_docs` are this
        tenant's `git_token_checks` documents; only this record's are used."""
        body = self.to_firestore()
        for key in ("expires_at", "registered_at", "rotated_at", "verified_at", "revoked_at",
                    "probe_attempted_at"):
            body[key] = _iso(body[key])
        body["last4"] = self.last4
        body["store_command"] = store_command(self.tenant_id, self.provider_suffix)
        body["probe"] = probe_summary(self, pair_docs)
        return body


def record_for_slot(
    tenant_id: str,
    scope: Scope | str,
    *,
    repo_id: str | None = None,
    user: str | None = None,
    repo_ids: Iterable[str] | None = None,
    repository: str | None = None,
    registered_by: str,
    now: datetime,
) -> GitTokenRecord:
    """A fresh, unverified record for one slot. Pure: names and nothing else.

    `repository`: the `owner/repo` a repository slot was registered by, kept
    so the probe knows what to read (its repo_id is a one-way hash)."""
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
        repositories=({repo_id: repository.strip()} if scope is Scope.REPOSITORY
                      and repository and repo_id else {}),
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
            named = {**existing.repositories, **record.repositories}
            if named != existing.repositories:
                existing.repositories = named
                txn.update(ref, {"repositories": named})
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

    # -- the probe (§5; lane GT2a) ------------------------------------------

    def pair_docs(self, tenant_id: str) -> list[dict[str, Any]]:
        """The tenant's `git_token_checks`, filtered again in the application."""
        query = self._db.collection(CHECKS_COLLECTION).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        ).limit(MAX_RECORDS * 10)
        docs = [snap.to_dict() for snap in query.stream()]
        return [doc for doc in docs if doc.get("tenant_id") == tenant_id]

    def probe(
        self,
        tenant: Tenant | None,
        tenant_id: str,
        token_id: str,
        *,
        tokens: Any,
        send: Any,
        repository: str | None = None,
        repo_id: str | None = None,
        allowed: Callable[[GitTokenRecord], None] = lambda record: None,
    ) -> tuple[GitTokenRecord, ProbeResult]:
        """Probe one record now: every repository it is known to cover, or
        only the one named (by `repository`, or by a `repo_id` whose name the
        record already knows). Stores the result and returns the record as
        stored. Another tenant's record is the same 404 as a missing one, and
        is checked before anything is read or sent."""
        record = self.get(tenant_id, token_id)
        allowed(record)
        if record.state is TokenState.REVOKED:
            raise Conflict(f"git token {token_id!r} is revoked; register its slot again "
                           "before verifying it")
        scoped: tuple[str, str] | None = None
        if repository is not None:
            if not REPOSITORY.match(repository):
                raise ValidationFailed("repository must be owner/repo",
                                       detail={"field": "repository"})
            scoped = (repo_id_for(tenant_id, repository), repository.strip())
        elif repo_id is not None:
            if not REPO_ID.match(repo_id) or repo_id not in record.repositories:
                raise ValidationFailed(
                    "repo_id must be a repository this token is known to cover by name; "
                    "name it by repository (owner/repo) instead",
                    detail={"field": "repo_id"})
            scoped = (repo_id, record.repositories[repo_id])
        if scoped is not None and not covers(record, scoped[0]):
            raise ValidationFailed(
                f"git token {token_id!r} does not cover {scoped[1]}",
                detail={"field": "repository"})
        targets = [scoped] if scoped is not None else _known_repositories(record)
        if tenant is None:
            tenant = Tenant(tenant_id=tenant_id, kind="group", principal="", created_at=utcnow())
        result = run_probe(record, tenant, targets, tokens=tokens, send=send, now=self._now())
        stored = self._store_probe(record, result, scoped=scoped)
        log.info("git token probed tenant=%s token_id=%s repositories=%d complete=%s",
                 tenant_id, token_id, len(result.pairs), result.complete)
        return stored, result

    def _store_probe(self, record: GitTokenRecord, result: ProbeResult, *,
                     scoped: tuple[str, str] | None) -> GitTokenRecord:
        for pair in result.pairs:
            ref = _check_ref(self._db, record.token_id, pair.repo_id)
            snap = ref.get()
            previous = snap.to_dict() if snap.exists else None
            if previous is not None and previous.get("tenant_id") != record.tenant_id:
                continue
            ref.set(_merge_pair(previous, pair, record.tenant_id, record.token_id, result))
        ref = self._ref(record.token_id)
        transaction = self._db.transaction()
        now = result.attempted_at

        @firestore.transactional
        def _apply(txn: Any) -> GitTokenRecord:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != record.tenant_id:
                raise self._not_found(record.token_id)
            fresh = GitTokenRecord.from_firestore(data)
            fresh.probe_attempted_at = now
            fresh.probe_complete = result.complete
            fresh.probe_error = result.error
            if result.rate_remaining is not None:
                fresh.rate_remaining = result.rate_remaining
            if scoped is not None:
                named = dict(fresh.repositories)
                named[scoped[0]] = scoped[1]
                if len(named) <= MAX_KNOWN_REPOSITORIES:
                    fresh.repositories = named
            if result.read_value:
                if result.kind:
                    fresh.kind = result.kind
                if result.forge_login:
                    fresh.forge_login = result.forge_login
                if result.expiry_read:
                    fresh.expires_at = result.expires_at
                rotated = (
                    (fresh.secret_version is not None and result.version is not None
                     and fresh.secret_version != result.version)
                    or (result.version is None and fresh.last4 is not None
                        and result.last4 is not None and fresh.last4 != result.last4)
                )
                if rotated:
                    fresh.rotated_at = now
                fresh.last4 = result.last4
                fresh.secret_version = result.version
                if result.complete and scoped is None:
                    fresh.verified_at = now
                if fresh.state is not TokenState.REVOKED:
                    if fresh.expires_at is not None and fresh.expires_at <= now:
                        fresh.state = TokenState.EXPIRED
                    elif result.complete and not result.rejected:
                        fresh.state = TokenState.ACTIVE
            txn.set(ref, fresh.to_firestore())
            return fresh

        return _apply(transaction)


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


# --------------------------------------------------------------------------
# the probe: what a token can do, per repository (§5; lane GT2a)
# --------------------------------------------------------------------------
#
# EIGHT READERS, EACH A NON-MUTATING READ. Every request is a GET, to
# api.github.com or (for the push advertisement) github.com, and nothing
# else: `forge.may_receive_forge_token` is asked before each one. The reads a
# reader shares are made once per repository, so a token x repository costs
# at most MAX_GETS_PER_REPOSITORY, the account read included.
#
# ok / missing / unknown, ALWAYS WITH A REASON AND THE EVIDENCE. `unknown` is
# a measured answer ("fine-grained grants are not readable"), not a failure.
# A read that did not come back -- a network error, a 5xx, a 429, a 403 with
# the rate limit spent -- is NOT an answer: the reader returns nothing, the
# previous row stands, and the probe is partial. `verified_at` is the time of
# the last COMPLETE probe (§5.3); a partial one moves `attempted_at` only and
# says why in `error`.
#
# THE SECRET (§5.4). `run_probe` reads it into a local, `probe_token`
# registers it with the redaction filter (`redaction_literal`) before the
# first request, and every forge message that becomes evidence goes through
# `redact` with it as a literal. A transport's exception is named by its type
# and never by its text. Nothing the probe returns or stores holds any of the
# value but `last4`.

CHECKS_COLLECTION = "git_token_checks"

#: §5.1's rows, in the order the permission matrix (A) draws them.
CAPABILITIES = (
    "clone",
    "push",
    "open_pull_requests",
    "read_checks",
    "merge",
    "close_issues",
    "read_issues",
    "workflow_dispatch",
)

OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"

#: §5.3: "a probe is at most eight GETs". The repository, the push
#: advertisement, check runs, branch rules, issues, workflows (fine-grained
#: only) and the account read, which is made once per token.
MAX_GETS_PER_REPOSITORY = 8

#: The repositories a record remembers by name, so an unscoped verify knows
#: what to probe. Past it the oldest-named are still covered, just not
#: re-probed until they are named again.
MAX_KNOWN_REPOSITORIES = 50

#: Wall clock for one probe. The console waits on a registration or a
#: "Verify now"; repositories not reached in time leave the probe partial.
PROBE_BUDGET_SECONDS = 45.0

#: Per GET.
PROBE_TIMEOUT_SECONDS = 10.0

#: How much of a forge's own message is kept as evidence, after masking.
MAX_EVIDENCE_CHARS = 200

#: Rows whose grant GitHub does not expose for a fine-grained token (§5.2).
FINE_GRAINED_UNKNOWN = "fine-grained grants are not readable; the role allows it"
#: The same for an App installation token held in the slot: its grant is in
#: the response that minted it, which this probe does not make.
APP_UNKNOWN = ("an installation token's grant is read from its mint response, "
               "which this probe does not make; the role allows it")

_ROLE_TRIAGE = ("triage", "push", "maintain", "admin")


def token_kind(value: str) -> str | None:
    """The token's kind from its prefix, read in memory and never stored.

    GitHub's documented prefixes: `github_pat_` fine-grained, `ghp_` classic
    (and `gho_`, an OAuth app's token, which carries classic scopes), `ghs_`
    an App installation token. Anything else is None, and its unreadable rows
    are `unknown` as a fine-grained token's are.
    """
    if value.startswith("github_pat_"):
        return "fine_grained_pat"
    if value.startswith(("ghp_", "gho_")):
        return "classic_pat"
    if value.startswith("ghs_"):
        return "app_installation"
    return None


def parse_expiry(header: str | None) -> datetime | None:
    """`github-authentication-token-expiration` (§3.4) as an aware UTC time.

    GitHub writes `2026-11-01 00:00:00 UTC`, and for some tokens a numeric
    offset (`-0800`). Anything unparseable is None: no expiry read, never a
    guessed one.
    """
    from datetime import timezone

    text = (header or "").strip()
    if not text:
        return None
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")] + " +0000"
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


class _LiteralFilter(logging.Filter):
    """Masks one literal in every record that passes, wherever it was logged."""

    def __init__(self, literal: str) -> None:
        super().__init__()
        self._literal = literal

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if self._literal in message:
            record.msg = redact(message, extra=(self._literal,)).text
            record.args = None
        if record.exc_text and self._literal in record.exc_text:
            record.exc_text = redact(record.exc_text, extra=(self._literal,)).text
        return True


def _all_handlers() -> list[logging.Handler]:
    handlers = list(logging.getLogger().handlers)
    for candidate in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(candidate, logging.Logger):
            handlers.extend(candidate.handlers)
    seen: set[int] = set()
    unique = []
    for handler in handlers:
        if id(handler) not in seen:
            seen.add(id(handler))
            unique.append(handler)
    return unique


@contextmanager
def redaction_literal(value: str):
    """§5.4: register `value` with the redaction filter for the probe's length.

    swarm-api has no process-wide secret registry, so the filter is attached
    to every handler that exists when the probe starts: a record logged by
    any code during the probe -- this module, the transport, a library under
    it -- is masked before any handler writes it.
    """
    if not value:
        yield
        return
    literal_filter = _LiteralFilter(value)
    handlers = _all_handlers()
    for handler in handlers:
        handler.addFilter(literal_filter)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(literal_filter)


@dataclass(frozen=True)
class Check:
    """One capability's answer for one token x repository."""

    state: str
    reason: str
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state, "reason": self.reason, "evidence": self.evidence}


@dataclass(frozen=True)
class PairResult:
    repo_id: str
    repository: str
    #: capability -> its answer, or None when its read did not come back.
    checks: dict[str, Check | None]
    complete: bool
    error: str | None = None


@dataclass
class ProbeResult:
    attempted_at: datetime
    complete: bool
    error: str | None = None
    kind: str | None = None
    forge_login: str | None = None
    #: True when some answer came back from which the expiry header was read
    #: (present or absent); `expires_at` is then the truth, None for no expiry.
    expiry_read: bool = False
    expires_at: datetime | None = None
    rate_remaining: int | None = None
    #: The forge answered 401 to the account read: it does not accept the token.
    rejected: bool = False
    pairs: tuple[PairResult, ...] = ()
    #: Set by `run_probe` from the value it read, never by `probe_token`.
    last4: str | None = None
    version: str | None = None
    read_value: bool = False


class _Unanswered(Exception):
    """A read that did not come back. Its text is built here, never upstream."""


@dataclass(frozen=True)
class _Answer:
    status: int
    data: Any
    evidence: str


class _Forge:
    """The probe's GETs for one token, each answer kept for the readers."""

    def __init__(self, value: str, send: Any, result: ProbeResult, timeout: float) -> None:
        self._value = value
        self._send = send
        self._result = result
        self._timeout = timeout
        self._cache: dict[str, _Answer] = {}
        self.gets = 0
        #: A classic token's scopes (`X-OAuth-Scopes`); None when not returned.
        self.scopes: frozenset[str] | None = None

    def _message(self, raw: bytes) -> str:
        try:
            data = _json.loads(raw.decode("utf-8"))
        except Exception:
            return ""
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, str):
            return ""
        masked = redact(message, extra=(self._value,)).text
        line = " ".join(masked.split())
        return line[:MAX_EVIDENCE_CHARS]

    def get(self, url: str, label: str, *, git: bool = False) -> _Answer:
        if url in self._cache:
            return self._cache[url]
        if not _forge.may_receive_forge_token(url):
            raise _Unanswered(f"{label}: refused to send the token to that host")
        headers = (_forge.git_basic_headers(self._value) if git
                   else _forge.github_headers(self._value))
        self.gets += 1
        try:
            response = self._send(url, headers, self._timeout)
        except Exception as exc:
            # The type only: a transport's message can quote the request.
            raise _Unanswered(f"{label}: the read failed ({type(exc).__name__})") from None
        finally:
            headers = {}
        status = int(response.status)
        answer_headers = {str(k).lower(): str(v) for k, v in dict(response.headers or {}).items()}
        remaining = answer_headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.strip().isdigit():
            self._result.rate_remaining = int(remaining)
        if status == 429 or status >= 500 or (status == 403 and remaining == "0"):
            raise _Unanswered(
                f"{label}: GitHub answered HTTP {status}"
                + (" with the rate limit spent" if remaining == "0" else "")
            )
        if not git:
            if "x-oauth-scopes" in answer_headers:
                self.scopes = frozenset(
                    s.strip() for s in answer_headers["x-oauth-scopes"].split(",") if s.strip()
                )
            if status != 401:
                self._result.expiry_read = True
                self._result.expires_at = parse_expiry(
                    answer_headers.get("github-authentication-token-expiration"))
        data: Any = None
        if status == 200 and not git:
            try:
                data = _json.loads(bytes(response.body or b"").decode("utf-8"))
            except Exception:
                data = None
        message = "" if status == 200 else self._message(bytes(response.body or b""))
        evidence = f"GET {label} -> HTTP {status}" + (f": {message}" if message else "")
        answer = _Answer(status=status, data=data, evidence=evidence)
        self._cache[url] = answer
        return answer


def _api(owner: str, repo: str, path: str = "") -> str:
    base = (f"https://{_forge.GITHUB_API_HOST}/repos/{_quote(owner, safe='')}/"
            f"{_quote(repo, safe='')}")
    return base + (f"/{path}" if path else "")


class _Repository:
    """The facts the eight readers share for one repository."""

    def __init__(self, forge: _Forge, repository: str, kind: str | None) -> None:
        self.forge = forge
        self.owner, self.name = repository.split("/", 1)
        self.repository = repository
        self.kind = kind

    def repo(self) -> _Answer:
        return self.forge.get(_api(self.owner, self.name), f"/repos/{self.repository}")

    @property
    def data(self) -> dict[str, Any]:
        data = self.repo().data
        return data if isinstance(data, dict) else {}

    @property
    def default_branch(self) -> str:
        branch = self.data.get("default_branch")
        return branch if isinstance(branch, str) and branch else "main"

    @property
    def private(self) -> bool:
        return self.data.get("private") is not False

    def role_allows(self, *roles: str) -> bool:
        permissions = self.data.get("permissions")
        if not isinstance(permissions, dict):
            return False
        return any(permissions.get(role) is True for role in roles)

    def classic_scope(self) -> bool | None:
        """Whether a classic token's scopes reach this repository; None if unread."""
        scopes = self.forge.scopes
        if scopes is None:
            return None
        return "repo" in scopes or (not self.private and "public_repo" in scopes)


def _refused(answer: _Answer, what: str) -> Check:
    """A 401 / 403 / 404 / 410 on a read, as `missing`."""
    if answer.status == 404:
        reason = f"{what}: not visible to this token"
    elif answer.status == 401:
        reason = f"{what}: GitHub does not accept this token (revoked, expired or mistyped)"
    else:
        reason = f"{what}: GitHub refused this token (HTTP {answer.status})"
    return Check(MISSING, reason, answer.evidence)


def _odd(answer: _Answer, what: str) -> Check:
    if 300 <= answer.status < 400:
        return Check(UNKNOWN, f"{what}: GitHub answered a redirect, which is never followed "
                     "(a renamed or moved repository?)", answer.evidence)
    return Check(UNKNOWN, f"{what}: GitHub answered HTTP {answer.status}", answer.evidence)


def _needs(prior: Check, name: str) -> Check | None:
    """A row that depends on `prior`: carried over unless `prior` is ok."""
    if prior.state == OK:
        return None
    return Check(prior.state, f"needs {name}: {prior.reason}", prior.evidence)


def read_clone(r: _Repository) -> Check:
    answer = r.repo()
    if answer.status == 200:
        return Check(OK, "the repository is readable", answer.evidence)
    if answer.status == 404:
        return Check(MISSING, "not visible to this token (GitHub answers 404 for a private "
                     "repository it cannot see)", answer.evidence)
    if answer.status in (401, 403, 410):
        return _refused(answer, "the repository")
    return _odd(answer, "the repository")


def read_push(r: _Repository, clone: Check) -> Check:
    carried = _needs(clone, "clone")
    if carried:
        return carried
    evidence = r.repo().evidence
    if not r.role_allows("push", "maintain", "admin"):
        return Check(MISSING, "the actor's role on the repository does not allow push", evidence)
    if r.kind == "classic_pat":
        scope = r.classic_scope()
        if scope is None:
            return Check(UNKNOWN, "the classic token's scopes were not returned", evidence)
        if not scope:
            return Check(MISSING, "a classic token needs the repo scope (public_repo for a "
                         "public repository)", evidence)
    url = (f"https://github.com/{_quote(r.owner, safe='')}/{_quote(r.name, safe='')}.git"
           "/info/refs?service=git-receive-pack")
    answer = r.forge.get(url, f"github.com/{r.repository}.git/info/refs?service=git-receive-pack",
                         git=True)
    if answer.status == 200:
        return Check(OK, "measured by receive-pack advertisement", answer.evidence)
    if answer.status in (401, 403, 404):
        return Check(MISSING, "measured by receive-pack advertisement: GitHub refused it "
                     f"(HTTP {answer.status})", answer.evidence)
    return _odd(answer, "the push advertisement")


def read_open_pull_requests(r: _Repository, push: Check) -> Check:
    carried = _needs(push, "push")
    if carried:
        return carried
    if r.kind == "classic_pat":
        return Check(OK, "classic repo scope, and the token may push", push.evidence)
    if r.kind == "app_installation":
        return Check(UNKNOWN, APP_UNKNOWN, push.evidence)
    return Check(UNKNOWN, FINE_GRAINED_UNKNOWN, push.evidence)


def read_read_checks(r: _Repository, clone: Check) -> Check:
    carried = _needs(clone, "clone")
    if carried:
        return carried
    branch = r.default_branch
    answer = r.forge.get(
        _api(r.owner, r.name, f"commits/{_quote(branch, safe='')}/check-runs?per_page=1"),
        f"/repos/{r.repository}/commits/{branch}/check-runs",
    )
    if answer.status == 200:
        return Check(OK, f"check runs on {branch} are readable", answer.evidence)
    if answer.status in (401, 403, 404):
        return _refused(answer, f"check runs on {branch}")
    return _odd(answer, f"check runs on {branch}")


def read_merge(r: _Repository, push: Check) -> Check:
    carried = _needs(push, "push")
    if carried:
        return carried
    branch = r.default_branch
    answer = r.forge.get(
        _api(r.owner, r.name, f"rules/branches/{_quote(branch, safe='')}"),
        f"/repos/{r.repository}/rules/branches/{branch}",
    )
    if answer.status != 200:
        return Check(UNKNOWN, f"the branch rules on {branch} could not be read "
                     f"(HTTP {answer.status})", answer.evidence)
    rules = answer.data if isinstance(answer.data, list) else []
    types = {rule.get("type") for rule in rules if isinstance(rule, dict)}
    if "merge_queue" in types:
        return Check(MISSING, f"merges into {branch} go through a merge queue", answer.evidence)
    if "update" in types:
        return Check(UNKNOWN, f"a ruleset restricts updates to {branch} to its bypass list; "
                     "whether this token's actor is on it is not readable from the branch "
                     "rules", answer.evidence)
    return Check(OK, f"the token may push, and no branch rule on {branch} restricts merging "
                 "to other actors", answer.evidence)


def read_close_issues(r: _Repository, clone: Check) -> Check:
    carried = _needs(clone, "clone")
    if carried:
        return carried
    evidence = r.repo().evidence
    if not r.role_allows(*_ROLE_TRIAGE):
        return Check(MISSING, "the actor's role on the repository cannot close issues "
                     "(it needs triage or above)", evidence)
    if r.kind == "classic_pat":
        scope = r.classic_scope()
        if scope is None:
            return Check(UNKNOWN, "the classic token's scopes were not returned", evidence)
        if not scope:
            return Check(MISSING, "a classic token needs the repo scope (public_repo for a "
                         "public repository)", evidence)
        return Check(OK, "classic repo scope, and the role is triage or above", evidence)
    if r.kind == "app_installation":
        return Check(UNKNOWN, APP_UNKNOWN, evidence)
    return Check(UNKNOWN, FINE_GRAINED_UNKNOWN, evidence)


def read_read_issues(r: _Repository, clone: Check) -> Check:
    carried = _needs(clone, "clone")
    if carried:
        return carried
    answer = r.forge.get(_api(r.owner, r.name, "issues?per_page=1"),
                         f"/repos/{r.repository}/issues")
    if answer.status == 200:
        return Check(OK, "issues are readable", answer.evidence)
    if answer.status == 410:
        return Check(MISSING, "issues are disabled on this repository", answer.evidence)
    if answer.status in (401, 403, 404):
        return _refused(answer, "issues")
    return _odd(answer, "issues")


def read_workflow_dispatch(r: _Repository, clone: Check) -> Check:
    carried = _needs(clone, "clone")
    if carried:
        return carried
    evidence = r.repo().evidence
    if not r.role_allows("push", "maintain", "admin"):
        return Check(MISSING, "the actor's role on the repository cannot dispatch workflows "
                     "(it needs write)", evidence)
    if r.kind == "classic_pat":
        scope = r.classic_scope()
        if scope is None:
            return Check(UNKNOWN, "the classic token's scopes were not returned", evidence)
        if "repo" not in (r.forge.scopes or ()):
            return Check(MISSING, "a classic token needs the repo scope to dispatch a "
                         "workflow", evidence)
        return Check(OK, "classic repo scope, and the role is write or above", evidence)
    if r.kind == "app_installation":
        return Check(UNKNOWN, APP_UNKNOWN, evidence)
    answer = r.forge.get(_api(r.owner, r.name, "actions/workflows?per_page=1"),
                         f"/repos/{r.repository}/actions/workflows")
    if answer.status in (401, 403):
        return Check(MISSING, f"the workflows are not readable by this token "
                     f"(HTTP {answer.status})", answer.evidence)
    return Check(UNKNOWN, FINE_GRAINED_UNKNOWN, answer.evidence)


def _probe_repository(forge: _Forge, repo_id: str, repository: str,
                      kind: str | None) -> PairResult:
    r = _Repository(forge, repository, kind)
    checks: dict[str, Check | None] = {cap: None for cap in CAPABILITIES}
    errors: list[str] = []

    def attempt(cap: str, reader: Callable[[], Check]) -> Check | None:
        try:
            checks[cap] = reader()
        except _Unanswered as unanswered:
            errors.append(str(unanswered))
        return checks[cap]

    clone = attempt("clone", lambda: read_clone(r))
    if clone is not None:
        push = attempt("push", lambda: read_push(r, clone))
        attempt("read_checks", lambda: read_read_checks(r, clone))
        attempt("close_issues", lambda: read_close_issues(r, clone))
        attempt("read_issues", lambda: read_read_issues(r, clone))
        attempt("workflow_dispatch", lambda: read_workflow_dispatch(r, clone))
        if push is not None:
            attempt("open_pull_requests", lambda: read_open_pull_requests(r, push))
            attempt("merge", lambda: read_merge(r, push))
    complete = all(check is not None for check in checks.values())
    error = None
    if not complete:
        error = "; ".join(dict.fromkeys(errors)) or "a read this row depends on did not come back"
    return PairResult(repo_id=repo_id, repository=repository, checks=checks,
                      complete=complete, error=error)


def probe_token(
    value: str,
    repositories: Iterable[tuple[str, str]],
    *,
    send: Any,
    now: datetime,
    clock: Callable[[], float] = _time.monotonic,
    budget_seconds: float = PROBE_BUDGET_SECONDS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Probe one token: the account it acts as, its expiry, and §5.1's eight
    rows for each `(repo_id, "owner/repo")`. GETs only; nothing is written.

    The value is registered with the redaction filter before the first
    request and is held by nothing this returns.
    """
    result = ProbeResult(attempted_at=now, complete=False)
    errors: list[str] = []
    with redaction_literal(value):
        kind = token_kind(value)
        result.kind = kind
        forge = _Forge(value, send, result, timeout)
        started = clock()
        user_complete = True
        if kind != "app_installation":
            # The account the token acts as. An installation token has no user
            # (GitHub answers it 403), so it is not asked.
            try:
                answer = forge.get(f"https://{_forge.GITHUB_API_HOST}/user", "/user")
                if answer.status == 200 and isinstance(answer.data, dict):
                    login = answer.data.get("login")
                    result.forge_login = login if isinstance(login, str) else None
                elif answer.status == 401:
                    result.rejected = True
            except _Unanswered as unanswered:
                user_complete = False
                errors.append(str(unanswered))
        pairs: list[PairResult] = []
        for repo_id, repository in repositories:
            if clock() - started > budget_seconds:
                errors.append(f"{repository}: not probed, the probe's time budget ran out")
                pairs.append(PairResult(repo_id=repo_id, repository=repository,
                                        checks={cap: None for cap in CAPABILITIES},
                                        complete=False, error="not probed: out of time"))
                continue
            pair = _probe_repository(forge, repo_id, repository, kind)
            if pair.error:
                errors.append(f"{repository}: {pair.error}")
            pairs.append(pair)
        forge = None
    result.pairs = tuple(pairs)
    result.complete = user_complete and all(p.complete for p in pairs)
    result.error = "; ".join(errors) if errors else None
    return result


def run_probe(
    record: GitTokenRecord,
    tenant: Tenant,
    repositories: Iterable[tuple[str, str]],
    *,
    tokens: Any,
    send: Any,
    now: datetime,
) -> ProbeResult:
    """Read the record's slot and probe it. The value lives in this frame only.

    A slot that cannot be read -- no value stored yet, or no grant for
    swarm-api to read it (a narrower slot before lane GT4 binds it) -- is a
    probe that did not run: incomplete, with the reason, and nothing sent.
    """
    reader = getattr(tokens, "read_slot", None)
    value = ""
    version: str | None = None
    try:
        if reader is not None:
            slot = reader(tenant, record.provider_suffix)
            value, version = slot.value, slot.version
        elif record.provider_suffix == GIT_PROVIDER and tokens is not None:
            value = tokens.token_for(tenant)
        else:
            return ProbeResult(attempted_at=now, complete=False,
                               error=f"swarm-api has no reader for {record.secret_name}")
    except _forge.NoForgeCredential:
        return ProbeResult(
            attempted_at=now, complete=False,
            error=(f"no value stored in {record.secret_name} yet: store one with "
                   f"{store_command(record.tenant_id, record.provider_suffix)}"),
        )
    except _forge.ForgeReadError as failed:
        # Its sentences are constant and name the secret, never a value.
        return ProbeResult(attempted_at=now, complete=False, error=str(failed))
    except Exception as exc:
        return ProbeResult(attempted_at=now, complete=False,
                           error=f"{record.secret_name} could not be read ({type(exc).__name__})")
    try:
        try:
            result = probe_token(value, repositories, send=send, now=now)
        except Exception as exc:
            # A bug in a reader: named by type, never by text, never raised on.
            log.warning("git token probe failed tenant=%s token_id=%s (%s)",
                        record.tenant_id, record.token_id, type(exc).__name__)
            return ProbeResult(attempted_at=now, complete=False,
                               error=f"the probe failed ({type(exc).__name__})")
        result.read_value = True
        result.last4 = value[-4:] if len(value) >= 8 else None
        result.version = version
        return result
    finally:
        value = ""


# --------------------------------------------------------------------------
# storing and serving a probe
# --------------------------------------------------------------------------

def _check_ref(db: Any, token_id: str, repo_id: str) -> Any:
    return db.collection(CHECKS_COLLECTION).document(f"{token_id}_{repo_id}")


def covers(record: GitTokenRecord, repo_id: str) -> bool:
    """Whether the record's slot is the one for `repo_id` under its scope (§1)."""
    if record.scope is Scope.REPOSITORY:
        return repo_id in record.repo_ids
    if record.scope is Scope.USER:
        return not record.repo_ids or repo_id in record.repo_ids
    return True


def _merge_pair(previous: dict[str, Any] | None, pair: PairResult, tenant_id: str,
                token_id: str, result: ProbeResult) -> dict[str, Any]:
    before = (previous or {}).get("capabilities") or {}
    capabilities: dict[str, Any] = {}
    for cap in CAPABILITIES:
        check = pair.checks.get(cap)
        if check is not None:
            capabilities[cap] = {**check.to_dict(), "verified_at": result.attempted_at}
        elif isinstance(before.get(cap), dict):
            capabilities[cap] = before[cap]
        else:
            capabilities[cap] = {"state": UNKNOWN, "reason": "not measured yet: "
                                 + (pair.error or "the read did not come back"),
                                 "evidence": "", "verified_at": None}
    return {
        "token_id": token_id,
        "tenant_id": tenant_id,
        "repo_id": pair.repo_id,
        "repository": pair.repository,
        "capabilities": capabilities,
        "verified_at": result.attempted_at if pair.complete else (previous or {}).get(
            "verified_at"),
        "attempted_at": result.attempted_at,
        "complete": pair.complete,
        "error": pair.error,
        "expires_at": result.expires_at if result.expiry_read else (previous or {}).get(
            "expires_at"),
        "rate_remaining": result.rate_remaining,
    }


def _pair_api(doc: dict[str, Any]) -> dict[str, Any]:
    capabilities = {}
    counts = {OK: 0, MISSING: 0, UNKNOWN: 0}
    for cap in CAPABILITIES:
        cell = dict((doc.get("capabilities") or {}).get(cap) or {
            "state": UNKNOWN, "reason": "not measured yet", "evidence": "", "verified_at": None})
        cell["verified_at"] = _iso(cell.get("verified_at"))
        counts[cell["state"]] = counts.get(cell["state"], 0) + 1
        capabilities[cap] = cell
    return {
        "repo_id": doc.get("repo_id"),
        "repository": doc.get("repository"),
        "verified_at": _iso(doc.get("verified_at")),
        "attempted_at": _iso(doc.get("attempted_at")),
        "complete": bool(doc.get("complete")),
        "error": doc.get("error"),
        "expires_at": _iso(doc.get("expires_at")),
        "rate_remaining": doc.get("rate_remaining"),
        "capabilities": capabilities,
        "summary": counts,
    }


def probe_summary(record: GitTokenRecord, pair_docs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """What the permission matrix (A) reads for one token: per repository,
    the eight rows with state, reason and evidence, and the probe's status."""
    rows = [_pair_api(doc) for doc in pair_docs
            if doc.get("token_id") == record.token_id and doc.get("tenant_id") == record.tenant_id]
    rows.sort(key=lambda row: (row["repository"] or "", row["repo_id"] or ""))
    return {
        "verified_at": _iso(record.verified_at),
        "attempted_at": _iso(record.probe_attempted_at),
        "complete": record.probe_complete,
        "error": record.probe_error,
        "rate_remaining": record.rate_remaining,
        "repositories": rows,
    }


def _known_repositories(record: GitTokenRecord) -> list[tuple[str, str]]:
    return [(repo_id, name) for repo_id, name in sorted(record.repositories.items(),
                                                         key=lambda kv: kv[1])
            if covers(record, repo_id)]
