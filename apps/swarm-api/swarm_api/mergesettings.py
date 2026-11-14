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
    REPOSITORY REGISTRATION as `merge_mode` (stored null for inherit);
  * the train's gather and parity settings (docs/design/merging-without-a-
    laptop.md §7): `gather_min`, `gather_wait_minutes`,
    `required_start_minutes` and `base_red_wait_minutes`, each a bounded
    scalar, with a per-repository override of the same keys kept on the
    registration as the map `merge_train` (a key left out inherits).

THE TRAIN SETTINGS ARE NOT SIGNED YET. The frozen
`swarm_common.specsign.SETTINGS_MAC_FIELDS["merge"]` covers only `mode`,
`train_size` and `max_train_age_minutes`, and a key it does not cover "must
not be acted on". So for a tenant cut over in SETTINGS_MAC_TENANTS the four
train settings read as their defaults (`TRAIN_FIELDS`, `_covered_only`)
until the contract covers them -- a request in the pull request that added
them, never an edit of the frozen file. A tenant not cut over reads them as
stored, as it reads the three covered ones.

WHERE IT IS STORED, AND WHY NOT ON `Tenant`. `workspace_settings/{tenant_id}.merge` is
this module's own collection, and `merge_mode` a field of `repositories/`,
which is swarm-api's own registry (repositories.py), never the frozen
contract. `Tenant` is frozen and has no field for any of this;
docs/contract-change-requests.md asks for one rather than adding it.

WHO (docs/merge-trains.md §3.2; docs/design/workspace-roles.md §4.1, Q13). A
workspace is a tenant. The owner of a personal workspace -- `kind: user`, its
principal the verified caller -- changes it; in a group workspace its `admin`
does: a workspace admin (`workspace_secrets.workspace_admin`, read fresh) or a
platform admin, who holds `admin` in every group workspace (Q5). A member who
is neither is refused `merge_settings_owner_required` (403) through
`refusals.refuse`, a SWITCHED refusal that ships report-only (refusals.py):
merge settings keep their own switch rather than `role_admin_required` (§4.9),
so a role read that fails is "not an admin" here and meets that switch. A caller who is not a member at
all is answered the established 404 whatever that switch says: another
workspace's settings are neither served nor confirmed to exist, and nothing a
non-member sends is written (invariant 9).

AUDIT. Every change is ONE `admin_audit` entry -- action
`workspace_merge_settings_set`, `by`, `at`, and `detail.from`/`detail.to`
naming only what changed -- written in the same transaction as the change,
so the settings and their audit cannot disagree. A PUT that changes nothing
writes nothing.

SIGNED (#1457). A tenant worker can write this document directly, so every
write stores `merge.mac`, swarm-api's MAC over the three settings
(`swarm_api.judgment.block_mac`, block "merge"), and for a tenant cut over in
SETTINGS_MAC_TENANTS a map whose MAC does not verify reads as the defaults:
direct, which is how every workspace merged before trains existed.

INVARIANTS. A setting is a Firestore document: it holds no capacity and
creates no infrastructure demand (invariant 1). Nothing here accepts an
image, a command or a resource spec (invariant 10): the body is three
scalars and a map of registration ids to one of three words.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Literal, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field, StrictInt, field_validator, model_validator

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import Forbidden, NotFound, RoleUnavailable, ValidationFailed
from .judgment import MAC_FIELD, SettingsMac, block_mac, mac_enforced, trusted_block
from .refusals import refuse
from .repositories import COLLECTION as REPOSITORIES
from .roles import ADMIN, RoleStore, at_least, with_tenant
from .schemas import StrictModel
from .validation import MERGE_MODE_DIRECT, MERGE_MODE_TRAIN

#: The workspace's one settings document, `workspace_settings/{tenant_id}`,
#: the document JUDGE-SELECT (`swarm_api.judgment`) already keeps its
#: `judgment_backend` in: a workspace has one settings document, one field
#: per setting. The merge settings are the map under `merge`; writes use
#: `merge=True` so they never touch another setting's field.
COLLECTION = "workspace_settings"
FIELD = "merge"
#: The block name `swarm_common.specsign.SETTINGS_MAC_FIELDS` signs this map under.
MAC_BLOCK = FIELD
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
#: Two riders is the smallest batch worth a train: one rider is direct mode
#: with extra steps, and two halve the CI runs without holding a quiet
#: repository. Never more than the train size (a train cannot carry more).
GATHER_MIN_DEFAULT = 2
GATHER_MIN_RANGE = (1, TRAIN_SIZE_RANGE[1])
#: A ready pull request boards after ten minutes even when `gather_min` is
#: not reached, so a lone change is never held long. The repository-level
#: train's 6-or-20 suits a busy repository, not a typical one.
GATHER_WAIT_DEFAULT = 10
GATHER_WAIT_RANGE = (0, 60)
#: A required check with no run on the composed head this long after compose
#: never starts (it skips drafts, or filters on branch names): the train
#: fails `required_check_not_started` rather than waiting out its max age.
REQUIRED_START_DEFAULT = 15
REQUIRED_START_RANGE = (5, 60)
#: How long a red train is held, not bisected, while the base's own required
#: checks are red at its head: the script's MERGE_TRAIN_MAIN_RED_WAIT_MINUTES.
BASE_RED_WAIT_DEFAULT = 60
BASE_RED_WAIT_RANGE = (0, 240)
#: The train settings: not covered by the frozen MAC (module docstring), and
#: the keys of a registration's `merge_train` override.
TRAIN_FIELDS = ("gather_min", "gather_wait_minutes", "required_start_minutes",
                "base_red_wait_minutes")
TRAIN_BOUNDS = {
    "gather_min": (GATHER_MIN_RANGE, GATHER_MIN_DEFAULT),
    "gather_wait_minutes": (GATHER_WAIT_RANGE, GATHER_WAIT_DEFAULT),
    "required_start_minutes": (REQUIRED_START_RANGE, REQUIRED_START_DEFAULT),
    "base_red_wait_minutes": (BASE_RED_WAIT_RANGE, BASE_RED_WAIT_DEFAULT),
}
#: A registration's override of the train settings.
REPO_TRAIN_FIELD = "merge_train"
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
    gather_min: int = GATHER_MIN_DEFAULT
    gather_wait_minutes: int = GATHER_WAIT_DEFAULT
    required_start_minutes: int = REQUIRED_START_DEFAULT
    base_red_wait_minutes: int = BASE_RED_WAIT_DEFAULT

    def as_dict(self) -> dict[str, Any]:
        """The MAC-covered settings (`judgment.WRITER_FIELDS["merge"]`)."""
        return {"mode": self.mode, "train_size": self.train_size,
                "max_train_age_minutes": self.max_train_age_minutes}

    def train_dict(self) -> dict[str, int]:
        """The train settings, `TRAIN_FIELDS`."""
        return {key: getattr(self, key) for key in TRAIN_FIELDS}


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
    size = _int_in(data.get("train_size"), TRAIN_SIZE_RANGE, TRAIN_SIZE_DEFAULT)
    return _bounded_gather(MergeSettings(
        mode=mode if mode in MODES else MODE_DEFAULT,
        train_size=size,
        max_train_age_minutes=_int_in(data.get("max_train_age_minutes"), MAX_TRAIN_AGE_RANGE,
                                      MAX_TRAIN_AGE_DEFAULT),
        **{key: _int_in(data.get(key), *TRAIN_BOUNDS[key]) for key in TRAIN_FIELDS},
    ))


def _bounded_gather(settings: MergeSettings) -> MergeSettings:
    """`gather_min` above the train size is malformed: its default, never
    more than the train size."""
    if settings.gather_min <= settings.train_size:
        return settings
    return replace(settings, gather_min=min(GATHER_MIN_DEFAULT, settings.train_size))


def repo_train_of(registration: Mapping[str, Any] | None) -> dict[str, int]:
    """A registration's train-setting overrides: each well-formed key; a
    malformed or absent one inherits."""
    stored = (registration or {}).get(REPO_TRAIN_FIELD)
    if not isinstance(stored, Mapping):
        return {}
    out: dict[str, int] = {}
    for key in TRAIN_FIELDS:
        bounds, _default = TRAIN_BOUNDS[key]
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool) \
                and bounds[0] <= value <= bounds[1]:
            out[key] = value
    return out


def effective_settings(settings: MergeSettings,
                       registration: Mapping[str, Any] | None) -> MergeSettings:
    """The settings one repository's train runs with: the workspace's, its
    mode and train settings overridden by the registration's own."""
    return _bounded_gather(replace(settings, mode=effective_mode(settings, registration),
                                   **repo_train_of(registration)))


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
    gather_min: StrictInt | None = Field(
        default=None, ge=GATHER_MIN_RANGE[0], le=GATHER_MIN_RANGE[1])
    gather_wait_minutes: StrictInt | None = Field(
        default=None, ge=GATHER_WAIT_RANGE[0], le=GATHER_WAIT_RANGE[1])
    required_start_minutes: StrictInt | None = Field(
        default=None, ge=REQUIRED_START_RANGE[0], le=REQUIRED_START_RANGE[1])
    base_red_wait_minutes: StrictInt | None = Field(
        default=None, ge=BASE_RED_WAIT_RANGE[0], le=BASE_RED_WAIT_RANGE[1])
    repositories: dict[str, Literal["inherit", "direct", "train"]] | None = None
    #: Per-registration train settings: a key set overrides, a key null
    #: inherits again, a key left out is unchanged.
    repository_train: dict[str, "RepositoryTrainPut"] | None = None

    @field_validator("repositories", "repository_train")
    @classmethod
    def _bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(value) > MAX_OVERRIDES_PER_PUT:
            raise ValueError(f"repositories may change at most {MAX_OVERRIDES_PER_PUT} "
                             "overrides in one request")
        return value

    @model_validator(mode="after")
    def _names_something(self) -> "MergeSettingsPut":
        if (self.mode is None and self.train_size is None
                and self.max_train_age_minutes is None and not self.repositories
                and all(getattr(self, key) is None for key in TRAIN_FIELDS)
                and not self.repository_train):
            raise ValueError("name at least one of mode, train_size, max_train_age_minutes, "
                             f"{', '.join(TRAIN_FIELDS)}, repositories or repository_train")
        return self


class RepositoryTrainPut(StrictModel):
    """One registration's train settings, each bounded as the workspace's."""

    gather_min: StrictInt | None = Field(
        default=None, ge=GATHER_MIN_RANGE[0], le=GATHER_MIN_RANGE[1])
    gather_wait_minutes: StrictInt | None = Field(
        default=None, ge=GATHER_WAIT_RANGE[0], le=GATHER_WAIT_RANGE[1])
    required_start_minutes: StrictInt | None = Field(
        default=None, ge=REQUIRED_START_RANGE[0], le=REQUIRED_START_RANGE[1])
    base_red_wait_minutes: StrictInt | None = Field(
        default=None, ge=BASE_RED_WAIT_RANGE[0], le=BASE_RED_WAIT_RANGE[1])


MergeSettingsPut.model_rebuild()


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


def group_admin(roles: RoleStore | None, auth: AuthContext, tenant: Tenant, *,
                fresh: bool) -> bool:
    """The caller holds `admin` in GROUP workspace `tenant`, of which they are
    a member (Q13). Asked without a refusal of its own -- the caller's gate
    decides what "no" costs -- and a role that could not be read is "no"."""
    if roles is None or tenant.kind == "user" or not is_member(auth, tenant.tenant_id):
        return False
    if auth.tenant_id != tenant.tenant_id:
        auth = with_tenant(auth, tenant.tenant_id, tenant.principal or "")
    try:
        me = roles.resolve(auth, fresh=fresh)
    except RoleUnavailable:
        return False
    return at_least(me.role, ADMIN)


def authorize(auth: AuthContext, tenant: Tenant | None, tenant_id: str, *, write: bool,
              roles: RoleStore | None = None) -> Tenant:
    """The workspace, when the caller may read it (and, `write`, change it).

    Not a member, not the owner, not an admin: the established 404, the same
    as a workspace that does not exist. A member who is not its admin: the
    switched 403, which off lets the change through and logs it."""
    if tenant is None:
        raise NotFound(f"workspace {tenant_id!r} not found")
    admin = bool(auth.is_admin)
    owner = is_owner(auth, tenant)
    if not (admin or owner or is_member(auth, tenant_id)):
        raise NotFound(f"workspace {tenant_id!r} not found")
    if write and not (admin or owner or group_admin(roles, auth, tenant, fresh=True)):
        refuse(MergeSettingsOwnerRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its "
            "merge settings. Nothing was changed.",
            detail={"tenant_id": tenant_id},
        ))
    return tenant


def can_edit(auth: AuthContext, tenant: Tenant, roles: RoleStore | None = None) -> bool:
    return bool(auth.is_admin) or is_owner(auth, tenant) \
        or group_admin(roles, auth, tenant, fresh=False)


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class MergeSettingsStore:
    """`workspace_settings/` and the `merge_mode` field of `repositories/`."""

    def __init__(self, db: Any, *, now: Callable[[], datetime], mac: SettingsMac) -> None:
        self._db = db
        self._now = now
        self._mac = mac

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def get(self, tenant_id: str) -> MergeSettings:
        return settings_of(self.document(tenant_id))

    def _covered_only(self, tenant_id: str, block: dict[str, Any]) -> dict[str, Any]:
        """For a tenant cut over, the block without the train settings the
        frozen MAC does not cover (module docstring): they read as defaults."""
        if not mac_enforced(tenant_id, self._mac.tenants):
            return block
        return {key: value for key, value in block.items() if key not in TRAIN_FIELDS}

    def document(self, tenant_id: str) -> dict[str, Any] | None:
        """The `merge` map of the workspace's settings document, or None --
        also None for a tenant cut over whose map's MAC does not verify."""
        snap = self._ref(tenant_id).get()
        value = (snap.to_dict() or {}).get(FIELD) if snap.exists else None
        if not isinstance(value, dict):
            return None
        return self._covered_only(tenant_id,
                                  trusted_block(tenant_id, MAC_BLOCK, value, self._mac)) or None

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
        train_overrides = dict(body.repository_train or {})
        repo_refs = {rid: self._db.collection(REPOSITORIES).document(rid)
                     for rid in sorted(set(overrides) | set(train_overrides))}
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> MergeSettings:
            snap = _snapshot(txn.get(settings_ref))
            whole = (snap.to_dict() or {}) if snap.exists else {}
            stored = whole.get(FIELD) if isinstance(whole.get(FIELD), dict) else None
            stored = (self._covered_only(tenant_id, trusted_block(tenant_id, MAC_BLOCK, stored,
                                                                  self._mac))
                      if stored is not None else None)
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
                **{key: getattr(body, key) if getattr(body, key) is not None
                   else getattr(before, key) for key in TRAIN_FIELDS},
            )
            if after.gather_min > after.train_size:
                raise ValidationFailed(
                    f"gather_min ({after.gather_min}) may not exceed train_size "
                    f"({after.train_size}): a train cannot carry more than its size",
                    detail={"gather_min": after.gather_min, "train_size": after.train_size})
            old = {**before.as_dict(), **before.train_dict()}
            new = {**after.as_dict(), **after.train_dict()}
            changed_from = {k: old[k] for k in old if old[k] != new[k]}
            changed_to = {k: new[k] for k in new if old[k] != new[k]}
            repo_from: dict[str, str | None] = {}
            repo_to: dict[str, str | None] = {}
            for rid, data in registrations.items():
                if rid not in overrides:
                    continue
                wanted = None if overrides[rid] == INHERIT else overrides[rid]
                current = repo_mode_of(data)
                if current != wanted or (wanted is None and data.get(REPO_FIELD) is not None):
                    repo_from[rid], repo_to[rid] = current, wanted
            train_from: dict[str, dict[str, int]] = {}
            train_to: dict[str, dict[str, int]] = {}
            for rid, put in train_overrides.items():
                current_train = repo_train_of(registrations[rid])
                wanted_train = dict(current_train)
                for key in put.model_fields_set & set(TRAIN_FIELDS):
                    value = getattr(put, key)
                    if value is None:
                        wanted_train.pop(key, None)
                    else:
                        wanted_train[key] = value
                if wanted_train != current_train:
                    train_from[rid], train_to[rid] = current_train, wanted_train
            if repo_to:
                changed_from["repositories"] = repo_from
                changed_to["repositories"] = repo_to
            if train_to:
                changed_from["repository_train"] = train_from
                changed_to["repository_train"] = train_to
            if not changed_to:
                return after
            block: dict[str, Any] = {"tenant_id": tenant_id, **new,
                                     "updated_at": now, "updated_by": by}
            mac = block_mac(tenant_id, MAC_BLOCK, after.as_dict(), self._mac.keys)
            if mac is not None:
                block[MAC_FIELD] = mac
            txn.set(settings_ref, {FIELD: block}, merge=True)
            for rid, wanted in repo_to.items():
                txn.update(repo_refs[rid], {REPO_FIELD: wanted, "updated_at": now})
            for rid, wanted_train in train_to.items():
                txn.update(repo_refs[rid], {REPO_TRAIN_FIELD: wanted_train or None,
                                            "updated_at": now})
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
        **settings.train_dict(),
        "updated_at": updated.isoformat() if isinstance(updated, datetime) else None,
        "can_edit": editable,
        "repositories": [
            {
                "repo_id": r.get("repo_id"),
                "repository": f"{r.get('owner')}/{r.get('repo')}",
                "override": repo_mode_of(r) or INHERIT,
                "effective_mode": effective_mode(settings, r),
                "train": repo_train_of(r),
            }
            for r in registrations
        ],
    }
