"""Firestore document -> frozen dataclass, for the scheduler's hot path.

Deliberately a separate implementation from the API's `swarm_api.codec`: the
scheduler and the API are separate deployables with separate dependency sets,
and making one import the other would couple their release cycles so that an API
change could not ship without redeploying the admission controller.

Both read the same documents described by `swarm_common.models`, which is the
frozen contract, so the shapes cannot drift apart without the contract changing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from swarm_common.models import ProviderState, QuotaState, SlotPool, Task, Tenant
from swarm_common.states import ParkReason, TaskState


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"cannot interpret {value!r} as a datetime")


def _required(value: Any) -> datetime:
    parsed = as_datetime(value)
    if parsed is None:
        raise ValueError("document is missing a required timestamp")
    return parsed


def task_from_dict(data: dict[str, Any]) -> Task:
    park = data.get("park_reason")
    return Task(
        id=data["id"],
        tenant_id=data["tenant_id"],
        created_at=_required(data.get("created_at")),
        updated_at=_required(data.get("updated_at")),
        state=TaskState(data["state"]),
        runner_profile=data["runner_profile"],
        resource_class=data["resource_class"],
        input=dict(data.get("input") or {}),
        submitted_by=data.get("submitted_by", ""),
        provider=data.get("provider"),
        model=data.get("model"),
        priority=int(data.get("priority", 0)),
        metadata=dict(data.get("metadata") or {}),
        repository_url=data.get("repository_url"),
        repository_ref=data.get("repository_ref"),
        timeout_seconds=int(data.get("timeout_seconds", 3600)),
        max_attempts=int(data.get("max_attempts", 3)),
        attempt_count=int(data.get("attempt_count", 0)),
        next_eligible_at=as_datetime(data.get("next_eligible_at")),
        park_reason=ParkReason(park) if park else None,
        blocked_by=list(data.get("blocked_by") or []),
        current_lease_id=data.get("current_lease_id"),
        current_generation=int(data.get("current_generation", 0)),
        workflow_id=data.get("workflow_id"),
        step_id=data.get("step_id"),
        depends_on=list(data.get("depends_on") or []),
        cancel_requested=bool(data.get("cancel_requested", False)),
        started_at=as_datetime(data.get("started_at")),
        completed_at=as_datetime(data.get("completed_at")),
        last_error=data.get("last_error"),
        result_summary=data.get("result_summary"),
        latest_checkpoint=data.get("latest_checkpoint"),
        # Contract request 14: a child's parent, which the dispatcher reads to
        # decide whether the attempt gets a child path, and the child sweeps
        # read to find a parent's children (docs/design/child-tasks.md).
        parent_task_id=data.get("parent_task_id") or None,
        parent_attempt_id=data.get("parent_attempt_id") or None,
    )


#: The blocker reason for a pool whose document carries no `hard_limit` (#374).
#: Not a `BlockedReason`: that enum is frozen, and none of its values is true
#: here -- TENANT_LIMIT / RESOURCE_CLASS_LIMIT "at 0" says somebody set the
#: pool to zero, and nobody did. The UI reads the same string
#: (`apps/swarm-ui/src/types.ts`, `blockerCeiling`).
POOL_LIMIT_UNSET = "POOL_LIMIT_UNSET"


class UnsetLimitPool(SlotPool):
    """A pool whose document has no `hard_limit`: its ceiling is UNKNOWN (#374).

    Not a new type of pool and not a restatement of `SlotPool`: it adds no
    field and overrides nothing, so every reader that takes a `SlotPool` reads
    it exactly as the frozen admission transaction reads the same document --
    `d.get("hard_limit", 0)`, a ceiling of 0, so admission refuses. What it
    adds is that the 0 is KNOWN to be a stand-in, so `hard_limit_known` can
    tell "never set" from "set to 0" wherever the difference is said aloud.
    """


def hard_limit_known(pool: SlotPool) -> bool:
    """False for a pool whose document carried no `hard_limit` (`UnsetLimitPool`)."""
    return not isinstance(pool, UnsetLimitPool)


def pool_from_dict(name: str, data: dict[str, Any]) -> SlotPool:
    """A pool document as a `SlotPool`; an `UnsetLimitPool` when no `hard_limit` was written.

    A missing (or null) `hard_limit` is UNKNOWN, never 0 (#374). The value it
    carries is the 0 the frozen admission transaction reads for the same
    document, so arithmetic agrees with admission; `hard_limit_known` is what
    says it was never set.
    """
    raw_limit = data.get("hard_limit")
    cls = SlotPool if raw_limit is not None else UnsetLimitPool
    return cls(
        name=name,
        hard_limit=int(raw_limit) if raw_limit is not None else 0,
        adaptive_target=data.get("adaptive_target"),
        quota_derived_limit=data.get("quota_derived_limit"),
        active=int(data.get("active", 0)),
        enabled=bool(data.get("enabled", True)),
        updated_at=as_datetime(data.get("updated_at")) or datetime.now(timezone.utc),
    )


def quota_from_dict(data: dict[str, Any]) -> QuotaState:
    return QuotaState(
        provider=data["provider"],
        tenant_id=data["tenant_id"],
        state=ProviderState(data.get("state", ProviderState.UNKNOWN.value)),
        updated_at=as_datetime(data.get("updated_at")) or datetime.now(timezone.utc),
        configured_hard_max=int(data.get("configured_hard_max", 50)),
        adaptive_target=data.get("adaptive_target"),
        quota_derived_limit=data.get("quota_derived_limit"),
        requests_remaining=data.get("requests_remaining"),
        tokens_remaining=data.get("tokens_remaining"),
        reset_at=as_datetime(data.get("reset_at")),
        cooldown_until=as_datetime(data.get("cooldown_until")),
        last_429_at=as_datetime(data.get("last_429_at")),
        retry_after_seconds=data.get("retry_after_seconds"),
        success_count=int(data.get("success_count", 0)),
        rate_limit_count=int(data.get("rate_limit_count", 0)),
    )


def tenant_from_dict(data: dict[str, Any]) -> Tenant:
    return Tenant(
        tenant_id=data["tenant_id"],
        kind=data.get("kind", "user"),
        principal=data.get("principal", ""),
        created_at=as_datetime(data.get("created_at")) or datetime.now(timezone.utc),
        display_name=data.get("display_name"),
        max_active=int(data.get("max_active", 20)),
        capacity_units=int(data.get("capacity_units", 40)),
        monthly_budget_usd=data.get("monthly_budget_usd"),
        enabled=bool(data.get("enabled", True)),
        credentials=list(data.get("credentials") or []),
        service_account=data.get("service_account"),
        gcs_prefix=data.get("gcs_prefix"),
        namespace=data.get("namespace"),
    )
