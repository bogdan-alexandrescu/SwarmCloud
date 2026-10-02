# Scaling

How the platform grows, what binds first, and how to tell which limit you are
actually hitting.

---

## 1. Scale the pools, not the services

The control plane is stateless and autoscales on its own. **Capacity is a number
in Firestore, not a deployment.** Growing the fleet means raising slot pool
limits; the control plane follows.

```bash
./scripts/api.sh PUT /admin/limits/global '{"hard_limit": 250}'
```

No redeploy, no Terraform apply, effective on the next scheduler pass. Lowering
kills nothing: running tasks keep their leases and the pool simply admits nothing
until `active` falls below the new ceiling.

The Terraform `pool_limits` in `terraform/environments/<env>/<env>.tfvars` is the
**declared baseline** — what a fresh apply sets. Live tuning through the admin
API is expected; fold a change you intend to keep back into the tfvars, or the
next apply will undo it.

---

## 2. What binds first

In rough order of what you will hit:

| Limit | Default | Symptom | Fix |
|---|---|---|---|
| `provider:<p>:tenant:<t>` | 5–10 | one tenant's work queues while others run | raise per-tenant provider cap, or add a key |
| `provider:<p>` | 10–60 | all tenants' work on that provider queues | raise the hard max; AIMD may still cap below it |
| `tenant:<id>` | 20 | one tenant's tasks queue, platform is idle | raise the tenant's `max_active` |
| `global` | 20 (dev) / 100 (prod) | everything queues evenly | raise the global pool |
| Cloud Run Jobs quota | project quota | dispatch errors, tasks return to READY | request a quota increase |
| Firestore write contention | ~1 write/s per document | admission transactions retry and slow | see §5 |
| Scheduler drain budget | 45 s / 200 leases / 25 passes | admission latency rises under a large backlog | see §4 |

`status.sh` tells you which one, because it prints `active/effective_limit` for
every pool. `blocked_by` on a queued task names the exact pool and reason —
`TENANT_LIMIT` and `GLOBAL_CONCURRENCY_LIMIT` are different problems with
different fixes.

---

## 3. Capacity units, not agent counts

100 `standard` agents and 25 `large` agents are the same count and very different
machines:

| Class | cpu | memory | units | 100 units buys |
|---|---|---|---|---|
| `standard` | 4 | 8 GiB | 1 | 100 agents (400 vCPU, 800 GiB) |
| `browser` | 8 | 16 GiB | 2 | 50 agents (400 vCPU, 800 GiB) |
| `large` | 8 | 32 GiB | 4 | 25 agents (200 vCPU, 800 GiB) |

`max_active_agents` caps the count; `global_capacity_units` caps the weight. Set
the units budget from what you are willing to run concurrently in vCPU and GiB,
then let the count follow.

---

## 4. Scheduler throughput

One drain is bounded three ways, whichever trips first:

```
MAX_RUN_SECONDS       45
MAX_LEASES_PER_RUN    200
MAX_PASSES_PER_RUN    25
CANDIDATE_BATCH_SIZE  200
```

So one instance admits up to 200 tasks per wake. Sustained throughput is
`200 / (wake interval)`, and wakes are event-driven (every submission publishes),
with a 1-minute safety tick as the floor.

This is deliberately not "run until the queue is empty". A scheduler that runs
long is a scheduler that gets killed mid-transaction, and whatever it could not
admit stays `READY` at zero cost until the next wake.

If admission latency is the bottleneck rather than capacity:

1. raise `MAX_LEASES_PER_RUN` and `CANDIDATE_BATCH_SIZE` together — a batch
   smaller than the leases budget starves the loop of candidates;
2. raise the Cloud Run `max_instances` for `swarm-scheduler`. Concurrent
   schedulers are safe: the admission transaction resolves the last-slot race to
   exactly one winner (see [concurrency.md](concurrency.md));
3. only then consider shortening the safety tick. The tick is a floor, not the
   mechanism — if it is doing real work, the wake path is broken.

Round-robin fairness has one interaction worth knowing at scale: the candidate
query returns the highest-priority slice, so a tenant with more than
`CANDIDATE_BATCH_SIZE` high-priority tasks can fill it entirely. When the slice
comes back full, the scheduler tops up with a few `READY` tasks from each
unrepresented tenant, bounded by `MAX_TOPUP_TENANTS` so a pass stays cheap.

---

## 5. Firestore

The hot documents are the pools. Every admission reads and writes `global` plus
five to seven narrower pools, inside a transaction. Firestore sustains roughly
one write per second per document before contention becomes visible as
transaction retries.

At 200 admissions/minute (~3.3/s) the `global` pool is the contended document.
Mitigations, in order of preference:

1. **Fewer admissions, not faster ones.** Longer tasks admit less often. This is
   an agent platform: a task is minutes to hours, so 3/s is already a very large
   fleet.
2. **Narrower pools absorb the rest.** Per-tenant and per-provider pools spread
   writes across documents naturally as tenants are added.
3. **If `global` genuinely saturates**, shard it (`global:0..N`, assigned by
   hash) — a change to the frozen `pool_names_for()`, so raise it as a contract
   change rather than editing. "Genuinely" is defined below, and it is a
   measurement, not an estimate.

### Is `global` saturated? Measure it

Spec §8 row 6 asks the question and says to measure before designing the
sharding. Two instruments exist:

* **In production, every admission logs its contention.**
  `SchedulerStore.acquire_lease` writes one record per admission with
  `admission_outcome` (`leased` / `denied` / `aborted` / `error`),
  `admission_runs`, `admission_reruns` and `admission_latency_ms`. A re-run is
  Firestore aborting the commit because a pool it read changed, and
  `firestore.transactional` running the body again. Leases, re-runs and
  failures log at INFO or above. A first-run denial is DEBUG, because a full
  pool denies every READY task on every pass and that is not contention. To
  read it:

  ```
  resource.labels.service_name="swarm-scheduler"
  jsonPayload.logger="scheduler.store"
  jsonPayload.admission_reruns>0
  ```

  The same numbers are Prometheus series on the scheduler's `/metrics`:
  `swarm_scheduler_admission_seconds{outcome}` (a histogram with a bucket at
  0.225 s, rule 2's line) and `swarm_scheduler_admission_reruns_total{outcome}`.
* **Before production finds out, the bench.** `scripts/bench-contention.sh`
  drives the same `acquire_lease`, and so the frozen admission, at 100, 200
  and 400 offered admissions per minute against a dedicated `swarm-bench`
  database, and prints p50/p95/p99 latency, re-runs and aborts with n
  ([benchmarks.md](benchmarks.md#contention--the-admission-transaction-under-offered-load-s32)).

**Saturated** means any of these, at a rate the platform must sustain:

1. **any admission aborted.** Firestore exhausted its retries and
   `firestore.transactional` raised. `Scheduler._admit_one` catches only
   `AdmissionDenied` around `acquire_lease`, so the exception ends the whole
   drain, not just that task. Nothing was reserved, which is the "nothing"
   half of all-or-nothing, but every READY task behind it waits for the next
   wake;
2. **p95 admission latency above 225 ms.** That is `MAX_RUN_SECONDS` 45 s ÷
   `MAX_LEASES_PER_RUN` 200: above it, a drain spends its time budget on
   admission before it reaches its lease budget, before any dispatch work is
   counted;
3. **p95 latency at 400/min more than twice the 100/min row.** Latency that
   rises with offered rate is contention. Latency flat across rates is distance
   to Firestore, and sharding does not touch distance.

The re-run columns say *why* a row crossed a line. They are not a threshold
of their own.

**Result: not yet measured (2026-10-02).** The harness and the log field
exist. The bench database is defined in Terraform
(`terraform/modules/firestore/bench.tf`, dev only) and is created by the next
dev apply; until then nothing has been measured. No sharding change is drafted, because the
rule above has not been applied to a number. When it has, record the table
here with its date, environment and n.

### If the numbers show saturation: what a sharding request must keep

Any contract change request for sharding `global` must keep invariant 2 as it
stands. **Every pool, every shard included, is reserved inside the ONE
admission transaction, or none is.** A design that reserves shard-by-shard
and rolls back on failure is not equivalent. The rollback is a second
transaction, and v1 produced a permanent capacity leak from exactly that shape
(spec §7.1, `docs/BUILD_PROMPT_V2.md:692-697`). The request must also answer
two questions:

* **How is a shard's limit set?** Splitting a limit L into N shards of L/N
  denies a task whose shard is full while another shard has room, which caps
  the platform below L. The request must say how much below, and why that is
  acceptable.
* **Which shard does a task take?** A hash of the task id keeps the read set
  at one shard. Reading every shard to choose one restores the contention the
  sharding exists to remove, because each of those reads conflicts with
  another admitter's write.

Reads are cheap by construction: `status.sh` uses server-side aggregation
queries, so counting a hundred thousand tasks is one request and zero document
reads.

---

## 6. Right-sizing

The classes were measured, not guessed. One working Claude Code lane on the
reference workstation, 2026-09-15:

```
claude            1,532 MB
pytest              769 MB
node/tsx guards     207 MB
                 ----------
                  ~2.5 GiB
```

`standard` is 8 GiB — roughly 2x measured — because an agent's peak is not its
average, and the cost of being wrong is an OOM kill, which is the one failure the
platform exists to avoid.

**Correct these from production, not arithmetic.** Every attempt records
`peak_rss_bytes`, `peak_disk_bytes` and `oom_near_miss`:

```bash
# p95 peak RSS by profile, last 200 attempts
./scripts/api.sh GET /stats | jq '.resource_usage'
```

Raise a class when p95 peak approaches 80% of the limit or `oom_near_miss` shows
up. Lower one only when p99 sits well under half — and remember that lowering
memory does not save money on Cloud Run unless it also lets more agents fit, which
it does not, because there are no nodes to pack.

---

## 7. Growth playbook

**More tenants.** Register each one (`register-tenant.sh`); they arrive with
`max_active` 20 so a new tenant's first bad loop is survivable. Per-tenant pools
also spread Firestore writes, so tenant growth scales better than fleet growth
inside one tenant.

**More providers.** Add the provider to the profile catalogue (a frozen-module
change), add `provider:<p>` and `provider:<p>:tenant:<t>` pool limits to the
tfvars, and have each tenant register a key. AIMD starts conservatively and
climbs.

**More regions.** Not supported today, and the reason is Firestore: the control
plane's correctness rests on single-region transactional consistency. A second
region means either cross-region transactions (slower, and the admission path is
the hot path) or a second independent control plane with its own pools — which is
the honest design, and a project rather than a config change.

**More backends.** See
[execution-backends.md](execution-backends.md#6-adding-a-backend). The step people
skip is teaching the reconciler to terminate it; without that, repair cannot
safely release slots on the new backend.

---

## 8. Measuring

```bash
make load-test                              # 100 tasks at 10/s
./scripts/load-test.sh --count 500 --rate 25
```

Reports p50/p90/p99 for admission latency (submitted -> LEASED) and end-to-end
latency (submitted -> terminal), plus throughput.

Reference expectations on the `mock` profile, which isolates the control plane
from provider time:

| Metric | Healthy | Investigate |
|---|---|---|
| Admission p50 (capacity available) | < 2 s | > 10 s: wake path or scheduler budget |
| Admission p99 under backlog | < 60 s | > 120 s: raise leases/run or instances |
| Cold start to RUNNING | 30–90 s | > 180 s: image size or registry region |
| End-to-end p50 (`mock`) | < 3 min | — |

Admission latency that rises with **queue depth** is a scheduler budget problem.
Admission latency that rises with **fleet size** is a pool ceiling — which is not
a problem, it is the ceiling doing its job.
