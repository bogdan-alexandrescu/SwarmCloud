# task_ec076b510a76406b854b  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit apps/reconciler/ for cases where it could reclaim a lease that is still legitimately held, or fail to reclaim one that is not. CONTRACT.md invariant 5 (fencing generations) is the rule. Give the interleaving that produces each. At most 5.

## Report

I read `apps/reconciler/`, `apps/common/swarm_common/{admission,states,models}.py`, and (to verify one hypothesis before trusting it) the scheduler's dispatch/store code. Three distinct, verified races violate invariant 5's guarantee. I checked a fourth hypothesis (a gap in `_never_dispatched`/`_is_actionable` around unreadable backends) against `scheduler/store.py`'s `create_attempt`, which writes `attempt.backend` *before* the real dispatch call — that closes the gap I suspected, so I dropped it rather than report something that doesn't hold up.

## 1. `repair_task_state` reclaims by task, not by lease — corrupts a newer, legitimately-held lease

`store.py:184-241`. Step 4 of `repair.py`'s `_repair()` re-reads the task's *current* state and, if non-terminal, forces it to `READY` and unconditionally sets `current_lease_id: None` (`store.py:227-231`). It never checks that the task's current lease is still the one the finding is about.

**Interleaving:**
1. Snapshot at T0: task X is `RUNNING`, lease A (gen 1), silent — a `STALE_LEASE`/`DEAD_WORKER` finding is queued for repair. It sorts after other termination-requiring findings that are slow to process (Cloud Run `terminate()` waits up to 120s each — `backends.py:384`), so real time passes before this finding's turn.
2. Before that turn, in the real world: worker A legitimately checkpoints and self-releases (invariant 4, quota park), lease A is genuinely released, task X goes `RUNNING→PARKED→READY`, and the scheduler legitimately re-admits it: `acquire_lease_in_transaction` mints lease B, bumps `current_generation` 1→2, sets `current_lease_id=B` (`admission.py:186-240`). Worker B is now running and heartbeating fine.
3. The reconciler finally repairs the old finding: `invalidate_generation(X, expected=1)` no-ops (current is 2) — correctly avoids fencing B. `release_lease(A)` no-ops (already released). But `repair_task_state(X, to_state=READY)` re-reads the task, sees `RUNNING` (via lease B) which is non-terminal, `RUNNING→READY` is legal (`states.py:83-91`), and applies it — setting task X to `READY` and **wiping `current_lease_id` to `None`** while lease B/worker B is still actively, validly running.
4. The next scheduler drain sees task X as `READY` and admits a *third* lease C, bumping `current_generation` to 3. Worker B only discovers it's fenced at its next poll — until then, B and C are both genuinely executing the same task: the exact "two agents, one repository" failure invariant 5 exists to prevent, plus lease B is now leaked until a later pass detects `task.lease_id not in (None, lease.lease_id)`.

## 2. Staleness is judged once at snapshot time, never re-checked before fencing/terminating

`detect.py:187` computes `lease.silent_seconds(now=snapshot.taken_at)` once, at the start of the pass. `repair.py`'s `_repair()` then acts on that captured `Finding` — but `invalidate_generation` (`store.py:137-174`) only re-checks `current_generation == expected`, never heartbeat recency, and `backend.terminate()` is called unconditionally once `finding.execution.is_active` was true at list-time.

**Interleaving:**
1. Snapshot at T0: lease A's `heartbeat_at` is just past `heartbeat_grace_seconds` (90s default). A `DEAD_WORKER` finding is created (execution still reports active).
2. At T0+2s, worker A — genuinely healthy, just delayed by a slow network blip — sends a fresh heartbeat, well inside grace now.
3. The reconciler doesn't reach this finding until T0+90s (many other `DEAD_WORKER`/`OBSOLETE_GENERATION` findings from the same incident are ahead of it, each blocked on a 120s-timeout `cancel_execution` call).
4. At T0+90s: `invalidate_generation` sees `current_generation` unchanged → bumps it, fencing a worker that has been proving liveness for 88 seconds. `backend.terminate()` then kills its real, live execution. The lease is released and the task requeued — mid-task work is thrown away even though nothing was ever actually stuck.

## 3. A `None` generation label defeats both detection rules that are supposed to catch a superseded, still-running lease

- `detect.py:196-229` (`detect_stale_leases`): when `task.generation != lease.generation`, it deliberately skips reporting *if* `executions_by_attempt.get(lease.attempt_id) is not None` — trusting that `detect_orphan_executions` will catch and terminate the stale execution.
- `detect.py:338` (`detect_orphan_executions`): that catch only fires `elif execution.generation is not None and execution.generation != task.generation`.
- `backends.py:341` / `:602-604`: `execution.generation` is populated via `_int_or_none(env.get(GENERATION_ENV) or label)` and is `None` whenever the backend doesn't surface that env var/label (e.g. an older execution predating generation-stamping, or a label/env write that silently failed).

**Interleaving:**
1. Task X's lease is superseded (invalidate_generation bumped `current_generation` from a prior partial repair, or a legitimate retry), so `task.generation (2) != lease.generation (1)`, and the old execution is still genuinely running on the backend but reports `generation=None`.
2. `detect_stale_leases` sees an active execution for this attempt → `continue`s, producing no finding, on the assumption the generation rule will handle it.
3. `detect_orphan_executions` evaluates the same execution: `execution.generation is not None` is `False`, so the `OBSOLETE_GENERATION` branch never triggers; task isn't terminal, the lease isn't released, so no other branch sets `reason` either → `continue`, no finding.
4. Neither rule ever fires again on later passes (the same `None` persists) — capacity leaks forever, and the truly-stale worker is never fenced or terminated by the reconciler at all, violating invariant 5 in the "should have reclaimed but didn't" direction.

**Suggested fixes**, roughly in priority order: (1) have `repair_task_state` take `expected_lease_id` and refuse the transition (like `invalidate_generation` refuses on generation mismatch) unless `data.get("current_lease_id") == expected_lease_id`; (2) re-read `heartbeat_at` inside a final transactional check immediately before invalidating/terminating, or at minimum re-snapshot heartbeat freshness right before each repair rather than trusting pass-start data; (3) treat `execution.generation is None` on an execution tied to a generation-mismatched lease as "unknown, assume superseded" (report it) rather than silently trusting the other rule to cover it.
