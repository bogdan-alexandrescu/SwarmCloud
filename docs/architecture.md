# Architecture

A control plane that runs long-lived coding agents on Google Cloud without ever
killing one by accident, and without paying for work that is waiting.

Everything here follows from two requirements the operator stated as absolute:
**no preemption, no OOM kills, no restarts**, and **queued work must cost
nothing**. Most of the decisions below look unusual until you hold them against
those two sentences, at which point they are the only options left.

---

## 1. The shape

```
        caller (Google ID token, hd=saga.xyz)
          |
          v
   +--------------+     Pub/Sub wake      +---------------+
   |  swarm-api   | --------------------> |   scheduler   |
   | (Cloud Run)  |                       | (Cloud Run)   |
   +--------------+                       +---------------+
          |                                   |        |
          | writes tasks                      |        | dispatch
          v                                   v        v
   +---------------------------+     Cloud Run Jobs   GKE Autopilot
   |  Firestore database swarm |      (primary)       (browser/GPU/>32GiB)
   |  tasks leases pools quota |          |                 |
   |  tenants attempts workflows|         v                 v
   +---------------------------+     agent-worker      agent-worker
          ^        ^                      |                 |
          |        |                      +--------+--------+
   +--------------+  +-----------------+           |
   | quota-broker |  |   reconciler    |           v
   | (Cloud Run)  |  |  (Cloud Run)    |     GCS: checkpoints,
   +--------------+  +-----------------+     artifacts, logs
```

Five processes. Three of them (`swarm-api`, `quota-broker`, `swarm-reconciler`)
are ordinary request/response services; `swarm-scheduler` is a service that only
ever runs bounded drains; `agent-worker` is the thing that actually runs an
agent, as a Cloud Run Job execution or a GKE Job pod.

| Component | Where it runs | Triggered by | Source |
|---|---|---|---|
| `swarm-api` | Cloud Run service | callers, over HTTPS | `apps/swarm-api/` |
| `swarm-scheduler` | Cloud Run service | Pub/Sub push + 1-minute Cloud Scheduler tick | `apps/scheduler/` |
| `swarm-quota-broker` | Cloud Run service | workers, scheduler, quota-refresh tick | `apps/quota-broker/` |
| `swarm-reconciler` | Cloud Run service | Cloud Scheduler tick | `apps/reconciler/` |
| `agent-worker` | Cloud Run Job execution / GKE Job pod | the scheduler's dispatcher | `apps/agent-worker/` |
| shared contract | imported by all of the above | — | `apps/common/swarm_common/` (FROZEN) |

---

## 2. The one idea everything else hangs off

**A lease, not a container, is what "running" means.**

`swarm_common.states` defines twelve task states, of which exactly four hold
capacity:

```
SUBMITTED -> QUEUED -> READY -> LEASED -> DISPATCHED -> STARTING -> RUNNING -> SUCCEEDED
                 \       ^                                              \
                  \      |                                               -> FAILED / CANCELLED
                   -> PARKED                                             -> PARKED (quota)
```

`CONCURRENCY_STATES = {LEASED, DISPATCHED, STARTING, RUNNING}`.

`QUEUED`, `PARKED` and `READY` are Firestore documents and nothing else. Ten
thousand queued tasks produce zero pods, zero Cloud Run executions, zero nodes
and zero dollars. This is why the platform never uses "pending pods" as a
backlog: a pending pod is a scheduling request the cluster autoscaler answers by
buying a node.

Counting from `LEASED` rather than `RUNNING` is the second half of the idea. A
container that has been admitted but is still pulling a 2 GiB image already
occupies its slot. Counting from `RUNNING` would let a burst of slow starts
oversubscribe every pool at once and then OOM the node they land on.

See [concurrency.md](concurrency.md) for the admission transaction itself.

---

## 3. Request path

### Submission (`POST /v1/tasks`)

1. **Authenticate.** A Google ID token is verified and the `hd` claim must be an
   allowed domain. There is no shared bearer token anywhere in this platform —
   see [security.md](security.md#authentication) for why that is a design
   decision rather than an omission.
2. **Resolve the tenant.** The caller's tenant is their highest-priority
   admin-registered Google group, or a personal fallback tenant `u-<user>`. The
   membership check is per group, never an enumeration — see
   [multi-tenancy.md](multi-tenancy.md#group-resolution).
3. **Validate against the frozen catalogue.** The caller names a
   `runner_profile`. Image, command, resource class, backend, provider and
   secrets all come from `swarm_common.profiles`; a request carrying any of them
   is rejected with 422, not silently ignored.
4. **Write `tasks/{id}` as SUBMITTED -> QUEUED**, then publish a wake message.

The API never admits anything. It writes a document and rings a bell.

### Admission (scheduler)

Woken by Pub/Sub, the scheduler runs **one bounded drain and exits**:

* promote what has become runnable (dependencies satisfied, credentials now
  present, cooldowns about to expire);
* read a slice of `READY` tasks;
* interleave them round-robin across tenants, with starvation aging inside each
  tenant;
* for each, run the admission transaction; on `AdmissionDenied`, record
  `blocked_by` and **move to the next task** rather than stopping.

It is bounded three independent ways — wall clock, admissions, passes — because
a scheduler that runs long is a scheduler that gets killed mid-transaction.
Whatever it could not admit stays `READY`, costing nothing, until the next wake
or the 1-minute safety tick.

### Dispatch

Strictly downstream of the lease. Cloud Run Jobs is the default;
`browser` goes to GKE Autopilot because Chromium needs a `/dev/shm` Cloud Run
will not size. Every dispatch failure hands the capacity straight back.

Cloud Run sets the service account **on the Job resource**, and it cannot be
overridden per execution. One shared Job would therefore run every tenant's
work under one identity and make tenant isolation unenforceable. So the
dispatcher maintains one Job resource per `(tenant, profile)` bound to that
tenant's service account, and the reconciler garbage-collects unused ones. See
[execution-backends.md](execution-backends.md).

### Execution (worker)

The worker's order is the contract, and step 1 is the safety property that
matters most:

1. **validate the fencing generation** — before the workspace exists, before a
   secret is read, before the runner starts;
2. STARTING -> RUNNING;
3. create an isolated workspace;
4. restore the newest checkpoint across all attempts of this task;
5. optional shallow clone;
6. resolve the tenant's provider credential;
7. start the runner as a child process (never a shell);
8. heartbeat, **checkpoint every 120 s**, watch for cancellation, quota
   exhaustion and generation changes;
9–13. capture output, upload artifacts and a final checkpoint, persist the
   terminal state, release the lease, exit.

A worker whose generation is stale emits `generation_fenced` and exits **without
touching the lease** — the live lease belongs to the attempt that replaced it.

### Reconciliation

Every Cloud Scheduler tick, the reconciler compares what the control plane
believes with what the backends report and repairs the difference in the one
order that cannot cause duplicate execution:

```
1. invalidate the generation   (the running worker now fences itself)
2. terminate the execution     (and confirm the backend accepted it)
3. release the slot            (only if 2 succeeded)
4. repair the task state       (READY, or FAILED if attempts are spent)
```

Releasing before terminating would hand the slot to the scheduler while the old
agent is still writing to a tenant's repository. A termination that is not
confirmed therefore does **not** release: one stuck slot until the next pass is
a far cheaper mistake than two agents on one workspace.

---

## 4. Data model

Firestore, database `swarm` — **not** `(default)`, which in this shared project
belongs to whoever created it first.

| Collection | Holds | Written by |
|---|---|---|
| `tasks/{id}` | the task and its denormalised admission inputs | api, scheduler, worker, reconciler |
| `tasks/{id}/events/{id}` | append-only audit trail | everything |
| `attempts/{id}` | one execution: backend, execution name, exit code, peak RSS | scheduler, worker |
| `leases/{id}` | the authoritative capacity reservation | admission / release transactions only |
| `pools/{name}` | one concurrency budget, with `active` | admission / release transactions, quota broker, admin API |
| `quota/{provider}:{tenant}` | provider health per tenant key | quota broker |
| `tenants/{id}` | limits, registered providers, GSA, GCS prefix, namespace | admin API, `register-tenant.sh` |
| `workflows/{id}` | DAG of steps | api, scheduler |

Firestore has no joins, so a task document carries everything admission needs —
tenant, provider, resource class, runner profile, priority — and the hot path is
one query plus the pool documents.

`pool_names_for()` produces every pool a task must hold simultaneously:

```
global
tenant:<tenant>
resource:<class>
runner:<profile>
backend:<backend>
provider:<provider>                    (when the profile has one)
provider:<provider>:tenant:<tenant>    (because keys are per tenant)
```

Terraform materialises a document for every name this can generate. A missing
pool is treated as unlimited, so "we forgot to create it" and "we chose not to
cap it" must never look the same.

---

## 5. Deliberate departures from the original design

These are changes from the brief this platform was built against. Each is here
because the alternative could not satisfy "no preemption, no OOM, no restarts".

### Cloud Run Jobs is the primary backend, not GKE Autopilot

GKE Autopilot has nodes; nodes have autoscalers, upgrades, repairs and pressure
eviction. Every one of those is a mechanism that can end a running agent for
reasons unrelated to the agent. Cloud Run Jobs has no nodes to upgrade, no
autoscaler to compact workloads onto fewer machines, and no node pool to repair.
Autopilot is retained for exactly three cases: browser work (needs a large
`/dev/shm`), GPU work, and anything needing more than 32 GiB.

### Spot is disabled platform-wide

Spot looks like an obvious saving and is verifiably incompatible with the
requirement: **Spot Pods cannot use GKE Autopilot extended run time**. Extended
run time is the feature that stops Autopilot evicting a long-running pod for
consolidation. So "Spot preferred" and "no preemption" are mutually exclusive,
and the frozen `RunnerProfile.__post_init__` raises if a profile tries to set
anything but `ON_DEMAND_ONLY`. See [cost-control.md](cost-control.md).

### requests == limits, no bursting

Bursting past a request is precisely what gets a container OOM-killed when the
node comes under pressure. A burst that usually works is a burst that fails on
the busiest day. Both numbers are set to the same value everywhere.

### Resource classes were measured, not guessed

One working Claude Code lane on the reference workstation, measured 2026-09-15:

| Process | RSS |
|---|---|
| `claude` | 1,532 MB |
| `pytest` | 769 MB |
| node/tsx guards | 207 MB |
| **total** | **~2.5 GiB** |

`standard` is 4 vCPU / 8 GiB — roughly 2x measured, because an agent's peak is
not its average and the cost of being wrong is an OOM kill. `browser` is
8 / 16 (Chromium), `large` is 8 / 32 (the Cloud Run ceiling). The worker reports
peak RSS and peak disk per attempt so these get corrected from production rather
than from arithmetic. See [scaling.md](scaling.md#right-sizing).

### A known risk, accepted deliberately

Cloud Run's ephemeral (second-generation) disk is **Preview**, and per Google's
documentation enabling it **disables live migration**. Live migration is part of
why Cloud Run was chosen for long jobs, so this specific feature partially
undermines the reason for the choice. It is accepted, not hidden, and the
compensation is mandatory 120-second checkpointing. Read
[checkpointing.md](checkpointing.md) and
[cost-control.md](cost-control.md#the-preview-disk-tension) before changing
either the disk configuration or the checkpoint interval.

### Multi-tenant from V1

A tenant is a Google group, with a personal fallback tenant so nobody is ever
hard-blocked. Each tenant gets its own service account, its own secrets, its own
GCS prefix and its own Kubernetes namespace, and brings its own provider keys.
Retrofitting tenancy means retrofitting every IAM binding and every storage path
at once; doing it first costs a fraction of that.

### Auth is Google ID tokens, with no shared platform token

A shared bearer token carries no identity. Without identity there is no tenant to
attribute a task to, no way to scope a list response, and no boundary to
enforce — so multi-tenancy would be decorative. Tokens are verified per request
and the hosted domain must be `saga.xyz`.

---

## 6. Where the code enforces each invariant

| Invariant | Enforced in |
|---|---|
| 1. Only LEASED+ costs money | `swarm_common/states.py`, scheduler `loop.py` |
| 2. All-or-nothing reservation | `swarm_common/admission.py` (one transaction) |
| 3. Count from LEASED | `CONCURRENCY_STATES` |
| 4. Never sleep through a long wait | `agent_worker/quota.py` |
| 5. Fencing generations | `agent_worker/lifecycle.py` step 1, `reconciler/repair.py` step 1 |
| 6. No Spot | `profiles.RunnerProfile.__post_init__` |
| 7. requests == limits | `scheduler/dispatch.py`, Terraform job/pod specs |
| 8. Mandatory checkpointing | `agent_worker/checkpoint.py` |
| 9. Per-tenant isolation | `swarm_api/credentials.py`, `agent_worker/secrets.py`, Terraform `tenancy` module |
| 10. Callers pick profiles by name | `swarm_api/validation.py` |

`apps/common/swarm_common/` is frozen (see `CONTRACT.md`). Import from it; never
restate its types. Terraform restates the catalogue in `terraform/infra/locals.tf`
only because Terraform cannot import Python, and a test asserts the restatement
still matches.
