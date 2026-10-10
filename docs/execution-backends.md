# Execution backends

> **Workspace sizes are memory, not disk.** These classes originally specified
> 20/40/100 GiB of disk-backed ephemeral storage. That is a Cloud Run Preview
> feature the Terraform google provider cannot express — `empty_dir.medium`
> accepts only `"MEMORY"` — so the workspace is a tmpfs carved out of the
> container's memory: **standard ~4 GiB, browser ~8 GiB, large ~16 GiB**, each a
> slice of that class's total memory rather than capacity on top of it.
> The upside is that this path is fully GA and supports live migration, which the
> Preview disk explicitly does not, so the no-interruption requirement that drove
> the Cloud Run choice is better served here than by the feature we set out to use.

Two backends. Cloud Run Jobs runs almost everything; GKE Autopilot exists for
the three things Cloud Run cannot do, and, since contract request 53
(2026-10-08), for `claude-code`, the one thing Cloud Run did too slowly: start
it (§4). Since contract request 63 (2026-10-10) `indexer` runs there too, the
canary for #939: a new Cloud Run instance's internet path opens too slowly.

---

## 1. Why Cloud Run Jobs is primary

The original design called for GKE Autopilot as the main execution surface. It
was changed, deliberately, because the operator's requirement is absolute: **no
preemption, no OOM kills, no restarts.**

GKE — Autopilot included — ends running pods for reasons that have nothing to do
with the pod:

| Mechanism | What it does to a two-hour agent |
|---|---|
| Node autoscaler consolidation | evicts to pack workloads onto fewer nodes |
| Node auto-upgrade | drains the node on the release channel's schedule |
| Node auto-repair | replaces an unhealthy node, taking its pods |
| Memory/disk pressure eviction | kills the largest offender on the node |
| Spot reclamation | 30 seconds' notice, any time |

Autopilot's **extended run time** annotation suppresses the first two for up to
seven days, and this platform sets it. But suppression is a promise about
Google's scheduler, not the absence of nodes.

Cloud Run Jobs has **no nodes to upgrade, no autoscaler to consolidate onto, and
no node pool to repair**. An execution runs to completion or fails; there is no
third party with a reason to move it. That is the whole argument.

What Cloud Run gives up:

* a hard ceiling of 8 vCPU and 32 GiB per execution;
* no control over `/dev/shm` size;
* no GPUs for this workload shape;
* ephemeral disk is **Preview** and disables live migration — see
  [checkpointing.md](checkpointing.md).

Those four limits are exactly the GKE Autopilot exception list.

---

## 2. Routing

`swarm_common.profiles.resolve_backend()` is the only router. Callers never pick
a backend (invariant 10) — they name a runner profile, and the profile names the
backend.

| Profile | Image | Class | Backend | Provider |
|---|---|---|---|---|
| `mock` | `agent-runtime-base` | standard | Cloud Run Job | none |
| `generic` | `agent-runtime-base` | standard | Cloud Run Job | none |
| `claude-code` | `agent-runtime-base` | standard | **GKE Autopilot** (contract request 53; its Cloud Run Jobs kept, idle, until 2026-10-15 as the rollback) | anthropic |
| `codex` | `agent-runtime-base` | standard | Cloud Run Job | openai |
| `browser` | `agent-runtime-browser` | browser | **GKE Autopilot** | anthropic |
| `indexer` | `agent-runtime-indexer` | standard | **GKE Autopilot** (contract request 63, the canary for #939; its Cloud Run Jobs kept, idle, as the rollback) | anthropic |

`Backend.AUTO` resolves to Cloud Run when the class fits within 8 vCPU / 32 GiB,
and to Autopilot otherwise.

`mock` has `provider=None` on purpose: the smoke path must work before any
tenant has registered an API key, so that the first thing you learn after a
deploy is not nothing.

---

## 3. Cloud Run Jobs

### One Job resource per (tenant, profile)

This is forced by a Cloud Run property, not chosen for tidiness:

> Cloud Run sets the service account on the **Job** resource. It cannot be
> overridden per execution.

A single shared Job would run every tenant's work under one identity, and
invariant 9 — a tenant's provider key must never be reachable from another
tenant's pod — would be unenforceable. So each `(tenant, profile)` pair gets its
own Job resource bound to that tenant's service account:

```
swarm-job-eng-claude-code        -> sa: swarm-agent-worker-eng@<project>.iam
swarm-job-eng-codex              -> sa: swarm-agent-worker-eng@<project>.iam
swarm-job-research-claude-code   -> sa: swarm-agent-worker-research@<project>.iam
```

Terraform materialises a Job only for combinations that can actually run: a
profile needing a provider is skipped for tenants without a key for it. Creating
the rest would produce Jobs whose every execution dies on a missing secret, when
the correct behaviour is for the task to park as `CREDENTIAL_MISSING` and cost
nothing.

A tenant with no key that a pool account serves is the exception: admission lets
it through (`apps/scheduler/scheduler/credentials.py`), terraform has made no Job
for it, and the dispatcher creates one on the first dispatch. That Job names
**no** tenant secret. Cloud Run resolves a `secretKeyRef` when the Job is
created, so naming a secret that does not exist would stop the Job being created
at all. The worker takes the account's token from the broker instead. The Job's
secret mount is decided by the same function admission asks, on the same account
list, so the two cannot disagree about whether the pool is the credential.

The reconciler garbage-collects Job resources that stop being used.

### What lives where

The Job resource carries only what is true for every execution of that
(tenant, profile): the image, the resource class, the timeout, the tenant's
service account, and the secret bindings for that tenant's keys.

The **execution** carries the identity of the attempt — `TASK_ID`,
`ATTEMPT_ID`, `LEASE_ID`, `GENERATION` — as environment overrides. The worker
reads its image and command from the frozen catalogue **by name** and refuses to
take them from the environment at all, so overriding an execution's environment
can never change what code runs.

### Sizing

`requests == limits`. Cloud Run expresses this as limits only; the number set is
the class's number, with no headroom to burst into. Bursting past a request is
what gets a container OOM-killed under pressure, so the platform never does it.

```
standard   4 vCPU   8 GiB   ~4 GiB workspace (tmpfs)
browser    8 vCPU  16 GiB   ~8 GiB workspace (tmpfs)   (GKE only)
large      8 vCPU  32 GiB  ~16 GiB workspace (tmpfs)   (Cloud Run ceiling)
```

`max_retries = 0` on every Job. Retries are a control-plane decision, made with
a new attempt, a new generation and a checkpoint restore — not a silent
re-execution that the lease accounting knows nothing about.

---

## 4. GKE Autopilot

Reserved for:

1. **browser work** — Chromium needs a large `/dev/shm`, which Cloud Run will not
   size and a GKE pod spec sets directly (2 GiB tmpfs, a `medium: Memory`
   `emptyDir` at `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`; this said 1 GiB
   until 2026-10-02);
2. **GPU work**;
3. **anything above 32 GiB**;
4. **`claude-code`, for its start latency** (contract request 53, applied
   2026-10-08). On Cloud Run its DISPATCHED -> STARTING wait was p50 128 s /
   p90 212 s over 874 attempts, 90-95 % of it in Cloud Run's own provisioning
   and all of it lease-held; contract request 55's canary ran it on Autopilot
   for five real steps at DISPATCHED -> RUNNING p50 ~23 s, max 44 s. That is
   the exception to §1, not a reversal of it: claude-code now carries the
   node-side ways to lose an attempt in the table above that extended run time
   does not suppress (auto-repair, pressure eviction), and mandatory periodic
   checkpointing is what makes them cost minutes rather than the run, not what
   makes them free. Its workspace is a disk `emptyDir` here, not tmpfs, so a
   run is bounded by the pod's ephemeral-storage limit instead of its memory.
   Rolling back while the fallback Jobs exist (until 2026-10-15,
   `cloud_run_fallback_profiles` in `terraform/infra/locals.tf`) is one line in
   `profiles.py`, the backend back to `CLOUD_RUN_JOB`, and a release.
5. **`indexer`, for its internet path** (contract request 63, owner
   2026-10-10, the canary for #939). A new Cloud Run instance's path to the
   internet opens a median 20.2 s after start (n=19) against a GKE pod's
   1.17 s (n=148), through the same Cloud NAT, and every index run starts by
   cloning. It moves alone, first; the other Cloud Run profiles that reach
   the internet (`merge`, `generic` with a repository) follow only after it is
   measured with `scripts/egress-ready-report.sh`. It carries the same
   node-side costs as `claude-code` above, and rolls back the same way: one
   line in `profiles.py` while `cloud_run_fallback_profiles` keeps its Jobs.

Every pod carries:

* `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`, on the Job AND on
  its pod template (`apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`) — the cluster
  autoscaler reads it from pods, not Jobs (the comment above the annotation says
  why both carry it). This annotation IS Autopilot's extended-run-time
  request: it suppresses eviction for scale-down/consolidation and for node
  auto-upgrade for up to seven days, and only on on-demand capacity
  (`apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`), which is why Spot is disabled
  platform-wide. Until 2026-10-02 this line gave the key as
  `cloud.google.com/gke-extended-run-time`, which is not a real annotation and
  which nothing in this repository sets;
* `requests == limits` on cpu, memory and ephemeral storage. Ephemeral storage
  is the WHOLE pod's local disk, because kubelet adds the workspace, `/tmp` and
  HOME emptyDirs together and evicts the pod (no checkpoint, no park) when
  their total passes it. On Cloud Run only the workspace is a capped tmpfs, and
  `/tmp` (where the worker builds checkpoint archives of up to 2 GiB) and HOME
  are outside that cap. So since contract request 53 a `standard` pod on GKE
  gets 10 GiB: 4 for the workspace, the same as on Cloud Run, plus 2 for `/tmp`
  and 4 for HOME. 10 GiB is also the most Autopilot accepts for a
  general-purpose pod. `browser` gets 8 GiB, its `disk_gib`, split 5 workspace +
  1 `/tmp` + 2 HOME (owner, 2026-10-07): its volumes were 8 + 2 + 4 = 14 GiB
  under that 8 GiB limit, so the pod could be evicted before any one volume was
  full. The per-class layout is `GkeDisk` in `scheduler/dispatch.py`, and a unit
  test holds every class to sum(sizeLimits) <= ephemeral-storage <= 10 GiB;
* `restartPolicy: Never` and `backoffLimit: 0` — the control plane owns retries;
* the tenant's Kubernetes service account, workload-identity-bound to the
  tenant's Google service account;
* `runAsNonRoot` (uid 10001), `allowPrivilegeEscalation: false`, all capabilities
  dropped, `seccompProfile: RuntimeDefault`, read-only root filesystem with
  writable `emptyDir` mounts for the workspace;
* a per-tenant namespace with default-deny network policy.

**Spot is disabled everywhere**, and the frozen catalogue raises at import time
if a profile tries to enable it. The reason is specific and verified: Spot Pods
**cannot use Autopilot extended run time**. Choosing Spot means choosing
preemption, which contradicts the platform's core requirement. There is no
"Spot for cheap tasks" compromise available, because the pods that would use it
are the long ones.

Chromium's own sandbox stays off: it needs user namespaces and `CAP_SYS_ADMIN`,
which the pod security posture refuses. The isolation boundary is the pod, not
Chromium's inner sandbox, and handing back a capability to gain the latter would
be a bad trade.

---

## 5. Dispatch and failure

Dispatch is strictly downstream of the lease:

```
lease acquired (capacity already reserved)
  -> resolve profile -> backend
  -> Cloud Run:  run.jobs.run with env overrides for this attempt
     GKE:        create a Job in the tenant's namespace
  -> record attempts/{id}.execution_name
  -> task LEASED -> DISPATCHED
```

Any failure in that sequence releases the lease and returns the task to `READY`.
A slot held by a container that will never exist is a permanent capacity leak
until the reconciler notices; releasing immediately is free.

The reconciler covers what dispatch cannot:

| Disagreement | Cost if ignored |
|---|---|
| stale lease | capacity leaks; the platform slowly stops admitting |
| missing execution | the task is stuck forever holding a slot |
| orphan execution | money, plus a second agent on someone's repository |
| orphan lease | capacity leaks |

Note what is **not** a finding: a task in `LEASED` with a fresh lease and no
execution yet. Dispatch takes time and image pulls take minutes; a reconciler
that treated "not started yet" as "dead" would kill every cold start.

---

## 6. Adding a backend

Not a configuration change — a code change, in this order:

1. add the value to `Backend` in the frozen catalogue (this requires a contract
   change, so raise it rather than editing);
2. implement dispatch in `scheduler/dispatch.py`;
3. implement `terminate` and `list_executions` in `reconciler/backends.py` — a
   backend the reconciler cannot terminate cannot be safely released, so repair
   would stall on it;
4. add the `backend:<NAME>` pool to `terraform/infra/locals.tf`;
5. extend `concurrency-test.sh` and `race-test.sh` to cover it.

Step 3 is the one people skip. Without a confirmed termination, `repair.py` will
not release the slot — correctly — and the backend will accumulate stuck leases.
