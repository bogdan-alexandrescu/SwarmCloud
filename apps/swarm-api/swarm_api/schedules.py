"""The `schedules/{schedule_id}` document: validation, gates, budgets (docs/schedules.md §1, §4).

Pure functions, no Firestore client and no directory: the routes (lane S3) and
the tick (lane S2) read and write the collection and pass in what this module
needs -- the caller's role, the tenant's registered repository ids, the
existing names. So the rules most worth testing exhaustively are tested with
no cloud and no emulator.

The collection is kept by this module and not by `store.py` or `codec.py`, for
the reason `issue_runs` gives: its shape is not the frozen contract's and must
not leak into it (§1.1, §1.5).

WHAT IS STORED IS RESOLVED. The gate, budget, policy and params are written
with every default filled in, so a later change of a type's default does not
loosen an existing schedule (§1.1).

GATES (§4.2). Each point (`run`, `plan`, `merge`) has a floor per type that
nobody goes below, an admin included. Between the floor and the type's
default only a platform admin may set it. The one exception is `merge: auto`,
which is never accepted as an ordinary edit (422 `use_merge_switch`) and is
reached only through the audited switch, and only where the type's floor
allows it (SD3). Raising a gate is always allowed.

BUDGETS (§4.3, SD4) bound what a SCHEDULE may start and nothing else: the
2026-10-01 "no dollar budgets" decision still holds for work started by hand,
and nothing in admission reads these figures. `per_day_usd` is a firing gate,
`per_run_usd` a tripwire read when an attempt ends, and an UNKNOWN COST COUNTS
AT THE PER-RUN CAP, never as zero. Money is compared in whole cents so float
rounding cannot move a decision.

INVARIANTS. Nothing here creates work, a lease or a pod: a schedule is a
document (invariant 1). A caller picks a type by name and the request models
refuse every other key by name, `image` and `command` included (invariant 10).
`tenant_id`, `owner` and the actor fields are arguments from the verified
token, never body fields (invariant 9).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, ValidationError

from . import cronexpr, scheduletypes
from .errors import Conflict, Forbidden, ValidationFailed
from .scheduletypes import EXECUTOR_DIR, GATE_POINTS, GateSpec, ScheduleType, strictness

# --------------------------------------------------------------------------
# Constants. Each carries its reason, because each is a decision.
# --------------------------------------------------------------------------

COLLECTION = "schedules"

#: At most this many schedules per tenant (§1.1, §5.6): with each type's
#: minimum interval it bounds how often one tenant can fire. An admin may
#: raise it per tenant; the route passes the raised value in.
SCHEDULES_PER_TENANT = 25

#: A `repos` scope lists 1-25 registrations (§1.1).
MAX_SCOPE_REPOS = 25

ID_PREFIX = "sch_"
STATES = ("enabled", "paused", "auto_paused", "disabled")

#: §4.7: a pending approval expires after this many hours, 1-336.
APPROVAL_TTL_DEFAULT_HOURS = 72
APPROVAL_TTL_MAX_HOURS = 336

#: §2.6: jitter is at most five minutes, and at most 10% of the interval, so
#: a 15-minute schedule is never spread across most of its own period.
JITTER_MAX_SECONDS = 300
JITTER_SHARE = 0.10

#: A named approvers list may name at most this many members.
MAX_NAMED_APPROVERS = 20

TIERS = ("R0", "R1", "R2", "R3")


# --------------------------------------------------------------------------
# Errors: the API's own types, each with a stable code
# --------------------------------------------------------------------------


class ScheduleInvalid(ValidationFailed):
    """422 with a schedule-specific code, e.g. `gate_below_floor`."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message, detail=detail)
        self.code = code


class ScheduleForbidden(Forbidden):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message, detail=detail)
        self.code = code


class ScheduleConflict(Conflict):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message, detail=detail)
        self.code = code


# --------------------------------------------------------------------------
# Request bodies. Extra keys are refused by name (invariant 10).
# --------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScopeIn(_Body):
    mode: Literal["repos", "all", "platform"]
    repo_ids: list[str] = Field(default_factory=list)


class GateIn(_Body):
    run: Literal["auto", "approve"] | None = None
    plan: Literal["auto", "approve"] | None = None
    merge: Literal["off", "approve", "auto"] | None = None
    approvers: Literal["members", "owner_only"] | list[str] | None = None
    approval_ttl_hours: StrictInt | None = Field(default=None, ge=1, le=APPROVAL_TTL_MAX_HOURS)


class BudgetIn(_Body):
    per_run_usd: StrictFloat | None = None
    per_day_usd: StrictFloat | None = None
    max_concurrent: StrictInt | None = None


class PolicyIn(_Body):
    overlap: Literal["skip", "queue_one"] | None = None
    catch_up: Literal["skip", "run_once"] | None = None
    jitter: StrictBool | None = None
    dry_run: StrictBool | None = None


class ScheduleCreate(_Body):
    """`POST /v1/schedules` (§7.1). `tenant_id` and `owner` are never body fields."""

    name: str = Field(min_length=1, max_length=80)
    type: str = Field(min_length=1, max_length=64)
    scope: ScopeIn
    cron: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    params: dict[str, Any] | None = None
    gate: GateIn | None = None
    budget: BudgetIn | None = None
    policy: PolicyIn | None = None
    state: Literal["enabled", "paused"] = "enabled"
    #: The route's idempotency key (§7.1): a repeat within 24 h returns the
    #: schedule the first request created. Not part of the document.
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)


class SchedulePatch(_Body):
    """`PATCH /v1/schedules/{id}`, carrying the revision it read (§1.1).

    `type` is not editable: a different type is a different schedule. `state`
    and `owner` move through their own audited actions.
    """

    revision: StrictInt = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    scope: ScopeIn | None = None
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    params: dict[str, Any] | None = None
    gate: GateIn | None = None
    budget: BudgetIn | None = None
    policy: PolicyIn | None = None


def parse_create(body: Mapping[str, Any]) -> ScheduleCreate:
    return _parse(ScheduleCreate, body)


def parse_patch(body: Mapping[str, Any]) -> SchedulePatch:
    return _parse(SchedulePatch, body)


def _parse(model: type[BaseModel], body: Mapping[str, Any]) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise ScheduleInvalid("invalid_schedule", "the schedule is not valid", errors=_errors(exc)) from None


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    # The location and the message only: never the input, which may be long.
    return [{"loc": ".".join(str(part) for part in err["loc"]), "msg": err["msg"]} for err in exc.errors()]


# --------------------------------------------------------------------------
# Type, cron and timezone
# --------------------------------------------------------------------------


def new_schedule_id() -> str:
    """`sch_` and 12 hex digits: random, so an id says nothing about its tenant (§1.1)."""
    return ID_PREFIX + secrets.token_hex(6)


def resolve_type(name: str, root: Path = EXECUTOR_DIR) -> ScheduleType:
    """The catalogue entry, or a 422 that names the available types (§1.1)."""
    available = scheduletypes.available_names(root)
    entry = scheduletypes.get(name)
    if entry is None:
        raise ScheduleInvalid("unknown_type", f"{name!r} is not a schedule type", available=available)
    ok, reason = scheduletypes.availability(entry, root)
    if not ok:
        raise ScheduleInvalid(
            "type_unavailable", f"{name} is not available: {reason}", available=available, reason=reason
        )
    return entry


def _cron(text: str, tz_name: str) -> tuple[cronexpr.CronExpr, Any]:
    try:
        return cronexpr.parse(text), cronexpr.zone(tz_name)
    except cronexpr.CronError as exc:
        field = "timezone" if exc.code in ("unknown_timezone", "no_tz_database") else "cron"
        raise ScheduleInvalid(f"invalid_{field}", exc.message, reason=exc.code) from None


def interval_refusal(entry: ScheduleType, gap: timedelta) -> dict[str, Any] | None:
    """§2.3: the smallest gap between the next 50 firings must reach the type's minimum."""
    if gap >= entry.min_interval:
        return None
    need = int(entry.min_interval.total_seconds() // 60)
    have = int(gap.total_seconds() // 60)
    return {
        "code": "interval_too_short",
        "message": f"{entry.name} fires at most every {need} minutes; this expression fires {have} minutes apart",
        "min_interval_minutes": need,
        "min_gap_minutes": have,
    }


def check_cron(entry: ScheduleType, text: str, tz_name: str, now: datetime) -> timedelta:
    """Validate `text` in `tz_name` for `entry`; the smallest gap, or a 422."""
    expr, tz = _cron(text, tz_name)
    gap = cronexpr.min_gap(expr, now, tz)
    refusal = interval_refusal(entry, gap)
    if refusal:
        code, message = refusal.pop("code"), refusal.pop("message")
        raise ScheduleInvalid(code, message, **refusal)
    return gap


def preview(
    text: str, tz_name: str, now: datetime, type_name: str | None = None, count: int = 5
) -> dict[str, Any]:
    """`POST /v1/schedules:preview` (§6.3): `{words, next, min_gap_minutes, refusal}`.

    A bad expression or zone is a 422, because there is nothing to preview. A
    type whose minimum interval the expression breaks is a `refusal` in the
    answer, so the form can show the words and the next five beside why it
    would be refused. The type is looked up whether or not it is available:
    the form previews while the person is still choosing.
    """
    _cron(text, tz_name)
    answer = cronexpr.preview(text, tz_name, now, count)
    answer["refusal"] = None
    if type_name is not None:
        entry = scheduletypes.get(type_name)
        if entry is None:
            raise ScheduleInvalid("unknown_type", f"{type_name!r} is not a schedule type")
        answer["refusal"] = interval_refusal(entry, timedelta(minutes=answer["min_gap_minutes"]))
    return answer


def jitter_seconds(schedule_id: str, interval: timedelta) -> int:
    """§2.6: `sha256(schedule_id) mod J`, J the smaller of 300 s and 10% of the interval.

    Deterministic, so a schedule fires at the same offset every time and a
    person can be told it. Jitter moves `next_run_at` only; the firing key is
    the un-jittered slot, so it cannot create or merge firings.
    """
    window = int(min(JITTER_MAX_SECONDS, interval.total_seconds() * JITTER_SHARE))
    if window <= 0:
        return 0
    return int.from_bytes(hashlib.sha256(schedule_id.encode()).digest()[:8], "big") % window


def next_times(doc: Mapping[str, Any], now: datetime) -> tuple[datetime, datetime]:
    """`(next_slot, next_run_at)`: the first slot after `now`, and it with jitter applied."""
    expr, tz = _cron(doc["cron"], doc["timezone"])
    slot = cronexpr.next_after(expr, now, tz)
    if not doc["policy"]["jitter"]:
        return slot, slot
    offset = jitter_seconds(doc["schedule_id"], cronexpr.min_gap(expr, now, tz))
    return slot, slot + timedelta(seconds=offset)


# --------------------------------------------------------------------------
# Scope and who may create
# --------------------------------------------------------------------------


def resolve_scope(entry: ScheduleType, scope: ScopeIn, registered_repo_ids: Iterable[str]) -> dict[str, Any]:
    """§1.1, §5.4: `repos` names 1-25 of THIS tenant's registrations; `all` and `platform` name none."""
    if scope.mode not in entry.creators:
        raise ScheduleInvalid(
            "scope_not_allowed",
            f"{entry.name} does not take scope {scope.mode!r}",
            allowed=sorted(entry.creators),
        )
    if scope.mode != "repos":
        if scope.repo_ids:
            raise ScheduleInvalid("invalid_scope", f"scope {scope.mode!r} names no repositories")
        return {"mode": scope.mode, "repo_ids": []}
    ids = scope.repo_ids
    if not 1 <= len(ids) <= MAX_SCOPE_REPOS:
        raise ScheduleInvalid("invalid_scope", f"a repos scope names 1-{MAX_SCOPE_REPOS} repositories")
    if len(set(ids)) != len(ids):
        raise ScheduleInvalid("invalid_scope", "a repository is named twice")
    registered = set(registered_repo_ids)
    missing = [repo_id for repo_id in ids if repo_id not in registered]
    if missing:
        # The same answer for another tenant's registration and for none at
        # all, so a repo id is never an oracle for another tenant (§5.1).
        raise ScheduleInvalid(
            "repository_not_registered",
            "these are not repositories registered in this tenant",
            repo_ids=missing,
        )
    return {"mode": "repos", "repo_ids": list(ids)}


def check_creator(
    entry: ScheduleType,
    scope_mode: str,
    *,
    is_admin: bool,
    is_owner: bool,
    platform_repository: bool = False,
    merge_auto: bool = False,
) -> None:
    """§3 "creates" and §3.13: a member, a platform admin or `PLATFORM_OWNER`."""
    role = scheduletypes.creator_role(
        entry, scope_mode, platform_repository=platform_repository, merge_auto=merge_auto
    )
    if role is None:
        raise ScheduleInvalid("scope_not_allowed", f"{entry.name} does not take scope {scope_mode!r}")
    if role == "owner" and not is_owner:
        raise ScheduleForbidden("owner_required", f"only the platform owner may create this {entry.name} schedule")
    if role == "admin" and not (is_admin or is_owner):
        raise ScheduleForbidden("admin_required", f"only a platform admin may create this {entry.name} schedule")


def check_tenant_room(existing_count: int, limit: int = SCHEDULES_PER_TENANT) -> None:
    if existing_count >= limit:
        raise ScheduleInvalid(
            "schedule_limit", f"this tenant already has {existing_count} schedules; the limit is {limit}", limit=limit
        )


def check_name(name: str, existing_names: Iterable[str]) -> None:
    """Unique within the tenant, compared case-insensitively: it is what the list shows."""
    folded = name.strip().casefold()
    if any(folded == other.strip().casefold() for other in existing_names):
        raise ScheduleConflict("name_taken", f"this tenant already has a schedule named {name!r}")


# --------------------------------------------------------------------------
# Gates (§4.2)
# --------------------------------------------------------------------------


def resolve_gate(
    entry: ScheduleType,
    requested: GateIn | None,
    *,
    current: Mapping[str, Any] | None = None,
    is_admin: bool = False,
    via_merge_switch: bool = False,
) -> dict[str, Any]:
    """The stored gate: `requested` over `current` (an edit) or the type's default (a create).

    Refused, each by its own code:
      * `merge: auto` outside the switch -> 422 `use_merge_switch`;
      * the switch where the type's floor does not allow `merge: auto` ->
        422 `merge_auto_not_allowed`;
      * any point below the type's floor -> 422 `gate_below_floor`, an admin
        included;
      * a point CHANGED to below the type's default by a non-admin -> 403
        `gate_below_default` (a point an admin already lowered may stay so
        while a member edits something else).
    """
    requested = requested or GateIn()
    base = dict(current) if current else {
        **entry.default_gate.as_dict(),
        "approvers": "members",
        "approval_ttl_hours": APPROVAL_TTL_DEFAULT_HOURS,
    }
    if requested.merge == "auto" and not via_merge_switch:
        raise ScheduleInvalid(
            "use_merge_switch",
            "merge: auto is set only through the audited merge switch (POST /v1/schedules/{id}:merge-mode)",
        )
    if via_merge_switch and requested.merge == "auto" and entry.floor_gate.merge != "auto":
        raise ScheduleInvalid("merge_auto_not_allowed", f"{entry.name} never merges unattended")
    gate = dict(base)
    for point in GATE_POINTS:
        value = getattr(requested, point)
        if value is None:
            continue
        floor = getattr(entry.floor_gate, point)
        default = getattr(entry.default_gate, point)
        if strictness(point, value) < strictness(point, floor):
            raise ScheduleInvalid(
                "gate_below_floor", f"{entry.name}'s {point} gate cannot be below {floor!r}", point=point, floor=floor
            )
        lowered = strictness(point, value) < strictness(point, default)
        changed = current is None or current.get(point) != value
        exempt = via_merge_switch and point == "merge"
        if lowered and changed and not exempt and not is_admin:
            raise ScheduleForbidden(
                "gate_below_default",
                f"only a platform admin may set {entry.name}'s {point} gate below {default!r}",
                point=point,
                default=default,
            )
        gate[point] = value
    if requested.approvers is not None:
        gate["approvers"] = _approvers(requested.approvers)
    if requested.approval_ttl_hours is not None:
        gate["approval_ttl_hours"] = requested.approval_ttl_hours
    return gate


def _approvers(value: str | list[str]) -> str | list[str]:
    if isinstance(value, str):
        return value
    names = [name.strip().lower() for name in value]
    if not 1 <= len(names) <= MAX_NAMED_APPROVERS:
        raise ScheduleInvalid("invalid_approvers", f"a named list holds 1-{MAX_NAMED_APPROVERS} members")
    if any("@" not in name or len(name) > 254 for name in names):
        raise ScheduleInvalid("invalid_approvers", "each approver is a member's email")
    if len(set(names)) != len(names):
        raise ScheduleInvalid("invalid_approvers", "an approver is named twice")
    # Membership is asked of the directory, at creation and at approval
    # (§4.6); the route does that, not this pure function.
    return names


def gate_spec(gate: Mapping[str, Any]) -> GateSpec:
    return GateSpec(run=gate["run"], plan=gate["plan"], merge=gate["merge"])


# --------------------------------------------------------------------------
# Params
# --------------------------------------------------------------------------


def resolve_params(
    entry: ScheduleType,
    requested: Mapping[str, Any] | None,
    *,
    gate: Mapping[str, Any],
    is_admin: bool = False,
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The type's own model validates them, with defaults filled in; an edit merges over `current`."""
    raw = {**(current or {}), **(requested or {})}
    if entry.name == "issue-sweep":
        raw = _sweep_follows_gate(raw, requested or {}, gate)
    try:
        params = entry.params_model.model_validate(raw)
    except ValidationError as exc:
        raise ScheduleInvalid("invalid_params", f"{entry.name}'s parameters are not valid", errors=_errors(exc)) from None
    if entry.name == "issue-sweep" and not params.territory_guard and not is_admin:
        raise ScheduleForbidden("territory_guard_admin_only", "only a platform admin may turn the territory guard off")
    return params.model_dump(mode="json")


_PLAN_APPROVAL = {"auto": "auto", "approve": "required"}


def _sweep_follows_gate(raw: dict[str, Any], requested: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
    """§3.1's `plan_approval` and `merge` restate the gate; the gate is the authority."""
    want = {"plan_approval": _PLAN_APPROVAL[gate["plan"]], "merge": gate["merge"]}
    for key, value in want.items():
        if key in requested and requested[key] != value:
            if key == "merge" and requested[key] == "auto":
                raise ScheduleInvalid(
                    "use_merge_switch", "merge: auto is set only through the audited merge switch"
                )
            raise ScheduleInvalid(
                "params_disagree_with_gate",
                f"params.{key} is {requested[key]!r} but the gate makes it {value!r}; change the gate instead",
                param=key,
            )
    return {**raw, **want}


# --------------------------------------------------------------------------
# Budget (§4.3)
# --------------------------------------------------------------------------


def resolve_budget(
    entry: ScheduleType,
    requested: BudgetIn | None,
    *,
    current: Mapping[str, Any] | None = None,
    root: Path = EXECUTOR_DIR,
) -> dict[str, Any]:
    """`{per_run_usd, per_day_usd, max_concurrent}` within the type's caps; the caps are the defaults."""
    caps = scheduletypes.budget_caps(entry, root)
    if caps is None:
        raise ScheduleInvalid("type_unavailable", f"{entry.name} has no budget caps yet (SD4)")
    budget = dict(current) if current else caps.as_dict()
    for key, value in (requested or BudgetIn()).model_dump(exclude_none=True).items():
        budget[key] = value
    limits = caps.as_dict()
    for key in ("per_run_usd", "per_day_usd"):
        if _cents(budget[key]) <= 0:
            raise ScheduleInvalid("invalid_budget", f"{key} must be above zero", field=key)
        if _cents(budget[key]) > _cents(limits[key]):
            raise ScheduleInvalid(
                "budget_above_cap", f"{key} is capped at {limits[key]} for {entry.name}", field=key, cap=limits[key]
            )
    if not 1 <= budget["max_concurrent"] <= caps.max_concurrent:
        raise ScheduleInvalid(
            "budget_above_cap",
            f"max_concurrent is 1-{caps.max_concurrent} for {entry.name}",
            field="max_concurrent",
            cap=caps.max_concurrent,
        )
    if _cents(budget["per_day_usd"]) < _cents(budget["per_run_usd"]):
        # A day smaller than one run is spent by the first live item's
        # reservation, so the schedule could never fire twice in a day.
        raise ScheduleInvalid("invalid_budget", "per_day_usd must be at least per_run_usd", field="per_day_usd")
    return {
        "per_run_usd": float(budget["per_run_usd"]),
        "per_day_usd": float(budget["per_day_usd"]),
        "max_concurrent": int(budget["max_concurrent"]),
    }


def _cents(value: float | int) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def spend_day(now: datetime, tz_name: str) -> str:
    """The budget's day is the schedule's timezone's (§4.3)."""
    try:
        tz = cronexpr.zone(tz_name)
    except cronexpr.CronError as exc:
        raise ScheduleInvalid("invalid_timezone", exc.message, reason=exc.code) from None
    return now.astimezone(tz).date().isoformat()


def spend_for(spend: Mapping[str, Any] | None, day: str) -> dict[str, Any]:
    """The `spend` map for `day`: the stored one if it is that day's, else a new day's zeros."""
    if spend and spend.get("day") == day:
        return dict(spend)
    return {"day": day, "reported_usd": 0.0, "unreported_attempts": 0, "reserved_usd": 0.0}


def reserved_usd(budget: Mapping[str, Any], *, live_items: int, unreported_attempts: int) -> float:
    """`per_run_usd` for each live work item and each attempt whose cost was not reported."""
    return _cents(budget["per_run_usd"]) * (live_items + unreported_attempts) / 100


def budget_exhausted(
    budget: Mapping[str, Any], *, reported_today: float, live_items: int, unreported_attempts: int
) -> bool:
    """A firing is skipped `BUDGET_EXHAUSTED` when `reported_today + reserved > per_day_usd`."""
    reserved = _cents(budget["per_run_usd"]) * (live_items + unreported_attempts)
    return _cents(reported_today) + reserved > _cents(budget["per_day_usd"])


def concurrency_room(budget: Mapping[str, Any], *, live_items: int) -> int:
    """How many work items a firing may create: the remainder under `max_concurrent`."""
    return max(0, int(budget["max_concurrent"]) - live_items)


def run_over_budget(budget: Mapping[str, Any], cost_usd: float | None) -> bool:
    """`RUN_OVER_BUDGET` when a recorded cost exceeds `per_run_usd` (§4.3).

    An unreported cost (None) is not over: it counts AT the cap, and the cap
    is not more than itself. It is reserved at the cap by `budget_exhausted`.
    """
    return cost_usd is not None and _cents(cost_usd) > _cents(budget["per_run_usd"])


def worst_case_day_usd(budget: Mapping[str, Any], actual_run_cost_usd: float) -> float:
    """`per_day_usd + max_concurrent * (actual run cost - per_run_usd)`, what the budget card states."""
    over = max(0, _cents(actual_run_cost_usd) - _cents(budget["per_run_usd"]))
    return (_cents(budget["per_day_usd"]) + int(budget["max_concurrent"]) * over) / 100


# --------------------------------------------------------------------------
# Policy and tier
# --------------------------------------------------------------------------


def resolve_policy(
    entry: ScheduleType, requested: PolicyIn | None, *, current: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """§2.4-§2.8. `catch_up` defaults per type and is overridable per schedule (SD6)."""
    policy = dict(current) if current else {
        "overlap": "skip",
        "catch_up": entry.catch_up,
        "jitter": True,
        "dry_run": False,
    }
    policy.update((requested or PolicyIn()).model_dump(exclude_none=True))
    return policy


def risk_tier(entry: ScheduleType, gate: Mapping[str, Any], params: Mapping[str, Any]) -> str:
    """§4.1, computed from the type and its resolved gate and parameters."""
    return entry.risk(gate_spec(gate), entry.params_model.model_validate(params))


def pushes(entry: ScheduleType, params: Mapping[str, Any]) -> bool:
    """§5.4: whether the owner needs a `write` grant on each repository in scope."""
    return entry.pushes(entry.params_model.model_validate(params))


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


def build_schedule(
    body: ScheduleCreate,
    *,
    tenant_id: str,
    actor: str,
    now: datetime,
    registered_repo_ids: Iterable[str],
    existing_names: Iterable[str] = (),
    existing_count: int = 0,
    limit: int = SCHEDULES_PER_TENANT,
    is_admin: bool = False,
    is_owner: bool = False,
    schedule_id: str | None = None,
    root: Path = EXECUTOR_DIR,
) -> dict[str, Any]:
    """The `schedules/{schedule_id}` document a create writes (§1.1), fully resolved."""
    check_tenant_room(existing_count, limit)
    check_name(body.name, existing_names)
    entry = resolve_type(body.type, root)
    scope = resolve_scope(entry, body.scope, registered_repo_ids)
    check_creator(entry, scope["mode"], is_admin=is_admin, is_owner=is_owner)
    check_cron(entry, body.cron, body.timezone, now)
    gate = resolve_gate(entry, body.gate, is_admin=is_admin)
    params = resolve_params(entry, body.params, gate=gate, is_admin=is_admin)
    budget = resolve_budget(entry, body.budget, root=root)
    policy = resolve_policy(entry, body.policy)
    doc: dict[str, Any] = {
        "schedule_id": schedule_id or new_schedule_id(),
        "tenant_id": tenant_id,
        "name": body.name,
        "type": entry.name,
        "scope": scope,
        "cron": " ".join(body.cron.split()),
        "timezone": body.timezone,
        "params": params,
        "gate": gate,
        "budget": budget,
        "policy": policy,
        "state": body.state,
        "pause": None,
        "owner": actor,
        "created_by": actor,
        "created_at": now,
        "updated_by": actor,
        "updated_at": now,
        "next_run_at": None,
        "next_slot": None,
        "last_firing": None,
        "consecutive_failures": 0,
        "spend": spend_for(None, spend_day(now, body.timezone)),
        "revision": 1,
    }
    if body.state == "paused":
        doc["pause"] = {"by": actor, "at": now, "reason": "created paused", "code": None}
    else:
        doc["next_slot"], doc["next_run_at"] = next_times(doc, now)
    return doc


def apply_edit(
    stored: Mapping[str, Any],
    patch: SchedulePatch,
    *,
    actor: str,
    now: datetime,
    registered_repo_ids: Iterable[str],
    existing_names: Iterable[str] = (),
    is_admin: bool = False,
    is_owner: bool = False,
    platform_repository: bool = False,
    root: Path = EXECUTOR_DIR,
) -> dict[str, Any]:
    """The document after `PATCH` (§7.1). A stale revision is 409 `schedule_changed`.

    A changed scope is held to the creator rule a create meets (§3, §3.13):
    otherwise a member could create `observer` on `repos` and edit it to the
    owner-only `platform` scope, which reads every tenant's aggregates.
    `platform_repository` is whether the new scope names a repository
    registered `platform: true` (S11); the caller resolves it.

    The stored gate, budget and policy are the base the patch is laid over,
    not the type's current defaults: an edit of one field never resets
    another, and a later change of a default never loosens this schedule.
    """
    if patch.revision != stored["revision"]:
        raise ScheduleConflict(
            "schedule_changed",
            "the schedule was changed since it was read; read it again",
            revision=stored["revision"],
        )
    entry = scheduletypes.get(stored["type"])
    if entry is None:
        raise ScheduleInvalid("unknown_type", f"{stored['type']!r} is no longer a schedule type")
    doc = dict(stored)
    if patch.name is not None and patch.name.strip().casefold() != stored["name"].strip().casefold():
        check_name(patch.name, existing_names)
    if patch.name is not None:
        doc["name"] = patch.name
    if patch.scope is not None:
        doc["scope"] = resolve_scope(entry, patch.scope, registered_repo_ids)
        if doc["scope"] != stored["scope"]:
            check_creator(
                entry,
                doc["scope"]["mode"],
                is_admin=is_admin,
                is_owner=is_owner,
                platform_repository=platform_repository,
                merge_auto=stored["gate"].get("merge") == "auto",
            )
    if patch.cron is not None:
        doc["cron"] = " ".join(patch.cron.split())
    if patch.timezone is not None:
        doc["timezone"] = patch.timezone
    if patch.cron is not None or patch.timezone is not None:
        check_cron(entry, doc["cron"], doc["timezone"], now)
    doc["gate"] = resolve_gate(entry, patch.gate, current=stored["gate"], is_admin=is_admin)
    doc["params"] = resolve_params(
        entry,
        patch.params,
        gate=doc["gate"],
        is_admin=is_admin or not _turns_guard_off(stored, patch),
        current=stored["params"],
    )
    if patch.budget is not None:
        doc["budget"] = resolve_budget(entry, patch.budget, current=stored["budget"], root=root)
    doc["policy"] = resolve_policy(entry, patch.policy, current=stored["policy"])
    if doc["state"] == "enabled":
        doc["next_slot"], doc["next_run_at"] = next_times(doc, now)
    doc["updated_by"] = actor
    doc["updated_at"] = now
    doc["revision"] = stored["revision"] + 1
    return doc


def _turns_guard_off(stored: Mapping[str, Any], patch: SchedulePatch) -> bool:
    """Whether this edit is what sets `territory_guard: false` (an admin's left alone)."""
    return bool(patch.params) and patch.params.get("territory_guard") is False and stored["params"].get(
        "territory_guard", True
    ) is not False
