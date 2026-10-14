"""A workspace's merge settings: direct or merge train (TRAIN-WS, docs/merge-trains.md §3).

Owner decision 2026-10-14: "the merge train should be a setting users manage
on their own workspace and admins on all workspaces". Settings > Workspace >
Merging:

  * `mode`: `direct` (the merge step merges its own pull request, as before
    this lane -- the default) or `train` (the step registers its pull request
    and the train coordinator, `mergetrain`, lands it in a batch);
  * `train_size`: how many ready pull requests one train carries, 1..10,
    default 4;
  * `max_train_age_minutes`: how long a train's checks may run before it is
    treated as red (bisected, or its one pull request ejected), default 90;
  * a per-repository override, `inherit | direct | train`, kept ON THE
    REPOSITORY REGISTRATION as `merge_mode` (stored null for inherit).

WHERE IT IS STORED, AND WHY NOT ON `Tenant`. `merge_settings/{tenant_id}` is
this module's own collection, and `merge_mode` a field of `repositories/`,
which is swarm-api's own registry (repositories.py), never the frozen
contract. `Tenant` is frozen and has no field for any of this;
docs/contract-change-requests.md asks for one rather than adding it.

WHO (docs/merge-trains.md §3.2). A workspace is a tenant. The owner of a
personal workspace -- `kind: user`, its principal the verified caller -- and a
platform admin may change it; a group workspace has no admin role of its own
in this API yet (repositories.py says the same of `readable_by_tasks`), so
its admins are the platform's. A member who is neither is refused
`merge_settings_owner_required` (403) through `refusals.refuse`, a SWITCHED
refusal that ships report-only (refusals.py). A caller who is not a member at
all is answered the established 404 whatever that switch says: another
workspace's settings are neither served nor confirmed to exist, and nothing a
non-member sends is written (invariant 9).

AUDIT. Every change is ONE `admin_audit` entry -- action
`workspace_merge_settings_set`, `by`, `at`, and `detail.from`/`detail.to`
naming only what changed -- written in the same transaction as the change,
so the settings and their audit cannot disagree. A PUT that changes nothing
writes nothing.

INVARIANTS. A setting is a Firestore document: it holds no capacity and
creates no infrastructure demand (invariant 1). Nothing here accepts an
image, a command or a resource spec (invariant 10): the body is three
scalars and a map of registration ids to one of three words.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field, StrictInt, field_validator, model_validator

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import Forbidden, NotFound
from .refusals import refuse
from .repositories import COLLECTION as REPOSITORIES
from .schemas import StrictModel
from .validation import MERGE_MODE_DIRECT, MERGE_MODE_TRAIN

#: `merge_settings/{tenant_id}`.
COLLECTION = "merge_settings"
#: The override's field on a `repositories/{repo_id}` registration.
REPO_FIELD = "merge_mode"

DIRECT = MERGE_MODE_DIRECT
TRAIN = MERGE_MODE_TRAIN
INHERIT = "inherit"
MODES = (DIRECT, TRAIN)
OVERRIDES = (INHERIT, DIRECT, TRAIN)

#: Direct is the default: it is what every workspace did before this lane,
#: and a train changes how work lands, which the owner chooses.
MODE_DEFAULT = DIRECT
#: Four is the owner's default (2026-10-14): enough to cut CI runs per
#: landing by most of the way, small enough that bisecting a red train costs
#: at most two more trains to find one culprit.
TRAIN_SIZE_DEFAULT = 4
TRAIN_SIZE_RANGE = (1, 10)
#: Ninety minutes is the owner's default: longer than this repository's own
#: full CI run, so a healthy train never times out.
MAX_TRAIN_AGE_DEFAULT = 90
#: At least 15 minutes, because no real CI settles faster and a shorter age
#: would eject green work; at most a day, because a train holds every pull
#: request it carries and a longer one is a stuck train, not a slow one.
MAX_TRAIN_AGE_RANGE = (15, 1440)
#: How many repository overrides one PUT may change: more than any tenant
#: registers, few enough that the transaction stays small.
MAX_OVERRIDES_PER_PUT = 100

#: The `admin_audit` action for any change of a workspace's merge settings.
AUDIT_ACTION = "workspace_merge_settings_set"


class MergeSettingsOwnerRequired(Forbidden):
    """A member of the workspace who is neither its owner nor a platform admin."""

    code = "merge_settings_owner_required"


@dataclass(frozen=True)
class MergeSettings:
    mode: str = MODE_DEFAULT
    train_size: int = TRAIN_SIZE_DEFAULT
    max_train_age_minutes: int = MAX_TRAIN_AGE_DEFAULT

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "train_size": self.train_size,
                "max_train_age_minutes": self.max_train_age_minutes}


def _int_in(value: Any, bounds: tuple[int, int], default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and bounds[0] <= value <= bounds[1]:
        return value
    return default


def settings_of(data: Mapping[str, Any] | None) -> MergeSettings:
    """The settings a stored document says, each malformed value its default.

    The document is written only through this module, but a value this code
    does not know must never decide how work lands: it reads as the default."""
    data = data or {}
    mode = data.get("mode")
    return MergeSettings(
        mode=mode if mode in MODES else MODE_DEFAULT,
        train_size=_int_in(data.get("train_size"), TRAIN_SIZE_RANGE, TRAIN_SIZE_DEFAULT),
        max_train_age_minutes=_int_in(data.get("max_train_age_minutes"), MAX_TRAIN_AGE_RANGE,
                                      MAX_TRAIN_AGE_DEFAULT),
    )


def repo_mode_of(registration: Mapping[str, Any] | None) -> str | None:
    """A registration's override, or None (inherit). Malformed reads as none."""
    value = (registration or {}).get(REPO_FIELD)
    return value if value in MODES else None


def effective_mode(settings: MergeSettings, registration: Mapping[str, Any] | None) -> str:
    """The repository's override when it has one, the workspace's mode otherwise."""
    return repo_mode_of(registration) or settings.mode


class MergeSettingsPut(StrictModel):
    """PUT /v1/workspaces/{id}/merge-settings. Every field optional; a field
    left out is unchanged, and a body that names nothing is refused."""

    mode: Literal["direct", "train"] | None = None
    train_size: StrictInt | None = Field(
        default=None, ge=TRAIN_SIZE_RANGE[0], le=TRAIN_SIZE_RANGE[1])
    max_train_age_minutes: StrictInt | None = Field(
        default=None, ge=MAX_TRAIN_AGE_RANGE[0], le=MAX_TRAIN_AGE_RANGE[1])
    repositories: dict[str, Literal["inherit", "direct", "train"]] | None = None

    @field_validator("repositories")
    @classmethod
    def _bounded(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and len(value) > MAX_OVERRIDES_PER_PUT:
            raise ValueError(f"repositories may change at most {MAX_OVERRIDES_PER_PUT} "
                             "overrides in one request")
        return value

    @model_validator(mode="after")
    def _names_something(self) -> "MergeSettingsPut":
        if (self.mode is None and self.train_size is None
                and self.max_train_age_minutes is None and not self.repositories):
            raise ValueError("name at least one of mode, train_size, max_train_age_minutes "
                             "or repositories")
        return self


# --------------------------------------------------------------------------
# who may read and change a workspace's settings
# --------------------------------------------------------------------------

def is_owner(auth: AuthContext, tenant: Tenant) -> bool:
    """The caller owns `tenant`: a personal workspace whose principal they are."""
    email = (auth.email or "").strip().lower()
    return bool(email) and tenant.kind == "user" \
        and (tenant.principal or "").strip().lower() == email


def is_member(auth: AuthContext, tenant_id: str) -> bool:
    """The caller may act in `tenant_id`: their resolved tenant, or one of the
    tenants their verified memberships offer (`AuthContext.tenant_choices`)."""
    return auth.tenant_id == tenant_id or any(
        choice[0] == tenant_id for choice in auth.tenant_choices)


def authorize(auth: AuthContext, tenant: Tenant | None, tenant_id: str, *, write: bool) -> Tenant:
    """The workspace, when the caller may read it (and, `write`, change it).

    Not a member, not the owner, not an admin: the established 404, the same
    as a workspace that does not exist. A member who may not change it: the
    switched 403, which off lets the change through and logs it."""
    if tenant is None:
        raise NotFound(f"workspace {tenant_id!r} not found")
    admin = bool(auth.is_admin)
    owner = is_owner(auth, tenant)
    if not (admin or owner or is_member(auth, tenant_id)):
        raise NotFound(f"workspace {tenant_id!r} not found")
    if write and not (admin or owner):
        refuse(MergeSettingsOwnerRequired(
            "Only the workspace's owner or a platform admin may change its merge settings. "
            "Nothing was changed.",
            detail={"tenant_id": tenant_id},
        ))
    return tenant


def can_edit(auth: AuthContext, tenant: Tenant) -> bool:
    return bool(auth.is_admin) or is_owner(auth, tenant)


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class MergeSettingsStore:
    """`merge_settings/` and the `merge_mode` field of `repositories/`."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def get(self, tenant_id: str) -> MergeSettings:
        snap = self._ref(tenant_id).get()
        return settings_of(snap.to_dict() if snap.exists else None)

    def document(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._ref(tenant_id).get()
        return snap.to_dict() if snap.exists else None

    def registrations(self, tenant_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """The tenant's registrations, by id. The filter again in the
        application, as `Repositories.list` does."""
        query = self._db.collection(REPOSITORIES).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)).limit(limit)
        rows = [snap.to_dict() or {} for snap in query.stream()]
        return sorted((r for r in rows if r.get("tenant_id") == tenant_id),
                      key=lambda r: str(r.get("repo_id")))

    def mode_for(self, tenant_id: str, registration: Mapping[str, Any] | None) -> str:
        """The effective mode of one repository, read now."""
        return effective_mode(self.get(tenant_id), registration)

    def put(self, tenant_id: str, body: MergeSettingsPut, *, by: str) -> MergeSettings:
        """Apply `body` and its audit in one transaction; returns the settings.

        Every repository named must be a registration of `tenant_id`, or the
        whole change is refused 404 with nothing written -- another tenant's
        registration is never touched, nor confirmed to exist."""
        settings_ref = self._ref(tenant_id)
        overrides = dict(body.repositories or {})
        repo_refs = {rid: self._db.collection(REPOSITORIES).document(rid)
                     for rid in sorted(overrides)}
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> MergeSettings:
            snap = _snapshot(txn.get(settings_ref))
            stored = snap.to_dict() if snap.exists else None
            registrations: dict[str, dict[str, Any]] = {}
            for rid, ref in repo_refs.items():
                repo_snap = _snapshot(txn.get(ref))
                data = repo_snap.to_dict() if repo_snap.exists else None
                if data is None or data.get("tenant_id") != tenant_id:
                    raise NotFound(f"repository {rid!r} is not registered in this workspace",
                                   detail={"repo_id": rid})
                registrations[rid] = data
            before = settings_of(stored)
            after = MergeSettings(
                mode=body.mode if body.mode is not None else before.mode,
                train_size=body.train_size if body.train_size is not None else before.train_size,
                max_train_age_minutes=(body.max_train_age_minutes
                                       if body.max_train_age_minutes is not None
                                       else before.max_train_age_minutes),
            )
            old, new = before.as_dict(), after.as_dict()
            changed_from = {k: old[k] for k in old if old[k] != new[k]}
            changed_to = {k: new[k] for k in new if old[k] != new[k]}
            repo_from: dict[str, str | None] = {}
            repo_to: dict[str, str | None] = {}
            for rid, data in registrations.items():
                wanted = None if overrides[rid] == INHERIT else overrides[rid]
                current = repo_mode_of(data)
                if current != wanted or (wanted is None and data.get(REPO_FIELD) is not None):
                    repo_from[rid], repo_to[rid] = current, wanted
            if repo_to:
                changed_from["repositories"] = repo_from
                changed_to["repositories"] = repo_to
            if not changed_to:
                return after
            txn.set(settings_ref, {"tenant_id": tenant_id, **new,
                                   "updated_at": now, "updated_by": by})
            for rid, wanted in repo_to.items():
                txn.update(repo_refs[rid], {REPO_FIELD: wanted, "updated_at": now})
            txn.set(self._db.collection(AUDIT_COLLECTION).document(), {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "from": changed_from, "to": changed_to},
            })
            return after

        return _apply(transaction)


def view(tenant_id: str, settings: MergeSettings, document: Mapping[str, Any] | None,
         registrations: list[Mapping[str, Any]], *, editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the settings, who may change them, and every
    registered repository's override and the mode it effectively merges in."""
    updated = (document or {}).get("updated_at")
    return {
        "tenant_id": tenant_id,
        **settings.as_dict(),
        "updated_at": updated.isoformat() if isinstance(updated, datetime) else None,
        "can_edit": editable,
        "repositories": [
            {
                "repo_id": r.get("repo_id"),
                "repository": f"{r.get('owner')}/{r.get('repo')}",
                "override": repo_mode_of(r) or INHERIT,
                "effective_mode": effective_mode(settings, r),
            }
            for r in registrations
        ],
    }
