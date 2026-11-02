"""The bounded drain loop.

Shape, and why it is this shape:

    woken by Pub/Sub
      (a `task_finished` wake runs `release_dependants` instead: that task's
       dependants, then one admission pass -- see there)
      -> promote what has become runnable (dependencies, credentials, prewarm),
         and cancel what a failed `fail_workflow` workflow will never run
      -> while admissible work exists and the run is within budget:
           read a slice of READY work
           interleave it round-robin across tenants
           try to admit each, in order
      -> exit

It EXITS. It does not sit in a loop waiting for capacity, because a scheduler
that waits is a scheduler that is billed for waiting and that gets killed
mid-transaction when the platform reclaims it. Whatever it could not admit stays
READY in Firestore, costing nothing, and the next push -- or the 1-minute Cloud
Scheduler safety tick -- picks it up.

AdmissionDenied is NOT an error and never stops the loop. It is written to
`task.blocked_by` so the caller can see why, and the loop moves to the next
task -- which will usually belong to a different tenant, and will usually be
admissible. Breaking out on the first denial is how one full pool ends up
stalling every other tenant's work.

Neither is a contended admission transaction (#1822). An `Aborted` from the
lease transaction, or the `ValueError` `firestore.transactional` raises once
every attempt was aborted, skips that one task for this pass after a short
jittered pause; it is counted as `contended`, and the drain goes on and answers
200. Only those two: see `_is_contention`.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.base_query import FieldFilter

from quota_broker.accounts import (
    ASSIGNABLE_STATES,
    HOLD_LOG_COLLECTION,
    Account,
    Unavailable,
    accounts_serving,
    choose,
    duplicates,
    eligibility,
)
from swarm_common.admission import AdmissionConfig, AdmissionDenied
from swarm_common.models import (
    PLATFORM_REFUSED_MAX_PARKS,
    EndCause,
    Lease,
    Task,
    Tenant,
    pool_names_for,
    utcnow,
)
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend
from swarm_common.states import (
    PENDING_STATES,
    TERMINAL_STATES,
    EventType,
    ParkReason,
    TaskState,
    can_transition,
)

from . import children as children_mod
from . import reviewgate as reviewgate_mod
from . import runtime as runtime_mod
from . import sizes as sizes_mod
from .awaitinput import is_control_plane
from .codec import as_datetime
from .credentials import (
    SUBSCRIPTION_TOKEN_ENV,
    AccountPool,
    CredentialAnswer,
    CredentialSource,
    ForgeConnections,
    credential_for,
    task_model_class,
)
from .mcp_admission import (
    MCP_CREDENTIAL,
    MCP_CREDENTIAL_PARK_MAX_SECONDS,
    McpCode,
    credential_block,
)
from .secret_slots import WorkspaceGrants
from .dispatch import PLATFORM_REFUSAL_CODES, BackendRouter, DispatchError, live_display_on
from .fairness import AgingConfig, round_robin_order
from .metrics import SchedulerMetrics
from .settings import SchedulerSettings
from .store import GuardedWrite, ParentEnd, SchedulerStore

log = logging.getLogger(__name__)

#: Who switched a tenant off (WAIT-PAUSER, part of #1782): swarm-api's
#: `PUT /v1/admin/tenants/{id}/limits` appends one `admin_audit` row per change
#: of `enabled` (`swarm_api.store.Store._switch_tenant`). Restated because the
#: scheduler image does not carry swarm-api; held equal by
#: tests/unit/control_plane/test_wait_pauser.py.
ADMIN_AUDIT = "admin_audit"
AUDIT_TENANT_DISABLED = "tenant_disabled"
AUDIT_TENANT_ENABLED = "tenant_enabled"
#: Rows read per action. A tenant is switched a handful of times in its life;
#: past this many the newest cannot be told from an arbitrary page (the query
#: has no order: two equalities need no composite index, an order would), so
#: the park names nobody rather than the wrong person.
TENANT_SWITCH_READ_CAP = 200


def tenant_disabled_by(db: Any, tenant: Tenant) -> dict[str, str]:
    """`{paused_by, paused_at}` for a tenant a person disabled, or `{}`.

    A person is named only when the tenant's NEWEST switch row is a
    `tenant_disabled` written since this tenant document was created. No row
    (disabled by a direct Firestore write, by the runbook's fallback, or
    before the record existed), a newer `tenant_enabled` (so a later
    unrecorded disable is not credited to the earlier admin), a row older
    than a re-registered tenant, too many rows to order, or a read that
    fails: `{}`, and `waiting_detail` serves `paused_by` null with the park's
    error. A principal is never invented.
    """
    try:
        return _newest_disable(db, tenant)
    except Exception as exc:  # noqa: BLE001 -- the park is written either way
        log.warning("tenant switch record unreadable tenant=%s error=%s",
                    tenant.tenant_id, type(exc).__name__)
        return {}


def _newest_disable(db: Any, tenant: Tenant) -> dict[str, str]:
    rows: list[dict[str, Any]] = []
    for action in (AUDIT_TENANT_DISABLED, AUDIT_TENANT_ENABLED):
        query = (
            db.collection(ADMIN_AUDIT)
            .where(filter=FieldFilter("target_tenant_id", "==", tenant.tenant_id))
            .where(filter=FieldFilter("action", "==", action))
            .limit(TENANT_SWITCH_READ_CAP)
        )
        page = [snap.to_dict() or {} for snap in query.stream()]
        if len(page) >= TENANT_SWITCH_READ_CAP:
            return {}
        rows.extend(page)
    dated = [(at, row) for row in rows if (at := as_datetime(row.get("at"))) is not None]
    if not dated:
        return {}
    at, newest = max(dated, key=lambda pair: pair[0])
    by = newest.get("by")
    if newest.get("action") != AUDIT_TENANT_DISABLED or not isinstance(by, str) or not by:
        return {}
    created = as_datetime(tenant.created_at)
    if created is not None and at < created:
        return {}
    return {"paused_by": by, "paused_at": at.isoformat()}


#: A merge step's CI wait (lane MS2, docs/merge-step.md "Revised 2026-10-06"
#: §1): `metadata.merge_wait`, written by the worker's
#: `control.ControlPlane.park_ci_pending`, and inside it the marker swarm-api's
#: wake tick (`swarm_api.mergewake`) writes once the checks have settled.
#: Restated because the scheduler image carries neither; held equal by
#: tests/unit/worker/test_merge_action.py.
MERGE_WAIT_METADATA_KEY = "merge_wait"
MERGE_WAKE_MARKER = "wake_requested_at"
#: The wake tick's record of a merge step's owner-question hold, inside
#: `merge_wait` (MERGE-OUTSIDE-ENDS, part of #2441): the park instant it read
#: (`parked_at`) and, once the hold's pull request was merged or closed
#: outside the workflow, the marker. `swarm_api.mergewake.OWNER_HOLD_KEY`,
#: held equal by tests/unit/control_plane/test_merge_outside_ends.py.
OWNER_HOLD_KEY = "owner_hold"

#: A MERGE verdict's integrator, published by swarm-api without a worker
#: (#748, `swarm_api.verdictpublish`): the marker it writes on the step, its
#: `claimed` state, and how long a claim is honoured. Restated because the
#: scheduler image does not carry swarm-api; held equal by
#: tests/unit/control_plane/test_verdict_publish.py.
CONTROL_PUBLISH_METADATA_KEY = "control_publish"
CONTROL_PUBLISH_CLAIMED = "claimed"
CONTROL_PUBLISH_CLAIM_SECONDS = 300

#: A workflow that waits for named pull requests to merge (lane SPEED-D3,
#: `swarm_api.prgate`): the gate on each of its root steps, PARKED on
#: DEPENDENCY_INCOMPLETE, and the two states that end the wait. Restated
#: because the scheduler image does not carry swarm-api; held equal by
#: tests/unit/control_plane/test_workflow_after_pr.py.
AFTER_PRS_METADATA_KEY = "after_prs"
AFTER_PRS_MERGED = "merged"
AFTER_PRS_CLOSED = "closed"


def after_prs_hold(metadata: Any) -> tuple[str, int | None] | None:
    """What a step's pull-request gate says: None when there is none to hold on.

    `("merged", None)`: every pull request merged; the step is promoted as any
    dependency park is. `("closed", n)`: pull request `n` was closed without
    merging; the step is ended. `("waiting", None)`: anything else, a gate this
    reader cannot make sense of included -- the document is tenant-writable,
    and a gate that cannot be read HOLDS, it is never read as released.
    """
    gate = metadata.get(AFTER_PRS_METADATA_KEY) if isinstance(metadata, dict) else None
    if gate is None:
        return None
    if not isinstance(gate, dict):
        return ("waiting", None)
    state = gate.get("state")
    if state == AFTER_PRS_MERGED:
        return (AFTER_PRS_MERGED, None)
    closed = gate.get("closed")
    if state == AFTER_PRS_CLOSED and isinstance(closed, int) and not isinstance(closed, bool):
        return (AFTER_PRS_CLOSED, closed)
    return ("waiting", None)


#: The live-browser hand-off (docs/design/live-browser.md §4.3, lane LB-L10).
#: A PARKED(HUMAN_REQUIRED) task is promoted ONLY by a person, through
#: swarm-api's `human-ready`; this scheduler never promotes it. Past
#: `human_park_max_seconds` (24 hours, the owner's Q4) it is dead-lettered with
#: this cause. Not an `EndCause` member -- the frozen enum has none -- so it is
#: the `last_error` prefix and the event's `cause`, and `end_cause` is None as
#: for every dead-lettered park. Restated from `swarm_api.handoff`, which this
#: image does not carry; tests/unit/control_plane/test_handoff_api.py holds
#: them equal.
HUMAN_WAIT_EXPIRED = "human_wait_expired"
#: The default for `human_park_max_seconds`, read through `getattr` so a
#: settings field can be added without a change here. 24 hours: a person's
#: day, and a parked task costs nothing while it waits (invariant 1).
HUMAN_PARK_MAX_SECONDS = 86_400


def mcp_park_blocked_by(credential: CredentialAnswer, now: datetime) -> list[dict[str, Any]] | None:
    """The `blocked_by` a CREDENTIAL_MISSING park writes when it waits for
    its caller to re-store a workflow's MCP credential (§4.10, BGD
    2026-10-26): one entry naming `reason`, `credential` ("mcp"), `code`, `server`,
    `expired_at` and `parked_at`, which is where the API serves a park's detail on
    the task and what a caller watches. `parked_at` is what the sweep's limit
    is measured from. None for every other park, which leaves the field as
    it was."""
    mcp = credential.mcp
    waiting = mcp.waits_for_credential if mcp is not None else ()
    if not waiting or mcp is None or mcp.terminal:
        return None
    return [{
        "reason": ParkReason.CREDENTIAL_MISSING.value,
        **credential_block(waiting[0]),
        "parked_at": now.isoformat(),
    }]


def mcp_park_since(task: Task) -> datetime | None:
    """When a park waiting for a re-stored MCP credential began: the
    earliest of its `blocked_by` entry's `parked_at` and the task's
    `updated_at`, which the park itself wrote. The entry sits in a document
    a tenant's agent may be able to write, so the earliest is taken: a
    forged instant can only end the wait sooner, never hold it longer."""
    moments: list[datetime] = []
    for block in task.blocked_by or ():
        if not isinstance(block, dict) or block.get("credential") != MCP_CREDENTIAL:
            continue
        try:
            parked = as_datetime(block.get("parked_at"))
        except (TypeError, ValueError):
            parked = None
        if parked is not None:
            moments.append(parked)
    if task.updated_at is not None:
        moments.append(task.updated_at)
    return min(moments) if moments else None


def held_for_control_publish(metadata: Any) -> bool:
    """`swarm_api.verdictpublish.held_by_scheduler`, restated: `integrate`'s gated
    integrator, and a step carrying a review gate, which swarm-api decides and
    may end SUCCEEDED without a worker (#2524, `swarm_api.reviewgate`)."""
    block = (metadata or {}).get("dispatch") if isinstance(metadata, dict) else None
    return (
        isinstance(block, dict)
        and block.get("strategy") == "integrate"
        and block.get("role") == "integrator"
        and isinstance(block.get("verdict_gate"), dict)
    ) or (isinstance(metadata, dict)
          and isinstance(metadata.get(reviewgate_mod.REVIEW_GATE_METADATA_KEY), dict))

def accounts_held_by(db: Any, task_id: str) -> set[str]:
    """The accounts any attempt of `task_id` held, from the broker's hold records.

    `account_holds/{assignment_id}` (`quota_broker.accounts.hold_log_entry`)
    carries the task id the worker sent on assign, open or closed, so this
    answers after the release as well as before it. A worker image older than
    that field sent none, and its task names no account: its finish wakes no
    park, which is what it did before.
    """
    query = db.collection(HOLD_LOG_COLLECTION).where(
        filter=FieldFilter("task_id", "==", task_id)
    )
    held: set[str] = set()
    for snap in query.stream():
        data = snap.to_dict() or {}
        account_id = data.get("account_id")
        if isinstance(account_id, str) and account_id:
            held.add(account_id)
    return held


def owner_wait(task: Task, key: str) -> bool:
    """Whether the worker's park carries an agent's `release_wait` or `question_wait` (AU-7).

    Those parks are swarm-api's to end: the merge-wake tick wakes a release
    wait on a terminal release, and the question sweep holds a blocking
    question for 72 hours. A scheduler sweep that promoted or expired them
    on its own clock would break both.
    """
    return any(isinstance(block, dict) and isinstance(block.get(key), dict)
               for block in task.blocked_by or ())


def _instant(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def owner_hold_marked(task: Task) -> str | None:
    """The wake reason of an owner-question hold the wake tick marked, else None (#2441).

    Marked means `merge_wait.owner_hold.wake_requested_at` set at the park
    instant this task is parked at now (`next_eligible_at`, the worker's park
    instant): a mark the tick wrote for an earlier park answers nothing.
    """
    wait = (task.metadata or {}).get(MERGE_WAIT_METADATA_KEY)
    hold = wait.get(OWNER_HOLD_KEY) if isinstance(wait, dict) else None
    if not isinstance(hold, dict) or not hold.get(MERGE_WAKE_MARKER):
        return None
    parked_at = _instant(hold.get("parked_at"))
    if parked_at is None or parked_at != _instant(task.next_eligible_at):
        return None
    return str(hold.get("wake_reason") or "wake_requested")


def account_park_provider(task: Task) -> str | None:
    """The provider a worker's no-account park waits on, or None for any other park.

    `lifecycle._park_no_account` writes `park_phase: account_assign` and the
    provider into the park's `blocked_by`. A PROVIDER_QUOTA_EXHAUSTED park
    without it is a 429 or a quota probe, which an account coming free says
    nothing about.
    """
    for block in task.blocked_by or ():
        if isinstance(block, dict) and block.get("park_phase") == "account_assign":
            provider = block.get("provider") or task.provider
            return provider if isinstance(provider, str) and provider else None
    return None


#: Park reasons that a provider pool's `quota_derived_limit` already guards, so
#: promoting them early is safe: admission still refuses until quota returns.
PREWARM_REASONS = (
    ParkReason.PROVIDER_QUOTA_EXHAUSTED,
    ParkReason.PROVIDER_COOLDOWN,
    ParkReason.PROVIDER_OUTAGE,
)

#: How soon a task the account pool held back before its lease is looked at
#: again, when what holds it clears with no instant (#2222): a slot freeing
#: on a full account, or the broker's next usage poll. The worker parks the
#: same two waits for the same five minutes
#: (`agent_worker.lifecycle.STALE_READING_RETRY_SECONDS`), and the finish
#: event wakes these parks sooner when an account comes free.
POOL_RECHECK_SECONDS = 300

#: The same, for a pool waiting on a person (every account paused, draining
#: or needing a sign-in) or spent with nothing saying when:
#: `agent_worker.lifecycle.NO_ACCOUNT_RETRY_SECONDS`.
POOL_IDLE_RECHECK_SECONDS = 900

#: The broker's reasons that clear in minutes with no instant.
_POOL_SOON = (Unavailable.NO_RECENT_READING, Unavailable.ACCOUNTS_FULL)


def _own_label(account_id: str | None, tenant_id: str | None) -> str | None:
    """The label of `account_id` when it is `tenant_id`'s own account, else None.

    An account id is `<owner_tenant>:<label>`. Another workspace's label is
    never written into this tenant's park (BGD, #2222 Q2: "only within the
    same tenant"), so the line says "an account in another workspace".
    """
    if not account_id or not tenant_id or not account_id.startswith(f"{tenant_id}:"):
        return None
    return account_id.split(":", 1)[1] or None


def pool_blockers(
    accounts: list[Account], now: datetime, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    """What keeps each account from a new agent, for a park's `blocked_by` (#2222).

    The facts the waiting line names, and nothing else: the label, a state
    other than AVAILABLE, a later registration of a subscription another
    account holds (`withheld`), each window under its floor with its share
    used and when it clears, the one agent a never-measured account takes
    until its first reading, `n of m` agents when the account is full, and an
    inferred duplicate (`possible_duplicate`, identical readings), which
    `choose()` still hands out and so is never called withheld. Never a
    credential, a secret name or a hold's id, and never another tenant's
    label: the other side of a pair is named only when it is `tenant_id`'s
    own account (POOL-WAITLINE).
    """
    # The withheld side as `choose()` decides it (`_withheld`): the stored
    # mark, or a pair in this very list the broker's sweep has not marked yet.
    pairs = duplicates(accounts)
    out: list[dict[str, Any]] = []
    for account in accounts:
        item: dict[str, Any] = {"account": account.label}
        if account.state not in ASSIGNABLE_STATES:
            item["state"] = account.state.value
        other, held = pairs.get(account.account_id, (None, False))
        if account.duplicate_withheld or held:
            partner = other if held else account.duplicate_of
            item["withheld"] = {"same_subscription_as": _own_label(partner, tenant_id)}
        windows = account.blocking_windows(now)
        if windows:
            item["windows"] = {
                name: {"used": round(float(w.utilization), 4), "resets_at": w.resets_at.isoformat()}
                for name, w in sorted(windows.items())
            }
        # Before `is_full`, as `eligibility()` asks it: a never-measured
        # account whose cap is one is waiting on a reading, not full.
        if account.awaits_first_reading(now):
            item["first_reading"] = "pending"
        elif account.is_full(now):
            item["full"] = f"{len(account.live_holds(now))} of {account.max_agents}"
        if account.possible_duplicate_of is not None:
            item["possible_duplicate"] = {
                "of": _own_label(account.possible_duplicate_of, tenant_id)
            }
        out.append(item)
    return out


_FAILED_PARENT_STATES = frozenset(
    {TaskState.FAILED, TaskState.CANCELLED, TaskState.DEAD_LETTERED}
)


#: A CANCELLED parent with one of these causes was ended by a FAILURE -- its
#: own failed parent, or the fail_workflow sweep -- and passes a failure on.
_FAILURE_SENT_CAUSES = frozenset({EndCause.FAILED_PARENT.value, EndCause.WORKFLOW_SWEEP.value})
#: ... and with one of these, by a cancel somebody asked for, at that step or
#: above it.
_CANCEL_SENT_CAUSES = frozenset({EndCause.CANCEL_REQUESTED.value, EndCause.CANCELLED_PARENT.value})


def _dependency_states(task: Task, parents: dict[str, ParentEnd]) -> dict[str, TaskState]:
    """Each parent's state as the dependency rule reads it.

    A gated review's requirements check that FAILED or was DEAD_LETTERED
    reads as SUCCEEDED here (#2524, `reviewgate.check_ended_without_list`):
    it confirms nothing, so the review is held for swarm-api as after a
    check that ran, and swarm-api skips it with no requirements list ("part
    of #N") rather than the run losing its pull request.
    """
    gated = isinstance(task.metadata, dict) and isinstance(
        task.metadata.get(reviewgate_mod.REVIEW_GATE_METADATA_KEY), dict)
    return {
        tid: (TaskState.SUCCEEDED
              if gated and reviewgate_mod.check_ended_without_list(end.state,
                                                                   end.requirements_check)
              else end.state)
        for tid, end in parents.items()
    }


def _parent_cause(parents: dict[str, ParentEnd], failed: list[str]) -> EndCause | None:
    """Why a dependant of `failed` is cancelled: after a failure, or after a cancel.

    The text the cancel writes -- "an upstream workflow step did not succeed"
    -- is the same for both, which is what made the outcome ledger count a
    cancel somebody pressed as a failure (#185, decision 2).

    A PARENT'S OWN END DECIDES, NOT ITS STATE (the review of #217). The rule
    is transitive -- `_FAILED_PARENT_STATES` holds CANCELLED, so a step
    cancelled after a failure is itself a "failed parent" to its own
    dependants -- and read by state alone, every step two or more hops below a
    FAILED one was recorded as "after a cancel" that nobody made. So:

      * FAILED_PARENT when any parent is FAILED or DEAD_LETTERED, or is
        CANCELLED by a failure (`failed_parent`, `workflow_sweep`). A failure
        wins over a stop beside it: the failure is what the dependant could
        not have survived.
      * CANCELLED_PARENT only when every cancelled parent was ended by a
        cancel somebody asked for (`cancel_requested`, `cancelled_parent`, or
        -- for a parent that ended before causes were recorded -- the flag).
      * None otherwise: a cancelled parent whose own end this read cannot name
        (it ended before causes were recorded, with no flag, or carries a cause
        this image does not know). The scheduler does not guess; the outcome
        ledger splits a task without a cause by following its chain of parents
        (`swarm_api.outcomes.cancel_cause`), which it can read and this sweep
        does not.
    """
    ends = [parents[tid] for tid in failed if tid in parents]
    if any(end.state in (TaskState.FAILED, TaskState.DEAD_LETTERED) for end in ends):
        return EndCause.FAILED_PARENT
    cancelled = [end for end in ends if end.state is TaskState.CANCELLED]
    if any(end.end_cause in _FAILURE_SENT_CAUSES for end in cancelled):
        return EndCause.FAILED_PARENT
    if cancelled and all(
        end.end_cause in _CANCEL_SENT_CAUSES or (end.end_cause is None and end.cancel_requested)
        for end in cancelled
    ):
        return EndCause.CANCELLED_PARENT
    return None


#: The one `on_step_failure` value the scheduler acts on. Anything else,
#: `continue` included, leaves only the dependency rule above in force.
#:
#: RESTATED, and the restatement is pinned. The vocabulary has no home in the
#: frozen contract: `Workflow.on_step_failure` is a bare `str`. The API
#: validates it as `Literal["fail_workflow", "continue"]`
#: (`swarm_api.schemas.WorkflowCreate`) and the MCP tool advertises it as an
#: enum. A rename on either side would make this scheduler ignore the setting
#: again, silently, which is the defect this code fixes.
#: `test_the_policy_vocabulary_agrees_everywhere_it_is_stated` holds all four
#: statements together, and contract request 20 asks for one home.
FAIL_WORKFLOW = "fail_workflow"

#: Step states that fail a workflow under `fail_workflow` (owner, 2026-09-24).
#:
#: CANCELLED IS DELIBERATELY ABSENT, although `_FAILED_PARENT_STATES` has it. A
#: step is CANCELLED because somebody stopped it, or because this sweep did.
#: If a stop counted as a failure, pressing stop on one agent would end the
#: whole run under the default setting, and the stop dialog's "the rest of the
#: run keeps going" would be false. A stop still takes its own dependents, by
#: the dependency rule.
_WORKFLOW_FAILING_STATES = (TaskState.FAILED, TaskState.DEAD_LETTERED)

#: "Has not started": SUBMITTED, QUEUED, READY, PARKED, which are exactly the
#: states that hold no capacity. Derived from the frozen set rather than
#: listed, and sorted only so that the sweep's query order is stable.
_NOT_STARTED_STATES = tuple(sorted(PENDING_STATES, key=lambda state: state.value))

#: How many failed steps a cancel event names. Ten is enough to say which ones
#: without sending one document per failure. A workflow with more failures
#: than that is no less failed, and the count is not what the reader needs.
_FAILED_STEPS_NAMED = 10


@dataclass(frozen=True)
class FailedWorkflow:
    """A `fail_workflow` workflow with at least one FAILED or DEAD_LETTERED step."""

    tenant_id: str
    workflow_id: str
    #: [{"step_id", "task_id", "state"}], in a stable order. This is what every
    #: cancel event carries, so a cancelled step's own record names the failure.
    failed_steps: tuple[dict[str, Any], ...]


def read_workflow_failure(
    store: SchedulerStore,
    tenant_id: str,
    workflow_id: str,
    *,
    enforced_since: datetime | None,
) -> FailedWorkflow | None:
    """Is this workflow failed under `fail_workflow`? One read of the answer.

    THE ONE STATEMENT OF THE VERDICT. The drain calls it (through its per-drain
    cache) and so does the read-only `on_step_failure_audit`, so what the audit
    says the next drain will cancel cannot drift from what the drain cancels.

    Costs one point read for the policy. Only under `fail_workflow` does it
    also cost one equality query per failing state. A `continue` workflow, and
    one created before `enforced_since`, is never asked about its failures,
    because nothing is done with the answer.
    """
    policy = store.workflow_on_step_failure(
        tenant_id, workflow_id, enforced_since=enforced_since
    )
    if policy != FAIL_WORKFLOW:
        return None
    failed: list[Task] = []
    for state in _WORKFLOW_FAILING_STATES:
        # A requirements check that failed is not the run's failure (#2524,
        # `reviewgate.check_ended_without_list`): the review beside it ends as
        # the gate decides, with no list.
        failed.extend(
            step
            for step in store.workflow_steps_in_state(
                tenant_id, workflow_id, state, _FAILED_STEPS_NAMED)
            if not reviewgate_mod.is_requirements_check(step.metadata)
        )
    if not failed:
        return None
    failed.sort(key=lambda step: (step.step_id or "", step.id))
    return FailedWorkflow(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        failed_steps=tuple(
            {"step_id": step.step_id, "task_id": step.id, "state": step.state.value}
            for step in failed[:_FAILED_STEPS_NAMED]
        ),
    )


#: The pause after a contended admission transaction, before the next
#: candidate (#1822): long enough that the competing transaction usually
#: commits first, short enough that a pass of 200 candidates all contended
#: still fits well inside `max_run_seconds`. Jittered, so two schedulers that
#: collided do not collide again in step.
CONTENTION_BACKOFF_MIN_SECONDS = 0.01
CONTENTION_BACKOFF_MAX_SECONDS = 0.05

#: The message `firestore.transactional` gives the ValueError it raises when
#: every attempt was aborted (`_EXCEED_ATTEMPTS_TEMPLATE`).
_RETRY_EXHAUSTED = "Failed to commit transaction in"

#: Looked up at call time, so a test can record the pauses instead of taking them.
_sleep = time.sleep


def _is_contention(exc: BaseException) -> bool:
    """An admission transaction that lost to another and committed NOTHING.

    `Aborted` (409) from a transactional read, which `firestore.transactional`
    does not retry, or the `ValueError` it raises once `max_attempts` commits
    were all aborted -- read by its cause, or by its message when the last
    attempt ended on another retryable code. Never `DeadlineExceeded`,
    `ServiceUnavailable` or anything broader: a deadline can expire after the
    commit landed, so the transaction may have written, and skipping it would
    be guessing.
    """
    if isinstance(exc, Aborted):
        return True
    if isinstance(exc, ValueError):
        return isinstance(exc.__cause__, Aborted) or str(exc).startswith(_RETRY_EXHAUSTED)
    return False


@dataclass
class DrainReport:
    passes: int = 0
    scanned: int = 0
    leased: int = 0
    dispatched: int = 0
    denied: int = 0
    parked: int = 0
    cancelled: int = 0
    skipped: int = 0
    dispatch_failures: int = 0
    promoted_dependencies: int = 0
    #: Dependants left PARKED for swarm-api's publish of a MERGE verdict (#748).
    held_dependencies: int = 0
    #: Root steps left PARKED because a pull request their workflow waits for
    #: (`after`, lane SPEED-D3) has not merged yet.
    held_pr_gates: int = 0
    promoted_credentials: int = 0
    #: READY or PARKED tasks ended FAILED before any lease because a per-run
    #: MCP credential they name expired or was revoked (contract request 99).
    failed_unrunnable: int = 0
    promoted_prewarm: int = 0
    #: SCHEDULED_RETRY parks returned to READY once `next_eligible_at` passed.
    promoted_scheduled_retries: int = 0
    #: MANUAL_PAUSE parks returned to READY once what paused them cleared.
    promoted_manual_pauses: int = 0
    #: Failed dispatches the platform refused, parked PLATFORM_REFUSED with no
    #: attempt spent (contract request 123).
    platform_refused: int = 0
    #: PLATFORM_REFUSED parks returned to READY once `next_eligible_at` passed.
    promoted_platform_refusals: int = 0
    #: SCHEDULED_RETRY parks on a task that had used its last attempt, ended
    #: DEAD_LETTERED instead of retried (`_promote_scheduled_retries`).
    dead_lettered: int = 0
    #: CHILDREN_INCOMPLETE parks returned to READY: every child terminal with
    #: its outputs staged, or the await deadline passed (child tasks, §3.3).
    promoted_child_awaits: int = 0
    #: CI_PENDING parks returned to READY: swarm-api's wake tick marked their
    #: checks settled, or their fallback instant passed (lane MS2).
    promoted_ci_waits: int = 0
    #: PROVIDER_QUOTA_EXHAUSTED parks at `account_assign` returned to READY by
    #: a finish event, because an account the finished task held serves their
    #: tenant and `choose()` now has one for them (#1885).
    promoted_account_waits: int = 0
    #: Tasks the account pool held back (#2222): parked by admission BEFORE a
    #: lease because no account the worker would be offered can take a new
    #: agent, or a due no-account park the prewarm sweep left parked for the
    #: same reason. Each is a container that was not started only to park.
    held_by_pool: int = 0
    #: Children this drain cancelled or flagged because of their parent: its
    #: cancel, its end, or its await deadline (child tasks, §3.4). The ones
    #: that became CANCELLED here are also in `cancelled`.
    children_cascaded: int = 0
    #: Workflows this drain found failed under `on_step_failure: fail_workflow`
    #: and swept. Their cancels are in `cancelled` like every other cancel.
    #: This field says how many workflows those cancels came from.
    failed_workflows_swept: int = 0
    #: Transitions this drain decided and did NOT write, because the task had
    #: moved on since it was read -- a cancel, another scheduler, the reconciler
    #: or the worker got there first. Each one used to be a blind overwrite
    #: (incident wf_ebb3ab2d65664707a559, F-9). The breakdown by write and
    #: reason is `swarm_scheduler_stale_writes_total{write, reason}`.
    stale_writes: int = 0
    #: Admissions skipped for this pass because their lease transaction was
    #: aborted by contention (`_is_contention`, #1822). Each wrote nothing, so
    #: the task is still READY and a later pass, wake or tick retries it.
    contended: int = 0
    tenants_seen: int = 0
    topped_up_tenants: int = 0
    stop_reason: str = "not_started"
    duration_seconds: float = 0.0
    blockers: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Scheduler:
    def __init__(
        self,
        *,
        settings: SchedulerSettings,
        store: SchedulerStore,
        router: BackendRouter,
        metrics: SchedulerMetrics | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        pool: AccountPool | None = None,
        forge: ForgeConnections | None = None,
        secrets: WorkspaceGrants | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._router = router
        self._metrics = metrics or SchedulerMetrics()
        # Admission latency and re-runs as Prometheus series (S32), from the
        # same numbers acquire_lease logs. Only a real store has the hook; a
        # test double without it is left alone.
        if isinstance(store, SchedulerStore):
            store.admission_observer = self._metrics.observe_admission
        # The account pool admission and the credential sweep ask about
        # (credentials.py). `main.build_scheduler` hands the SAME instance to
        # the Cloud Run dispatcher, so the Job's secret mount is decided on the
        # list admission read, and `drain()` forgets it at the top of each run.
        self._pool = pool if pool is not None else AccountPool.for_deployment(
            settings, store.db
        )
        # The submitters' GitHub grants and connections, which admission and
        # the credential sweep ask about and the dispatcher never does
        # (credentials.py, docs/onboarding.md §3.3 step 2). Forgotten at the
        # top of each run, like the pool.
        self._forge = forge if forge is not None else ForgeConnections(store.db)
        # The workspace's enabled secret grants, which admission and the
        # credential sweep ask about when a profile or a tool it launches
        # declares a REQUIRED slot (secret_slots.py, workspace-secrets.md
        # §4.2). One query at most per admission pass: forgotten at the top
        # of each run and each pass.
        self._secrets = secrets if secrets is not None else WorkspaceGrants(store.db)
        self._now = now
        self._monotonic = monotonic
        self._aging = AgingConfig(
            interval_seconds=settings.aging_interval_seconds,
            step=settings.aging_step,
            max_bonus=settings.aging_max_bonus,
        )
        # Populated per drain; initialised here so `_admit_one` is callable in
        # isolation (the unit tests exercise it directly).
        self._tenant_cache: dict[str, Tenant | None] = {}
        self._paused_by_cache: dict[str, dict[str, str]] = {}
        # Per drain as well, keyed by (tenant_id, workflow_id). A verdict is at
        # most one drain old, except that a failure the drain itself writes
        # evicts it at once (see the DispatchError branch of `_admit_one`). A
        # failure a WORKER or the reconciler writes mid-drain is not seen until
        # the next drain, so a sibling admitted in between starts, holds
        # capacity and runs to completion (docs/workflows.md says so).
        self._workflow_verdicts: dict[tuple[str, str], FailedWorkflow | None] = {}
        # Where each paged park sweep resumes (`_parked_window`), keyed by
        # (sweep, reason): the account wake and the prewarm both read
        # PROVIDER_QUOTA_EXHAUSTED, and one cursor between them would hand each
        # only every other page. Kept across drains on purpose: that is what
        # moves the window.
        self._park_cursors: dict[tuple[str, ParkReason], str | None] = {}
        self._swept_workflows: set[tuple[str, str]] = set()
        # Tasks this run ended itself (`_note_ended`), whose dependants it
        # resolves before it returns (`_release_ended`, #636). Per run.
        self._ended: list[str] = []
        # Where the child-cascade sweep resumes: (parent_task_id, child id).
        self._child_cursor: tuple[str, str] | None = None
        # The runner-size version this admission pass applies (`_size_version`),
        # read at most once per pass and forgotten at the top of each.
        self._sizes: sizes_mod.SizeVersion | None = None
        cutoff = settings.on_step_failure_enforced_since
        log.info(
            "on_step_failure: fail_workflow applies to %s",
            "every workflow, whenever it was submitted"
            if cutoff is None
            else f"workflows created at or after {cutoff.isoformat()} "
            "(ON_STEP_FAILURE_ENFORCED_SINCE); older ones keep the dependency rule",
        )
        self._admission = AdmissionConfig(
            dispatch_timeout_seconds=settings.core.dispatch_timeout_seconds,
            lease_timeout_seconds=settings.core.lease_timeout_seconds,
            heartbeat_interval_seconds=settings.core.heartbeat_interval_seconds,
        )

    @property
    def metrics(self) -> SchedulerMetrics:
        return self._metrics

    @property
    def store(self) -> SchedulerStore:
        return self._store

    @property
    def settings(self) -> SchedulerSettings:
        return self._settings

    # -- entry point ------------------------------------------------------

    def _begin_run(self) -> None:
        """Forget what the previous run cached. Every entry point starts here."""
        self._tenant_cache: dict[str, Tenant | None] = {}
        self._paused_by_cache: dict[str, dict[str, str]] = {}
        self._topup_tenant_ids: list[str] | None = None
        self._workflow_verdicts = {}
        self._swept_workflows = set()
        self._ended = []
        # A loan made or withdrawn since the last drain is seen by this one.
        self._pool.forget()
        # A reconnect, or a refresh that failed, since the last drain likewise.
        self._forge.forget()
        # And a secret grant ticked or unticked.
        self._secrets.forget()

    def drain(self) -> DrainReport:
        started = self._monotonic()
        report = DrainReport()
        self._begin_run()

        if self._store.dispatch_paused():
            self._metrics.paused.set(1)
            report.stop_reason = "dispatch_paused"
            report.duration_seconds = self._monotonic() - started
            self._metrics.runs.labels(stop_reason=report.stop_reason).inc()
            self._metrics.run_seconds.observe(report.duration_seconds)
            log.info("drain skipped: dispatch is paused by an admin")
            return report
        self._metrics.paused.set(0)

        report.promoted_dependencies = self._promote_dependencies(report)
        report.promoted_credentials = self._promote_credentials(report)
        report.promoted_scheduled_retries = self._promote_scheduled_retries(report)
        report.promoted_platform_refusals = self._promote_platform_refusals(report)
        report.promoted_manual_pauses = self._promote_manual_pauses(report)
        report.promoted_ci_waits = self._promote_ci_waits(report)
        report.dead_lettered += self._expire_human_waits(report)
        self._sweep_awaiting_input(report)
        # Guarded: a child sweep that cannot query (its index still building
        # after a release, say) must not stop admission for every tenant.
        try:
            report.promoted_child_awaits = self._promote_child_awaits(report)
            report.children_cascaded += self._sweep_child_cascade(report)
        except Exception:
            log.exception("child-task sweeps failed this drain; admission continues")
        if self._settings.enable_prewarm:
            report.promoted_prewarm = self._prewarm(report)
        # The sweeps above run AFTER `_promote_dependencies`, so what they
        # ended is resolved here, not on the next tick (#636).
        self._release_ended(report)

        while True:
            if report.passes >= self._settings.max_passes_per_run:
                report.stop_reason = "max_passes"
                break
            if report.leased >= self._settings.max_leases_per_run:
                report.stop_reason = "max_leases"
                break
            if self._monotonic() - started >= self._settings.max_run_seconds:
                report.stop_reason = "time_budget"
                break

            scanned_before, contended_before = report.scanned, report.contended
            leased_this_pass = self._admission_pass(report, started)
            if leased_this_pass is None:
                report.stop_reason = "queue_empty"
                break

            contended_this_pass = report.contended - contended_before
            if leased_this_pass == 0 and 0 < contended_this_pass == report.scanned - scanned_before:
                # Every candidate lost its transaction to another writer. The
                # drain still ends normally (200): nothing was written, and the
                # next wake or the 1-minute tick retries (#1822).
                report.stop_reason = "all_contended"
                break

            if leased_this_pass == 0:
                # Nothing in this slice could be admitted. Re-reading the same
                # slice would produce the same answer, so the run is over.
                report.stop_reason = "no_admissible_work"
                break

        # And what admission ended: a cancel it honoured, a step its workflow
        # sweep cancelled, a dispatch that failed on the last attempt.
        self._release_ended(report)
        report.duration_seconds = self._monotonic() - started
        self._metrics.runs.labels(stop_reason=report.stop_reason).inc()
        self._metrics.run_seconds.observe(report.duration_seconds)
        log.info("drain finished %s", report.to_dict())
        return report

    def _admission_pass(
        self, report: DrainReport, started: float, *, include: list[Task] | tuple[Task, ...] = ()
    ) -> int | None:
        """One pass of admission: read a slice of READY work, interleave it, try each.

        Returns how many were leased, or None when there was nothing READY to
        try. `include` adds tasks the caller knows are READY to the slice --
        the finish event's released dependants, which a full slice of older or
        higher-priority work might otherwise leave out -- and they are then
        ordered with everything else by `round_robin_order`, so a released
        task is admitted in its fair turn and never ahead of it.
        """
        # A grant ticked since the previous pass is seen by this one, and
        # every task in this pass is judged on one read of the grants.
        self._secrets.forget()
        # And a runner size an admin saved: read on this pass's first GKE
        # admission, then the same version for every agent in the pass.
        self._sizes = None
        candidates = self._store.ready_tasks(self._settings.candidate_batch_size)
        if include:
            seen = {task.id for task in candidates}
            candidates = candidates + [task for task in include if task.id not in seen]
        if not candidates:
            self._metrics.candidates.observe(0)
            return None
        candidates, topped_up = self._top_up_starved_tenants(candidates)
        report.topped_up_tenants = max(report.topped_up_tenants, topped_up)
        self._metrics.candidates.observe(len(candidates))

        ordered = round_robin_order(
            candidates,
            now=self._now(),
            config=self._aging,
            active_by_tenant=self._store.active_by_tenant(),
        )
        tenants = {task.tenant_id for task in ordered}
        report.tenants_seen = max(report.tenants_seen, len(tenants))
        self._metrics.tenants_in_rotation.set(len(tenants))

        leased_this_pass = 0
        for task in ordered:
            report.scanned += 1
            if report.leased >= self._settings.max_leases_per_run:
                break
            if self._monotonic() - started >= self._settings.max_run_seconds:
                break
            if self._admit_one(task, report):
                leased_this_pass += 1
        report.passes += 1
        return leased_this_pass

    # -- the finish event (#636) ------------------------------------------

    def release_dependants(self, task_id: str) -> DrainReport:
        """A task has ended: release what waited on it, and admit once. Now, not next tick.

        Called from `/pubsub/push` for a `task_finished` wake, which the worker
        publishes after its terminal write and its lease release. Measured
        before it existed (#636, 2026-10-05): parent done -> child READY p50
        27 s, p90 54 s, because only `drain()` ran `_promote_dependencies` and
        nothing woke a drain when a task ended.

        THE SAME RULE, NOT A SECOND COPY. Each of the parent's dependants goes
        through `_resolve_dependency`, the body of `_promote_dependencies`:
        promoted only when EVERY parent SUCCEEDED, cancelled when one did not,
        left PARKED otherwise, with `on_step_failure` read first. Promotion is
        `SchedulerStore.promote_to_ready`, the guarded write the sweep uses, so
        whichever of the event and the tick gets there second finds the task
        READY, not PARKED, and writes nothing: a duplicate event, a redelivery
        and the tick after an event are all no-ops (the dependants query no
        longer returns the task at all). A dependant this cancels has ended
        too, so its own dependants are resolved in the same event
        (`_release_chain`), rather than one tick per link.

        AND THE PARKS ITS ACCOUNT CAN TAKE (#1885): a no-account park of a
        tenant an account the parent held serves, which `choose()` would now
        hand an account, is promoted too (`_wake_account_parks`).

        THEN ONE ADMISSION PASS, `_admission_pass`, with the released tasks
        added to the slice: the same `_admit_one`, the same all-or-nothing
        lease transaction (invariant 2) and the same fencing generation on the
        lease (invariant 5). The pass runs even when nothing was released,
        because the parent's lease release freed capacity some READY task may
        have been denied. Only `_admit_one` takes a lease, and only for a task
        that is READY, so nothing this path does creates demand for a task
        that is not LEASED (invariant 1).

        A NO-OP for a task that is missing or has not ended -- a wake is a
        doorbell, and anyone who may publish one may ring it for any id -- and
        while dispatch is paused, as `drain()` is.

        THE TICK STAYS THE SAFETY NET. Anything that stops this path -- a wake
        never published, a push refused, this method raising -- leaves the
        dependant PARKED and the next drain's `_promote_dependencies` releases
        it exactly as before #636.
        """
        started = self._monotonic()
        report = DrainReport()
        self._begin_run()

        if self._store.dispatch_paused():
            report.stop_reason = "dispatch_paused"
            return self._end_finish_event(report, started)

        parent = self._store.get_task(task_id)
        if parent is None or parent.state not in TERMINAL_STATES:
            report.stop_reason = "not_terminal"
            return self._end_finish_event(report, started)

        released = self._release_chain([parent], report)
        report.promoted_dependencies = len(released)
        # The account the parent held is free now, too (#1885).
        woken = self._wake_account_parks(parent, report)
        report.promoted_account_waits = len(woken)
        released = released + woken

        # Re-read: each `_admit_one` write is guarded on the state it was
        # handed, and what was handed here was the PARKED snapshot.
        fresh = [
            task
            for task in (self._store.get_task(tid) for tid in released)
            if task is not None and task.state is TaskState.READY
        ]
        self._admission_pass(report, started, include=fresh)
        # What that pass ended itself has no worker to ring for it either.
        self._release_ended(report)
        report.stop_reason = "task_finished"
        return self._end_finish_event(report, started)

    def _wake_account_parks(self, ended: Task, report: DrainReport) -> list[str]:
        """Re-ask the pool for the no-account parks the ended task's account serves (#1885).

        Before this, a park at `account_assign` resumed ONLY at its own
        `next_eligible_at`, the broker's instant -- the prewarm sweep is the
        one place such a park returns to READY -- and nothing happened when an
        account came free. So this runs on the finish event: the accounts the
        ended task held (`accounts_held_by`), the tenants each serves (its
        owner and the tenants it is lent to, `Account.may_serve`), and every
        PROVIDER_QUOTA_EXHAUSTED park at `account_assign` of one of those
        tenants, for that account's provider.

        GATED ON THE BROKER'S OWN `choose()`, over the pool admission reads
        this run and the parked step's model class (`task_model_class`, the
        rule the broker's `step_class_for` applies). Promoting unconditionally
        would start a container per parked task per finish, each to park
        again. Promoting only when an account would be handed over costs a
        container only when one runs.

        AND EACH PROMOTION TAKES ITS SLOT (#2222). The list is read once, so
        before this one freed slot answered yes for every park in turn and
        woke them all. Each promotion now `claim`s the account `choose()`
        named, so the next park is asked about one slot fewer, and a finish
        wakes at most as many parks as there are free slots, in the order the
        store returns them.

        Invariant 1 holds: a park this leaves alone holds nothing, a park it
        promotes becomes READY, which holds nothing either, and only the
        admission pass that follows takes a lease, through the ordinary
        all-or-nothing transaction. A tenant no freed account serves is never
        looked at.
        """
        if not self._pool.configured:
            return []
        held = accounts_held_by(self._store.db, ended.id)
        if not held:
            return []
        pool = self._pool.accounts()
        served: set[tuple[str, str]] = set()
        for account in pool:
            if account.account_id in held:
                for tenant_id in (account.owner_tenant, *account.lend_to):
                    served.add((tenant_id, account.provider))
        if not served:
            return []
        now = self._now()
        woken: list[str] = []
        for task in self._parked_window(ParkReason.PROVIDER_QUOTA_EXHAUSTED, sweep="account_wake"):
            provider = account_park_provider(task)
            if provider is None or (task.tenant_id, provider) not in served:
                continue
            if self._stop_for_failed_workflow(task, report):
                continue
            profile = RUNNER_PROFILES.get(task.runner_profile)
            if profile is None:
                continue
            model_class = task_model_class(profile, task)
            candidates = accounts_serving(
                self._pool.accounts(), task.tenant_id, provider, model_class=model_class
            )
            chosen = choose(candidates, task.tenant_id, now, model_class=model_class)
            if chosen is None:
                continue
            if self._promote(
                task,
                kind="account_freed",
                detail={
                    "reason": "account_freed",
                    "park_reason": ParkReason.PROVIDER_QUOTA_EXHAUSTED.value,
                    "finished_task_id": ended.id,
                    "was_eligible_at": (
                        task.next_eligible_at.isoformat() if task.next_eligible_at else None
                    ),
                },
                report=report,
            ):
                self._pool.claim(
                    chosen.account_id, task_id=task.id, tenant_id=task.tenant_id, now=now
                )
                woken.append(task.id)
        return woken

    def _release_chain(self, ended: list[Task], report: DrainReport) -> list[str]:
        """Resolve the dependants of the `ended` tasks, and of every task that ends meanwhile.

        A dependant the rule CANCELS -- its parent did not succeed -- has
        itself reached a terminal state, and no worker will ring a wake for
        it. Stopping at the first level left its own dependants PARKED until
        the next drain's sweep, one tick per link of the chain (#636). So
        every task ended while this runs -- a dependant cancelled, or the
        steps a `fail_workflow` sweep inside `_resolve_dependency` cancelled
        -- is resolved in turn (`_note_ended`), breadth first, through the
        same `_resolve_dependency`, with the same tenant filter in
        `dependants_waiting_on` (invariant 9).

        Only an end extends the chain. A promoted dependant is READY, not
        ended; its dependants wait for its own finish. Bounded by
        `dependency_sweep_size` dependants in all and as many ended tasks
        looked up, and by visited sets; whatever the bounds leave is the next
        drain's, as before. Returns the ids promoted, for the admission pass.
        """
        budget = self._settings.dependency_sweep_size
        lookups = self._settings.dependency_sweep_size
        released: list[str] = []
        resolved: set[str] = set()
        expanded: set[str] = {task.id for task in ended}
        queue: list[Task] = list(ended)
        while queue and budget > 0 and lookups > 0:
            current = queue.pop(0)
            lookups -= 1
            for task in self._store.dependants_waiting_on(current, budget):
                if task.id in resolved or budget <= 0:
                    continue
                resolved.add(task.id)
                budget -= 1
                if self._resolve_dependency(task, report):
                    released.append(task.id)
            queue.extend(self._take_ended(expanded))
        # Left by the bounds: the next drain's `_promote_dependencies` has them.
        self._ended.clear()
        return released

    def _note_ended(self, task_id: str) -> None:
        """This run wrote a terminal state on `task_id`. Every such write calls it.

        No worker rings `task_finished` for a task the scheduler ended, and
        `_promote_dependencies` has already run by the time the sweeps and
        admission write these, so its dependants waited for the next tick
        (#636, left by #741). `_release_ended` resolves them in this run.
        """
        self._ended.append(task_id)

    def _take_ended(self, expanded: set[str]) -> list[Task]:
        """The tasks noted ended since the last call and not yet expanded, re-read."""
        noted, self._ended = self._ended, []
        out: list[Task] = []
        for task_id in dict.fromkeys(noted):
            if task_id in expanded:
                continue
            expanded.add(task_id)
            task = self._store.get_task(task_id)
            if task is not None and task.state in TERMINAL_STATES:
                out.append(task)
        return out

    def _release_ended(self, report: DrainReport) -> None:
        """Resolve the dependants of what this run ended itself. Never fails the run.

        A task the scheduler ends is FAILED, DEAD_LETTERED or CANCELLED, never
        SUCCEEDED, so the rule CANCELS its dependants and promotes none: this
        writes no READY and takes no lease, and creates no demand (invariant
        1). The scheduler does not publish a `task_finished` wake to itself
        instead: it is the wake's receiver, and that message would queue
        behind the drain lock this run holds.

        A failure here is logged and the run goes on: resolving now is a
        latency win, and the next drain's `_promote_dependencies` resolves the
        same dependants by the same rule, as it did before #636.
        """
        if not self._ended:
            return
        try:
            ended = self._take_ended(set())
            if ended:
                report.promoted_dependencies += len(self._release_chain(ended, report))
        except Exception:
            self._ended.clear()
            log.exception(
                "resolving the dependants of tasks this run ended failed; "
                "the next drain's dependency sweep resolves them"
            )

    def _end_finish_event(self, report: DrainReport, started: float) -> DrainReport:
        report.duration_seconds = self._monotonic() - started
        self._metrics.finish_events.labels(outcome=report.stop_reason).inc()
        log.info("finish event handled %s", report.to_dict())
        return report

    # -- candidate selection ----------------------------------------------

    def _top_up_starved_tenants(self, candidates: list[Task]) -> tuple[list[Task], int]:
        """Add candidates for tenants the global slice left out entirely.

        `ready_tasks` returns the globally highest-priority slice. Round-robin
        and starvation aging both operate on that slice, so neither can help a
        tenant that is not IN it -- and a tenant holding more than
        `candidate_batch_size` high-priority tasks fills it by itself. Priority
        is a number the caller chooses, so that is a one-line denial of service
        against every other tenant, and it is exactly the failure round-robin
        exists to prevent.

        The top-up runs only when the slice came back FULL. A short slice is the
        entire READY queue, so nobody can be missing from it and the extra
        queries would buy nothing. That keeps the ordinary case at exactly one
        query per pass, which is what the hot path was designed around.
        """
        budget = self._settings.tenant_topup_candidates
        if budget <= 0 or len(candidates) < self._settings.candidate_batch_size:
            return candidates, 0

        if self._topup_tenant_ids is None:
            # Once per drain, not once per pass: the tenant roster does not
            # change inside a 45-second run.
            self._topup_tenant_ids = self._store.enabled_tenant_ids(
                self._settings.max_topup_tenants
            )

        represented = {task.tenant_id for task in candidates}
        missing = [tid for tid in self._topup_tenant_ids if tid not in represented]
        if not missing:
            return candidates, 0

        seen = {task.id for task in candidates}
        topped = list(candidates)
        tenants_added = 0
        for tenant_id in missing:
            extra = self._store.ready_tasks_for_tenant(tenant_id, budget)
            added = False
            for task in extra:
                if task.id in seen:
                    continue
                seen.add(task.id)
                topped.append(task)
                added = True
            if added:
                tenants_added += 1
        if tenants_added:
            self._metrics.topped_up.inc(tenants_added)
            log.info(
                "candidate slice was saturated; topped up %d starved tenant(s)",
                tenants_added,
            )
        return topped, tenants_added

    # -- one task ---------------------------------------------------------

    def _tenant(self, tenant_id: str) -> Tenant | None:
        if tenant_id not in self._tenant_cache:
            self._tenant_cache[tenant_id] = self._store.get_tenant(tenant_id)
        return self._tenant_cache[tenant_id]

    def _tenant_paused_by(self, tenant: Tenant) -> dict[str, str]:
        """`tenant_disabled_by`, read once per tenant per drain like the tenant."""
        cache = self._paused_by_cache
        if tenant.tenant_id not in cache:
            cache[tenant.tenant_id] = tenant_disabled_by(self._store.db, tenant)
        return cache[tenant.tenant_id]

    def _admit_one(self, task: Task, report: DrainReport) -> bool:
        """Try to admit and dispatch one task. Returns True only if leased."""
        now = self._now()

        if task.cancel_requested:
            # It holds no capacity in READY, so it can be finished here.
            self._cancel(
                task,
                "cancellation requested before admission",
                report,
                why="cancel_requested",
                end_cause=children_mod.cancel_end_cause(task.metadata),
            )
            return False

        # THE ADMISSION GATE for `on_step_failure: fail_workflow`. Every step
        # that starts passes through here, and an independent READY sibling of
        # a failed step shares no dependency edge with it, so no dependency
        # check would ever stop it. It sees failures as of this drain's verdict
        # (see `_stop_for_failed_workflow` for the window that leaves).
        if self._stop_for_failed_workflow(task, report):
            return False

        # A fix round whose pull request already ended buys nothing (#2524).
        skipped = reviewgate_mod.end_if_noop(self._store, task)
        if skipped is not None:
            if skipped.applied:
                self._count_cancel(report, reason="noop_precheck")
                self._note_ended(task.id)
            else:
                self._count_stale(skipped, report)
            return False

        if task.next_eligible_at is not None and task.next_eligible_at > now:
            report.skipped += 1
            return False

        profile = RUNNER_PROFILES.get(task.runner_profile)
        if profile is None:
            # The catalogue no longer has this profile. Parking rather than
            # failing keeps the work recoverable if an admin restores it.
            self._park(
                task,
                ParkReason.MANUAL_PAUSE,
                report,
                detail={"error": f"runner_profile {task.runner_profile!r} is not in the catalogue"},
            )
            return False

        if task.depends_on:
            parents = self._store.parent_ends(task.depends_on, tenant_id=task.tenant_id)
            states = _dependency_states(task, parents)
            failed = [tid for tid, state in states.items() if state in _FAILED_PARENT_STATES]
            if failed:
                self._cancel(
                    task,
                    "an upstream workflow step did not succeed",
                    report,
                    why="failed_parent",
                    detail={"failed_parents": failed},
                    end_cause=_parent_cause(parents, failed),
                )
                return False
            if any(states.get(tid) is not TaskState.SUCCEEDED for tid in task.depends_on):
                self._park(
                    task,
                    ParkReason.DEPENDENCY_INCOMPLETE,
                    report,
                    detail={
                        "waiting_on": [
                            tid
                            for tid in task.depends_on
                            if states.get(tid) is not TaskState.SUCCEEDED
                        ]
                    },
                )
                return False

        # THE AWAIT-INPUT BACKSTOP (docs/design/human-steps.md §4.4 step 2).
        # A `control_plane` profile is never run, so it is parked here, before
        # the tenant, the credential or any capacity is looked at, and never
        # leased whatever the headroom (invariant 1). A step submitted with no
        # dependencies reaches READY this way, and so does one an operator
        # promotes by hand; the dependency rule parks the rest itself.
        if profile.control_plane:
            self._park_awaiting_input(task, report)
            return False

        tenant = self._tenant(task.tenant_id)
        if tenant is None or not tenant.enabled:
            # Who disabled it, when a person did through the API (WAIT-PAUSER,
            # part of #1782): `paused_by` is what waiting_detail reads. A
            # missing tenant was paused by nobody.
            paused = self._tenant_paused_by(tenant) if tenant is not None else {}
            self._park(
                task,
                ParkReason.MANUAL_PAUSE,
                report,
                detail={"error": "tenant is missing or disabled", **paused},
            )
            return False

        # ONE QUESTION, asked here, in the credential sweep and by the Cloud
        # Run Job's secret mount: can this tenant run this profile, on a key of
        # its own or on a pool account it may use (credentials.py, #169). No
        # means a container that can only park or fail, holding a slot while
        # it does, so it parks here, before any lease. The detail names what
        # the account pool answered, so the park says what would clear it.
        # The submitter's GitHub connection is part of the same question: a
        # task that runs on their user slot while the refresh sweep says
        # `refresh_failed`, or with no connection at all, would clone with a
        # token GitHub refuses (docs/onboarding.md §3.3 step 2, OB6). Parked
        # here it holds nothing (invariant 1), and nothing was reserved, so
        # the all-or-nothing reservation below is never half-made (2).
        # And so is a slot the profile, or a tool its `browser_tool` launches,
        # declares `required` with no enabled grant (workspace-secrets.md
        # §4.2, SEC-ADMIT-PARK): parked here rather than by the worker after
        # a pod has started. The event names each consumer and slot, never a
        # secret id. Nothing leased means nothing for concurrency to count (3).
        credential = credential_for(
            profile, tenant, self._pool, task=task, forge=self._forge,
            secrets=self._secrets, tenant_of=self._tenant,
        )
        # A per-run MCP credential that expired or was revoked, on a task that
        # is no workflow's, is not a park: nobody can hand it a new value, so
        # it ends FAILED here, from READY, in one guarded write that takes no
        # lease and reserves nothing (registered-mcp-servers.md §4.10,
        # contract request 99). A WORKFLOW's task parks instead (BGD
        # 2026-10-26): its caller re-stores the credential, and the park's
        # `blocked_by` entry says which server and when it expired.
        if credential.terminal:
            self._fail_unrunnable(task, credential, report)
            return False
        if not credential.runnable:
            self._park(
                task,
                ParkReason.CREDENTIAL_MISSING,
                report,
                detail=credential.park_detail(),
                blocked_by=mcp_park_blocked_by(credential, now),
            )
            return False

        # ASK THE POOL BEFORE THE LEASE (#2222, BGD 2026-10-24, "Ask the pool
        # before a container"). The worker asks the broker for an account
        # whenever one serves this tenant, key or no key (`lifecycle.
        # _lease_account`), and until now it was the first to: it leased,
        # started a container, cloned and checkpointed, then learned there
        # was no account and parked. So admission asks the broker's own
        # `choose()` first, over the accounts this run read with the slots it
        # already handed out (`AccountPool.claim`). None is a park here,
        # PROVIDER_QUOTA_EXHAUSTED at `account_assign` as the worker would
        # have written it, before any lease: it holds nothing (invariant 1),
        # nothing was reserved (2), and nothing leased means nothing for
        # concurrency to count (3). A task this run promoted from such a park
        # carries a claim; it is dropped first, so the task is not counted
        # against itself.
        self._pool.release_claim(task.id)
        pool_choice: Account | None = None
        question = self._pool_question(task, profile)
        if question is not None:
            candidates, provider, model_class = question
            pool_choice = choose(candidates, task.tenant_id, now, model_class=model_class)
            if pool_choice is None:
                self._park_on_pool(task, candidates, provider, model_class, now, report)
                return False

        backend = resolve_backend(profile)
        # THE SIZE IS PINNED HERE, BEFORE THE LEASE (docs/design/runner-sizes.md
        # §4.4, SIZES-SCHED-2). The units the transaction charges, the size the
        # attempt records and the pod the GKE renderer builds are this one
        # object, so a version saved between the lease and the dispatch cannot
        # split them (invariant 2), and a running agent keeps the size it was
        # admitted with. Units are charged on REQUESTS, as Autopilot bills them;
        # a CPU burst above the request is charged nothing (§2.4).
        sized = sizes_mod.for_task(
            self._size_version(backend), task, backend=backend,
            live=live_display_on(profile, self._settings),
        )
        units = sized.units

        try:
            lease = self._store.acquire_lease(
                task, units=units, backend=backend.value, config=self._admission
            )
        except (Aborted, ValueError) as exc:
            if not _is_contention(exc):
                raise
            # SAFE TO SKIP because an aborted transaction WROTE NOTHING: the
            # task is still READY, no pool count moved and no lease document
            # exists, so the all-or-nothing reservation is untouched
            # (invariant 2) and concurrency, counted from LEASED, is unchanged
            # (invariant 3). A later pass, the next wake or the 1-minute tick
            # retries it. Ending the drain here instead answered 503 and
            # Pub/Sub redelivered the wake into the same contention: 199 such
            # drains in 48 h (#1822). `_is_contention` admits exactly the two
            # forms that cannot have committed; a deadline is not one of them.
            report.contended += 1
            log.warning(
                "admission contended, skipped for this pass task_id=%s tenant_id=%s error=%s",
                task.id,
                task.tenant_id,
                type(exc).__name__,
            )
            _sleep(random.uniform(CONTENTION_BACKOFF_MIN_SECONDS, CONTENTION_BACKOFF_MAX_SECONDS))
            return False
        except AdmissionDenied as denied:
            reasons = denied.reasons
            recorded = self._store.record_blockers(task, reasons)
            if not recorded.applied:
                # A `not_ready` denial: another scheduler admitted it first.
                self._count_stale(recorded, report)
            report.denied += 1
            first = reasons[0] if reasons else {}
            reason = str(first.get("reason", "unknown"))
            report.blockers[reason] = report.blockers.get(reason, 0) + 1
            self._metrics.denied.labels(reason=reason).inc()
            # Deliberately NOT a break: the next task is probably another
            # tenant's, and probably admissible.
            return False

        report.leased += 1
        self._metrics.leased.labels(
            tenant=task.tenant_id, runner_profile=task.runner_profile
        ).inc()
        if pool_choice is not None:
            # This run's arithmetic only: the broker takes the real hold when
            # the worker asks, and may pick another account of the same pool.
            self._pool.claim(
                pool_choice.account_id, task_id=task.id, tenant_id=task.tenant_id, now=now
            )

        # EVERYTHING FROM HERE IS INSIDE THE GUARD, and the guard catches more
        # than DispatchError. `acquire_lease_in_transaction` has committed: the
        # pools are incremented, the lease document exists and the task is
        # LEASED. Nothing in this service ever looks at a LEASED task again --
        # `ready_tasks()` selects `state == "READY"` only -- so any exception
        # escaping from here is a slot this scheduler can neither see nor undo,
        # and invariant 3 counts it as occupied until the reconciler's deadline
        # sweep reaches it minutes later, in another service.
        #
        # `create_attempt` and `append_event` used to sit above the try entirely,
        # and the try caught DispatchError alone -- which the Cloud Run client
        # construction inside `ensure_job` does NOT raise: a credential refresh
        # failure there is a `DefaultCredentialsError` and went straight out of
        # `drain()`. docs/audits/2026-09-18/05-scheduler-capacity-leaks.md,
        # findings 1 and 2.
        try:
            self._store.create_attempt(task, lease, backend.value)
            self._store.pin_attempt_size(lease.attempt_id, sized.attempt_fields())
            self._store.append_event(
                task,
                EventType.LEASE_ACQUIRED,
                {"pools": lease.pools, "units": lease.units, "backend": backend.value},
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
            )
            execution = self._router.dispatch(
                task=task, lease=lease, profile=profile, tenant=tenant, backend=backend,
                sized=sized,
            )
        except DispatchError as exc:
            # Give the capacity straight back. Leaving the lease would hold a
            # slot no container will ever occupy until the dispatch deadline.
            #
            # The tenant gets `exc.code` plus the attempt id; the upstream API's
            # own message goes to the log line below and nowhere else. Backend
            # errors routinely echo the resource they were handed -- the tenant
            # service account, the job name, the secret names -- and
            # `task.last_error` is returned to the caller verbatim.
            # A refusal that is the PLATFORM's (an admission policy, the Job
            # quota) parks PLATFORM_REFUSED and spends no attempt (contract
            # request 123): BGD's rule is that a quota pauses work and never
            # fails it. Any other code -- an RBAC 403 included -- is retried
            # and capped as before.
            refused = exc.code in PLATFORM_REFUSAL_CODES
            returned = self._store.return_to_ready_after_failed_dispatch(
                task, lease, exc.code, correlation_id=lease.attempt_id,
                platform_refusal=refused,
            )
            if refused:
                self._metrics.platform_refusals.labels(code=exc.code).inc()
            if returned.applied and returned.target == TaskState.PARKED.value:
                report.platform_refused += 1
                parks = task.platform_refusals + 1
                # ERROR, every time: one refusal is a policy or quota refusing
                # work this scheduler was allowed to admit. Past the bound it
                # is trouble -- still parked, never FAILED, retried every ten
                # minutes -- and says so.
                log.error(
                    "platform refused dispatch task=%s attempt=%s backend=%s code=%s "
                    "parks=%s%s: %s",
                    task.id,
                    lease.attempt_id,
                    backend.value,
                    exc.code,
                    parks,
                    (
                        f" (past {PLATFORM_REFUSED_MAX_PARKS}: trouble, stays parked)"
                        if parks > PLATFORM_REFUSED_MAX_PARKS
                        else ""
                    ),
                    exc,
                )
            if not returned.applied:
                self._count_stale(returned, report)
            elif returned.target in (TaskState.FAILED.value, TaskState.CANCELLED.value):
                # CANCELLED when a cancel was requested meanwhile (store
                # `_returned`): terminal all the same, so its dependants are
                # resolved in this run too (#636).
                self._note_ended(task.id)
                if returned.target == TaskState.FAILED.value and task.workflow_id:
                    # This drain just wrote FAILED on a workflow step. Its
                    # siblings may be next in this very pass, and the verdict
                    # cached for the workflow was read before the failure
                    # existed.
                    self._workflow_verdicts.pop((task.tenant_id, task.workflow_id), None)
            report.dispatch_failures += 1
            self._metrics.dispatch_failures.labels(backend=backend.value).inc()
            log.warning(
                "dispatch failed task=%s attempt=%s backend=%s code=%s: %s",
                task.id,
                lease.attempt_id,
                backend.value,
                exc.code,
                exc,
            )
            # False: no capacity is held and nothing started, so this is not
            # progress. Reporting it as progress would keep the loop spinning on
            # a backend that is refusing dispatches.
            return False
        except Exception as exc:
            # Not a dispatch failure -- an unexpected one. Give the capacity back
            # and RE-RAISE. Swallowing it would turn a broken credential or a
            # broken Firestore into a scheduler that quietly admits, rolls back
            # and retries the whole queue on every tick with nothing red
            # anywhere; the leak is the bug being fixed here, not the crash.
            self._rollback_admission(task, lease, exc)
            raise

        marked = self._store.mark_dispatched(task, lease, execution, backend.value)
        if not marked.applied:
            # Usually its worker started first and already walked the task past
            # DISPATCHED. The dispatch itself succeeded -- the execution exists
            # -- so it is still counted below; only the state write was not made.
            self._count_stale(marked, report)
        report.dispatched += 1
        self._metrics.dispatched.labels(backend=backend.value).inc()
        # A SUCCESS LINE, because the absence of one is what hid a total outage.
        #
        # Only `dispatch failed` was ever logged. That reads as reasonable --
        # why log the happy path -- until you try to answer "has GKE_AUTOPILOT
        # ever dispatched?" and find the query returns nothing whether the
        # backend is healthy or has never worked once. On 2026-09-23 it had
        # never worked once: seven browser tasks, seven failures, over two days,
        # and the only way to establish that was to read task documents.
        #
        # `swarm_scheduler_dispatched_total{backend}` already counts this, and a
        # counter that stays at zero is exactly as invisible as a log line that
        # is never written unless something is watching it -- which nothing was.
        # The log line is the cheap half of the fix; the alert on the metric is
        # in terraform/modules/monitoring/alerts.tf.
        log.info(
            "dispatch ok task=%s attempt=%s backend=%s profile=%s execution=%s",
            task.id,
            lease.attempt_id,
            backend.value,
            profile.name,
            execution,
        )
        return True

    def _size_version(self, backend: Backend) -> sizes_mod.SizeVersion:
        """The runner-size version this pass applies. A Cloud Run profile
        never follows it (§4.7), so admitting one reads nothing."""
        if backend is not Backend.GKE_AUTOPILOT:
            return sizes_mod.DEFAULTS
        if self._sizes is None:
            self._sizes = sizes_mod.current(self._store)
        return self._sizes

    def _rollback_admission(self, task: Task, lease: Lease, cause: BaseException) -> None:
        """Return a lease taken by an admission whose follow-through blew up.

        Best effort, and it must never replace `cause` with its own failure: the
        operator needs the fault that started this, not the second one it caused.
        `return_to_ready_after_failed_dispatch` releases the lease and returns
        the task in ONE transaction and writes the event only after it commits,
        so a failing event write never keeps the pools -- and a lease whose task
        has since moved on is still released, while one a running worker holds,
        or one the reconciler has fenced and not yet released, is not.

        `scheduler_internal_error` is a stable code, not the exception text:
        `task.last_error` is returned to the tenant verbatim by
        `codec.task_to_api`, and an auth or transport error routinely echoes the
        service account, the job name or the secret names it was handed.
        """
        self._metrics.admission_rollbacks.inc()
        log.exception(
            "admission follow-through failed task=%s lease=%s attempt=%s: %s",
            task.id,
            lease.lease_id,
            lease.attempt_id,
            cause,
        )
        try:
            returned = self._store.return_to_ready_after_failed_dispatch(
                task, lease, "scheduler_internal_error", correlation_id=lease.attempt_id
            )
            if not returned.applied:
                self._count_stale(returned, None)
        except Exception:
            log.exception(
                "could not return the capacity for task=%s lease=%s; the reconciler's "
                "deadline sweep is now the only thing that will reclaim it",
                task.id,
                lease.lease_id,
            )

    def _count_cancel(self, report: DrainReport, *, reason: str) -> None:
        """One place that counts a cancel, so the report and the metric agree.

        The dependency sweep below used to cancel without counting at all: the
        drain that cancelled a workflow's `synthesis` step at 07:40:11Z on
        2026-09-24 reported `cancelled: 0`. Every cancel path now calls this.
        """
        report.cancelled += 1
        self._metrics.cancelled.labels(reason=reason).inc()

    def _count_stale(self, outcome: GuardedWrite, report: DrainReport | None) -> None:
        """One place that counts a write the store refused to force.

        `report` is None only on the rollback path, which has none; the metric
        still counts it there.
        """
        if report is not None:
            report.stale_writes += 1
        self._metrics.stale_writes.labels(
            write=outcome.write, reason=outcome.reason or "unknown"
        ).inc()

    def _park(
        self,
        task: Task,
        reason: ParkReason,
        report: DrainReport,
        *,
        detail: dict[str, Any],
        next_eligible_at: datetime | None = None,
        blocked_by: list[dict[str, Any]] | None = None,
    ) -> None:
        # A BUDGET_EXHAUSTED park names its budget (WAIT-RECORD, part of
        # #1782), which the "Why it's waiting" panel serves. Nothing parks on
        # it today -- there are no budgets (owner decision, 2026-10-01) -- so
        # this is where a future writer is held to it, before anything lands.
        budget = detail.get("budget")
        if reason is ParkReason.BUDGET_EXHAUSTED and not (isinstance(budget, str) and budget):
            raise ValueError("a BUDGET_EXHAUSTED park names its budget in detail['budget']")
        outcome = self._store.park(
            task, reason, detail=detail, next_eligible_at=next_eligible_at, blocked_by=blocked_by
        )
        if outcome.applied:
            self._metrics.parked.labels(reason=reason.value).inc()
            report.parked += 1
        else:
            self._count_stale(outcome, report)

    def _fail_unrunnable(
        self, task: Task, credential: CredentialAnswer, report: DrainReport,
        *, extra: dict[str, Any] | None = None,
    ) -> None:
        """End a READY or PARKED task whose MCP credential answer is terminal.

        FAILED with the answer's terminal code as the error
        (`mcp_credential_expired` for an expired or destroyed row,
        `mcp_credential_missing` for none), through `store.fail_before_lease`:
        no lease, no reservation, no attempt, and never retried. Its
        dependants are resolved in this run like any other end this drain
        writes, and a workflow step drops the cached verdict so its siblings
        next in this pass see the failure.
        """
        detail = {**credential.park_detail(), **(extra or {})}
        error = str(detail.get("code") or McpCode.CREDENTIAL_EXPIRED.value)
        outcome = self._store.fail_before_lease(task, error, detail=detail)
        if not outcome.applied:
            self._count_stale(outcome, report)
            return
        report.failed_unrunnable += 1
        self._note_ended(task.id)
        if task.workflow_id:
            self._workflow_verdicts.pop((task.tenant_id, task.workflow_id), None)

    def _park_awaiting_input(self, task: Task, report: DrainReport) -> None:
        """Park a READY await-input step AWAITING_INPUT, with its window (`store.park_awaiting_input`).

        The wait is held to `human_park_max_seconds`, the same 24 hours as the
        hand-off's (the owner's Q4); swarm-api restates the bound and its
        submission test holds the two equal.
        """
        limit = int(getattr(self._settings, "human_park_max_seconds", HUMAN_PARK_MAX_SECONDS))
        outcome = self._store.park_awaiting_input(task, max_seconds=limit)
        if outcome.applied:
            self._metrics.parked.labels(reason=ParkReason.AWAITING_INPUT.value).inc()
            report.parked += 1
        else:
            self._count_stale(outcome, report)

    def _promote_dependant(self, task: Task, *, reason: str, report: DrainReport) -> bool:
        """Promote a dependant whose dependencies are satisfied. True only if it is now READY.

        An await-input step is promoted like any other and then parked
        AWAITING_INPUT in the same call, guarded on the READY just written
        (docs/design/human-steps.md §4.4 step 1). It passes through READY for
        that one write and holds nothing during it. It is not returned as
        released, so the finish event's admission pass is never handed it.
        """
        patch_from, event_detail = self._runtime_resolution(task)
        if not self._promote(task, kind="dependency", detail={"reason": reason}, report=report,
                             patch_from=patch_from, event_detail=event_detail):
            return False
        if not is_control_plane(task.runner_profile):
            return True
        promoted = replace(task, state=TaskState.READY, park_reason=None, next_eligible_at=None)
        self._park_awaiting_input(promoted, report)
        return False

    def _cancel(
        self,
        task: Task,
        text: str,
        report: DrainReport,
        *,
        why: str,
        end_cause: EndCause | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        outcome = self._store.cancel(task, text, detail, end_cause=end_cause)
        if outcome.applied:
            self._count_cancel(report, reason=why)
            self._note_ended(task.id)
        else:
            self._count_stale(outcome, report)

    def _runtime_resolution(self, task: Task) -> tuple[
        Callable[[dict[str, Any]], dict[str, Any]] | None, Callable[[], dict[str, Any]] | None
    ]:
        """A `browser_if_ui` step's profile, resolved as it is readied (#2322, `runtime`).

        `(None, None)` for every other step, and for one already resolved.
        Otherwise the parents' changed files are read now -- every parent has
        SUCCEEDED, so what they changed is final -- and the swap is computed
        from the document as stored inside the promotion's own transaction:
        the step is READY on the resolved profile in the one write, before
        any admission has looked at it (invariants 2 and 3). The READY event
        records the profile and the reason (`runtime` in its detail).
        """
        policy = runtime_mod.policy_of(task.metadata)
        if policy is None or "resolved" in policy:
            return None, None
        resolution = runtime_mod.resolve(
            list(task.depends_on), policy,
            lambda task_id: self._store.task_document(task_id, tenant_id=task.tenant_id),
        )
        at = self._now()
        record: dict[str, Any] = {}

        def patch_from(stored: dict[str, Any]) -> dict[str, Any]:
            patch = runtime_mod.swap_patch(stored, resolution, at)
            record.clear()
            record.update(patch["metadata"][runtime_mod.RUNTIME_METADATA_KEY]["resolved"])
            return patch

        def event_detail() -> dict[str, Any]:
            return {"runtime": {key: value for key, value in record.items() if key != "at"}}

        log.info(
            "runtime resolved as the step is readied",
            extra={"task_id": task.id, "tenant_id": task.tenant_id,
                   "profile": resolution.profile, "reason": resolution.reason},
        )
        return patch_from, event_detail

    def _promote(
        self, task: Task, *, kind: str, detail: dict[str, Any], report: DrainReport | None,
        patch_from: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        event_detail: Callable[[], dict[str, Any]] | None = None,
    ) -> bool:
        """Promote one PARKED task; True only if the promotion was written."""
        outcome = self._store.promote_to_ready(
            task, detail=detail, patch_from=patch_from, event_detail=event_detail,
        )
        if not outcome.applied:
            self._count_stale(outcome, report)
            return False
        self._metrics.promoted.labels(kind=kind).inc()
        return True

    # -- on_step_failure --------------------------------------------------

    def _stop_for_failed_workflow(self, task: Task, report: DrainReport) -> bool:
        """Apply `on_step_failure: fail_workflow` from a step that has not started.

        Returns True when the caller must leave `task` alone for the rest of
        this drain. That happens when its workflow has failed and has now been
        swept, which cancelled `task` along with every other step of the
        workflow that had not started.

        HOW A FAILED WORKFLOW IS FOUND. From its not-started steps, never from
        the failure. Every touch point the scheduler already has on waiting
        work calls this: the dependency sweep, the credential sweep, the
        prewarm sweep and, above all, admission, which every step passes
        through before it can start. Asking "which steps failed recently"
        instead would mean a query over FAILED tasks, which grows with history,
        re-read on every drain to rediscover workflows that were finished long
        ago. Every park reason anything writes is read by a sweep that calls
        this (`_promote_scheduled_retries` and `_promote_manual_pauses` were
        the last two, `_sweep_awaiting_input` the one that only cancels). BUDGET_EXHAUSTED is the one reason no sweep reads, and
        nothing writes it: there are no budgets (owner, 2026-10-01). A
        workflow whose remaining steps are all parked on a reason that has not
        cleared -- a disabled tenant, a retry not yet due -- is swept when its
        sweep's moving window next reaches them (`_parked_window`: within
        ceil(parks / sweep size) drains); until then it holds no
        capacity, so it costs nothing (invariant 1).

        WHAT IT DOES NOT PROMISE: that nothing starts after the failure. The
        verdict is cached for the drain, so a failure a worker or the reconciler
        writes while a drain is running is not seen until the next drain, and
        no read can close the gap between reading the verdict and taking the
        lease anyway. A sibling admitted in that window holds capacity, so it
        is left to run to completion like any other running step. The window
        is the rest of the drain that was running when the failure was
        written, which `max_run_seconds` bounds. A failure the drain writes
        itself is seen at once (the DispatchError branch of `_admit_one`).
        """
        if not task.workflow_id:
            return False
        key = (task.tenant_id, task.workflow_id)
        if key in self._swept_workflows:
            # Swept earlier in this drain. `task` is a snapshot from before the
            # sweep, so acting on it could cancel twice or admit a step the
            # sweep has just cancelled.
            return True
        failed = self._workflow_failure(task)
        if failed is None:
            return False
        self._sweep_failed_workflow(failed, report)
        return True

    def _workflow_failure(self, task: Task) -> FailedWorkflow | None:
        """The workflow's failure verdict (`read_workflow_failure`), read at most once per drain."""
        workflow_id = task.workflow_id
        if not workflow_id:
            return None
        key = (task.tenant_id, workflow_id)
        if key not in self._workflow_verdicts:
            self._workflow_verdicts[key] = read_workflow_failure(
                self._store,
                task.tenant_id,
                workflow_id,
                enforced_since=self._settings.on_step_failure_enforced_since,
            )
        return self._workflow_verdicts[key]

    def _sweep_failed_workflow(self, failed: FailedWorkflow, report: DrainReport) -> None:
        """Cancel every step of `failed` that has not started, and nothing else.

        "Has not started" is SUBMITTED, QUEUED, READY or PARKED, and each
        cancel is re-checked inside a transaction
        (`SchedulerStore.cancel_if_not_started`), so a step leased by a
        concurrent drain since it was read is left to run. Steps holding
        capacity are not cancelled, not flagged with `cancel_requested` and not
        written to. They run to completion, and the workflow derives FAILED once
        they finish. Killing them would throw away work that checkpointing
        exists to keep, and releasing their capacity from here would decrement
        pools that a live container still occupies (invariant 1).

        Each query is bounded by `dependency_sweep_size`. A workflow with more
        not-started steps than that in one state is finished by the next drain,
        because the failed step is still FAILED and the verdict is re-read.
        """
        key = (failed.tenant_id, failed.workflow_id)
        self._swept_workflows.add(key)
        report.failed_workflows_swept += 1

        first = failed.failed_steps[0]
        label = first["step_id"] or first["task_id"]
        reason = (
            f"workflow step {label} is {first['state']} and on_step_failure is "
            f"{FAIL_WORKFLOW}, so steps that had not started were cancelled"
        )
        detail = {
            "workflow_id": failed.workflow_id,
            "on_step_failure": FAIL_WORKFLOW,
            "failed_steps": [dict(step) for step in failed.failed_steps],
        }

        cancelled = 0
        for state in _NOT_STARTED_STATES:
            for step in self._store.workflow_steps_in_state(
                failed.tenant_id,
                failed.workflow_id,
                state,
                self._settings.dependency_sweep_size,
            ):
                outcome = self._store.cancel_if_not_started(
                    step, reason, detail, end_cause=EndCause.WORKFLOW_SWEEP
                )
                if outcome.applied:
                    self._count_cancel(report, reason="workflow_failed")
                    self._note_ended(step.id)
                    cancelled += 1
                else:
                    # Leased by a concurrent drain, or finished, since the
                    # query read it: left alone, and counted like every other
                    # write the store refused to force.
                    self._count_stale(outcome, report)
        log.info(
            "workflow %s failed at %s (%s); on_step_failure=%s cancelled %d step(s) "
            "that had not started; steps holding capacity were left to finish",
            failed.workflow_id,
            label,
            first["state"],
            FAIL_WORKFLOW,
            cancelled,
        )

    # -- promotion sweeps -------------------------------------------------

    def _promote_dependencies(self, report: DrainReport) -> int:
        """Return DEPENDENCY_INCOMPLETE tasks to READY once every parent SUCCEEDED.

        This is the "returns to READY when the last parent succeeds" half of
        dependency resolution. It runs at the top of every drain, so a worker
        that dies immediately after writing SUCCEEDED cannot strand its
        children. The finish event (`release_dependants`, #636) applies the
        same rule to one parent's dependants the moment it ends; this sweep is
        what still releases them when that event is lost.

        A dependent whose parent did NOT succeed is cancelled here, and counted
        in `report.cancelled` (and `swarm_scheduler_cancelled_total`) exactly as
        `_admit_one` counts the same cancel. Returns the number PROMOTED -- a
        promotion the store refused to force (the task had moved on since this
        sweep listed it) is counted in `report.stale_writes` instead.

        `on_step_failure` is read here first. Under `fail_workflow` a FAILED or
        DEAD_LETTERED step anywhere in the workflow cancels every step that has
        not started, whether or not it depends on the failure
        (`_stop_for_failed_workflow`). Under `continue`, and for a CANCELLED
        parent under either setting, only the dependency rule below applies. It
        is transitive: a cancelled child is itself a "failed parent" to its own
        children, resolved in this drain (`_release_ended`, #636), or on the
        next when a bound stops it.
        """
        promoted = 0
        for task in self._parked_window(ParkReason.DEPENDENCY_INCOMPLETE):
            if self._resolve_dependency(task, report):
                promoted += 1
        return promoted

    def _resolve_dependency(self, task: Task, report: DrainReport) -> bool:
        """The dependency rule for one DEPENDENCY_INCOMPLETE park. True only if promoted.

        Shared by the sweep above and the finish event (`release_dependants`),
        so the two cannot come to disagree about when a dependant runs.
        """
        if self._stop_for_failed_workflow(task, report):
            return False
        if not self._pull_requests_merged(task, report):
            return False
        if owner_wait(task, "release_wait"):
            # An agent's release wait (AU-7, agent-autonomy §3.8): only
            # swarm-api's merge-wake tick, on a terminal release, returns it.
            return False
        if not task.depends_on:
            # Counted in the metric as well as the report. Incrementing only
            # the report here made `swarm_scheduler_promoted{kind=
            # "dependency"}` disagree with DrainReport.promoted_dependencies,
            # so a dashboard built on the metric under-counted promotions.
            return self._promote_dependant(task, reason="no_dependencies", report=report)
        parents = self._store.parent_ends(task.depends_on, tenant_id=task.tenant_id)
        states = _dependency_states(task, parents)
        failed = [tid for tid, state in states.items() if state in _FAILED_PARENT_STATES]
        if failed:
            self._cancel(
                task,
                "an upstream workflow step did not succeed",
                report,
                why="failed_parent",
                detail={"failed_parents": failed},
                end_cause=_parent_cause(parents, failed),
            )
            return False
        if all(states.get(tid) is TaskState.SUCCEEDED for tid in task.depends_on):
            if self._held_for_control_publish(task, parents):
                report.held_dependencies += 1
                return False
            return self._promote_dependant(task, reason="dependencies_satisfied", report=report)
        return False

    def _pull_requests_merged(self, task: Task, report: DrainReport) -> bool:
        """The pull-request gate of a step whose workflow waits on `after` (SPEED-D3).

        True when the step carries no gate, or its gate says every pull
        request merged: the dependency rule below then applies as it always
        did. False otherwise, and the step is left alone:

          * still waiting -- the step stays PARKED, holding no lease, no pool
            count and no execution (invariant 1), counted in
            `report.held_pr_gates`;
          * a pull request closed without merging -- the step is ended
            DEAD_LETTERED (PARKED -> FAILED, contract request 99, is kept for
            a task that can never run, `_fail_unrunnable`), naming it, or CANCELLED when a cancel was asked for. Its
            workflow has failed: under `fail_workflow` the next touch sweeps
            the rest, and under either policy its dependants are cancelled as
            a failed parent's.

        The scheduler never reads GitHub. swarm-api's wake tick writes the
        gate (`swarm_api.prgate.wake_tenant`); this reads only its `state`.
        """
        hold = after_prs_hold(task.metadata)
        if hold is None or hold[0] == AFTER_PRS_MERGED:
            return True
        if hold[0] == AFTER_PRS_CLOSED:
            gate = (task.metadata or {}).get(AFTER_PRS_METADATA_KEY) or {}
            repository = gate.get("repository") if isinstance(gate, dict) else None
            where = f" of {repository}" if isinstance(repository, str) and repository else ""
            text = (
                f"pull request #{hold[1]}{where} was closed without being merged; "
                "this workflow waited for it to merge (after) and will not start"
            )
            if task.cancel_requested:
                self._cancel(
                    task, text, report, why="cancel_requested",
                    end_cause=children_mod.cancel_end_cause(task.metadata),
                )
                return False
            outcome = self._store.dead_letter_parked(
                task, text, detail={"park_reason": ParkReason.DEPENDENCY_INCOMPLETE.value,
                                    "after_pr_closed": hold[1]},
            )
            if outcome.applied:
                report.dead_lettered += 1
                self._note_ended(task.id)
            else:
                self._count_stale(outcome, report)
            return False
        report.held_pr_gates += 1
        return False

    def _held_for_control_publish(self, task: Task, parents: dict[str, ParentEnd]) -> bool:
        """Leave a MERGE verdict's integrator PARKED while swarm-api may publish it (#748).

        swarm-api receives the same `task_finished` wake and, for `integrate`'s
        gated integrator whose review said MERGE, opens the pull request itself
        and ends the step without a worker (`swarm_api.verdictpublish`).
        Promoting it here first would start the container that path saves.
        So the step stays PARKED -- no lease, no pool count, no execution
        (invariant 1) -- until one of:

          * swarm-api DECLINES it (`metadata.control_publish.state` is
            anything but `claimed`) and rings this parent's wake again: it is
            promoted on that wake, as before;
          * swarm-api's CLAIM is older than `CONTROL_PUBLISH_CLAIM_SECONDS`
            (its holder died mid-publish), or is dated in the future (the
            document is tenant-writable): promoted;
          * there is NO marker and `control_publish_hold_seconds` have passed
            since the last parent ended: swarm-api never got the wake, so the
            next drain promotes it as before.

        Nothing is held while the hold is 0 (the default) or while the frozen
        contract does not let a step end SUCCEEDED from PARKED (contract
        request 52): then swarm-api publishes nothing, and holding would only
        delay the worker. A SUCCEEDED step is not PARKED and never reaches
        this.
        """
        hold = self._settings.control_publish_hold_seconds
        if hold <= 0 or not held_for_control_publish(task.metadata):
            return False
        if not can_transition(TaskState.PARKED, TaskState.SUCCEEDED):
            return False
        now = self._now()
        marker = (task.metadata or {}).get(CONTROL_PUBLISH_METADATA_KEY)
        if marker is not None:
            if not isinstance(marker, dict) or marker.get("state") != CONTROL_PUBLISH_CLAIMED:
                return False
            claimed_at = as_datetime(marker.get("claimed_at"))
            return (
                claimed_at is not None
                and claimed_at <= now
                and now - claimed_at < timedelta(seconds=CONTROL_PUBLISH_CLAIM_SECONDS)
            )
        ended = [end.completed_at for end in parents.values() if end.completed_at is not None]
        if not ended:
            return False
        return now - max(ended) < timedelta(seconds=hold)

    def _promote_credentials(self, report: DrainReport) -> int:
        """Re-ready tasks that admission would now let run.

        The same question admission asks (`credential_for`): the tenant has
        since registered the missing key, or has since been lent -- or has
        registered -- a pool account that serves the profile. Asked in any
        other words, a task this sweep promotes could be parked again by
        admission a moment later, or one admission would run could wait here
        for a key nobody needs to register.

        A step of a failed `fail_workflow` workflow is cancelled here instead,
        which can happen long before its key is registered. `report` is
        required, not optional: that cancel, and any promotion the store
        refused to force, is counted in it.

        A POOL ANSWER WAITS FOR THE PARK'S OWN INSTANT. "Could this tenant run
        on paper" is not what a WORKER's CREDENTIAL_MISSING park is about. A
        worker that could not reach the broker falls back to the tenant's key,
        finds none, and parks for an hour (`lifecycle._park_credential_missing`);
        one the broker refused, or that could read no account, parks on
        `_park_no_account`'s fallback. The accounts in Firestore still say the
        pool serves the tenant, so promoting on that answer -- which also
        clears `next_eligible_at` -- started the task again on every drain, a
        container that could only park, for as long as the cause lasted. So
        when the answer is ACCOUNT_POOL and the park carries an instant still
        ahead, the task waits for it. Admission's own parks set no instant and
        are unaffected. A key registered meanwhile makes the answer TENANT_KEY,
        which promotes at once: the wait never delays a fix an admin made.

        A GITHUB CONNECTION IS PART OF THE ANSWER (OB6). A task admission
        parked because its submitter's connection was `refresh_failed`, or
        missing while a grant names them, stays parked until the connection is
        `active` again -- the person reconnected -- and is then promoted like
        any other. If the person instead disconnected, swarm-api deleted their
        grants, the task no longer needs the user slot, and it is promoted to
        run as it would have before onboarding.

        SO ARE THE WORKSPACE'S SECRET GRANTS (SEC-ADMIT-PARK). A task parked
        for a required slot with no enabled grant -- by admission, or by its
        worker's backstop (`workspace-secret:<slots>`) -- is promoted once
        every required slot has one, and only if the key and the connection
        still answer yes: a park for a provider key or an account that is
        still missing stays parked whatever the grants say.
        """
        promoted = 0
        now = self._now()
        for task in self._parked_window(ParkReason.CREDENTIAL_MISSING):
            if self._stop_for_failed_workflow(task, report):
                continue
            tenant = self._tenant(task.tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            profile = RUNNER_PROFILES.get(task.runner_profile)
            if profile is None:
                continue
            credential = credential_for(
                profile, tenant, self._pool, task=task, forge=self._forge,
                secrets=self._secrets, tenant_of=self._tenant,
            )
            if credential.terminal:
                # Parked before contract request 99 existed, or parked for a
                # server an admin disabled while its credential then expired:
                # either way it can never run, so it ends here, PARKED -> FAILED.
                self._fail_unrunnable(task, credential, report)
                continue
            if credential.mcp is not None and credential.mcp.waits_for_credential:
                # Waiting for its caller to re-store a workflow's MCP
                # credential (§4.10, BGD 2026-10-26). Promoted below once the
                # row is live; past the limit it ends PARKED -> FAILED with
                # the credential's code. Either way nothing was leased.
                limit = int(getattr(self._settings, "mcp_credential_park_max_seconds",
                                    MCP_CREDENTIAL_PARK_MAX_SECONDS))
                since = mcp_park_since(task)
                if since is not None and now - since >= timedelta(seconds=limit):
                    self._fail_unrunnable(task, credential, report, extra={
                        "waited_since": since.isoformat(), "limit_seconds": limit})
                continue
            if (
                credential.source is CredentialSource.ACCOUNT_POOL
                and task.next_eligible_at is not None
                and task.next_eligible_at > now
            ):
                continue
            if credential.runnable:
                if self._promote(
                    task,
                    kind="credential",
                    detail=credential.promote_detail(),
                    report=report,
                ):
                    promoted += 1
        return promoted

    def _parked_window(self, reason: ParkReason, *, sweep: str = "") -> list[Task]:
        """The next `dependency_sweep_size` parks on `reason`, resuming where the last drain stopped.

        Every park sweep reads through here (#1822). Each leaves a park in
        place while what parked it has not cleared -- a retry not yet due, a
        tenant still disabled, a parent still running. With a fixed
        window, that many such parks ahead in the index would hide every park
        behind them for ever, which is the defect these sweeps exist to end.
        So the window moves: a full page leaves its last id as the next drain's
        start, and a short page sends the next drain back to the top. Every
        park is looked at within ceil(parks / sweep size) drains.

        `sweep` names the cursor when two sweeps read one reason, so each
        moves its own window.
        """
        size = self._settings.dependency_sweep_size
        key = (sweep, reason)
        page = self._store.parked_page(reason, size, self._park_cursors.get(key))
        self._park_cursors[key] = page[-1].id if len(page) >= size else None
        return page

    def _promote_scheduled_retries(self, report: DrainReport) -> int:
        """Return SCHEDULED_RETRY parks to READY once their `next_eligible_at` has passed.

        The worker writes this park when it is SIGTERMed
        (`lifecycle._handle_interruption`), with `next_eligible_at=now`, and
        until this sweep nothing ended it: the task, and any workflow it was a
        step of, sat PARKED for ever. A park with no instant at all is due: the
        reason means "retry", and nothing would ever supply a later time.

        THE RETRY CAP DECIDES WHAT AN INTERRUPTED LAST ATTEMPT BECOMES.
        Admission does not check `max_attempts`, so READY on a task that has
        used its last attempt would lease one more than the cap -- the defect
        `retries_exhausted` exists for. Such a task is ended DEAD_LETTERED
        (PARKED -> FAILED, contract request 99, is kept for a task that can
        never run), or CANCELLED when a cancel was requested, and counted in `report.dead_lettered` / `cancelled`.

        Promotion is `promote_to_ready`, guarded on the state and park reason
        this sweep read, so two drains racing promote it once. It writes the
        task alone: no lease, no pool count. Only admission, later, takes
        capacity (invariant 1).
        """
        promoted = 0
        now = self._now()
        for task in self._parked_window(ParkReason.SCHEDULED_RETRY):
            if self._stop_for_failed_workflow(task, report):
                continue
            eligible_at = task.next_eligible_at
            if eligible_at is not None and eligible_at > now:
                continue
            if task.retries_exhausted():
                self._end_exhausted_retry(task, report)
                continue
            if self._promote(
                task,
                kind="scheduled_retry",
                detail={
                    "reason": "retry_due",
                    "park_reason": ParkReason.SCHEDULED_RETRY.value,
                    "was_eligible_at": eligible_at.isoformat() if eligible_at else None,
                },
                report=report,
            ):
                promoted += 1
        return promoted

    def _promote_platform_refusals(self, report: DrainReport) -> int:
        """Return PLATFORM_REFUSED parks to READY once `next_eligible_at` has passed.

        The failed-dispatch path writes this park when the platform refused
        the dispatch (an admission policy, the namespace's Job quota), with
        its lease released and `next_eligible_at` backed off
        (`store.platform_refusal_backoff`: 30 s doubling, at most ten
        minutes). Contract request 123, docs/design/dispatch-refusals.md.

        THERE IS NO CAP HERE, ON PURPOSE. The refusal spent no attempt, and
        BGD's rule (2026-09-14) is that a quota pauses work and never fails
        it: a task refused past `PLATFORM_REFUSED_MAX_PARKS` times is still
        promoted, every ten minutes, until the quota frees or the policy is
        corrected, and is reported as trouble meanwhile. A park on a task
        whose attempts were ALREADY spent before it was refused is ended as
        `_end_exhausted_retry` ends one, since admission does not check the
        cap.

        Promotion is `promote_to_ready`, guarded on the state and park reason
        this sweep read, so two drains racing promote it once. It writes the
        task alone: no lease, no pool count. Only admission, later, takes
        capacity (invariant 1).
        """
        promoted = 0
        now = self._now()
        for task in self._parked_window(ParkReason.PLATFORM_REFUSED):
            if self._stop_for_failed_workflow(task, report):
                continue
            eligible_at = task.next_eligible_at
            if eligible_at is not None and eligible_at > now:
                continue
            if task.retries_exhausted():
                self._end_exhausted_retry(
                    task,
                    report,
                    reason=ParkReason.PLATFORM_REFUSED,
                    text=(
                        f"refused by the platform with its attempts already spent "
                        f"({task.attempt_count} of {task.max_attempts})"
                    ),
                )
                continue
            if self._promote(
                task,
                kind="platform_refused",
                detail={
                    "reason": "platform_refusal_retry_due",
                    "park_reason": ParkReason.PLATFORM_REFUSED.value,
                    "platform_refusals": task.platform_refusals,
                    "was_eligible_at": eligible_at.isoformat() if eligible_at else None,
                },
                report=report,
            ):
                promoted += 1
        return promoted

    def _end_exhausted_retry(
        self,
        task: Task,
        report: DrainReport,
        *,
        reason: ParkReason = ParkReason.SCHEDULED_RETRY,
        text: str | None = None,
    ) -> None:
        text = text or (
            f"interrupted on its last attempt ({task.attempt_count} of "
            f"{task.max_attempts}); retries exhausted"
        )
        if task.cancel_requested:
            self._cancel(
                task,
                text,
                report,
                why="cancel_requested",
                end_cause=children_mod.cancel_end_cause(task.metadata),
            )
            return
        outcome = self._store.dead_letter_parked(
            task, text, detail={"park_reason": reason.value}
        )
        if outcome.applied:
            report.dead_lettered += 1
            self._note_ended(task.id)
        else:
            self._count_stale(outcome, report)

    def _promote_ci_waits(self, report: DrainReport) -> int:
        """Return CI_PENDING parks to READY on the wake marker or the fallback.

        docs/merge-step.md "Revised 2026-10-06" §1. The scheduler promotes; it
        never reads GitHub -- it holds no forge token and must not, so a
        GitHub outage can never slow admission. A merge step parked waiting
        for its pull request's checks is due when either:

          * `metadata.merge_wait.wake_requested_at` is set: swarm-api's wake
            tick read the checks settled. Any value counts: the marker is on
            a tenant-writable document, and a forged one costs one early
            wake, whose worker re-reads every fact and trusts nothing the
            tick saw;
          * `next_eligible_at` has passed: the worker set it to the park
            instant plus `MERGE_CI_FALLBACK_SECONDS`, so a dead tick or a
            broken token never strands a merge. A park with no instant at all
            is due, as `_promote_scheduled_retries` reads one.

        A park on a task that has used its last attempt -- past the worker's
        `MERGE_CI_MAX_WAKES` a park is not refunded -- is ended at once,
        DEAD_LETTERED (or CANCELLED when a cancel was requested), exactly as
        `_end_exhausted_retry` ends a SCHEDULED_RETRY park: admission does not
        check the cap, so READY would lease one attempt too many.

        Promotion is `promote_to_ready`, guarded on the state and park reason
        this sweep read, so two drains racing promote it once. It writes the
        task alone: no lease, no pool count. Only admission, later, takes
        capacity (invariants 1 and 3).
        """
        promoted = 0
        now = self._now()
        for task in self._parked_window(ParkReason.CI_PENDING):
            if self._stop_for_failed_workflow(task, report):
                continue
            if task.retries_exhausted():
                self._end_exhausted_retry(
                    task,
                    report,
                    reason=ParkReason.CI_PENDING,
                    text=(
                        f"waited for its pull request's checks on its last attempt "
                        f"({task.attempt_count} of {task.max_attempts}); retries exhausted"
                    ),
                )
                continue
            wait = (task.metadata or {}).get(MERGE_WAIT_METADATA_KEY)
            marked = isinstance(wait, dict) and bool(wait.get(MERGE_WAKE_MARKER))
            eligible_at = task.next_eligible_at
            if not marked and eligible_at is not None and eligible_at > now:
                continue
            if self._promote(
                task,
                kind="ci_wait",
                detail={
                    "reason": "wake_requested" if marked else "fallback_due",
                    "park_reason": ParkReason.CI_PENDING.value,
                    "was_eligible_at": eligible_at.isoformat() if eligible_at else None,
                },
                report=report,
            ):
                promoted += 1
        return promoted

    # -- the live-browser hand-off (docs/design/live-browser.md §4.3) --------

    def _expire_human_waits(self, report: DrainReport) -> int:
        """Dead-letter a PARKED(HUMAN_REQUIRED) task nobody came back for.

        THIS SWEEP NEVER PROMOTES. A hand-off parks when nobody took control
        within the hold, and only a person -- swarm-api's `human-ready` -- may
        return it to READY: re-admitting it with nobody there to sign in would
        cost a cold pod start and park again. So the only thing done here is
        the end: past `human_park_max_seconds` since the park it is ended
        DEAD_LETTERED (PARKED -> FAILED, contract request 99, is kept for a
        task that can never run), with `human_wait_expired`. A park on a task that has used its last attempt
        -- past the worker's `max_human_resumes` a park is not refunded -- is
        ended at once, as `_end_exhausted_retry` ends a SCHEDULED_RETRY park,
        because `human-ready` refuses it and nothing else can resume it.

        The park instant is `next_eligible_at`, which the worker's park writes
        as the moment it parked (as the child await's does), with `updated_at`
        standing in for a park that carries none. Returns how many it ended past
        the maximum; the last-attempt ends are counted by `_end_exhausted_retry`.

        Two human parks are not this sweep's to expire: an agent's blocking
        question (`question_wait`, swarm-api ends it at 72 hours), and a merge
        held by its review's blocking owner questions (`owner_questions`,
        #2070), which swarm-api's answer route returns to READY once none is
        open and which never expires (owner decision 2026-10-23, "Resume on
        answer, no expiry"). Neither holds a lease while it waits (invariant 1).
        """
        ended = 0
        now = self._now()
        limit = int(getattr(self._settings, "human_park_max_seconds", HUMAN_PARK_MAX_SECONDS))
        reason = ParkReason.HUMAN_REQUIRED
        for task in self._parked_window(reason):
            if self._stop_for_failed_workflow(task, report):
                continue
            if task.retries_exhausted():
                # Counted into `report.dead_lettered` by `_end_exhausted_retry`
                # itself, so not into this sweep's return.
                self._end_exhausted_retry(
                    task,
                    report,
                    reason=reason,
                    text=(
                        f"waited for a person on its last attempt ({task.attempt_count} of "
                        f"{task.max_attempts}); retries exhausted"
                    ),
                )
                continue
            if owner_wait(task, "question_wait"):
                # A blocking question (AU-7, §3.9) is held for 72 hours and
                # ended by swarm-api with "owner question unanswered".
                continue
            if owner_wait(task, "owner_questions"):
                # A merge held by its review's blocking owner questions
                # (#2070) is woken by swarm-api's answer route once none is
                # open, and never expires (owner decision 2026-10-23,
                # "Resume on answer, no expiry"). It holds no lease while
                # it waits (invariant 1). Its pull request merged or closed
                # outside the workflow wakes it too (#2441).
                self._wake_owner_hold(task, report)
                continue
            parked_at = task.next_eligible_at or task.updated_at
            if parked_at + timedelta(seconds=limit) > now:
                continue
            text = (
                f"{HUMAN_WAIT_EXPIRED}: nobody said they were ready to sign in within "
                f"{limit} seconds of the park"
            )
            if task.cancel_requested:
                self._cancel(
                    task,
                    text,
                    report,
                    why="cancel_requested",
                    end_cause=children_mod.cancel_end_cause(task.metadata),
                )
                continue
            outcome = self._store.dead_letter_parked(
                task, text, detail={"park_reason": reason.value, "cause": HUMAN_WAIT_EXPIRED}
            )
            if outcome.applied:
                ended += 1
                self._note_ended(task.id)
            else:
                self._count_stale(outcome, report)
        return ended

    def _wake_owner_hold(self, task: Task, report: DrainReport) -> bool:
        """Return an owner-question hold to READY on the wake tick's marker (#2441).

        MERGE-OUTSIDE-ENDS (BGD 2026-10-26): a merge held on owner questions
        whose pull request was merged or closed outside the workflow finishes
        on its own. swarm-api's wake tick reads the pull request -- the
        scheduler holds no forge token -- and marks `merge_wait.owner_hold`
        with `wake_requested_at` and the park instant it read. Only a mark
        bound to THIS park counts (`owner_hold_marked`): a mark that outlived
        an earlier park, which the worker parked again on an open pull
        request, would otherwise wake it on every drain. A forged mark costs
        one early wake, whose worker re-reads the pull request and parks
        again on an open one. Promotion writes the task alone; only
        admission takes capacity (invariants 1 and 3), and the next attempt
        is fenced as any other (invariant 5).
        """
        reason = owner_hold_marked(task)
        if reason is None:
            return False
        return self._promote(
            task,
            kind="owner_hold_wake",
            detail={
                "reason": "wake_requested",
                "wake_reason": reason,
                "park_reason": ParkReason.HUMAN_REQUIRED.value,
            },
            report=report,
        )

    def _sweep_awaiting_input(self, report: DrainReport) -> None:
        """Apply `fail_workflow` to await-input steps waiting for a person. Never promotes, never expires.

        A PARKED(AWAITING_INPUT) step is ended by swarm-api alone: on the
        answer, on its declared default or on its expiry, which honours
        `on_expire` (docs/design/human-steps.md §4.6). So nothing here returns
        it to READY, and `_expire_human_waits` keeps its HUMAN_REQUIRED filter.
        What the scheduler does own is the failed-workflow rule (§4.4 step 3):
        a waiting step is a step that has not started, and when it is the last
        one in its workflow no other sweep would ever reach it. Through
        `_stop_for_failed_workflow`, a failed sibling under `fail_workflow`
        cancels it, exactly as it cancels any step that has not started. Under
        `continue` it is left waiting. The window moves (`_parked_window`), so
        every waiting step is looked at within ceil(waits / sweep size)
        drains. Nothing is leased or released (invariant 1).
        """
        for task in self._parked_window(ParkReason.AWAITING_INPUT):
            self._stop_for_failed_workflow(task, report)

    # -- child tasks (docs/design/child-tasks.md) ---------------------------

    def _promote_child_awaits(self, report: DrainReport) -> int:
        """Return CHILDREN_INCOMPLETE parks to READY once their children are done.

        §3.3 step 6, beside `_promote_dependencies` for the reason that sweep
        gives: nothing waits for a child to announce itself, so a worker that
        dies right after its last child's success cannot strand the parent.
        "Done" is every child terminal and every SUCCEEDED child's manifest
        written. Past `child_await_max_seconds` the outstanding children are
        cancelled with `why: await_expired` (§5 F7); the parent is promoted on
        the drain that finds them terminal.

        A parent that has used its last attempt -- its await counted, past
        `max_child_await_resumes` (§5 F14) -- is dead-lettered AS SOON AS the
        sweep finds it, whatever its children are doing, as
        `_end_exhausted_retry` does for a SCHEDULED_RETRY park: admission does
        not check the cap, and no resume is coming that could read its
        children's results. Ending it then, rather than after they finish, is
        what lets the cascade sweep cancel them as `parent_ended` (§5 F6)
        instead of running them for a consumer that is gone. Promotion writes
        READY only (invariant 1).
        """
        promoted = 0
        now = self._now()
        db = self._store.db
        for task in self._parked_window(ParkReason.CHILDREN_INCOMPLETE):
            if task.retries_exhausted():
                text = (
                    f"awaited its children on its last attempt ({task.attempt_count} of "
                    f"{task.max_attempts}); retries exhausted"
                )
                outcome = self._store.dead_letter_parked(
                    task, text, detail={"park_reason": ParkReason.CHILDREN_INCOMPLETE.value}
                )
                if outcome.applied:
                    report.dead_lettered += 1
                    self._note_ended(task.id)
                else:
                    self._count_stale(outcome, report)
                continue
            decision = children_mod.decide_await(
                children_mod.children_of(db, task.tenant_id, task.id)
            )
            expired = now >= children_mod.await_deadline(
                task, self._settings.child_await_max_seconds
            )
            if decision.outstanding and expired:
                for child_id in decision.outstanding:
                    report.children_cascaded += self._cascade_one(
                        child_id, task, why=children_mod.AWAIT_EXPIRED, report=report
                    )
                continue
            if not decision.settled and not expired:
                continue
            if self._promote(
                task,
                kind="child_await",
                detail={
                    "reason": "children_done" if decision.settled else "await_expired",
                    "park_reason": ParkReason.CHILDREN_INCOMPLETE.value,
                    "unstaged": list(decision.unstaged),
                },
                report=report,
            ):
                promoted += 1
        return promoted

    def _sweep_child_cascade(self, report: DrainReport) -> int:
        """Cancel every live child whose parent is terminal or cancel-requested.

        §3.4 step 2: the API's cascade is for responsiveness, this is what
        guarantees it -- an API that died between the parent and its third
        child strands nothing, within one safety tick. One page per drain,
        resuming where the last stopped, so every live child is looked at
        within ceil(children / sweep size) drains. Each parent is read once per
        page; a child in another tenant than its parent is left alone (§5 F11).
        """
        size = self._settings.dependency_sweep_size
        page = children_mod.live_children_page(
            self._store.db, limit=size, after=self._child_cursor
        )
        self._child_cursor = (
            (page[-1].parent_task_id or "", page[-1].id) if len(page) >= size else None
        )
        parents: dict[str, Task | None] = {}
        changed = 0
        for child in page:
            parent_id = child.parent_task_id or ""
            if parent_id not in parents:
                parents[parent_id] = self._store.get_task(parent_id)
            parent = parents[parent_id]
            if parent is None or parent.tenant_id != child.tenant_id:
                continue
            why = children_mod.cascade_reason(parent)
            if why is not None:
                changed += self._cascade_one(child.id, parent, why=why, report=report)
        return changed

    def _cascade_one(self, child_id: str, parent: Task, *, why: str, report: DrainReport) -> int:
        result = children_mod.cancel_child(
            self._store.db,
            child_id,
            tenant_id=parent.tenant_id,
            parent_task_id=parent.id,
            why=why,
            now=self._now(),
        )
        if result == "cancelled":
            self._count_cancel(report, reason="child_cascade")
            self._note_ended(child_id)
        return 1 if result is not None else 0

    def _promote_manual_pauses(self, report: DrainReport) -> int:
        """Return MANUAL_PAUSE parks to READY once what paused them has cleared.

        `_admit_one` writes this park for a tenant that is missing or disabled
        and for a runner profile the catalogue no longer has. Nothing read it
        back, so re-enabling the tenant did not start its work. The sweep asks
        the same questions admission asked, and promotes only when every one
        is now answered:

          * the profile is in the catalogue;
          * the tenant exists and is enabled;
          * no pool the task must clear is switched off. Compared as
            `enabled is False`, never by defaulting: a pool document with no
            `enabled` field is on, as admission reads it, and only an explicit
            False is a pause (`resume-swarm.sh` is what writes it back).

        A paused pool does not by itself park a task -- admission leaves the
        task READY with a MANUAL_PAUSE blocker -- so the pool question is the
        one that holds an operator's pause in place until it is lifted, rather
        than re-readying work into a pool that will refuse it.

        Guarded like every promotion; it writes the task alone (invariant 1).
        """
        parked = self._parked_window(ParkReason.MANUAL_PAUSE)
        if not parked:
            return 0
        pools = self._store.pools()
        promoted = 0
        for task in parked:
            if self._stop_for_failed_workflow(task, report):
                continue
            profile = RUNNER_PROFILES.get(task.runner_profile)
            if profile is None:
                continue
            tenant = self._tenant(task.tenant_id)
            if tenant is None or tenant.enabled is False:
                continue
            required = pool_names_for(
                tenant_id=task.tenant_id,
                provider=task.provider,
                resource_class=task.resource_class,
                runner_profile=task.runner_profile,
                backend=resolve_backend(profile).value,
            )
            paused = [name for name in required if name in pools and pools[name].enabled is False]
            if paused:
                continue
            if self._promote(
                task,
                kind="manual_pause",
                detail={"reason": "pause_lifted", "park_reason": ParkReason.MANUAL_PAUSE.value},
                report=report,
            ):
                promoted += 1
        return promoted

    def _prewarm(self, report: DrainReport) -> int:
        """Promote quota-parked work just before its window reopens.

        BOUNDED by `prewarm_max_agents`, and safe by construction: the only
        tasks promoted EARLY are ones with a `provider:{p}:tenant:{t}` pool,
        whose `quota_derived_limit` holds admission shut, so if the estimate is
        wrong admission simply refuses and the task stays READY -- which costs
        nothing (invariant 1). No container is started ahead of time; that would
        be spending money on a guess.

        A step of a failed `fail_workflow` workflow met here is cancelled rather
        than promoted.

        THIS IS ALSO WHERE THESE PARKS END, not only where they end early.
        Nothing else in the platform returns a PROVIDER_QUOTA_EXHAUSTED,
        PROVIDER_COOLDOWN or PROVIDER_OUTAGE park to READY: the reconciler has
        no rule for parked tasks. So the pool guard below applies to EARLY
        promotion only. A task whose `next_eligible_at` has passed is promoted
        whether or not the guard pool exists, because by then there is no
        window left to promote it ahead of. Before this, a task with no
        `provider:{p}:tenant:{t}` pool stayed PARKED for ever. Terraform
        creates that pool only from a tenant's declared `providers`, and a
        tenant that runs on a lent pool account declares none, so the worker's
        wait on a spent, unobserved or paused account
        (`lifecycle._park_no_account`) never ended (#171 review). A park with
        no instant at all is still promoted only under a guard: without one
        there is nothing to say the window has reopened.

        NOT FIXED HERE: because the end of the wait lives in this sweep,
        `enable_prewarm=False` or a zero `prewarm_max_agents` stops these parks
        ending at all, not only ending early. docs/quota-management.md section
        5 says so.

        A NO-ACCOUNT PARK IS PROMOTED ONLY WHEN AN ACCOUNT CAN TAKE IT (#2222).
        A park the worker wrote at `account_assign` waits on the pool, not on
        the provider pool's ceiling, and promoting it because its instant
        passed started a container per park to ask the broker the same
        question: at 10:10Z on 2026-10-24, one 5-hour reset was the instant
        of 28 of them. So such a park is asked about with the broker's own
        `choose()` and the slots this run already handed out
        (`AccountPool.claim`), and left PARKED -- counted in `held_by_pool`,
        and looked at again on the next drain -- when no account can take it.
        A park whose tenant no account serves any more is promoted as before:
        its worker falls back to the tenant's own key. These parks are read
        through `_parked_window`, so a backlog of held parks larger than one
        page of `dependency_sweep_size` no longer hides the rest (#1822). When
        the budget stops a page part-way, the cursor goes back to the last
        park looked at, so the next drain starts at the first one this drain
        did not reach rather than a page beyond it.
        """
        budget = max(0, self._settings.core.prewarm_max_agents)
        if budget == 0:
            return 0
        now = self._now()
        horizon = now + timedelta(seconds=self._settings.core.prewarm_lead_seconds)
        # Read once: the guard below needs to know which provider pools exist.
        pools = self._store.pools()
        promoted = 0
        for reason in PREWARM_REASONS:
            if promoted >= budget:
                break
            key = ("prewarm", reason)
            resume = self._park_cursors.get(key)
            for task in self._parked_window(reason, sweep="prewarm"):
                if promoted >= budget:
                    self._park_cursors[key] = resume
                    break
                resume = task.id
                if self._stop_for_failed_workflow(task, report):
                    continue
                eligible_at = task.next_eligible_at
                if eligible_at is not None and eligible_at > horizon:
                    continue
                due = eligible_at is not None and eligible_at <= now
                guard = f"provider:{task.provider}:tenant:{task.tenant_id}"
                if not due and (not task.provider or guard not in pools):
                    # Without that pool there is nothing carrying the provider's
                    # quota cap, so promoting EARLY would let the task be
                    # admitted before the window reopens -- a container started
                    # on a guess. It waits for its own instant instead.
                    continue
                chosen: Account | None = None
                if reason is ParkReason.PROVIDER_QUOTA_EXHAUSTED and account_park_provider(task):
                    profile = RUNNER_PROFILES.get(task.runner_profile)
                    question = self._pool_question(task, profile) if profile else None
                    if question is not None:
                        candidates, _provider, model_class = question
                        chosen = choose(candidates, task.tenant_id, now, model_class=model_class)
                        if chosen is None:
                            report.held_by_pool += 1
                            continue
                if self._promote(
                    task,
                    kind="prewarm",
                    detail={"reason": "window_reopened" if due else "prewarm",
                            "park_reason": reason.value,
                            "was_eligible_at": eligible_at.isoformat() if eligible_at else None},
                    report=report,
                ):
                    promoted += 1
                    if chosen is not None:
                        self._pool.claim(
                            chosen.account_id, task_id=task.id, tenant_id=task.tenant_id,
                            now=now,
                        )
        return promoted

    def _pool_question(
        self, task: Task, profile: Any
    ) -> tuple[list[Account], str, str | None] | None:
        """The accounts the worker would ask the broker among, or None when it would not ask.

        The worker asks whenever a broker is configured, the profile takes a
        subscription token and an account serves the tenant, the provider and
        the step's model class -- whatever key the tenant also has
        (`lifecycle._lease_account`, and the broker's assign route, which
        answers `no_accounts_registered` when none serves, the one answer
        that sends the worker to the key). So this asks the same three things.
        """
        provider = getattr(profile, "provider", None)
        if not provider or not self._pool.configured:
            return None
        if SUBSCRIPTION_TOKEN_ENV not in (getattr(profile, "secrets", None) or ()):
            return None
        model_class = task_model_class(profile, task)
        candidates = accounts_serving(
            self._pool.accounts(), task.tenant_id, provider, model_class=model_class
        )
        if not candidates:
            return None
        return candidates, provider, model_class

    def _park_on_pool(
        self,
        task: Task,
        candidates: list[Account],
        provider: str,
        model_class: str | None,
        now: datetime,
        report: DrainReport,
    ) -> None:
        """Park a task no pool account can take, before its lease (#2222).

        The park the worker would have written after a container (`lifecycle.
        _park_no_account`): PROVIDER_QUOTA_EXHAUSTED, `park_phase:
        account_assign`, the broker's reason and the provider, so the finish
        event's wake and the prewarm sweep treat it exactly as theirs. Its
        instant is the worker's rule: an undated wait that clears in minutes
        (`_POOL_SOON`) is looked at again within POOL_RECHECK_SECONDS, or at
        a window that clears sooner; anything else at the broker's instant, or
        POOL_IDLE_RECHECK_SECONDS when it has none. `accounts` names what
        holds each account, which is what the waiting line reads.
        """
        reason, instant = eligibility(candidates, task.tenant_id, now, model_class=model_class)
        if reason in _POOL_SOON:
            soon = now + timedelta(seconds=POOL_RECHECK_SECONDS)
            next_eligible = min(instant, soon) if instant is not None else soon
        else:
            next_eligible = instant or now + timedelta(seconds=POOL_IDLE_RECHECK_SECONDS)
        block = {
            "reason": ParkReason.PROVIDER_QUOTA_EXHAUSTED.value,
            "provider": provider,
            "account_pool_reason": reason.value,
            "park_phase": "account_assign",
            "parked_by": "admission",
            "model_class": model_class,
            "accounts": pool_blockers(candidates, now, task.tenant_id),
        }
        report.held_by_pool += 1
        self._park(
            task,
            ParkReason.PROVIDER_QUOTA_EXHAUSTED,
            report,
            detail=block,
            next_eligible_at=next_eligible,
            blocked_by=[block],
        )
