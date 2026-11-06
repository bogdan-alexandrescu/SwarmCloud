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
   |  Firestore database swarm |   (mock, fallback)  (every other agent)
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
| `agent-worker` | GKE Job pod (every agent profile but `mock`) / Cloud Run Job execution (`mock`, and the fallback Jobs) | the scheduler's dispatcher | `apps/agent-worker/` |
| shared contract | imported by all of the above | — | `apps/common/swarm_common/` (FROZEN) |

---

## 2. The one idea everything else hangs off

**A lease, not a container, is what "running" means.**

`swarm_common.states` defines twelve agent states, of which exactly four hold
capacity:

```
SUBMITTED -> QUEUED -> READY -> LEASED -> DISPATCHED -> STARTING -> RUNNING -> SUCCEEDED
                 \       ^                                              \
                  \      |                                               -> FAILED / CANCELLED
                   -> PARKED                                             -> PARKED (quota)
```

`CONCURRENCY_STATES = {LEASED, DISPATCHED, STARTING, RUNNING}`.

`QUEUED`, `PARKED` and `READY` are Firestore documents and nothing else. Ten
thousand queued agents produce zero pods, zero Cloud Run executions, zero nodes
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
   [multi-tenancy.md](multi-tenancy.md#2-group-resolution--the-constraint-that-shapes-the-api).
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
* read a slice of `READY` agents;
* interleave them round-robin across tenants, with starvation aging inside each
  tenant;
* for each, run the admission transaction; on `AdmissionDenied`, record
  `blocked_by` and **move to the next agent** rather than stopping.

It is bounded three independent ways — wall clock, admissions, passes — because
a scheduler that runs long is a scheduler that gets killed mid-transaction.
Whatever it could not admit stays `READY`, costing nothing, until the next wake
or the 1-minute safety tick.

### Dispatch

Strictly downstream of the lease. Every dispatch failure hands the capacity
straight back. Where each profile runs, as the catalogue
(`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`) declares it and as
the 2026-10-21 system analysis found it running (read-only `gcloud` and
`kubectl`, epic #1822):

* **GKE Autopilot runs nearly every agent:** `browser` (Chromium needs a
  `/dev/shm` Cloud Run will not size), `claude-code` since 2026-10-08
  (contract request 53, a start of about 23 s against Cloud Run's 128 s
  median), `indexer`, `generic`, `codex` and `merge` since 2026-10-10
  (contract requests 63-66, for their internet path), and
  `claude-code-browser`, and `claude-code-large` (contract request 113,
  2026-10-22): `claude-code` on the `large` class, 8 CPU and 32 GiB, with a
  14,400 s timeout, on-demand, for a lane that was OOM-killed or timed out,
  and `claude-code-lightpanda` (contract request 127, 2026-10-31):
  `claude-code` on the `agent-runtime-lightpanda` image and the `standard`
  class, a headless browser with no live view, for DOM-only browser work.
* **Cloud Run Jobs runs `mock`**, which reaches no internet, the disabled
  `post-verdict` and `claude-code-review`, and the fallback Jobs Terraform
  still keeps for `claude-code`, `indexer`, `generic`, `codex` and `merge`
  (`cloud_run_fallback_profiles` in `terraform/infra/locals.tf`), which
  nothing dispatches to while those profiles name GKE.

The workspace differs by backend. On GKE the workspace, `/tmp` and HOME are
each a disk `emptyDir` counted against the pod's ephemeral-storage limit
(`apps/scheduler/scheduler/dispatch.py::GkeDisk`), plus a 20 GiB cache PVC
(`apps/scheduler/scheduler/dispatch.py::CACHE_VOLUME_GIB`) for tool caches,
which is a network disk and never an eviction. **tmpfs workspaces apply to
Cloud Run only**: there the workspace is a memory-backed slice of the
container's memory (§5, "A known risk, accepted deliberately").

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
4. restore the checkpoint an earlier attempt of this agent recorded, and
   nothing on a first attempt (#347; [checkpointing.md](checkpointing.md#3-restore)
   says why the prefix is not listed);
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
1. terminate the execution     (and confirm the backend accepted it)
2. in ONE Firestore transaction, only once 1 succeeded:
     invalidate the generation (a worker that wakes later fences itself)
     release the slot          (the frozen release, every pool at once)
     repair the task state     (READY, FAILED if attempts are spent,
                                CANCELLED if a cancel was requested)
```

Releasing before terminating would hand the slot to the scheduler while the old
agent is still writing to a tenant's repository. A termination that is not
confirmed therefore does **not** release: one stuck slot until the next pass is
a far cheaper mistake than two agents on one workspace.

**The fence is never written alone.** Until #560 the generation was invalidated
first, in a transaction of its own, before the kill. When the kill then failed,
the pass returned with the agent fenced and the lease still held. Every later
pass saw a superseded lease whose execution "belonged" to the obsolete-generation
rule, and that rule's kill failed the same way, so nothing more was ever
written. On 2026-10-04 four DISPATCHED agents held their leases like that for ten
hours, one with a cancel pressed on it. Now a pass that cannot finish the repair
writes nothing, and the next pass starts again from a clean state.

**A worker that never started is its own evidence, after a bounded wait.** The
lease never heartbeated, the attempt records no start, and the agent never
reached RUNNING. Once the lease is `NEVER_STARTED_RELEASE_SECONDS` old (20
minutes; 2.5 times the dispatch deadline, nearly five times the slowest start
measured), the repair goes ahead even if the kill is not confirmed or the
backend cannot be read. It is safe because the fence commits with the release:
a container that starts late checks its generation before it does anything, and
exits (invariant 5). A worker that **did** start is still held until something
proves its execution gone (#450). Until the wait is over, the hold is listed in
the pass's `held_past_ttl` and logged at ERROR once an hour, with the time left.

**A cancel with no live worker ends the agent at once.** The API cancels a
DISPATCHED or STARTING agent outright when its worker never started, or when its
generation is fenced and its lease has been silent for 90 s. In the cancel's
own transaction it releases the lease and fences the generation. An agent with a
live worker is only flagged, and the worker ends it. A LEASED agent is left to
the scheduler, which is between admission and dispatch and finishes a flagged
cancel itself.

---

## 4. Data model

Firestore, database `swarm` — **not** `(default)`, which in this shared project
belongs to whoever created it first.

| Collection | Holds | Written by |
|---|---|---|
| `tasks/{id}` | the agent and its denormalised admission inputs | api, scheduler, worker, reconciler |
| `tasks/{id}/events/{id}` | append-only audit trail | everything |
| `attempts/{id}` | one execution: backend, execution name, exit code, peak RSS | scheduler, worker |
| `leases/{id}` | the authoritative capacity reservation | admission / release transactions only |
| `pools/{name}` | one concurrency budget, with `active` | admission / release transactions, quota broker, admin API |
| `quota/{provider}:{tenant}` | provider health per tenant key | quota broker |
| `tenants/{id}` | limits, registered providers, GSA, GCS prefix, namespace | admin API, `register-tenant.sh` |
| `workflows/{id}` | DAG of steps | api, scheduler |
| `control/runner_sizes` | the current runner-size version, who moved it and when | admin API |
| `runner_size_versions/{version}` | one immutable runner-size table, its reason and what changed | admin API |

Firestore has no joins, so a task document carries everything admission needs —
tenant, provider, resource class, runner profile, priority — and the hot path is
one query plus the pool documents.

`pool_names_for()` produces every pool an agent must hold simultaneously:

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

### Cloud Run Jobs was chosen as the primary backend; GKE now runs nearly every agent

This was the platform's first decision, and the reasoning below still says
what each backend risks. It no longer describes where the work runs: since
2026-10-10 every agent profile but `mock` runs on GKE Autopilot (§3,
Dispatch), and `CONTRACT.md`'s "Primary backend Cloud Run Jobs" line is
drift, filed as a request in
[contract-change-requests.md](contract-change-requests.md) rather than edited.

GKE Autopilot has nodes; nodes have autoscalers, upgrades, repairs and pressure
eviction. Every one of those is a mechanism that can end a running agent for
reasons unrelated to the agent. Cloud Run Jobs has no nodes to upgrade, no
autoscaler to compact workloads onto fewer machines, and no node pool to repair.
Autopilot is retained for exactly three cases: browser work (needs a large
`/dev/shm`), GPU work, and anything needing more than 32 GiB -- and one
exception made for speed, not capability: `claude-code`, since contract
request 53 (2026-10-08), because Cloud Run's own provisioning held its start at
p50 128 s / p90 212 s while Autopilot starts it in about 23 s. That buys back
the node-side failure modes above for the profile that runs nearly every step;
mandatory periodic checkpointing makes them cost minutes, not the run, and
does not make them free ([execution-backends.md](execution-backends.md) §4).
`indexer` followed on 2026-10-10 (contract request 63), the canary for #939,
for its internet path rather than its start: a new Cloud Run instance's opens a
median 20.2 s after start, a GKE pod's 1.17 s. `generic`, `codex` and `merge`
followed it (contract requests 64-66, #939 option A, owner 2026-10-10) for the
same reason, leaving Cloud Run with `mock`, which reaches no internet, and the
disabled `post-verdict` and `claude-code-review`, each designed to run as an
account of its own that a GKE pod cannot be.

### Spot is disabled platform-wide

Spot looks like an obvious saving and is verifiably incompatible with the
requirement: **Spot Pods cannot use GKE Autopilot extended run time**. Extended
run time is the feature that stops Autopilot evicting a long-running pod for
consolidation. So "Spot preferred" and "no preemption" are mutually exclusive,
and the frozen `RunnerProfile.__post_init__` raises if a profile tries to set
anything but `ON_DEMAND_ONLY`. See [cost-control.md](cost-control.md).

### Memory never bursts; CPU may burst past its request

Memory and ephemeral storage are one number on every container: request ==
limit. Memory is not compressible, so a container that uses more than it
requested is the one Autopilot kills when the node comes under pressure, and
the kill takes the work with it: on Cloud Run because the workspace is a tmpfs
inside that memory, on GKE because the pod and its disk `emptyDir`s go with
it. A memory burst that usually works is one that fails on the busiest day.

CPU is the exception (contract request 108, the runner-sizes design's RS-1,
accepted by the owner 2026-10-20 for #1689). On GKE Autopilot a container's
CPU limit may be up to `CPU_BURST_MAX_RATIO` (2) times its request
(`apps/common/swarm_common/profiles.py::CPU_BURST_MAX_RATIO`), and every class
runs at that ratio: `standard` requests 4 cores and may use 8. The reasons it
is safe for CPU and not for memory:

* a CPU burst on a busy node is throttled, never killed. The worst a burst
  can do is run at its request, which is the old ceiling, so no attempt runs
  slower than before;
* Autopilot bills requests, so the limit costs nothing, and admission
  charges units on the request too: a burst borrows other pods' idle
  requested CPU on the same node and is not counted against any pool;
* the pod's QoS class becomes Burstable, so under a node-wide kernel OOM it
  is chosen before a Guaranteed pod. Memory request == limit keeps it out of
  the first eviction tier; the case left is the one invariant 8's periodic
  checkpoint already bounds.

Cloud Run renders the request alone: a Cloud Run container is given, and
billed, its `limits`, so a limit above the request there would double the
bill rather than allow a burst. The GKE manifest renders the two numbers in
`apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`, and the
cluster's admission policy refuses anything else:
`kubernetes/policies/pod-security.yaml` (`a cpu limit must be between its request and twice it (invariant 7, RS-1)`).
That policy still demanded CPU request == limit when bursting first
released, and refused every GKE job for about 40 minutes on 2026-10-20 until
it was amended; the policy and the renderer are one rule in two places.

The control-plane services follow the same rule on Cloud Run, with one
recorded exception: swarm-api runs with startup CPU boost on, which bursts
past its 1 vCPU limit while an instance starts, by BGD's decision of
2026-10-23 (#1816 option (a)) after slow Python starts outlasted its startup
probe; every other Cloud Run service keeps boost off, swarm-api keeps CPU
always allocated, and the terraform tests (`tests/terraform/cloud_run.tftest.hcl`,
`github_hooks.tftest.hcl`, `mcp_test_server.tftest.hcl`) fail if another
service turns it on.

### Resource classes were measured, not guessed

One working Claude Code lane on the reference workstation, measured 2026-09-15:

| Process | RSS |
|---|---|
| `claude` | 1,532 MB |
| `pytest` | 769 MB |
| node/tsx guards | 207 MB |
| **total** | **~2.5 GiB** |

The classes are about 2x that, because an agent's peak is not its average and
the cost of being wrong is an OOM kill. Their code defaults
(`RESOURCE_CLASSES`), which run whenever no admin has saved a size:

| class | CPU request | CPU limit | memory (request = limit) | Cloud Run workspace tmpfs (of the memory) | units |
|---|---|---|---|---|---|
| `standard` | 4 | 8 | 8 GiB | 4 GiB | 1 |
| `browser` | 4 | 8 | 8 GiB | 4 GiB | 1 |
| `large` | 8 | 16 | 32 GiB | 16 GiB | 4 |

`large`'s memory is the Cloud Run ceiling, and so is every class's CPU
**request** (8); a limit may exceed it because Cloud Run never renders the
limit. Measured on 41 implement attempts on 2026-10-20 in `standard`: mean CPU
1.74 of 4 cores, at the 4-core limit 22% of the time (30% on runs of 40
minutes or more), memory peak at most 6.0 of 8 GiB — which is why CPU got a
burst limit and memory did not move. The worker reports CPU, throttling,
peak RSS and peak disk per attempt so these keep being corrected from
production rather than from arithmetic. See [scaling.md](scaling.md#6-right-sizing).

### Runner sizes are a versioned setting, pinned at admission

The class **names** and the **bounds** are frozen code; the live sizes are not
(contract requests 109-111, [design/runner-sizes.md](design/runner-sizes.md)).
A platform admin changes a class's CPU request, CPU limit and memory in
Admin › Runner sizes, which calls `PUT /v1/admin/runner-sizes` with a reason.
The route validates the whole table with the frozen `check_size` (the request
0.5-8 cores in quarter cores, the limit at most twice the request, memory
2-32 GiB and 1-6.5 GiB per core, the class order, the live-browser split),
derives `units` itself, and in one transaction writes a new immutable
document `runner_size_versions/{version}` and moves the pointer
`control/runner_sizes` to it. Every version is the whole table and is never
updated or deleted, so the collection is the audit trail and a restore is a
new version copying an old one. No pointer is version 0, the code defaults.

The owner's answers bind the page: `disk_gib` is shown, not edited; a memory
cut below the class's 7-day max peak is refused as `below_recent_peak`
unless the admin types the class name as a typed confirmation; two admins
saving from the same base version get a 409, and the second reloads.

The scheduler reads the version once per admission pass
(`apps/scheduler/scheduler/loop.py::Scheduler._size_version`) and, for each
agent, derives one size from it before the lease
(`apps/scheduler/scheduler/loop.py::Scheduler._admit_one`). That one object
is the lease's units, the attempt's record (`size_version` and the `sized_*`
fields) and the pod the GKE renderer draws, so a version saved between the
lease and the dispatch cannot split them, and a running agent keeps the size it
was admitted with: nothing re-renders a running pod or re-charges a lease. A
stored version that fails `check_size` reads as the code defaults, logged at
ERROR. `GET /v1/resource-classes` serves the current version, which is how
the console's pickers and the MCP plugin follow a resize without a deploy.

Only GKE profiles follow: `browser`, `claude-code`, `claude-code-browser`,
`claude-code-large`, `claude-code-lightpanda`, `codex`, `generic`, `indexer` and `merge`. The Cloud Run profiles `mock`,
`post-verdict` and `claude-code-review` always run at the code defaults,
because Cloud Run fixes resources on the Job, not on the execution, and
updating a shared Job in place would race between schedulers; the page lists
them under "does not follow". A GKE profile's Cloud Run fallback Job does not
follow either: Terraform sizes it from the code defaults it mirrors in
`terraform/infra/locals.tf`.

### A known risk, accepted deliberately

**This section described a risk this platform does not carry. Retracted, and
kept because the conclusion — mandatory 120-second checkpointing — still holds
for different reasons.**

What was written here: Cloud Run's ephemeral (second-generation) disk is
**Preview**, per Google's documentation enabling it **disables live migration**,
and since live migration is part of why Cloud Run was chosen for long jobs, the
feature partially undermined the reason for the choice.

What is deployed: that feature is not used and cannot be. The Terraform google
provider cannot express it — `empty_dir.medium` accepts only `"MEMORY"` — so a
workspace is a tmpfs slice of the container's memory (`standard` ~4 GiB of its
8, `browser` ~4 GiB of its 8, `large` ~16 GiB of its 32) and the deployment runs on the fully-GA
path, **with** live migration.

That tmpfs is Cloud Run only. On GKE, where every agent profile but `mock`
now runs, the workspace, `/tmp` and HOME are disk `emptyDir`s and the tool
caches a 20 GiB PVC (§3, Dispatch); live migration does not apply there, and
the node-side ways to lose an attempt do.

Checkpointing is not relaxed on that news. Live migration covers infrastructure
moves. It does nothing about a quota park-and-exit, a cancellation, a reconciler
reclaim of a stale generation or an ordinary crash, and those are what actually
end attempts here — checkpointing is what makes each of them cost minutes rather
than the whole attempt. Read [checkpointing.md](checkpointing.md) and
[cost-control.md](cost-control.md#4-the-preview-disk-tension) before changing
either the workspace configuration or the checkpoint interval.

### Multi-tenant from V1

A tenant is a Google group, with a personal fallback tenant so nobody is ever
hard-blocked. Each tenant gets its own service account, its own secrets, its own
GCS prefix and its own Kubernetes namespace, and brings its own provider keys.
Retrofitting tenancy means retrofitting every IAM binding and every storage path
at once; doing it first costs a fraction of that.

### Auth is Google ID tokens, with no shared platform token

A shared bearer token carries no identity. Without identity there is no tenant to
attribute an agent to, no way to scope a list response, and no boundary to
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
| 7. Memory request == limit; CPU limit <= 2x request on GKE | `profiles.check_size`, `scheduler/sizes.py`, `scheduler/dispatch.py`, `kubernetes/policies/pod-security.yaml`, Terraform job specs (request only) |
| 8. Mandatory checkpointing | `agent_worker/checkpoint.py` |
| 9. Per-tenant isolation | `swarm_api/credentials.py`, `agent_worker/secrets.py`, Terraform `tenancy` module |
| 10. Callers pick profiles by name | `swarm_api/validation.py` |

`apps/common/swarm_common/` is frozen (see `CONTRACT.md`). Import from it; never
restate its types. Terraform restates the catalogue in `terraform/infra/locals.tf`
only because Terraform cannot import Python, and a test asserts the restatement
still matches.
