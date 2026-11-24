# Workflow pause and resume

**Status:** design only. Nothing here is built. The owner decided to design
pause first, before the reconciler's pause-overdue rule. That rule (part of
issue 7) needs a pause to act on, and main has none. Section 7's frozen-contract
change is **NOT ACCEPTED** (request 159 in
[`docs/contract-change-requests.md`](../contract-change-requests.md)). Section 10
lists the owner decisions that are still open.

Today's behaviour is cited by file and symbol, never by line number, so a
citation survives the code moving. Everything about pause itself is a proposal.

---

## 0. Why pause, and why it is not cancel

A person sometimes needs a workflow to stop spending without losing it:

* a provider is misbehaving;
* a reviewer wants to read a step's output before the next step starts;
* a budget conversation is pending.

Today the only stop is cancel, which is terminal. Re-running a cancelled
workflow is refused outright (`swarm_api/steprerun.py::rerun` refuses
`workflow_cancelled`). Pause is the non-terminal stop:

* nothing new starts;
* what is running checkpoints and gives back its capacity;
* resume carries on from the checkpoints.

**Pause borrows cancel's enforcement shape on purpose.** Cancel is the one stop
this platform already enforces end to end:

| Stage | Cancel's mechanism (cited below) |
| --- | --- |
| Record | A request recorded on the step |
| Live worker | Honours it at its next control poll |
| Enforcement | A reconciler rule that stops a worker that does not honour it, through a fenced release, after a bound |

Pause differs in where it **ends**. A cancelled step ends `CANCELLED`. A paused
step ends `PARKED` and holds nothing, so a later resume can return it to
`READY`.

## 1. What exists today

### 1.1 Routes

The workflow routes are in `apps/swarm-api/swarm_api/routes/workflows.py`:

| Route | Handler |
| --- | --- |
| create | `create_workflow` |
| list | `list_workflows` |
| read | `get_workflow` |
| `POST /{workflow_id}/cancel` | `cancel_workflow` |
| `POST /{workflow_id}/steps/{step_id}/rerun` | `rerun_workflow_step` |
| `PUT /{workflow_id}/mcp-credentials` | `restore_mcp_credentials` |

There is no pause or resume route.

### 1.2 How cancel is recorded

**Who may cancel.** `cancel_workflow` calls `routes/tasks.py::require_member`,
which calls `roles.RoleStore.require(auth, roles.MEMBER)`. Any member of the
tenant may cancel any of its workflows. A viewer is refused with 403
`role_member_required`.

**The workflow document.** `swarm_api/store.py::Store.cancel_workflow` writes
`cancel_requested: True` with a plain update. It writes no cancel time and no
workflow-level event.

**Each step.** For every step, `Store.request_cancel_with_target` runs its own
transaction:

* It sets `cancel_requested`.
* It sets `cancel_requested_at` on the first request only, so a second cancel
  does not reset the clock.
* A step that holds nothing goes straight to `CANCELLED`. These are the steps in
  `PENDING_STATES`, and steps with no live worker. For a step with no live
  worker it also releases the lease and bumps `current_generation`.
* A step with a live worker is only flagged.
* It writes a task event in the same transaction: `EventType.CANCEL_REQUESTED`
  (or `CANCELLED`), plus `LEASE_RELEASED` if a lease was released.

**Refusals and contention.**

* A terminal step's `Conflict` is folded into the route's
  `tasks_already_terminal`.
* `store.CancelContended` (a 503 with `Retry-After`) names the steps whose
  transaction stayed contended. The others are still cancelled.

The per-step transaction is a deliberate choice: one transaction across 50 live
task documents (`swarm_common/config.py::Settings.max_workflow_steps`) would
contend with every worker's writes.

**Typed fields.** `cancel_requested_at` is **not** a field of the frozen
`swarm_common/models.py::Task`. It is written raw and read raw: by
`reconciler/model.py::TaskView.cancel_requested_at`, and by the worker through
the task dictionary. This is the precedent section 7 uses to keep the
frozen-contract change small.

### 1.3 How cancel is enforced

**The worker's control poll.**

* `agent_worker/lifecycle.py::Worker._run_child_supervised` polls every
  `config.py::WorkerConfig.control_poll_seconds` (10 s, env
  `CONTROL_POLL_SECONDS`).
* `ControlPlane.poll` and `RemoteControlPlane.poll` return
  `control.py::ControlSignals`.
* Through swarm-api, the fields come from
  `swarm_api/attemptstate.py::fence_view`, which carries `cancel_requested`.

**What the worker does with a signal.** `Worker._apply_control_signals` handles
the signals in this order:

1. **Fenced.** `Worker._exit_fenced_mid_run`: the worker exits without writing.
2. **Cancel.** The worker ends a live-browser hand-off if one is held, then
   terminates the child. `Worker._finish_cancelled` then checkpoints
   (`_checkpoint("cancellation")`), uploads, and calls `control.finish(CANCELLED)`.
   That one fenced transaction writes the terminal state, ends the attempt and
   releases the lease.
3. **Quota.** `Worker._park_for_quota`: checkpoint, upload, then
   `control.park(...)` (PARKED, attempt end and lease release in one fenced
   transaction), then exit with `ExitCode.PARKED`.

**The reconciler's backstop.**

* `reconciler/detect.py::detect_cancel_overdue` selects in memory from the
  snapshot. A candidate is an active execution whose task has
  `cancel_requested`, holds capacity, and has an unreleased lease of the same
  attempt and generation.
* It measures from `detect.py::_cancel_reference` (`cancel_requested_at`, or
  else `updated_at`) against `config.py::ReconcilerConfig.cancel_enforce_after_seconds`
  (600 s, env `CANCEL_ENFORCE_AFTER_SECONDS`).
* The comment next to that value sizes it: one poll, plus the 300 s
  `WORKER_FINALISE_BUDGET_SECONDS`, doubled. It must also stay far below the
  7.6 h and 12.8 h that an ignored cancel cost.
* The finding is `FindingKind.CANCEL_OVERDUE`. It is one of the kinds that must
  terminate the execution before repair.

**The repair.**

* `reconciler/repair.py::Reconciler._repair` builds a `RepairPlan`.
* `reconciler/store.py::ControlStore.fence_release_repair` does the rest in one
  transaction: bump `current_generation`, release the lease through
  `swarm_common/admission.py::release_lease_in_transaction`, end the attempt,
  and set the target state. The target is forced to `CANCELLED` by
  `ControlStore._repair_decision` when `cancel_requested` is set.

### 1.4 Parking and promotion in the scheduler

**Admission.** Every step passes through
`scheduler/loop.py::Scheduler._prepare_admission` before any lease. The
following checks run there, in order:

1. A cancel finishes the step.
2. `Scheduler._stop_for_failed_workflow`: the `fail_workflow` gate. It uses a
   per-drain cached point read of the workflow
   (`scheduler/store.py::SchedulerStore.workflow_on_step_failure`).
3. Dependencies park the step `DEPENDENCY_INCOMPLETE`.
4. A missing or disabled tenant parks the step `ParkReason.MANUAL_PAUSE`, with
   `paused_by` and `paused_at` taken from the admin audit
   (`Scheduler._tenant_paused_by`).

**Park and promote.**

* `SchedulerStore.park` writes READY to PARKED, guarded on the state read, and
  appends a `PARKED` event. There is no `parked_at` field; the event is the park
  time.
* `SchedulerStore.promote_to_ready` writes PARKED to READY, guarded on the state
  and the park reason.

**The `MANUAL_PAUSE` sweep.**

* `Scheduler._promote_manual_pauses` reads `Scheduler._parked_window(MANUAL_PAUSE)`.
* That is `SchedulerStore.parked_page`: an equality query on `state == PARKED`
  and `park_reason == ...`, paged by document id.
* It promotes a step once the profile exists, the tenant is enabled, and no
  required pool has `enabled is False`.

**A pool pause does not park.** `swarm_common/admission.py::evaluate_capacity`
leaves the step READY with a `MANUAL_PAUSE` blocker
(`SchedulerStore.record_blockers`).

**Other pauses.**

* The global kill switch is `SchedulerStore.dispatch_paused`, read at the top of
  `Scheduler.drain`. Its routes are `swarm_api/routes/admin.py::pause_dispatch`
  and `resume_dispatch`, which are admin-only and write no event.
* Schedules have their own pause: `routes/schedules.py::pause_schedule`.

### 1.5 The reconciler's snapshot and workflows

* `reconciler/store.py::ControlStore.snapshot` reads tasks in `_active_states()`
  (`CONCURRENCY_STATES`) and unreleased leases. `incremental_snapshot` is the
  between-passes delta.
* The reconciler reads workflows only in `ControlStore.workflows_for_stall_check`
  (non-terminal by state, paged).
* Nothing in `apps/reconciler/reconciler` mentions a pause.

### 1.6 The frozen contract

* `swarm_common/states.py::ParkReason` has `MANUAL_PAUSE`. It is pool, tenant
  and profile scoped, and it is promoted by a sweep that knows nothing of
  workflows.
* `states.py::_ALLOWED` lets LEASED, DISPATCHED, STARTING and RUNNING go back to
  READY.
* Only RUNNING may go to PARKED by a worker. LEASED to PARKED is the scheduler's
  `PLATFORM_REFUSED` edge alone. See `states.py::WORKER_TRANSITIONS`.
* `models.py::Workflow` has `cancel_requested` and no pause field.
* `states.py::EventType` has `CANCEL_REQUESTED` (contract request 17) and no
  pause event.

## 2. The model

### 2.1 Where the pause is recorded

**The workflow document is the one source of truth.** Three fields are added
there (section 7):

| Field | Meaning | Why it exists |
| --- | --- | --- |
| `paused: bool` | The pause is in force | The **equality key**. The reconciler and any sweep find paused workflows with `workflows where paused == true`, a single-field index Firestore builds automatically. No full scan. `paused_at != null` would be an inequality, and a boolean has no such cost. |
| `paused_at` | When the pause started | Set on the first pause and kept on a repeat pause, so the overdue bound cannot be reset by pressing Pause again (the same rule as `cancel_requested_at`). |
| `paused_by` | Who paused it | Shown in the waiting line. |

**Each non-terminal step task also carries a mirror, `pause_requested_at`.**
It is written raw, outside the frozen `Task` model, exactly as
`cancel_requested_at` is.

**Why a mirror on the step as well as the workflow.** The worker's control poll
reads one task document and nothing else: `ControlPlane.poll`, or swarm-api's
`fence_view`.

* Making it read the workflow too would add a second read to every poll of
  every worker, every 10 s, to serve a rare event.
* The mirror rides the read the poll already makes.

The mirror is a **signal, never the authority**. The scheduler and the
reconciler decide from the workflow document. So a step whose mirror was never
written is still parked at admission and still caught by the overdue rule.

### 2.2 What pause means in each state

| State | On pause | Holds capacity meanwhile? |
| --- | --- | --- |
| QUEUED | Nothing written to the state. It reaches READY by its usual path and is parked at admission (section 4). | No |
| READY | Parked `WORKFLOW_PAUSED` by the scheduler at admission, before any lease. | No |
| PARKED (any reason) | Left under its own reason. Whatever sweep promotes it to READY, admission then parks it `WORKFLOW_PAUSED`. The original reason's own clock (a quota window, a dependency) keeps running. | No |
| LEASED, DISPATCHED, STARTING | The worker sees the mirror at its first poll, before the runner starts. It walks to RUNNING and parks at once (section 5), because a worker has no pre-running edge into PARKED. If no worker ever polls, the reconciler's rule returns it to READY (section 6). | Yes, until the park, or at most the bound |
| RUNNING | Parks at its next control poll: checkpoint, park, release, exit (section 5). If it never polls, the reconciler's rule (section 6). | Yes, until the park, or at most the bound |
| Control-plane parks (`AWAITING_INPUT`, `FIX_ROUNDS_RUNNING`, `HUMAN_REQUIRED`) | Untouched. They hold nothing, and swarm-api or a person ends them. A step they release reaches READY and is parked at admission. | No |
| Terminal | Untouched. | No |

**Why the scheduler parks READY steps, and the route does not.** Parking at
admission keeps READY to PARKED with the scheduler, its only writer today
(`SchedulerStore.park`). It also needs no transaction across steps.

**Pause does not rewrite other parks.** Rewriting would lose a park's own reason
and clock. A dependency park would then be promoted on resume and re-parked: two
writes for nothing.

### 2.3 Who may pause and resume

The `MEMBER` role, through `routes/tasks.py::require_member`, the same gate as
cancel. `roles.required_role` already maps an unlisted write to `MEMBER`, so the
new routes need no entry in `VIEWER_WRITES` or `ADMIN_WRITES`.

* A viewer is refused with 403 `role_member_required`.
* Another tenant's workflow is a 404, from `Store.get_workflow`'s tenant check,
  as for cancel.

Whether only the submitter and admins may pause is decision Q3 (section 10).

## 3. Routes

```
POST /workflows/{workflow_id}/pause    body: {"reason": "<optional, at most 500 characters>"}
POST /workflows/{workflow_id}/resume   body: {}
```

Both live in `routes/workflows.py` beside `cancel_workflow`, and both call
`require_member`.

### 3.1 Pause

Pause runs in two stages, in the same order as cancel and for the same reason:
contention.

**Stage 1: the workflow transaction.**

1. Read the workflow.
2. Refuse if needed:
   * 404 `NotFound`: missing, or another tenant's.
   * 409 `workflow_terminal`: `state` is in `TERMINAL_STATES`.
   * 409 `workflow_cancel_requested`: `cancel_requested` is set. Cancel wins,
     and pausing a workflow being cancelled would only delay the cancel.
3. If already `paused`: write nothing and answer 200 with
   `{"already_paused": true, "paused_at": <the first time>}`. This is the
   idempotency rule: a repeat pause **never moves `paused_at`**, so it cannot
   extend the overdue bound.
4. Otherwise write `paused: true`, `paused_at: now`, `paused_by`, the optional
   `pause_reason`, and `updated_at`.

**Stage 2: one transaction per non-terminal step,** like
`Store.request_cancel_with_target`:

* Write `pause_requested_at = workflow.paused_at`. The value is the workflow's
  pause time, not now, so all steps agree.
* Append `EventType.PAUSE_REQUESTED` with detail
  `{requested_by, workflow_id, paused_at, from_state}`.
* Skip a step whose mirror already equals `paused_at`. Retrying is idempotent.
* If a step stays contended, return 503 `Retry-After` with `steps_contended`, as
  `CancelContended` does. Retrying finishes the mirrors without moving
  `paused_at`.

**The answer.**

```
{"workflow_id", "paused": true, "paused_at",
 "steps_signalled", "steps_already_terminal"}
```

The route then rings the waker (a `"workflow_paused"` ring on `ctx.waker`, as
cancel rings `workflow_cancelled`). The next drain then parks READY steps
promptly.

**Events.**

* `PAUSE_REQUESTED` on each non-terminal step, in its stage 2 transaction.
* No workflow-level event. None exists today (cancel writes none), and inventing
  a workflow event stream is out of scope. The workflow document's `paused_by`,
  `paused_at` and `pause_reason` are the workflow-level record.
* The scheduler's `PARKED` events (reason `WORKFLOW_PAUSED`) follow from
  section 4.

### 3.2 Resume

**Stage 1: the workflow transaction.**

1. Refuse if needed:
   * 404 `NotFound`.
   * 409 `workflow_cancel_requested`: nothing is left to resume.
   * 409 `workflow_terminal`.
2. If not `paused`: write nothing and answer 200 with `{"already_running": true}`.
3. Otherwise write `paused: false`, `resumed_at`, `resumed_by`, and
   `paused_at: null`. Clearing `paused_at` lets a later pause start a fresh
   clock.

**Stage 2: per step, clear the mirror,** but only where it equals the
`paused_at` being lifted. The guard stops a resume from clearing a later pause's
mirror. Each cleared step gets `EventType.RESUME_REQUESTED` with
`{requested_by, workflow_id, paused_at}`.

**Who promotes the parked steps.** The route does **not** promote
`WORKFLOW_PAUSED` parks itself. The scheduler's sweep does (section 4), so
PARKED to READY for this reason has one writer. The route rings the waker with
`"workflow_resumed"`.

**A live step that had not parked yet.** Its mirror is cleared, and its next
poll no longer asks it to park. If it parks in the instant between the poll and
the resume, the sweep promotes it on the next drain. The cost is one restart
from its checkpoint, never lost work.

### 3.3 Interactions with the existing routes

* **Cancel on a paused workflow** is allowed and wins. `WORKFLOW_PAUSED` parks
  are in `PENDING_STATES`, so `request_cancel_with_target` ends them `CANCELLED`
  at once. `ControlStore._repair_decision` already makes cancel win over a READY
  repair target.
* **Step rerun on a paused workflow** is allowed. Its new steps reach READY and
  are parked at admission, because the gate reads the workflow, not the task.
  `rerun_workflow_step` needs no new refusal.

## 4. Scheduler

### 4.1 The admission gate

In `Scheduler._prepare_admission`, add `Scheduler._park_for_workflow_pause`
right after `_stop_for_failed_workflow`, before dependencies, credentials or
capacity are examined.

* If the step has a `workflow_id` and its workflow is paused, park it with
  `SchedulerStore.park(task, ParkReason.WORKFLOW_PAUSED, detail={"paused_by",
  "paused_at", "workflow_id"})` and return `None`. Nothing is reserved or leased.
* The read is a new `SchedulerStore.workflow_pause(workflow_id, tenant_id)`. It
  is a point read cached for the drain, as `workflow_on_step_failure` is, and it
  checks the tenant matches.

Gating at admission is enough to keep paused work from creating demand. Every
path to a lease goes through `_prepare_admission`, and the lease itself is taken
by `SchedulerStore.acquire_leases`, which only takes READY tasks.

### 4.2 The promotion sweep

Add `Scheduler._promote_workflow_pauses`, run in the drain beside
`_promote_manual_pauses`.

1. Select `_parked_window(ParkReason.WORKFLOW_PAUSED)`: equality on
   `state == PARKED` and `park_reason == "WORKFLOW_PAUSED"`, paged by id.
2. For each step, call `_stop_for_failed_workflow` first, so a failed
   workflow's paused steps are cancelled, not promoted.
3. Promote with `_promote(task, kind="workflow_pause",
   detail={"reason": "workflow_resumed"})` once `workflow_pause(...)` says it is
   no longer paused.
4. Promotion only returns the step to READY. Admission then asks every other
   question again: tenant, pools, credentials, dependencies.

### 4.3 Why a new park reason and not `MANUAL_PAUSE`

`_promote_manual_pauses` promotes any `MANUAL_PAUSE` park once tenant, profile
and pools are clear. A workflow-paused step would be re-readied at once, and
then re-parked by admission, every drain: two guarded writes per step per drain
for as long as the pause lasts.

Teaching that sweep to read the workflow would mix two owners' pauses under one
reason. `waiting_detail._pause` would also show a tenant pause's wording for a
workflow pause.

This is the same argument that gave `HUMAN_REQUIRED` its own reason (see its
comment in `states.py::ParkReason`). The reuse alternative is recorded as
declined in request 159.

### 4.4 Demand

A paused workflow's QUEUED, READY and PARKED steps hold no lease and no pool
units, so they create no infrastructure demand (invariant 1). What is LEASED
through RUNNING at the pause holds its reservation only until the worker parks,
or at most until the overdue bound plus one reconciler pass. After that the
paused workflow holds nothing.

The waiting line in `swarm_api/waiting_detail.py` gains a `WORKFLOW_PAUSED`
branch beside `_pause`: "Paused by {paused_by}", resumes when "the workflow is
resumed".

## 5. Worker

### 5.1 The signal

* `ControlSignals` gains `pause_requested: bool`.
* `ControlPlane.poll` reads `bool(task.get("pause_requested_at"))`.
  `RemoteControlPlane.poll` reads it from `fence_view`, which gains
  `"pause_requested": bool(task.get("pause_requested_at"))`.
* No new read anywhere.

### 5.2 The path, `Worker._park_for_pause`

`Worker._apply_control_signals` checks the pause after fenced and cancel, and
before quota. A cancel outranks a pause, and a fenced worker writes nothing.

When the pause is set, the worker:

1. If a live-browser hand-off is held, ends it with `outcome="paused"`, as the
   cancel branch does.
2. Calls `child.terminate(cfg.termination_grace_seconds, reason="workflow paused")`,
   then `child.finish()` and `_child_ended()`.
3. Calls `self._checkpoint("workflow-pause")`. This is not the `periodic` label,
   so it is always written (`checkpointing.py::CheckpointMixin._checkpoint`).
4. Calls `self._upload_outputs()`.
5. Calls `self.control.park(reason=ParkReason.WORKFLOW_PAUSED,
   detail={"park_phase": "workflow_pause", "paused_at": ...})`. This is the same
   single fenced transaction the quota park uses: PARKED, the attempt's end and
   the lease release (`ControlPlane.park`). Through swarm-api it is
   `AttemptState.park`.
6. Returns `Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)`. The
   process exits.

**Never a sleep.** The worker does not wait for resume. Resume is the
scheduler's promotion plus a fresh lease, a fresh generation and a restore from
the checkpoint. A paused workflow may stay paused for days, and a worker
sleeping through that would hold its lease and its pool units the whole time
(invariant 4).

### 5.3 Before the runner starts

A worker in LEASED, DISPATCHED or STARTING that reads the mirror at
`state/open` or at its first poll does the following:

1. Walks to RUNNING. `WORKER_TRANSITIONS` has no pre-running edge into PARKED,
   and STARTING to READY is not a worker edge.
2. Runs the same path with nothing to terminate. The checkpoint holds the
   restored workspace.

The alternative, exiting and letting the reconciler requeue, would hold the
capacity for the whole bound.

### 5.4 Refunds

`AttemptState.park` decides refunds by reason. `WORKFLOW_PAUSED` joins the
refunded reasons, with a bound. Whether, and how far, is decision Q1.

## 6. Reconciler: the pause-overdue rule (issue 7)

### 6.1 Selection

**The snapshot gains the paused workflows.**
`ControlStore.paused_workflows(limit, after)` runs `workflows where paused ==
true`, paged by id. It is an equality query, never a full scan, and it is
answered from the automatic single-field index. Both `ControlStore.snapshot`
and `incremental_snapshot` attach the result, as `snapshot.paused_workflows`:
`{workflow_id: (tenant_id, paused_at)}`.

**`detect.py::detect_pause_overdue` sits beside `detect_cancel_overdue`.** It
follows the same rules:

1. For each active execution, take its task from the snapshot.
2. The task's `workflow_id` must be in `paused_workflows`, with the same tenant.
3. The task must hold capacity and must **not** have `cancel_requested`. A cancel
   is `detect_cancel_overdue`'s, and must not produce two findings for one
   attempt.
4. The lease must be unreleased, of the same attempt and generation, and the
   execution's generation must match if set.
5. The reference is `max(paused_at, lease.created_at)`. A step leased after the
   pause (the admission race in section 8) gets the full bound from its own
   lease, not from a pause it never saw.
6. A finding is emitted when `now - reference >= pause_enforce_after_seconds`.

### 6.2 The finding

The finding is `FindingKind.PAUSE_OVERDUE = "pause_overdue"`, added to the kinds
that must terminate the execution before repair, beside `CANCEL_OVERDUE`. It
records:

* `task_id`, `lease_id`, `attempt_id`, `tenant_id`, `generation`, `execution`;
* `reason`: "workflow {workflow_id} paused {age}s ago and execution {name} is
  still active, {overdue}s past the bound ({bound}s)";
* `detail`: `{workflow_id, task_state, paused_at, pause_age_seconds,
  overdue_by_seconds, measured_from: "paused_at" | "lease_created_at",
  namespace}`.

This names the step, the pause time and how long it ran past the bound, as the
issue asks.

### 6.3 The repair

`Reconciler._repair` handles the finding the way it handles `CANCEL_OVERDUE`:

1. Terminate the execution through the backend.
2. Only on a confirmed kill, call `ControlStore.fence_release_repair` with
   `RepairPlan(to_state=READY, next_eligible_at=None)`. In one transaction this
   bumps `current_generation`, releases the lease through
   `release_lease_in_transaction(reason="reconciler:pause_overdue")`, ends the
   attempt and writes READY.

`next_eligible_at` stays `None`. The ordinary requeue's backoff
(`requeue_backoff_seconds`) is for failures, and this is not one.

The refund, if Q1 says yes, is applied inside that transaction. It uses the
pattern of `RepairPlan.startup_refund_limit`: only if this transaction did the
fence, and only for a READY target. A refunded step is therefore never
downgraded to FAILED by `retries_exhausted` in `_repair_decision`.

If the pause was lifted between detection and repair, the repair re-reads the
workflow inside the transaction and stands down. The execution was already
killed, so the step goes READY and is admitted as normal. That is the reclaim
outcome, and it is correct.

**Why READY, not PARKED.** `_ALLOWED` lets all four live states go to READY. The
only edge from them into PARKED is RUNNING's, plus the scheduler's own
LEASED/`PLATFORM_REFUSED` edge. READY then meets the admission gate (section
4.1), which parks it `WORKFLOW_PAUSED`. One rule, in one place, decides what a
paused step becomes.

After the transaction, `_repair` emits `GENERATION_FENCED` and `LEASE_RELEASED`
as today, plus a `READY` event with `{"finding": "pause_overdue", "paused_at"}`.

### 6.4 The bound

The bound is `ReconcilerConfig.pause_enforce_after_seconds`, or
`cancel_enforce_after_seconds` if the owner prefers. That is decision Q2. Its
default is explained next to its value.

### 6.5 What makes the issue's checks pass

| Issue check | How it holds |
| --- | --- |
| A worker past the bound is stopped and its step parked | Repair to READY, then the admission gate parks it |
| A worker that parks in time is untouched | Its lease is released and its task leaves `_active_states`, so it has no execution to select |
| The snapshot reads paused workflows by equality | `paused == true` |

## 7. Frozen contract

Pause needs four additions in `apps/common/swarm_common/`. They are written as
**request 159** in `docs/contract-change-requests.md`, marked **NOT ACCEPTED**:

* `ParkReason.WORKFLOW_PAUSED`;
* `Workflow.paused`, `Workflow.paused_at` and `Workflow.paused_by`;
* `EventType.PAUSE_REQUESTED` and `EventType.RESUME_REQUESTED`.

Nothing in the frozen package is edited by this design.

**Not asked for:**

* **A typed `Task.pause_requested_at`.** It stays raw, as `cancel_requested_at`
  does.
* **A change to `_ALLOWED` or `WORKER_TRANSITIONS`.** The design uses only
  existing edges: READY to PARKED (scheduler), RUNNING to PARKED (worker),
  live states to READY (reconciler), and PARKED to READY (scheduler).
* **A change to `admission.py`.** The gate lives in the scheduler (section 4.1).

`FindingKind` is reconciler-local and not frozen.

## 8. Invariants (CONTRACT.md)

**1. Demand only from LEASED through RUNNING.**

* Pause writes no state for QUEUED or PARKED.
* READY is parked before any reservation.
* `WORKFLOW_PAUSED` is a park, so it holds nothing.
* A paused workflow's demand is what was live at the pause, and it falls to zero
  within one poll, or within the bound.
* Nothing uses pending pods.

**2. Capacity is all-or-nothing in one transaction.**

* Admission is unchanged: `SchedulerStore.acquire_leases` with
  `acquire_leases_in_transaction`. The gate runs before it and reserves nothing.
* Releases go through the existing single transactions: `ControlPlane.park`,
  `AttemptState.park` and `fence_release_repair`, each with
  `release_lease_in_transaction`.

**3. Concurrency counts from LEASED.**

* Unchanged. A step leased in the race window counts from its lease until it
  parks.

**4. Workers never sleep through a wait.**

* The pause path is checkpoint, park, release, exit (section 5.2). No worker
  waits for resume.

**5. Fencing.**

* The worker's park is a fenced transaction (`ControlPlane._fenced_task`, or
  swarm-api's `fence_reason`).
* The reconciler bumps the generation before releasing. A worker of the old
  generation that wakes up is refused at its next poll or write
  (`ControlSignals.is_fenced`, `FencedWriteRefused`). It then exits through
  `Worker._exit_fenced_mid_run` without running the agent and without touching
  the lease.
* The pause routes write only `pause_requested_at` and an event to a live step.
  They never write its state, generation or lease.

**6. Spot.** Untouched.

**7. Memory and storage request equals limit; CPU burst.** Untouched.

**8. Checkpointing is mandatory and periodic.**

* The pause park forces a `workflow-pause` checkpoint, so resume restores the
  work up to the park.
* A step stopped by the reconciler loses at most what followed its last periodic
  checkpoint (`checkpoint_interval_seconds`). That loss is why the bound exists
  rather than an immediate kill.
* Periodic checkpointing is not relaxed.

**9. Per-tenant isolation.**

* Each route resolves the tenant with `deps.tenant_scope`.
* `Store.get_workflow` refuses another tenant's workflow with 404.
* The scheduler's read checks the tenant, as `workflow_on_step_failure` does.
* The reconciler joins on both `workflow_id` and `tenant_id`.

**10. Runner profile by name.** The pause body takes only an optional free-text
reason, bounded. No image, command or resource field.

### 8.1 The race the design leaves, and why it is safe

A drain may cache "not paused" just before the pause lands, and then lease a
READY step. That step:

* holds its lease;
* already carries the mirror, because stage 2 writes the mirror to every
  non-terminal step whatever its state, and admission's update does not clear
  it;
* so its worker parks at its first poll.

If stage 2 had not reached it, the overdue rule stops it at
`lease.created_at + bound`. Closing the window inside `acquire_leases_in_transaction`
would need a frozen-contract change for a gap of one drain. That is not worth
it.

## 9. Console and plugin

* **Console:** Pause and Resume buttons sit beside Cancel in the workflow
  board's header in `apps/swarm-ui/src/Workflows.tsx`, where `cancelWorkflow` is
  used. A paused step's waiting line comes from swarm-api.
* **Plugin and CLI:** `swarm workflow-pause` and `swarm workflow-resume` sit
  beside `cmd_workflow_cancel` in `apps/swarm-mcp/swarm_mcp/cli.py`. The MCP
  tools `swarm_workflow_pause` and `swarm_workflow_resume` sit beside
  `swarm_workflow_cancel` in `apps/swarm-mcp/swarm_mcp/server.py`.

## 10. Open owner decisions

These are the same as `questions.json` in the step's artifacts.

### Q1. Is a pause-overdue requeue refunded?

That is, is it not counted against the attempt limit? The same question covers
the worker's own pause park.

**Facts.**

* Admission adds one to `attempt_count` (`swarm_common/admission.py`).
* `max_attempts` defaults to 3.
* The ordinary reclaim does not refund.
* Bounded refunds already exist: the quota park's `QUOTA_MAX_RESUMES = 5` in
  `agent_worker/control.py`, the reconciler's `startup_refund_limit` and its
  never-started requeue limit.

**Options.**

* **(a) Refund both the worker park and the reconciler requeue, bounded.** Keep
  a per-task `metadata.pause_refunds` count with a limit of 5, the quota park's
  number. Past it, the requeue counts.
* **(b) Refund the worker park only.** The reconciler's requeue counts, since a
  worker that ignored its poll may be genuinely wedged.
* **(c) Refund neither.**

**Recommendation: (a).** A pause is a person's choice, not a failure. Without a
refund, two pauses of a running step exhaust a default three-attempt step and
FAIL it. The bound stops a wedged worker from looping forever on refunds.
Option (b) penalises a step for the platform's slow poll. The stuck detector,
not the attempt count, is what catches a truly wedged worker.

### Q2. Does the bound reuse `cancel_enforce_after_seconds` or get its own setting?

**Options.**

* **(a) Reuse `cancel_enforce_after_seconds` (600 s).**
* **(b) Its own `pause_enforce_after_seconds`, env `PAUSE_ENFORCE_AFTER_SECONDS`,
  default 600.** The default is explained the same way: one poll, plus the
  worker's finalise budget, doubled.

**Recommendation: (b).** The worker's pause path costs the same as its cancel
path, so the default is equal. But the two trade off differently:

* An overdue cancel wastes money on work nobody wants, so its bound is tuned
  down.
* An overdue pause kills work somebody wants to keep, so an operator may want
  its bound looser.

One setting would couple them, and a separate setting costs one config field.

### Q3. Who may pause and resume?

**Options.**

* **(a) Any `MEMBER` of the tenant,** as cancel.
* **(b) The submitter and tenant admins only.**

**Recommendation: (a).** Pause is less destructive than cancel, which any member
may already do. A stricter rule for the milder act would be incoherent.

### Q4. Does pausing a workflow pause the child tasks its running steps submitted?

**Options.**

* **(a) No, in the first build.** A child runs as its own task. The parent,
  parked `WORKFLOW_PAUSED`, re-awaits its children on resume.
* **(b) Cascade the pause mirror to children,** through `ChildService.cascade`,
  as cancel cascades.

**Recommendation: (a).** Then (b) as a follow-up if children turn out to hold
meaningful capacity during pauses. Cascading needs the gate and the overdue
rule to resolve a child's root workflow, which doubles the first build.

### Q5. A new park reason, or reuse `MANUAL_PAUSE`? (request 159)

**Options.**

* **(a) `WORKFLOW_PAUSED`,** a frozen-contract change.
* **(b) Reuse `MANUAL_PAUSE`** and teach `_promote_manual_pauses` to read the
  workflow.

**Recommendation: (a),** for the reasons in section 4.3. Option (b) needs no
contract change, but costs two writes per paused step per drain and confuses
the waiting line.

## 11. The build, as lanes

Each lane is red first: the failing tests are pushed alone, then the fix. No two
parallel lanes share a file. They run in dependency order.

### P0. Contract (after the owner accepts request 159)

* **Files:** `apps/common/swarm_common/states.py`, `models.py`;
  `swarm_api/codec.py` (`workflow_to_firestore` and `workflow_from_dict`); the
  parity script's tables.
* **Red tests:**
  * `WORKFLOW_PAUSED` is a `ParkReason`.
  * `Workflow` round-trips `paused`, `paused_at` and `paused_by`, and a stored
    workflow without them reads as not paused.
  * The two event types exist.

### P1. API routes (needs P0)

* **Files:** `swarm_api/routes/workflows.py`, `swarm_api/store.py` (new
  `Store.pause_workflow`, `Store.resume_workflow`, `Store.signal_step_pause`),
  `swarm_api/attemptstate.py` (`fence_view` gains `pause_requested`;
  `AttemptState.park`'s refund rule for `WORKFLOW_PAUSED`, per Q1),
  `swarm_api/waiting_detail.py`.
* **Red tests**, in `tests/unit/control_plane/`:
  * The viewer 403.
  * 404 for another tenant's workflow.
  * 409 `workflow_terminal` and `workflow_cancel_requested`.
  * A repeat pause keeps `paused_at` and writes no event.
  * Each non-terminal step gets `pause_requested_at == paused_at` and one
    `PAUSE_REQUESTED`.
  * Contention answers 503 and the retry completes.
  * Resume clears only a matching mirror.
  * `fence_view` carries the flag.

### P2. Scheduler (needs P0; parallel with P1 and P3)

* **Files:** `scheduler/scheduler/loop.py` (`_park_for_workflow_pause` in
  `_prepare_admission`, `_promote_workflow_pauses` in the drain),
  `scheduler/scheduler/store.py` (`workflow_pause`).
* **Red tests**, in `tests/unit/scheduler/`:
  * A READY step of a paused workflow is parked `WORKFLOW_PAUSED` with no lease
    and no pool counter change.
  * The sweep leaves it while paused and promotes it on resume.
  * A failed workflow's paused step is cancelled, not promoted.
  * One workflow read per drain.

### P3. Worker (needs P0; parallel with P1 and P2)

* **Files:** `agent_worker/control.py` (`ControlSignals.pause_requested`, both
  polls), `agent_worker/lifecycle.py` (`_park_for_pause`, and the branch in
  `_apply_control_signals`).
* **Red tests**, in `tests/unit/worker/`:
  * The poll's flag leads to terminate, then the `workflow-pause` checkpoint,
    then park `WORKFLOW_PAUSED`, then exit `ExitCode.PARKED`, with no sleep.
  * Cancel outranks pause.
  * A fenced worker writes nothing.
  * A pre-running worker walks to RUNNING and parks.

### P4. Reconciler, the issue-7 rule (needs P0 and P1's field; P2 for the end-to-end park)

* **Files:** `reconciler/reconciler/store.py` (`paused_workflows`, snapshot
  attachment, the refund in `fence_release_repair` per Q1),
  `reconciler/reconciler/detect.py` (`detect_pause_overdue`, `PAUSE_OVERDUE`),
  `reconciler/reconciler/repair.py`, `reconciler/reconciler/config.py` (the
  bound, per Q2, its default explained beside it).
* **Red tests**, in `tests/unit/reconciler/`:
  * Past the bound: terminated, generation bumped, lease released, READY, the
    finding's detail.
  * Inside the bound: untouched.
  * A parked step: no finding.
  * `cancel_requested`: only `CANCEL_OVERDUE`.
  * The reference is `lease.created_at` for a lease after the pause.
  * The snapshot reads paused workflows by `paused == true` alone (the fake
    store asserts the query).

### P5. Console and CLI (needs P1)

* **Files:** `apps/swarm-ui/src/Workflows.tsx` and its api module;
  `apps/swarm-mcp/swarm_mcp/cli.py`, `server.py`.
* **Red tests:** the buttons' calls, and the CLI's argument parse.

### Deployed check

Because P2 and P4 change dispatch and reconciliation, CLAUDE.md requires
`make smoke concurrency-test race-test` against a deployed environment
afterwards, then the checklist.
