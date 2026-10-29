# Runner sizes: CPU that bursts, memory that does not, and sizes an admin sets

**Status: built. Every lane of §6 is on main: phase 1 `SIZES-CONTRACT-1`,
`SIZES-SCHED-1` and `SIZES-WORKER-1` (canary passed 2026-10-20 ~20:25Z,
recorded on #1689: four claude-code attempts read `cpu_limit_cores: 8` from
the cgroup, and `efficiency-daemon` ran 16 of 16), then phase 2
`SIZES-CONTRACT-2`, `SIZES-API`, `SIZES-SCHED-2`, `SIZES-UI`, `SIZES-DETAIL`,
`SIZES-MCP` and `SIZES-DOCS`. RS-1 to RS-4 were filed as contract requests
108 to 111. This document was designed 2026-10-20 (lane RUNNER-SIZES-DESIGN,
part of #1689) and answered by the owner the same day: every §7 question took
its recommendation, and contract requests RS-1 to RS-4 were accepted (§5, §7
"The owner's answers").** What runs is described in
[architecture.md](../architecture.md#runner-sizes-are-a-versioned-setting-pinned-at-admission)
and [kubernetes/README.md](../../kubernetes/README.md); the sections below
are the design as written, read at `ed7abe6`. The two halves of the issue
that are not this document were built beside it: close-to-real-time CPU and
memory use with throttling (lanes LIVE-UTIL-API and LIVE-UTIL-UI), and the
live browser's 2-core agent share (lane BROWSER-SPLIT).

Owner decisions of 2026-10-20 this design takes as settled, not as
questions:

* admin-editable runner sizes are wanted;
* CPU may burst: a CPU limit may be higher than its request;
* **memory stays fixed, request == limit**;
* live utilisation with throttling is being built now (LIVE-UTIL-*);
* a live-browser agent gets 2 cores instead of 1 (BROWSER-SPLIT).

Every file, symbol and line cited below was read on 2026-10-20 at `ed7abe6`.
Lines move, so search for the symbol named beside each one.

What the design settles, in one line each:

* **CPU limit = 2 × CPU request on GKE Autopilot, memory request == limit,
  cost unchanged.** Autopilot bills requests, so a limit above the request
  costs nothing, and a burst on a busy node is throttled, never killed (§2.2).
* **A burst borrows other pods' idle requests on the same node, not spare
  hardware.** Google's own definition, which bounds the gain: a pod alone on
  its node bursts into almost nothing (§2.2.2).
* **Sizes become one versioned platform setting** beside Pool limits, with an
  audit trail. Class names, invariants and validation bounds stay frozen in
  code (§4).
* **The size is pinned at admission.** The scheduler reads the current
  version, charges units from it, records it on the attempt, and renders the
  pod from that record. A running agent keeps the size it was admitted with
  (§4.4).
* **Cloud Run-backed profiles do not follow the setting and cannot burst**
  (§3, §4.7).

---

## 1. The problem, as measured

The owner asked on 2026-10-20 whether claude-code runners are starved of CPU
("I see the CPU is showing at 4 cores and peak is 4 cores"), how runner
resources are configured, and what it would take to set CPU and memory from
the admin UI (issue #1689).

Measured on 41 implement attempts of 2026-10-20, on GKE Autopilot, in the
4-core `standard` class (issue #1689, "How would you know it worked?"):

| measure | value |
|---|---|
| median of each attempt's mean CPU | **1.74 of 4 cores** |
| share of time idle (< 0.5 core) | 15% |
| share of time at the 4-core limit | **22%** |
| the same, long runs (≥ 40 min) | **30%** |
| the same, short runs | 7% |
| median peak memory | 3.1 GiB |
| max peak memory | 6.0 of 8 GiB |

Read together: the typical attempt uses less than half its CPU on average,
but spends a fifth of its life — nearly a third, on the long runs that
matter most — pinned at the ceiling. That is the shape of a workload with
idle stretches (waiting on the model) and CPU-bound bursts (a test suite, a
build, a type check). A fixed 4-core ceiling makes every burst wait, and the
idle stretches give the cores back to nobody. Memory is the opposite: the
worst attempt used 6.0 of 8 GiB, so there is headroom but not much, and no
reason to move it.

**What "done" lets someone do that they cannot today:**

1. An agent in a CPU-bound phase runs on up to twice its cores when its node
   has idle requested CPU, at no extra cost, and the console shows how much
   of its time it was throttled.
2. A platform admin changes a class's CPU request, CPU limit or memory in the
   console — today it is a frozen-contract change, a pull request and a
   release — sees the units and the cost it implies before saving, and can
   see who changed what, when and why, and restore an earlier version.
3. The next admission follows the change; a running agent is untouched.

---

## 1a. Today

### 1a.1 The classes

`apps/common/swarm_common/profiles.py::RESOURCE_CLASSES` (FROZEN):

| class | cpu | memory_gib | disk_gib (tmpfs, a slice of memory) | units |
|---|---|---|---|---|
| `standard` | 4 | 8 | 4 | 1 |
| `browser` | 4 | 8 | 4 | 1 |
| `large` | 8 | 32 | 16 | 4 |

`ResourceClass`'s docstring: "`cpu` and `memory_gib` are BOTH request and
limit." `ResourceClass.__post_init__` refuses a non-positive size, and
refuses `cpu > 8 or memory_gib > 32` as "exceeds the Cloud Run Jobs
ceiling": every class must fit a Cloud Run Job, whichever backend runs it.
The module docstring records the rule this design amends: "requests ==
limits. Bursting past a request is precisely what gets a container
OOM-killed under node pressure, so we never do it." CONTRACT.md invariant 7
says the same in one line.

`units` are what admission charges against the weighted pools
(`apps/scheduler/scheduler/loop.py`, `units = RESOURCE_CLASSES[task.resource_class].units`,
passed to the frozen `acquire_lease_in_transaction`, which writes
`lease.units` and adds them to every required pool's `active`). The unit
weights are restated once in the UI (`RESOURCE_UNITS`, `apps/swarm-ui/src/types.ts`),
held to the Python by `scripts/lib/check-contract-parity.sh` §1, and
mirrored for Terraform's Cloud Run Jobs in `terraform/infra/locals.tf`
(`standard = { cpu = 4, memory_gib = 8, disk_gib = 4, units = 1 }` and the
other two).

`resolve_backend` sends an `AUTO` profile to Cloud Run when its class fits
8 / 32 — which, given the ceiling check, is every class.

### 1a.2 How the scheduler renders them

`apps/scheduler/scheduler/dispatch.py`:

* **Sizing comes from `task.resource_class`, not the profile's** (module
  docstring, "SIZING COMES FROM"). A workflow step may name a smaller class
  (`swarm_api.validation.validate_resource_class_override` refuses a larger
  one in any of cpu, memory, disk or units), and admission charged the task's
  class, so the container must be that class.
* **GKE** (`GkeJobDispatcher._manifest`): one `resources` dict, `cpu`,
  `memory`, `ephemeral-storage`, set as both `requests` and `limits`
  ("requests == limits, both directions, no bursting"). No compute class,
  no node selector, `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`
  for Autopilot extended run time.
* **The live split** (`LIVE_DISPLAY_SHARE`, `with_live_display`): a
  `live_browser` profile's pod, while `settings.live_browser_sidecar` is on,
  is SPLIT between the worker and the `display` sidecar, each container with
  requests == limits, summing to the class so admission's units and the bill
  are unchanged. `browser` is display 3 / 5 GiB, worker 1 / 3 GiB today;
  BROWSER-SPLIT moves it to display 2, worker 2 (memory unchanged).
  `standard` is 2 / 4 each, `large` 5 / 10 to the display.
* **Cloud Run** (`CloudRunJobDispatcher._build_job`): `limits` only, `cpu`
  and `memory`; "the platform never sets a request below the limit, so
  requests == limits holds." Cloud Run pins resources on the **Job**
  resource; a `RunJobRequest`'s overrides carry arguments, environment, Cloud Run task
  count and timeout, never resources. That is why `job_id_for` gives a step
  that named a smaller class a Job of its own
  (`swarm-job-<tenant>-<profile>-<class>`).

### 1a.3 Which profiles run where

From `RUNNER_PROFILES` (`backend=`):

| backend | profiles (class) |
|---|---|
| GKE Autopilot | `claude-code` (standard), `claude-code-large` (large, 14,400 s; contract request 113), `codex` (standard), `generic` (standard), `merge` (standard), `indexer` (standard), `browser` (browser), `claude-code-browser` (browser, `live_browser`) |
| Cloud Run Jobs | `mock` (standard), `post-verdict` (standard, its own Job's account), `claude-code-review` (standard, its own account), `await-input` (standard, `control_plane`: never reserved, never run) |

`terraform/infra/locals.tf` `cloud_run_fallback_profiles` keeps Cloud Run
Jobs for the GKE profiles as a rollback; they are sized from the locals
mirror.

### 1a.4 What the worker measures

`apps/agent-worker/agent_worker/metrics.py` reads cgroup v2: `cpu.stat`
`usage_usec` (`cgroup_cpu_seconds`), `cpu.max` (`cgroup_cpu_limit_cores`,
the limit the kernel enforces), `memory.peak`, `memory.events`. The attempt
records `cpu_seconds`, `peak_cpu_cores`, `mean_cpu_cores`, `cpu_limit_cores`
and `cpu_limit_source` (`cgroup` or `resource_class`; frozen `Attempt`,
contract request 26). The worker's memory ceiling for `oom_near_miss`
comes from the catalogue (`agent_worker/config.py::memory_limit_bytes_of`).
Throttling (`cpu.stat` `nr_throttled`, `throttled_usec`) is LIVE-UTIL's.

---

## 2. Constraints that bind the design

### 2.1 The invariants it touches

**1 — only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Preserved.
Nothing here creates a pod or a Job for an agent that is not leased. A size
change writes two Firestore documents and nothing else; it does not touch a
queued, parked or ready agent, which costs nothing before and after. A size
version is read at admission, the moment demand begins.

**2 — all-or-nothing reservation in one transaction.** Preserved, untouched.
The frozen `acquire_lease_in_transaction` still takes `units` as a
parameter and reserves every required pool in one transaction. What changes
is where the scheduler gets `units` from: the size version it read for this
admission instead of a code constant. The size is read **before** the
transaction and pinned after it (§4.4); a change landing between the two
cannot split a reservation, because the units the transaction charged and the
size the pod is rendered at are the same object.

**3 — concurrency counts from LEASED.** Untouched: units are charged at
LEASED as today.

**5 — fencing.** Preserved. The size is recorded on the attempt (generation
N); a stale worker exits before it runs anything, whatever it was sized at.
A retry is a new attempt, a new admission, and takes the size current then.

**6 — no Spot.** Untouched. Bursting needs no Spot and no compute class
(§2.2.1).

**7 — requests == limits. AMENDED FOR CPU ONLY** (contract change request
RS-1, §5). Memory and ephemeral storage keep request == limit on every
container. CPU limit may exceed CPU request on GKE Autopilot, bounded by a
frozen ratio; Cloud Run keeps a single number. §2.3 says why memory does not
move; §2.4 what follows for units and fairness.

**8 — mandatory periodic checkpointing.** Untouched and still needed: a CPU
burst cannot cause an interruption (it is throttled, §2.2.3), but the pod's
QoS class becomes Burstable, which slightly raises its kernel OOM score
(§2.3). Checkpointing is what bounds the cost of any kill, as it does today.

**9 — per-tenant isolation.** Preserved. Identity, secrets, GCS prefix and
namespace are untouched. Pods of different tenants already share Autopilot
nodes; what bursting adds is that one tenant's pod may use CPU another
tenant's pod requested and left idle. It can never take CPU that pod is
using: the kernel's CFS weights are proportional to requests, and under
contention every container gets its request (§2.2.3). That is a performance
coupling above the request, never below it, and never a data path. §2.4
records it as a fairness property, and §7 Q8 lets the owner refuse it.

**10 — callers pick a runner_profile by name.** Preserved, and it is the
reason sizes are an **admin** setting and never a request field. A caller
still names a profile and, in a workflow step, at most a smaller **class
name**; no route accepts a CPU, a memory figure or a limit from a caller.
`FORBIDDEN_CALLER_FIELDS` is untouched. The admin route is behind
`admin_auth`, and its values are bounded by frozen code (§4.2).

### 2.2 CPU bursting on this cluster

#### 2.2.1 Does this cluster support it

**The live read was refused.** On 2026-10-20 this lane ran, read-only:

    gcloud container clusters describe swarm-autopilot --region us-central1 \
      --project saga-agents-staging --format="yaml(currentMasterVersion,...)"
    -> 403: Required "container.clusters.get" permission(s) ...
       authenticated as swarm-agent-worker-eng@...

and this container has no kubeconfig context at all (`kubectl config
get-contexts` lists none), so the pinned kubectl (`kubectl_bin`, v1.36.5
here) had nothing to read through. That is a real block: the agent's
identity is the `eng` worker account, which correctly cannot read the
cluster. The fix round of the same day retried both reads with the same
result (403 on the describe; `kubectl_bin` resolved, `get-contexts` empty).
**The cluster's version and bursting state are therefore taken from
the repository's last dated reading, not measured today**, and stay
unverified until the SIZES-SCHED-1 canary (§6) reads them:

* `docs/DEPLOY_STATE.md` (measured 2026-09-24, `gcloud container clusters
  describe swarm-autopilot`): Autopilot, control plane `1.36.4-gke.1082000`,
  **created 2026-09-17**. The dev release channel is `RAPID`
  (`terraform/environments/dev/dev.tfvars` `gke_release_channel`), so the
  version today is that or later.
* Compute class: none. `GkeJobDispatcher._manifest` sets no
  `cloud.google.com/compute-class` selector and no node selector
  (`docs/browser-startup-speed.md` §4.1 records the same), so pods land on
  Autopilot's default general-purpose class, on the container-optimized
  compute platform that clusters on 1.32+ get.

What Google's documentation says
([GKE: configure Pod bursting](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-bursting-gke),
read 2026-10-20):

* Autopilot supports bursting from **`1.30.2-gke.1394000`**.
* A cluster **created before** that version needs cgroup v2 and a manually
  started control-plane upgrade before bursting works. This cluster was
  created on 1.36.x, after it, so **no cluster setting and no upgrade is
  needed** — by the record. The proof is one read, which a build lane's
  operator runs through `scripts/configure-kubectl.sh` and `kc`:

      kc get daemonset --namespace=kube-system efficiency-daemon

  Google: "If the output shows the daemon, bursting is enabled."
* Bursting applies to pods that request no compute class, or Balanced or
  Scale-Out. Ours request none.
* **On a cluster that does not support bursting, Autopilot sets limits equal
  to requests.** So a manifest with limit 8 on a non-bursting cluster would
  silently run at 4. The worker would catch it — `cgroup_cpu_limit_cores`
  reads `cpu.max`, the limit the kernel enforces, and records it as
  `cpu_limit_cores` with `cpu_limit_source: cgroup` — and the first canary
  attempt's record is the end-to-end proof (§6, lane SIZES-SCHED's canary).

Autopilot's request rules
([GKE: resource requests in Autopilot](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-resource-requests),
read 2026-10-20), general-purpose class: CPU request 50m minimum on a
bursting cluster, 30 vCPU maximum; memory 52 MiB minimum, 110 GiB maximum;
**vCPU:GiB between 1:1 and 1:6.5, and outside it Autopilot raises the smaller
resource**; Pods with the extended-run-time annotation (ours) need at least
**0.5 vCPU and 0.5 GiB**.

#### 2.2.2 Where a burst's CPU comes from

Google defines Autopilot's burstable capacity as "the sum of resource
requests of all Pods on that node, regardless of the actual resource capacity
of the node." A burst borrows **other pods' requested-and-idle CPU on the
same node**. It does not borrow the VM's unrequested cores, and a pod alone
on its node bursts into almost nothing.

That matters for what to expect. Our agents are good neighbours for each
other: each is idle 15% of the time and averages 1.74 of 4 cores, so
co-located agents leave, on average, more than half of their requested CPU
unused, and their CPU-bound phases rarely coincide. The gain is real when
pods share nodes (several agents running at once, which is the normal state
of a wave) and small when one agent runs alone.

#### 2.2.3 A burst on a busy node is throttled, never killed

Google: "If the CPU usage exceeds the burstable capacity, GKE throttles the
CPU usage of some containers so that all containers on the node get the CPU
that they request." CPU is compressible: past the request a container gets
less time, not a kill. The worst case of a burst is the CPU it would have had
without bursting — its request — which is today's ceiling. So with the
request held at today's number, **no attempt can run slower than today** from
bursting; it can only run faster when a neighbour is idle.

#### 2.2.4 Billing

Autopilot bills the sum of Pod resource **requests** (Google, same page:
"you pay for the sum of your Pod resource requests"). A limit above the
request is not billed. At the list price `docs/browser-startup-speed.md`
records (us-central1 general-purpose, $0.0445 per vCPU-hour and $0.0049225
per GiB-hour, confirmed 2026-10-13 only through secondary sources; that doc
says so), a `standard` pod costs 4 × 0.0445 + 8 × 0.0049225 ≈ **$0.217 an
hour** before and after the recommended change.

### 2.3 Why memory stays request == limit

* **Memory is not compressible.** Google: "If the memory usage exceeds the
  burstable capacity, GKE terminates containers to reclaim memory on the
  node." A memory burst does not slow down; it ends the attempt.
* **The workspace is memory.** `disk_gib` is a tmpfs carved from the
  container's memory (`profiles.py`, "WORKSPACE SIZES ARE MEMORY, NOT
  DISK"), so a memory overshoot is often a file written into `work/`, and
  the kill would take the work with it.
* **It keeps the pod out of the first eviction tier.** A pod whose CPU limit
  exceeds its request is no longer Guaranteed QoS; it is Burstable. The
  kubelet ranks pods for node-pressure eviction first by whether their
  memory **usage exceeds their request**
  ([Kubernetes: node-pressure eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)).
  With memory limit == request, usage can never exceed the request, so our
  pods stay in the tier evicted last, with Guaranteed pods. What does change
  is the kernel's `oom_score_adj`: Guaranteed is -997, Burstable is
  `1000 − 1000 × memoryRequest / nodeMemory` (clamped to 2..999), so under a
  node-wide kernel OOM a Burstable pod is chosen before a Guaranteed one. On
  Autopilot, where the node is sized to the requests, that is a narrow case,
  and it is the case invariant 8's checkpoint already covers. The amended
  invariant names it rather than hiding it.
* **The measurement gives no reason to.** Max peak 6.0 of 8 GiB: there is no
  attempt that would have been saved by a memory burst, and the one at 6.0 is
  close enough to 8 that shrinking the request is not on the table either.

### 2.4 Units, invariant 7 and fairness

* **Units are charged on requests**, because requests are what is billed and
  what Autopilot schedules on. A burst is free capacity borrowed from a
  neighbour, not capacity the platform reserved, so it is not charged and it
  does not count against any pool. Admission's arithmetic is unchanged.
* **Units become derived from the size**, so a resize cannot silently leave
  the budget charging the wrong weight. The frozen rule (RS-2) is
  `units = max(ceil(cpu_request / 4), ceil(memory_gib / 8))`, which
  reproduces today's 1 / 1 / 4 exactly (`large`: max(2, 4) = 4). A pool's
  `hard_limit` is in units, so the admin page shows what a resize does to it
  (§4.6).
* **Fairness between tenants is kept at the request, and not above it.**
  Every container is guaranteed its request; the budget, which is how
  tenants share the platform, is charged in requests. Above the request,
  CPU is shared by the kernel in proportion to requests between whichever
  pods happen to share a node, so a tenant with more bursty pods running
  gets more unbilled CPU than one with fewer. That is the property the owner
  chose ("agents may burst when they need it"); §7 Q8 B is the way to
  refuse it between tenants. (Q1 C is a different knob: it turns bursting
  off for a whole class, for every tenant alike.)
* **Invariant 7 as amended** (RS-1): memory and ephemeral storage request ==
  limit everywhere; CPU request ≤ limit ≤ `CPU_BURST_MAX_RATIO` × request on
  GKE Autopilot; Cloud Run renders the request as its only number.

### 2.5 Cloud Run-backed profiles cannot burst

Cloud Run Jobs have no request/limit pair to separate: the container is
given, and billed for, its `limits`. Rendering the CPU limit there would
double the bill, not allow a burst. So a Cloud Run container is rendered at
the CPU **request**, exactly as today. And because Cloud Run fixes resources
on the Job and not per execution (§1a.2), Cloud Run profiles do not follow
the admin setting either (§4.7).

### 2.6 What it needs in `apps/common/swarm_common/`

Four requests, written out in §5: RS-1 (invariant 7 and `ResourceClass`
gain a CPU limit), RS-2 (the frozen bounds, the derived units and the pure
validator), RS-3 (the size-version shape the scheduler and swarm-api both
read), RS-4 (`Attempt` records the size it was rendered at).

---

## 3. Designs that could work

### Design A — bursting only, sizes stay in code

Add `cpu_limit` to the frozen classes, render it on GKE, done. Every resize
is a contract request and a release, as today.

* **Costs.** Smallest: RS-1 and a scheduler change. No migration, no
  latency, no money.
* **Breaks.** Nothing — but it delivers one of the owner's two asks. The
  admin page the owner asked for does not exist.

### Design B — sizes in Terraform (tfvars → scheduler environment)

Sizes move to `terraform/environments/<env>/<env>.tfvars`, rendered into the
scheduler's and swarm-api's environment, as `pool_limits` seeds pools.

* **Costs.** A Terraform variable, a scheduler setting, a deploy per change
  (minutes, through CI). No Firestore shape.
* **Breaks.** It is not "in the console". Every change needs a PR and a
  release; there is no live preview against the pools; the audit trail is
  git history. And it repeats the mistake `AdminSettings.tsx`'s header
  records for pool limits ("WHY THIS SCREEN EXISTS RATHER THAN A TFVARS
  EDIT"): a tfvars value becomes the birth value of an environment and drifts
  from what is running.

### Design C — a versioned platform setting, pinned at admission (recommended)

Code keeps the class **names**, today's sizes as **defaults**, and the
**bounds** (frozen). The live sizes are a versioned Firestore setting
written by `PUT /v1/admin/runner-sizes`. The scheduler reads the current
version per drain pass, derives units from it, and pins the size on the
attempt; the GKE renderer reads the pinned size. Cloud Run-backed profiles
keep the code default.

* **Costs.** Four contract requests; one new swarm-api module and routes; a
  scheduler read of one document per drain pass (one Firestore `get`, cached
  for the pass — no measurable admission latency against a transaction that
  already reads every pool); one new console tab. Migration: none — with no
  version document the platform runs at the code defaults, which are today's
  sizes plus the recommended CPU limits. Money: zero at the recommended
  numbers; any change an admin makes is previewed in dollars before saving.
* **Breaks.** Anything that reads `RESOURCE_CLASSES[...].cpu` as "the size
  this attempt ran at" becomes wrong after a resize and must read the pinned
  attempt size instead. §4.8 lists every such reader (scheduler, worker,
  swarm-api's waiting and validation, MCP, UI). The parity check that holds
  `RESOURCE_UNITS` to the code becomes a check of the code **defaults**
  only, and the UI's copy is retired in favour of the served route (it is
  already documented as a bundled fallback).

### Design C′ — as C, but Cloud Run profiles follow by a Job per size version

The Cloud Run dispatcher gives each (tenant, profile, class, size version) a
Job of its own, so a Cloud Run profile could follow a resize.

* **Costs.** A Job per version per tenant per profile; the reconciler's GC
  must learn size versions; Terraform's Jobs stop being the ones that run.
* **Breaks.** Updating one Job in place is not safe instead: two schedulers
  holding different versions can update and run in either order, so an
  execution could run at a size other than the one its lease charged. And it
  buys little: the Cloud Run profiles (`mock`, `post-verdict`,
  `claude-code-review`, and the never-run `await-input`) are short, light,
  and none was in the measurement. Kept as §7 Q3's option B.

**Recommended: C**, delivered in two phases so the measured gain arrives
first: phase 1 is bursting at code defaults (RS-1, the scheduler render, the
worker's reading); phase 2 is the setting and the console (RS-2 to RS-4).
Phase 1 alone is Design A, so if the owner redirects phase 2, nothing of
phase 1 is wasted.

---

## 4. The recommended design

### 4.1 The numbers

| class | CPU request | CPU limit | memory (req = lim) | units | $/hour (unchanged) |
|---|---|---|---|---|---|
| `standard` | 4 | **8** | 8 GiB | 1 | ≈ 0.217 |
| `browser`, not live | 4 | **8** | 8 GiB | 1 | ≈ 0.217 |
| `browser`, live split: worker | 2 | **4** | 3 GiB | — | — |
| `browser`, live split: display | 2 | **3** | 5 GiB | — | — |
| `large` | 8 | **16** | 32 GiB | 4 | ≈ 0.514 |

* **`CPU_BURST_MAX_RATIO = 2`.** Twice the request is what the measurement
  can justify: the agents average 1.74 cores, so a node of co-located agents
  has on average more than 2 cores of idle request per agent to lend, and a
  higher limit would mostly describe CPU that is not there (§2.2.2).
* **The live split** follows BROWSER-SPLIT's 2 / 2 requests and memory
  unchanged. The worker gets the standard 2× ratio. The display's limit is
  3, its pre-BROWSER-SPLIT size: its measured peak was 1.90 cores
  (`dispatch.py` `LIVE_DISPLAY_SHARE` comment, `docs/browser-startup-speed.md`
  §11), so 2 guaranteed with room to 3 keeps what it had for Chromium's
  spikes while the agent gets its 2. Pod limit 7, pod request 4.
* **`large`** was reached by no profile when this was written (every
  profile named `standard` or `browser`, and a step only shrinks); it got the
  same ratio so it would be consistent when a profile named it. Since
  2026-10-22 `claude-code-large` does (contract request 113, §1a.3). Its limit of 16 exceeds
  Cloud Run's 8 — which is fine because the ceiling binds the **request**
  (§2.5), and the frozen validator says so (RS-2).
* **Every request ratio is inside Autopilot's 1:1 to 1:6.5**: standard 1:2,
  live worker 1:1.5, display 1:2.5, large 1:4. A size outside it would be
  silently raised by Autopilot and billed, so the validator refuses it.

**The gain expected.** The share of time pinned at the limit is the most
bursting can help: 22% overall, 30% on long runs. A pinned interval that
runs at s times today's CPU takes 1/s of its time, so the wall-time saving
is (pinned share) × (1 − 1/s):

| case | speed-up s in a pinned interval | overall (22% pinned) | long runs (30% pinned) |
|---|---|---|---|
| lower bound | 1 (alone on its node, or demand barely above 4) | 0% | 0% |
| **expected** | **1.5** (bursts to about 6 cores on average) | **≈ 7%** | **≈ 10%** |
| upper bound | 2 (the full limit, every pinned interval) | ≈ 11% | ≈ 15% |

The expected row's 1.5 is an assumption, stated so it can be checked, not a
measurement: a cgroup cannot see demand above its limit, so nobody yet knows
how many pinned intervals wanted 8 rather than 4.5. It is chosen between the
bounds for two reasons. A pinned agent's neighbours on a node average 1.74
of their 4 requested cores, so about 2.3 cores of idle request per
neighbour are there to borrow, which is enough for 1.5× with one neighbour
and more with several. But those neighbours are themselves pinned 22% of
the time, and when two pinned agents meet they split the idle CPU between
them, so the full 2× is the exception. The figures that will replace the
assumption, once LIVE-UTIL ships and phase 1 is live, are the ones §4.9
reads: the share of time above the **request** (4), the share at the
**limit** (8), and `throttled_usec` per attempt. The issue's own success measure — "the share
of time pinned at the CPU limit drops on long runs" — is read from the same
attempts' records before and after phase 1.

### 4.2 What stays frozen in code

In `apps/common/swarm_common/profiles.py` (RS-1, RS-2):

* **Class names**: `standard`, `browser`, `large`. An admin resizes a class;
  an admin never creates or deletes one. Profiles name classes, workflow
  steps name classes, pools are named `resource:<class>`, and the parity
  check reads them: a class appearing or disappearing at runtime would
  orphan every one of those.
* **The defaults**: `RESOURCE_CLASSES`, with the §4.1 CPU limits. They are
  what runs with no version document, what Cloud Run profiles always run at,
  and what Terraform's `locals.tf` mirrors.
* **The bounds**, as named constants and one pure validator,
  `check_size(name, size) -> list[SizeRefusal]`:

  | bound | value | why |
  |---|---|---|
  | `cpu_request` | 0.5 … 8, in steps of 0.25 | 0.5: Autopilot extended run time's minimum. 8: Cloud Run's ceiling, which binds every class because every class can be reached by a Cloud Run profile's step or fallback Job. 0.25: Autopilot's own granularity on non-bursting clusters, so the number is valid on both. |
  | `cpu_limit` | `cpu_request` … `CPU_BURST_MAX_RATIO` × `cpu_request` | the owner's "limit may be higher than request"; 2× per §4.1. No separate absolute cap: the request's maximum of 8 already bounds the limit at 16, and a second constant would only be one more number to keep in step with the ratio. |
  | `memory_gib` | 2 … 32, integer | 32: Cloud Run's ceiling. 2: below it the worker and an agent CLI do not start (measured reference 2.5 GiB, `profiles.py` "Sizing note"). |
  | `memory_gib / cpu_request` | 1 … 6.5 | Autopilot's ratio; outside it Autopilot raises a resource and bills it. |
  | `disk_gib` | 1 … `memory_gib` − 2 | it is a tmpfs slice of memory; at least 2 GiB stays for processes. |
  | ordering | `standard` ≤ `browser` ≤ `large`, every dimension of the request | `validate_resource_class_override` lets a step shrink a profile's class; a resize that made `standard` larger than `browser` would let a `claude-code-browser` step grow by naming "smaller" `standard`. |
  | live split | worker ≥ 1 cpu request and ≥ 2 GiB; display ≥ 1 cpu request and ≥ 5 GiB; the two requests and memories sum to the class's | 5 GiB: the display's resident tmpfs plus its measured peak is 4.73 GiB (`LIVE_DISPLAY_SHARE` comment). The sum is what makes the split leave units and billing unchanged. |

* **The invariants**: RS-1's amended 7 is enforced by the validator (memory
  and disk have no limit field at all; they cannot differ from their
  request because only one number is stored).
* **The unit formula** (§2.4).

### 4.3 Firestore

Two shapes, in database `swarm`, both written only by swarm-api's admin
route and read by the scheduler and swarm-api:

`control/runner_sizes` — the pointer, beside `control/settings`:

```json
{
  "current_version": 3,
  "updated_at": "2026-10-21T09:12:00Z",
  "updated_by": "admin@saga.xyz"
}
```

`runner_size_versions/{version}` — one immutable document per version
(document id the version number as a zero-padded string, `"000003"`):

```json
{
  "version": 3,
  "previous_version": 2,
  "created_at": "2026-10-21T09:12:00Z",
  "created_by": "admin@saga.xyz",
  "reason": "long implement runs pinned at 4 cores 30% of the time",
  "restored_from": null,
  "classes": {
    "standard": {"cpu_request": 4, "cpu_limit": 8, "memory_gib": 8, "disk_gib": 4, "units": 1},
    "browser":  {"cpu_request": 4, "cpu_limit": 8, "memory_gib": 8, "disk_gib": 4, "units": 1,
                 "live_split": {
                   "worker":  {"cpu_request": 2, "cpu_limit": 4, "memory_gib": 3},
                   "display": {"cpu_request": 2, "cpu_limit": 3, "memory_gib": 5}}},
    "large":    {"cpu_request": 8, "cpu_limit": 16, "memory_gib": 32, "disk_gib": 16, "units": 4}
  },
  "changed": [
    {"class": "standard", "field": "cpu_limit", "from": 4, "to": 8}
  ]
}
```

* **Every version is the whole table**, never a patch, so a reader needs one
  document and a restore is a copy.
* **`units` is stored, but derived**: the route computes it with the frozen
  formula and refuses a body that sends one. Stored so a reader of an old
  version sees what it charged without re-deriving.
* **The write is one transaction**: read the pointer, refuse 409 if the
  body's `base_version` is not `current_version` (two admins editing at
  once), validate with the frozen `check_size`, create version N+1, move the
  pointer. A version document is never updated or deleted; the audit trail
  is the collection.
* **No pointer document** means version 0: the code defaults. The first
  write creates version 1 with `previous_version: 0`.
* **A malformed stored version reads as the code defaults**, and the
  scheduler logs it at ERROR, as `_platform_setting` treats a malformed
  setting: a typo written straight to Firestore must not size a pod.
  Validation runs again on read (the same `check_size`).
* Indexes: none beyond the default; the history list orders by document id.
* Terraform: `terraform/modules/firestore` needs no new index. The
  scheduler's and swarm-api's accounts already read `control/*`; the
  scheduler needs `runner_size_versions/*` read, which the existing
  database-level `datastore.user` grant should cover. That is not taken on
  trust: lane SIZES-SCHED-2 carries it as a red-first check (§6), a test
  that reads the scheduler's Firestore role bindings out of
  `terraform/modules/iam` and fails unless the scheduler's account holds a
  role that reads every collection of database `swarm`, not only
  `control/*`. Today `terraform/modules/iam/bindings.tf` grants
  `roles/datastore.user` to each of `local.firestore_accounts` (every
  platform account) under a condition scoped to the database, so the check is expected to pass at
  once; it exists so that a later narrowing of that condition to named
  collections fails a test instead of failing admission.

### 4.4 Admission and dispatch: the size is pinned

In `apps/scheduler/scheduler/loop.py`, the admission path becomes:

1. Once per drain pass, `sizes.current(store)` reads the pointer and its
   version, validates it, and caches it for the pass. (One `get` of two
   documents; the drain loop already reads every READY agent.)
2. For an agent, `sized = sizes.for_task(version, task, profile, backend)`:
   the class's size from the version for a GKE profile, the **code default**
   for a Cloud Run profile (§4.7), with `units` derived.
3. `acquire_lease(task, units=sized.units, ...)` — the frozen transaction,
   unchanged.
4. `create_attempt` writes the pinned size on the attempt document (RS-4):
   `size_version`, `sized_cpu_request`, `sized_cpu_limit`,
   `sized_memory_gib`, `sized_disk_gib`, and for a live pod
   `sized_live_split`.
5. `self._router.dispatch(..., sized=sized)`: the GKE renderer renders
   **from `sized`**, never from `RESOURCE_CLASSES`.

A size change between step 1 and step 3 is harmless: the lease charged
`sized.units` and the pod is rendered from `sized`; they agree by
construction. The agent simply took the version of its pass. **A running agent
keeps the size it was admitted with**, because nothing re-renders a running
pod or re-charges a lease. A retried agent is a new admission and takes the
version current then; a parked agent released its lease and is re-admitted
the same way, which is invariant 1 working (it costs nothing while parked,
so there is nothing to keep).

**Rendering** (`apps/scheduler/scheduler/dispatch.py`):

* `GkeJobDispatcher._manifest`: `requests = {cpu: sized.cpu_request,
  memory, ephemeral-storage}`, `limits = {cpu: sized.cpu_limit, memory,
  ephemeral-storage}` — memory and ephemeral-storage identical in both,
  CPU differing. CPU rendered as millicores (`"4000m"`), so a quarter-core
  request is exact.
* `with_live_display`: the split comes from `sized.live_split` (the browser
  class's), or for `standard` and `large`, which have no stored split, from
  `LIVE_DISPLAY_SHARE` scaled to the class as today. The function keeps its
  refusal when the split leaves the worker nothing.
* `CloudRunJobDispatcher._build_job`: unchanged values (the code default,
  `limits` = the request); the comment changes to say why it is the request.
* `job_id_for`: unchanged — Cloud Run profiles do not follow, so no Job ever
  needs a size in its name.
* The env the worker gets gains `SWARM_SIZED_CPU_REQUEST`,
  `SWARM_SIZED_CPU_LIMIT`, `SWARM_SIZED_MEMORY_GIB`: platform-derived, from
  the scheduler's own reading, never from a task document (invariant 10;
  the same rule `spec_job_env` follows).

### 4.5 The API

All under `/v1/admin`, `admin_auth`, in a new router section of
`apps/swarm-api/swarm_api/routes/admin.py` with its logic in a new
`apps/swarm-api/swarm_api/runnersizes.py`.

**`GET /v1/admin/runner-sizes`** →

```json
{
  "current_version": 3,
  "classes": { "standard": {"cpu_request": 4, "cpu_limit": 8, "memory_gib": 8,
                            "disk_gib": 4, "units": 1}, "...": {} },
  "defaults": { "...the code's RESOURCE_CLASSES, same shape..." },
  "bounds": {
    "cpu_request": {"min": 0.5, "max": 8, "step": 0.25},
    "cpu_burst_max_ratio": 2,
    "memory_gib": {"min": 2, "max": 32},
    "memory_per_cpu": {"min": 1, "max": 6.5},
    "live_split": {"worker_min_cpu": 1, "worker_min_memory_gib": 2,
                   "display_min_cpu": 1, "display_min_memory_gib": 5}
  },
  "followers": {
    "standard": {"follows": ["claude-code", "codex", "generic", "merge", "indexer"],
                 "fixed": ["mock", "post-verdict", "claude-code-review"]},
    "browser":  {"follows": ["browser", "claude-code-browser"], "fixed": []},
    "large":    {"follows": [], "fixed": []}
  },
  "prices": {"vcpu_hour": 0.0445, "gib_hour": 0.0049225, "source": "docs list price, 2026-10-13"},
  "updated_at": "...", "updated_by": "..."
}
```

`followers` is computed from `RUNNER_PROFILES` and `resolve_backend`, so the
page never restates which profile is where. `await-input` is listed nowhere:
it is `control_plane` and never reserved.

**`POST /v1/admin/runner-sizes:preview`** — body as the PUT's, nothing
written →

```json
{
  "valid": false,
  "refusals": [{"class": "standard", "field": "cpu_limit", "value": 12,
                "bound": "cpu_limit <= 2 x cpu_request (8)"}],
  "classes": {"standard": {"units_before": 1, "units_after": 1,
                           "cost_hour_before": 0.217, "cost_hour_after": 0.217}},
  "pools": [{"pool": "global", "hard_limit": 40,
             "admits_before": {"standard": 40}, "admits_after": {"standard": 40}}],
  "recent_use": {"standard": {"attempts": 41, "since": "...",
                              "memory_peak_gib_max": 6.0, "memory_peak_gib_p95": 5.4,
                              "cpu_mean_cores_median": 1.74,
                              "share_idle": 0.15, "share_above_request": null,
                              "share_at_limit": 0.22, "share_at_limit_long": 0.30,
                              "throttled_share": null, "oom_near_miss": 0,
                              "by_size_version": [{"size_version": 0, "attempts": 41,
                                                   "wall_minutes_median": 31.0}]}}
}
```

`pools` reads the current pool documents (`hard_limit` in units) and says how
many agents of each class they admit before and after. `recent_use` reads the
last 7 days of attempts of that class (the LIVE-UTIL records once they ship,
the attempt summary fields until then) so a cut below what was used is
visible before it is saved. Its CPU shares are §4.9's readings, `null` where
the records that feed them do not exist yet (`share_above_request` and
`throttled_share` need LIVE-UTIL's samples); `by_size_version` groups the
same attempts by the `size_version` they were admitted at (RS-4), which is
how a change is confirmed (§4.9). The `wall_minutes_median` shown is
illustrative.

**`PUT /v1/admin/runner-sizes`** — body:

```json
{
  "base_version": 3,
  "reason": "free text, 1-500 characters, required",
  "classes": {"standard": {"cpu_request": 4, "cpu_limit": 8, "memory_gib": 8}},
  "confirm_below_recent_peak": false
}
```

A class the body does not name keeps its values; a field it does not name
keeps its value. `units` in the body is refused. Responses: **200** the new
version (the GET's shape); **409 `stale_base_version`** with the current
version; **422 `size_out_of_bounds`** with the preview's `refusals`; **422
`below_recent_peak`** when a memory cut goes under the class's recent max
peak and `confirm_below_recent_peak` is not true (§7 Q6).

**`POST /v1/admin/runner-sizes/restore`** — `{base_version, version,
reason}`: writes a new version whose `classes` are version `version`'s,
`restored_from: version`. Never rewinds the pointer.

**`GET /v1/admin/runner-sizes/versions?limit=50&before=<version>`** — the
audit: each version's `version`, `created_at`, `created_by`, `reason`,
`changed`, `restored_from`.

**`GET /v1/resource-classes`** (existing, `routes/platform.py`, any member):
serves the **current version's** sizes with `cpu_request`, `cpu_limit`,
`memory_gib`, `disk_gib`, `units` and `size_version`, keeping `cpu` as an
alias of `cpu_request` for one release so old clients read the guaranteed
number. It is already the route the UI and `check-contract-parity.sh` call
the source of truth, so a resize reaches every picker with no deploy.

Each write increments `admin_actions{action="runner_sizes"}` and writes one
structured log line naming the classes and fields changed (no values a
person typed beyond numbers and the reason).

### 4.6 The console: Admin → Runner sizes

A new tab beside **Pool limits** in the Admin section
(`apps/swarm-ui/src/App.tsx` `SECTIONS`, `{ id: 'runner-sizes', label:
'Runner sizes', admin: true }`), a new screen `apps/swarm-ui/src/RunnerSizes.tsx`
(not inside `AdminSettings.tsx`, which is 2,072 lines and Pool limits' own):

* **One card per class**: CPU request, CPU limit, memory (and the live split
  for `browser`), each a number input with its bound beside it. `disk_gib` is
  shown, not edited in phase 2 (§7 Q5).
* **Live validation**: the served `bounds` check every keystroke
  client-side, worded as the bound ("limit is at most 2 × request: 8"); the
  `:preview` call (debounced, 400 ms) is the authority and its refusals
  replace the client's.
* **Cost and units preview**: per class, units before → after and dollars
  an hour per agent before → after, and the pools table: "global admits 40
  standard agents → 20". A CPU limit change shows "$0.00 / hour: Autopilot
  bills requests"; a Cloud Run-fixed profile is listed under "does not
  follow" with the reason (§4.7).
* **Recent use** beside memory: max and p95 peak over 7 days, from the
  preview. A cut below the max needs a typed confirmation of the class name
  (CLAUDE.md: anything destructive requires a typed confirmation).
* **Save** needs a reason; a 409 reloads the current version and shows what
  changed under the admin.
* **History**: the versions list — when, who, why, what changed — with
  "Restore this version", which goes through the same preview.

### 4.7 Which profiles follow, and which cannot

| profiles | follow a resize | burst | why |
|---|---|---|---|
| `claude-code`, `codex`, `generic`, `merge`, `indexer` | yes | yes | GKE: rendered per pod from the pinned size |
| `browser`, `claude-code-browser` (live split) | yes | yes | GKE; the live split is part of the browser class's setting |
| `mock`, `post-verdict`, `claude-code-review` | **no** | **no** | Cloud Run fixes resources on the Job, not the execution; updating a shared Job in place races between schedulers (§3 C′); and a Cloud Run container is billed its limit |
| `await-input`, `fix-loop` | n/a | n/a | `control_plane`: never reserved, never run |
| any GKE profile on its `cloud_run_fallback_profiles` rollback Job | no | no | the same Cloud Run rule; the Terraform Job is sized from `locals.tf` |

The admin page lists the second group under each class it uses, so nobody
resizes `standard` and wonders why `claude-code-review` did not change.

### 4.8 Every file a build touches

| area | file | change |
|---|---|---|
| contract | `apps/common/swarm_common/profiles.py` | RS-1, RS-2, RS-3 |
| contract | `apps/common/swarm_common/models.py` | RS-4 (`Attempt` sized fields) |
| contract | `CONTRACT.md` | invariant 7's text (RS-1) |
| contract | `docs/contract-change-requests.md` | entries for RS-1 to RS-4 |
| contract | `scripts/lib/check-contract-parity.sh` | §1 compares `RESOURCE_UNITS` to the code **defaults**, named so |
| scheduler | `apps/scheduler/scheduler/sizes.py` (new) | read, validate, cache the current version; `for_task` |
| scheduler | `apps/scheduler/scheduler/loop.py` | units from `sized`; pass `sized` to dispatch |
| scheduler | `apps/scheduler/scheduler/store.py` | `create_attempt` writes the sized fields; read the two documents |
| scheduler | `apps/scheduler/scheduler/dispatch.py` | render requests ≠ limits for CPU; split from `sized`; worker env |
| worker | `apps/agent-worker/agent_worker/config.py` | `memory_limit_bytes_of` from the sized env, then cgroup `memory.max`, then the catalogue |
| worker | `apps/agent-worker/agent_worker/lifecycle.py` | `_cpu_limit` fallback reads the sized env before the catalogue; records the request beside the limit |
| worker | `apps/agent-worker/agent_worker/metrics.py` | nothing beyond LIVE-UTIL's throttling; the `cpu.max` read already proves the limit |
| swarm-api | `apps/swarm-api/swarm_api/runnersizes.py` (new) | store, preview, validation glue, followers |
| swarm-api | `apps/swarm-api/swarm_api/routes/admin.py` | the five admin routes |
| swarm-api | `apps/swarm-api/swarm_api/schemas.py` | request models |
| swarm-api | `apps/swarm-api/swarm_api/routes/platform.py` | `/v1/resource-classes` serves the current version |
| swarm-api | `apps/swarm-api/swarm_api/validation.py` | `validate_resource_class_override` compares current sizes |
| swarm-api | `apps/swarm-api/swarm_api/waiting.py`, `service.py` | units and sizes from the current version |
| UI | `apps/swarm-ui/src/RunnerSizes.tsx` (new), `App.tsx`, `api.ts`, `types.ts`, `styles/admin.css` | the tab, its calls and types |
| UI | `apps/swarm-ui/src/AgentDetail.tsx` | shows "4 guaranteed, bursts to 8" from the attempt's sized fields (after LIVE-UTIL-UI merges; same file) |
| forms | `.github/ISSUE_TEMPLATE/*.yml` | the "Where" list gains "Admin → Runner sizes" (`tests/unit/scripts/test_issue_forms.py` holds it to `SECTIONS`) |
| plugin | `apps/swarm-mcp/swarm_mcp/profiles.py`, `render.py` | sizes from `/v1/resource-classes`, not the code table |
| Terraform | `terraform/infra/locals.tf` | the mirror's comment says it is the Cloud Run default; values unchanged |
| docs | `docs/architecture.md`, `kubernetes/README.md`, `docs/web-ui/` admin page doc | invariant 7 as amended; where sizes live |

### 4.9 How an admin sizes a class from the utilisation charts

LIVE-UTIL gives every attempt close-to-real-time CPU and memory with
throttling: per sample, cores used (from `cpu.stat` `usage_usec`), the
enforced limit (`cpu.max`), `nr_throttled` / `nr_periods` and
`throttled_usec`, memory in use and `memory.peak`. Agent detail charts one
attempt; the Runner sizes page's preview (`recent_use`, §4.5) aggregates the
same records per class over 7 days. An admin sizes a class from the
aggregate and uses single-attempt charts to see why. The readings, per
class:

| reading | what it is | what it says |
|---|---|---|
| mean cores, idle share | median of attempts' mean use; share of samples under 0.5 core | how much of the **request** is used; today 1.74 of 4, idle 15% |
| share above request | samples using more than the request | how often bursting is doing work; 0 before phase 1 |
| share at limit | samples within 5% of the enforced limit, overall and for runs ≥ 40 min | demand the class still does not meet; today 22%, 30% on long runs |
| throttled share | `throttled_usec` ÷ the attempt's wall time; `nr_throttled` ÷ `nr_periods` | time the kernel held the agent at its limit |
| memory peak | max and p95 of `memory.peak`, `oom_near_miss` count | headroom under the one memory number; today max 6.0 of 8 GiB |

What each pattern means and the change it leads to:

* **Pinned at the limit on long runs, and throttled.** The class wants more
  CPU than its limit. If the limit is below 2 × request, raise the limit:
  Autopilot bills requests, so the preview shows $0.00 and units
  unchanged. If it is already at 2 ×, the only lever is the request, which
  costs money and may change units; the preview prices it, and §7 Q2 is the
  owner's.
* **Often above the request, rarely at the limit.** Bursting is doing what
  it is for. No change.
* **Above the request but neither at the limit nor throttled, yet runs got
  no faster.** The node had nothing idle to lend: a burst past the request
  is shared by the kernel between the pods on a node, and contention below
  the limit is **not counted** by `nr_throttled`, which only counts the
  `cpu.max` quota. The charts cannot show that demand, so the evidence is
  run time (below), not the CPU panels. Raising the limit does not help;
  only a larger request does.
* **Mean far under the request, idle often, rarely at the limit.** The
  class is over-requested: a candidate to lower the request, which cuts
  cost and may free units. Lowering the guarantee makes contended runs
  slower than today, so it is §7 Q2's decision, not an admin's alone.
* **Memory max peak within about 15% of the class, or any OOM or
  `oom_near_miss`.** Raise memory; the preview shows units (memory over
  8 GiB moves `standard` to 2) and the Autopilot ratio bound. Memory never
  bursts, so a peak above it is a kill and costs the attempt back to its
  last checkpoint.
* **Memory max peak under half the class over 30 or more attempts.** It
  may come down; the page asks for the class name typed when the cut is
  under the 7-day max (§7 Q6), because one rare large run is exactly what
  the max records.

**Enough data first.** At least 30 attempts of the class in the window, and
long runs read separately: today they sit at the limit 30% of the time
against 7% for short runs, so a mix shifted toward short runs would look
like a fix. Below 30 attempts the page shows the readings without the
pattern hints.

**Confirming a change, before and after.** Every attempt records the size
version it was admitted at (RS-4), so the preview's `by_size_version` puts
the version before a change beside the version after it, over attempts of
the same class: median wall time overall and on long runs, share at the
limit, throttled share, memory max peak, dollars an hour. A change has
worked when the at-limit share on long runs and their median wall time both
drop with no rise in memory near-misses; it has not when they do not move
after 30 attempts on the new version, and History's "Restore this version"
(§4.6) takes the class back, as a new version, through the same preview.
Phase 1 predates size versions, so its measurement is the same comparison
split by date instead, attempts before the release (limit 4) against
attempts after it (limit 8), each attempt's `cpu_limit_cores` saying which
side it is on.

The thresholds above (5% of the limit, 15% memory headroom, half, 30
attempts) are the page's hints, held in the UI, not frozen bounds: they
guide an admin and refuse nothing. The bounds that refuse are §4.2's.

---

## 5. FROZEN CONTRACT: change requests

Written in the shape of `docs/contract-change-requests.md`. They are **not**
filed there: the operator adds them, at the next free numbers (107 onwards
at this reading), once the owner approves. The letters are their stable
names.

### RS-1. `profiles.py`, `CONTRACT.md`: invariant 7 amended for CPU; `ResourceClass.cpu_limit`

**Status:** ACCEPTED by the owner 2026-10-20 (proposed by RUNNER-SIZES-DESIGN, part of #1689). Owner
decision of 2026-10-20 ("CPU limits may be higher than requests ... memory
stays fixed").

#### What is true today

Invariant 7: "requests == limits. No bursting; bursting is what gets
OOM-killed." `ResourceClass` has one `cpu`, documented as "BOTH request and
limit", and `profiles.py`'s module docstring forbids bursting.

#### The change

```python
@dataclass(frozen=True)
class ResourceClass:
    """A sizing envelope.

    `cpu` is the CPU REQUEST: what admission charges, what Autopilot bills
    and what every container is guaranteed. `cpu_limit` is what it may burst
    to on GKE Autopilot when its node has idle requested CPU; a burst past
    the request is throttled under contention, never killed. Cloud Run
    renders `cpu` alone. `memory_gib` and `disk_gib` are request AND limit:
    memory is not compressible, and a memory burst is a kill.
    """

    name: str
    cpu: float
    memory_gib: int
    disk_gib: int
    units: int
    requires_preview_disk: bool = False
    cpu_limit: float | None = None   # None: equal to `cpu`

    @property
    def cpu_limit_cores(self) -> float:
        return self.cpu if self.cpu_limit is None else self.cpu_limit
```

`__post_init__` adds: `cpu <= cpu_limit_cores <= CPU_BURST_MAX_RATIO * cpu`;
the Cloud Run ceiling keeps binding
`cpu` and `memory_gib` (not the limit). `RESOURCE_CLASSES` gains
`cpu_limit=8`, `8`, `16` on `standard`, `browser`, `large`. The module
docstring's "Two hard rules" becomes: memory and disk requests == limits;
CPU limit within the ratio on GKE only.

CONTRACT.md invariant 7 becomes:

> 7. Memory and ephemeral storage: requests == limits, on every container.
>    CPU: request <= limit <= `CPU_BURST_MAX_RATIO` x request, on GKE
>    Autopilot only; Cloud Run renders the request as its single limit.
>    Admission charges, and Autopilot bills, the request. A CPU burst is
>    throttled under contention, never killed; memory never bursts, because
>    a memory burst is what gets OOM-killed. The pod's QoS class is
>    Burstable, so its kernel OOM score is above a Guaranteed pod's; the
>    periodic checkpoint (invariant 8) bounds that kill as it bounds any.

**Tests:** `tests/unit/common/test_cpu_burst_contract.py`: every class has
`cpu <= cpu_limit_cores <= 2 * cpu`; memory has no limit field; a class with
`cpu_limit = 2.5 * cpu`, or below `cpu`, is refused at construction; `large`
with `cpu_limit=16` constructs (the ceiling binds the request);
`test_invariant_7_text_names_cpu_only` reads CONTRACT.md.

#### Rollback

`cpu_limit` back to `None` on every class: every pod renders requests ==
limits again. A running pod keeps its limit until it ends.

#### What it would break

Tests that assert requests == limits for CPU on the GKE manifest
(`tests/unit/scheduler/test_dispatch*.py`), changed in SIZES-SCHED, red
first. Nothing reads `rc.cpu` as a limit except `lifecycle.py`'s fallback,
which reads the cgroup first.

#### Invariants

**7** amended as above. **8**: unchanged and named in 7's text. **1-6, 9,
10** untouched.

### RS-2. `profiles.py`: the frozen bounds, the derived units, `check_size`

**Status:** ACCEPTED by the owner 2026-10-20 (proposed the same day, part of #1689).

#### The change

```python
CPU_BURST_MAX_RATIO = 2
CPU_REQUEST_MIN, CPU_REQUEST_MAX, CPU_REQUEST_STEP = 0.5, 8, 0.25
MEMORY_GIB_MIN, MEMORY_GIB_MAX = 2, 32
MEMORY_PER_CPU_MIN, MEMORY_PER_CPU_MAX = 1.0, 6.5
LIVE_WORKER_MIN = (1, 2)      # cpu request, memory GiB
LIVE_DISPLAY_MIN = (1, 5)
CLASS_ORDER = ("standard", "browser", "large")

def units_for(cpu_request: float, memory_gib: int) -> int:
    return max(math.ceil(cpu_request / 4), math.ceil(memory_gib / 8))

@dataclass(frozen=True)
class SizeRefusal:
    resource_class: str
    field: str
    value: float | None
    bound: str

def check_size(sizes: Mapping[str, ResourceSize]) -> list[SizeRefusal]: ...
```

`check_size` is pure (no I/O), takes the whole table so it can check the
ordering, and returns every refusal rather than the first. `RESOURCE_CLASSES`'
units are asserted equal to `units_for` at import.

**Tests:** `tests/unit/common/test_runner_size_bounds.py`: each bound at,
just inside and just outside its edge; the ordering (a `standard` larger
than `browser` in any dimension is refused); the live split summing to the
class; `units_for` gives 1 / 1 / 4 for today's classes; a property test over
random tables that whatever `check_size` accepts renders a manifest whose
request ratio is inside Autopilot's 1:1 to 1:6.5.

#### What it would break

Nothing that exists: new names only.

#### Invariants

**7**: the validator is its enforcement for admin-set sizes. **10**: the
bounds are frozen code, so neither a caller nor an admin can size a pod
outside them.

### RS-3. `profiles.py`: `ResourceSize` and `effective_class`

**Status:** ACCEPTED by the owner 2026-10-20 (proposed the same day, part of #1689).

#### The change

```python
@dataclass(frozen=True)
class LiveShare:
    cpu_request: float
    cpu_limit: float
    memory_gib: int

@dataclass(frozen=True)
class ResourceSize:
    cpu_request: float
    cpu_limit: float
    memory_gib: int
    disk_gib: int
    live_split: tuple[LiveShare, LiveShare] | None = None   # (worker, display)

    @property
    def units(self) -> int:
        return units_for(self.cpu_request, self.memory_gib)

def default_sizes() -> Mapping[str, ResourceSize]: ...
def effective_size(name: str, version: Mapping[str, ResourceSize] | None,
                   backend: Backend) -> ResourceSize:
    """`version`'s size for a GKE profile; the code default for Cloud Run."""
```

One shape, frozen, so the scheduler, swarm-api and the worker parse a
version document the same way instead of each restating it (the reason
`check-contract-parity.sh` exists). `default_sizes()` is built from
`RESOURCE_CLASSES` and the live split's defaults.

**Tests:** `tests/unit/common/test_runner_size_shape.py`: `default_sizes()`
equals `RESOURCE_CLASSES`; `effective_size` returns the default for
`CLOUD_RUN_JOB` whatever the version says, and the version's for
`GKE_AUTOPILOT`; a version missing a class falls back to that class's
default.

#### What it would break

Nothing that exists.

#### Invariants

**10**: the backend, not a caller, decides whether a size follows.

### RS-4. `models.py`: `Attempt` records the size it was rendered at

**Status:** ACCEPTED by the owner 2026-10-20 (proposed the same day, part of #1689).

#### What is true today

`Attempt.cpu_limit_cores` is what the worker MEASURED (`cpu.max`, else the
catalogue). Nothing records what the scheduler RENDERED, and after a resize
the catalogue no longer says what an older attempt ran at.

#### The change

```python
    size_version: int | None = None
    sized_cpu_request: float | None = None
    sized_cpu_limit: float | None = None
    sized_memory_gib: int | None = None
    sized_disk_gib: int | None = None
    sized_live_split: dict[str, dict[str, float]] | None = None
    # {"worker":  {"cpu_request": 2, "cpu_limit": 4, "memory_gib": 3},
    #  "display": {"cpu_request": 2, "cpu_limit": 3, "memory_gib": 5}}
    # set only on a live-browser attempt; RS-3's LiveShare pair as a mapping
```

Written by the scheduler's `create_attempt`, never by the worker or a
caller. All optional: attempts written before the change have none, and
readers fall back to the class's code default as they do now.
`sized_live_split` is `None` on every attempt that is not a live pod, so a
live attempt's two containers are as checkable after the fact as a plain
attempt's one. It is a mapping keyed `worker` / `display` rather than RS-3's
`LiveShare` tuple because the attempt document is Firestore JSON and a
named key survives a reader that does not know the tuple's order.

**Tests:** `tests/unit/common/test_attempt_sized_fields.py`: the fields
default to `None`, round-trip through the codec (including
`sized_live_split` with both keys, and a field validator refusing a
mapping missing either key or carrying anything but the three numbers), and are not in the
signed step spec (`test_specsign_covers` — the worker does not act on them; it
acts on its cgroup).

#### What it would break

Nothing: optional fields.

#### Invariants

**5**: the record is per attempt, so a retried agent's new size never
rewrites an old attempt's. **7**: the record is what makes "a running agent
keeps its admitted size" checkable after the fact.

---

## 6. Build plan

Lanes split by territory: no two lanes edit the same file. Each pushes its
tests alone first (red), then the fix (`<branch>-fix`). Order is by
dependency; a lane starts only after the ones it names have merged.

**Prerequisites already running:** BROWSER-SPLIT (edits
`dispatch.py::LIVE_DISPLAY_SHARE`), LIVE-UTIL-API (worker sampler,
`routes/tasks.py`), LIVE-UTIL-UI (`AgentDetail.tsx`). Lanes below that share
those files start after them.

### Phase 1 — bursting at the code defaults

**SIZES-CONTRACT-1** (after owner approval of RS-1). Files:
`apps/common/swarm_common/profiles.py`, `CONTRACT.md`,
`docs/contract-change-requests.md`, `tests/unit/common/test_cpu_burst_contract.py`.
Red first: `test_cpu_burst_contract.py` (fails: no `cpu_limit`).

**SIZES-SCHED-1** (after SIZES-CONTRACT-1 and BROWSER-SPLIT). Files:
`apps/scheduler/scheduler/dispatch.py`,
`tests/unit/scheduler/test_dispatch_cpu_burst.py` (new),
`tests/unit/scheduler/test_dispatch_live_sidecar.py`. Red first: the GKE
manifest's worker container has `requests.cpu == "4000m"`,
`limits.cpu == "8000m"`, memory and ephemeral-storage equal in both; the live
split renders worker 2/4 and display 2/3 with memory equal; the Cloud Run
Job's `limits.cpu` is the request (`"4"`), never 8; no container anywhere
has a memory limit different from its request (a sweep over every profile
× class × live on/off, printing the count visited).
**Canary**, run by the operator after release on dev: one `claude-code`
attempt's `cpu_limit_cores` reads 8 with `cpu_limit_source: cgroup`, and
`kc get daemonset -n kube-system efficiency-daemon` shows the daemon.
Without both, phase 1 is not done.

**SIZES-WORKER-1** (parallel with SIZES-SCHED-1). Files:
`apps/agent-worker/agent_worker/lifecycle.py`,
`apps/agent-worker/agent_worker/config.py`,
`tests/unit/worker/test_sized_limits.py` (new). Red first: with
`SWARM_SIZED_CPU_LIMIT=8` and no cgroup, `_cpu_limit` returns 8 with
source `resource_class`; with `SWARM_SIZED_MEMORY_GIB=6`,
`memory_limit_bytes_of` returns 6 GiB, not the catalogue's 8.

### Phase 2 — sizes as a setting

**SIZES-CONTRACT-2** (after phase 1 merges and RS-2 to RS-4 are approved).
Files: `apps/common/swarm_common/profiles.py`,
`apps/common/swarm_common/models.py`, `docs/contract-change-requests.md`,
`scripts/lib/check-contract-parity.sh`,
`tests/unit/common/test_runner_size_bounds.py`,
`tests/unit/common/test_runner_size_shape.py`,
`tests/unit/common/test_attempt_sized_fields.py`. Red first: those three
files.

**SIZES-API** (after SIZES-CONTRACT-2). Files:
`apps/swarm-api/swarm_api/runnersizes.py` (new),
`apps/swarm-api/swarm_api/routes/admin.py`,
`apps/swarm-api/swarm_api/schemas.py`,
`apps/swarm-api/swarm_api/routes/platform.py`,
`apps/swarm-api/swarm_api/validation.py`,
`apps/swarm-api/swarm_api/waiting.py`,
`apps/swarm-api/swarm_api/service.py`,
`tests/unit/control_plane/test_runner_sizes_api.py` (new),
`tests/unit/control_plane/test_resource_classes_follow_version.py` (new).
Red first: a PUT with `base_version` stale is 409 and writes nothing; a PUT
out of bounds is 422 naming every refusal; a PUT that sends `units` is 422;
a successful PUT creates version N+1 and never updates version N; restore
writes a new version; `/v1/resource-classes` serves the new version; a
non-admin gets 403 on every route; a memory cut below the recent max
without the confirmation is 422 `below_recent_peak`; a malformed stored
version reads as the code defaults; the preview's `recent_use` reports
§4.9's shares, `null` (never 0) where the LIVE-UTIL records are missing,
and `by_size_version` splits the window's attempts by the version they
were admitted at.

**SIZES-SCHED-2** (after SIZES-CONTRACT-2; parallel with SIZES-API).
Files: `apps/scheduler/scheduler/sizes.py` (new),
`apps/scheduler/scheduler/loop.py`, `apps/scheduler/scheduler/store.py`,
`apps/scheduler/scheduler/dispatch.py`,
`tests/unit/scheduler/test_admission_follows_size.py` (new),
`tests/unit/scheduler/test_scheduler_reads_size_versions.py` (new). Red
first: with version 2 making `standard` 6/12, a READY claude-code agent is
leased with `units == 2` and its manifest requests 6 / 12 GiB; the attempt
document records `size_version: 2` and the sized fields; a live-browser
attempt records `sized_live_split` equal to the two containers its manifest
renders, and a non-live attempt records it as `None`; a version change
**after** the lease and before dispatch does not change the manifest; a
`mock` agent (Cloud Run) still gets the default and units 1 whatever the
version says; a malformed version leases at the default and logs at ERROR;
`test_scheduler_reads_size_versions.py` (§4.3) parses
`terraform/modules/iam/bindings.tf` and fails unless the scheduler's
account is a key of `local.platform_accounts` (which
`local.firestore_accounts` is built from) and the Firestore grant's
`firestore_condition`, when set, names the database, not a collection
under it.
Then `make smoke concurrency-test race-test` against dev (CLAUDE.md: this
lane touches admission).

**SIZES-UI** (after SIZES-API). Files:
`apps/swarm-ui/src/RunnerSizes.tsx` (new), `apps/swarm-ui/src/App.tsx`,
`apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/types.ts`,
`apps/swarm-ui/src/styles/admin.css`, `.github/ISSUE_TEMPLATE/*.yml`,
`apps/swarm-ui/src/__tests__/runnerSizes.*.test.tsx` (new). Red first: the
tab is listed under Admin for an admin only; a limit above 2× the request
shows the bound and disables Save; the preview's units and pools are drawn;
a 409 reloads; a memory cut below the recent max asks for the class name
typed; history lists versions and Restore calls the restore route; §4.9's pattern
hints appear with 30 or more attempts and not with 29, and a `null` share
renders as "no data", not 0%;
`tests/unit/scripts/test_issue_forms.py` passes with the new tab.

**SIZES-DETAIL** (after LIVE-UTIL-UI and SIZES-API). Files:
`apps/swarm-ui/src/AgentDetail.tsx` and its test. Red first: an attempt
with `sized_cpu_request: 4, sized_cpu_limit: 8` reads "4 cores guaranteed,
bursts to 8"; an attempt without sized fields reads as today.

**SIZES-MCP** (after SIZES-API). Files:
`apps/swarm-mcp/swarm_mcp/profiles.py`, `apps/swarm-mcp/swarm_mcp/render.py`,
`tests/unit/mcp/test_profiles_sizes_served.py` (new). Red first: with the
served route returning version 2, `swarm_profiles` shows version 2's sizes.

**SIZES-DOCS** (last). Files: `docs/architecture.md`, `kubernetes/README.md`,
`terraform/infra/locals.tf` (comment only), this document's status line.
Red first: a `test_docs_describe_what_was_built.py` case that the status line
names the merged lanes.

---

## 7. Owner questions

Recommendation first in each. Also in `questions.json`.

**The owner's answers, 2026-10-20 (about 11:00Z and 15:55Z).** Every answer took the recommendation:

| Q | Answer |
|---|---|
| Q1 burst limit | A: limit = 2 × request, with requests unchanged |
| Q2 lower the request | A: no, keep the request at 4 |
| Q3 Cloud Run profiles | A: they stay at the code defaults, listed as "does not follow" |
| Q4 who may change sizes | A: any platform admin, with a reason required; every version is audited and restorable |
| Q5 `disk_gib` editable | A: no, it is shown and not edited |
| Q6 a memory cut below recent use | A: allowed with a typed confirmation; the preview shows the 7-day max and the p95 |
| Q7 order of delivery | A: phase 1 first, then phase 2 after its canary |
| Q8 fairness above the request | A: accept it |

Contract requests RS-1 to RS-4 (§5) were all accepted on 2026-10-20, so phase 2 starts after phase 1's canary without a further sign-off.

**Q1. The burst limit per class.**
* **A (recommended): limit = 2 × request, requests unchanged** (standard
  4/8, browser 4/8, live worker 2/4 and display 2/3, large 8/16). Cost
  unchanged; no attempt can be slower than today.
* B: no CPU limit at all — burst to whatever the node's idle requests allow.
  More burst on quiet nodes; one runaway process can soak a neighbour's idle
  CPU without bound, and the console can no longer state a ceiling.
* C: bursting only on some classes (e.g. standard but not live browser).
  Keeps the live pod Guaranteed QoS at the cost of its agent's speed.
* D: 1.5 × request (standard 4/6). More conservative; less of the 22%
  helped.

**Q2. Lower the request to save money?**
* **A (recommended): no — keep the request at 4.** The request is the
  guarantee; it is what keeps "never slower than today".
* B: standard request 2, limit 8. Halves the CPU bill (mean use is 1.74),
  doubles concurrency per unit if units follow; under contention an agent
  is throttled to 2, slower than today.
* C: decide after a month of LIVE-UTIL data with bursting on.

**Q3. Cloud Run-backed profiles and the setting.**
* **A (recommended): they stay at the code defaults** (`mock`,
  `post-verdict`, `claude-code-review`), listed on the page as "does not
  follow".
* B: they follow, through a Cloud Run Job per (tenant, profile, class, size
  version), GC'd by the reconciler. More Jobs, more moving parts.
* C: move them to GKE so everything follows (each is its own contract
  request; `post-verdict` and `claude-code-review` run as their own Job's
  account by design, so this is a security-posture change).

**Q4. Who may change sizes, and with what check.**
* **A (recommended): any platform admin, reason required, every version
  audited and restorable.**
* B: as A, plus a second admin's approval before a version takes effect.
* C: the owner only.

**Q5. Is `disk_gib` (the tmpfs workspace) editable in phase 2?**
* **A (recommended): no — shown, not edited**; it is a slice of memory and
  the measured case for changing it does not exist yet.
* B: yes, within 1 … memory − 2.

**Q6. A memory cut below what the class recently used.**
* **A (recommended): allowed with a typed confirmation**, the preview
  showing the 7-day max and p95 peaks.
* B: refused outright while any attempt in 7 days peaked above it.
* C: no check.

**Q7. Order of delivery.**
* **A (recommended): phase 1 (bursting at code defaults) first, phase 2
  (the setting and the page) after its canary.** The measured gain arrives
  in two lanes.
* B: both phases together, one release.

**Q8. Fairness above the request.**
* **A (recommended): accept it** — every tenant keeps its request; CPU
  above it is shared by the kernel between whichever pods share a node.
* B: refuse cross-tenant burst by giving each tenant's pods their own nodes
  (Autopilot workload separation): raises minimum requests and cost, and
  ends most of the gain, since burst comes from neighbours.
