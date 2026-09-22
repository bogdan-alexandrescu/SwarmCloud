"""Firestore access for the API. Every read path is tenant-scoped.

The tenant boundary is enforced HERE and not in the routes, because a route is
easy to add and easy to forget. `get_task` takes a tenant_id and treats another
tenant's document as absent -- not as a 403, which would confirm the id exists.
Every list method starts from an equality filter on `tenant_id`.

Composite indexes this module relies on (owned by the terraform track). Every
list query below is `<equality filters> ORDER BY created_at DESC`, and Firestore
serves that only from an index whose ordered field follows the equality fields
immediately -- an index with any other field in between does NOT satisfy it:

    tasks-tenant-created            tenant_id ASC, created_at DESC
    tasks-tenant-state-created      tenant_id ASC, state ASC, created_at DESC
    tasks-tenant-workflow-created   tenant_id ASC, workflow_id ASC, created_at DESC
    tasks-tenant-runner-created     tenant_id ASC, runner_profile ASC, created_at DESC
    workflows-tenant-created        tenant_id ASC, created_at DESC

Only the first exists in terraform/modules/firestore/indexes.tf today. See the
handover note in this track's report: the other four are required before
`GET /v1/tasks?state=`, `GET /v1/tasks?runner_profile=`, `GET /v1/workflows/{id}`
and `GET /v1/workflows` will work against a real Firestore. `count_tasks_by_state`
uses equality filters with no ordering, which Firestore serves by merge join from
single-field indexes, so it needs nothing added.

Pagination uses an inequality on `created_at` rather than a Firestore cursor
token so a page token stays a plain, opaque timestamp the caller can hold across
processes. Two tasks created in the same microsecond would collapse a page
boundary; ids are generated with microsecond-resolution timestamps and random
suffixes, so that is a theoretical rather than an operational concern, and the
`id` tiebreak below makes it deterministic anyway.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import (
    Attempt,
    Lease,
    ProviderState,
    QuotaState,
    SlotPool,
    Task,
    TaskEvent,
    Tenant,
    Workflow,
    new_id,
    utcnow,
)
from swarm_common.states import EventType, TaskState, assert_transition

from .codec import (
    attempt_from_dict,
    event_from_dict,
    event_to_firestore,
    lease_from_dict,
    pool_from_dict,
    quota_from_dict,
    task_from_dict,
    task_to_firestore,
    tenant_from_dict,
    tenant_to_firestore,
    workflow_from_dict,
    workflow_to_firestore,
)
from .errors import Conflict, NotFound, ValidationFailed

log = logging.getLogger(__name__)

TASKS = "tasks"
WORKFLOWS = "workflows"
TENANTS = "tenants"
POOLS = "pools"
QUOTA = "quota"
LEASES = "leases"
ATTEMPTS = "attempts"
CONTROL = "control"
EVENTS = "events"

CONTROL_DOC = "dispatch"

#: Firestore caps a write batch at 500 operations. Each task costs two (the
#: document and its `submitted` event), so a chunk of 200 tasks is the ceiling.
_BATCH_CHUNK = 200

#: Step-task point reads one list request may spend deriving workflow states.
#: A page of `max_page_size` workflows at `max_workflow_steps` each would be
#: 10,000 documents, which is not a cost a list route may incur on a caller's
#: behalf. Past this the remaining steps come back UNREAD and the workflows that
#: needed them derive as UNKNOWN -- slower to answer, never wrong.
_STEP_READ_BUDGET = 500

#: `evaluate_capacity` treats a MISSING pool as unlimited, but a Firestore
#: document has no "absent integer" -- so a pool created only to carry an
#: enabled/disabled flag (a drain, say) needs a hard limit that will never bind.
#: Writing 0 there instead would turn "drain this resource class" into "cap it at
#: zero forever", and undraining would silently leave it blocked.
UNLIMITED_HARD_LIMIT = 1_000_000


#: Google caps a service account id at 30 characters. Provisioning truncates to
#: fit (scripts/register-tenant.sh does `${GSA_ID:0:30}`), and terraform simply
#: refuses a tenant id that would not fit. Either way a name the API derived by
#: concatenation would silently point at a DIFFERENT tenant's identity once two
#: ids share a prefix, so the API refuses to derive one at all past the limit.
MAX_SERVICE_ACCOUNT_ID = 30


def derived_service_account(
    tenant_id: str, *, project_id: str, prefix: str = "swarm-agent-worker"
) -> str | None:
    """The tenant's worker service account email, or None if it cannot be derived.

    None means "this tenant has no identity the API can name". Downstream that is
    a loud failure -- `put_credential` refuses, and the dispatcher refuses to
    create a job with no service account -- which is the correct outcome for a
    tenant whose infrastructure was never provisioned. Guessing a truncated name
    instead would hand the tenant whichever identity the truncation collided
    with.
    """
    account_id = f"{prefix}-{tenant_id}"
    if len(account_id) > MAX_SERVICE_ACCOUNT_ID:
        log.warning(
            "tenant %r cannot have a derived service account: %r is %d characters, "
            "over the %d character limit; provisioning must assign one explicitly",
            tenant_id,
            account_id,
            len(account_id),
            MAX_SERVICE_ACCOUNT_ID,
        )
        return None
    return f"{account_id}@{project_id}.iam.gserviceaccount.com"


def encode_cursor(moment: datetime) -> str:
    return base64.urlsafe_b64encode(moment.isoformat().encode("utf-8")).decode("ascii")


def decode_cursor(token: str | None) -> datetime | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parsed = datetime.fromisoformat(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        # A malformed token is a bad request, not a missing resource: 404 here
        # would tell a caller their own tasks had disappeared.
        raise ValidationFailed("page_token is not a valid cursor") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Page:
    items: list[Any]
    next_page_token: str | None


@dataclass
class StepStateRead:
    """The outcome of reading a set of workflow steps' task states.

    Three fields because there are three outcomes and collapsing them is how a
    failed read becomes a cheerful answer:

      * `states`  -- task_id -> state, for the tasks that were read.
      * `absent`  -- read, and the document was not there or belonged to another
                     tenant. A data fault.
      * `unread`  -- never read: the budget ran out first. A capacity decision.

    Both `absent` and `unread` make a workflow's rollup incomplete, and the
    rollup reports which kind it hit.
    """

    states: dict[str, TaskState]
    absent: list[str]
    unread: list[str]
    reads: int


class Store:
    def __init__(self, db: Any, *, now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._now = now

    @property
    def db(self) -> Any:
        return self._db

    # -- helpers ----------------------------------------------------------

    def _count(self, query: Any) -> int:
        result = query.count().get()
        for row in result:
            for item in row:
                return int(item.value)
        return 0

    def _chunks(self, items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
        for start in range(0, len(items), size):
            yield items[start : start + size]

    # -- tenants ----------------------------------------------------------

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        if not snap.exists:
            return None
        return tenant_from_dict(snap.to_dict())

    def list_tenants(self, limit: int = 200) -> list[Tenant]:
        query = self._db.collection(TENANTS).order_by("tenant_id").limit(limit)
        return [tenant_from_dict(snap.to_dict()) for snap in query.stream()]

    def ensure_tenant(
        self,
        tenant_id: str,
        *,
        principal: str,
        default_max_active: int,
        default_capacity_units: int,
        project_id: str,
        artifact_bucket: str,
        service_account_prefix: str = "swarm-agent-worker",
        namespace_prefix: str = "swarm-tenant-",
    ) -> Tenant:
        """Read the tenant, creating the control-plane record on first sight.

        What this DOES create: the Firestore tenant document and the
        `tenant:<id>` slot pool (a tenant with no pool document is unlimited by
        construction in `evaluate_capacity`, so it is created with the tenant
        rather than lazily on first admission).

        What this does NOT create, and must not be read as creating: the Google
        service account, the Secret Manager secrets, the bucket IAM condition,
        the Kubernetes namespace, the NetworkPolicy or the ResourceQuota. Every
        one of those is provisioned out of band -- terraform's `tenants` map or
        scripts/register-tenant.sh -- and both of those paths also seed this
        document, with the same field values. The strings written below are
        REFERENCES to that infrastructure, derived by the same naming rule, so a
        self-service tenant is recognisable (its work fails to dispatch with a
        missing-identity error) rather than silently running as something else.

        `principal` is the TENANT's principal -- the group email for a group
        tenant -- not the caller's address. It is checked against the existing
        document on every request, because the frozen `tenant_id_for_group`
        slugs the local part only: `eng@saga.xyz`, `eng@partner.com`,
        `eng.team@saga.xyz` and `Eng-Team@saga.xyz` all derive tenant `eng`.
        Without this check the second group would inherit the first group's
        service account, secrets, GCS prefix and namespace with no error
        anywhere.
        """
        existing = self.get_tenant(tenant_id)
        if existing is not None:
            self._assert_principal_matches(existing, principal)
            return existing
        kind = "user" if tenant_id.startswith("u-") else "group"
        tenant = Tenant(
            tenant_id=tenant_id,
            kind=kind,
            principal=principal.strip().lower(),
            created_at=self._now(),
            display_name=tenant_id,
            max_active=default_max_active,
            capacity_units=default_capacity_units,
            service_account=derived_service_account(
                tenant_id, project_id=project_id, prefix=service_account_prefix
            ),
            gcs_prefix=f"gs://{artifact_bucket}/tenants/{tenant_id}",
            namespace=f"{namespace_prefix}{tenant_id}",
        )
        self._db.collection(TENANTS).document(tenant_id).set(tenant_to_firestore(tenant))
        self.upsert_pool(
            f"tenant:{tenant_id}",
            hard_limit=min(tenant.max_active, tenant.capacity_units),
        )
        return tenant

    @staticmethod
    def _assert_principal_matches(existing: Tenant, principal: str) -> None:
        """Refuse a caller whose tenant id collides with a different principal.

        A blank stored principal is treated as a match: a document seeded before
        this check existed should not lock its own tenant out. Anything else that
        differs is a genuine collision and is refused rather than served, because
        serving it hands one group another group's credentials.
        """
        stored = (existing.principal or "").strip().lower()
        incoming = (principal or "").strip().lower()
        if not stored or stored == incoming:
            return
        raise Conflict(
            f"tenant id {existing.tenant_id!r} already belongs to a different "
            "principal; two distinct groups or users cannot share one tenant",
            detail={
                "tenant_id": existing.tenant_id,
                "registered_principal": stored,
                "requested_principal": incoming,
            },
        )

    def assert_tenant_scope(self, tenant_id: str, principal: str) -> None:
        """The READ-side half of the collision check `ensure_tenant` runs.

        A tenant id is not a unique key for a principal. The frozen
        `tenant_id_for_group` slugs the local part only, so `eng@saga.xyz` and
        `eng@partner.com` both derive `eng`; and the group path adds no prefix
        while the personal path adds `u-`, so a registered group named
        `u-eng@saga.xyz` derives the same id as the personal tenant of
        `eng@saga.xyz`. Two verified, unrelated identities therefore arrive
        holding the same `tenant_id` string, and every query below filters on
        exactly that string.

        Until this existed the collision was refused on the SUBMIT paths only,
        which is the wrong way round: submitting was 409 while listing, reading
        and CANCELLING the other principal's tasks all succeeded. Reproduced
        through the real routes on 2026-09-21 before the fix, in-process against
        the test Firestore -- not against a deployment.

        A missing document means nothing has ever been filed under the id.
        Both paths that create work -- `submit_tasks` and `submit_workflow` --
        go through `SubmissionService.tenant_for`, which calls `ensure_tenant`
        before the first write, so a task under a tenant with no document is a
        state this service cannot produce. Nothing to reach across, nothing to
        refuse; refusing anyway would 409 every brand-new tenant's first list.

        COLLISION ONLY, and deliberately not `enabled`: disabling a tenant stops
        it starting work, and a stopped tenant still has to see and cancel what
        it already has running. `SubmissionService.tenant_for` is what refuses a
        disabled tenant, on the paths that create work.
        """
        existing = self.get_tenant(tenant_id)
        if existing is None:
            return
        self._assert_principal_matches(existing, principal)

    def set_tenant_limits(
        self,
        tenant_id: str,
        *,
        max_active: int | None = None,
        capacity_units: int | None = None,
        enabled: bool | None = None,
    ) -> Tenant:
        """Change a tenant's limits, and move the pool that enforces them with it.

        Both `max_active` and `capacity_units` are ceilings on the SAME thing.
        `acquire_lease_in_transaction` increments every pool by the task's
        weighted `units`, so `tenant:<id>.active` is a count of units, and a task
        always costs at least one unit. The pool's hard limit is therefore the
        smaller of the two numbers: that bound is correct read either way, and it
        can never raise a ceiling an operator set.

        Before this, `capacity_units` was written to the document and consulted
        by nothing -- the same shape as a limit that is a lie.
        """
        ref = self._db.collection(TENANTS).document(tenant_id)
        snap = ref.get()
        if not snap.exists:
            raise NotFound(f"tenant {tenant_id!r} does not exist")
        current = tenant_from_dict(snap.to_dict())
        patch: dict[str, Any] = {}
        if max_active is not None:
            patch["max_active"] = int(max_active)
        if capacity_units is not None:
            patch["capacity_units"] = int(capacity_units)
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if patch:
            ref.update(patch)
        if max_active is not None or capacity_units is not None:
            effective = min(
                int(max_active if max_active is not None else current.max_active),
                int(
                    capacity_units
                    if capacity_units is not None
                    else current.capacity_units
                ),
            )
            self.upsert_pool(f"tenant:{tenant_id}", hard_limit=effective)
        return tenant_from_dict(ref.get().to_dict())

    def register_credential(self, tenant_id: str, provider: str) -> Tenant:
        """Record that the tenant has a key for `provider`. Never the key itself."""
        ref = self._db.collection(TENANTS).document(tenant_id)
        snap = ref.get()
        if not snap.exists:
            raise NotFound(f"tenant {tenant_id!r} does not exist")
        tenant = tenant_from_dict(snap.to_dict())
        providers = sorted(set(tenant.credentials) | {provider})
        ref.update({"credentials": providers})
        tenant.credentials = providers
        return tenant

    # -- tasks ------------------------------------------------------------

    def create_tasks(self, tasks: Sequence[Task]) -> list[Task]:
        """Write tasks and their `submitted` events.

        Batched so a partially written submission cannot leave a task document
        with no event trail. Ordering within the batch matters for workflows:
        callers pass tasks in topological order so a child is never written
        before its parent.
        """
        now = self._now()
        for chunk in self._chunks(list(tasks), _BATCH_CHUNK):
            batch = self._db.batch()
            for task in chunk:
                ref = self._db.collection(TASKS).document(task.id)
                batch.set(ref, task_to_firestore(task))
                event = TaskEvent(
                    event_id=new_id("ev"),
                    task_id=task.id,
                    tenant_id=task.tenant_id,
                    type=EventType.SUBMITTED,
                    at=now,
                    detail={
                        "runner_profile": task.runner_profile,
                        "resource_class": task.resource_class,
                        "state": task.state.value,
                        "workflow_id": task.workflow_id,
                    },
                )
                batch.set(ref.collection(EVENTS).document(event.event_id),
                          event_to_firestore(event))
            batch.commit()
        return list(tasks)

    def get_task(self, tenant_id: str, task_id: str) -> Task:
        snap = self._db.collection(TASKS).document(task_id).get()
        if not snap.exists:
            raise NotFound(f"task {task_id!r} not found")
        data = snap.to_dict()
        if data.get("tenant_id") != tenant_id:
            # Deliberately the SAME error as a genuinely missing task: a
            # different status here would confirm the id exists in another
            # tenant, which is an enumeration oracle.
            raise NotFound(f"task {task_id!r} not found")
        return task_from_dict(data)

    def list_tasks(
        self,
        tenant_id: str,
        *,
        state: TaskState | None = None,
        workflow_id: str | None = None,
        runner_profile: str | None = None,
        limit: int = 50,
        page_token: str | None = None,
    ) -> Page:
        query = self._db.collection(TASKS).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        )
        if state is not None:
            query = query.where(filter=FieldFilter("state", "==", state.value))
        if workflow_id is not None:
            query = query.where(filter=FieldFilter("workflow_id", "==", workflow_id))
        if runner_profile is not None:
            query = query.where(filter=FieldFilter("runner_profile", "==", runner_profile))
        before = decode_cursor(page_token)
        if before is not None:
            query = query.where(filter=FieldFilter("created_at", "<", before))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit + 1)

        rows = [task_from_dict(snap.to_dict()) for snap in query.stream()]
        rows.sort(key=lambda t: (t.created_at, t.id), reverse=True)
        next_token = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_token = encode_cursor(rows[-1].created_at)
        return Page(items=rows, next_page_token=next_token)

    def request_cancel(self, tenant_id: str, task_id: str, *, by: str) -> Task:
        """Flag the task for cancellation, terminating it immediately if idle.

        A task that holds no capacity (SUBMITTED / QUEUED / READY / PARKED) goes
        straight to CANCELLED. A task that does hold capacity keeps it until the
        worker or the reconciler releases the lease, because releasing it from
        here would decrement a pool that the running container still occupies.
        """
        ref = self._db.collection(TASKS).document(task_id)
        snap = ref.get()
        if not snap.exists or snap.to_dict().get("tenant_id") != tenant_id:
            raise NotFound(f"task {task_id!r} not found")
        task = task_from_dict(snap.to_dict())
        now = self._now()
        if task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
                          TaskState.DEAD_LETTERED}:
            raise Conflict(
                f"task {task_id!r} is already terminal ({task.state.value})",
                detail={"state": task.state.value},
            )

        patch: dict[str, Any] = {"cancel_requested": True, "updated_at": now}
        if task.state in {TaskState.SUBMITTED, TaskState.QUEUED, TaskState.READY,
                          TaskState.PARKED}:
            assert_transition(task.state, TaskState.CANCELLED)
            patch["state"] = TaskState.CANCELLED.value
            patch["completed_at"] = now
            patch["park_reason"] = None
            patch["blocked_by"] = []
        ref.update(patch)
        immediate = "state" in patch
        self.append_event(
            task_id=task_id,
            tenant_id=tenant_id,
            type=EventType.CANCELLED,
            detail={
                "requested_by": by,
                "from_state": task.state.value,
                # "cancel_requested" means the flag is set but the task still
                # holds capacity: the worker or reconciler releases the lease.
                "phase": "cancelled" if immediate else "cancel_requested",
            },
        )
        return task_from_dict(ref.get().to_dict())

    def append_event(
        self,
        *,
        task_id: str,
        tenant_id: str,
        type: EventType,
        detail: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        lease_id: str | None = None,
        generation: int | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=new_id("ev"),
            task_id=task_id,
            tenant_id=tenant_id,
            type=type,
            at=self._now(),
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            detail=dict(detail or {}),
        )
        (
            self._db.collection(TASKS)
            .document(task_id)
            .collection(EVENTS)
            .document(event.event_id)
            .set(event_to_firestore(event))
        )
        return event

    def list_events(self, tenant_id: str, task_id: str, *, limit: int = 200) -> list[TaskEvent]:
        self.get_task(tenant_id, task_id)         # tenant check before any read
        query = (
            self._db.collection(TASKS)
            .document(task_id)
            .collection(EVENTS)
            .order_by("at", direction=firestore.Query.ASCENDING)
            .limit(limit)
        )
        return [event_from_dict(snap.to_dict()) for snap in query.stream()]

    def list_artifacts(self, tenant_id: str, task_id: str, *, limit: int = 200) -> dict:
        """Artifact METADATA only, read from where the worker actually writes it.

        Artifacts live in the tenant's own GCS prefix and are passed by
        reference; nothing about them is inlined through Firestore, and this
        endpoint never mints a download URL -- the caller reads GCS with their
        own credentials, which keeps the tenant boundary in one place.

        THE SOURCE IS `task.result_summary`, not a subcollection. This read used
        to stream `tasks/<id>/artifacts`, and NOTHING in this repository has ever
        written that subcollection: `ARTIFACTS` was referenced in exactly one
        place, here. `AgentLifecycle._finalize` builds the manifest
        -- `[{"name", "bytes", "uri"}, ...]` -- and `control.finish()` stores it
        as `task.result_summary["artifacts"]`; `agent_worker.inputs` calls that
        field "the manifest" and stages a downstream step's inputs from it. So
        the route answered `[]` for every task that ever ran, with a 200, and
        apps/swarm-ui/src/api.ts had already written the workaround into a
        comment rather than the endpoint being fixed.

        Pointing the reader at the existing writer is the fix. Inventing a second
        writer would put the same manifest in two places and let them disagree.

        `artifacts_skipped` comes back too. The worker drops files once the
        attempt passes `max_artifact_bytes`, and an artifact list that silently
        omits them is the same class of lie in miniature: the caller sees a short
        list and no reason for it.

        A task that has not reached a terminal state has no `result_summary`
        yet, so `artifacts` is empty and `complete` is false -- which is a
        different statement from "this task produced nothing".
        """
        task = self.get_task(tenant_id, task_id)
        summary = task.result_summary or {}
        entries = summary.get("artifacts")
        if not isinstance(entries, list):
            entries = []
        skipped = summary.get("artifacts_skipped")
        if not isinstance(skipped, list):
            skipped = []
        return {
            "artifacts": [dict(e) for e in entries[:limit] if isinstance(e, dict)],
            "artifacts_skipped": [str(name) for name in skipped],
            "artifact_bytes": summary.get("artifact_bytes"),
            "complete": bool(task.result_summary),
        }

    def count_tasks_by_state(self, tenant_id: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for state in TaskState:
            query = self._db.collection(TASKS).where(
                filter=FieldFilter("state", "==", state.value)
            )
            if tenant_id is not None:
                query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            counts[state.value] = self._count(query)
        return counts

    # -- workflows --------------------------------------------------------

    def create_workflow(self, workflow: Workflow, tasks: Sequence[Task]) -> Workflow:
        self.create_tasks(tasks)
        (
            self._db.collection(WORKFLOWS)
            .document(workflow.workflow_id)
            .set(workflow_to_firestore(workflow))
        )
        return workflow

    def get_workflow(self, tenant_id: str, workflow_id: str) -> Workflow:
        snap = self._db.collection(WORKFLOWS).document(workflow_id).get()
        if not snap.exists:
            raise NotFound(f"workflow {workflow_id!r} not found")
        data = snap.to_dict()
        if data.get("tenant_id") != tenant_id:
            raise NotFound(f"workflow {workflow_id!r} not found")
        return workflow_from_dict(data)

    def list_workflows(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        page_token: str | None = None,
    ) -> Page:
        query = self._db.collection(WORKFLOWS).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        )
        before = decode_cursor(page_token)
        if before is not None:
            query = query.where(filter=FieldFilter("created_at", "<", before))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit + 1)
        rows = [workflow_from_dict(snap.to_dict()) for snap in query.stream()]
        rows.sort(key=lambda w: (w.created_at, w.workflow_id), reverse=True)
        next_token = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_token = encode_cursor(rows[-1].created_at)
        return Page(items=rows, next_page_token=next_token)

    def workflow_step_states(
        self,
        tenant_id: str,
        workflows: Sequence[Workflow],
        *,
        budget: int | None = None,
    ) -> StepStateRead:
        """Task states for every step of these workflows, by point read.

        POINT READS, NOT A QUERY. Each `WorkflowStep` already carries its
        `task_id`, so the ids are in hand and a query would only re-derive them
        -- and a query per workflow costs the same documents plus a round trip
        each. This is the pattern `SchedulerStore.task_states` already uses for
        `depends_on`, for the same reason: a bounded number of point reads is not
        a scan.

        TENANT-CHECKED, like `get_task`: a step naming a task that belongs to
        someone else reads as ABSENT rather than being returned. Invariant 9 does
        not get an exception for a derived field.

        BOUNDED, because a page of 200 workflows at 50 steps each is 10,000
        documents and a list route must not be able to cost that. When the budget
        runs out the remaining ids come back in `unread`, which makes every
        workflow that needed one derive as UNKNOWN. That is the correct
        degradation: the answer gets slower to appear, never wrong.
        """
        limit = _STEP_READ_BUDGET if budget is None else max(0, budget)
        wanted: list[str] = []
        for workflow in workflows:
            for step in workflow.steps:
                if step.task_id:
                    wanted.append(step.task_id)
        wanted = list(dict.fromkeys(wanted))

        states: dict[str, TaskState] = {}
        absent: list[str] = []
        reads = 0
        for task_id in wanted[:limit]:
            snap = self._db.collection(TASKS).document(task_id).get()
            reads += 1
            if not snap.exists:
                absent.append(task_id)
                continue
            data = snap.to_dict()
            if data.get("tenant_id") != tenant_id:
                absent.append(task_id)
                continue
            states[task_id] = TaskState(data["state"])
        return StepStateRead(states=states, absent=absent, unread=wanted[limit:], reads=reads)

    def set_workflow_state(self, workflow_id: str, state: TaskState) -> None:
        """Write the derived state onto the workflow document.

        The ONLY writer of `workflow.state` after `create_workflow`. It takes a
        `TaskState`, not a string, so nothing can put `"UNKNOWN"` in a field that
        `workflow_from_dict` decodes with `TaskState(...)` and would then raise on
        for every subsequent read.

        No `assert_transition`: the frozen state machine governs a TASK's
        lifecycle, and a workflow's rollup legitimately moves in ways a task
        never does -- QUEUED straight to RUNNING when the first step is admitted,
        or RUNNING back to PARKED when the last live step parks on quota. Running
        the task machine over it would reject the truth.
        """
        (
            self._db.collection(WORKFLOWS)
            .document(workflow_id)
            .update({"state": state.value, "updated_at": self._now()})
        )

    def cancel_workflow(self, tenant_id: str, workflow_id: str, *, by: str) -> dict[str, Any]:
        workflow = self.get_workflow(tenant_id, workflow_id)
        (
            self._db.collection(WORKFLOWS)
            .document(workflow_id)
            .update({"cancel_requested": True, "updated_at": self._now()})
        )
        cancelled, already_terminal = [], []
        for step in workflow.steps:
            if not step.task_id:
                continue
            try:
                self.request_cancel(tenant_id, step.task_id, by=by)
                cancelled.append(step.task_id)
            except Conflict:
                already_terminal.append(step.task_id)
            except NotFound:
                already_terminal.append(step.task_id)
        return {
            "workflow_id": workflow_id,
            "cancel_requested": True,
            "tasks_cancelled": cancelled,
            "tasks_already_terminal": already_terminal,
        }

    # -- pools, quota, control -------------------------------------------

    def list_pools(self, limit: int = 500) -> list[SlotPool]:
        query = self._db.collection(POOLS).limit(limit)
        return [pool_from_dict(snap.id, snap.to_dict()) for snap in query.stream()]

    def get_pool(self, name: str) -> SlotPool | None:
        snap = self._db.collection(POOLS).document(name).get()
        if not snap.exists:
            return None
        return pool_from_dict(name, snap.to_dict())

    def upsert_pool(
        self,
        name: str,
        *,
        hard_limit: int | None = None,
        enabled: bool | None = None,
        adaptive_target: int | None = None,
        quota_derived_limit: int | None = None,
    ) -> SlotPool:
        ref = self._db.collection(POOLS).document(name)
        snap = ref.get()
        now = self._now()
        if not snap.exists:
            pool = SlotPool(
                name=name,
                hard_limit=int(
                    hard_limit if hard_limit is not None else UNLIMITED_HARD_LIMIT
                ),
                adaptive_target=adaptive_target,
                quota_derived_limit=quota_derived_limit,
                active=0,
                enabled=True if enabled is None else bool(enabled),
                updated_at=now,
            )
            ref.set(
                {
                    "name": pool.name,
                    "hard_limit": pool.hard_limit,
                    "adaptive_target": pool.adaptive_target,
                    "quota_derived_limit": pool.quota_derived_limit,
                    "active": 0,
                    "enabled": pool.enabled,
                    "updated_at": now,
                }
            )
            return pool
        patch: dict[str, Any] = {"updated_at": now}
        if hard_limit is not None:
            patch["hard_limit"] = int(hard_limit)
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if adaptive_target is not None:
            patch["adaptive_target"] = int(adaptive_target)
        if quota_derived_limit is not None:
            patch["quota_derived_limit"] = int(quota_derived_limit)
        ref.update(patch)
        return pool_from_dict(name, ref.get().to_dict())

    def list_quota(self, tenant_id: str | None = None, limit: int = 500) -> list[QuotaState]:
        query: Any = self._db.collection(QUOTA)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        query = query.limit(limit)
        return [quota_from_dict(snap.to_dict()) for snap in query.stream()]

    def list_leases(
        self,
        tenant_id: str | None = None,
        *,
        active_only: bool = True,
        limit: int = 200,
    ) -> list[Lease]:
        """Leases, newest first.

        THIS IS THE READ PATH THAT DID NOT EXIST. The documents were always
        written (`agent_worker/control.py`), the decoder was always here
        (`codec.lease_from_dict`) and the index was always declared
        (`leases-tenant-created`, tenant_id ASC + created_at DESC). Only this
        method and its route were missing, and their absence is what made
        "which agents are holding capacity right now" unanswerable -- the
        single highest-value gap in the operator UI.

        `active_only` filters to leases that have not been released. It is a
        CLIENT-SIDE filter on purpose: `released_at == None` plus an ordered
        `created_at` would need a third composite index for a predicate that
        is true of almost every row in the window anyway. If that stops being
        true, add the index rather than paging blindly.

        `tenant_id` None means every tenant, which is why the route is
        admin-gated. Passing a tenant id uses the declared composite index;
        passing None orders on created_at alone, which a single-field index
        already covers.
        """
        query: Any = self._db.collection(LEASES)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit)
        leases = [lease_from_dict(snap.to_dict()) for snap in query.stream()]
        if active_only:
            leases = [lease for lease in leases if not lease.is_released]
        return leases

    def list_attempts(
        self,
        tenant_id: str,
        task_id: str | None = None,
        *,
        limit: int = 200,
    ) -> list[Attempt]:
        """Attempts, newest first, for one tenant or one task.

        Same story as leases: written, decodable and indexed
        (`attempts-task-created`, `attempts-tenant-created`), never readable.

        This is what makes a retried task legible. `result_summary` is written
        once, by `finish()`, so a task that failed twice and succeeded on the
        third attempt carries only attempt three's numbers. The earlier two
        exist only here -- with their exit codes, their errors and their peak
        RSS -- and until now nothing could read them.
        """
        query: Any = self._db.collection(ATTEMPTS)
        query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        if task_id is not None:
            query = query.where(filter=FieldFilter("task_id", "==", task_id))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit)
        return [attempt_from_dict(snap.to_dict()) for snap in query.stream()]

    def set_provider_enabled(self, provider: str, enabled: bool) -> None:
        """Enable/disable a provider platform-wide.

        Flips the provider pool and every per-tenant provider pool, because a
        provider-wide disable that left the per-tenant pools open would still
        admit work.
        """
        self.upsert_pool(f"provider:{provider}", enabled=enabled)
        prefix = f"provider:{provider}:tenant:"
        for pool in self.list_pools():
            if pool.name.startswith(prefix):
                self.upsert_pool(pool.name, enabled=enabled)

    def set_provider_quota_state(self, provider: str, state: ProviderState) -> int:
        """Force every (provider, tenant) quota document to one state.

        Used by the admin disable path so the broker's AIMD loop cannot raise
        the adaptive target back up underneath an operator's drain.
        """
        query = self._db.collection(QUOTA).where(
            filter=FieldFilter("provider", "==", provider)
        )
        touched = 0
        now = self._now()
        for snap in query.stream():
            patch: dict[str, Any] = {"state": state.value, "updated_at": now}
            if state is ProviderState.DISABLED:
                patch["quota_derived_limit"] = 0
            else:
                # None means "no quota-derived cap", which is what re-enabling
                # has to mean: the hard max takes over again.
                patch["quota_derived_limit"] = None
                patch["cooldown_until"] = None
            self._db.collection(QUOTA).document(snap.id).update(patch)
            touched += 1
        return touched

    def get_control(self) -> dict[str, Any]:
        snap = self._db.collection(CONTROL).document(CONTROL_DOC).get()
        if not snap.exists:
            return {"dispatch_paused": False, "updated_at": None, "updated_by": None,
                    "reason": None}
        data = dict(snap.to_dict())
        data.setdefault("dispatch_paused", False)
        return data

    def set_dispatch_paused(self, paused: bool, *, by: str, reason: str | None = None) -> dict:
        payload = {
            "dispatch_paused": bool(paused),
            "updated_at": self._now(),
            "updated_by": by,
            "reason": reason,
        }
        self._db.collection(CONTROL).document(CONTROL_DOC).set(payload)
        return payload
