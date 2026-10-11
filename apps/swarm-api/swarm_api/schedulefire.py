"""The schedule tick and its firings (docs/schedules.md §2, lane S2).

One Cloud Scheduler job calls `POST /v1/admin/schedules/tick` every minute as
`swarm-schedule-tick` (SD10). This module is everything that call does:

  1. CLAIM each due schedule's slot, in one Firestore transaction per
     schedule (§2.2): re-read the schedule, work out which slots came due
     since `next_slot` (catch-up, §2.4), create `schedule_firings/{id}:{slot}`
     -- a document that already exists aborts the claim, so a retried or a
     second tick never fires a slot twice -- and advance `next_slot` and
     `next_run_at` past now, jitter included (§2.6).
  2. FINISH each claimed firing (§2.7): as the schedule's OWNER, in the
     schedule's TENANT, while the directory still says they are a member
     (`schedule_owner_auth`); through the type's executor, which submits
     through the ordinary `SubmissionService` paths. A firing whose creation
     failed half-way stays `claimed`, and the next tick finishes it AFTER
     looking for work already carrying its `metadata.schedule.firing_id`,
     so work created and not recorded is adopted, never created twice.
  3. ADVANCE live firings (§2.9): derive each one's outcome from its work,
     the way a workflow's state is derived from its steps, add its cost to
     the day's spend, and apply the hard stops of §4.3-§4.4.

INVARIANTS, each by construction here:

  1. Demand only from LEASED..RUNNING. A schedule, a firing, a `queued`
     firing and an `awaiting_approval` firing are Firestore documents and
     nothing else. Work is created only by `SubmissionService`, `QUEUED` or
     `READY`, and takes capacity only when admission leases it. Nothing here
     reserves, waits for or even reads capacity; a full pool is the ordinary
     queue.
  2. All-or-nothing reservation: untouched. Admission is not called, and the
     work this module submits is admitted by the same transaction as any.
  3. Concurrency from LEASED: untouched for the same reason. A schedule's
     `max_concurrent` counts its own live WORK ITEMS before it creates more;
     it is a bound on what a schedule may start, not an admission rule.
  9. Per-tenant isolation. Every firing is submitted as the schedule's
     stored `owner` in its stored `tenant_id`, both from the document, never
     from the caller -- the tick's identity submits nothing as itself. The
     owner's membership is asked of the directory at every firing; a
     departed owner's firing is skipped and the schedule auto-paused. Work is
     found again only under the schedule's own tenant id.

WHAT IS NOT HERE. Each type's executor (`swarm_api/schedtypes/<type>.py`,
lanes S6 and S10) decides what a firing creates; this module defines the seam
they implement (`Firing`, `load_executor`). An `approve` run gate holds the
firing at `awaiting_approval` with no work; the approval itself is lane S5's
(`approvals.firing_move` records `run_approval` on the firing in the
decision's transaction), and the tick then finishes the approved firing like
a claimed one (`_finish_approved`), only while the approval is the firing's
own tenant's, approved, and for the digest it would run with. The tenant
routes (S3) call `fire_now`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_common.admission import _snapshot
from swarm_common.identity import Principal
from swarm_common.states import TaskState

from . import cronexpr, refusals, schedaudit, scheduletypes, schedules
from .attempt_totals import totals_for
from .auth import AuthContext
from .errors import ApiError, Conflict, Forbidden, NotFound, UpstreamUnavailable
from .issueruns import TERMINAL_RUN_STATES, IssueRuns, RunState
from .repoindex import POLL_BUDGET_SECONDS
from .repositories import Repositories
from .schemas import TaskCreate, WorkflowCreate
from .validation import SCHEDULE_METADATA_KEY, schedule_mark_allowed

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants. Each is a decision, with its reason.
# --------------------------------------------------------------------------

FIRINGS = "schedule_firings"
TICKS = "schedule_ticks"
#: One document: the windows in which SCHEDULES_ENABLED was off (§2.11).
CONTROL = "schedule_control"
CONTROL_DOC = "switch"
TASKS = "tasks"

#: §2.10: at most this many due schedules read per tick, oldest due first.
MAX_DUE_PER_TICK = 200
#: §2.10: at most this many fired per tenant per tick, round-robin by tenant,
#: so one tenant's 25 schedules due at 09:00 delay another's by one tick.
PER_TENANT_PER_TICK = 5
#: §2.10: stop STARTING work at 240 s, the repository poll's budget and
#: reason (the Cloud Scheduler attempt deadline sits above it).
TICK_BUDGET_SECONDS = POLL_BUDGET_SECONDS
#: §2.2: a firing still `claimed` this long after its claim is finished by
#: the next tick.
CLAIM_STALE = timedelta(minutes=2)
#: How long one finisher holds a claimed firing, from the WALL CLOCK at the
#: lease (never the tick's start). Longer than a whole tick's budget (240 s),
#: so a firing leased late in one tick is still held when the next ticks
#: start; each lease carries a token, and only its holder may move the
#: firing. Two ticks that overlap would otherwise both create its work; the
#: adoption lookup is the second line.
FINISH_LEASE = timedelta(minutes=5)
#: §2.4: a slot is missed once `now - slot` passes the smaller of this and
#: half the schedule's interval.
GRACE_MAX = timedelta(minutes=15)
#: §2.4: a long outage writes at most this many skip records, the last of
#: them carrying the count of the rest.
MAX_SKIP_RECORDS = 50
#: Catch-up stops counting missed slots one by one past this many and jumps
#: to the last week before now. A 15-minute schedule broken for a year is
#: 35,000 slots; the count is then reported as "at least".
SCAN_LIMIT = 5000
SCAN_JUMP = timedelta(days=8)
#: §1.2, §2.10: firings kept 90 days, tick reports 7, by TTL on `expire_at`.
FIRING_TTL = timedelta(days=90)
TICK_TTL = timedelta(days=7)
#: §2.9: live firings advanced per tick, oldest first.
ADVANCE_PAGE = 50
#: Read this many to find the oldest: an equality filter with no order needs
#: no composite index (S4 owns the indexes; see `_live_firings`).
SCAN_PAGE = 500
#: §4.4: N failed or refused firings in a row pause the schedule; 1-10.
CONSECUTIVE_FAILURES_DEFAULT = 3
#: The actor recorded on what the tick itself does.
TICK_ACTOR = "schedule-tick"
#: §2.11: the owner's kill switch. settings.py is not lane S2's file, so the
#: variable is read here until a settings field exists; the field wins.
ENABLED_ENV = "SCHEDULES_ENABLED"

#: §1.4's firing states.
CLAIMED = "claimed"
AWAITING_APPROVAL = "awaiting_approval"
QUEUED = "queued"
CREATED = "created"
DONE = "done"
SKIPPED = "skipped"
REFUSED = "refused"

#: §1.2's skip codes. `SCHEDULE_NOT_ENABLED` and `TYPE_UNAVAILABLE` are this
#: lane's additions: a schedule paused, disabled or deleted between a claim
#: and its finish, and a type withdrawn from the catalogue (§1.3).
OVERLAP = "OVERLAP"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
MISSED_SLOT = "MISSED_SLOT"
OWNER_NOT_MEMBER = "OWNER_NOT_MEMBER"
REPOSITORY_NOT_GRANTED = "REPOSITORY_NOT_GRANTED"
SCHEDULES_DISABLED = "SCHEDULES_DISABLED"
DRY_RUN = "DRY_RUN"
SCHEDULE_NOT_ENABLED = "SCHEDULE_NOT_ENABLED"
TYPE_UNAVAILABLE = "TYPE_UNAVAILABLE"
RUN_OVER_BUDGET = "RUN_OVER_BUDGET"
CONSECUTIVE_FAILURES = "CONSECUTIVE_FAILURES"

#: The `schedule_audit` action written when an approved firing is held back
#: because its approval does not release it, once per approval and digest.
RUN_APPROVAL_REFUSED = "run_approval_refused"
#: Why, in that entry's `detail.code`: the approval record is missing, another
#: tenant's, of another kind or for another firing; it is not approved; or the
#: digest approved is not the one the firing would run with. The last is the
#: approvals route's own 409 code for the same case.
APPROVAL_NOT_FOUND = "approval_not_found"
APPROVAL_NOT_APPROVED = "approval_not_approved"
APPROVAL_CHANGED = "approval_changed"

#: What a person reads for each auto-pause (§1.3).
PAUSE_COPY = {
    OWNER_NOT_MEMBER: "Paused: its owner is no longer a member of this tenant; take ownership to resume",
    RUN_OVER_BUDGET: "Paused: a run cost more than its per-run budget",
    CONSECUTIVE_FAILURES: "Paused after {n} failed runs in a row",
    TYPE_UNAVAILABLE: "Disabled: its type is no longer in the catalogue",
}


# --------------------------------------------------------------------------
# Refusals. The three new stops ship report-only (refusals.SWITCHES, §4.4).
# --------------------------------------------------------------------------


class BudgetExhausted(Forbidden):
    """§4.3: today's reported spend plus what is reserved passes `per_day_usd`."""

    code = BUDGET_EXHAUSTED


class RunOverBudget(Forbidden):
    """§4.3: a run recorded more than `per_run_usd`; the schedule pauses."""

    code = RUN_OVER_BUDGET


class ConsecutiveFailures(Forbidden):
    """§4.4: N failed or refused firings in a row; the schedule pauses."""

    code = CONSECUTIVE_FAILURES


class ScheduleOwnerNotMember(Forbidden):
    """The schedule's owner has left its tenant. ALWAYS enforced: the
    established `owner_not_member` refusal of the repository poll, for the
    same reason (invariant 9)."""

    code = "owner_not_member"


class RoomExceeded(RuntimeError):
    """An executor tried to create more work items than `max_concurrent`
    leaves. A defect in the executor, not a refusal: the tick counts it as an
    error and the firing stays claimed."""


# --------------------------------------------------------------------------
# Who a firing submits as
# --------------------------------------------------------------------------


def member_auth(
    ctx: Any,
    *,
    tenant_id: str,
    email: str,
    subject: str,
    not_member: Callable[[str], Exception],
) -> AuthContext:
    """The AuthContext of a stored member, in a stored tenant, NOW a member.

    The one shape `routes.runs.run_owner_auth` and
    `routes.admin.registration_owner_auth` each restate (§2.7 asks this lane
    to fold them into one helper in its own file, and to leave those two
    alone). Built from documents, never from the caller; nothing wider than
    an ordinary member (`is_admin`, `member_scope` and `tenant_member` are
    the empty values); `tenant_principal` is the STORED one, so
    `SubmissionService.tenant_for` checks the tenant as registered.

    Membership is asked of the directory on every call
    (`Authenticator.is_tenant_member`). Not a member: `not_member(message)`
    is raised. The lookup failing: `UpstreamUnavailable`, untouched, so the
    caller retries rather than deciding either way.
    """
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    email = (email or "").strip().lower()
    if not ctx.authenticator.is_tenant_member(email, tenant):
        raise not_member(
            f"{email or '(not recorded)'} is no longer a member of tenant {tenant_id!r}, "
            "so nothing is submitted on their behalf"
        )
    return AuthContext(
        principal=Principal(
            email=email,
            # Not a token subject: this context was never authenticated.
            subject=subject,
            domain=email.rsplit("@", 1)[-1],
            groups=(),
        ),
        tenant_id=tenant_id,
        is_admin=False,
        tenant_principal=tenant.principal,
    )


def schedule_owner_auth(ctx: Any, schedule: Mapping[str, Any]) -> AuthContext:
    """§2.7: the schedule's stored `owner`, in its stored `tenant_id`, while a member."""
    return member_auth(
        ctx,
        tenant_id=str(schedule["tenant_id"]),
        email=str(schedule.get("owner") or ""),
        subject=f"schedule:{schedule['schedule_id']}",
        not_member=ScheduleOwnerNotMember,
    )


# --------------------------------------------------------------------------
# The executor seam (lanes S6, S10 implement it)
# --------------------------------------------------------------------------


class Executor(Protocol):
    """What `swarm_api/schedtypes/<type>.py` provides, as module functions.

    `create(firing)` creates the firing's work through `firing.submit_tasks`,
    `firing.submit_workflow` or `firing.api_action` (or, for an issue run, the
    issue-run creation with `firing.mark`), at most `firing.room` items, and
    returns the work entries those return. `[]` is an answer: nothing was
    due (a sweep with no candidates), and the firing ends `succeeded`.

    `dry_run(firing)` returns, as data, what `create` would have made. It
    submits nothing, writes nothing to GitHub and never runs an agent (§2.8).
    """

    def create(self, firing: "Firing") -> list[dict[str, Any]]: ...

    def dry_run(self, firing: "Firing") -> dict[str, Any]: ...


def load_executor(entry: scheduletypes.ScheduleType) -> Executor | None:
    """The type's executor module, or None when the type is not available."""
    available, _reason = scheduletypes.availability(entry)
    if not available:
        return None
    module = importlib.import_module(entry.module)
    if not (callable(getattr(module, "create", None)) and callable(getattr(module, "dry_run", None))):
        log.error("schedule type %s: its executor defines no create/dry_run", entry.name)
        return None
    return module  # type: ignore[return-value]


@dataclass
class Firing:
    """One firing, as its executor sees it. Submits as `owner` and marks the work."""

    ctx: Any
    schedule: Mapping[str, Any]
    firing: Mapping[str, Any]
    owner: AuthContext
    #: The scope resolved AT FIRING TIME (§1.1): registrations the tenant
    #: still has. Empty for a platform-scope type.
    repo_ids: list[str]
    #: How many work items this firing may create (`max_concurrent` less the
    #: schedule's live items, §4.3).
    room: int
    now: datetime
    created: int = 0
    #: Persists a non-task work item on the firing the moment it exists, so a
    #: finisher after a crash adopts it rather than doing it again (§2.2).
    record: Callable[[dict[str, Any]], None] = lambda item: None

    @property
    def mark(self) -> dict[str, Any]:
        """`metadata.schedule` (§2.7): what links the work to this firing."""
        return {
            "schedule_id": self.schedule["schedule_id"],
            "firing_id": self.firing["firing_id"],
            "slot": _iso(self.firing.get("slot")),
            "type": self.schedule["type"],
        }

    def _spend(self, count: int) -> None:
        if self.created + count > self.room:
            raise RoomExceeded(
                f"schedule {self.schedule['schedule_id']}: {self.created + count} work items "
                f"exceed the {self.room} max_concurrent leaves"
            )
        self.created += count

    def submit_tasks(self, specs: Sequence[TaskCreate], *, repo_id: str | None = None) -> list[dict[str, Any]]:
        """Through `SubmissionService.submit_tasks`, as the owner, each task marked."""
        self._spend(len(specs))
        mark = self.mark
        marked = [
            spec.model_copy(update={"metadata": {**spec.metadata, SCHEDULE_METADATA_KEY: mark}})
            for spec in specs
        ]
        with schedule_mark_allowed(mark):
            result = self.ctx.submissions.submit_tasks(self.owner, marked)
        return [{"kind": "task", "id": task.id, "repo_id": repo_id} for task in result.tasks]

    def submit_workflow(self, spec: WorkflowCreate, *, repo_id: str | None = None) -> dict[str, Any]:
        """Through `SubmissionService.submit_workflow`, as the owner; every step marked."""
        self._spend(1)
        mark = self.mark
        marked = spec.model_copy(update={"metadata": {**spec.metadata, SCHEDULE_METADATA_KEY: mark}})
        with schedule_mark_allowed(mark):
            result = self.ctx.submissions.submit_workflow(self.owner, marked)
        return {"kind": "workflow", "id": result.workflow.workflow_id, "repo_id": repo_id}

    def api_action(self, name: str, *, outcome: str, detail: Mapping[str, Any] | None = None,
                   repo_id: str | None = None) -> dict[str, Any]:
        """An action run inside swarm-api (SD8): no task, no capacity, no worker.

        Ended when it is recorded: `outcome` is `succeeded` or `failed`.
        """
        if outcome not in ("succeeded", "failed"):
            raise ValueError(f"an api_action ends succeeded or failed, not {outcome!r}")
        self._spend(1)
        item = {"kind": "api_action", "id": name, "repo_id": repo_id, "outcome": outcome,
                "detail": dict(detail or {})}
        self.record(item)
        return item

    def recorded(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Record work made outside `submit_tasks`/`submit_workflow` -- an issue
        run (S5, S6) -- as soon as it exists, and return it. Tasks need no
        record: their `metadata.schedule` mark is what is adopted."""
        if item.get("kind") not in ("issue_run", "api_action"):
            raise ValueError("tasks and workflows are recorded by their mark")
        self._spend(1)
        entry = dict(item)
        self.record(entry)
        return entry


# --------------------------------------------------------------------------
# Pure parts: slots, catch-up, fairness, outcome
# --------------------------------------------------------------------------


def _aware(moment: Any) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _iso(moment: Any) -> str | None:
    moment = _aware(moment)
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def unix_minute(moment: datetime) -> int:
    return int(_aware(moment).timestamp()) // 60


def firing_id(schedule_id: str, slot: datetime) -> str:
    """§1.2: `{schedule_id}:{slot}`, the slot's Unix minute before jitter."""
    return f"{schedule_id}:{unix_minute(slot)}"


@dataclass(frozen=True)
class SlotPlan:
    """What one claim does: at most one slot fires, the rest are skip records."""

    fire: datetime | None
    trigger: str | None
    #: (slot, code, detail) for each skip record, oldest first.
    skips: list[tuple[datetime, str, dict[str, Any]]]


def in_windows(slot: datetime, windows: Iterable[Mapping[str, Any]]) -> bool:
    for window in windows:
        start, end = _aware(window.get("from")), _aware(window.get("to"))
        if start is not None and slot >= start and (end is None or slot <= end):
            return True
    return False


def plan_slots(doc: Mapping[str, Any], now: datetime, windows: Sequence[Mapping[str, Any]] = ()) -> SlotPlan:
    """§2.3-§2.4 and §2.11: which due slot fires and which are recorded skipped.

    The slots come from the STORED cron and `next_slot`, never from the tick's
    time, so a late tick does not move a slot: it only decides which are due.
    A slot is missed once `now - slot` passes its grace, the smaller of 15
    minutes and half the interval. The newest due slot fires if it is within
    its grace; otherwise it fires only under `catch_up: run_once`, with
    trigger `catch_up`. Every other due slot gets a skip record --
    `SCHEDULES_DISABLED` when it fell while the kill switch was off,
    `MISSED_SLOT` otherwise -- at most MAX_SKIP_RECORDS of them, the oldest
    of which then carries the count of the slots it stands for.
    """
    expr = cronexpr.parse(doc["cron"])
    tz = cronexpr.zone(doc["timezone"])
    earliest = _aware(doc["next_slot"])
    # Only the newest MAX_SKIP_RECORDS + 1 due slots are kept; the rest are
    # counted. `at_least` when the scan jumped and the count is a floor.
    kept: deque[datetime] = deque([earliest], maxlen=MAX_SKIP_RECORDS + 1)
    count = 1
    at_least = False
    while True:
        following = cronexpr.next_after(expr, kept[-1], tz)
        if following > now:
            break
        if count >= SCAN_LIMIT and not at_least:
            at_least = True
            restart = cronexpr.next_after(expr, max(kept[-1], now - SCAN_JUMP), tz)
            if restart > now:
                following = restart
                break
            following = restart
        kept.append(following)
        count += 1
    newest = kept[-1]
    grace = min(GRACE_MAX, (following - newest) / 2)
    fire: datetime | None = None
    trigger: str | None = None
    if now - newest <= grace:
        fire, trigger = newest, "cron"
    elif doc["policy"].get("catch_up") == "run_once":
        fire, trigger = newest, "catch_up"
    older = list(kept)[:-1] if fire is not None else list(kept)
    older_count = count - 1 if fire is not None else count

    def code_for(moment: datetime) -> str:
        return SCHEDULES_DISABLED if in_windows(moment, windows) else MISSED_SLOT

    if older_count <= MAX_SKIP_RECORDS:
        return SlotPlan(fire=fire, trigger=trigger, skips=[(m, code_for(m), {}) for m in older])
    # A long outage: the newest MAX_SKIP_RECORDS - 1 one by one, and one
    # record, at the earliest slot, standing for every slot before them.
    individual = older[len(older) - (MAX_SKIP_RECORDS - 1):]
    through = older[len(older) - MAX_SKIP_RECORDS]
    summary: dict[str, Any] = {"slots": older_count - len(individual), "through": _iso(through)}
    if at_least:
        summary["at_least"] = True
    skips = [(earliest, MISSED_SLOT, summary)] + [(m, code_for(m), {}) for m in individual]
    return SlotPlan(fire=fire, trigger=trigger, skips=skips)


def fair_pick(rows: Sequence[Mapping[str, Any]], per_tenant: int = PER_TENANT_PER_TICK) -> tuple[list[Mapping[str, Any]], int]:
    """§2.10: round-robin by tenant in due order, at most `per_tenant` each.

    Returns (picked, deferred). The deferred stay due for the next minute.
    """
    queues: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        queues.setdefault(str(row.get("tenant_id")), []).append(row)
    picked: list[Mapping[str, Any]] = []
    for round_ in range(per_tenant):
        for tenant in list(queues):
            if round_ < len(queues[tenant]):
                picked.append(queues[tenant][round_])
    return picked, len(rows) - len(picked)


def task_outcome(state: str) -> str | None:
    """A task's contribution to its firing's outcome; None while it is live."""
    try:
        task_state = TaskState(state)
    except ValueError:
        return None
    if task_state == TaskState.SUCCEEDED:
        return "succeeded"
    if task_state in (TaskState.FAILED, TaskState.DEAD_LETTERED):
        return "failed"
    if task_state == TaskState.CANCELLED:
        return "cancelled"
    return None


def run_outcome(state: str) -> str | None:
    """An issue run's: NOT_READY is an answer, so it counts as succeeded (§2.9)."""
    try:
        run_state = RunState(state)
    except ValueError:
        return None
    if run_state not in TERMINAL_RUN_STATES:
        return None
    if run_state in (RunState.DONE, RunState.NOT_READY):
        return "succeeded"
    if run_state == RunState.FAILED:
        return "failed"
    return "cancelled"


def combine(outcomes: Sequence[str], *, independent: bool) -> str:
    """§2.9: a firing's outcome from its work items' outcomes.

    All succeeded: `succeeded`. Any failed: `failed`, or `partial` when the
    type does several independent things and some succeeded. Otherwise any
    cancelled: `cancelled` (again `partial` beside successes of an
    independent type). No work at all: `succeeded`, because "nothing was due"
    is an answer.
    """
    if not outcomes or all(o == "succeeded" for o in outcomes):
        return "succeeded"
    some_ok = any(o == "succeeded" for o in outcomes)
    if independent and some_ok:
        return "partial"
    if any(o == "failed" for o in outcomes):
        return "failed"
    return "cancelled"


def params_digest(schedule: Mapping[str, Any], repo_ids: Sequence[str]) -> str:
    """§1.2: sha256 of what the firing runs with. A run approval approves this."""
    body = {
        "type": schedule["type"],
        "scope": {"mode": schedule["scope"]["mode"], "repo_ids": sorted(repo_ids)},
        "params": schedule.get("params") or {},
        "gate": schedule.get("gate") or {},
        "budget": schedule.get("budget") or {},
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def failures_to_pause(schedule: Mapping[str, Any]) -> int:
    value = (schedule.get("policy") or {}).get("max_consecutive_failures", CONSECUTIVE_FAILURES_DEFAULT)
    try:
        return min(10, max(1, int(value)))
    except (TypeError, ValueError):
        return CONSECUTIVE_FAILURES_DEFAULT


def schedules_enabled(settings: Any, environ: Mapping[str, str] | None = None) -> bool:
    """§2.11's switch. On unless the owner set it to false."""
    configured = getattr(settings, "schedules_enabled", None)
    if configured is not None:
        return bool(configured)
    environ = os.environ if environ is None else environ
    raw = environ.get(ENABLED_ENV, "").strip().lower()
    if raw in ("", "true", "1", "on", "yes"):
        return True
    if raw in ("false", "0", "off", "no"):
        return False
    # A value nobody meant: closed, and said.
    log.error("%s=%r is neither true nor false; the schedule tick is off", ENABLED_ENV, raw)
    return False


# --------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------


@dataclass
class TickReport:
    started_at: datetime
    disabled: bool = False
    due: int = 0
    claimed: int = 0
    fired: int = 0
    skipped: int = 0
    refused: int = 0
    held: int = 0
    queued: int = 0
    advanced: int = 0
    deferred: int = 0
    errors: int = 0
    truncated: bool = False
    lateness: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        if self.disabled:
            return {"disabled": True}
        return {
            "started_at": _iso(self.started_at),
            "due": self.due,
            "claimed": self.claimed,
            "fired": self.fired,
            "skipped": self.skipped,
            "refused": self.refused,
            "held": self.held,
            "queued": self.queued,
            "advanced": self.advanced,
            "deferred": self.deferred,
            "errors": self.errors,
            "truncated": self.truncated,
            "failures": self.failures,
        }


class Ticker:
    """One tick's work over one AppContext. `executors` maps a type name to
    an executor for tests; production loads them by file (`load_executor`)."""

    def __init__(
        self,
        ctx: Any,
        *,
        executors: Mapping[str, Executor] | None = None,
        clock: Callable[[], float] = time.monotonic,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self._executors = dict(executors or {})
        #: fid -> this Ticker's lease token, while it finishes that firing.
        self._leases: dict[str, str] = {}
        self._clock = clock
        self._environ = environ

    # -- collections --------------------------------------------------------

    def _schedule_ref(self, schedule_id: str) -> Any:
        return self.db.collection(schedules.COLLECTION).document(schedule_id)

    def _firing_ref(self, fid: str) -> Any:
        return self.db.collection(FIRINGS).document(fid)

    def _control_ref(self) -> Any:
        return self.db.collection(CONTROL).document(CONTROL_DOC)

    # -- the tick -----------------------------------------------------------

    def tick(self) -> TickReport:
        now = _aware(self.ctx.now())
        report = TickReport(started_at=now)
        started = self._clock()
        if not schedules_enabled(self.ctx.settings, self._environ):
            self._switch(now, enabled=False)
            report.disabled = True
            return report
        windows = self._switch(now, enabled=True)
        rows = self._due(now)
        report.due = len(rows)
        picked, report.deferred = fair_pick(rows)
        if report.deferred or len(rows) >= MAX_DUE_PER_TICK:
            report.truncated = True
        for row in picked:
            if self._clock() - started >= TICK_BUDGET_SECONDS:
                report.truncated = True
                break
            schedule_id = str(row.get("schedule_id"))
            try:
                fid = self.claim(schedule_id, now, windows, report)
                if fid:
                    self.finish(fid, now, report)
            except Exception as exc:  # one schedule's error does not stop the page (§2.10)
                self._failed(report, schedule_id, exc)
        if self._clock() - started < TICK_BUDGET_SECONDS:
            self._finish_stale(now, report, started)
        if self._clock() - started < TICK_BUDGET_SECONDS:
            self._finish_approved(now, report, started)
        if self._clock() - started < TICK_BUDGET_SECONDS:
            self._advance_live(now, report, started)
        else:
            report.truncated = True
        self._write_report(now, report)
        return report

    def _failed(self, report: TickReport, ref: str, exc: Exception) -> None:
        report.errors += 1
        code = getattr(exc, "code", type(exc).__name__)
        report.failures.append({"ref": ref, "error": str(code)})
        log.warning("schedule tick: %s failed (%s)", ref, code)

    def _due(self, now: datetime) -> list[Mapping[str, Any]]:
        """§2.1: `state == enabled and next_run_at <= now order by next_run_at` (S4's index)."""
        query = (
            self.db.collection(schedules.COLLECTION)
            .where(filter=FieldFilter("state", "==", "enabled"))
            .where(filter=FieldFilter("next_run_at", "<=", now))
            .order_by("next_run_at")
            .limit(MAX_DUE_PER_TICK)
        )
        return [snap.to_dict() or {} for snap in query.stream()]

    # -- the kill switch (§2.11) --------------------------------------------

    def _switch(self, now: datetime, *, enabled: bool) -> list[Mapping[str, Any]]:
        """Off: open a disabled window if none is open, the one write a
        disabled tick makes. On: close an open one. Either way in a
        transaction, so overlapping ticks cannot lose a window; returns the
        recent windows for the skip codes."""
        ref = self._control_ref()

        @firestore.transactional
        def _apply(txn: Any) -> list[Mapping[str, Any]]:
            snap = _snapshot(txn.get(ref))
            windows = list((snap.to_dict() or {}).get("windows", [])) if snap.exists else []
            open_ = bool(windows) and windows[-1].get("to") is None
            if not enabled and not open_:
                windows.append({"from": now, "to": None})
                txn.set(ref, {"windows": windows[-10:]})
            elif enabled and open_:
                windows[-1] = {**windows[-1], "to": now}
                txn.set(ref, {"windows": windows[-10:]})
            return windows

        return _apply(self.db.transaction())

    # -- 1. claim (§2.2) ----------------------------------------------------

    def claim(self, schedule_id: str, now: datetime, windows: Sequence[Mapping[str, Any]],
              report: TickReport) -> str | None:
        """One transaction: re-read, plan the slots, create the firing, advance.

        Returns the claimed firing's id, or None when another tick won, the
        schedule is no longer due or enabled, or every due slot was skipped.
        """
        ref = self._schedule_ref(schedule_id)

        @firestore.transactional
        def _apply(txn: Any) -> tuple[str | None, int]:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return None, 0
            doc = snap.to_dict() or {}
            next_run_at = _aware(doc.get("next_run_at"))
            if doc.get("state") != "enabled" or next_run_at is None or next_run_at > now \
                    or doc.get("next_slot") is None:
                return None, 0
            plan = plan_slots(doc, now, windows)
            fire_id = firing_id(schedule_id, plan.fire) if plan.fire else None
            if fire_id is not None:
                # Firestore's create, inside the transaction: a firing that
                # exists is another tick's claim, and nothing is written.
                if _snapshot(txn.get(self._firing_ref(fire_id))).exists:
                    return None, 0
            # Reads before writes: a slot already holding a firing (a rewound
            # `next_slot` after an edit) is never overwritten.
            skips = [
                (slot, code, detail) for slot, code, detail in plan.skips
                if not _snapshot(txn.get(self._firing_ref(firing_id(schedule_id, slot)))).exists
            ]
            last: dict[str, Any] | None = None
            for slot, code, detail in skips:
                record = self._firing_doc(doc, slot, now, trigger="cron", state=SKIPPED)
                record["skip"] = {"code": code, "detail": detail}
                record["outcome"] = "skipped"
                record["ended_at"] = now
                txn.set(self._firing_ref(record["firing_id"]), record)
                last = _last_firing(record)
            if fire_id is not None:
                record = self._firing_doc(doc, plan.fire, now, trigger=plan.trigger, state=CLAIMED)
                txn.set(self._firing_ref(fire_id), record)
                last = _last_firing(record)
            next_slot, next_run = schedules.next_times(doc, now)
            update: dict[str, Any] = {"next_slot": next_slot, "next_run_at": next_run}
            if last is not None:
                update["last_firing"] = last
            txn.update(ref, update)
            return fire_id, len(skips)

        fid, skipped = _apply(self.db.transaction())
        report.skipped += skipped
        if fid:
            report.claimed += 1
        return fid

    def _firing_doc(self, schedule: Mapping[str, Any], slot: datetime | None, now: datetime, *,
                    trigger: str, state: str, fid: str | None = None) -> dict[str, Any]:
        fid = fid or firing_id(schedule["schedule_id"], slot)
        lateness = (now - slot).total_seconds() if slot is not None else 0.0
        return {
            "firing_id": fid,
            "schedule_id": schedule["schedule_id"],
            "tenant_id": schedule["tenant_id"],
            "type": schedule["type"],
            "slot": slot,
            "fired_at": now,
            "lateness_seconds": lateness,
            "trigger": trigger,
            "state": state,
            "params_digest": None,
            "work": [],
            "skip": None,
            "dry_run": None,
            "approval_id": None,
            "outcome": None,
            "cost": None,
            "history": [{"state": state, "at": now, "by": TICK_ACTOR}],
            "finishing_until": None,
            "ended_at": None,
            "expire_at": now + FIRING_TTL,
        }

    # -- 2. finish (§2.7) ---------------------------------------------------

    def finish(self, fid: str, now: datetime, report: TickReport, *, dry_run: bool = False) -> str | None:
        """Turn a claimed (or released queued, or approved) firing into work,
        or a skip, a refusal or a hold. Returns the state it moved to, or None
        when another finisher holds it, it is no longer claimed, or its
        approval does not release it."""
        firing = self._lease(fid)
        if firing is None:
            return None
        try:
            state = self._finish(firing, now, report, dry_run=dry_run or bool(firing.get("dry_run_requested")))
        except BaseException:
            self._release(fid)
            raise
        finally:
            self._leases.pop(fid, None)
        return state

    def _lease(self, fid: str) -> dict[str, Any] | None:
        """Hold a claimed firing for FINISH_LEASE from NOW, the wall clock.

        An `awaiting_approval` firing is leased too, but only when its
        `run_approval` releases it (`_approval_refusal`), checked in this
        transaction; one that does not stays held, and the refusal is audited
        once per approval and digest, never on every tick.
        """
        ref = self._firing_ref(fid)
        wall = _aware(self.ctx.now())
        token = uuid.uuid4().hex

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return None
            doc = snap.to_dict() or {}
            if doc.get("state") == AWAITING_APPROVAL and doc.get("run_approval"):
                refusal = self._approval_refusal(txn, doc)
                if refusal is not None:
                    self._audit_refusal(txn, ref, doc, wall, refusal)
                    return None
            elif doc.get("state") != CLAIMED:
                return None
            held = _aware(doc.get("finishing_until"))
            if held is not None and held > wall:
                return None
            txn.update(ref, {"finishing_until": wall + FINISH_LEASE, "finisher": token})
            return doc

        doc = _apply(self.db.transaction())
        if doc is not None:
            self._leases[fid] = token
        return doc

    def _approval_refusal(self, txn: Any, firing: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Why the firing's `run_approval` does not release it, or None when it does.

        It releases the firing only when its digest is the firing's
        `params_digest`, and the record it names is this firing's own: in the
        firing's tenant, a `run` approval, for this firing, named on the
        firing, decided `approved`, at the same digest. The field alone is
        never trusted: the record is the decision, written with its audit in
        the decision's transaction (`approvals.decide`).
        """
        # Imported here: `approvals` imports this module.
        from . import approvals

        approval = firing.get("run_approval") or {}
        approval_id = approval.get("approval_id")
        detail: dict[str, Any] = {"approval_id": approval_id, "digest": approval.get("digest"),
                                  "params_digest": firing.get("params_digest")}
        if not approval.get("digest") or approval.get("digest") != firing.get("params_digest"):
            return APPROVAL_CHANGED, detail
        if not approval_id or approval_id != firing.get("approval_id"):
            return APPROVAL_NOT_FOUND, detail
        snap = _snapshot(txn.get(self.db.collection(approvals.COLLECTION).document(str(approval_id))))
        record = (snap.to_dict() or {}) if snap.exists else {}
        if record.get("tenant_id") != firing.get("tenant_id") or record.get("kind") != approvals.RUN \
                or (record.get("subject") or {}).get("firing_id") != firing.get("firing_id"):
            return APPROVAL_NOT_FOUND, detail
        if record.get("state") != approvals.APPROVED:
            return APPROVAL_NOT_APPROVED, {**detail, "state": record.get("state")}
        if record.get("digest") != approval.get("digest"):
            return APPROVAL_CHANGED, detail
        return None

    def _audit_refusal(self, txn: Any, ref: Any, firing: Mapping[str, Any], now: datetime,
                       refusal: tuple[str, Mapping[str, Any]]) -> None:
        """The held firing's audit entry and its `run_approval_refused`, once per refusal."""
        code, detail = refusal
        marker = {"code": code, "approval_id": detail.get("approval_id"), "digest": detail.get("digest"),
                  "params_digest": detail.get("params_digest")}
        seen = dict(firing.get("run_approval_refused") or {})
        seen.pop("at", None)
        if seen == marker:
            return
        schedaudit.append(
            txn, self.db, schedule_id=str(firing.get("schedule_id")), tenant_id=str(firing.get("tenant_id")),
            action=RUN_APPROVAL_REFUSED, by=TICK_ACTOR, at=now,
            detail={"firing_id": firing.get("firing_id"), **dict(detail), "code": code},
        )
        txn.update(ref, {"run_approval_refused": {**marker, "at": now}})
        log.warning("schedule firing %s: approval does not release it (%s)", firing.get("firing_id"), code)

    def _hold_approved(self, fid: str, now: datetime, report: TickReport,
                       refusal: tuple[str, Mapping[str, Any]]) -> str:
        """An approved firing whose digest at finish is not the one approved:
        it stays `awaiting_approval`, the lease is given back, and it is audited."""
        ref = self._firing_ref(fid)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            doc = _snapshot(txn.get(ref)).to_dict() or {}
            if not self._holds(fid, doc):
                raise Conflict(f"firing {fid} is held by another finisher")
            self._audit_refusal(txn, ref, doc, now, refusal)
            txn.update(ref, {"finishing_until": None, "finisher": None})

        _apply(self.db.transaction())
        report.held += 1
        return AWAITING_APPROVAL

    def _holds(self, fid: str, doc: Mapping[str, Any]) -> bool:
        token = self._leases.get(fid)
        return token is not None and doc.get("finisher") == token

    def _release(self, fid: str) -> None:
        """Give the lease back, only if it is still this finisher's."""
        ref = self._firing_ref(fid)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            doc = _snapshot(txn.get(ref)).to_dict() or {}
            if self._holds(fid, doc):
                txn.update(ref, {"finishing_until": None, "finisher": None})

        try:
            _apply(self.db.transaction())
        except Exception:  # pragma: no cover - the lease lapses by itself
            log.warning("schedule firing %s: lease release failed", fid)

    def _record_item(self, fid: str) -> Callable[[dict[str, Any]], None]:
        """`Firing.record`: append one item to the firing's `recorded`, under the lease."""
        ref = self._firing_ref(fid)

        def record(item: dict[str, Any]) -> None:
            @firestore.transactional
            def _apply(txn: Any) -> None:
                doc = _snapshot(txn.get(ref)).to_dict() or {}
                if not self._holds(fid, doc):
                    raise Conflict(f"firing {fid} is no longer this finisher's")
                txn.update(ref, {"recorded": list(doc.get("recorded") or []) + [dict(item)]})

            _apply(self.db.transaction())

        return record

    def _finish(self, firing: Mapping[str, Any], now: datetime, report: TickReport, *, dry_run: bool) -> str | None:
        fid = firing["firing_id"]
        snap = self._schedule_ref(firing["schedule_id"]).get()
        schedule = snap.to_dict() if snap.exists else None
        if schedule is None or schedule.get("tenant_id") != firing.get("tenant_id"):
            return self._end(fid, now, report, state=SKIPPED, code=SCHEDULE_NOT_ENABLED,
                             detail={"reason": "the schedule was deleted"}, schedule=None)
        runnable = ("enabled", "paused", "auto_paused") if firing.get("trigger") == "run_now" else ("enabled",)
        if schedule.get("state") not in runnable:
            return self._end(fid, now, report, state=SKIPPED, code=SCHEDULE_NOT_ENABLED,
                             detail={"state": schedule.get("state")}, schedule=schedule)
        entry = scheduletypes.get(str(schedule.get("type")))
        executor = self._executors.get(str(schedule.get("type"))) if entry else None
        if entry is not None and executor is None:
            executor = load_executor(entry)
        if entry is None or executor is None:
            self._end(fid, now, report, state=REFUSED, code=TYPE_UNAVAILABLE,
                      detail={"type": schedule.get("type")}, schedule=schedule)
            self.pause(schedule["schedule_id"], TYPE_UNAVAILABLE, now, state="disabled")
            return REFUSED
        # As whom (§2.7). A lookup that fails propagates: the firing stays
        # claimed, and the next tick asks again.
        try:
            owner = schedule_owner_auth(self.ctx, schedule)
        except ScheduleOwnerNotMember:
            self._end(fid, now, report, state=SKIPPED, code=OWNER_NOT_MEMBER,
                      detail={"owner": schedule.get("owner")}, schedule=schedule)
            self.pause(schedule["schedule_id"], OWNER_NOT_MEMBER, now)
            return SKIPPED
        # Work already carrying this firing's mark (§2.2): adopted, never
        # created twice.
        adopted = self._adopt(schedule, fid)
        if adopted:
            return self._created(fid, now, report, adopted, schedule, digest=firing.get("params_digest"))
        # Overlap (§2.5): first bring the schedule's live firings up to date,
        # so one that just ended is not counted live.
        live = self._live_of(schedule, exclude=fid, now=now, report=report)
        if live and firing.get("trigger") != "queued":
            policy = (schedule.get("policy") or {}).get("overlap", "skip")
            queued = self._queued_of(schedule["schedule_id"], exclude=fid)
            if policy == "queue_one" and not queued:
                return self._move(fid, now, report, QUEUED, {"skip": None})
            return self._end(fid, now, report, state=SKIPPED, code=OVERLAP,
                             detail={"live_firing": live[0]["firing_id"]}, schedule=schedule)
        # Scope at firing time (§1.1, §4.4).
        repo_ids, missing = self._scope(schedule)
        if schedule["scope"]["mode"] != "platform" and not repo_ids:
            return self._refused(fid, now, report, schedule, REPOSITORY_NOT_GRANTED,
                                 {"repo_ids": missing})
        digest = params_digest(schedule, repo_ids)
        # An approved firing runs only what was approved: the scope or
        # parameters it would run with now must give the digest approved.
        approved = firing.get("state") == AWAITING_APPROVAL
        if approved and digest != (firing.get("run_approval") or {}).get("digest"):
            return self._hold_approved(fid, now, report, (APPROVAL_CHANGED, {
                "approval_id": (firing.get("run_approval") or {}).get("approval_id"),
                "digest": (firing.get("run_approval") or {}).get("digest"),
                "params_digest": digest,
            }))
        # Budget (§4.3): a firing gate, never an admission control.
        budget = schedule["budget"]
        spend = schedules.spend_for(schedule.get("spend"), schedules.spend_day(now, schedule["timezone"]))
        live_items = sum(len(f.get("work") or []) for f in live)
        if schedules.budget_exhausted(
            budget,
            reported_today=float(spend.get("reported_usd") or 0.0),
            live_items=live_items,
            unreported_attempts=int(spend.get("unreported_attempts") or 0),
        ):
            try:
                refusals.refuse(BudgetExhausted(
                    f"schedule {schedule['schedule_id']} has spent its per_day_usd for {spend['day']}"
                ))
            except BudgetExhausted:
                return self._end(fid, now, report, state=SKIPPED, code=BUDGET_EXHAUSTED,
                                 detail={"day": spend["day"], "per_day_usd": budget["per_day_usd"]},
                                 schedule=schedule)
        room = schedules.concurrency_room(budget, live_items=live_items)
        handle = Firing(ctx=self.ctx, schedule=schedule, firing=firing, owner=owner,
                        repo_ids=repo_ids, room=room, now=now, record=self._record_item(fid))
        detail_missing = {"skipped_repos": [{"repo_id": r, "code": REPOSITORY_NOT_GRANTED} for r in missing]}
        if dry_run or (schedule.get("policy") or {}).get("dry_run"):
            output = executor.dry_run(handle)
            return self._end(fid, now, report, state=SKIPPED, code=DRY_RUN, detail=detail_missing,
                             schedule=schedule, extra={"dry_run": output, "params_digest": digest})
        if (schedule.get("gate") or {}).get("run") == "approve" and not approved:
            # §1.4: held as a document and nothing else. The approval record
            # and its inbox are lane S5's; nothing is submitted here.
            report.held += 1
            return self._move(fid, now, report, AWAITING_APPROVAL, {"params_digest": digest})
        try:
            work = executor.create(handle) or []
        except ApiError as exc:
            if exc.status_code >= 500:
                raise
            # A submission refusal: WORKSPACE_NOT_READY, NO_CLAUDE_ACCOUNT,
            # REPOSITORY_NOT_GRANTED, a validation failure (§2.7). Work an
            # earlier submission of this firing already made is recorded, not
            # orphaned: it is ordinary live work, found by its mark.
            made = self._adopt(schedule, fid)
            if made:
                return self._created(fid, now, report, made, schedule, digest=digest,
                                     extra={**detail_missing, "refusal": {"code": exc.code}})
            return self._refused(fid, now, report, schedule, exc.code,
                                 {"message": exc.message, **detail_missing})
        return self._created(fid, now, report, work, schedule, digest=digest, extra=detail_missing)

    def _scope(self, schedule: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        """(registered now, no longer registered). `all` lists every registration NOW."""
        scope = schedule["scope"]
        tenant_id = schedule["tenant_id"]
        repos = Repositories(self.db, now=self.ctx.now)
        if scope["mode"] == "repos":
            wanted = list(scope.get("repo_ids") or [])
            found = repos.registered(tenant_id, wanted)
            return [r for r in wanted if r in found], [r for r in wanted if r not in found]
        if scope["mode"] == "all":
            rows: list[str] = []
            token: str | None = None
            while True:
                page, token = repos.list(tenant_id, limit=100, page_token=token)
                rows.extend(str(row["repo_id"]) for row in page)
                if not token:
                    return rows, []
        return [], []

    def _adopt(self, schedule: Mapping[str, Any], fid: str) -> list[dict[str, Any]]:
        """Work this firing already made (§2.2): items it recorded (issue runs,
        API actions) and tasks carrying its mark, in THIS tenant."""
        snap = self._firing_ref(fid).get()
        work: list[dict[str, Any]] = list((snap.to_dict() or {}).get("recorded") or []) if snap.exists else []
        tasks = self._marked_tasks(schedule["tenant_id"], fid)
        seen: set[str] = set()
        for task in tasks:
            if task.get("workflow_id"):
                if task["workflow_id"] not in seen:
                    seen.add(task["workflow_id"])
                    work.append({"kind": "workflow", "id": task["workflow_id"], "repo_id": None})
            else:
                work.append({"kind": "task", "id": task["id"], "repo_id": None})
        return work

    def _marked_tasks(self, tenant_id: str, fid: str) -> list[dict[str, Any]]:
        query = (
            self.db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter(f"metadata.{SCHEDULE_METADATA_KEY}.firing_id", "==", fid))
        )
        rows = [snap.to_dict() or {} for snap in query.stream()]
        # The filter again, in the application: a fake or an index mistake
        # must not move the tenant boundary.
        return [r for r in rows if r.get("tenant_id") == tenant_id
                and ((r.get("metadata") or {}).get(SCHEDULE_METADATA_KEY) or {}).get("firing_id") == fid]

    def _of_schedule(self, schedule_id: str, states: Sequence[str]) -> list[dict[str, Any]]:
        query = (
            self.db.collection(FIRINGS)
            .where(filter=FieldFilter("schedule_id", "==", schedule_id))
            .where(filter=FieldFilter("state", "in", list(states)))
            .limit(SCAN_PAGE)
        )
        return [snap.to_dict() or {} for snap in query.stream()]

    def _live_of(self, schedule: Mapping[str, Any], *, exclude: str, now: datetime,
                 report: TickReport) -> list[dict[str, Any]]:
        live = []
        for other in self._of_schedule(schedule["schedule_id"], [CREATED]):
            if other.get("firing_id") == exclude:
                continue
            if self.advance(other, now, report, release_queued=False) is None:
                live.append(other)
        return live

    def _queued_of(self, schedule_id: str, *, exclude: str) -> list[dict[str, Any]]:
        return [f for f in self._of_schedule(schedule_id, [QUEUED]) if f.get("firing_id") != exclude]

    # -- moving a firing ----------------------------------------------------

    def _move(self, fid: str, now: datetime, report: TickReport, state: str,
              patch: Mapping[str, Any]) -> str:
        """claimed -> `state`, only from claimed, or from an approved
        `awaiting_approval` this finisher leased; the history says when and by whom."""
        ref = self._firing_ref(fid)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = snap.to_dict() or {}
            approved = doc.get("state") == AWAITING_APPROVAL and bool(doc.get("run_approval"))
            if doc.get("state") != CLAIMED and not approved:
                raise Conflict(f"firing {fid} is {doc.get('state')}, not claimed")
            if not self._holds(fid, doc):
                raise Conflict(f"firing {fid} is held by another finisher")
            history = list(doc.get("history") or []) + [{"state": state, "at": now, "by": TICK_ACTOR}]
            txn.update(ref, {**patch, "state": state, "history": history,
                             "finishing_until": None, "finisher": None})

        _apply(self.db.transaction())
        if state == QUEUED:
            report.queued += 1
        return state

    def _end(self, fid: str, now: datetime, report: TickReport, *, state: str, code: str,
             detail: Mapping[str, Any], schedule: Mapping[str, Any] | None,
             extra: Mapping[str, Any] | None = None) -> str:
        outcome = "skipped" if state == SKIPPED else "refused"
        self._move(fid, now, report, state, {
            "skip": {"code": code, "detail": dict(detail)},
            "outcome": outcome,
            "ended_at": now,
            **(extra or {}),
        })
        if state == SKIPPED:
            report.skipped += 1
        else:
            report.refused += 1
        if schedule is not None:
            self._record_last(schedule["schedule_id"], fid, now, outcome=outcome,
                              failed=state == REFUSED)
        return state

    def _refused(self, fid: str, now: datetime, report: TickReport, schedule: Mapping[str, Any],
                 code: str, detail: Mapping[str, Any]) -> str:
        return self._end(fid, now, report, state=REFUSED, code=code, detail=detail, schedule=schedule)

    def _created(self, fid: str, now: datetime, report: TickReport, work: list[dict[str, Any]],
                 schedule: Mapping[str, Any], *, digest: str | None,
                 extra: Mapping[str, Any] | None = None) -> str:
        patch: dict[str, Any] = {"work": work, "params_digest": digest}
        if extra and extra.get("refusal"):
            # Part of the work was made and then a submission was refused.
            patch["skip"] = {"code": extra["refusal"]["code"], "detail": dict(extra)}
        elif extra and extra.get("skipped_repos"):
            patch["skip"] = {"code": REPOSITORY_NOT_GRANTED, "detail": dict(extra)}
        self._move(fid, now, report, CREATED, patch)
        report.fired += 1
        snap = self._firing_ref(fid).get()
        firing = snap.to_dict() or {}
        report.lateness.append({"firing_id": fid, "seconds": firing.get("lateness_seconds")})
        self._record_last(schedule["schedule_id"], fid, now, outcome=None, failed=None,
                          work_ref=work[0] if work else None)
        # Work that ended before it was recorded (or none at all) ends now.
        self.advance(firing, now, report)
        return CREATED

    # -- the schedule document ---------------------------------------------

    def _record_last(self, schedule_id: str, fid: str, now: datetime, *, outcome: str | None,
                     failed: bool | None, work_ref: Mapping[str, Any] | None = None,
                     cost: Mapping[str, Any] | None = None, day_tz: str | None = None) -> None:
        """`last_firing`, `consecutive_failures` and the day's spend, in one transaction."""
        ref = self._schedule_ref(schedule_id)
        pause_after: list[int] = []

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return
            doc = snap.to_dict() or {}
            fsnap = _snapshot(txn.get(self._firing_ref(fid)))
            firing = fsnap.to_dict() or {}
            previous = doc.get("last_firing") or {}
            if work_ref is None and previous.get("firing_id") == fid:
                work_ref_now = previous.get("work_ref")
            else:
                work_ref_now = dict(work_ref) if work_ref else None
            update: dict[str, Any] = {
                "last_firing": {
                    "firing_id": fid,
                    "slot": firing.get("slot"),
                    "outcome": outcome,
                    "state": firing.get("state"),
                    "work_ref": work_ref_now,
                    "ended_at": now if outcome is not None else None,
                }
            }
            if failed is True:
                update["consecutive_failures"] = int(doc.get("consecutive_failures") or 0) + 1
                if update["consecutive_failures"] >= failures_to_pause(doc):
                    pause_after.append(update["consecutive_failures"])
            elif failed is False:
                update["consecutive_failures"] = 0
            if cost is not None:
                spend = schedules.spend_for(doc.get("spend"), schedules.spend_day(now, doc["timezone"]))
                spend["reported_usd"] = round(float(spend.get("reported_usd") or 0.0)
                                              + float(cost.get("reported_usd") or 0.0), 6)
                spend["unreported_attempts"] = int(spend.get("unreported_attempts") or 0) \
                    + int(cost.get("unreported_attempts") or 0)
                update["spend"] = spend
            txn.update(ref, update)

        _apply(self.db.transaction())
        if pause_after:
            try:
                refusals.refuse(ConsecutiveFailures(
                    f"schedule {schedule_id} failed {pause_after[0]} times in a row"
                ))
            except ConsecutiveFailures:
                self.pause(schedule_id, CONSECUTIVE_FAILURES, now, n=pause_after[0])

    def pause(self, schedule_id: str, code: str, now: datetime, *, state: str = "auto_paused",
              n: int | None = None) -> bool:
        """§1.3: `auto_paused` (or `disabled`) with its code. Only an ENABLED
        schedule moves: a person's pause is not overwritten by the tick's."""
        ref = self._schedule_ref(schedule_id)
        reason = PAUSE_COPY[code].format(n=n if n is not None else CONSECUTIVE_FAILURES_DEFAULT)

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return False
            doc = snap.to_dict() or {}
            if doc.get("state") != "enabled":
                return False
            txn.update(ref, {
                "state": state,
                "pause": {"by": TICK_ACTOR, "at": now, "reason": reason, "code": code},
                "next_run_at": None,
                "updated_by": TICK_ACTOR,
                "updated_at": now,
            })
            return True

        moved = _apply(self.db.transaction())
        if moved:
            log.warning("schedule %s %s: %s", schedule_id, state, code)
        return moved

    # -- 3. advance (§2.9) --------------------------------------------------

    def advance(self, firing: Mapping[str, Any], now: datetime, report: TickReport, *,
                release_queued: bool = True) -> str | None:
        """End a `created` firing whose work has all ended. Returns its outcome,
        or None while any of its work is live."""
        if firing.get("state") != CREATED:
            return firing.get("outcome")
        tenant_id = firing["tenant_id"]
        work = list(firing.get("work") or [])
        tasks = self._marked_tasks(tenant_id, firing["firing_id"])
        by_workflow: dict[str, list[dict[str, Any]]] = {}
        by_id = {t.get("id"): t for t in tasks}
        for task in tasks:
            if task.get("workflow_id"):
                by_workflow.setdefault(task["workflow_id"], []).append(task)
        outcomes: list[str] = []
        per_item_tasks: list[list[str]] = []
        for item in work:
            kind = item.get("kind")
            if kind == "api_action":
                outcomes.append(str(item.get("outcome") or "failed"))
                per_item_tasks.append([])
                continue
            if kind == "issue_run":
                outcome = self._issue_run_outcome(tenant_id, str(item.get("id")))
                if outcome is None:
                    return None
                outcomes.append(outcome)
                per_item_tasks.append([])
                continue
            members = by_workflow.get(item.get("id"), []) if kind == "workflow" else \
                [by_id[item.get("id")]] if item.get("id") in by_id else []
            if not members:
                # Not found under this tenant with this firing's mark: never
                # read as done. It stays live until it can be read.
                return None
            states = [task_outcome(str(m.get("state"))) for m in members]
            if any(s is None for s in states):
                return None
            outcomes.append(combine([s for s in states if s], independent=False))
            per_item_tasks.append([str(m.get("id")) for m in members])
        entry = scheduletypes.get(str(firing.get("type")))
        independent = bool(entry and entry.executor == "issue_runs") or len(work) > 1
        outcome = combine(outcomes, independent=independent)
        cost = self._cost(tenant_id, per_item_tasks)
        snap = self._schedule_ref(firing["schedule_id"]).get()
        schedule = snap.to_dict() if snap.exists else None
        over = False
        code = None
        if schedule is not None and any(
            schedules.run_over_budget(schedule["budget"], c) for c in cost["per_item"]
        ):
            try:
                refusals.refuse(RunOverBudget(
                    f"a run of schedule {schedule['schedule_id']} cost more than its per_run_usd"
                ))
            except RunOverBudget:
                over = True
                outcome, code = "failed", RUN_OVER_BUDGET
        if not self._done(firing["firing_id"], now, outcome, cost, code):
            return outcome
        report.advanced += 1
        if schedule is not None:
            self._record_last(schedule["schedule_id"], firing["firing_id"], now, outcome=outcome,
                              failed=outcome == "failed", cost=cost)
            if over:
                self.pause(schedule["schedule_id"], RUN_OVER_BUDGET, now)
            if release_queued:
                self._release_queued(schedule["schedule_id"], now, report)
        return outcome

    def _issue_run_outcome(self, tenant_id: str, run_id: str) -> str | None:
        """Advances the run first (§2.9: the tick reaches every tenant's runs)."""
        from .routes.runs import advance_run  # the route module imports this one's deps

        try:
            run = IssueRuns(self.db, now=self.ctx.now).get(tenant_id, run_id)
        except NotFound:
            return "failed"
        if run.state not in TERMINAL_RUN_STATES:
            run = advance_run(self.ctx, tenant_id, run)
        return run_outcome(run.state.value if hasattr(run.state, "value") else str(run.state))

    def _cost(self, tenant_id: str, per_item_tasks: list[list[str]]) -> dict[str, Any]:
        """§4.3: reported cost summed; an attempt with no cost is counted, not zero."""
        every = [t for item in per_item_tasks for t in item]
        totals = totals_for(self.db, tenant_id, every) if every else {}
        reported = 0.0
        unreported = 0
        per_item: list[float | None] = []
        for item in per_item_tasks:
            item_cost = 0.0
            known = False
            for task_id in item:
                row = totals.get(task_id) or {}
                if row.get("attempts_read") != "ok":
                    unreported += 1
                    continue
                if row.get("cost_usd_total") is not None:
                    item_cost += float(row["cost_usd_total"])
                    known = True
                unreported += int(row.get("attempts") or 0) - int(row.get("attempts_with_cost") or 0)
            reported += item_cost
            per_item.append(round(item_cost, 6) if known else None)
        return {"reported_usd": round(reported, 6), "unreported_attempts": unreported, "per_item": per_item}

    def _done(self, fid: str, now: datetime, outcome: str, cost: Mapping[str, Any], code: str | None) -> bool:
        ref = self._firing_ref(fid)

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            doc = snap.to_dict() or {}
            if doc.get("state") != CREATED:
                return False
            history = list(doc.get("history") or []) + [{"state": DONE, "at": now, "by": TICK_ACTOR}]
            patch: dict[str, Any] = {
                "state": DONE,
                "outcome": outcome,
                "cost": {"reported_usd": cost["reported_usd"], "unreported_attempts": cost["unreported_attempts"]},
                "ended_at": now,
                "history": history,
            }
            if code:
                patch["skip"] = {"code": code, "detail": {"per_item_usd": list(cost["per_item"])}}
            txn.update(ref, patch)
            return True

        return _apply(self.db.transaction())

    def _release_queued(self, schedule_id: str, now: datetime, report: TickReport) -> None:
        """§2.5: the one queued firing is created as the live one ends."""
        if self._of_schedule(schedule_id, [CREATED]):
            return
        for queued in self._of_schedule(schedule_id, [QUEUED])[:1]:
            ref = self._firing_ref(queued["firing_id"])

            @firestore.transactional
            def _apply(txn: Any) -> bool:
                snap = _snapshot(txn.get(ref))
                doc = snap.to_dict() or {}
                if doc.get("state") != QUEUED:
                    return False
                history = list(doc.get("history") or []) + [{"state": CLAIMED, "at": now, "by": TICK_ACTOR}]
                txn.update(ref, {"state": CLAIMED, "trigger": "queued", "history": history,
                                 "finishing_until": None, "finisher": None})
                return True

            if _apply(self.db.transaction()):
                self.finish(queued["firing_id"], now, report)

    # -- the tick's other pages ---------------------------------------------

    def _by_state(self, state: str) -> list[dict[str, Any]]:
        """An equality filter only, so no composite index is needed; the
        oldest are sorted here from up to SCAN_PAGE rows."""
        query = self.db.collection(FIRINGS).where(filter=FieldFilter("state", "==", state)).limit(SCAN_PAGE)
        rows = [snap.to_dict() or {} for snap in query.stream()]
        rows.sort(key=lambda r: _aware(r.get("fired_at")) or datetime.min.replace(tzinfo=timezone.utc))
        return rows

    def _finish_stale(self, now: datetime, report: TickReport, started: float) -> None:
        """§2.2: claimed firings older than 2 minutes, finished by this tick."""
        for firing in self._by_state(CLAIMED)[:ADVANCE_PAGE]:
            if self._clock() - started >= TICK_BUDGET_SECONDS:
                report.truncated = True
                return
            fired_at = _aware(firing.get("fired_at"))
            if fired_at is None or now - fired_at < CLAIM_STALE:
                continue
            try:
                self.finish(firing["firing_id"], now, report)
            except Exception as exc:
                self._failed(report, firing["firing_id"], exc)

    def _finish_approved(self, now: datetime, report: TickReport, started: float) -> None:
        """`awaiting_approval` firings that carry a `run_approval`, finished
        by this tick under the gate they were claimed with. An unapproved one
        is not touched; `_lease` decides whether the approval releases it."""
        for firing in self._by_state(AWAITING_APPROVAL)[:ADVANCE_PAGE]:
            if not firing.get("run_approval"):
                continue
            if self._clock() - started >= TICK_BUDGET_SECONDS:
                report.truncated = True
                return
            try:
                self.finish(firing["firing_id"], now, report)
            except Exception as exc:
                self._failed(report, firing["firing_id"], exc)

    def _advance_live(self, now: datetime, report: TickReport, started: float) -> None:
        for firing in self._by_state(CREATED)[:ADVANCE_PAGE]:
            if self._clock() - started >= TICK_BUDGET_SECONDS:
                report.truncated = True
                return
            try:
                self.advance(firing, now, report)
            except Exception as exc:
                self._failed(report, firing["firing_id"], exc)

    def _write_report(self, now: datetime, report: TickReport) -> None:
        """§2.10: `schedule_ticks/{unix_minute}`, 7 days, for the admin view's tick health."""
        try:
            self.db.collection(TICKS).document(str(unix_minute(now))).set({
                **report.to_api(),
                "started_at": now,
                "lateness": report.lateness,
                "expire_at": now + TICK_TTL,
            })
        except Exception as exc:  # the report is not the work
            log.warning("schedule tick report not written (%s)", type(exc).__name__)

    # -- run now (§7.1 `:run`, the S3 route calls it) ------------------------

    def fire_now(self, schedule: Mapping[str, Any], *, actor: str, dry_run: bool = False) -> dict[str, Any]:
        """A `run_now` firing, keyed `{schedule_id}:run_now:{uuid}`, under the
        same finish as a slot's: the owner's identity, the overlap, the
        budget and the run gate. The caller has checked the tenant."""
        now = _aware(self.ctx.now())
        report = TickReport(started_at=now)
        fid = f"{schedule['schedule_id']}:run_now:{uuid.uuid4().hex}"
        record = self._firing_doc(schedule, None, now, trigger="run_now", state=CLAIMED, fid=fid)
        record["history"] = [{"state": CLAIMED, "at": now, "by": actor}]
        record["dry_run_requested"] = bool(dry_run)
        self._firing_ref(fid).set(record)
        self.finish(fid, now, report, dry_run=dry_run)
        snap = self._firing_ref(fid).get()
        return snap.to_dict() or {}


def _last_firing(record: Mapping[str, Any]) -> dict[str, Any]:
    """The copy on the schedule for the list screen (§1.1)."""
    return {
        "firing_id": record["firing_id"],
        "slot": record.get("slot"),
        "outcome": record.get("outcome"),
        "state": record.get("state"),
        "work_ref": None,
        "ended_at": record.get("ended_at"),
    }


def run_tick(ctx: Any, *, executors: Mapping[str, Executor] | None = None,
             clock: Callable[[], float] = time.monotonic,
             environ: Mapping[str, str] | None = None) -> TickReport:
    """`POST /v1/admin/schedules/tick`'s body."""
    return Ticker(ctx, executors=executors, clock=clock, environ=environ).tick()


__all__ = [
    "BudgetExhausted",
    "ConsecutiveFailures",
    "Executor",
    "Firing",
    "RunOverBudget",
    "ScheduleOwnerNotMember",
    "TickReport",
    "Ticker",
    "UpstreamUnavailable",
    "combine",
    "fair_pick",
    "firing_id",
    "load_executor",
    "member_auth",
    "params_digest",
    "plan_slots",
    "run_tick",
    "schedule_owner_auth",
    "schedules_enabled",
]
