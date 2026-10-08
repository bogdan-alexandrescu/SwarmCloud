"""Submission logic: everything between a validated request and Firestore.

Two decisions here are worth reading before changing anything.

FIRST, a submitted task is written straight to READY (or PARKED), never to a
state that costs compute. SUBMITTED -> QUEUED -> READY is walked through
`assert_transition` so the state machine still proves the path is legal, but
only the end state is persisted: three writes per task would triple the cost of
a 100-task batch to prove something the type system already knows.

SECOND, the API does NOT decide whether the tenant has a credential for the
task's runner profile. It used to: a task whose tenant had no key for the
profile's provider was PARKED as CREDENTIAL_MISSING here, at submission. That
was a copy of a rule that also lived in the scheduler's admission and in its
Cloud Run dispatcher, and the copies disagreed about the account pool -- a
tenant with no key of its own can run on an account it owns or is lent, which
neither this copy nor admission's knew (#169). The rule now has one statement,
`scheduler/credentials.py`, and this service cannot import it: its image does
not carry the scheduler.

So a task with no dependencies is written READY, and ADMISSION parks it on
CREDENTIAL_MISSING if the tenant can run it on neither a key nor a pool
account -- before any lease, so nothing is reserved and no container starts,
which is what the park here was for. READY costs nothing (invariant 1), and
the submission's wake message means that drain is seconds away. The caller
sees READY in the response and PARKED on its next read. It is still not
rejected: the tenant may register a key, or be lent an account, a minute
later, and the scheduler's credential sweep re-readies it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Sequence

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

from . import gitidentity
from .auth import AuthContext
from .codec import hard_limit_known, quota_to_api
from .cifix import stamp as stamp_ci_fix
from .continuation import NamedPull, resolve_continuation, resolve_merge_pr
from .access import MODES, grant_id_for
from .errors import Forbidden, NotFound, ValidationFailed
from .expected_outputs import expected_outputs_by_step, record_expected_outputs
from .forge import GIT_PROVIDER
from .forgeapp import GRANTS, Caller
from .gittokens import Scope, provider_suffix
from .metrics import ApiMetrics
from .repositories import Repositories, repo_id_for
from .runnerinputs import input_contract
from .schemas import TaskCreate, WorkflowCreate, WorkflowStepCreate
from .served_limits import configured_limits
from .settings import ApiSettings
from .specsigning import SpecSigner, sign_task_specs
from .store import Store
from .task_accounts import ACCOUNT_TOKEN_ENV
from .validation import (
    APP_CREDENTIAL_PROVIDERS,
    DISPATCH_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    INPUT_LAYOUT_BY_PARENT,
    MERGE_METADATA_KEY,
    MERGE_STEP_MAX_ATTEMPTS,
    SERVICE_FORGE_ACCESS,
    SINGLE_PR,
    DispatchOptionError,
    DispatchOptions,
    MergePlan,
    SinglePrPlan,
    StepSpec,
    check_repository_ref,
    is_merge_step,
    is_mergeable_forge,
    merge_repository,
    merge_step_for,
    plan_merge,
    refuse_unmergeable_forge,
    refuse_worker_action_outside_single_pr,
    reject_non_finite,
    reject_reserved_metadata,
    resolve_dispatch_options,
    resolve_input_layout,
    resolve_integrator_step,
    resolve_merge_choice,
    resolve_merge_fix_rounds,
    github_repository,
    is_service_submitter,
    repository_not_granted,
    validate_batch_size,
    validate_dag,
    validate_input_size,
    validate_resource_class_override,
    validate_runner_input,
    validate_runner_profile,
    validate_step_routing,
    validate_storable,
    validate_timeout,
    workflow_label,
)
from .waker import SchedulerWaker, ring

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
        signer: SpecSigner | None = None,
        forge_tokens: Any = None,
        forge_writer: Any = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._waker = waker
        self._metrics = metrics
        self._now = now
        #: Signs each step's canonical spec (contract request 34). None only
        #: in local development: `build_context` refuses a hardened
        #: environment without a key.
        self._signer = signer
        #: The tenant's `-git` token reader and the pinned GitHub client, for
        #: the one read a `merge_pr` workflow makes at submission (#352,
        #: `continuation.resolve_merge_pr`). Neither builds a client until used.
        self._forge_tokens = forge_tokens
        self._forge_writer = forge_writer

    def _sign(self, tasks: Sequence[Task]) -> None:
        """After every write to the tasks, immediately before the store call.

        A refusal is counted like every other rejected submission; a KMS
        failure is 503 and, like the refusal, nothing is stored.
        """
        try:
            sign_task_specs(tasks, self._signer)
        except ValidationFailed as exc:
            self._metrics.tasks_rejected.labels(reason=exc.code).inc()
            raise

    def _resolve_forge(self, tenant_id: str, tasks: Sequence[Task], *, service: bool) -> None:
        """Set `forge_credential` and `forge_access` on every task (#780 OB7, D4).

        BEFORE `_sign`, so the signature covers both (contract request 54).
        A person's task on a GitHub repository runs with the person's own
        user slot, `git-u-<hex>`, and their grant's mode; a person with no
        grant on it is refused, every refused task named in one 403
        (`validation.repository_not_granted`), before anything is stored. A
        service submission runs with the tenant token, `git`, with write. A
        task with no repository, or not on GitHub, is left with neither.

        Never from the request: `TaskCreate` and `WorkflowCreate` refuse both
        names (`extra="forbid"`), and each task's `submitted_by` is the
        authenticated caller's, a child's parent's, or the stored submitter
        `run_owner_auth` and its kind rebuild (invariant 10).
        """
        refused: list[tuple[str | None, str]] = []
        for task in tasks:
            named = github_repository(task.repository_url)
            if named is None:
                continue
            if service:
                # The tenant's own slot, the one every task read before #780.
                task.forge_credential = GIT_PROVIDER
                task.forge_access = SERVICE_FORGE_ACCESS
                continue
            owner, repo = named
            mode = self._grant_mode(tenant_id, task.submitted_by or "",
                                    repo_id_for(tenant_id, owner, repo))
            if mode is None:
                refused.append((task.step_id, f"{owner}/{repo}"))
                continue
            task.forge_credential = provider_suffix(Scope.USER, user=task.submitted_by)
            task.forge_access = mode
        if refused:
            error = repository_not_granted(refused)
            self._metrics.tasks_rejected.labels(reason=error.code).inc()
            log.info("submission refused tenant=%s code=%s repositories=%s",
                     tenant_id, error.code, ",".join(error.detail["repositories"]))
            raise error

    def _grant_mode(self, tenant_id: str, email: str, repo_id: str) -> str | None:
        """The mode of `email`'s grant on `repo_id` in `tenant_id`, or None.

        The document `AccessService.grant` writes, read by the id it is
        stored under and checked again on its own fields, as `AccessService._doc`
        checks it: another person's or another tenant's grant is no grant
        (invariant 9). A mode that is neither `read` nor `write` is no grant
        either, so a damaged document refuses rather than writes.
        """
        if not email:
            return None
        caller = Caller(email=email, tenant_id=tenant_id)
        snap = self._store.db.collection(GRANTS).document(
            grant_id_for(tenant_id, email, repo_id)).get()
        doc = snap.to_dict() if snap.exists else None
        if (
            not doc
            or doc.get("tenant_id") != tenant_id
            or doc.get("user") != caller.key
            or doc.get("repo_id") != repo_id
        ):
            return None
        mode = doc.get("mode")
        return mode if mode in MODES else None

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
        ctx: AuthContext | None,
        now: datetime,
        dispatch: DispatchOptions,
        workflow_id: str | None = None,
        step_id: str | None = None,
        depends_on: Sequence[str] = (),
        resource_class_override: str | None = None,
        priority: int | None = None,
        repository_url: str | None = None,
        repository_ref: str | None = None,
        submitted_by: str | None = None,
        git_identity: dict[str, str] | None = None,
    ) -> Task:
        # `submitted_by` is set only by the child route (swarm_api.children),
        # which has no person on the call: a child's submitter is its parent's,
        # so the person behind the tree sees it. Every other caller passes ctx.
        if submitted_by is None:
            assert ctx is not None
            submitted_by = ctx.email
        profile = validate_runner_profile(spec.runner_profile)
        # A worker action (merge, post-verdict) only inside a `single-pr`
        # workflow (#295). A workflow's steps were already checked by
        # `validate_step_routing`; this is what refuses one as a plain task.
        refuse_worker_action_outside_single_pr(profile, dispatch.strategy, step_id=step_id)
        # A short sha fails in the clone, after admission (F4): refused here,
        # for a task, a batch, a workflow step and a child alike.
        check_repository_ref(repository_ref or spec.repository_ref)
        validate_input_size(spec.input, self._settings.core.max_input_bytes)
        # NaN and +/-Infinity, before the declaration, so the refusal names the
        # path rather than a bound a NaN compares false against (#294).
        reject_non_finite(spec.input, step_id=step_id)
        # After the size, so an oversized input is refused for its size. What
        # it may carry is the catalogue's declaration (contract request 25).
        validate_runner_input(
            profile, spec.input, step_id=step_id,
            repository_url=repository_url or spec.repository_url,
        )
        # After the declaration, so a declared key out of range is refused
        # naming its bound; this is the store's own limit, for every profile.
        validate_storable(spec.input, step_id=step_id)
        # The CALLER's metadata is what the 16 KiB limit measures, which is why
        # this runs before the dispatch block is added below. The block this
        # service adds is two short strings, plus -- on an integrator only -- one
        # task id per upstream step, so `max_workflow_steps` is its ceiling.
        reject_reserved_metadata(spec.metadata)
        validate_input_size(spec.metadata, 16 * 1024, label="metadata")
        reject_non_finite(spec.metadata, label="metadata", step_id=step_id)
        validate_storable(spec.metadata, label="metadata")
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
        # Who the agent's commits name (P37, `gitidentity`): the authenticated
        # caller unless the caller of this method resolved someone else (a
        # child's parent, a service account's continued task). Inside the
        # dispatch block, which the signature covers and no caller can write.
        if git_identity is None:
            git_identity = (
                gitidentity.for_caller(ctx.email, ctx.display_name)
                if ctx is not None
                else gitidentity.for_caller(submitted_by)
            )
        gitidentity.record_on(metadata[DISPATCH_METADATA_KEY], git_identity, submitted_by)

        # Walk the real state machine even though only the end state is stored.
        assert_transition(TaskState.SUBMITTED, TaskState.QUEUED)
        park_reason: ParkReason | None = None
        if depends_on:
            assert_transition(TaskState.QUEUED, TaskState.PARKED)
            state = TaskState.PARKED
            park_reason = ParkReason.DEPENDENCY_INCOMPLETE
        else:
            # NOT parked on a missing credential here. Whether this tenant can
            # run this profile -- on a key of its own or on a pool account it
            # may use -- is admission's question, asked once
            # (scheduler/credentials.py). See the module docstring.
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
            submitted_by=submitted_by,
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

    def submit_tasks(
        self, ctx: AuthContext, specs: Sequence[TaskCreate], *, service_submission: bool = False
    ) -> SubmissionResult:
        """`service_submission` is set by the platform's own automation that
        submits as the person who registered a repository (`repoindex`), never
        by a route from a request: the owner's D4 for automation (2026-10-07)
        runs repository indexing with the tenant token."""
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
        # #780 OB7: whose GitHub credential each task runs with, before the
        # signature so it covers them.
        self._resolve_forge(
            tenant.tenant_id, tasks,
            service=service_submission or is_service_submitter(ctx.email),
        )
        # Contract request 34: signed over the task as it will be stored.
        self._sign(tasks)
        self._store.create_tasks(tasks, tenant_member=ctx.tenant_member)
        for task in tasks:
            self._metrics.tasks_submitted.labels(
                tenant=task.tenant_id, runner_profile=task.runner_profile
            ).inc()
        woke = self._wake("task_submitted", tenant_id=tenant.tenant_id, count=str(len(tasks)))
        return SubmissionResult(tasks=tasks, woke_scheduler=woke)

    # -- workflows --------------------------------------------------------

    def _continued_task(self, tenant_id: str, continuation: Any) -> Task | None:
        """The task a continuation names, for whose person its commits carry.

        Unfiltered by submitter, as `resolve_continuation` reads it: a
        continuation-scoped account continues a task someone else submitted.
        None when it has gone since that check, so the bot is named.
        """
        task_id = continuation.task_id or continuation.root_task_id
        try:
            return self._store.get_task(tenant_id, task_id, submitted_by=None)
        except NotFound:
            return None

    def submit_workflow(self, ctx: AuthContext, spec: WorkflowCreate) -> WorkflowSubmission:
        # A continuation-scoped caller (a listed service account, contract
        # request 30) reaches this route because it is in CONTINUATION_ROUTES,
        # but being on the allow-list opens the ROUTE, not every request to it:
        # it may submit a workflow that continues an existing task and nothing
        # else. Before `tenant_for`, so a refused request writes nothing.
        # getattr: `continues_task` arrives with #273's schema; until then no
        # spec carries one, and this scope can submit no workflow at all --
        # closed, which is the direction to be wrong in.
        if ctx.member_scope == "continuation" and getattr(spec, "continues_task", None) is None:
            raise Forbidden(
                "a continuation-scoped account may only submit a workflow with "
                "continues_task; it cannot start new work"
            )
        tenant = self.tenant_for(ctx)
        integrator_step_id: str | None = None
        try:
            # The WORKFLOW's own metadata, up front and inside this try. It is
            # copied onto every step's task below, so a reserved key here would
            # otherwise reach the root steps verbatim and be silently replaced
            # on a step that declares its own `input_from` (#151), or on every
            # upstream step by the recorded `expected_outputs` (#149).
            # `_build_task` would refuse it too, but only mid-loop, outside this
            # try, so the refusal would go uncounted. BEFORE `validate_dag`: a
            # workflow-level `metadata.input_from` has no valid form to check the
            # filenames of, so it answers 422 `invalid_dispatch` whatever its
            # value, and a step's own `input_from` is the only declaration
            # `validate_dag` ever sees (#151).
            reject_reserved_metadata(spec.metadata)
            # Copied onto every step's task, so refused once, here, without a
            # step id: it is the workflow's (#294).
            reject_non_finite(spec.metadata, label="metadata")
            # Every step clones it, so refused once, here and counted, rather
            # than by `_build_task` on the first step (F4).
            check_repository_ref(spec.repository_ref)
            # Inside the try, because resolving each step's `input_layout`
            # (#75) can refuse, and a refusal is counted like every other.
            step_specs = self._step_specs(spec)
            order = validate_dag(step_specs, max_steps=self._settings.core.max_workflow_steps)
            # A pull request no workflow opened (#352): checked against the
            # tenant's registry and read from GitHub once, before the
            # continuation (a `continues_task` beside it is refused for
            # naming two) and before the dispatch options, because it
            # supplies the repository they require. A continuation-scoped
            # caller was refused above: `merge_pr` carries no continues_task.
            named_pull = self._named_pull(tenant, spec) if spec.merge_pr else None
            # Before the dispatch options, because a continuation supplies the
            # repository they require (#263, see continuation.py).
            # A continuation-scoped account continues a `direct-pr` task or an
            # integrate workflow's integrator, never a merge (continuation.py).
            continuation = resolve_continuation(
                self._store, tenant.tenant_id, spec,
                continuation_scoped=ctx.member_scope == "continuation",
            )
            # The CI fixer's record (#263): checked, counted against its
            # per-pull-request cap, and stamped for the post-back, before
            # anything is built from the workflow's metadata (cifix.py).
            spec = stamp_ci_fix(self._store, tenant.tenant_id, spec, continuation)
            repository_url = (
                continuation.repository_url if continuation
                else named_pull.repository_url if named_pull
                else spec.repository_url
            )
            dispatch = resolve_dispatch_options(
                strategy=spec.strategy,
                carrier=spec.carrier,
                scale="workflow",
                repository_url=repository_url,
            )
            if continuation:
                dispatch = replace(dispatch, continues=continuation.root_task_id)
            # The merge step (contract request 47): appended here, once the
            # strategy is known and before anything is built or signed, then
            # checked with the rest of the graph.
            merged_spec = self._with_merge_step(
                spec, step_specs, dispatch.strategy, repository_url,
                continued_task=continuation.task_id if continuation else None,
            )
            if merged_spec is not spec:
                spec = merged_spec
                step_specs = self._step_specs(spec)
                order = validate_dag(
                    step_specs, max_steps=self._settings.core.max_workflow_steps
                )
            if dispatch.strategy == "integrate":
                # After validate_dag, which has already rejected the cycles and
                # dangling dependencies this would otherwise have to reason about.
                integrator_step_id = resolve_integrator_step(step_specs)
            # A verdict gate and a `builds_on` (#264), once the integrator is
            # known: under `integrate` only the integrator may be gated. Under
            # `single-pr` this is also where the chain's shape is refused or
            # planned (#295), and where a worker-action profile under any other
            # strategy is refused -- AFTER each step's profile is known to be
            # one the platform runs, so a disabled merge-chain profile is
            # refused as disabled, with the reason, before anything about where
            # it was placed (test_merge_chain_profiles_are_refused_on_submission).
            for step in spec.steps:
                validate_runner_profile(step.runner_profile)
            single_pr = validate_step_routing(
                step_specs,
                strategy=dispatch.strategy,
                integrator_step_id=integrator_step_id,
            )
            merge_plan = plan_merge(
                step_specs, dispatch.strategy,
                continuation.task_id if continuation else None,
                named_pull=named_pull is not None,
            )
            if merge_plan is not None:
                # At submission, never at merge time: a host no `ForgeMerger`
                # serves is refused before any step runs.
                refuse_unmergeable_forge(repository_url)
            # Every step's profile and input, before any task is built and
            # inside this try, so a refused step is counted like every other
            # refusal. The size first, as `_build_task` orders them, which
            # checks each step again; by then neither can fail.
            for step in spec.steps:
                profile = validate_runner_profile(step.runner_profile)
                validate_input_size(step.input, self._settings.core.max_input_bytes)
                reject_non_finite(step.input, step_id=step.step_id)
                validate_runner_input(
                    profile, step.input, step_id=step.step_id,
                    repository_url=spec.repository_url,
                )
                validate_storable(step.input, step_id=step.step_id)
                # The step's own metadata, merged as `_build_task` will store
                # it, under the same rules as any task's metadata.
                reject_reserved_metadata(step.metadata)
                step_metadata = {**spec.metadata, **step.metadata}
                # Lane MS1: the CI-fix rounds a merge step may spend, as each
                # step's task will store them.
                resolve_merge_fix_rounds(step_metadata, merge_step=merge_plan is not None)
                validate_input_size(step_metadata, 16 * 1024, label="metadata")
                reject_non_finite(step.metadata, label="metadata", step_id=step.step_id)
                validate_storable(step_metadata, label="metadata", step_id=step.step_id)
        except ValidationFailed as exc:
            self._metrics.tasks_rejected.labels(reason=exc.code).inc()
            raise

        by_id = {s.step_id: s for s in spec.steps}
        layout_of = {s.step_id: s.input_layout for s in step_specs}
        # The review a gated integrator reads its verdict from is not one of
        # the branches it merges (#264). A review's deliverable is its verdict
        # file; it changes no files, so it pushes no branch, and the integrator
        # would report it "not found" on the pull request -- and if it did edit
        # the repository, those edits are exactly the unreviewed work the gate
        # exists to keep out.
        not_integrated: frozenset[str] = frozenset()
        if integrator_step_id is not None and by_id[integrator_step_id].when is not None:
            not_integrated = frozenset({by_id[integrator_step_id].when.step})
        now = self._now()
        workflow_id = new_id("wf")

        tasks: list[Task] = []
        steps: list[WorkflowStep] = []
        # Topological order, so a child task document is never written before
        # the parent it names in `depends_on`.
        step_task_id: dict[str, str] = {}
        # Read once, at submission (#638): the gated step files its review's
        # minors on this issue, and a later change reaches later workflows.
        findings_epic = self._store.get_findings_epic(tenant.tenant_id)
        # Lane MS1, read once at submission like the epic: the default branch
        # the tenant registered this repository with, which the merge step
        # refuses any other base against (`base_not_default`, MS3).
        merge_base = (
            self._registered_base(tenant.tenant_id, repository_url)
            if merge_plan is not None else None
        )
        # Every step's commits name the workflow's submitter (P37). A
        # continuation a service account submitted names the person behind
        # the task it continues instead, else the bot (`gitidentity`).
        git_identity = gitidentity.for_continuation(
            ctx.email, ctx.display_name,
            self._continued_task(tenant.tenant_id, continuation) if continuation else None,
        )
        for step_id in order:
            source = by_id[step_id]
            parent_task_ids = [step_task_id[dep] for dep in source.depends_on]
            dispatch_for_step = self._step_dispatch(
                dispatch,
                step_id=step_id,
                integrator_step_id=integrator_step_id,
                order=order,
                step_task_id=step_task_id,
                not_integrated=not_integrated,
                single_pr=single_pr,
                merge_plan=merge_plan,
                merge_base=merge_base,
                named_pull=named_pull,
            ).with_routing(
                # Both name upstream steps, so topological order has
                # already minted their task ids.
                builds_on=step_task_id[source.builds_on] if source.builds_on else None,
                gate_task_id=step_task_id[source.when.step] if source.when else None,
                gate_verdicts=source.when.verdict_in if source.when else (),
                allow_empty_diff=source.allow_empty_diff,
                # Kept on a gated step only (`with_routing`): the MERGE path's
                # pull request title when the implementer wrote none.
                pr_label=workflow_label(spec.metadata),
                # Kept on a gated step only too: the step that reads the
                # verdict files its minors on the tenant's epic.
                findings_epic=findings_epic,
            )
            if source.input_from and layout_of[step_id] == INPUT_LAYOUT_BY_PARENT:
                # Each parent's STEP id beside the task id the worker sees in
                # `metadata.input_from`, so it can stage under the step id (#75).
                dispatch_for_step = dispatch_for_step.with_input_parents(
                    {step_task_id[src]: src for src in source.input_from}
                )
            task = self._build_task(
                spec=TaskCreate(
                    runner_profile=source.runner_profile,
                    input=source.input,
                    priority=spec.priority,
                    metadata={**spec.metadata, **source.metadata, "workflow_step": step_id},
                    timeout_seconds=source.timeout_seconds,
                    # A merge step waits for CI by failing its attempt while a
                    # required check runs, so it gets more attempts than an
                    # agent step (`MERGE_STEP_MAX_ATTEMPTS`, contract request 47).
                    max_attempts=(
                        MERGE_STEP_MAX_ATTEMPTS if is_merge_step(source.runner_profile) else None
                    ),
                ),
                tenant=tenant,
                ctx=ctx,
                now=now,
                dispatch=dispatch_for_step,
                workflow_id=workflow_id,
                step_id=step_id,
                depends_on=parent_task_ids,
                resource_class_override=source.resource_class,
                priority=spec.priority,
                repository_url=repository_url,
                repository_ref=spec.repository_ref,
                git_identity=git_identity,
            )
            # The one place `metadata.input_from` is written. After `_build_task`,
            # which refused the key in the caller's metadata, so what lands here
            # is only ever this rewrite of a step declaration the DAG check has
            # already accepted (#151).
            if source.input_from:
                task.metadata[INPUT_FROM_METADATA_KEY] = {
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
        # Each UPSTREAM step's task records the files its dependants stage from
        # it, so the worker can tell that agent to write them where they are
        # uploaded (#149). Set before the store call: it is part of the write
        # that creates the task document, never a second update.
        expected = expected_outputs_by_step((s.step_id, s.input_from) for s in spec.steps)
        for task in tasks:
            record_expected_outputs(task.metadata, expected.get(task.step_id or ""))
        # Contract request 34: AFTER `metadata.input_from` and
        # `record_expected_outputs`, both written after `_build_task`, and
        # immediately before the write that creates the documents. #780 OB7
        # first: every step's GitHub credential, so the signature covers it,
        # and the whole workflow refused when any step's repository is not
        # granted to the person.
        self._resolve_forge(tenant.tenant_id, tasks, service=is_service_submitter(ctx.email))
        self._sign(tasks)
        self._store.create_workflow(workflow, tasks, tenant_member=ctx.tenant_member)
        self._metrics.workflows_submitted.labels(tenant=tenant.tenant_id).inc()
        self._wake("workflow_submitted", tenant_id=tenant.tenant_id, workflow_id=workflow_id)
        return WorkflowSubmission(
            workflow=workflow,
            tasks=tasks,
            dispatch=dispatch,
            integrator_step_id=integrator_step_id,
        )

    @staticmethod
    def _step_specs(spec: WorkflowCreate) -> list[StepSpec]:
        """The DAG-relevant slice of every step, each `input_layout` resolved (#75)."""
        return [
            StepSpec(
                step_id=s.step_id,
                depends_on=tuple(s.depends_on),
                # The filenames too, not only the parent ids: validate_dag
                # refuses two parents staging one filename, and a filename
                # that is absolute or traverses, before anything is created
                # (#64) -- unless the step stages by parent (#75).
                input_from=dict(s.input_from),
                when_step=s.when.step if s.when else None,
                when_verdicts=tuple(s.when.verdict_in) if s.when else (),
                builds_on=s.builds_on,
                input_layout=resolve_input_layout(
                    spec.metadata, s.metadata, step_id=s.step_id
                ),
                # By name only, to tell a worker action from an agent
                # step, and the step's declared part in a `single-pr`
                # chain (#295); `validate_step_routing` checks both.
                runner_profile=s.runner_profile,
                pr_role=s.pr_role,
                merges=dict(s.merges) if s.merges is not None else None,
            )
            for s in spec.steps
        ]

    def _with_merge_step(
        self,
        spec: WorkflowCreate,
        step_specs: Sequence[StepSpec],
        strategy: str,
        repository_url: str | None,
        *,
        continued_task: str | None = None,
    ) -> WorkflowCreate:
        """`spec`, with a `merge` step appended when the merge choice says so.

        Contract request 47, owner decisions 2026-10-04. The choice is the
        workflow's `metadata.merge` ("on" | "off"), else the platform's
        `merge_by_default` (`Store.get_platform_settings`, default off):

          * a spec that states its own merge step keeps it -- unless
            `metadata.merge` is "off", which contradicts it and is refused;
          * otherwise, when the choice is on and the workflow opens ONE pull
            request (`validation.merge_sources`), one merge step is appended,
            depending on the step that opens it and on the review if there is
            one, before anything is built or signed;
          * "on" for a workflow that opens no single pull request is refused,
            because the caller asked for a merge that would not happen; the
            platform default simply does not apply to one;
          * the platform default does not apply to a repository on a host no
            `ForgeMerger` serves either: the worker harvests a patch there and
            opens no pull request, so a tenant who never asked for a merge is
            not refused for an admin's setting. An explicit "on", or a stated
            merge step, on such a host is still refused at submission.

        `single-pr` ends in its own merge step and is left as submitted.
        """
        if strategy == SINGLE_PR:
            return spec
        choice = resolve_merge_choice(spec.metadata)
        stated = [s.step_id for s in spec.steps if is_merge_step(s.runner_profile)]
        if stated:
            if choice == "off":
                raise DispatchOptionError(
                    f"metadata.{MERGE_METADATA_KEY} is 'off', but step "
                    f"{stated[0]!r} is a merge step. Remove one or the other.",
                    detail={"merge_steps": stated, MERGE_METADATA_KEY: choice},
                )
            return spec
        if choice == "off":
            return spec
        if choice is None and (
            not is_mergeable_forge(repository_url)
            or not self._store.get_platform_settings().get("merge_by_default")
        ):
            return spec
        appended = merge_step_for(step_specs, strategy, continued_task)
        if appended is None:
            if choice == "on":
                raise DispatchOptionError(
                    f"metadata.{MERGE_METADATA_KEY} is 'on', but this workflow opens no "
                    "single pull request to merge: "
                    + ("under 'direct-pr' every agent step opens its own, so a merge "
                       "needs exactly one agent step; use 'integrate'."
                       if strategy == "direct-pr" else
                       f"strategy {strategy!r} opens none.")
                    + f" Set metadata.{MERGE_METADATA_KEY} to 'off', or change the strategy.",
                    detail={"strategy": strategy, MERGE_METADATA_KEY: choice},
                )
            return spec
        return spec.model_copy(
            update={"steps": [*spec.steps, WorkflowStepCreate.model_validate(appended)]}
        )

    def _named_pull(self, tenant: Tenant, spec: WorkflowCreate) -> NamedPull | None:
        """`spec.merge_pr`, checked (`continuation.resolve_merge_pr`)."""
        if self._forge_tokens is None or self._forge_writer is None:
            raise DispatchOptionError(
                "merge_pr reads the pull request from GitHub at submission, and this "
                "deployment has no forge reader configured.",
                detail={"merge_pr": spec.merge_pr.model_dump() if spec.merge_pr else None},
            )
        return resolve_merge_pr(
            spec, tenant,
            repositories=Repositories(self._store.db, now=self._now),
            tokens=self._forge_tokens,
            writer=self._forge_writer,
        )

    def _registered_base(self, tenant_id: str, repository_url: str | None) -> str | None:
        """The default branch `tenant_id` registered this repository with, or None.

        Read from the tenant's own registration only (`Repositories.find`
        checks the tenant), by the id `repositories.register` stored it
        under, so another tenant's registration of the same repository never
        names this workflow's base. None when unregistered: the worker then
        asks GitHub for the default branch itself (MS3).
        """
        named = merge_repository(repository_url)
        if named is None:
            return None
        record = Repositories(self._store.db, now=self._now).find(
            tenant_id, repo_id_for(tenant_id, *named)
        )
        branch = (record or {}).get("default_branch")
        return branch if isinstance(branch, str) and branch else None

    @staticmethod
    def _step_dispatch(
        dispatch: DispatchOptions,
        *,
        step_id: str,
        integrator_step_id: str | None,
        order: Sequence[str],
        step_task_id: dict[str, str],
        not_integrated: frozenset[str] = frozenset(),
        single_pr: SinglePrPlan | None = None,
        merge_plan: MergePlan | None = None,
        merge_base: str | None = None,
        named_pull: NamedPull | None = None,
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
        graph's only sink, so that prefix is in fact every other step -- less
        `not_integrated`, the review a gated integrator reads (#264).

        Under `single-pr` (#295) every step gets its `pr_role`, and the task ids
        its role needs: a reader or amender the author's (`pr_author`), the
        post-verdict step the review's (`verdict_source`), the merge step every
        chain step's (`merges`). Every one names an ANCESTOR of the step --
        `resolve_single_pr` has checked the author precedes everything, the
        review precedes post-verdict, and the merge is the only sink -- so
        topological order has already minted each id. All of it is inside the
        dispatch block, which the spec signature covers.
        """
        if single_pr is not None:
            role = single_pr.roles[step_id]
            return dispatch.with_pr_role(
                role,
                pr_author=(
                    step_task_id[single_pr.author] if role in ("reader", "amender") else None
                ),
                verdict_source=(
                    step_task_id[single_pr.review] if step_id == single_pr.post_verdict else None
                ),
                merges=(
                    {key: step_task_id[sid] for key, sid in single_pr.merges_steps().items()}
                    if step_id == single_pr.merge else None
                ),
            )
        if merge_plan is not None and step_id == merge_plan.merge_step:
            # The merge step's target, by TASK id (contract request 47): every
            # step it names is one it depends on, so topological order has
            # minted each id already.
            sources = merge_plan.sources
            if sources.named and named_pull is not None:
                # No task opened it (#352): the number and the head the
                # caller named, as `resolve_merge_pr` checked them.
                return dispatch.with_merge_target(
                    number=named_pull.number, head_sha=named_pull.head_sha, base=merge_base,
                )
            return dispatch.with_merge_target(
                pull_request=(
                    sources.pull_request if sources.continued
                    else step_task_id[sources.pull_request]
                ),
                review=step_task_id[sources.review] if sources.review else None,
                verdict_file=sources.verdict_file,
                base=merge_base,
            )
        if integrator_step_id is None:
            return dispatch
        if step_id != integrator_step_id:
            return dispatch.with_role("contributor")
        upstream = [
            sid for sid in order[: list(order).index(step_id)] if sid not in not_integrated
        ]
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
            # U26: whether that False is a resumed switch or a missing
            # document (`Store.get_control`); `unknown` / `missing` for the
            # second, which the scheduler treats as dispatching.
            "dispatch_state": control.get("dispatch_state"),
            "control_document": control.get("control_document"),
            # Configured, never remaining: see `swarm_api.served_limits` for
            # why no headroom figure is served. /v1/version calls the same
            # helper.
            "limits": configured_limits(self._settings),
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
            # A POOL WITH NO LIMIT SET IS UNKNOWN, NOT 0 (#374), exactly as
            # `waiting.waiting_for` reads it: its stand-in 0 would serve this
            # profile a measured headroom of 0 and a TENANT_LIMIT-at-0 blocker,
            # the untruth `pool_to_api` stopped serving one step removed. A
            # paused one stays: a pause refuses at any limit, so it is known.
            limit_unset = [
                n for n, p in readable.items() if not hard_limit_known(p) and p.enabled
            ]
            for n in limit_unset:
                del readable[n]
            unread_pools = [] if listing_complete else [n for n in required if n not in by_name]
            profiles[name] = {
                "resource_class": profile.resource_class,
                "backend": backend,
                "provider": profile.provider,
                # WHETHER IT MAY BE DISPATCHED AT ALL, read off the frozen
                # catalogue exactly as /v1/runtimes serves it. Without these
                # two keys every client check of `available` on this route was
                # dead code, and a disabled profile (codex) was drawn with
                # headroom on Pools and Profile headroom and offered on Submit
                # (visual QA 2026-09-25, CP-3). The admission block below is
                # still served for a disabled profile: it is a true statement
                # about the pools, and the screens decide not to draw it as an
                # offer.
                "available": profile.available,
                "disabled_reason": profile.disabled_reason,
                "units": units,
                "pools": required,
                # What this profile's RUNNER refuses to start without, so a
                # submit form can refuse locally instead of spending a slot on
                # an attempt that cannot succeed. Served as data for the same
                # reason `blocked_reason_groups` is: a client that restated it
                # would be a second copy of a worker rule nothing checks.
                "input_contract": input_contract(profile),
                "admission": analyse_profile(
                    required=required,
                    pools=readable,
                    units=units,
                    # Absent from a COMPLETE listing means unconfigured, which
                    # is unlimited by construction. Absent from a truncated one
                    # means unread, and the two must never be conflated.
                    unread=sorted(unread_pools + limit_unset),
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

    def providers(
        self,
        ctx: AuthContext,
        accounts: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Which providers this tenant can run on, and on which credential.

        A KEY OR A POOL ACCOUNT, the way admission reads it (#76, from #171's
        second review). `credential_registered` used to be `name in
        tenant.credentials` alone, so a keyless tenant that an account LENT to
        it serves read "no credential" while admission
        (`scheduler.credentials.credential_for`) admitted its tasks and they
        ran. It is now true when either serves, and `credential_source` says
        which, in admission's own words: `tenant_key` (asked first, as there)
        or `account_pool`. `runnable_profiles` narrows it the same way: an
        account is a Claude subscription and runs only a profile whose
        `secrets` take `CLAUDE_CODE_OAUTH_TOKEN`, so `browser` is not runnable
        on one however many a tenant is lent.

        WHICH ACCOUNTS COUNT: one this tenant owns or is lent, for this
        provider, in any state -- `quota_broker.accounts.accounts_serving`,
        which admission asks. A paused or spent account is a wait the worker
        parks on, not a missing credential. A withdrawn loan counts for
        nothing: the account no longer names this tenant in `lend_to`.

        `accounts` is the broker's `list_accounts`, or None on a deployment
        with no broker -- where, as in admission, the pool serves nobody. It
        is asked only when some provider has no key. `account_pool` on the
        page says what was read, so a `false` beside a broker that could not
        be reached is not read as "nobody lent you anything".
        """
        tenant = self.tenant_for(ctx)
        quota_by_provider = {q.provider: q for q in self._store.list_quota(ctx.tenant_id)}
        # Never a retired #295 App key (`git-review`, which the frozen
        # catalogue's disabled post-verdict entry still names): nothing reads
        # one, so listing it would offer a tenant a credential to register
        # that no route accepts and no Job reads.
        named = {p.provider for p in RUNNER_PROFILES.values() if p.provider}
        listed = sorted(named - APP_CREDENTIAL_PROVIDERS)
        keyed = set(tenant.credentials or ())
        pool_read, served = self._pool_providers(
            tenant.tenant_id, accounts, any(name not in keyed for name in listed)
        )
        entries = []
        for name in listed:
            quota = quota_by_provider.get(name)
            profiles = sorted(p.name for p in RUNNER_PROFILES.values() if p.provider == name)
            on_account = sorted(
                p.name
                for p in RUNNER_PROFILES.values()
                if p.provider == name and ACCOUNT_TOKEN_ENV in (p.secrets or ())
            )
            if name in keyed:
                source, runnable = "tenant_key", profiles
            elif name in served and on_account:
                source, runnable = "account_pool", on_account
            else:
                source, runnable = None, []
            entries.append(
                {
                    "provider": name,
                    "credential_registered": source is not None,
                    "credential_source": source,
                    "runnable_profiles": runnable,
                    "runner_profiles": profiles,
                    "quota": quota_to_api(quota) if quota else None,
                }
            )
        return {
            "tenant_id": ctx.tenant_id,
            "providers": entries,
            "account_pool": pool_read,
            "generated_at": self._now(),
        }

    def _pool_providers(
        self,
        tenant_id: str,
        accounts: Callable[[str], dict[str, Any]] | None,
        needed: bool,
    ) -> tuple[str, set[str]]:
        """What the account pool was read as, and the providers it serves this tenant.

        `not_asked` when every provider has a key, `not_configured` with no
        broker, `unreadable` when the broker could not answer, `read`
        otherwise. A failed read degrades the page, never fails it: the key
        half of the answer needs no broker.
        """
        if not needed:
            return "not_asked", set()
        if accounts is None:
            return "not_configured", set()
        try:
            listing = accounts(tenant_id)
        except Exception as exc:
            log.warning(
                "providers: the account pool could not be read",
                extra={"tenant_id": tenant_id, "error": type(exc).__name__},
            )
            return "unreadable", set()
        served: set[str] = set()
        for account in listing.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            # `Account.may_serve`, on the served shape: owned, or lent and not
            # withdrawn. The broker narrows its listing the same way; this
            # holds it to that rather than trusting a listing's scope.
            lend_to = account.get("lend_to") or ()
            if account.get("owner_tenant") == tenant_id or tenant_id in lend_to:
                provider = account.get("provider")
                if isinstance(provider, str) and provider:
                    served.add(provider)
        return "read", served

    # -- plumbing ---------------------------------------------------------

    def _wake(self, reason: str, **attributes: str) -> bool:
        """Best effort. The submission is already durable when this runs.

        Only a configured waker that failed is counted; a deployment with no
        topic drains on the Cloud Scheduler safety tick by design (`ring`).
        """
        return ring(self._waker, self._metrics, reason, **attributes)
