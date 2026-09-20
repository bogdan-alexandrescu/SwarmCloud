# Contract invariants 1, 2 and 3, observed under real contention

2026-09-20. The first evidence any of these has had against a deployed
environment. `make smoke concurrency-test race-test` has been unable to reach
the API since the load balancer went in, so until now the concurrency
guarantees were asserted by unit tests and by reading the code.

This is **observation, not proof**. It happened once, under one shape of load,
with a human watching. It is weaker than the gate and does not replace it.
What it is not is nothing.

---

## Method

Four real `claude-code` tasks were already running (agents working on this
repository). Thirty `mock` tasks were submitted in one batch on top.

`mock` was chosen over `claude-code` deliberately: it shares the `global`,
`tenant:u-bogdan`, `resource:standard` and `backend:CLOUD_RUN_JOB` pools, so
it contends for admission identically, and it needs no provider key, so
thirty of them cost nothing in provider quota. The invariant is exercised the
same way; only the bill differs.

Note which pool binds. `tenant:u-bogdan` allows 40 and `global` allows 20, so
the refusals are `GLOBAL_CONCURRENCY_LIMIT`, not `TENANT_LIMIT`. That
distinction is the one the UI exists to make: a tenant limit is yours to
raise, a global limit means the platform is full.

## What was observed

Three samples, seven seconds apart, all identical:

```
global pool     active 20 / effective_limit 20 / available 0
live leases     20
units held      20
READY           13
blocked         13
reason          GLOBAL_CONCURRENCY_LIMIT @ global, 20/20 at refusal
```

## What each number establishes

**Invariant 2 — capacity is reserved all-or-nothing across every pool, in one
transaction.** `active` reached exactly 20 and never 21, under 34 concurrent
submissions, stable across three samples. A partial reservation or a lost
transaction would show as an overshoot or as a pool disagreeing with its
siblings. Neither appeared.

**Invariant 3 — concurrency counts from LEASED, not RUNNING.** The three
independent counters agreed exactly: 20 live leases, 20 units held, 20 active
on the global pool. These are written by different code paths --
`admission.py` increments the pool, the lease document is a separate write,
and `units_held` is summed by the API at read time. Agreement across all
three is what "counted from LEASED" looks like from the outside.

**Invariant 1 — QUEUED, PARKED and READY create no infrastructure demand.**
Thirteen tasks sat in READY holding zero units. They were refused, recorded
why, and cost nothing while they waited.

**And the drift check does not cry wolf.** `sum(lease.units)` equalled
`pool.active` exactly, so the accounting-drift panel correctly showed
nothing. A leak detector that fires on a healthy platform is worse than none,
and this is the harder half of trusting it.

## What this did NOT test

**`race-test`'s question: does the last free slot go to exactly one task?**
This probe filled a pool and watched the overflow be refused. It never
created the specific race where two admissions contend for one remaining
slot. That is the subtlest of the three guarantees and remains unproven here.

Also untested: behaviour when a pool is paused mid-flight, when a lease is
reclaimed by the reconciler under load, and anything at all about a second
tenant.

## Cleanup

The thirteen refused probes were cancelled rather than left to drain, because
they were competing for slots with real work. Cancellation on a READY task
releases immediately -- `released_immediately: true` -- since nothing had
been dispatched.
