"""Submission logic: everything between a validated request and Firestore.

Two decisions here are worth reading before changing anything.

FIRST, a submitted task is written straight to READY (or PARKED), never to a
state that costs compute. SUBMITTED -> QUEUED -> READY is walked through
`assert_transition` so the state machine still proves the path is legal, but
only the end state is persisted: three writes per task would triple the cost of
a 100-task batch to prove something the type system already knows.

SECOND, a task whose runner profile needs a provider the tenant has not
registered a key for is PARKED as CREDENTIAL_MISSING at submission. It is not
rejected -- the tenant may register the key a minute later and the reconciler
re-readies it -- and it is not admitted, because admitting it would start a
container that can only fail. Parked costs nothing (invariant 1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from swarm_common.models import (
    Task,
    Tenant,
    Workflow,
    WorkflowStep,
    new_id,
    pool_names_for,
    utcnow,
)
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import ParkReason, TaskState, assert_transition

from .auth import AuthContext
from .codec import quota_to_api
from .errors import Forbidden, ValidationFailed
from .metrics import ApiMetrics
from .schemas import TaskCreate, WorkflowCreate
from .settings import ApiSettings
from .store import Store
from .validation import (
    StepSpec,
    validate_batch_size,
    validate_dag,
    validate_input_size,
    validate_resource_class_override,
    validate_runner_profile,
    validate_timeout,
)
from .waker import SchedulerWaker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmissionResult:
    tasks: list[Task]
    woke_scheduler: bool


class SubmissionService:
    def __init__(
        self,
        *,
        settings: ApiSettings,
        store: Store,
        waker: SchedulerWaker,
        metrics: ApiMetrics,
        now=utcnow,
    ) -> None:
        self._settings = settings
        self._store = store
        self._waker = waker
        self._metrics = metrics
        self._now = now

    # -- tenant -----------------------------------------------------------

    def tenant_for(self, ctx: AuthContext) -> Tenant:
        tenant = self._store.ensure_tenant(
            ctx.tenant_id,
            # The TENANT's principal (the group, for a group tenant), never the
            # caller's: a tenant is shared by every member of its group, and the
            # store compares this against the stored value to refuse two
            # different groups whose ids collide.
            principal=ctx.tenant_principal or ctx.email,
            default_max_active=self._settings.core.default_tenant_max_active,
            default_capacity_units=self._settings.core.default_tenant_capacity_units,
            project_id=self._settings.project_id,
            artifact_bucket=self._settings.core.artifact_bucket,
            service_account_prefix=self._settings.tenant_service_account_prefix,
            namespace_prefix=self._settings.tenant_namespace_prefix,
        )
        if not tenant.enabled:
            raise Forbidden(f"tenant {tenant.tenant_id!r} is disabled")
        return tenant

    # -- tasks ------------------------------------------------------------

    def _build_task(
        self,
        *,
        spec: TaskCreate,
        tenant: Tenant,
        ctx: AuthContext,
        now: datetime,
        workflow_id: str | None = None,
        step_id: str | None = None,
        depends_on: Sequence[str] = (),
        resource_class_override: str | None = None,
        priority: int | None = None,
    ) -> Task:
        profile = validate_runner_profile(spec.runner_profile)
        validate_input_size(spec.input, self._settings.core.max_input_bytes)
        validate_input_size(spec.metadata, 16 * 1024, label="metadata")
        resource_class = validate_resource_class_override(profile, resource_class_override)
        timeout = validate_timeout(profile, spec.timeout_seconds)

        # Walk the real state machine even though only the end state is stored.
        assert_transition(TaskState.SUBMITTED, TaskState.QUEUED)
        park_reason: ParkReason | None = None
        if depends_on:
            assert_transition(TaskState.QUEUED, TaskState.PARKED)
            state = TaskState.PARKED
            park_reason = ParkReason.DEPENDENCY_INCOMPLETE
        elif profile.provider and profile.provider not in tenant.credentials:
            assert_transition(TaskState.QUEUED, TaskState.PARKED)
            state = TaskState.PARKED
            park_reason = ParkReason.CREDENTIAL_MISSING
        else:
            assert_transition(TaskState.QUEUED, TaskState.READY)
            state = TaskState.READY

        return Task(
            id=new_id("task"),
            tenant_id=tenant.tenant_id,
            created_at=now,
            updated_at=now,
            state=state,
            runner_profile=profile.name,
            resource_class=resource_class,
            input=dict(spec.input),
            submitted_by=ctx.email,
            provider=profile.provider,
            model=spec.model,
            priority=spec.priority if priority is None else priority,
            metadata=dict(spec.metadata),
            repository_url=spec.repository_url,
            repository_ref=spec.repository_ref,
            timeout_seconds=timeout,
            max_attempts=spec.max_attempts or 3,
            park_reason=park_reason,
            workflow_id=workflow_id,
            step_id=step_id,
            depends_on=list(depends_on),
        )

    def submit_tasks(self, ctx: AuthContext, specs: Sequence[TaskCreate]) -> SubmissionResult:
        validate_batch_size(len(specs), self._settings.core.max_batch_size)
        tenant = self.tenant_for(ctx)
        now = self._now()
        try:
            tasks = [
                self._build_task(spec=spec, tenant=tenant, ctx=ctx, now=now) for spec in specs
            ]
        except ValidationFailed as exc:
            self._metrics.tasks_rejected.labels(reason=exc.code).inc()
            raise
        self._store.create_tasks(tasks)
        for task in tasks:
            self._metrics.tasks_submitted.labels(
                tenant=task.tenant_id, runner_profile=task.runner_profile
            ).inc()
        woke = self._wake("task_submitted", tenant_id=tenant.tenant_id, count=str(len(tasks)))
        return SubmissionResult(tasks=tasks, woke_scheduler=woke)

    # -- workflows --------------------------------------------------------

    def submit_workflow(self, ctx: AuthContext, spec: WorkflowCreate) -> Workflow:
        tenant = self.tenant_for(ctx)
        step_specs = [
            StepSpec(
                step_id=s.step_id,
                depends_on=tuple(s.depends_on),
                input_from=tuple(s.input_from),
            )
            for s in spec.steps
        ]
        try:
            order = validate_dag(step_specs, max_steps=self._settings.core.max_workflow_steps)
        except ValidationFailed as exc:
            self._metrics.tasks_rejected.labels(reason=exc.code).inc()
            raise

        by_id = {s.step_id: s for s in spec.steps}
        now = self._now()
        workflow_id = new_id("wf")

        tasks: list[Task] = []
        steps: list[WorkflowStep] = []
        # Topological order, so a child task document is never written before
        # the parent it names in `depends_on`.
        step_task_id: dict[str, str] = {}
        for step_id in order:
            source = by_id[step_id]
            parent_task_ids = [step_task_id[dep] for dep in source.depends_on]
            task = self._build_task(
                spec=TaskCreate(
                    runner_profile=source.runner_profile,
                    input=source.input,
                    priority=spec.priority,
                    metadata={**spec.metadata, "workflow_step": step_id},
                    timeout_seconds=source.timeout_seconds,
                ),
                tenant=tenant,
                ctx=ctx,
                now=now,
                workflow_id=workflow_id,
                step_id=step_id,
                depends_on=parent_task_ids,
                resource_class_override=source.resource_class,
                priority=spec.priority,
            )
            if source.input_from:
                task.metadata["input_from"] = {
                    step_task_id[src]: filename for src, filename in source.input_from.items()
                }
            step_task_id[step_id] = task.id
            tasks.append(task)

        for source in spec.steps:
            steps.append(
                WorkflowStep(
                    step_id=source.step_id,
                    runner_profile=source.runner_profile,
                    input=dict(source.input),
                    depends_on=list(source.depends_on),
                    resource_class=source.resource_class,
                    input_from=dict(source.input_from),
                    timeout_seconds=source.timeout_seconds,
                    task_id=step_task_id[source.step_id],
                )
            )

        validate_input_size(
            [s.input for s in spec.steps],
            self._settings.core.max_input_bytes,
            label="workflow input",
        )
        workflow = Workflow(
            workflow_id=workflow_id,
            tenant_id=tenant.tenant_id,
            created_at=now,
            updated_at=now,
            state=TaskState.QUEUED,
            submitted_by=ctx.email,
            steps=steps,
            on_step_failure=spec.on_step_failure,
            priority=spec.priority,
        )
        self._store.create_workflow(workflow, tasks)
        self._metrics.workflows_submitted.labels(tenant=tenant.tenant_id).inc()
        self._wake("workflow_submitted", tenant_id=tenant.tenant_id, workflow_id=workflow_id)
        return workflow

    # -- read models ------------------------------------------------------

    def stats(self, ctx: AuthContext) -> dict[str, Any]:
        control = self._store.get_control()
        self._metrics.dispatch_paused.set(1 if control.get("dispatch_paused") else 0)
        payload: dict[str, Any] = {
            "tenant_id": ctx.tenant_id,
            "tasks_by_state": self._store.count_tasks_by_state(ctx.tenant_id),
            "dispatch_paused": bool(control.get("dispatch_paused")),
            "limits": {
                "max_batch_size": self._settings.core.max_batch_size,
                "max_input_bytes": self._settings.core.max_input_bytes,
                "max_workflow_steps": self._settings.core.max_workflow_steps,
                "requests_per_second_per_instance": self._settings.core.requests_per_second,
            },
            "generated_at": self._now(),
        }
        if ctx.is_admin:
            payload["platform_tasks_by_state"] = self._store.count_tasks_by_state(None)
        return payload

    def capacity(self, ctx: AuthContext) -> dict[str, Any]:
        """Pools this caller is entitled to see.

        A caller sees the shared pools (global, resource, runner, backend,
        provider) plus their OWN tenant pool. Another tenant's pool is not
        listed: its `active` count is a usage signal about that tenant.
        """
        from .codec import pool_to_api

        own_tenant_pool = f"tenant:{ctx.tenant_id}"
        own_provider_suffix = f":tenant:{ctx.tenant_id}"
        visible = []
        for pool in self._store.list_pools():
            if not ctx.is_admin:
                # Another tenant's pool leaks that tenant's live usage, so it is
                # filtered here rather than at the route.
                if pool.name.startswith("tenant:") and pool.name != own_tenant_pool:
                    continue
                if ":tenant:" in pool.name and not pool.name.endswith(own_provider_suffix):
                    continue
            visible.append(pool_to_api(pool))
        visible.sort(key=lambda p: p["name"])
        return {
            "tenant_id": ctx.tenant_id,
            "pools": visible,
            "runner_profiles": {
                name: {
                    "resource_class": profile.resource_class,
                    "backend": resolve_backend(profile).value,
                    "provider": profile.provider,
                    "units": RESOURCE_CLASSES[profile.resource_class].units,
                    "pools": pool_names_for(
                        tenant_id=ctx.tenant_id,
                        provider=profile.provider,
                        resource_class=profile.resource_class,
                        runner_profile=name,
                        backend=resolve_backend(profile).value,
                    ),
                }
                for name, profile in RUNNER_PROFILES.items()
            },
            "generated_at": self._now(),
        }

    def providers(self, ctx: AuthContext) -> dict[str, Any]:
        tenant = self.tenant_for(ctx)
        quota_by_provider = {q.provider: q for q in self._store.list_quota(ctx.tenant_id)}
        entries = []
        for name in sorted({p.provider for p in RUNNER_PROFILES.values() if p.provider}):
            quota = quota_by_provider.get(name)
            entries.append(
                {
                    "provider": name,
                    "credential_registered": name in tenant.credentials,
                    "runner_profiles": sorted(
                        p.name for p in RUNNER_PROFILES.values() if p.provider == name
                    ),
                    "quota": quota_to_api(quota) if quota else None,
                }
            )
        return {"tenant_id": ctx.tenant_id, "providers": entries, "generated_at": self._now()}

    # -- plumbing ---------------------------------------------------------

    def _wake(self, reason: str, **attributes: str) -> bool:
        """Best effort. The submission is already durable when this runs."""
        try:
            woke = self._waker.wake(reason, **attributes)
        except Exception as exc:  # pragma: no cover - transport level
            log.warning("scheduler wake raised: %r", exc)
            woke = False
        # Only a configured waker that failed is worth alerting on; a deployment
        # with no topic drains on the Cloud Scheduler safety tick by design.
        if not woke and getattr(self._waker, "enabled", True):
            self._metrics.wake_failures.inc()
        return woke
