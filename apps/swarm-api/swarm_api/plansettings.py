"""The plan-review settings: diagrams in plans, the plan chat's model class and
the plan review caps (docs/design/plans-pipeline.md §6.4, §7.8; lane P9).

The one place every other plans lane reads them from. Three levels, the
narrowest that is set winning:

  * PLATFORM. `plan_diagrams_default`, `plan_chat_model_class` and
    `plan_review_caps` in `store.PLATFORM_SETTINGS_DEFAULTS` (the
    `control/settings` document `GET/PUT /v1/admin/settings` serves and a
    platform admin sets). A stored value out of its range or of the wrong type
    reads as the default (`platform_value`), so an admin's typo written
    straight to Firestore never lifts a cap.
  * WORKSPACE. `workspace_settings/{tenant_id}.plans`: `diagrams` (`on` |
    `off`, absent for the platform's) and `chat_model_class` (a class, absent
    for the platform's), set by the workspace's admin through
    `PUT /v1/workspaces/{tenant_id}/plan-settings`. The caps have no workspace
    level: the route serves them read-only to any member and refuses a `caps`
    key by name as the platform's.
  * SCHEDULE. The schedule document's `plan_diagrams`: `inherit` | `on` |
    `off`, default `inherit` (`schedules.py`), set through the schedule's
    PATCH. Diagrams only: the owner named no schedule level for the model or
    the caps (§7.8 "Why no schedule level").

`effective_plan_settings` resolves each setting to its value and the level it
came from; an `inherit` schedule reports the level it inherited from, never
`schedule`. A run made by hand has no schedule and reads the workspace's.

WHO. Read: any member of the workspace, its owner, a platform admin; anyone
else the established 404, as for a workspace that does not exist (invariant
9). Write: the workspace's owner (personal), its group `admin` read fresh, or
a platform admin -- `qasettings`' rule -- and anyone else in it 403
`role_admin_required`, raised directly as the role write routes raise it.

AUDIT. One `admin_audit` entry, `workspace_plan_settings_set`, naming the keys
that changed and both values, in the same transaction as the change. A PUT
that changes nothing writes nothing.

SIGNED WHEN THE CONTRACT LISTS THE BLOCK. A tenant worker can write the
settings document directly, so the design MACs the `plans` block as every
writer there does (tenant-settings-integrity.md §3.4). The MAC covers only the
blocks the FROZEN `swarm_common.specsign.SETTINGS_MAC_FIELDS` lists, and
`plans` is not one yet: listing it is a contract change this lane requests in
its PR rather than makes. `SIGNED` follows the listing, as `budget.SIGNED`
does, so the day the block is listed every write carries its MAC and every
read for a tenant on the cutover acts only on a block that verifies, with no
change here. Until then the reader acts on a stored value only when it is one
the workspace's admin could have chosen, so the worst a forger achieves is a
choice the admin had (diagrams on or off, a class the profile carries).

INVARIANTS. Every value here is a Firestore field: nothing admits, reserves
or runs work (invariant 1), and a model class is named BY NAME from the ones
the frozen `claude-code` profile carries, never a model ID, an image or a
resource (invariant 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator, model_validator
from swarm_common.admission import _snapshot
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.specsign import SETTINGS_MAC_FIELDS

from .admins import AUDIT_COLLECTION
from .errors import NotFound, RoleAdminRequired
from .judgment import MAC_FIELD, SettingsMac, block_mac, trusted_block

if TYPE_CHECKING:  # `schemas` imports this module, and `auth` imports `store`
    from .auth import AuthContext
    from .roles import RoleStore

#: The workspace's settings document (`mergesettings.COLLECTION`'s), and this
#: module's map in it, written with `merge=True` so no other setting moves.
COLLECTION = "workspace_settings"
FIELD = "plans"
MAC_BLOCK = FIELD
#: True once `SETTINGS_MAC_FIELDS` lists the block (the contract request in
#: this lane's PR); `budget.SIGNED`'s rule.
SIGNED = MAC_BLOCK in SETTINGS_MAC_FIELDS
AUDIT_ACTION = "workspace_plan_settings_set"

#: The platform keys (`store.PLATFORM_SETTINGS_DEFAULTS`).
DIAGRAMS_SETTING = "plan_diagrams_default"
CHAT_MODEL_SETTING = "plan_chat_model_class"
CAPS_SETTING = "plan_review_caps"
PLATFORM_KEYS = (DIAGRAMS_SETTING, CHAT_MODEL_SETTING, CAPS_SETTING)

#: The owner 2026-10-28 (Q2): "by default on".
DIAGRAMS_DEFAULT = True
#: The owner 2026-10-28 (Q4): Sonnet by default.
CHAT_MODEL_DEFAULT = "sonnet"
#: The owner 2026-10-28 (Q5): 5 / 20 / 40 / $25, "configurable in platform".
CAPS_DEFAULT: dict[str, Any] = {"replans": 5, "diagram_asks": 20, "chat_turns": 40,
                                "review_usd": 25.00}
#: The accepted range of each cap, inclusive (§7.8). 0 is accepted: it refuses
#: that kind of ask on every run, how a platform admin turns a costly feature
#: off without a deploy. The tops are four times the defaults, rounded, so a
#: typo cannot lift a cap past what any review needs.
CAPS_RANGES: dict[str, tuple[float, float]] = {
    "replans": (0, 20), "diagram_asks": (0, 100), "chat_turns": (0, 200),
    "review_usd": (0, 500.00)}
CAP_KEYS = tuple(CAPS_DEFAULT)
#: The caps counted in asks; `review_usd` alone is dollars.
_COUNT_CAPS = ("replans", "diagram_asks", "chat_turns")

#: The profile every plan-review agent runs on (§7.2), whose classes are the
#: ones a chat model may name.
CHAT_PROFILE = "claude-code"

PLATFORM = "platform"
WORKSPACE = "workspace"
SCHEDULE = "schedule"
ON = "on"
OFF = "off"
INHERIT = "inherit"
WORKSPACE_DIAGRAMS = (ON, OFF)
SCHEDULE_DIAGRAMS = (INHERIT, ON, OFF)
#: The schedule document's field (`schedules.py`).
SCHEDULE_FIELD = "plan_diagrams"


def chat_model_classes() -> tuple[str, ...]:
    """The classes the plan chat may run at: the ones `claude-code` carries."""
    return tuple(RUNNER_PROFILES[CHAT_PROFILE].model_classes)


def chat_model_class_problem(value: Any) -> str | None:
    """Why `value` may not be saved as a chat model class, or None."""
    if not isinstance(value, str) or value not in chat_model_classes():
        return (f"plan_chat_model_class {value!r} is not a class {CHAT_PROFILE} carries; "
                f"it carries {list(chat_model_classes())}")
    return None


def _cap_valid(key: str, value: Any) -> bool:
    low, high = CAPS_RANGES[key]
    if isinstance(value, bool):
        return False
    if key in _COUNT_CAPS and not isinstance(value, int):
        return False
    if not isinstance(value, (int, float)):
        return False
    return low <= value <= high


def caps_problem(value: Any) -> str | None:
    """Why `value` may not be saved as caps (some or all of them), or None."""
    if not isinstance(value, Mapping) or not value:
        return "plan_review_caps names at least one of " + ", ".join(CAP_KEYS)
    unknown = sorted(set(value) - set(CAP_KEYS))
    if unknown:
        return f"plan_review_caps has no {', '.join(unknown)}; it has {', '.join(CAP_KEYS)}"
    for key, cap in value.items():
        if not _cap_valid(key, cap):
            low, high = CAPS_RANGES[key]
            kind = "an integer" if key in _COUNT_CAPS else "a number"
            return f"plan_review_caps.{key} is {kind} from {low:g} to {high:g}"
    return None


def caps_of(value: Any) -> dict[str, Any]:
    """The caps a stored map says, each malformed or out-of-range cap its
    default, with who set them (`set_by`, `set_at`) when the map says."""
    stored = value if isinstance(value, Mapping) else {}
    out: dict[str, Any] = {}
    for key in CAP_KEYS:
        cap = stored.get(key)
        cap = cap if _cap_valid(key, cap) else CAPS_DEFAULT[key]
        out[key] = float(cap) if key == "review_usd" else cap
    set_by = stored.get("set_by")
    out["set_by"] = set_by if isinstance(set_by, str) else None
    out["set_at"] = stored.get("set_at")
    return out


def platform_value(key: str, data: Mapping[str, Any]) -> Any:
    """One of this module's platform settings as stored, or its default when
    unset, of the wrong type or out of its range (`store._platform_setting`)."""
    value = data.get(key)
    if key == DIAGRAMS_SETTING:
        return value if isinstance(value, bool) else DIAGRAMS_DEFAULT
    if key == CHAT_MODEL_SETTING:
        return value if chat_model_class_problem(value) is None else CHAT_MODEL_DEFAULT
    return caps_of(value)


def platform_changes(changes: Mapping[str, Any], current: Mapping[str, Any], *, by: str,
                     now: datetime) -> dict[str, Any]:
    """`changes` with this module's keys checked and made whole, for
    `Store.set_platform_settings`. Raises ValueError naming the first refusal.

    The caps named replace theirs and the others keep their current figure;
    the map is written whole with who set it, so a merge write never leaves a
    stale cap beside a new one and the workspace's read can say who set them."""
    out = dict(changes)
    if DIAGRAMS_SETTING in changes and not isinstance(changes[DIAGRAMS_SETTING], bool):
        raise ValueError(f"{DIAGRAMS_SETTING} is true or false")
    if CHAT_MODEL_SETTING in changes:
        why = chat_model_class_problem(changes[CHAT_MODEL_SETTING])
        if why is not None:
            raise ValueError(why)
    if CAPS_SETTING in changes:
        why = caps_problem(changes[CAPS_SETTING])
        if why is not None:
            raise ValueError(why)
        whole = {key: current.get(key, CAPS_DEFAULT[key]) for key in CAP_KEYS}
        whole.update(changes[CAPS_SETTING])
        whole["review_usd"] = float(whole["review_usd"])
        out[CAPS_SETTING] = {**whole, "set_by": by, "set_at": now}
    return out


class PlanReviewCapsRequest(BaseModel):
    """`plan_review_caps` in `PUT /v1/admin/settings`: the caps named replace
    theirs, the others keep their figure. Ranges: `CAPS_RANGES`."""

    model_config = ConfigDict(extra="forbid")

    replans: StrictInt | None = Field(default=None, ge=0, le=20)
    diagram_asks: StrictInt | None = Field(default=None, ge=0, le=100)
    chat_turns: StrictInt | None = Field(default=None, ge=0, le=200)
    review_usd: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=500)

    @model_validator(mode="after")
    def _names_one(self) -> "PlanReviewCapsRequest":
        if all(getattr(self, key) is None for key in CAP_KEYS):
            raise ValueError("plan_review_caps names at least one of " + ", ".join(CAP_KEYS))
        return self


def validate_chat_model_class(value: str | None, *, setting: str) -> str | None:
    """A pydantic field validator's body: the class, or ValueError naming the
    ones `claude-code` carries."""
    if value is None:
        return None
    if value not in chat_model_classes():
        raise ValueError(f"{setting} {value!r} is not a class {CHAT_PROFILE} carries; "
                         f"it carries {list(chat_model_classes())}")
    return value


# --------------------------------------------------------------------------
# the workspace level
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspacePlans:
    """What a workspace's `plans` map says; None for a level left to the platform."""

    diagrams: str | None = None
    chat_model_class: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"diagrams": self.diagrams, "chat_model_class": self.chat_model_class}


def workspace_of(data: Mapping[str, Any] | None) -> WorkspacePlans:
    """The workspace's choices as stored, a value it could not have chosen as
    unset. A class the profile no longer carries is kept: the resolution falls
    through it and says so (§7.8)."""
    data = data or {}
    diagrams = data.get("diagrams")
    model = data.get("chat_model_class")
    return WorkspacePlans(
        diagrams=diagrams if diagrams in WORKSPACE_DIAGRAMS else None,
        chat_model_class=model if isinstance(model, str) and model else None,
    )


class PlanSettingsPut(BaseModel):
    """`PUT /v1/workspaces/{tenant_id}/plan-settings`. A field left out is
    unchanged; `inherit` clears the workspace's choice so the platform's
    applies. The caps are the platform's: a `caps` key is refused by name."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    diagrams: Literal["on", "off", "inherit"] | None = None
    chat_model_class: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def _caps_are_the_platforms(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "caps" in data:
            raise ValueError("caps is a platform setting (Admin › Settings, "
                             "PUT /v1/admin/settings plan_review_caps); a workspace reads it "
                             "and cannot set it")
        return data

    @field_validator("chat_model_class")
    @classmethod
    def _carried(cls, value: str | None) -> str | None:
        if value == INHERIT:
            return value
        return validate_chat_model_class(value, setting="chat_model_class")

    @model_validator(mode="after")
    def _names_something(self) -> "PlanSettingsPut":
        if self.diagrams is None and self.chat_model_class is None:
            raise ValueError("name at least one of diagrams or chat_model_class")
        return self


def authorize_read(auth: AuthContext, tenant: Tenant | None, tenant_id: str) -> Tenant:
    """The workspace, when the caller is in it, owns it or is a platform
    admin; the established 404 otherwise, as for one that does not exist."""
    from .mergesettings import is_member, is_owner  # lazy: see TYPE_CHECKING above

    if tenant is None:
        raise NotFound(f"workspace {tenant_id!r} not found")
    if not (auth.is_admin or is_owner(auth, tenant) or is_member(auth, tenant_id)):
        raise NotFound(f"workspace {tenant_id!r} not found")
    return tenant


def can_edit(auth: AuthContext, tenant: Tenant, roles: RoleStore | None, *,
             fresh: bool = False) -> bool:
    """Owner, the group's `admin` (a role that cannot be read is "no") or a
    platform admin; `fresh` for the write."""
    from .mergesettings import group_admin, is_owner

    return bool(auth.is_admin) or is_owner(auth, tenant) \
        or group_admin(roles, auth, tenant, fresh=fresh)


def authorize_write(auth: AuthContext, tenant: Tenant | None, tenant_id: str,
                    roles: RoleStore | None) -> Tenant:
    """The workspace, when the caller may change its plan settings; 404 for
    one they are not in, 403 for a member who is not its admin."""
    tenant = authorize_read(auth, tenant, tenant_id)
    if not can_edit(auth, tenant, roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its plan "
            "settings. Nothing was changed.", detail={"tenant_id": tenant_id, "required": "admin"})
    return tenant


# --------------------------------------------------------------------------
# the resolution
# --------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def resolve(platform: Mapping[str, Any], workspace: WorkspacePlans,
            schedule: Mapping[str, Any] | None, *, read_at: datetime) -> dict[str, Any]:
    """Each setting, its value and the level it came from (§6.4, §7.8).

    `platform` is `Store.get_platform_settings()` (or the raw document: each
    key is read through `platform_value` again). `schedule` is the schedule
    document the run fires from, or None for a run made by hand."""
    diagrams = platform_value(DIAGRAMS_SETTING, platform)
    diagrams_from = PLATFORM
    if workspace.diagrams in WORKSPACE_DIAGRAMS:
        diagrams, diagrams_from = workspace.diagrams == ON, WORKSPACE
    chosen = (schedule or {}).get(SCHEDULE_FIELD)
    if chosen in (ON, OFF):
        diagrams, diagrams_from = chosen == ON, SCHEDULE

    model = platform_value(CHAT_MODEL_SETTING, platform)
    model_from = PLATFORM
    fell_through = None
    if workspace.chat_model_class is not None:
        if workspace.chat_model_class in chat_model_classes():
            model, model_from = workspace.chat_model_class, WORKSPACE
        else:
            fell_through = (f"the workspace's class {workspace.chat_model_class!r} is not one "
                            f"{CHAT_PROFILE} carries now, so the platform's applies")

    caps = caps_of(platform.get(CAPS_SETTING))
    return {
        "diagrams": {"value": diagrams, "from": diagrams_from},
        "chat_model_class": {"value": model, "from": model_from, "fell_through": fell_through},
        "caps": {**{key: caps[key] for key in CAP_KEYS}, "from": PLATFORM,
                 "set_by": caps["set_by"], "set_at": _iso(caps["set_at"])},
        "read_at": read_at.isoformat(),
    }


def plan_settings_record(effective: Mapping[str, Any]) -> dict[str, Any]:
    """The `plan_settings` a run, a revision, a thread entry or a chat turn
    records when it is made (§6.4), from `effective_plan_settings`."""
    return {"diagrams": effective["diagrams"]["value"], "from": effective["diagrams"]["from"],
            "read_at": effective["read_at"]}


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class PlanSettingsStore:
    """`workspace_settings/{tenant}.plans`, read and audited write, and the
    resolution every plans lane reads."""

    def __init__(self, db: Any, *, now: Callable[[], datetime], mac: SettingsMac,
                 platform: Callable[[], Mapping[str, Any]]) -> None:
        self._db = db
        self._now = now
        self._mac = mac
        self._platform = platform

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def _trusted(self, tenant_id: str, block: Any) -> dict[str, Any] | None:
        if not isinstance(block, Mapping):
            return None
        if SIGNED:
            return trusted_block(tenant_id, MAC_BLOCK, block, self._mac) or None
        return dict(block)

    def document(self, tenant_id: str) -> dict[str, Any] | None:
        """The `plans` map the reader may act on, or None."""
        snap = self._ref(tenant_id).get()
        return self._trusted(tenant_id, (snap.to_dict() or {}).get(FIELD)) if snap.exists else None

    def get(self, tenant_id: str) -> WorkspacePlans:
        return workspace_of(self.document(tenant_id))

    def effective_plan_settings(self, tenant_id: str,
                                schedule: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Each setting for a run of `tenant_id`, resolved now. A schedule of
        another tenant is never read: the established 404."""
        if schedule is not None and schedule.get("tenant_id") != tenant_id:
            raise NotFound("schedule not found")
        return resolve(self._platform(), self.get(tenant_id), schedule, read_at=self._now())

    def put(self, tenant_id: str, body: PlanSettingsPut, *, by: str) -> WorkspacePlans:
        """Apply `body` and its audit in one transaction; a change of nothing
        writes nothing."""
        ref = self._ref(tenant_id)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> WorkspacePlans:
            snap = _snapshot(txn.get(ref))
            whole = (snap.to_dict() or {}) if snap.exists else {}
            before = workspace_of(self._trusted(tenant_id, whole.get(FIELD)))

            def _wanted(value: str | None, current: str | None) -> str | None:
                if value is None:
                    return current
                return None if value == INHERIT else value

            after = WorkspacePlans(
                diagrams=_wanted(body.diagrams, before.diagrams),
                chat_model_class=_wanted(body.chat_model_class, before.chat_model_class),
            )
            old, new = before.as_dict(), after.as_dict()
            changed = [key for key in old if old[key] != new[key]]
            if not changed:
                return after
            block: dict[str, Any] = {**new, "updated_at": now, "updated_by": by}
            mac = block_mac(tenant_id, MAC_BLOCK, new, self._mac.keys) if SIGNED else None
            if mac is not None:
                block[MAC_FIELD] = mac
            txn.set(ref, {"tenant_id": tenant_id, FIELD: block}, merge=True)
            txn.set(self._db.collection(AUDIT_COLLECTION).document(), {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "keys": changed,
                           "from": {key: old[key] for key in changed},
                           "to": {key: new[key] for key in changed}},
            })
            return after

        return _apply(self._db.transaction())


def view(tenant_id: str, store: PlanSettingsStore, *, editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the workspace's choices as stored, what each
    setting resolves to for a run made by hand, the caps read-only, and
    whether the caller may change it."""
    document = store.document(tenant_id) or {}
    effective = store.effective_plan_settings(tenant_id)
    return {
        "tenant_id": tenant_id,
        "plans": workspace_of(document).as_dict(),
        "effective": {"diagrams": effective["diagrams"],
                      "chat_model_class": effective["chat_model_class"]},
        "caps": effective["caps"],
        "chat_model_classes": list(chat_model_classes()),
        "updated_at": _iso(document.get("updated_at")),
        "updated_by": document.get("updated_by"),
        "read_at": effective["read_at"],
        "can_edit": editable,
    }
