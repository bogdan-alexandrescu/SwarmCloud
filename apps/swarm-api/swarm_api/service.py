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
    DISPATCH_METADATA_KEY,
    DispatchOptions,
    StepSpec,
    reject_reserved_metadata,
    resolve_dispatch_options,
    resolve_integrator_step,
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


@dataclass(frozen=True)
class WorkflowSubmission:
    """What a workflow submission produced, including the dispatch it resolved.

    The dispatch options are returned alongside the workflow rather than read
    off it because the frozen `Workflow` dataclass has no metadata field to
    hold them -- they are stored once per TASK. A create response has to echo
    what was accepted, so the value travels out of here directly.
    """

    workflow: Workflow
    tasks: list[Task]
    dispatch: DispatchOptions
    #: The step that integrates the others, or None when the strategy is not
    #: `integrate`.
    integrator_step_id: str | None


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
        # BEFORE ensure_tenant, because ensure_tenant CREATES on first sight.
        #
        # terraform/infra/variables.tf refuses to let a tenant's principal
        # appear in secret_admin_members, for a reason it states at length: a
        # secret admin holds secretVersionAdder on every tenant's provider-key
        # secrets, and while that cannot read a key in place it can REPLACE one
        # with a key pointing at attacker-controlled infrastructure, after which
        # the victim tenant's prompts, source and output all flow through it.
        #
        # That validation can only see tenants DECLARED in var.tenants. This
        # method creates one for any allowed-domain caller who has never been
        # seen before, which is the path that actually fired: on 2026-09-21
        # admin@saga.xyz signed in to the web UI and tenant u-admin was written,
        # with the platform's secret admin as its principal.
        #
        # 403 rather than a silent skip: the caller is authenticated and known,
        # and the honest answer is that this identity may not own a tenant --
        # not that it has one which happens to be empty.
        principal = (ctx.tenant_principal or ctx.email or "").strip().lower()
        forbidden = {p.strip().lower() for p in self._settings.secret_admin_principals if p.strip()}
        if principal and principal in forbidden:
            raise Forbidden(
                f"{principal} administers every tenant's provider-key secrets and "
                "therefore may not own a tenant of its own: a tenant whose principal "
                "can add a secret version to another tenant's key can redirect that "
                "tenant's work through infrastructure it controls. Sign in as an "
                "ordinary user, or declare this principal in terraform's `tenants` "
                "and remove it from `secret_admin_members`."
            )

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

    def scope_for(self, ctx: AuthContext) -> str:
        """The tenant id this caller may READ, with the collision check applied.

        `tenant_for` above is the entry for paths that CREATE work: it writes
        the tenant document on first sight and refuses a disabled tenant. A read
        must do neither -- a GET that creates a document is a surprise, and a
        disabled tenant still has to see and cancel what it already has running
        -- but it must still refuse a caller whose tenant id belongs to a
        different principal. That is the whole of `Store.assert_tenant_scope`,
        and why it is a separate call rather than a flag on this one.
        """
        self._store.assert_tenant_scope(
            ctx.tenant_id, ctx.tenant_principal or ctx.email
        )
        return ctx.tenant_id

    # -- tasks ------------------------------------------------------------

    def _build_task(
        self,
        *,
        spec: TaskCreate,
        tenant: Tenant,
        ctx: AuthContext,
        now: datetime,
        dispatch: DispatchOptions,
        workflow_id: str | None = None,
        step_id: str | None = None,
        depends_on: Sequence[str] = (),
        resource_class_override: str | None = None,
        priority: int | None = None,
        repository_url: str | None = None,
        repository_ref: str | None = None,
    ) -> Task:
        profile = validate_runner_profile(spec.runner_profile)
        validate_input_size(spec.input, self._settings.core.max_input_bytes)
        # The CALLER's metadata is what the 16 KiB limit measures, which is why
        # this runs before the dispatch block is added below. The block this
        # service adds is two short strings, plus -- on an integrator only -- one
        # task id per upstream step, so `max_workflow_steps` is its ceiling.
        reject_reserved_metadata(spec.metadata)
        validate_input_size(spec.metadata, 16 * 1024, label="metadata")
        resource_class = validate_resource_class_override(profile, resource_class_override)
        timeout = validate_timeout(profile, spec.timeout_seconds)

        # `dispatch` is resolved by the CALLER of this method, because the rules
        # differ by scale: a standalone task may not ask for `integrate`, and a
        # workflow resolves one integrator for all of its steps. `spec.strategy`
        # and `spec.carrier` are deliberately not read here -- `submit_workflow`
        # synthesises a TaskCreate whose defaults would otherwise silently
        # override the workflow's choice.
        metadata = dict(spec.metadata)
        metadata[DISPATCH_METADATA_KEY] = dispatch.to_metadata()

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
            metadata=metadata,
            # Exactly one of these two is ever set. A standalone task carries
            # its own repository on the spec; a workflow names one repository
            # for all of its steps and `submit_workflow` passes it here, into a
            # synthesised TaskCreate that has none of its own.
            repository_url=repository_url or spec.repository_url,
            repository_ref=repository_ref or spec.repository_ref,
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
            # Inside the try so a refused dispatch is counted like every other
            # rejected submission rather than being invisible to the metric.
            tasks = [
                self._build_task(
                    spec=spec,
                    tenant=tenant,
                    ctx=ctx,
                    now=now,
                    dispatch=resolve_dispatch_options(
                        strategy=spec.strategy,
                        carrier=spec.carrier,
                        # A batch is N INDEPENDENT tasks -- nothing in it
                        # depends on anything else in it -- so every task in a
                        # batch is at task scale, not workflow scale.
                        scale="task",
                        repository_url=spec.repository_url,
                    ),
                )
                for spec in specs
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

    def submit_workflow(self, ctx: AuthContext, spec: WorkflowCreate) -> WorkflowSubmission:
        tenant = self.tenant_for(ctx)
        step_specs = [
            StepSpec(
                step_id=s.step_id,
                depends_on=tuple(s.depends_on),
                input_from=tuple(s.input_from),
            )
            for s in spec.steps
        ]
        integrator_step_id: str | None = None
        try:
            order = validate_dag(step_specs, max_steps=self._settings.core.max_workflow_steps)
            dispatch = resolve_dispatch_options(
                strategy=spec.strategy,
                carrier=spec.carrier,
                scale="workflow",
                repository_url=spec.repository_url,
            )
            if dispatch.strategy == "integrate":
                # After validate_dag, which has already rejected the cycles and
                # dangling dependencies this would otherwise have to reason about.
                integrator_step_id = resolve_integrator_step(step_specs)
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
                dispatch=self._step_dispatch(
                    dispatch,
                    step_id=step_id,
                    integrator_step_id=integrator_step_id,
                    order=order,
                    step_task_id=step_task_id,
                ),
                workflow_id=workflow_id,
                step_id=step_id,
                depends_on=parent_task_ids,
                resource_class_override=source.resource_class,
                priority=spec.priority,
                repository_url=spec.repository_url,
                repository_ref=spec.repository_ref,
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
        return WorkflowSubmission(
            workflow=workflow,
            tasks=tasks,
            dispatch=dispatch,
            integrator_step_id=integrator_step_id,
        )

    @staticmethod
    def _step_dispatch(
        dispatch: DispatchOptions,
        *,
        step_id: str,
        integrator_step_id: str | None,
        order: Sequence[str],
        step_task_id: dict[str, str],
    ) -> DispatchOptions:
        """The dispatch block for ONE step of a workflow.

        Only `integrate` gives its steps distinct roles, so every other strategy
        hands every step the same options.

        The integrator's `integrates` list is the tasks of every step that comes
        before it in TOPOLOGICAL order, which is the order its patches must be
        applied in. Taking the prefix of `order` rather than "every step except
        this one" is what makes the ids already known: the loop assigns task ids
        as it walks `order`, and a step that comes after the integrator would not
        have one yet. `resolve_integrator_step` guarantees the integrator is the
        graph's only sink, so that prefix is in fact every other step.
        """
        if integrator_step_id is None:
            return dispatch
        if step_id != integrator_step_id:
            return dispatch.with_role("contributor")
        upstream = list(order[: list(order).index(step_id)])
        return dispatch.with_role(
            "integrator", integrates=[step_task_id[sid] for sid in upstream]
        )

    # -- read models ------------------------------------------------------

    def stats(self, ctx: AuthContext) -> dict[str, Any]:
        # Through the collision check, never the raw `ctx.tenant_id`: these
        # counts are this tenant's, and two unrelated principals can hold the
        # same id string. See `scope_for`.
        tenant_id = self.scope_for(ctx)
        control = self._store.get_control()
        self._metrics.dispatch_paused.set(1 if control.get("dispatch_paused") else 0)
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "tasks_by_state": self._store.count_tasks_by_state(tenant_id),
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
        """Pools this caller is entitled to see, and what refuses each profile.

        A caller sees the shared pools (global, resource, runner, backend,
        provider) plus their OWN tenant pool. Another tenant's pool is not
        listed: its `active` count is a usage signal about that tenant.

        Each runner profile carries an `admission` block computed here by
        `headroom.analyse_profile`, which asks `evaluate_capacity` -- the same
        function the admission transaction calls -- rather than re-deriving its
        arithmetic. It is served rather than left to the client because the two
        clients that computed it themselves both collapsed a LIST of blockers
        to one pool; see the header of `swarm_api/headroom.py` for why a route
        is the remedy here and a parity check is not.
        """
        from .codec import pool_to_api
        from .headroom import analyse_profile, blocked_reason_groups

        # Same guard as `stats`: a pool's `active` count is a usage signal about
        # whoever really owns the id, so the id has to be the checked one.
        tenant_id = self.scope_for(ctx)
        own_tenant_pool = f"tenant:{tenant_id}"
        own_provider_suffix = f":tenant:{tenant_id}"

        # An EXPLICIT page size, because whether the listing was truncated is
        # the difference between "this pool does not exist, so it is unlimited"
        # and "this pool was not read, so nothing is known". `list_pools`
        # defaults to the same 500; naming it here is what makes the comparison
        # below possible at all, and a default that is read but never compared
        # is how a truncated list gets reported as a complete one.
        page = 500
        rows = self._store.list_pools(limit=page)
        # `>=` not `==`: a store that returned more than asked for is still not
        # evidence that there is no next page.
        listing_complete = len(rows) < page

        by_name = {pool.name: pool for pool in rows}
        visible = []
        for pool in rows:
            if not ctx.is_admin:
                # Another tenant's pool leaks that tenant's live usage, so it is
                # filtered here rather than at the route.
                if pool.name.startswith("tenant:") and pool.name != own_tenant_pool:
                    continue
                if ":tenant:" in pool.name and not pool.name.endswith(own_provider_suffix):
                    continue
            visible.append(pool_to_api(pool))
        visible.sort(key=lambda p: p["name"])

        profiles: dict[str, Any] = {}
        for name, profile in RUNNER_PROFILES.items():
            backend = resolve_backend(profile).value
            required = pool_names_for(
                tenant_id=tenant_id,
                provider=profile.provider,
                resource_class=profile.resource_class,
                runner_profile=name,
                backend=backend,
            )
            units = RESOURCE_CLASSES[profile.resource_class].units
            # Narrowed to `required` before the analyser sees it. Every name in
            # `required` is built from THIS caller's tenant id, so none of them
            # is another tenant's pool -- and restricting the map here is what
            # keeps that true if `pool_names_for` ever grows a name that the
            # visibility filter above would have hidden.
            readable = {n: by_name[n] for n in required if n in by_name}
            profiles[name] = {
                "resource_class": profile.resource_class,
                "backend": backend,
                "provider": profile.provider,
                "units": units,
                "pools": required,
                "admission": analyse_profile(
                    required=required,
                    pools=readable,
                    units=units,
                    # Absent from a COMPLETE listing means unconfigured, which
                    # is unlimited by construction. Absent from a truncated one
                    # means unread, and the two must never be conflated.
                    unread=() if listing_complete else [n for n in required if n not in readable],
                ),
            }

        return {
            "tenant_id": tenant_id,
            "pools": visible,
            # False means the pool listing hit its page size, so any required
            # pool missing from it is unread rather than unconfigured.
            "pools_complete": listing_complete,
            "runner_profiles": profiles,
            # Served as data so no client restates the split. The grouping is
            # by remedy: somebody must act, versus waiting is a valid answer.
            "blocked_reason_groups": blocked_reason_groups(),
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
