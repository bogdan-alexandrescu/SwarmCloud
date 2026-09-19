# task_76c152369a2747e6ac51  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit apps/scheduler/ for ways capacity could be reserved and never released - a leak that makes the platform slowly refuse work it has room for. CONTRACT.md invariants 1, 2 and 3 are the rules. Consider crashes between transactions, partial failures, and retries. For each, give the exact sequence. At most 5.

## Report

I audited `apps/scheduler/` against invariants 1–3 in `CONTRACT.md`. The admission transaction itself (`apps/common/swarm_common/admission.py`) is correctly atomic — that part of invariant 2 holds. The leaks are all in what happens *around* that transaction: every write after `acquire_lease` commits is a separate, non-transactional Firestore call, and `apps/scheduler/scheduler/loop.py`'s only safety net (`except DispatchError`) doesn't cover most of the ways those calls can fail. Crucially, `SchedulerStore.ready_tasks()` (`store.py:88-95`) only ever selects `state == "READY"`, and nothing under `apps/scheduler/` ever re-examines a `LEASED` task — recovery is 100% delegated to `apps/reconciler`'s stale-lease sweep, which is out of scope here. So any of the following leaves a reservation that this app can never see or undo again; it just looks like a pool that's permanently full (invariant 3: concurrency is counted from `LEASED`, so these stick immediately).

**1. Cloud Run client construction escapes the only exception handler**
- `loop.py:349` `acquire_lease` commits: pools incremented, lease written, task → `LEASED` (`admission.py:202-240`, correctly atomic).
- `loop.py:367-375` `create_attempt` / `append_event` succeed.
- `loop.py:378` → `BackendRouter.dispatch` → `CloudRunJobDispatcher.dispatch` (`dispatch.py:398`) → `ensure_job` (`dispatch.py:406`).
- Inside `ensure_job`, `client = self._jobs()` (`dispatch.py:365`) sits *between* the two `try/except gexc.GoogleAPICallError` blocks (366-376 and 378-394) — it is unprotected. If credential refresh or transport setup fails here (expired workload-identity token, metadata-server DNS hiccup), it raises e.g. `google.auth.exceptions.DefaultCredentialsError`, which is not a `GoogleAPICallError`.
- That exception isn't a `DispatchError`, so `loop.py:381`'s `except DispatchError` never fires. It propagates out of `_admit_one` and out of `drain()` entirely.
- No `release_lease` is ever called. The task sits at `LEASED` forever from the scheduler's point of view — `ready_tasks()` will never select it again.

**2. `create_attempt`/`append_event` have no exception handling at all**
- Same steps through the transaction committing (`loop.py:349`).
- `loop.py:367` `create_attempt` is a bare Firestore `.set()` — it is *before* the `try:` at `loop.py:377`, so nothing in `_admit_one` guards it.
- A transient Firestore error (`DeadlineExceeded`/`UNAVAILABLE`) or any serialization bug here raises immediately, before `dispatch()` is even attempted.
- Result is identical: capacity reserved in every required pool, task `LEASED`, zero attempt/dispatch ever issued, no cleanup path reachable.

**3. Raw process termination between the transaction and the follow-up writes**
- `admission.py:19-21`'s own docstring calls partial reservation *inside* the transaction "the one failure mode that would leak capacity permanently" — and says there's deliberately no path for it. But the same failure mode exists one layer up, in code that isn't transactional and isn't even try/excepted: if the scheduler's process is SIGKILLed, OOM-killed, or its Cloud Run instance is torn down by autoscaling between `acquire_lease` returning and `mark_dispatched` (`loop.py:408`) or `return_to_ready_after_failed_dispatch` (`loop.py:390`) committing, no code runs to release anything.
- There is no "list `LEASED` tasks this instance orphaned" sweep anywhere in `apps/scheduler/` — the only place that ever looks at existing leases again is `apps/reconciler`.

**4. Ack lost on the admission commit itself**
- `store.py:414-427` (`acquire_lease`) wraps the transaction in `@firestore.transactional`, which retries on *contention/abort* — not on "commit succeeded but the response never reached the client."
- If the commit RPC actually lands (pools incremented, lease written, task → `LEASED`) but the response is lost to a deadline/connection reset, the Python client raises a transport exception from `_acquire(transaction)`.
- `loop.py:352`'s `except AdmissionDenied` only catches the deliberate "not admissible" exception type — never transport errors — so this propagates raw. `report.leased` is never incremented and `create_attempt`/dispatch never run, yet the reservation is real: the caller believes admission "failed" and has no lease id to clean up even if it wanted to.

All four converge on the same root cause: **the scheduler treats admission as a one-way door.** Once `acquire_lease_in_transaction` commits, invariant 2 has been honored for that single transaction, but nothing downstream in `apps/scheduler/` is transactional with it, most of it isn't even exception-guarded, and the scheduler has no query that ever revisits a `LEASED` task to notice the follow-through never happened. That's the exact mechanism behind "the platform slowly refuses work it has room for" — every one of these produces a pool that reports `active` capacity consumed by a task that will never run, with only the out-of-scope reconciler's deadline sweep able to undo it.
