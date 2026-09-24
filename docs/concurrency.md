# Concurrency control

The property this platform must provide:

> If two schedulers race for the last free slot, exactly one wins, and the
> configured limit is never exceeded — not even transiently.

"Not even transiently" is the hard part. A design that checks capacity, then
writes a lease in a second step, settles back to the right number quickly enough
that an end-of-run assertion passes, while having briefly run more agents than
the platform can hold. `scripts/concurrency-test.sh` samples continuously during
a deliberate overload and fails on the **worst** sample for exactly this reason.

---

## 1. Slot pools

A pool is a named budget: `pools/{name}` in Firestore.

```
name                  hard_limit  adaptive_target  quota_derived_limit  active  enabled
global                       100             null                 null      37     true
tenant:eng                    40             null                 null      12     true
resource:standard            100             null                 null      31     true
runner:claude-code            60             null                 null      12     true
backend:CLOUD_RUN_JOB        100             null                 null      35     true
provider:anthropic            60               22                   18      12     true
provider:anthropic:tenant:eng 10             null                    8       5     true
```

```
effective_limit = max(0, min(hard_limit, adaptive_target?, quota_derived_limit?))
available       = max(0, effective_limit - active)
```

Three separate ceilings, one floor:

* `hard_limit` — the admin's statement of what is allowed. Only an admin changes
  it (`PUT /v1/admin/limits/...`, or `terraform apply`).
* `adaptive_target` — what AIMD currently believes the provider tolerates. It may
  only ever go **below** `hard_limit`; see [quota-management.md](quota-management.md).
* `quota_derived_limit` — derived from what the provider actually reported
  (remaining requests, reset time).
* `enabled` — `false` means paused. `scripts/pause-swarm.sh` flips this and
  records what it flipped so `resume-swarm.sh` re-enables exactly that set and
  nothing else.

`active` is the authoritative count of leases holding the pool. It is mutated
**only** inside the admission and release transactions — never by a background
job, never by a reconciliation sweep that recounts and writes. A recount-and-write
would race with a concurrent admission and lose writes under exactly the load
where correctness matters.

### Which pools a task needs

`swarm_common.models.pool_names_for()`:

```
global
tenant:<tenant_id>
resource:<resource_class>
runner:<runner_profile>
backend:<CLOUD_RUN_JOB|GKE_AUTOPILOT>
provider:<provider>                       # only when the profile has one
provider:<provider>:tenant:<tenant_id>    # because tenants bring their own keys
```

The last one is the interesting one. Provider quota is tracked **per tenant**
because each tenant's key has its own rate limit. A provider-wide throttle
triggered by one tenant's 429 would let that tenant halt everyone else's work —
the exact cross-tenant blast radius the platform exists to prevent.

A pool name that has no document is treated as unlimited. Terraform therefore
materialises a document for **every** name the catalogue can generate, so
"nobody capped this" and "we deliberately left it uncapped" never look alike.

---

## 2. The admission transaction

`swarm_common/admission.py::acquire_lease_in_transaction`, inside a Firestore
transaction, which Firestore aborts and retries if any document it read changed:

```
1. re-read the task; confirm it is still READY and not cancel_requested
2. read every applicable pool
3. confirm every pool has capacity for `units`
4. increment `active` on every pool
5. write leases/{id}
6. move the task READY -> LEASED, bump current_generation, attempt_count += 1
```

All six, or none.

**Step 1 re-reads rather than trusting the caller's copy.** The scheduler queried
this task some milliseconds ago; since then it may have been cancelled, or
admitted by another scheduler instance. Optimistic concurrency only works if the
condition is re-checked inside the transaction.

**Step 4 is why partial reservation is impossible.** Reserving three pools and
failing on the fourth would leak capacity permanently: no lease exists, so
nothing will ever release it, and the platform silently shrinks. There is
deliberately no code path in the frozen module that can produce that state.

**The last-slot race** resolves because both transactions read the same pool
document. One commits; the other's read set is now stale, Firestore aborts it and
re-runs the whole function against fresh reads, where step 3 now fails and raises
`AdmissionDenied`. Two winners is not a race the code has to handle — it is a
state Firestore will not let exist.

### AdmissionDenied is not an error

It is the normal answer to "is there room right now?", carrying structured
blockers:

```json
[{"pool": "provider:anthropic:tenant:eng",
  "reason": "PROVIDER_CONCURRENCY_LIMIT", "limit": 10, "active": 10}]
```

These land in `task.blocked_by` and are returned verbatim by `GET /v1/tasks/{id}`,
so a caller can tell "the platform is busy" from "you personally are at your
limit" — `GLOBAL_CONCURRENCY_LIMIT` versus `TENANT_LIMIT`.

The drain loop **continues** after a denial. Breaking out on the first one is
how a single full pool stalls every other tenant's work: the next task in the
rotation usually belongs to a different tenant and is usually admissible.

---

## 3. Release

`release_lease_in_transaction` returns `units` to every pool the lease held and
stamps `released_at`. It is **idempotent per lease**: if `released_at` is already
set it returns `false` and changes nothing.

That matters more than it looks. Three actors can race to release the same lease:

* the worker, finishing normally;
* the reconciler, having decided the worker is dead;
* the cancellation path.

A double release decrements pools below their true `active`, silently inflating
capacity — the platform then admits more than its limit and nothing reports an
error. The idempotency check is one comparison and removes the entire class.

Every terminal state releases exactly once. `scripts/failure-test.sh` asserts
this across normal completion, task failure, cancellation mid-run, and malformed
input, because a leaked lease is worse than the failure that leaked it.

---

## 4. Capacity units

Not every task costs the same. `ResourceClass.units` weights them:

| Class | cpu | memory | workspace | units |
|---|---|---|---|---|
| `standard` | 4 | 8 GiB | 4 GiB of the 8 | 1 |
| `browser` | 8 | 16 GiB | 8 GiB of the 16 | 2 |
| `large` | 8 | 32 GiB | 16 GiB of the 32 | 4 |

**The workspace column is memory, and it is a slice of the memory column, not
capacity beside it.** This table previously read 20/40/100 GiB of disk, which was
wrong by more than an order of magnitude and wrong in kind: disk-backed ephemeral
storage is a Cloud Run Preview feature the Terraform google provider cannot
express -- `empty_dir.medium` accepts only `"MEMORY"` -- so the workspace is a
tmpfs carved out of the container's own memory. Adding `memory` and `workspace`
together, or planning a node budget from the old disk figures, overstates what a
class holds and understates what it costs. `swarm_common.profiles.ResourceClass`
is the authority; `disk_gib` there is the slice.

`max_active_agents` caps the number of agents; `global_capacity_units` caps their
weight. Both exist because 100 `standard` agents and 25 `large` agents are the
same count and very different machines.

---

## 5. Fairness

Two mechanisms, deliberately separate, solving two different problems.

**Aging** fixes starvation *within* a tenant: effective priority rises by
`aging_step` every `aging_interval_seconds` waited, capped at `aging_max_bonus`.
The cap keeps aging from inverting the priority scheme permanently — an old
trivial task should eventually overtake a fresh medium one, never outrank a fresh
urgent one forever.

**Round-robin** fixes starvation *across* tenants, and it is the reason a tenant
cannot take the platform by submitting ten thousand priority-100 tasks. Ordering
purely by priority would let them, because priority is a number the caller
chooses. Instead tenants take turns: one task each per round. Priority and aging
decide which of a tenant's own tasks goes first; they never decide how many turns
a tenant gets.

One subtlety worth knowing when reading the scheduler: the global candidate query
returns the highest-priority slice, so a tenant with more high-priority tasks than
`candidate_batch_size` can fill that slice entirely and disappear the rest from
the rotation — round-robin cannot interleave what it never read. When the slice
comes back *full*, the scheduler tops up with a few `READY` tasks from each
unrepresented tenant, bounded by `max_topup_tenants` so a pass stays cheap.

The interleave is a pure function of its inputs, so the fairness property is
tested without Firestore, a clock or a dispatcher.

---

## 6. Changing limits

Limits live in Firestore, **not** in code and not in environment variables, so
they change without a redeploy:

```bash
# Global ceiling
./scripts/api.sh PUT /admin/limits/global '{"hard_limit": 150}'

# One provider, one tenant
./scripts/api.sh PUT /admin/limits/provider/anthropic '{"hard_limit": 40}'
./scripts/api.sh PUT /admin/limits/tenant/eng         '{"hard_limit": 25}'

# What every pool is doing right now
./scripts/status.sh
```

Lowering a limit below current `active` does **not** kill anything. Running tasks
keep their leases and finish; the pool simply admits nothing until `active` falls
below the new ceiling. Draining is the deliberate, non-destructive shape — see
`POST /v1/admin/providers/{provider}/drain`.

---

## 7. Timeouts that keep accounting honest

| Setting | Default | What it protects |
|---|---|---|
| `dispatch_timeout_seconds` | 300 | A lease that never became an execution. The reconciler reclaims it. |
| `lease_timeout_seconds` | 120 | A worker that stopped heartbeating. |
| `heartbeat_interval_seconds` | 30 | How often a live worker proves it. |

`Settings.__post_init__` refuses to start if `lease_timeout <= heartbeat_interval`,
because that configuration reaps every healthy worker in the gap between two
heartbeats — a platform that looks broken in a way that reads like flakiness.

---

## 8. Verifying it

```bash
make concurrency-test    # continuous sampling under deliberate overload
make race-test           # last free slot, cancel-during-dispatch, stale generation
make failure-test        # every failure path returns its capacity
make test                # the pure admission logic, exhaustively, no emulator
```

`race-test.sh` narrows a **narrow** pool (`runner:mock`, and only that one)
rather than the global pool, so the rest of the platform keeps working while it
runs, and it restores the original limit on every exit path. Both the narrow and
the restore go through `PUT /v1/admin/limits/runner/mock`, the same route an
operator uses, so the suite never writes `active` — the counter only admission
and release may move.
