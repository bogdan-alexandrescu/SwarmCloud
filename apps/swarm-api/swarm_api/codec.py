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

from .validation import DEFAULT_CARRIER, DEFAULT_STRATEGY, DISPATCH_METADATA_KEY


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


def dispatch_of(task: Task) -> dict[str, Any]:
    """The EFFECTIVE dispatch options for a task, always complete.

    `task.metadata["dispatch"]` is where they are stored, and it is absent on
    every task submitted before this feature existed. Absent means today's
    behaviour -- harvest the patch, push nothing -- so it reads back as
    `collect`/`checkpoints` rather than as null. A caller reading this key never
    has to know that the encoding lives in metadata, or that a task may predate
    it.
    """
    raw = task.metadata.get(DISPATCH_METADATA_KEY)
    block = raw if isinstance(raw, dict) else {}
    return {
        "strategy": block.get("strategy") or DEFAULT_STRATEGY,
        "carrier": block.get("carrier") or DEFAULT_CARRIER,
        # None on everything but an `integrate` workflow's steps.
        "role": block.get("role"),
        "integrates": list(block.get("integrates") or ()),
    }


def workflow_dispatch(tasks: Any) -> dict[str, Any]:
    """A workflow's dispatch options, read back from the tasks that carry them.

    The frozen `Workflow` dataclass has no metadata field, so there is nowhere
    on the workflow document to store them; every task of a workflow is written
    with the same strategy and carrier, so any one of them answers. The per-step
    ROLE is not reported here because it differs by step -- the integrator is
    named instead, which is the part a caller wants from the workflow level.
    """
    strategy, carrier = DEFAULT_STRATEGY, DEFAULT_CARRIER
    integrator_task_id: str | None = None
    for task in tasks:
        options = dispatch_of(task)
        strategy, carrier = options["strategy"], options["carrier"]
        if options["role"] == "integrator":
            integrator_task_id = task.id
            break
    return {
        "strategy": strategy,
        "carrier": carrier,
        "integrator_task_id": integrator_task_id,
    }


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
        # THE FENCING PAIR, which this serialiser never emitted.
        #
        # CONTRACT invariant 5 -- a stale worker exits without running the
        # agent -- turns on `current_generation`, and `task_from_dict` above has
        # always read both fields back. Neither ever reached a caller, so the
        # platform's core safety mechanism was invisible through the API and no
        # screen could show it. The live case: task_b5dc2568713a40158851 sat
        # DISPATCHED for twenty minutes at generation 2 while its
        # `current_lease_id` named an UNRELEASED generation-1 lease whose
        # attempt never started, so the slot stayed held for work that could
        # never run. The one number that says so was not served.
        #
        # THAT TASK'S DOCUMENT STILL PROVES THE POINT after the reconciler
        # reclaimed it (`release_reason: reconciler:missing_execution`) and the
        # retry succeeded: it is SUCCEEDED with `current_generation` 3 and
        # `attempt_count` 2. Generation above attempt count is the permanent
        # record that a stale worker was fenced, and it was unreadable through
        # every API a person or a screen could call.
        #
        # Neither is withheld material. The docstring above promises no
        # credential material and no backend spec: a generation is a small
        # integer and the lease id is already public -- `lease_to_api` serves
        # `lease_id` itself, and that lease's own generation, to the same
        # callers. Nothing in this function's history ever removed them; they
        # were absent from the first commit that wrote it.
        #
        # ALWAYS EMITTED, INCLUDING AS 0 AND None. Zero is an answer -- the
        # task has never been admitted -- and it is what makes
        # `current_generation > attempt_count` ("a stale worker was fenced")
        # readable. A `or None` here would turn that answer back into "not
        # reported", the conflation this codec has spent days removing.
        "current_generation": task.current_generation,
        "current_lease_id": task.current_lease_id,
        "workflow_id": task.workflow_id,
        "step_id": task.step_id,
        "depends_on": task.depends_on,
        "cancel_requested": task.cancel_requested,
        "metadata": task.metadata,
        # Also inside `metadata`, which is where it is STORED. It is lifted out
        # here so a caller reads the effective values -- including on a task
        # that predates the feature and has no block -- without knowing the
        # encoding.
        "dispatch": dispatch_of(task),
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


def stored_event_type(data: dict[str, Any]) -> EventType:
    """The type a stored event RECORDS, which for one legacy shape is not its field.

    Until 2026-09-24 `Store.request_cancel` wrote a flag-only cancel -- a task
    that still held capacity, so nothing was cancelled -- as `type: cancelled`
    with `detail.phase: "cancel_requested"`. Contract request 17 gave that its
    own type, CANCEL_REQUESTED, and the API writes it now. The events already
    stored keep the old shape: nothing rewrites them, because a migration is a
    write to every task's history for a fact this one line can read correctly.

    So the old shape is read HERE, the one decoder every event route goes
    through, and every API reader -- the console, `swarm_follow`, `swarm tail`
    -- gets one vocabulary whenever the event was written. Only that exact
    shape: a `cancelled` with `phase: "cancelled"` (the immediate path) or with
    no phase (the scheduler's cascade, the worker, the reconciler) was a real
    cancel and stays one. The detail is served as stored, so the served legacy
    event is the same shape as a new one, `phase` included.
    """
    kind = EventType(data["type"])
    detail = data.get("detail")
    if (
        kind is EventType.CANCELLED
        and isinstance(detail, dict)
        and detail.get("phase") == EventType.CANCEL_REQUESTED.value
    ):
        return EventType.CANCEL_REQUESTED
    return kind


def event_from_dict(data: dict[str, Any]) -> TaskEvent:
    return TaskEvent(
        event_id=data["event_id"],
        task_id=data["task_id"],
        tenant_id=data["tenant_id"],
        type=stored_event_type(data),
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
        # THE FIVE SPEND FIELDS, which this decoder used to drop.
        #
        # `control.record_spend` merge-sets all five into the attempt document
        # and they arrive intact -- but they were never read back here, so the
        # dataclass defaults applied and `attempt_to_api` faithfully served
        # None for every attempt that ever ran. Every cost and token figure in
        # the product was unreachable, and the serialiser's own comment blamed
        # a worker fix that had already shipped.
        #
        # `peak_rss_bytes` above is the control: same document, same decoder,
        # and it round-tripped throughout. Exactly these five were missing.
        #
        # NOTE the `is None` checks rather than `or`: 0 tokens and $0.00 are
        # measurements, and `data.get(k) or None` would turn a real zero back
        # into "not measured" -- the same conflation this codebase has spent
        # three days removing, reintroduced in the line that fixes it.
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        cache_read_input_tokens=data.get("cache_read_input_tokens"),
        cache_creation_input_tokens=data.get("cache_creation_input_tokens"),
        cost_usd=data.get("cost_usd"),
        # CONTRACT REQUEST #15 (accepted on #184, 2026-09-25): the attempt's
        # CPU, written by `control.record_cpu_usage`. `is None`-safe for the
        # reason the spend fields are: an idle agent's 0.0 cores is a
        # measurement, and `or None` would erase it.
        cpu_seconds=data.get("cpu_seconds"),
        peak_cpu_cores=data.get("peak_cpu_cores"),
        mean_cpu_cores=data.get("mean_cpu_cores"),
        cpu_limit_cores=data.get("cpu_limit_cores"),
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
        #
        # `dispatch_overdue` no longer carries a `state is LEASED` guard. The
        # old comment here argued the guard was safe "because nothing ever
        # writes STARTING or RUNNING to a lease document". That premise was
        # true and the conclusion did not follow: the state that ends the
        # window is DISPATCHED, written by `mark_dispatched` as soon as the
        # backend accepts the create call. So the flag read false on every
        # lease whose dispatch was in flight -- the only population it is for.
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
        # NULL IS NOT ZERO: a caller must render an em dash, never $0.00, or a
        # run with no measurement reads as a free one.
        #
        # This comment used to say "null until the worker fix ships in an
        # agent-runtime-base image". That was false by the time anyone read it:
        # the worker records all five, and `attempt_from_dict` was silently
        # dropping them on the way back out. A comment naming the wrong cause
        # is worse than none -- it was read, believed, and cited as the reason
        # AgentDetail.tsx omits the cost columns, so a working feature stayed
        # hidden behind an explanation that had stopped being true.
        "input_tokens": attempt.input_tokens,
        "output_tokens": attempt.output_tokens,
        "cache_read_input_tokens": attempt.cache_read_input_tokens,
        "cache_creation_input_tokens": attempt.cache_creation_input_tokens,
        "cost_usd": attempt.cost_usd,
        # The attempt's CPU (request #15), on EVERY row: serving it costs no
        # read the route was not already making. It replaced #188's opt-in
        # `include=usage`, which read the task's events per request. Null is
        # not measured, never zero.
        "cpu_seconds": attempt.cpu_seconds,
        "peak_cpu_cores": attempt.peak_cpu_cores,
        "mean_cpu_cores": attempt.mean_cpu_cores,
        "cpu_limit_cores": attempt.cpu_limit_cores,
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


def workflow_to_api(
    workflow: Workflow, rollup: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Public JSON shape for a workflow.

    `state` SERVES THE DERIVED VALUE when a rollup is supplied, and the value
    read out of Firestore is served beside it as `stored_state`. That is a
    deliberate change of meaning for an existing field and it is the point of the
    change: every consumer already asks "what state is this workflow in" by
    reading `.state`, and until now the answer was QUEUED forever because nothing
    advanced the stored field. Leaving `.state` faithful to the document would
    have preserved the defect for the sake of a fidelity nobody asked for.

    `rollup` is None only on a path that did not read the steps. The field then
    reports the stored value and says so, rather than implying it was confirmed.
    """
    served = dict(rollup or {})
    state = served.pop("state", None) or workflow.state.value
    return {
        "workflow_id": workflow.workflow_id,
        "tenant_id": workflow.tenant_id,
        "state": state,
        #: Always the value in Firestore. Kept so the cache can be audited, and
        #: so a consumer that specifically wants the document gets the document.
        "stored_state": workflow.state.value,
        "state_source": "derived" if rollup else "stored",
        **served,
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
