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

The EVENTS and cross-task ATTEMPTS pages use a (timestamp, document id) keyset
instead -- `_keyset_page` -- because for them a collapsed boundary is not
theoretical to their callers: `swarm_mcp.follow` gave up on a timestamp cursor
precisely because "two events written in the same microsecond ... a timestamp
cursor would either duplicate or drop". Both still order on ONE field, so both
are served by the single-field index on it (events) or by the composite index
already declared (`attempts-tenant-created`); no index is added.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import (
    Attempt,
    EndCause,
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
from swarm_common.states import (
    PENDING_STATES,
    TERMINAL_STATES,
    EventType,
    TaskState,
    assert_transition,
)

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
from .errors import Conflict, NotFound, Unpageable, ValidationFailed

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

#: Documents one keyset page may read because they share a single instant.
#:
#: A page boundary that falls inside a run of equal timestamps is placed by
#: document id, which means reading the whole run. Every writer of an event or
#: an attempt stamps `utcnow()` at microsecond resolution, so a run is one
#: document nearly always and a handful at worst. 1000 is far past anything
#: those writers produce and still a bounded read; past it the page refuses
#: (`Unpageable`) rather than guessing -- see `_same_instant`.
_TIE_READ_LIMIT = 1000


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
class KeysetCursor:
    """Where a keyset page stopped: the last row's timestamp and document id."""

    at: datetime
    doc_id: str


def encode_keyset(*, scope: str, order: str, at: datetime, doc_id: str) -> str:
    """An opaque token for the row a keyset page ended on.

    `scope` and `order` travel inside the token so that a token is refused,
    rather than reinterpreted, when it is sent back to a different listing or
    in the other direction. Both misuses would otherwise produce a page that
    looks right: an ascending cursor read descending serves the rows BEFORE it.

    Nothing in it is a credential. It names a timestamp and a document id the
    caller was already served, and every read it continues re-applies the
    tenant check before the cursor is used.
    """
    payload = json.dumps(
        {"v": 1, "s": scope, "o": order, "at": at.isoformat(), "id": doc_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_keyset(token: str | None, *, scope: str, order: str) -> KeysetCursor | None:
    if not token:
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        version, token_scope, token_order = raw["v"], raw["s"], raw["o"]
        at = datetime.fromisoformat(raw["at"])
        doc_id = raw["id"]
        if version != 1 or not isinstance(doc_id, str) or not doc_id:
            raise ValueError("not a keyset cursor")
    except (binascii.Error, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        # 422, as for `decode_cursor`: a bad token is a bad request, and a 404
        # would say the caller's rows had disappeared.
        raise ValidationFailed("page_token is not a valid cursor") from None
    if token_scope != scope:
        raise ValidationFailed(
            "page_token was issued by a different listing; start again without it",
            detail={"page_token_scope": token_scope},
        )
    if token_order != order:
        raise ValidationFailed(
            f"page_token was issued for order={token_order}; continue with the same "
            "order, or start again without a token",
            detail={"page_token_order": token_order, "requested_order": order},
        )
    return KeysetCursor(
        at=at if at.tzinfo else at.replace(tzinfo=timezone.utc), doc_id=doc_id
    )


@dataclass(frozen=True)
class Page:
    items: list[Any]
    next_page_token: str | None


@dataclass(frozen=True)
class LeaseScan:
    """A lease listing plus what the read behind it actually covered.

    The question this exists to answer is the accounting-drift check's: is
    there a LIVE lease that is not in these rows? Only when there is none can
    `units_held` against `pool.active` be read as evidence of a leak
    (docs/audits/2026-09-20/data-gaps-found-by-fanout.md, section 1).

    `active_beyond_window` is that answer: unreleased leases, under the same
    tenant filter, that the window left out. It is the field to read.

    `truncated` is only whether the window was full with more behind it. For
    an active-only read that means more live leases than `limit`; for a
    history read it means older documents exist, which after an
    environment's 201st admission is true for ever -- lease documents are
    never deleted and carry no TTL. It is kept for a pager, not for the
    drift check.

    `examined` is how many rows the route's `state` and `overdue_only`
    filters ran over.

    Computed HERE, where the query runs, and not inferred by a caller from the
    page length: `len(leases) == limit` is false in exactly the case that
    matters -- a window of released leases filtered down to nothing.
    """

    leases: list[Lease]
    examined: int
    truncated: bool
    active_beyond_window: int


@dataclass(frozen=True)
class ArtifactManifest:
    """What one task's `result_summary` says it wrote, unresolved.

    `complete` is the field that stops an empty list reading as an answer, and
    `skipped` is the same idea for a list that is short rather than empty: the
    worker drops files once an attempt passes `max_artifact_bytes`, and a
    listing that omitted them silently would show a short list with no reason
    for it.

    Nothing here has been resolved to an object. The `uri` on each entry is
    Firestore DATA, written by a worker, and the reader that turns one into a
    GCS key re-derives the key rather than trusting it -- see
    `InspectionService.read_artifact`.
    """

    artifacts: list[dict[str, Any]]
    skipped: list[str]
    artifact_bytes: Any
    complete: bool

    def find(self, name: str) -> dict[str, Any] | None:
        """The entry the SERVER already knows about, by exact name.

        Exact equality, never a prefix, a suffix or a normalised path: the
        caller names something this manifest lists or it names nothing. That is
        what makes the content route's key a function of the manifest rather
        than of the request.
        """
        for entry in self.artifacts:
            if entry.get("name") == name:
                return entry
        return None


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

    `tasks` -- the tasks the reads returned, decoded, so the workflow list can
    mask each step's input with its own task's masker (the PR #229 review)
    without a second read of the same documents. A document that does not
    decode is left out of it and still counts in `states`.
    """

    states: dict[str, TaskState]
    absent: list[str]
    unread: list[str]
    reads: int
    tasks: dict[str, Task] = field(default_factory=dict)


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

    def _same_instant(
        self,
        base: Any,
        field: str,
        at: datetime,
        decode: Callable[[dict[str, Any]], Any],
    ) -> list[Any]:
        """Every document under `base` whose `field` is exactly `at`.

        An equality filter on the ordering field, which the same index serves.
        BOUNDED, and loud past the bound: a run longer than `_TIE_READ_LIMIT`
        cannot be ordered by id without reading all of it, and the alternative
        to refusing is serving a page that skips or repeats rows with a 200.
        """
        query = base.where(filter=FieldFilter(field, "==", at)).limit(_TIE_READ_LIMIT + 1)
        snaps = list(query.stream())
        if len(snaps) > _TIE_READ_LIMIT:
            log.error(
                "more than %d documents share %s=%s; refusing to place a page boundary",
                _TIE_READ_LIMIT, field, at.isoformat(),
            )
            raise Unpageable(
                f"more than {_TIE_READ_LIMIT} records share one {field} timestamp, so "
                "this page's boundary cannot be placed exactly; no writer on this "
                "platform should produce that",
                detail={"field": field, "at": at.isoformat()},
            )
        return [decode(snap.to_dict()) for snap in snaps]

    def _keyset_page(
        self,
        base: Any,
        *,
        field: str,
        decode: Callable[[dict[str, Any]], Any],
        key: Callable[[Any], tuple[datetime, str]],
        limit: int,
        after: KeysetCursor | None,
        descending: bool,
        mint: Callable[[datetime, str], str],
        lower: datetime | None = None,
        upper: datetime | None = None,
    ) -> Page:
        """One page ordered by (`field`, document id), continuing from `after`.

        WHY NOT A TIMESTAMP CURSOR. `at > t` drops the rest of a run of equal
        timestamps at a boundary and `at >= t` serves it again. The order here
        is (timestamp, id), which is total, and the token names both halves.

        WHY NOT ORDER BY TWO FIELDS. `order_by(field).order_by(id)` needs a
        composite index nobody has declared. So the query orders on `field`
        alone -- served by the index that already exists -- and the id half of
        the order is applied here, in the two places it can matter:

          * the run AT the cursor's own instant is read whole
            (`_same_instant`) and filtered by id; the window query then starts
            strictly past that instant;
          * when the window came back full and the page ends inside the run at
            the window's far edge, that run is read whole too, because which of
            its members the window happened to return is the backend's choice,
            not the (timestamp, id) order the next token continues from.

        Neither extra read happens on the first page of an untied history.

        `lower` (inclusive) and `upper` (exclusive) bound `field`, so windows
        with a shared edge tile without overlap. `base` must carry EQUALITY
        filters only: the run reads add an equality on `field` to it.
        """
        mark = (after.at, after.doc_id) if after is not None else None

        def inside(row: Any) -> bool:
            at, doc_id = key(row)
            if lower is not None and at < lower:
                return False
            if upper is not None and at >= upper:
                return False
            if mark is None:
                return True
            return (at, doc_id) < mark if descending else (at, doc_id) > mark

        found: dict[str, Any] = {}

        def keep(rows: Iterable[Any]) -> None:
            for row in rows:
                if inside(row):
                    found.setdefault(key(row)[1], row)

        window_query = base
        if lower is not None:
            window_query = window_query.where(filter=FieldFilter(field, ">=", lower))
        if upper is not None:
            window_query = window_query.where(filter=FieldFilter(field, "<", upper))
        if after is not None:
            keep(self._same_instant(base, field, after.at, decode))
            window_query = window_query.where(
                filter=FieldFilter(field, "<" if descending else ">", after.at)
            )
        window_query = window_query.order_by(
            field,
            direction=firestore.Query.DESCENDING if descending else firestore.Query.ASCENDING,
        ).limit(limit + 1)
        window = [decode(snap.to_dict()) for snap in window_query.stream()]
        keep(window)

        rows = sorted(found.values(), key=key, reverse=descending)
        if len(window) > limit:
            # Full window: every instant before its far edge was read whole,
            # the edge's own run may not have been.
            edges = [key(row)[0] for row in window]
            edge = min(edges) if descending else max(edges)
            if key(rows[limit - 1])[0] == edge:
                keep(self._same_instant(base, field, edge, decode))
                rows = sorted(found.values(), key=key, reverse=descending)

        page = rows[:limit]
        token = mint(*key(page[-1])) if len(rows) > limit else None
        return Page(items=page, next_page_token=token)

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
        by: str | None = None,
    ) -> Tenant:
        """Change a tenant's limits, and move the pool that enforces them with it.

        `by` is passed through to `upsert_pool` as the admin who made the
        change; see there.

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
            self.upsert_pool(f"tenant:{tenant_id}", hard_limit=effective, by=by)
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

    def tasks_by_id(self, tenant_id: str, task_ids: Iterable[str]) -> dict[str, Task]:
        """This tenant's tasks among `task_ids`, by id, in batched reads.

        For a route that serves rows of many tasks and needs each row's task:
        `GET /v1/attempts` masks every attempt's `error` with its task's masker
        (the PR #229 review). `get_all` in chunks of 100, as the outcome ledger
        reads (`outcomes.py`), so a page costs one round trip per hundred
        tasks, not one per row. Tenant-checked like `get_task`: a task that is
        missing or another tenant's is simply not in the answer.
        """
        wanted = list(dict.fromkeys(task_id for task_id in task_ids if task_id))
        collection = self._db.collection(TASKS)
        found: dict[str, Task] = {}
        for start in range(0, len(wanted), 100):
            chunk = wanted[start : start + 100]
            for snap in self._db.get_all([collection.document(task_id) for task_id in chunk]):
                if not snap.exists:
                    continue
                data = snap.to_dict()
                if data.get("tenant_id") != tenant_id:
                    continue
                task = task_from_dict(data)
                found[task.id] = task
        return found

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

        ONE TRANSACTION, AND THE STATE READ INSIDE IT IS THE PRECONDITION. The
        branch -- cancel outright, flag only, or refuse -- is chosen from the
        state this transaction read, and the write commits only if nobody has
        changed the document since. This used to be a plain read, then a blind
        `update`. Two things could land between them, and each was overwritten
        by a decision about a document that no longer existed (incident
        wf_ebb3ab2d65664707a559, F-9; latent, never observed):

          * the scheduler admits the task. Read QUEUED, write CANCELLED over a
            DISPATCHED task: a terminal task still holding a live lease, with
            every pool it reserved still counted;
          * the worker finishes the task. Read RUNNING, write the flag and a
            `cancelled` event onto a SUCCEEDED task, and answer 200 where one
            read later the answer is 409.

        Inside a transaction Firestore serialises this against both writers.
        Admission is itself transactional and re-reads the task, so it either
        commits first (and this re-runs, sees LEASED/DISPATCHED and only flags)
        or it reads CANCELLED / `cancel_requested` and denies. The worker's
        terminal write works the same way. `test_request_cancel_is_
        transactional.py` drives both interleavings through the real route.

        The event is written in the same transaction as the flag, so a retried
        attempt records it once, and an event never exists for a write that
        did not commit.

        WHAT THIS STILL DOES NOT DO, deliberately: release a lease or touch a
        pool. Serialising the read does not make it safe to give capacity back
        from here (CONTRACT invariant 1). Only the worker, which knows its
        container has stopped, or the reconciler, which fences the generation
        first, may do that.
        """
        ref = self._db.collection(TASKS).document(task_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> Task:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != tenant_id:
                raise NotFound(f"task {task_id!r} not found")
            task = task_from_dict(data)
            if task.state in TERMINAL_STATES:
                raise Conflict(
                    f"task {task_id!r} is already terminal ({task.state.value})",
                    detail={"state": task.state.value},
                )

            now = self._now()
            patch: dict[str, Any] = {"cancel_requested": True, "updated_at": now}
            if task.state in PENDING_STATES:
                assert_transition(task.state, TaskState.CANCELLED)
                patch["state"] = TaskState.CANCELLED.value
                patch["completed_at"] = now
                # Why it ended, typed, beside when (contract request 23). Only
                # here, where THIS write ends the task: a flag on a task that
                # holds capacity ends nothing, and the worker or the reconciler
                # that ends it writes the same cause.
                patch["end_cause"] = EndCause.CANCEL_REQUESTED.value
                patch["park_reason"] = None
                patch["blocked_by"] = []
            txn.update(ref, patch)

            immediate = "state" in patch
            event = TaskEvent(
                event_id=new_id("ev"),
                task_id=task_id,
                tenant_id=tenant_id,
                # CANCELLED only for the transition THIS call made. A task that
                # holds capacity is not cancelled by this write -- only flagged
                # -- so its event is CANCEL_REQUESTED, and the terminal
                # CANCELLED is written by the worker (`control.finish`) or the
                # reconciler (F-3) when one of them actually finishes it.
                # Until 2026-09-24 this wrote CANCELLED for both and left
                # `phase` as the only discriminator, which is how incident
                # wf_ebb3ab2d65664707a559 showed four DISPATCHED tasks as
                # cancelled for over an hour (contract request 17).
                type=EventType.CANCELLED if immediate else EventType.CANCEL_REQUESTED,
                at=now,
                detail={
                    "requested_by": by,
                    "from_state": task.state.value,
                    # Kept although the type now says the same thing: a reader
                    # written against the old discriminator keeps working, and
                    # a stored legacy request that `event_from_dict` serves as
                    # CANCEL_REQUESTED is then the same shape as a new one.
                    "phase": "cancelled" if immediate else "cancel_requested",
                },
            )
            txn.set(
                ref.collection(EVENTS).document(event.event_id),
                event_to_firestore(event),
            )
            # What THIS call committed, not a re-read afterwards: the route
            # derives `released_immediately` from the returned state, and a
            # re-read would report a worker's later CANCELLED as ours.
            return task_from_dict({**data, **patch})

        return _apply(transaction)

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

    def list_events(
        self,
        tenant_id: str,
        task_id: str,
        *,
        limit: int = 200,
        page_token: str | None = None,
        descending: bool = False,
    ) -> Page:
        """One page of a task's events, oldest first unless `descending`.

        THIS USED TO RETURN THE HEAD AND NOTHING ELSE: `order_by(at).limit(n)`
        with no cursor, so a history longer than one page had an unreachable
        tail -- the end of a long run, which is the part anybody opens it for.

        THE DEFAULT IS UNCHANGED. No token and ascending is still "the oldest
        `limit` events", and row N is still row N for every limit that reaches
        it. `swarm_mcp.follow` keeps a COUNT as its cursor and depends on
        exactly that, so the id tie-break below is the one Firestore already
        applied implicitly (`__name__`, and an event's document id IS its
        `event_id`) -- made explicit, not changed.

        `descending` is the end of the run in one request.
        """
        self.get_task(tenant_id, task_id)         # tenant check before any read
        order = "desc" if descending else "asc"
        # Bound to the task: a token from another task's history would be a
        # timestamp that means nothing here, served as though it did.
        scope = f"events:{task_id}"
        after = decode_keyset(page_token, scope=scope, order=order)
        return self._keyset_page(
            self._db.collection(TASKS).document(task_id).collection(EVENTS),
            field="at",
            decode=event_from_dict,
            key=lambda event: (event.at, event.event_id),
            limit=limit,
            after=after,
            descending=descending,
            mint=lambda at, doc_id: encode_keyset(
                scope=scope, order=order, at=at, doc_id=doc_id
            ),
        )

    @staticmethod
    def artifact_manifest(task: Task) -> "ArtifactManifest":
        """WHERE THE MANIFEST LIVES, spelled once.

        Two readers need this now -- the metadata listing below and
        `InspectionService.read_artifact`, which resolves one entry to a GCS
        key -- and CLAUDE.md is explicit that every rule restated twice in this
        repository has since drifted. So the location is a single pure function
        over a task document, and neither reader is allowed its own copy.

        Pure on purpose: it takes the task the caller has ALREADY resolved
        inside its tenant rather than a `(tenant_id, task_id)` pair. A second
        `get_task` here would be a second tenant check, and two checks of one
        boundary are two chances for them to differ.
        """
        summary = task.result_summary or {}
        entries = summary.get("artifacts")
        if not isinstance(entries, list):
            entries = []
        skipped = summary.get("artifacts_skipped")
        if not isinstance(skipped, list):
            skipped = []
        return ArtifactManifest(
            artifacts=[dict(e) for e in entries if isinstance(e, dict)],
            skipped=[str(name) for name in skipped],
            artifact_bytes=summary.get("artifact_bytes"),
            # `result_summary` is written once, by `finish()`, at terminal
            # state. Until then "no artifacts yet" and "produced none" are the
            # same empty list, and only this flag tells them apart.
            complete=bool(task.result_summary),
        )

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
        manifest = self.artifact_manifest(self.get_task(tenant_id, task_id))
        return {
            "artifacts": manifest.artifacts[:limit],
            "artifacts_skipped": manifest.skipped,
            "artifact_bytes": manifest.artifact_bytes,
            "complete": manifest.complete,
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
        each. This is the pattern `SchedulerStore.parent_ends` already uses for
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
        tasks: dict[str, Task] = {}
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
            try:
                tasks[task_id] = task_from_dict(data)
            except (KeyError, TypeError, ValueError):
                # Its state was read; only the step input's masking goes
                # without it, and the codec then uses a sibling's masker.
                pass
        return StepStateRead(
            states=states, absent=absent, unread=wanted[limit:], reads=reads, tasks=tasks
        )

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
        by: str | None = None,
    ) -> SlotPool:
        """Create or patch a pool's ceilings and switch. Never `active`.

        `by` is the VERIFIED caller of an admin route, and only an admin route
        passes it. It lands as `admin_changed_by` / `admin_changed_at`, not as
        `updated_by`: `updated_at` on a pool is rewritten by the admission and
        release transactions on every lease, so a name beside it would pair
        one writer's timestamp with another writer's identity. Before this, a
        ceiling the verification gate narrowed could not be told apart from
        one an operator narrowed -- the only record was a counter with no
        caller (docs/audits/2026-09-22/race-test-needs-a-write.md).

        Internal writers (tenant creation, the quota broker) pass nothing, and
        leave the last admin's name in place.
        """
        ref = self._db.collection(POOLS).document(name)
        snap = ref.get()
        now = self._now()
        attribution: dict[str, Any] = (
            {"admin_changed_by": by, "admin_changed_at": now} if by else {}
        )
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
                    **attribution,
                }
            )
            return pool
        patch: dict[str, Any] = {"updated_at": now, **attribution}
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

        `active_only` returns the newest `limit` UNRELEASED leases. It used to
        read the newest `limit` documents of any state and drop the released
        ones afterwards, so a live lease older than `limit` newer released
        ones never reached the page -- and lease documents are never deleted,
        so every environment past `limit` admissions was exposed to that. See
        `scan_leases` for how the live set is read without a new index.

        `tenant_id` None means every tenant, which is why the route is
        admin-gated. The history read (`active_only=False`) orders on
        `created_at`: with a tenant id the declared `leases-tenant-created`
        composite index serves it, and without one a single-field index does.

        The listing alone cannot say whether a live lease was left out;
        `scan_leases` returns the same rows with that answer, and is what the
        admin route uses.
        """
        return self.scan_leases(tenant_id, active_only=active_only, limit=limit).leases

    def _live_leases(self, tenant_id: str | None) -> list[Lease]:
        """Every unreleased lease, under the tenant filter, newest first.

        `released_at == None` is the query the reconciler already runs in full
        on every pass (reconciler/store.py, `snapshot`), and
        `acquire_lease_in_transaction` (swarm_common/admission.py) writes the
        field as an explicit null, so a live lease matches. With the tenant
        equality added it is two equality filters and no ordering, which
        Firestore serves by merging single-field indexes -- no composite index.
        Ordering on `created_at` in the query would need one
        (`released_at`, `created_at DESC`), and the set is small enough not
        to: it is what holds capacity NOW, not history. Every unreleased lease
        holds units of the `global` pool until it is released or the
        reconciler reclaims it, so its size follows that pool's limit rather
        than the number of admissions ever made -- and the reconciler already
        pays for reading all of it once a pass.

        Sorted here instead. `lease_id` breaks ties so two leases admitted in
        the same microsecond keep a stable order between calls.
        """
        query: Any = self._db.collection(LEASES).where(
            filter=FieldFilter("released_at", "==", None)
        )
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        live = [lease_from_dict(snap.to_dict()) for snap in query.stream()]
        live.sort(key=lambda lease: (lease.created_at, lease.lease_id), reverse=True)
        return live

    def scan_leases(
        self,
        tenant_id: str | None = None,
        *,
        active_only: bool = True,
        limit: int = 200,
    ) -> LeaseScan:
        """`list_leases`, plus how many live leases the rows left out.

        ACTIVE ONLY reads the live set itself (`_live_leases`) and keeps the
        newest `limit`. One read, so the rows and the count come from the same
        snapshot; a live lease is outside the rows only when more live leases
        exist than `limit`.

        HISTORY (`active_only=False`) reads the newest `limit` documents of
        any state -- one past `limit` to say whether older ones exist, then
        dropped -- and then the live set, and counts the live leases whose id
        is not in the window. Window FIRST, and by id rather than by
        subtracting two counts: a lease admitted between the reads is counted
        as beyond (true, and the safe direction), and a lease released
        between them is counted nowhere. Subtracting a count from the
        window's live rows would instead undercount whenever a lease in the
        window was released between the reads -- turning a real cut into
        "nothing was cut", the one wrong answer the drift check cannot absorb.
        """
        if active_only:
            live = self._live_leases(tenant_id)
            window = live[:limit]
            return LeaseScan(
                leases=window,
                examined=len(window),
                truncated=len(live) > limit,
                active_beyond_window=len(live) - len(window),
            )

        query: Any = self._db.collection(LEASES)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        query = query.limit(limit + 1)
        documents = [lease_from_dict(snap.to_dict()) for snap in query.stream()]
        truncated = len(documents) > limit
        documents = documents[:limit]
        in_window = {lease.lease_id for lease in documents}
        beyond = sum(
            1 for lease in self._live_leases(tenant_id) if lease.lease_id not in in_window
        )
        return LeaseScan(
            leases=documents,
            examined=len(documents),
            truncated=truncated,
            active_beyond_window=beyond,
        )

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

    def page_attempts(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        page_token: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Page:
        """A tenant's attempts across every task, newest first, paged.

        `list_attempts(task_id=None)` answered the same question with no
        cursor, and nothing called it: the only route passed a task id, so "what
        did this tenant's runs cost" was one request per task. This is the paged
        form `GET /v1/attempts` serves.

        THE TENANT FILTER IS THE FIRST CLAUSE, an equality on `tenant_id`, as
        on every list in this module; the caller's tenant comes from
        `deps.tenant_scope` and never from the request (invariant 9).

        Served by `attempts-tenant-created` (tenant_id ASC, created_at DESC):
        `since`/`until` and the cursor are all ranges on `created_at`, the
        index's ordered field, and the run read at a boundary is two equalities.
        """
        after = decode_keyset(page_token, scope="attempts", order="desc")
        return self._keyset_page(
            self._db.collection(ATTEMPTS).where(
                filter=FieldFilter("tenant_id", "==", tenant_id)
            ),
            field="created_at",
            decode=attempt_from_dict,
            key=lambda attempt: (attempt.created_at, attempt.attempt_id),
            limit=limit,
            after=after,
            descending=True,
            mint=lambda at, doc_id: encode_keyset(
                scope="attempts", order="desc", at=at, doc_id=doc_id
            ),
            lower=since,
            upper=until,
        )

    def set_provider_enabled(
        self, provider: str, enabled: bool, *, by: str | None = None
    ) -> None:
        """Enable/disable a provider platform-wide.

        Flips the provider pool and every per-tenant provider pool, because a
        provider-wide disable that left the per-tenant pools open would still
        admit work. `by` attributes every pool it flips; see `upsert_pool`.
        """
        self.upsert_pool(f"provider:{provider}", enabled=enabled, by=by)
        prefix = f"provider:{provider}:tenant:"
        for pool in self.list_pools():
            if pool.name.startswith(prefix):
                self.upsert_pool(pool.name, enabled=enabled, by=by)

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
