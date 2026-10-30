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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from swarm_common.models import (
    Task,
    Tenant,
    Workflow,
    WorkflowStep,
    new_id,
    pool_names_for,
    utcnow,
)
from swarm_common.profiles import RUNNER_PROFILES, effective_size, resolve_backend
from swarm_common.states import ParkReason, TaskState, assert_transition

from . import budget as budget_mod
from . import gitidentity, publishtopr, runnersizes
from .auth import AuthContext
from .codec import hard_limit_known, quota_to_api
from .cifix import CI_FIX_METADATA_FIELD, stamp as stamp_ci_fix
from .prgate import gate_for, resolve_after
from .continuation import (
    NamedPull,
    resolve_continuation,
    resolve_merge_pr,
    with_merge_pr_label,
)
from .access import MODES, grant_id_for, org_id_for, via_token
from .extrarepos import Limits, Resolver, refuse_unless_enabled, requested_any, runs_an_agent
from . import refusals
from .errors import ApiError, BrowserToolUnavailable, Forbidden, NotFound, ValidationFailed
from .browser_logins import refuse_unless_personal
from .qa_identity import refuse_unless_granted
from .expected_outputs import expected_outputs_by_step, record_expected_outputs
from .forge import (
    FirestoreSlotIndex,
    ForgeSlot,
    NoForgeCredential,
    no_github_credential,
    service_slot,
)
from .forgeapp import GRANTS, ORGS, Caller
from .forge_installation import APP_INSTALLATION_SOURCE, installation_record_in
from .gittokens import Scope, owner_suffix, provider_suffix
from .mcp_credentials import (
    OWNER_TASK,
    OWNER_WORKFLOW,
    McpCredentials,
    RowKeys,
    SecretManagerMcpCredentials,
    check_credential,
    credential_for,
)
from .mcp_servers import (
    McpUnavailable,
    enabled_names,
    profile_allowed,
    read_registration,
    require_enabled as require_mcp_enabled,
    server_id as mcp_server_id,
)
from .metrics import ApiMetrics
from .judgment import TYPESAFE_BACKEND, JudgmentSettings, settings_mac
from .provider_keys import task_key_scope
from .repositories import Repositories, merge_policy_of, repo_id_for
from .mergesettings import MergeSettingsStore
from .runnerinputs import input_contract
from .schemas import TaskCreate, WorkflowCreate, WorkflowStepCreate
from .served_limits import configured_limits
from .settings import ApiSettings
from .specsigning import SpecSigner, sign_task_specs
from .store import Store, derived_service_account
from .task_accounts import ACCOUNT_TOKEN_ENV
from .validation import (
    APP_CREDENTIAL_PROVIDERS,
    AWAIT_STEP_REFUSED_FIELDS,
    BROWSER_TOOL_INPUT_KEY,
    CONFLICT_ROUND_METADATA_KEY,
    TYPESAFE_PROVIDER,
    DISPATCH_METADATA_KEY,
    FORGE_SOURCE_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    INPUT_LAYOUT_BY_PARENT,
    MERGE_FIX_ROUNDS_KEY,
    MERGE_METADATA_KEY,
    MERGE_ON_VERDICT,
    MERGE_STEP_MAX_ATTEMPTS,
    REVIEW_GATE_DEFAULT_BLOCK,
    REVIEW_GATE_METADATA_KEY,
    RUNTIME_METADATA_KEY,
    RuntimePolicyInvalid,
    SERVICE_FORGE_ACCESS,
    SINGLE_PR,
    DispatchOptionError,
    DispatchOptions,
    McpServerProfileRefused,
    McpServerUnregistered,
    MergePlan,
    SinglePrPlan,
    StepSpec,
    check_repository_ref,
    is_merge_step,
    is_mergeable_forge,
    merge_repository,
    merge_sources,
    merge_step_for,
    mcp_server_names,
    plan_merge,
    reader_step_ids,
    refuse_control_plane_integrator,
    refuse_control_plane_outside_workflow,
    refuse_missing_mcp_credentials,
    refuse_uninherited_mcp_servers,
    refuse_unexpected_mcp_credentials,
    refuse_unmergeable_forge,
    rereview_step_for,
    refuse_worker_action_outside_single_pr,
    reject_non_finite,
    reject_reserved_metadata,
    resolve_await_input,
    resolve_dispatch_options,
    resolve_input_layout,
    resolve_integrator_step,
    resolve_merge_choice,
    resolve_fix_rounds,
    resolve_merge_fix_rounds,
    default_review_gate_steps,
    requirements_check_steps,
    resolve_review_gate,
    resolve_runtime_policy,
    AFTER_PRS_METADATA_KEY,
    github_repository,
    is_service_submitter,
    repository_not_granted,
    validate_batch_size,
    validate_dag,
    validate_input_size,
    check_model_class_available,
    resolve_task_model,
    stored_model_class,
    validate_model_class,
    validate_resource_class_override,
    validate_runner_input,
    validate_runner_profile,
    validate_step_routing,
    validate_storable,
    validate_timeout,
    workflow_label,
)
from .waker import SchedulerWaker, ring
from .workspaces import Workspaces

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmissionResult:
    tasks: list[Task]
    woke_scheduler: bool
    #: Per task id, each per-run MCP credential stored for it, as
    #: `{"server", "expires_at"}` (docs/design/registered-mcp-servers.md
    #: §4.3): never the value, its length, a prefix or a hash.
    mcp_credentials: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)


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
    #: The workflow's stored per-run MCP credentials, as `SubmissionResult`'s.
    mcp_credentials: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class McpInheritance:
    """What a child may use of its parent's MCP servers (Q7 of #1433): only
    the servers its parent's SIGNED input names, with the credential rows its
    parent's submission stored (the parent's own, or its workflow's). A child
    has no `mcp_credentials` of its own: the agent writing its spec is the
    party the credential is kept from."""

    servers: tuple[str, ...]
    owners: tuple[str, ...]


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
        workspaces: Workspaces | None = None,
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
        #: Personal workspaces (docs/workspaces.md, #847 W1): the submission
        #: gate, behind WORKSPACE_GATE, which is OFF unless the environment
        #: says on (`workspaces.gate_from_env`). Off, it reads and refuses
        #: nothing.
        self.workspaces = workspaces if workspaces is not None else Workspaces(
            getattr(store, "db", None), now=now,
            console_url=getattr(settings, "console_url", ""))
        #: The per-run MCP credential's Secret Manager writer (docs/design/
        #: registered-mcp-servers.md §4.4), built on first use so constructing
        #: the service opens no connection; a test substitutes one.
        self.mcp_credential_writer: Any = None

    @property
    def signer(self) -> SpecSigner | None:
        """The step-spec signer, which also signs an issue run's owner fields
        (#1569, `issueruns.sign_owner`); None exactly where step specs go
        unsigned, which is where `routes.runs.verify_run_owner` verifies
        nothing. Read-only: nothing outside this service swaps it."""
        return self._signer

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

    def _resolve_forge(self, tenant: Tenant, tasks: Sequence[Task], *, service: bool) -> None:
        """Set `forge_credential` and `forge_access` on every task (#780 OB7, D4).

        BEFORE `_sign`, so the signature covers both (contract request 54).
        A person's task on a GitHub repository runs with the person's own
        user slot, `git-u-<hex>`, and their grant's mode -- their slot for
        that owner (`gittokens.owner_suffix`, D5) when they enabled the
        owner through a fallback token, their App slot otherwise; a person with no
        grant on it is refused, every refused task named in one 403
        (`validation.repository_not_granted`), before anything is stored. A
        service submission runs, with write, on the slot `forge.service_slot`
        picks -- the rule every server-side read resolves through: the
        tenant's `-git` slot when it exists, else a personal tenant's owner's
        GitHub connection (INDEX-CRED: an index run in a personal workspace
        clones with what the owner's Try it checked, not a `-git` slot the
        workspace never had). Which one is recorded in
        `metadata.forge_credential_source`, so the task says so. When the
        submitter is a platform service account and the repository's owner
        has the deployment's GitHub App installed for this tenant (`_app_installed`), the source is `app_installation`
        instead: the slot stays as the rule picked it (the frozen
        `forge_credential` shape has no other name), and the broker mints
        that run an installation token for its one repository at release
        (`credbroker`, DAYONE-SERVICE-APP-TOKEN; lesson 43), recomputing
        the decision from the signed fields, never from this label. A personal
        workspace with no credential at all has its task on a repository
        registered as private refused up front (`no_github_credential`)
        rather than dispatched to a clone that cannot succeed. Only a
        personal one: its credentials are exactly the records its GitHub
        connection writes, while a group tenant's `-git` secret can be stored
        by an operator (`scripts/create-secrets.sh`) with nothing swarm-api
        reads listing it, and refusing there would stop a clone that works.
        A task with no repository, or not on GitHub, is left with neither.

        Never from the request: `TaskCreate` and `WorkflowCreate` refuse both
        names (`extra="forbid"`), and each task's `submitted_by` is the
        authenticated caller's, a child's parent's, or the stored submitter
        `run_owner_auth` and its kind rebuild (invariant 10).
        """
        tenant_id = tenant.tenant_id
        refused: list[tuple[str | None, str]] = []
        uncredentialed: list[str] = []
        resolved: list[ForgeSlot] = []

        installed: dict[str, bool] = {}

        def app_installed(owner: str) -> bool:
            # Once per owner per submission, as the slot is once per batch.
            key = owner.lower()
            if key not in installed:
                installed[key] = self._app_installed(tenant_id, owner)
            return installed[key]

        def on_tenants_behalf(task: Task, owner: str, repo: str, *, app: bool = False) -> None:
            if not resolved:
                # Resolved once per submission: one rule, one answer per batch.
                resolved.append(service_slot(tenant, FirestoreSlotIndex(self._store.db)))
            slot = resolved[0]
            minted = app and app_installed(owner)
            if (not minted and not slot.known and tenant.kind == "user"
                    and self._registered_private(tenant_id, owner, repo)):
                uncredentialed.append(f"{owner}/{repo}")
                return
            task.forge_credential = slot.provider
            task.forge_access = SERVICE_FORGE_ACCESS
            task.metadata[FORGE_SOURCE_METADATA_KEY] = (
                APP_INSTALLATION_SOURCE if minted else slot.source)

        for task in tasks:
            named = github_repository(task.repository_url)
            if named is None:
                continue
            owner, repo = named
            if service:
                # Only a platform service account's own submission: an index
                # run submitted as the person who registered the repository
                # (`service_submission`) keeps the slot, as the broker would.
                on_tenants_behalf(task, owner, repo,
                                  app=is_service_submitter(task.submitted_by))
                continue
            mode = self._grant_mode(tenant_id, task.submitted_by or "",
                                    repo_id_for(tenant_id, owner, repo))
            if mode is None:
                if not getattr(self._settings, "repository_grants_enforced", False):
                    # The switch is off (REPOSITORY_GRANTS_ENFORCED, owner
                    # 2026-10-08): no grant runs on the tenant's own slot, as
                    # before #780, until the migration turns refusal on --
                    # the service rule, so a personal workspace with no `-git`
                    # slot runs on its owner's connection.
                    on_tenants_behalf(task, owner, repo)
                    continue
                refused.append((task.step_id, f"{owner}/{repo}"))
                continue
            task.forge_credential = self._person_slot(tenant_id, task.submitted_by or "", owner)
            task.forge_access = mode
        if refused:
            error = repository_not_granted(refused)
            self._metrics.tasks_rejected.labels(reason=error.code).inc()
            log.info("submission refused tenant=%s code=%s repositories=%s",
                     tenant_id, error.code, ",".join(error.detail["repositories"]))
            raise error
        if uncredentialed:
            names = sorted(set(uncredentialed), key=str.lower)
            self._metrics.tasks_rejected.labels(reason=NoForgeCredential.code).inc()
            log.info("submission refused tenant=%s code=%s repositories=%s",
                     tenant_id, NoForgeCredential.code, ",".join(names))
            raise NoForgeCredential(
                f"{no_github_credential(tenant)}. {', '.join(names)} "
                f"{'is' if len(names) == 1 else 'are'} private, so a run could not clone "
                f"{'it' if len(names) == 1 else 'them'}; nothing was queued.",
                detail={"repositories": names},
            )

    def _resolve_additional(
        self, tenant: Tenant, asked: Sequence[tuple[Task, Sequence[Any] | None]], *,
        service: bool, also_named: Sequence[Sequence[Any] | None] = (),
    ) -> None:
        """Set `additional_repositories` on every task that names some (#1434).

        AFTER `_resolve_forge` and BEFORE `_sign`, so the signature covers the
        list (format 4, contract request 96) and a task that names none keeps
        the bytes and format it had. `asked` pairs each task with the list
        that applies to it -- a workflow step's own, else the workflow's --
        and `also_named` is any list that applies to no task (a workflow's,
        when no step runs an agent), which the flag still refuses. One
        `Resolver` serves the whole submission, so each (repository, ref)
        pins to one commit for every step. Every refusal is counted like
        every other and leaves nothing stored (`extrarepos`).
        """
        lists = [items for _, items in asked] + list(also_named)
        if not requested_any(lists):
            return
        try:
            refuse_unless_enabled(
                bool(getattr(self._settings, "additional_repositories_enabled", False)), lists)
            if self._forge_tokens is None or self._forge_writer is None:
                raise ValidationFailed(
                    "additional_repositories are pinned from GitHub at submission, and this "
                    "deployment has no forge reader configured",
                    detail={"field": "additional_repositories"},
                )
            # Read at THIS submission (owner, 2026-10-17): an admin's change to
            # either limit applies to the next one, with no deploy.
            limits = Limits.from_settings(self._store.get_platform_settings())
            index = FirestoreSlotIndex(self._store.db)
            # One per submitter, which every task of one submission shares:
            # the slot, and so the pin, is the submitter's.
            resolvers: dict[str, Resolver] = {}
            for task, items in asked:
                if not items:
                    continue
                submitter = task.submitted_by or ""
                if submitter not in resolvers:
                    resolvers[submitter] = Resolver(
                        db=self._store.db, now=self._now, tenant=tenant,
                        submitter=submitter, service=service,
                        grants_enforced=bool(getattr(self._settings,
                                                     "repository_grants_enforced", False)),
                        limits=limits, tokens=self._forge_tokens, writer=self._forge_writer,
                        grant_mode=self._grant_mode, person_slot=self._person_slot,
                        service_slot=lambda: service_slot(tenant, index),
                    )
                task.additional_repositories = resolvers[submitter].resolve(
                    task, items, step_id=task.step_id)
        except ApiError as exc:
            if exc.status_code < 500:
                self._metrics.tasks_rejected.labels(reason=exc.code).inc()
                log.info("submission refused tenant=%s code=%s field=additional_repositories",
                         tenant.tenant_id, exc.code)
            raise

    def _app_installed(self, tenant_id: str, owner: str) -> bool:
        """Whether this tenant's own `forge_orgs` say the deployment's GitHub
        App is installed on `owner` (`forge_installation.installation_record_in`,
        never a `method: pat` owner): then a service submission's run gets an
        installation token minted for it at release (DAYONE-SERVICE-APP-TOKEN).
        False -- today's service rule -- when FORGE_INSTALLATION_READS is off,
        which is also when the broker has no minter, or when the read fails."""
        if getattr(self._settings, "forge_installation_reads", False) is not True:
            return False
        try:
            return installation_record_in(self._store.db, tenant_id, owner) is not None
        except Exception as exc:  # noqa: BLE001 -- the type is enough; fall back
            log.warning("app installation lookup failed tenant=%s owner=%s (%s); "
                        "the service rule's slot is used", tenant_id, owner.lower(),
                        type(exc).__name__)
            return False

    def _registered_private(self, tenant_id: str, owner: str, repo: str) -> bool:
        """Whether the tenant registered `owner/repo` and its registration
        says GitHub served it as private (or internal): read by the id it is
        stored under and checked on its tenant (`Repositories.find`). An
        unregistered repository, or one whose visibility was never read, is
        not refused here -- a public repository clones with no token."""
        record = Repositories(self._store.db, now=self._now).find(
            tenant_id, repo_id_for(tenant_id, owner, repo))
        return (record or {}).get("visibility") in ("private", "internal")

    def _person_slot(self, tenant_id: str, email: str, owner: str) -> str:
        """The person's slot for a repository of `owner`: their token for that
        owner when their `forge_orgs` document says they enabled it through
        one (D5), else their App slot. The document is read by id and checked
        on its own fields, as `_grant_mode` checks a grant; the suffix is
        computed from the person and the owner, never taken from it."""
        caller = Caller(email=email, tenant_id=tenant_id)
        snap = self._store.db.collection(ORGS).document(
            org_id_for(tenant_id, email, owner)).get()
        doc = snap.to_dict() if snap.exists else None
        if (doc and doc.get("tenant_id") == tenant_id and doc.get("user") == caller.key
                and str(doc.get("owner") or "") == owner.lower() and via_token(doc)):
            return owner_suffix(email, owner)
        return provider_suffix(Scope.USER, user=email)

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

        # The People screen's sighting (docs/workspaces.md §6.4), here and not
        # in `scope_for`: a read path must not write (see there), and this is
        # the entry every console load (`GET /v1/tenants/me`) and every
        # submission already takes. At most once per ten minutes per person.
        self.workspaces.touch_person(ctx)
        if self.workspaces.withholds_tenant(ctx):
            # WORKSPACE_GATE on: a person's tenant is made by the workspace job
            # (docs/workspaces.md A8), never written here on first sight. Until
            # then they may still sign in and look around, so the read paths
            # get the tenant as it would be, unwritten, and every submission
            # path refuses it in `workspace_gate`.
            existing = self._store.get_tenant(ctx.tenant_id)
            if existing is None:
                return self._unwritten_tenant(ctx)
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

    def _unwritten_tenant(self, ctx: AuthContext) -> Tenant:
        """The personal tenant `Store.ensure_tenant` would write, NOT written.

        The same fields by the same naming rule, so a read of it says what the
        workspace job will make. store.py is not lane W1's file; a write-free
        mode there would replace this."""
        tenant_id = ctx.tenant_id
        return Tenant(
            tenant_id=tenant_id,
            kind="user",
            principal=(ctx.tenant_principal or ctx.email).strip().lower(),
            created_at=self._now(),
            display_name=tenant_id,
            max_active=self._settings.core.default_tenant_max_active,
            capacity_units=self._settings.core.default_tenant_capacity_units,
            service_account=derived_service_account(
                tenant_id, project_id=self._settings.project_id,
                prefix=self._settings.tenant_service_account_prefix,
            ),
            gcs_prefix=f"gs://{self._settings.core.artifact_bucket}/tenants/{tenant_id}",
            namespace=f"{self._settings.tenant_namespace_prefix}{tenant_id}",
        )

    def workspace_gate(self, ctx: AuthContext, tenant: Tenant) -> None:
        """docs/workspaces.md §5: a person's submission into their own tenant
        is refused until its workspace is `ready` and it has a Claude account.

        Called FIRST in both submission methods, right after `tenant_for`,
        before any compile, signature or write, so a refused workflow creates
        no workflow document, no task and no event (§5.3). A group tenant, a
        service account and a listed scoped identity are never judged (WD7).
        With WORKSPACE_GATE off it returns at once."""
        self.workspaces.check(ctx, tenant)

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
        mcp_credentials: Mapping[str, Any] | None = None,
        mcp_whole_submission: bool = True,
        mcp_inherited: McpInheritance | None = None,
        budget_scope: tuple[budget_mod.Limits, budget_mod.Limits] | None = None,
    ) -> Task:
        # `submitted_by` is set only by the child route (swarm_api.children),
        # which has no person on the call: a child's submitter is its parent's,
        # so the person behind the tree sees it. Every other caller passes ctx.
        if submitted_by is None:
            assert ctx is not None
            submitted_by = ctx.email
        profile = validate_runner_profile(spec.runner_profile)
        # An `await-input` step only inside a workflow (docs/design/
        # human-steps.md §4.1): as a task, a batch entry or a child it has no
        # step to answer and no dependant to hand the answer to.
        refuse_control_plane_outside_workflow(profile, workflow_id=workflow_id)
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
        # A session is kept only in a personal workspace (live-browser §5.5.1):
        # a group tenant naming one is told that rule, ahead of the catalogue.
        refuse_unless_personal(tenant, spec.input, step_id=step_id)
        # After the size, so an oversized input is refused for its size. What
        # it may carry is the catalogue's declaration (contract request 25).
        validate_runner_input(
            profile, spec.input, step_id=step_id,
            repository_url=repository_url or spec.repository_url,
        )
        # The QA identity only for a tenant a platform admin granted it to
        # (docs/design/qa-identity.md §4.3): a task, a batch task, a workflow
        # step and a child all come through here, after the catalogue has
        # refused any other value or profile. Signing reads the grant again.
        refuse_unless_granted(self._settings, self._store.db, tenant.tenant_id, spec.input,
                              step_id=step_id)
        # The browser tool it names can run for it (BSTATUS-API): after the
        # declaration, which has already refused a name outside the enum.
        self._refuse_unavailable_browser_tool(
            profile, spec.input, tenant.tenant_id, submitted_by, step_id=step_id,
        )
        # The MCP servers it names are this tenant's, enabled, allowed for the
        # profile, and each `dispatch` one has a credential to use (docs/
        # design/registered-mcp-servers.md §4.3): after the declaration, which
        # has refused a wrong-typed or over-long list, and before anything is
        # stored. A workflow step's map is the workflow's (passed in); a
        # task's is its own. Storing it is the caller's, after the signature.
        self.check_mcp_servers(
            profile, spec.input, tenant.tenant_id,
            credentials=mcp_credentials if mcp_credentials is not None else spec.mcp_credentials,
            whole_submission=mcp_whole_submission, inherited=mcp_inherited, step_id=step_id,
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
        # "Larger" is judged at the CURRENT runner sizes (design §4.8), read
        # only when a step names a class: most submissions name none.
        resource_class = validate_resource_class_override(
            profile, resource_class_override,
            sizes=(runnersizes.current_sizes(self._store.db)
                   if resource_class_override is not None else None),
        )
        timeout = validate_timeout(profile, spec.timeout_seconds)
        # THE BUDGET (#2459), for a task, a batch entry, a workflow step and a
        # child alike: the step's own may only LOWER the limit, and the
        # attempt's is min(step, workspace, ceiling), each unit on its own.
        # `budget_scope` is (workspace default, ceiling), read once by a
        # caller that builds many tasks; read here otherwise.
        step_budget = budget_mod.step_limits(spec.budget)
        workspace_budget, budget_ceiling = budget_scope or self.budget_scope(tenant.tenant_id)
        budget_mod.refuse_step_above(step_budget, workspace_budget, budget_ceiling,
                                     step_id=step_id)
        resolved_budget = budget_mod.resolve(step_budget, workspace_budget, budget_ceiling)
        # The model class, by name, and `Task.model` from its pin (#1435,
        # docs/design/model-per-step.md §4.4). For a task, a batch entry, a
        # workflow step and a child alike: each reaches here with its own
        # `model_class`, and a child naming none runs its OWN profile's
        # default, never its parent's (owner's R3).
        class_models = self._settings.worker_class_models
        model_class = validate_model_class(profile, spec.model_class, step_id=step_id)
        check_model_class_available(profile, model_class, class_models, step_id=step_id)
        model = resolve_task_model(profile, model_class, spec.model, class_models, step_id=step_id)

        # `dispatch` is resolved by the CALLER of this method, because the rules
        # differ by scale: a standalone task may not ask for `integrate`, and a
        # workflow resolves one integrator for all of its steps. `spec.strategy`
        # and `spec.carrier` are deliberately not read here -- `submit_workflow`
        # synthesises a TaskCreate whose defaults would otherwise silently
        # override the workflow's choice.
        metadata = dict(spec.metadata)
        metadata[DISPATCH_METADATA_KEY] = dispatch.to_metadata()
        # The limits go in the typed `Task.budget` below (contract request
        # 120, signed at spec format 6); where each came from goes in the
        # signed dispatch block, which no caller can write
        # (`reject_reserved_metadata`). Only when there is a budget: with
        # none, the task, its format and its block are byte for byte what
        # they were before #2459.
        if resolved_budget is not None:
            metadata[DISPATCH_METADATA_KEY][budget_mod.PROVENANCE_KEY] = budget_mod.provenance(
                step=step_budget, workspace=workspace_budget, ceiling=budget_ceiling)
        # A `browser_if_ui` step (#2322): stored on the browser, with the
        # `code` profile's own fields beside it, each checked here exactly as
        # if the step had named that profile, so a swap at READY can only
        # land on a spec this submission would have accepted. Signed by
        # `_sign` with the rest (`specsigning.sign_task_specs`).
        runtime = resolve_runtime_policy(
            spec.metadata, runner_profile=profile.name, step_id=step_id,
            workflow_step=workflow_id is not None,
        )
        # A review step's gate (#2524), stored normalised: the scheduler holds
        # the step and swarm-api decides it (`reviewgate`).
        review_gate = resolve_review_gate(
            spec.metadata, step_id=step_id, workflow_step=workflow_id is not None,
        )
        if review_gate is not None:
            metadata[REVIEW_GATE_METADATA_KEY] = review_gate
        else:
            # None for no block, and for a malformed one the switch let past.
            metadata.pop(REVIEW_GATE_METADATA_KEY, None)
        if runtime is not None:
            metadata[RUNTIME_METADATA_KEY] = {
                **runtime,
                "alternate": self._runtime_alternate(
                    runtime, spec, tenant=tenant, step_id=step_id,
                    resource_class_override=resource_class_override,
                    repository_url=repository_url, mcp_credentials=mcp_credentials,
                    mcp_whole_submission=mcp_whole_submission, mcp_inherited=mcp_inherited,
                ),
            }
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
            model=model,
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
            budget=resolved_budget.as_dict() if resolved_budget is not None else None,
            park_reason=park_reason,
            workflow_id=workflow_id,
            step_id=step_id,
            depends_on=list(depends_on),
            # Only a class other than the profile's default is stored, so a
            # default step keeps the bytes and the signing format it had
            # before classes existed; a stored class is signed at the format
            # that covers it (`specsign.signing_format`).
            model_class=stored_model_class(profile, model_class),
        )

    def _runtime_alternate(
        self, runtime: Mapping[str, str], spec: TaskCreate, *, tenant: Tenant,
        step_id: str | None, resource_class_override: str | None,
        repository_url: str | None, mcp_credentials: Mapping[str, Any] | None,
        mcp_whole_submission: bool, mcp_inherited: McpInheritance | None,
    ) -> dict[str, Any]:
        """The `code` profile's fields for a `browser_if_ui` step (#2322).

        Every check `_build_task` makes of a step's profile, made again for
        the other one: its declared inputs (so a browser-only input such as
        `browser_tool` is refused, not dropped), its MCP servers, its
        resource class, timeout and model class. The signature fields are
        filled by `specsigning.sign_task_specs`.
        """
        code = validate_runner_profile(runtime["code"])
        validate_runner_input(
            code, spec.input, step_id=step_id,
            repository_url=repository_url or spec.repository_url,
        )
        self.check_mcp_servers(
            code, spec.input, tenant.tenant_id,
            credentials=mcp_credentials if mcp_credentials is not None else spec.mcp_credentials,
            whole_submission=mcp_whole_submission, inherited=mcp_inherited, step_id=step_id,
        )
        resource_class = validate_resource_class_override(
            code, resource_class_override,
            sizes=(runnersizes.current_sizes(self._store.db)
                   if resource_class_override is not None else None),
        )
        class_models = self._settings.worker_class_models
        model_class = validate_model_class(code, spec.model_class, step_id=step_id)
        check_model_class_available(code, model_class, class_models, step_id=step_id)
        return {
            "runner_profile": code.name,
            "resource_class": resource_class,
            "timeout_seconds": validate_timeout(code, spec.timeout_seconds),
            "provider": code.provider,
            "model": resolve_task_model(code, model_class, spec.model, class_models,
                                        step_id=step_id),
            "model_class": stored_model_class(code, model_class),
            "spec_signature": None,
            "spec_key_version": None,
            "spec_format": None,
        }

    # -- registered MCP servers (docs/design/registered-mcp-servers.md) --------

    def check_mcp_servers(
        self, profile: Any, payload: Mapping[str, Any], tenant_id: str, *,
        credentials: Mapping[str, Any] | None,
        whole_submission: bool = True,
        inherited: McpInheritance | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """§4.3's refusals for one task or step, in the table's order; the
        auth mode of every server `payload` names, by name.

        `whole_submission` is False for a workflow's step: the map is the
        workflow's, so a key one step does not name is not unexpected there
        (`submit_workflow` checks the map once, over every step). Reads the
        tenant's registrations and nothing else: the value is not stored here.

        DARK UNTIL SWARM_MCP_SERVERS=on: off, a submission naming a server or
        carrying a credential is refused `mcp_unavailable` and one naming
        neither is exactly what it was. Without the child key no attempt can
        register the key an attach is signed with (§4.6), so a server is
        refused at submission rather than admitted to fail.
        """
        names = mcp_server_names(payload)
        if not names and not credentials:
            return {}
        require_mcp_enabled()
        keys = RowKeys.from_settings(self._settings)
        if not keys.configured:
            raise McpUnavailable(
                "this deployment has no swarm-child-key, so no attempt can register the key "
                "an MCP attach is signed with; nothing was stored")
        if inherited is not None:
            refuse_uninherited_mcp_servers(names, inherited.servers)
        db = getattr(self._store, "db", None)
        where = f"step {step_id!r}: " if step_id is not None else ""
        modes: dict[str, Any] = {}
        for name in names:
            registration = read_registration(db, tenant_id, name) if db is not None else None
            detail: dict[str, Any] = {"field": "input.mcp_servers", "server": name}
            if step_id is not None:
                detail["step_id"] = step_id
            if registration is None or registration.get("enabled") is not True:
                raise McpServerUnregistered(
                    f"{where}input.mcp_servers names a server that is not an enabled MCP "
                    "server registered in this workspace; nothing was stored", detail=detail)
            if not profile_allowed(registration, profile.name):
                raise McpServerProfileRefused(
                    f"{where}this MCP server's registration does not allow runner_profile "
                    f"{profile.name!r}; nothing was stored",
                    detail={**detail, "runner_profile": profile.name})
            modes[name] = (registration.get("auth") or {}).get("mode")
        if whole_submission:
            self.check_mcp_credential_map(credentials, modes)
        available = set(credentials or {})
        if inherited is not None:
            available |= self._inherited_mcp_credentials(tenant_id, names, inherited, keys)
        refuse_missing_mcp_credentials(names, modes, available, step_id=step_id)
        return modes

    def mcp_auth_modes(self, tenant_id: str) -> dict[str, Any]:
        """The tenant's enabled MCP registrations by name, each its auth mode:
        what `schedules.build_schedule` and `apply_edit` take as `mcp_modes`,
        so a schedule naming a `dispatch` server is refused when it is saved."""
        db = getattr(self._store, "db", None)
        if db is None:
            return {}
        modes: dict[str, Any] = {}
        for name in enabled_names(db, tenant_id):
            registration = read_registration(db, tenant_id, name)
            if registration is not None and registration.get("enabled") is True:
                modes[name] = (registration.get("auth") or {}).get("mode")
        return modes

    @staticmethod
    def check_mcp_credential_map(credentials: Mapping[str, Any] | None,
                                 modes: Mapping[str, Any]) -> None:
        """Every key a named `dispatch` server, every value the right shape.
        `check_credential`'s refusal names the rule, never the value."""
        refuse_unexpected_mcp_credentials(credentials, modes)
        for name in sorted(credentials or {}):
            entry = (credentials or {})[name]
            check_credential(getattr(entry, "value", None), getattr(entry, "expires_in", None))

    def _inherited_mcp_credentials(self, tenant_id: str, names: Sequence[str],
                                   inherited: McpInheritance, keys: RowKeys) -> set[str]:
        """The servers a live, attested credential row of the parent's
        submission serves: the attach reads the same rows, by the same rule."""
        db = getattr(self._store, "db", None)
        if db is None:
            return set()
        now = self._now()
        return {
            name for name in names
            if credential_for(db, tenant_id=tenant_id, server_id=mcp_server_id(tenant_id, name),
                              owner_ids=inherited.owners, now=now, keys=keys)[0] is not None
        }

    def _store_mcp_credentials(
        self, tenant_id: str, *, owner_id: str, owner_kind: str,
        credentials: Mapping[str, Any] | None, by: str,
    ) -> list[dict[str, Any]]:
        """Store each per-run credential, AFTER every check and the signature,
        immediately before the write that creates the documents: the value
        goes to one Secret Manager `add_version` under a TTL, the row holds
        its secret's name (`McpCredentials.store`). A row whose task is not
        written yet is not swept as done; a failed write leaves secrets the
        TTL removes. Answers `{"server", "expires_at"}` per credential."""
        if not credentials:
            return []
        db = getattr(self._store, "db", None)
        if self.mcp_credential_writer is None:
            self.mcp_credential_writer = SecretManagerMcpCredentials(self._settings.project_id)
        store = McpCredentials(db, self.mcp_credential_writer, now=self._now,
                               keys=RowKeys.from_settings(self._settings))
        stored: list[dict[str, Any]] = []
        for name in sorted(credentials):
            registration = read_registration(db, tenant_id, name)
            if registration is None or registration.get("enabled") is not True:
                # Disabled since the check above: refused as it would have been.
                raise McpServerUnregistered(
                    "a server mcp_credentials names stopped being an enabled MCP server "
                    "registered in this workspace during the submission; nothing was written",
                    detail={"field": "mcp_credentials", "server": name})
            entry = credentials[name]
            stored.append(store.store(
                tenant_id=tenant_id, owner_id=owner_id, owner_kind=owner_kind,
                registration=registration, value=entry.value, expires_in=entry.expires_in,
                by=by))
        return stored

    def _refuse_unavailable_browser_tool(
        self, profile: Any, payload: Mapping[str, Any], tenant_id: str,
        submitted_by: str | None, *, step_id: str | None = None,
    ) -> None:
        """`browser_tool: jev` with nothing to run it on: refused through its
        switch (`browser_tool_unavailable`, default off), never silently.

        Jev needs the image to carry it (`browser_jev_installed`) and, ONLY
        when the workspace's judgment backend is `typesafe` (JUDGE-SELECT,
        `judgment.JudgmentSettings`), a TypeSafe key for the submitter or the
        tenant -- the resolution `GET /v1/provider-keys/typesafe` serves
        (`task_key_scope`). The default backend, Internal Jev, and Internal
        Laya need no key, so they are never refused for its absence. Every
        other name runs on what every live-browser attempt has. Off, the
        task is admitted and the runner falls back to Playwright, saying why.
        """
        if payload.get(BROWSER_TOOL_INPUT_KEY) != "jev" or BROWSER_TOOL_INPUT_KEY not in profile.inputs:
            return
        if not getattr(self._settings, "browser_jev_installed", True):
            reason = "not_installed"
            message = ("browser_tool 'jev' cannot run: this deployment's browser image has no "
                       "jev-browser; pick auto, playwright or dev-browser")
        else:
            db = getattr(self._store, "db", None)
            if db is not None and JudgmentSettings(db, now=self._now, mac=settings_mac(self._settings)).read(
                    tenant_id)["judgment_backend"] != TYPESAFE_BACKEND:
                return
            scope = task_key_scope(db, self._store, tenant_id, submitted_by, TYPESAFE_PROVIDER,
                                   now=self._now) if db is not None else None
            if scope is not None:
                return
            reason = "no_key"
            message = ("browser_tool 'jev' needs a TypeSafe key because this workspace's "
                       "judgment backend is TypeSafe, and none is set for you or for this "
                       "tenant: add one on the console's key page, choose Internal Jev as "
                       "the backend, or pick auto, playwright or dev-browser")
        detail: dict[str, Any] = {"runner_profile": profile.name, "key": BROWSER_TOOL_INPUT_KEY,
                                  "browser_tool": "jev", "reason": reason}
        if step_id is not None:
            detail["step_id"] = step_id
            message = f"step {step_id!r}: {message}"
        refusals.refuse(BrowserToolUnavailable(message, detail=detail))

    def submit_tasks(
        self, ctx: AuthContext, specs: Sequence[TaskCreate], *, service_submission: bool = False
    ) -> SubmissionResult:
        """`service_submission` is set by the platform's own automation that
        submits as the person who registered a repository (`repoindex`), never
        by a route from a request: the owner's D4 for automation (2026-10-07)
        runs repository indexing with the tenant token."""
        validate_batch_size(len(specs), self._settings.core.max_batch_size)
        tenant = self.tenant_for(ctx)
        self.workspace_gate(ctx, tenant)
        now = self._now()
        budget_scope = self.budget_scope(tenant.tenant_id)
        try:
            # Inside the try so a refused dispatch is counted like every other
            # rejected submission rather than being invisible to the metric.
            tasks = [
                self._build_task(
                    spec=spec,
                    tenant=tenant,
                    ctx=ctx,
                    now=now,
                    budget_scope=budget_scope,
                    dispatch=self._with_publish_target(
                        resolve_dispatch_options(
                            strategy=spec.strategy,
                            carrier=spec.carrier,
                            # A batch is N INDEPENDENT tasks -- nothing in it
                            # depends on anything else in it -- so every task in a
                            # batch is at task scale, not workflow scale.
                            scale="task",
                            repository_url=spec.repository_url,
                        ),
                        tenant, spec, person=ctx.email,
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
            tenant, tasks,
            service=service_submission or is_service_submitter(ctx.email),
        )
        # #1434: the repositories each task reads beside its own, gated and
        # pinned, before the signature so it covers them.
        self._resolve_additional(
            tenant, [(task, spec.additional_repositories) for task, spec in zip(tasks, specs)],
            service=service_submission or is_service_submitter(ctx.email),
        )
        # Contract request 34: signed over the task as it will be stored.
        self._sign(tasks)
        # Each task's per-run MCP credentials, after every check and the
        # signature, before the write (§4.4): owned by the task.
        mcp_stored: dict[str, list[dict[str, Any]]] = {}
        for spec, task in zip(specs, tasks):
            stored = self._store_mcp_credentials(
                tenant.tenant_id, owner_id=task.id, owner_kind=OWNER_TASK,
                credentials=spec.mcp_credentials, by=ctx.email)
            if stored:
                mcp_stored[task.id] = stored
        self._store.create_tasks(tasks, tenant_member=ctx.tenant_member)
        for task in tasks:
            self._metrics.tasks_submitted.labels(
                tenant=task.tenant_id, runner_profile=task.runner_profile
            ).inc()
        woke = self._wake("task_submitted", tenant_id=tenant.tenant_id, count=str(len(tasks)))
        return SubmissionResult(tasks=tasks, woke_scheduler=woke, mcp_credentials=mcp_stored)

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
        # Before compiling any step: the workflow is admitted or refused whole.
        self.workspace_gate(ctx, tenant)
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
            # A runtime policy is one step's (#2322): copied onto every step,
            # it would put every step on the browser profile or refuse it.
            if RUNTIME_METADATA_KEY in spec.metadata:
                raise RuntimePolicyInvalid(
                    "metadata.runtime is a step's, not the workflow's: put it in the "
                    "metadata of each step that should run on the browser only when "
                    "its upstream change touches UI",
                    detail={"field": f"metadata.{RUNTIME_METADATA_KEY}"},
                )
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
            # `publish_to_pr` (#2183) beside either of them names two pull
            # requests: refused before either is read.
            publishtopr.refuse_another_pull_request(spec)
            named_pull = (self._named_pull(tenant, spec, person=ctx.email)
                          if spec.merge_pr else None)
            # A merge workflow its caller left nameless is stored as "Merge PR
            # <number>" (owner report 2026-10-11), before any step task copies
            # the workflow's metadata.
            spec = with_merge_pr_label(spec)
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
            # Lane SPEED-D3: the pull requests of the workflow's own
            # repository it waits for, once that repository is known.
            after_prs = resolve_after(
                [entry.pr for entry in spec.after] if spec.after else None, repository_url,
            )
            if continuation:
                dispatch = replace(dispatch, continues=continuation.root_task_id)
                # A merge step's conflict fix round (#1321), marked inside the
                # signed block so the worker may check, before it pushes, that
                # the round left the branch on the default branch's head.
                if isinstance(spec.metadata.get(CONFLICT_ROUND_METADATA_KEY), dict):
                    dispatch = replace(dispatch, conflict_round=True)
                # A CI fix's red head (#2276), inside the signed block so the
                # worker may end the fix as superseded at attempt start when the
                # pull request has moved past it. Only the two fields
                # `stamp_ci_fix` checked above: no claim, no post-back state.
                ci_fix = spec.metadata.get(CI_FIX_METADATA_FIELD)
                if isinstance(ci_fix, dict) and ci_fix.get("head_sha"):
                    dispatch = replace(dispatch, ci_fix_head={
                        "pull_request": ci_fix["pull_request"],
                        "head_sha": ci_fix["head_sha"],
                    })
            # The merge step (contract request 47): appended here, once the
            # strategy is known and before anything is built or signed, then
            # checked with the rest of the graph.
            merged_spec, rereview_step_id = self._with_merge_step(
                spec, step_specs, dispatch.strategy, repository_url,
                tenant_id=tenant.tenant_id,
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
                integrator_step_id = resolve_integrator_step(
                    step_specs, rereview=rereview_step_id
                )
            # A verdict gate and a `builds_on` (#264), once the integrator is
            # known: under `integrate` only the integrator may be gated, and the
            # re-review "on_merge_verdict" appended beside it (2026-10-10). Under
            # `single-pr` this is also where the chain's shape is refused or
            # planned (#295), and where a worker-action profile under any other
            # strategy is refused -- AFTER each step's profile is known to be
            # one the platform runs, so a disabled merge-chain profile is
            # refused as disabled, with the reason, before anything about where
            # it was placed (test_merge_chain_profiles_are_refused_on_submission).
            for step in spec.steps:
                validate_runner_profile(step.runner_profile)
            # An `await-input` step's block (docs/design/human-steps.md §4.1,
            # HW-API-SUBMIT), checked and normalised once each profile is
            # known to be one the platform runs, and BEFORE the routing
            # checks, so a `when`, `builds_on`, `pr_role` or `merges` on such
            # a step is refused naming itself for that reason. The profile
            # ships unavailable (contract request 94), so until request D the
            # loop above refuses every such step first.
            await_inputs: dict[str, dict[str, Any]] = {}
            control_plane_steps: set[str] = set()
            for step in spec.steps:
                step_profile = validate_runner_profile(step.runner_profile)
                if step_profile.control_plane:
                    control_plane_steps.add(step.step_id)
                    if step.step_id == integrator_step_id:
                        refuse_control_plane_integrator(step_profile, step_id=step.step_id)
                block = resolve_await_input(
                    step_profile,
                    step.await_input,
                    step_id=step.step_id,
                    container_fields={
                        name: getattr(step, name) for name in AWAIT_STEP_REFUSED_FIELDS
                    },
                    input_from=step.input_from,
                    submitted_by=ctx.email,
                    allowed_domains=self._settings.core.allowed_domains,
                    allowed_users=self._settings.allowed_users,
                )
                if block is not None:
                    await_inputs[step.step_id] = block
            # `publish_to_pr` (#2183): the shape first, then the pull request
            # read from GitHub once, after every other refusal that needs no
            # read -- the merge step appended above included -- so a refused
            # spec costs no forge call. Every step's block carries it.
            publish_target = self._publish_target(
                tenant, spec, repository_url, person=ctx.email,
                agent_steps=[
                    step.step_id for step in spec.steps
                    if step.step_id not in control_plane_steps
                    and validate_runner_profile(step.runner_profile).worker_action is None
                ],
            )
            if publish_target is not None:
                dispatch = replace(
                    dispatch, publish_to_pr=tuple(publish_target.to_metadata().items())
                )
            single_pr = validate_step_routing(
                step_specs,
                strategy=dispatch.strategy,
                integrator_step_id=integrator_step_id,
                rereview=rereview_step_id,
            )
            # A `direct-pr` step that only feeds a dependant (#2662): stored
            # with `allow_empty_diff`, so a read-only review that changes
            # nothing succeeds `no_change` instead of failing
            # `published_nothing:` and sweeping the step that reads it.
            readers = reader_step_ids(step_specs, strategy=dispatch.strategy)
            merge_plan = plan_merge(
                step_specs, dispatch.strategy,
                continuation.task_id if continuation else None,
                named_pull=named_pull is not None,
                rereview=rereview_step_id,
            )
            if merge_plan is not None:
                # At submission, never at merge time: a host no `ForgeMerger`
                # serves is refused before any step runs.
                refuse_unmergeable_forge(repository_url)
            # Every step's profile and input, before any task is built and
            # inside this try, so a refused step is counted like every other
            # refusal. The size first, as `_build_task` orders them, which
            # checks each step again; by then neither can fail.
            merge_metadata: Mapping[str, Any] = spec.metadata
            mcp_modes: dict[str, Any] = {}
            for step in spec.steps:
                profile = validate_runner_profile(step.runner_profile)
                validate_input_size(step.input, self._settings.core.max_input_bytes)
                reject_non_finite(step.input, step_id=step.step_id)
                refuse_unless_personal(tenant, step.input, step_id=step.step_id)
                validate_runner_input(
                    profile, step.input, step_id=step.step_id,
                    repository_url=spec.repository_url,
                )
                refuse_unless_granted(self._settings, self._store.db, tenant.tenant_id,
                                      step.input, step_id=step.step_id)
                # Its MCP servers, against the workflow's one map (§4.3).
                mcp_modes.update(self.check_mcp_servers(
                    profile, step.input, tenant.tenant_id, credentials=spec.mcp_credentials,
                    whole_submission=False, step_id=step.step_id,
                ))
                validate_storable(step.input, step_id=step.step_id)
                # The step's own metadata, merged as `_build_task` will store
                # it, under the same rules as any task's metadata.
                reject_reserved_metadata(step.metadata)
                # Its runtime policy (#2322), refused here so it is counted;
                # `_build_task` checks it again and builds the alternate.
                resolve_runtime_policy(step.metadata, runner_profile=step.runner_profile,
                                       step_id=step.step_id, workflow_step=True)
                resolve_review_gate(step.metadata, step_id=step.step_id, workflow_step=True)
                step_metadata = {**spec.metadata, **step.metadata}
                # Lane MS1: the CI-fix rounds a merge step may spend, as each
                # step's task will store them.
                resolve_merge_fix_rounds(step_metadata, merge_step=merge_plan is not None)
                if merge_plan is not None and step.step_id == merge_plan.merge_step:
                    merge_metadata = step_metadata
                validate_input_size(step_metadata, 16 * 1024, label="metadata")
                reject_non_finite(step.metadata, label="metadata", step_id=step.step_id)
                validate_storable(step_metadata, label="metadata", step_id=step.step_id)
            # Lane SPEED-D2: the fix rounds the merge step may spend, stamped
            # on the merge task below; None when there is no merge step.
            fix_rounds = resolve_fix_rounds(
                spec.fix_rounds, merge_metadata, merge_step=merge_plan is not None,
                named_pull=named_pull is not None,
            )
            # The workflow's map, once, over every step's servers: a key no
            # step names, or one for a server whose key is not the caller's,
            # is unexpected; every value is the right shape (§4.3).
            self.check_mcp_credential_map(spec.mcp_credentials, mcp_modes)
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
        # Nor is an `await-input` step (docs/design/human-steps.md §4.1): it
        # runs no container, so it pushes no branch to merge.
        not_integrated: frozenset[str] = frozenset(control_plane_steps)
        if integrator_step_id is not None and by_id[integrator_step_id].when is not None:
            not_integrated |= {by_id[integrator_step_id].when.step}
        # An issue run's requirements check (#2524) is read-only: when it
        # runs it writes requirements.json, and when the review runs it runs
        # no agent and pushes nothing at all.
        not_integrated |= requirements_check_steps(spec.steps)
        # Every review step names a gate by default (#2524, BGD 2026-10-28):
        # one that names none is stored with the platform's default block,
        # which swarm-api resolves against the workspace's setting.
        default_gated = default_review_gate_steps(spec.steps, spec.metadata)
        now = self._now()
        workflow_id = new_id("wf")

        tasks: list[Task] = []
        steps: list[WorkflowStep] = []
        # Topological order, so a child task document is never written before
        # the parent it names in `depends_on`.
        step_task_id: dict[str, str] = {}
        # Each step's stored model class, copied to its `WorkflowStep` (#1435).
        step_model_class: dict[str, str | None] = {}
        # Read once, at submission (#638): the gated step files its review's
        # minors on this issue, and a later change reaches later workflows.
        findings_epic = self._store.get_findings_epic(tenant.tenant_id)
        # Read once for every step (#2459): each step resolves its budget
        # against the same workspace default and ceiling.
        budget_scope = self.budget_scope(tenant.tenant_id)
        # Lane MS1, read once at submission like the epic: the default branch
        # the tenant registered this repository with, which the merge step
        # refuses any other base against (`base_not_default`, MS3).
        merge_base = (
            self._registered_base(tenant.tenant_id, repository_url)
            if merge_plan is not None else None
        )
        # TRAIN-WS, read once at submission like the base: the workspace's
        # merge mode for this repository, its registration's override first
        # (docs/merge-trains.md §5.1). Signed into the merge step's target;
        # a setting changed later reaches later workflows.
        merge_mode = (
            self._merge_mode(tenant.tenant_id, repository_url)
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
                merge_mode=merge_mode,
                named_pull=named_pull,
                continued_workflow=continuation.workflow_id if continuation else None,
            ).with_routing(
                # Both name upstream steps, so topological order has
                # already minted their task ids.
                builds_on=step_task_id[source.builds_on] if source.builds_on else None,
                gate_task_id=step_task_id[source.when.step] if source.when else None,
                gate_verdicts=source.when.verdict_in if source.when else (),
                allow_empty_diff=source.allow_empty_diff or step_id in readers,
                # Kept on a gated step only (`with_routing`): the MERGE path's
                # pull request title when the implementer wrote none.
                # Not on the re-review, which is gated on the same review
                # (2026-10-10) but opens no pull request.
                pr_label=None if step_id == rereview_step_id else workflow_label(spec.metadata),
                # Kept on a gated step only too: the step that reads the
                # verdict files its minors on the tenant's epic. The integrator
                # does; the re-review reads the same verdict and would file
                # every minor a second time.
                findings_epic=None if step_id == rereview_step_id else findings_epic,
            ).with_await_input(
                # Signed with the rest of the block (§4.1); None on every other
                # step, which then stores exactly the block it stored before.
                await_inputs.get(step_id)
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
                    metadata={
                        **spec.metadata, **source.metadata, "workflow_step": step_id,
                        # SPEED-D2: what the wake tick and the worker read as
                        # the rounds left (`resolve_fix_rounds`), on the merge
                        # task only.
                        **({MERGE_FIX_ROUNDS_KEY: fix_rounds}
                           if fix_rounds is not None and merge_plan is not None
                           and step_id == merge_plan.merge_step else {}),
                    },
                    timeout_seconds=source.timeout_seconds,
                    budget=source.budget,
                    model_class=source.model_class,
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
                # The workflow's map serves every step (§4.3); checked whole above.
                mcp_credentials=spec.mcp_credentials,
                mcp_whole_submission=False,
                budget_scope=budget_scope,
            )
            if after_prs and not source.depends_on:
                # SPEED-D3: a root step waits for the pull requests to merge,
                # PARKED and holding nothing (invariant 1); every other step
                # already waits on a root. The wake tick marks the gate and
                # the scheduler's dependency sweep promotes it (`prgate`).
                assert_transition(TaskState.QUEUED, TaskState.PARKED)
                task.state = TaskState.PARKED
                task.park_reason = ParkReason.DEPENDENCY_INCOMPLETE
                task.metadata[AFTER_PRS_METADATA_KEY] = gate_for(after_prs, repository_url or "")
            # The one place `metadata.input_from` is written. After `_build_task`,
            # which refused the key in the caller's metadata, so what lands here
            # is only ever this rewrite of a step declaration the DAG check has
            # already accepted (#151).
            if source.input_from:
                task.metadata[INPUT_FROM_METADATA_KEY] = {
                    step_task_id[src]: filename for src, filename in source.input_from.items()
                }
            # The re-review runs on what its review RESOLVED to (#2322): the
            # review's task id beside the policy it copied, read by the
            # scheduler as the re-review is readied (`scheduler.runtime`).
            # Only when the review itself asked for the policy: a review
            # pinned to claude-code resolved nothing, and its re-review's
            # block (#2275, `rereview_step_for`) decides from the lane's files.
            runtime_block = task.metadata.get(RUNTIME_METADATA_KEY)
            if (
                step_id == rereview_step_id and isinstance(runtime_block, dict) and source.when
                and RUNTIME_METADATA_KEY in (by_id[source.when.step].metadata or {})
            ):
                runtime_block["inherit"] = {
                    "step": source.when.step, "task_id": step_task_id[source.when.step],
                }
            if step_id in default_gated:
                task.metadata[REVIEW_GATE_METADATA_KEY] = dict(REVIEW_GATE_DEFAULT_BLOCK)
            step_task_id[step_id] = task.id
            step_model_class[step_id] = task.model_class
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
                    # Its task's stored class: None for the profile's default.
                    model_class=step_model_class[source.step_id],
                    # What the step ASKED for (contract request 120); its
                    # task's `budget` is what that resolved to.
                    budget=budget_mod.step_limits(source.budget).as_dict() or None,
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
        self._resolve_forge(tenant, tasks, service=is_service_submitter(ctx.email))
        # #1434: each step's additional repositories -- its own list, which
        # REPLACES the workflow's (`[]` is none), else the workflow's for a
        # step that runs an agent -- pinned once per submission, signed.
        self._resolve_additional(
            tenant,
            [(task, self._step_additional(by_id[task.step_id or ""], spec)) for task in tasks],
            service=is_service_submitter(ctx.email),
            also_named=[spec.additional_repositories],
        )
        self._sign(tasks)
        # The workflow's per-run MCP credentials, after every check and the
        # signature, before the write (§4.4): owned by the workflow, so every
        # step that names the server is attached the one stored credential.
        mcp_stored = self._store_mcp_credentials(
            tenant.tenant_id, owner_id=workflow_id, owner_kind=OWNER_WORKFLOW,
            credentials=spec.mcp_credentials, by=ctx.email)
        self._store.create_workflow(workflow, tasks, tenant_member=ctx.tenant_member)
        self._metrics.workflows_submitted.labels(tenant=tenant.tenant_id).inc()
        self._wake("workflow_submitted", tenant_id=tenant.tenant_id, workflow_id=workflow_id)
        return WorkflowSubmission(
            workflow=workflow,
            tasks=tasks,
            dispatch=dispatch,
            integrator_step_id=integrator_step_id,
            mcp_credentials=mcp_stored,
        )

    @staticmethod
    def _step_additional(step: WorkflowStepCreate, spec: WorkflowCreate) -> list[Any] | None:
        """The additional repositories a workflow step reads (owner's Q6): its
        own list when it names one, `[]` included, else the workflow's -- but
        only for a step that runs an agent. A worker action (merge,
        post-verdict, including one swarm-api appended) and an `await-input`
        step read no repository, so the workflow's default is not theirs; a
        list a step names itself is still checked, and refused there."""
        if step.additional_repositories is not None:
            return list(step.additional_repositories)
        if runs_an_agent(validate_runner_profile(step.runner_profile)):
            return list(spec.additional_repositories or []) or None
        return None

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
        tenant_id: str,
        continued_task: str | None = None,
    ) -> tuple[WorkflowCreate, str | None]:
        """`spec`, with a `merge` step appended when the merge choice says so,
        and the id of the re-review appended with it (None when there is none).

        Contract request 47, owner decisions 2026-10-04, and WF-MERGE-API
        (2026-10-10, part of #295). The choice is the workflow's
        `metadata.merge` ("on" | "on_merge_verdict" | "off"), else the
        `merge_policy` the tenant registered the repository with ("off" |
        "on_merge_verdict"), else the platform's `merge_by_default`
        (`Store.get_platform_settings`, default off), which means "on":

          * a spec that states its own merge step keeps it -- unless
            `metadata.merge` is "off", which contradicts it, or
            "on_merge_verdict", which derives the merge and its re-review
            itself; both are refused;
          * "on": when the workflow opens ONE pull request
            (`validation.merge_sources`), one merge step is appended,
            depending on the step that opens it and on the review if there is
            one, before anything is built or signed;
          * "on_merge_verdict": only for an `integrate` workflow whose
            integrator is gated on a review. A re-review of the integrator's
            pushed head is appended (`rereview_step_for`, one round,
            `REREVIEW_ROUNDS`), and the merge step depends on the integrator
            and the re-review and reads the re-review's verdict: the latest;
          * an explicit "on" or "on_merge_verdict" for a workflow it cannot
            apply to is refused, because the caller asked for a merge that
            would not happen; a repository's policy or the platform default
            simply does not apply to one;
          * "off" appends nothing, but a spec that states the re-review
            itself -- an issue run's compiled workflow (#2217), whose merge
            the CI loop submits later -- has its id returned when the step is
            exactly what `rereview_step_for` derives (`_stated_rereview`), so
            it is accepted as the appended one is;
          * neither default applies to a repository on a host no `ForgeMerger`
            serves either: the worker harvests a patch there and opens no pull
            request, so a tenant who never asked for a merge is not refused
            for an admin's setting. An explicit choice, or a stated merge
            step, on such a host is still refused at submission.

        `single-pr` ends in its own merge step and is left as submitted.
        """
        if strategy == SINGLE_PR:
            return spec, None
        choice = resolve_merge_choice(spec.metadata)
        stated = [s.step_id for s in spec.steps if is_merge_step(s.runner_profile)]
        if stated:
            if choice in ("off", MERGE_ON_VERDICT):
                raise DispatchOptionError(
                    f"metadata.{MERGE_METADATA_KEY} is {choice!r}, but step "
                    f"{stated[0]!r} is a merge step. "
                    + ("Remove one or the other." if choice == "off" else
                       "'on_merge_verdict' appends the merge step and its re-review "
                       "itself: remove the step, or set metadata."
                       f"{MERGE_METADATA_KEY} to 'on' to keep your own."),
                    detail={"merge_steps": stated, MERGE_METADATA_KEY: choice},
                )
            return spec, None
        if choice == "off":
            return spec, self._stated_rereview(spec, step_specs, strategy)
        explicit = choice is not None
        if not explicit:
            if not is_mergeable_forge(repository_url):
                return spec, None
            # A lane that publishes onto an existing pull request opens none
            # for a merge step to merge (#2183): a repository's policy or the
            # platform default does not apply to it, and an explicit merge
            # beside it is refused (`publishtopr.check_workflow_shape`).
            if spec.publish_to_pr is not None:
                return spec, None
            policy = self._registered_merge_policy(tenant_id, repository_url)
            if policy is not None:
                choice = policy
            elif self._store.get_platform_settings().get("merge_by_default"):
                choice = "on"
            else:
                return spec, None
            if choice == "off":
                return spec, None
        if choice == MERGE_ON_VERDICT:
            return self._with_verdict_merge(spec, step_specs, strategy, explicit=explicit)
        appended = merge_step_for(step_specs, strategy, continued_task)
        if appended is None:
            if explicit:
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
            return spec, None
        return spec.model_copy(
            update={"steps": [*spec.steps, WorkflowStepCreate.model_validate(appended)]}
        ), None

    @staticmethod
    def _stated_rereview(
        spec: WorkflowCreate, step_specs: Sequence[StepSpec], strategy: str,
    ) -> str | None:
        """The id of the re-review `spec` states itself, or None (#2217).

        An issue run compiles implement -> review -> fix -> re-review under
        `metadata.merge` "off" (`issueruns._with_rereview`), because its CI
        loop merges once CI is green rather than a merge step. Its re-review
        is gated on the review, and under `integrate` a gated step that is
        not the integrator is refused unless it is the re-review -- which
        swarm-api names only when it appended it. So a stated step is that
        re-review only when it is EXACTLY what `rereview_step_for` derives
        from the other steps: the review's profile, input and metadata behind
        `REREVIEW_PREAMBLE`, its id, the integrator's gate, building on the
        integrator, staging the review's verdict and the review's own input,
        and nothing depending on it. Anything looser is a caller's own gated
        non-integrator, and `validate_step_routing` still refuses it.
        """
        if strategy != "integrate":
            return None
        by_id = {s.step_id: s for s in spec.steps}
        for step in spec.steps:
            if any(step.step_id in s.depends_on or step.step_id in s.input_from
                   for s in spec.steps):
                continue
            rest = [s for s in step_specs if s.step_id != step.step_id]
            sources = merge_sources(rest, strategy)
            if sources is None or sources.review not in by_id:
                continue
            derived = rereview_step_for(rest, by_id[sources.review].model_dump(), strategy)
            if derived is not None and (
                WorkflowStepCreate.model_validate(derived).model_dump() == step.model_dump()
            ):
                return step.step_id
        return None

    def _with_verdict_merge(
        self,
        spec: WorkflowCreate,
        step_specs: Sequence[StepSpec],
        strategy: str,
        *,
        explicit: bool,
    ) -> tuple[WorkflowCreate, str | None]:
        """`spec` with a re-review and a merge step that reads its verdict.

        Refused when asked for explicitly and the workflow has no review a
        merge could wait on: `collect` and `single-pr` are not here, and
        `direct-pr` cannot carry a review (every agent step opens its own pull
        request, and a gate is refused there), so a merge under it would read
        no verdict. A repository's policy that cannot apply appends nothing.
        """
        by_id = {s.step_id: s for s in spec.steps}
        sources = merge_sources(step_specs, strategy)
        rereview = (
            rereview_step_for(step_specs, by_id[sources.review].model_dump(), strategy)
            if sources is not None and sources.review in by_id else None
        )
        if rereview is None:
            if not explicit:
                return spec, None
            raise DispatchOptionError(
                f"metadata.{MERGE_METADATA_KEY} is '{MERGE_ON_VERDICT}', which merges only "
                "on a review's MERGE verdict, and this workflow has no review to read one "
                "from: "
                + ("strategy 'collect' opens no pull request."
                   if strategy == "collect" else
                   "under 'direct-pr' every agent step opens its own pull request and no "
                   "step may be gated on a review."
                   if strategy == "direct-pr" else
                   "its integrator is not gated on a review "
                   '(`"when": {"step": <review>, "verdict_in": ["NOT_YET"]}` with the '
                   "review's verdict file in its input_from).")
                + " Use 'integrate' with implement -> review -> fix, set metadata."
                f"{MERGE_METADATA_KEY} to 'on' to merge on green CI alone, or 'off'.",
                detail={"strategy": strategy, MERGE_METADATA_KEY: MERGE_ON_VERDICT},
            )
        with_rereview = spec.model_copy(
            update={"steps": [*spec.steps, WorkflowStepCreate.model_validate(rereview)]}
        )
        appended = merge_step_for(
            self._step_specs(with_rereview), strategy, rereview=rereview["step_id"]
        )
        if appended is None:  # rereview_step_for found the same sources: unreachable
            raise DispatchOptionError(
                f"the re-review {rereview['step_id']!r} was appended but no merge step "
                "could be derived from it.",
                detail={"strategy": strategy, MERGE_METADATA_KEY: MERGE_ON_VERDICT},
            )
        return with_rereview.model_copy(
            update={"steps": [*with_rereview.steps, WorkflowStepCreate.model_validate(appended)]}
        ), rereview["step_id"]

    def _with_publish_target(
        self, dispatch: DispatchOptions, tenant: Tenant, spec: TaskCreate, *,
        person: str | None,
    ) -> DispatchOptions:
        """A single task's dispatch with its `publish_to_pr` (#2183), if any."""
        target = self._publish_target(tenant, spec, spec.repository_url, person=person)
        if target is None:
            return dispatch
        return replace(dispatch, publish_to_pr=tuple(target.to_metadata().items()))

    def _publish_target(
        self, tenant: Tenant, spec: WorkflowCreate | TaskCreate,
        repository_url: str | None, *, person: str | None,
        agent_steps: Sequence[str] | None = None,
    ) -> publishtopr.Target | None:
        """`spec.publish_to_pr`, checked and read from GitHub
        (`publishtopr`), or None when the spec names none. `agent_steps` is
        given for a workflow, None for a single task."""
        number = publishtopr.parse_number(spec.publish_to_pr, repository_url)
        if number is None:
            return None
        if agent_steps is None:
            publishtopr.check_task_shape(spec, number)
        else:
            publishtopr.check_workflow_shape(
                spec, number, repository_url=repository_url, agent_steps=agent_steps,
            )
        if self._forge_tokens is None or self._forge_writer is None:
            raise DispatchOptionError(
                "publish_to_pr reads the pull request from GitHub at submission, and this "
                "deployment has no forge reader configured.",
                detail={"publish_to_pr": number},
            )
        return publishtopr.resolve(
            number, store=self._store, tenant=tenant,
            repository_url=str(repository_url), tokens=self._forge_tokens,
            writer=self._forge_writer, person=person,
        )

    def _named_pull(self, tenant: Tenant, spec: WorkflowCreate, *,
                    person: str | None = None) -> NamedPull | None:
        """`spec.merge_pr`, checked (`continuation.resolve_merge_pr`), read
        with the slot `forge.resolve_forge_slot` picks for `person`."""
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
            person=person,
        )

    def _registration(self, tenant_id: str, repository_url: str | None) -> dict[str, Any]:
        """`tenant_id`'s registration of this repository, or {} when there is none.

        Read from the tenant's own registration only (`Repositories.find`
        checks the tenant), by the id `repositories.register` stored it
        under, so another tenant's registration of the same repository never
        decides anything about this workflow.
        """
        named = merge_repository(repository_url)
        if named is None:
            return {}
        record = Repositories(self._store.db, now=self._now).find(
            tenant_id, repo_id_for(tenant_id, *named)
        )
        return record or {}

    def _registered_merge_policy(
        self, tenant_id: str, repository_url: str | None
    ) -> str | None:
        """The `merge_policy` the tenant registered this repository with, or
        None when it is unregistered or set none (the platform default then)."""
        return merge_policy_of(self._registration(tenant_id, repository_url))

    def budget_scope(self, tenant_id: str) -> tuple[budget_mod.Limits, budget_mod.Limits]:
        """(the workspace's budget default, the platform ceiling), as they
        read now (#2459): what `_build_task` resolves every step against."""
        workspace = budget_mod.BudgetStore(
            self._store.db, now=self._now, mac=settings_mac(self._settings)).default(tenant_id)
        ceiling = budget_mod.ceiling_of(
            self._store.get_platform_settings().get(budget_mod.CEILING_SETTING))
        return workspace, ceiling

    def _merge_mode(self, tenant_id: str, repository_url: str | None) -> str:
        """The mode `tenant_id`'s merge step merges this repository in: the
        registration's override, else the workspace's (TRAIN-WS)."""
        return MergeSettingsStore(self._store.db, now=self._now,
                                  mac=settings_mac(self._settings)).mode_for(
            tenant_id, self._registration(tenant_id, repository_url))

    def _registered_base(self, tenant_id: str, repository_url: str | None) -> str | None:
        """The default branch `tenant_id` registered this repository with, or None.

        None when unregistered: the worker then asks GitHub for the default
        branch itself (MS3).
        """
        branch = self._registration(tenant_id, repository_url).get("default_branch")
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
        merge_mode: str | None = None,
        named_pull: NamedPull | None = None,
        continued_workflow: str | None = None,
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
                    merge_mode=merge_mode,
                )
            return dispatch.with_merge_target(
                pull_request=(
                    sources.pull_request if sources.continued
                    else step_task_id[sources.pull_request]
                ),
                review=step_task_id[sources.review] if sources.review else None,
                verdict_file=sources.verdict_file,
                base=merge_base,
                # A merge-only continuation's task is of an EARLIER workflow
                # (#900): name it, signed, so the worker verifies the task's
                # spec against the workflow it belongs to, not this one.
                pull_request_workflow=(
                    (continued_workflow or None) if sources.continued else None
                ),
                merge_mode=merge_mode,
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

    def stats(self, ctx: AuthContext, *, platform: bool = False) -> dict[str, Any]:
        """The caller's tenant counts, and the platform's when asked for.

        `platform` is `GET /v1/stats?scope=platform` (#2149): the cross-tenant
        counts run only when the caller asks AND is an admin, never on a plain
        read, because the shell's 30 s poll makes plain reads.
        """
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
        if platform and ctx.is_admin:
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

        # Units at the CURRENT runner size version, which is what admission
        # charges (design §4.4); a Cloud Run profile at the code default.
        sizes = runnersizes.current_sizes(self._store.db)
        profiles: dict[str, Any] = {}
        for name, profile in RUNNER_PROFILES.items():
            resolved = resolve_backend(profile)
            backend = resolved.value
            required = pool_names_for(
                tenant_id=tenant_id,
                provider=profile.provider,
                resource_class=profile.resource_class,
                runner_profile=name,
                backend=backend,
            )
            units = effective_size(profile.resource_class, sizes, resolved).units
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
