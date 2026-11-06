"""What limit a refusal applied, and why it was lower than the ceiling (#3047).

On 24 October 2026 admission refused up to 48 steps with "provider concurrency
limit" while 6 or 7 agents ran against a configured ceiling of 40, and nothing
afterwards could say what limit it had applied or why. The frozen admission
transaction (`swarm_common.admission.evaluate_capacity`) names the pool and
gives `limit` (the pool's `effective_limit` as the transaction read it) and
`active`. It does not say what the ceiling was, nor which of the inputs that
`SlotPool.effective_limit` takes the minimum of -- the configured `hard_limit`,
the AIMD `adaptive_target`, the `quota_derived_limit` -- pulled it down.

`explain` adds that, AFTER the denial, from one read of each refused pool and,
for a per-tenant provider pool, of the broker's quota document and its admin
audit (an override: a target an admin set). It changes no admission decision:
the transaction has already refused and written nothing, and what this reads
is never fed back into it. The values the transaction applied (`limit`,
`active`) are kept as `applied` and `in_use` unchanged; the ceiling and the
inputs are as read a moment later, which `inputs_read_at` dates.

Every value here is a number, a pool name or an instant. Nothing from a
credential, a quota document's other fields or an audit row's caller or reason
is copied, so no record this builds can carry one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from google.cloud.firestore_v1.base_query import FieldFilter

from .codec import as_datetime

log = logging.getLogger("scheduler.limits")

#: The broker's per-(provider, tenant) quota documents and their admin audit
#: (`quota_broker.service.QUOTA` and `QUOTA_AUDIT`). Read here, never written.
QUOTA = "quota"
QUOTA_AUDIT = "quota_audit"

#: How many audit rows of one (provider, tenant) are read to find the newest
#: target an admin set. Admin changes are rare; the read is bounded anyway.
#: THE BOUND CAN MISS THE NEWEST OVERRIDE: the read is unordered (ordering by
#: `at` behind the two equality filters needs a composite index this repository
#: does not declare), so once a (provider, tenant) has more than this many
#: audit rows the newest may not be among those read. The record then names no
#: override, or an older one -- it still never changes an admission decision.
AUDIT_READ_CAP = 50

#: The admin actions in `quota_audit` that set a working target by hand.
_OVERRIDE_ACTIONS = ("adaptive_target_reset", "hard_max_set")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _iso(value: Any) -> str | None:
    moment = as_datetime(value)
    return moment.isoformat() if moment is not None else None


def _tenant_provider(pool: str) -> tuple[str, str] | None:
    """`provider:<p>:tenant:<t>` -> (p, t); None for every other pool name."""
    parts = pool.split(":")
    if len(parts) == 4 and parts[0] == "provider" and parts[2] == "tenant":
        return parts[1], parts[3]
    return None


@dataclass
class PoolReading:
    """One pool's inputs, read once per drain."""

    ceiling: int | None
    adaptive_target: int | None
    quota_derived_limit: int | None
    read_at: str | None
    #: The broker's quota document for a per-tenant provider pool, reduced to
    #: its instants and state word. `quota_updated_at` is the document's last
    #: write (any success or refresh), not when an input changed.
    quota_updated_at: str | None = None
    last_rate_limited_at: str | None = None
    provider_state: str | None = None
    #: The newest admin override of the target: (target, instant, action).
    override: tuple[int, str | None, str] | None = None
    #: When the broker last CHANGED the target (`target_changed_at`, owner
    #: 2026-11-02): a cut, a climb, an override or a max's clamp, never a
    #: success that left it alone. None for a document written before it.
    target_changed_at: str | None = None


def _override_holds(reading: PoolReading, applied: int) -> bool:
    """Whether the newest admin override is still what holds `applied`.

    It is when it set that value and no rate limit has cut the target since.
    The instants are compared as datetimes, not as ISO strings, so two
    instants written with different UTC offsets still order correctly.
    """
    override = reading.override
    if override is None or override[0] != applied:
        return False
    at = as_datetime(override[1])
    last_cut = as_datetime(reading.last_rate_limited_at)
    return last_cut is None or (at is not None and at >= last_cut)


def _target_dating(reading: PoolReading, override_holds: bool) -> tuple[str | None, str | None]:
    """(set_at, set_by) for the adaptive target, from `target_changed_at`.

    The instant is the broker's own record of the target's last change. What
    made that change: the override when one still holds the value, else the
    rate limit when the change was not after the last cut (a cut stamps both
    with one instant), else a climb -- the +1-per-successes step, the
    time-based recovery -- since nothing else raises a target after a cut.
    """
    changed = reading.target_changed_at
    if override_holds:
        return changed, "override"
    at = as_datetime(changed)
    last_cut = as_datetime(reading.last_rate_limited_at)
    if last_cut is not None and at is not None and at <= last_cut:
        return changed, "rate_limit"
    return changed, "climb"


def lowered_by(reading: PoolReading, applied: int | None) -> list[dict[str, Any]]:
    """The inputs that hold `applied` below the ceiling, with when each was set.

    Empty when nothing lowers it: the ceiling applies, or it is unknown. An
    input is named when it is below the ceiling and equal to the applied value
    -- the minimum `SlotPool.effective_limit` takes -- so two inputs at the same
    value are both named.

    WHEN AN INPUT WAS SET (`set_at`, with `set_by` saying what it is): the
    override's audit instant when an admin override still holds the value
    (`set_by: "override"`), else the rate limit that last cut the target
    (`set_by: "rate_limit"`, the quota document's `last_429_at`), else null.
    It is NOT the quota document's `updated_at`: the broker rewrites that on
    every success and refresh while the target holds, so it reads minutes ago
    for a target that has held 7 for hours. That last write is served apart,
    as `quota_written_at`.

    THE TARGET'S OWN INSTANT FIRST (owner, 2026-11-02): the broker writes
    `target_changed_at` only when the target moves, so the adaptive target is
    dated by it when the document has it, and a target a climb raised since
    the cut reads `set_by: "climb"` at the climb (`_target_dating`). A
    document written before that field is dated as above, by the override or
    the cut. A quota-derived limit is dated as above either way. A shared
    `provider:<p>` pool has no quota document of its own, so its instants are
    null rather than a guess.
    """
    ceiling = reading.ceiling
    if ceiling is None or applied is None or applied >= ceiling:
        return []
    out: list[dict[str, Any]] = []
    override_holds = _override_holds(reading, applied)
    if override_holds and reading.override is not None:
        set_at, set_by = reading.override[1], "override"
    elif reading.last_rate_limited_at is not None:
        set_at, set_by = reading.last_rate_limited_at, "rate_limit"
    else:
        set_at, set_by = None, None
    for name, value in (
        ("adaptive_target", reading.adaptive_target),
        ("quota_derived_limit", reading.quota_derived_limit),
    ):
        if value is None or value >= ceiling or max(0, value) != applied:
            continue
        at, by = set_at, set_by
        if name == "adaptive_target" and reading.target_changed_at is not None:
            at, by = _target_dating(reading, override_holds)
        entry: dict[str, Any] = {
            "input": name, "value": value, "set_at": at, "set_by": by,
            "quota_written_at": reading.quota_updated_at,
        }
        if name == "adaptive_target" and reading.last_rate_limited_at is not None:
            entry["last_rate_limited_at"] = reading.last_rate_limited_at
        if name == "quota_derived_limit" and reading.provider_state is not None:
            entry["provider_state"] = reading.provider_state
        out.append(entry)
    if override_holds and reading.override is not None:
        target, at, action = reading.override
        out.append({"input": "override", "value": target, "set_at": at, "action": action})
    return out


def _read_override(db: Any, provider: str, tenant_id: str) -> tuple[int, str | None, str] | None:
    query = (
        db.collection(QUOTA_AUDIT)
        .where(filter=FieldFilter("provider", "==", provider))
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .limit(AUDIT_READ_CAP)
    )
    newest: tuple[datetime, int, str] | None = None
    for snap in query.stream():
        row = snap.to_dict() or {}
        action = row.get("action")
        target = _int(row.get("to_target"))
        at = as_datetime(row.get("at"))
        if action not in _OVERRIDE_ACTIONS or target is None or at is None:
            continue
        if action == "hard_max_set" and _int(row.get("from_target")) == target:
            # A max that left the target alone did not set it -- every pool
            # raise through swarm-api writes one (owner, 2026-11-02).
            continue
        if newest is None or at > newest[0]:
            newest = (at, target, str(action))
    if newest is None:
        return None
    return newest[1], newest[0].isoformat(), newest[2]


def read_pool(db: Any, pool: str, now: datetime) -> PoolReading | None:
    """The refused pool's ceiling and inputs, or None when it cannot be read."""
    snap = db.collection("pools").document(pool).get()
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict() or {}
    reading = PoolReading(
        ceiling=_int(data.get("hard_limit")),
        adaptive_target=_int(data.get("adaptive_target")),
        quota_derived_limit=_int(data.get("quota_derived_limit")),
        read_at=now.isoformat(),
    )
    scoped = _tenant_provider(pool)
    if scoped is None:
        return reading
    provider, tenant_id = scoped
    quota = db.collection(QUOTA).document(f"{provider}:{tenant_id}").get()
    if getattr(quota, "exists", False):
        q = quota.to_dict() or {}
        reading.quota_updated_at = _iso(q.get("updated_at"))
        reading.last_rate_limited_at = _iso(q.get("last_429_at"))
        reading.target_changed_at = _iso(q.get("target_changed_at"))
        state = q.get("state")
        reading.provider_state = str(state) if isinstance(state, str) else None
    reading.override = _read_override(db, provider, tenant_id)
    return reading


@dataclass
class LimitExplainer:
    """Explains refusals for one drain, reading each refused pool once.

    One per drain run: the pool inputs move between drains (a 429, a sweep),
    and a cache that outlived the run would date a refusal with an old reading.
    """

    db: Any
    now: Callable[[], datetime]
    readings: dict[str, PoolReading | None] = field(default_factory=dict)

    def _reading(self, pool: str) -> PoolReading | None:
        if pool not in self.readings:
            try:
                self.readings[pool] = read_pool(self.db, pool, self.now())
            except Exception as exc:  # noqa: BLE001 - a record, never a refusal
                # Explaining a refusal must never turn into one: the denial
                # stands with the transaction's own figures.
                log.warning("pool %s could not be read to explain a refusal: %s",
                            pool, type(exc).__name__)
                self.readings[pool] = None
        return self.readings[pool]

    def explain(self, blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`blockers` with each pool limit's applied value, ceiling and reason.

        Every key admission wrote is kept as it was (`pool`, `reason`, `limit`,
        `active`), so every reader of `blocked_by` reads what it read before.
        A blocker with no pool (`not_ready`, `task_missing`) is left alone.
        """
        out: list[dict[str, Any]] = []
        for blocker in blockers:
            pool = blocker.get("pool")
            if not isinstance(pool, str) or not pool:
                out.append(blocker)
                continue
            reading = self._reading(pool)
            applied = _int(blocker.get("limit"))
            entry = dict(blocker)
            entry["limit_name"] = pool
            entry["applied"] = applied
            entry["in_use"] = _int(blocker.get("active"))
            entry["ceiling"] = reading.ceiling if reading is not None else None
            entry["lowered_by"] = lowered_by(reading, applied) if reading is not None else []
            entry["inputs_read_at"] = reading.read_at if reading is not None else None
            out.append(entry)
        return out


def add_to_report(limits: dict[str, dict[str, Any]], blockers: list[dict[str, Any]]) -> None:
    """Sum explained blockers into a drain report's `limits`, per limit.

    `refused` counts the refusals that named the limit. `applied` is the
    lowest value applied in the drain and `in_use` the most units found in use,
    the two readings that explain a refusal; `ceiling` and `lowered_by` are the
    latest. A blocker without a pool is not a limit and is not summed.
    """
    for blocker in blockers:
        pool = blocker.get("limit_name")
        if not isinstance(pool, str):
            continue
        row = limits.setdefault(
            pool,
            {"reason": blocker.get("reason"), "refused": 0, "applied": None,
             "ceiling": None, "in_use": None, "lowered_by": []},
        )
        row["refused"] += 1
        row["reason"] = blocker.get("reason")
        applied = blocker.get("applied")
        if isinstance(applied, int) and (row["applied"] is None or applied < row["applied"]):
            row["applied"] = applied
        in_use = blocker.get("in_use")
        if isinstance(in_use, int) and (row["in_use"] is None or in_use > row["in_use"]):
            row["in_use"] = in_use
        row["ceiling"] = blocker.get("ceiling")
        row["lowered_by"] = list(blocker.get("lowered_by") or [])
