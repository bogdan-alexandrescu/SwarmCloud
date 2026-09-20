"""Firestore document <-> frozen dataclass conversion.

The dataclasses in `swarm_common.models` are the contract. This module is the
only place that knows how they are spelled as Firestore documents, so a field
rename in a query never silently reads `None`.

Firestore hands datetimes back as `DatetimeWithNanoseconds`, a `datetime`
subclass, so no conversion is needed on the read path; the ISO-string branch
exists for documents written by tooling (seed scripts, the emulator REST API)
rather than by this service.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from swarm_common.models import (
    Attempt,
    Lease,
    QuotaState,
    SlotPool,
    Task,
    TaskEvent,
    Tenant,
    Workflow,
    WorkflowStep,
)
from swarm_common.states import BlockedReason, EventType, ParkReason, TaskState
from swarm_common.models import ProviderState


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"cannot interpret {value!r} as a datetime")


def _required_datetime(value: Any) -> datetime:
    parsed = as_datetime(value)
    if parsed is None:
        raise ValueError("document is missing a required timestamp")
    return parsed


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------

def task_to_firestore(task: Task) -> dict[str, Any]:
    return task.to_firestore()


def task_from_dict(data: dict[str, Any]) -> Task:
    park = data.get("park_reason")
    return Task(
        id=data["id"],
        tenant_id=data["tenant_id"],
        created_at=_required_datetime(data.get("created_at")),
        updated_at=_required_datetime(data.get("updated_at")),
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
    )


def task_to_api(task: Task) -> dict[str, Any]:
    """Public JSON shape. Contains no credential material and no backend spec."""
    return {
        "id": task.id,
        "tenant_id": task.tenant_id,
        "state": task.state.value,
        "runner_profile": task.runner_profile,
        "resource_class": task.resource_class,
        "provider": task.provider,
        "model": task.model,
        "priority": task.priority,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "submitted_by": task.submitted_by,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "timeout_seconds": task.timeout_seconds,
        "next_eligible_at": task.next_eligible_at,
        "park_reason": task.park_reason.value if task.park_reason else None,
        "blocked_by": task.blocked_by,
        "workflow_id": task.workflow_id,
        "step_id": task.step_id,
        "depends_on": task.depends_on,
        "cancel_requested": task.cancel_requested,
        "metadata": task.metadata,
        "repository_url": task.repository_url,
        "repository_ref": task.repository_ref,
        "input": task.input,
        "last_error": task.last_error,
        "result_summary": task.result_summary,
        "latest_checkpoint": task.latest_checkpoint,
    }


# --------------------------------------------------------------------------
# Events / attempts / leases
# --------------------------------------------------------------------------

def event_to_firestore(event: TaskEvent) -> dict[str, Any]:
    d = asdict(event)
    d["type"] = event.type.value
    return d


def event_from_dict(data: dict[str, Any]) -> TaskEvent:
    return TaskEvent(
        event_id=data["event_id"],
        task_id=data["task_id"],
        tenant_id=data["tenant_id"],
        type=EventType(data["type"]),
        at=_required_datetime(data.get("at")),
        attempt_id=data.get("attempt_id"),
        lease_id=data.get("lease_id"),
        generation=data.get("generation"),
        detail=dict(data.get("detail") or {}),
    )


def attempt_from_dict(data: dict[str, Any]) -> Attempt:
    return Attempt(
        attempt_id=data["attempt_id"],
        task_id=data["task_id"],
        tenant_id=data["tenant_id"],
        generation=int(data.get("generation", 0)),
        lease_id=data.get("lease_id", ""),
        backend=data.get("backend", ""),
        created_at=_required_datetime(data.get("created_at")),
        execution_name=data.get("execution_name"),
        started_at=as_datetime(data.get("started_at")),
        completed_at=as_datetime(data.get("completed_at")),
        exit_code=data.get("exit_code"),
        error=data.get("error"),
        peak_rss_bytes=data.get("peak_rss_bytes"),
        peak_disk_bytes=data.get("peak_disk_bytes"),
        oom_near_miss=bool(data.get("oom_near_miss", False)),
        checkpoints=list(data.get("checkpoints") or []),
    )


def lease_from_dict(data: dict[str, Any]) -> Lease:
    return Lease(
        lease_id=data["lease_id"],
        task_id=data["task_id"],
        attempt_id=data.get("attempt_id", ""),
        tenant_id=data["tenant_id"],
        generation=int(data.get("generation", 0)),
        pools=list(data.get("pools") or []),
        units=int(data.get("units", 1)),
        state=TaskState(data.get("state", TaskState.LEASED.value)),
        created_at=_required_datetime(data.get("created_at")),
        dispatch_deadline=_required_datetime(data.get("dispatch_deadline")),
        expires_at=_required_datetime(data.get("expires_at")),
        heartbeat_at=as_datetime(data.get("heartbeat_at")),
        released_at=as_datetime(data.get("released_at")),
        release_reason=data.get("release_reason"),
    )


# --------------------------------------------------------------------------
# Pools / quota / tenants
# --------------------------------------------------------------------------

def pool_from_dict(name: str, data: dict[str, Any]) -> SlotPool:
    return SlotPool(
        name=name,
        hard_limit=int(data.get("hard_limit", 0)),
        adaptive_target=data.get("adaptive_target"),
        quota_derived_limit=data.get("quota_derived_limit"),
        active=int(data.get("active", 0)),
        enabled=bool(data.get("enabled", True)),
        updated_at=as_datetime(data.get("updated_at")) or datetime.now(timezone.utc),
    )


def lease_to_api(lease: Lease) -> dict[str, Any]:
    """Public JSON shape for a lease. No credential material, no backend spec.

    `dispatch_overdue` and `expired` are COMPUTED here rather than left to the
    caller. Both are one-line predicates on the model, and both are exactly
    the kind of thing a UI gets subtly wrong -- `dispatch_overdue` is only
    meaningful while the lease is still LEASED, because nothing ever writes
    STARTING or RUNNING to a lease document. Computing them server-side means
    every caller agrees with the reconciler.
    """
    return {
        "lease_id": lease.lease_id,
        "task_id": lease.task_id,
        "attempt_id": lease.attempt_id,
        "tenant_id": lease.tenant_id,
        "generation": lease.generation,
        "pools": lease.pools,
        "units": lease.units,
        # Only ever LEASED or DISPATCHED. The worker advances the TASK through
        # STARTING and RUNNING and never touches this field, so a UI must not
        # label this column "state" -- see docs/web-ui/02, trap B.
        "dispatch_state": lease.state.value,
        "created_at": lease.created_at,
        "dispatch_deadline": lease.dispatch_deadline,
        "expires_at": lease.expires_at,
        "heartbeat_at": lease.heartbeat_at,
        "released_at": lease.released_at,
        "release_reason": lease.release_reason,
        # `is_released` is a @property while `is_expired` and
        # `dispatch_overdue` are methods. Mixed, on a frozen model, so it
        # cannot be tidied -- calling the property returns a bool and then
        # tries to call it, which fails at runtime rather than at import.
        "released": lease.is_released,
        "expired": lease.is_expired(),
        "dispatch_overdue": lease.dispatch_overdue(),
    }


def attempt_to_api(attempt: Attempt) -> dict[str, Any]:
    """Public JSON shape for one attempt.

    This is the per-attempt record that `result_summary` cannot give you:
    result_summary is written once, at terminal state, so a task that failed
    twice and succeeded on the third try carries only the third attempt's
    numbers. The first two live here.
    """
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "tenant_id": attempt.tenant_id,
        "generation": attempt.generation,
        "lease_id": attempt.lease_id,
        "backend": attempt.backend,
        "execution_name": attempt.execution_name,
        "created_at": attempt.created_at,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "exit_code": attempt.exit_code,
        "error": attempt.error,
        "peak_rss_bytes": attempt.peak_rss_bytes,
        "peak_disk_bytes": attempt.peak_disk_bytes,
        "oom_near_miss": attempt.oom_near_miss,
        "checkpoints": attempt.checkpoints,
        # Null until the worker fix ships in an agent-runtime-base image and
        # attempts run on it. NULL IS NOT ZERO: a caller must render an em
        # dash, never $0.00, or a run with no measurement reads as a free one.
        "input_tokens": attempt.input_tokens,
        "output_tokens": attempt.output_tokens,
        "cache_read_input_tokens": attempt.cache_read_input_tokens,
        "cache_creation_input_tokens": attempt.cache_creation_input_tokens,
        "cost_usd": attempt.cost_usd,
    }


def pool_to_api(pool: SlotPool) -> dict[str, Any]:
    return {
        "name": pool.name,
        "hard_limit": pool.hard_limit,
        "adaptive_target": pool.adaptive_target,
        "quota_derived_limit": pool.quota_derived_limit,
        "effective_limit": pool.effective_limit,
        "active": pool.active,
        "available": pool.available,
        "enabled": pool.enabled,
        "updated_at": pool.updated_at,
    }


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


def quota_to_firestore(state: QuotaState) -> dict[str, Any]:
    d = asdict(state)
    d["state"] = state.state.value
    return d


def quota_to_api(state: QuotaState) -> dict[str, Any]:
    payload = quota_to_firestore(state)
    payload["effective_limit"] = state.effective_limit
    return payload


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


def tenant_to_firestore(tenant: Tenant) -> dict[str, Any]:
    return asdict(tenant)


def tenant_to_api(tenant: Tenant) -> dict[str, Any]:
    """Tenant view for callers.

    `credentials` is a list of PROVIDER NAMES, never key material: registering a
    key is write-only by design and no read path in this service can surface it.
    """
    return {
        "tenant_id": tenant.tenant_id,
        "kind": tenant.kind,
        "principal": tenant.principal,
        "display_name": tenant.display_name,
        "created_at": tenant.created_at,
        "max_active": tenant.max_active,
        "capacity_units": tenant.capacity_units,
        "monthly_budget_usd": tenant.monthly_budget_usd,
        "enabled": tenant.enabled,
        "credentials": sorted(tenant.credentials),
        "service_account": tenant.service_account,
        "gcs_prefix": tenant.gcs_prefix,
        "namespace": tenant.namespace,
    }


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------

def workflow_to_firestore(workflow: Workflow) -> dict[str, Any]:
    d = asdict(workflow)
    d["state"] = workflow.state.value
    return d


def workflow_from_dict(data: dict[str, Any]) -> Workflow:
    return Workflow(
        workflow_id=data["workflow_id"],
        tenant_id=data["tenant_id"],
        created_at=_required_datetime(data.get("created_at")),
        updated_at=_required_datetime(data.get("updated_at")),
        state=TaskState(data["state"]),
        submitted_by=data.get("submitted_by", ""),
        steps=[
            WorkflowStep(
                step_id=s["step_id"],
                runner_profile=s["runner_profile"],
                input=dict(s.get("input") or {}),
                depends_on=list(s.get("depends_on") or []),
                resource_class=s.get("resource_class"),
                input_from=dict(s.get("input_from") or {}),
                timeout_seconds=s.get("timeout_seconds"),
                task_id=s.get("task_id"),
            )
            for s in (data.get("steps") or [])
        ],
        on_step_failure=data.get("on_step_failure", "fail_workflow"),
        priority=int(data.get("priority", 0)),
        cancel_requested=bool(data.get("cancel_requested", False)),
    )


def workflow_to_api(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.workflow_id,
        "tenant_id": workflow.tenant_id,
        "state": workflow.state.value,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
        "submitted_by": workflow.submitted_by,
        "priority": workflow.priority,
        "on_step_failure": workflow.on_step_failure,
        "cancel_requested": workflow.cancel_requested,
        "steps": [
            {
                "step_id": s.step_id,
                "runner_profile": s.runner_profile,
                "resource_class": s.resource_class,
                "depends_on": s.depends_on,
                "input_from": s.input_from,
                "timeout_seconds": s.timeout_seconds,
                "task_id": s.task_id,
                "input": s.input,
            }
            for s in workflow.steps
        ],
    }


def blocked_reason_values() -> list[str]:
    return [r.value for r in BlockedReason]
