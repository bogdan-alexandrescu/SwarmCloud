# Execution backends

> **Workspace sizes are memory, not disk.** These classes originally specified
> 20/40/100 GiB of disk-backed ephemeral storage. That is a Cloud Run Preview
> feature the Terraform google provider cannot express — `empty_dir.medium`
> accepts only `"MEMORY"` — so the workspace is a tmpfs carved out of the
> container's memory: **standard ~4 GiB, browser ~4 GiB, large ~16 GiB**, each a
> slice of that class's total memory rather than capacity on top of it.
> The upside is that this path is fully GA and supports live migration, which the
> Preview disk explicitly does not, so the no-interruption requirement that drove
> the Cloud Run choice is better served here than by the feature we set out to use.
> **This tmpfs is Cloud Run only.** On GKE the workspace, `/tmp` and HOME are
> disk `emptyDir`s and the tool caches a 20 GiB PVC (§4), and every agent
> profile but `mock` runs there.

Two backends. As read on 2026-10-21 (system analysis, epic #1822), GKE
Autopilot runs nearly every agent and Cloud Run Jobs runs `mock` plus the
fallback Jobs Terraform keeps for the profiles that moved. It got there one
profile at a time: GKE Autopilot first existed for the three things Cloud Run
cannot do, and, since contract request 53 (2026-10-08), for `claude-code`, the
one thing Cloud Run did too slowly: start it (§4). Since contract request 63 (2026-10-10) `indexer` runs there too, the
canary for #939: a new Cloud Run instance's internet path opens too slowly.
Contract requests 64-66 (owner, 2026-10-10, #939 option A) move `generic`,
`codex` and `merge` after it, for the same reason, so Cloud Run is left with
`mock` and the two disabled profiles that cannot move (§4, item 6).

---

## 1. Why Cloud Run Jobs was chosen as primary

This is the reasoning behind the first design, and it still says what each
backend risks. It no longer says where the work runs: see the intro, and
§4 for each profile that moved.

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
| `generic` | `agent-runtime-base` | standard | **GKE Autopilot** (contract request 64, #939 option A; its Cloud Run Jobs kept, idle, as the rollback) | none |
| `claude-code` | `agent-runtime-base` | standard | **GKE Autopilot** (contract request 53; its Cloud Run Jobs kept, idle, until 2026-10-15 as the rollback) | anthropic |
| `codex` | `agent-runtime-base` | standard | **GKE Autopilot** (contract request 65, #939 option A; its Cloud Run Jobs kept, idle, as the rollback; disabled) | openai |
| `browser` | `agent-runtime-browser` | browser | **GKE Autopilot** | anthropic |
| `indexer` | `agent-runtime-indexer` | standard | **GKE Autopilot** (contract request 63, the canary for #939; its Cloud Run Jobs kept, idle, as the rollback) | anthropic |
| `claude-code-browser` | `agent-runtime-browser` | browser | **GKE Autopilot** (contract request 67: Cloud Run cannot size Chromium's `/dev/shm`) | anthropic |
| `claude-code-lightpanda` | `agent-runtime-lightpanda` | standard | **GKE Autopilot** (contract request 127: born on GKE, so it has no Cloud Run rollback Job) | anthropic |

`claude-code-browser` (contract request 67, accepted by the owner 2026-10-11)
is `claude-code` with Playwright and Chromium beside the agent, for work that
has to SEE a page: visual QA, a screenshot of what a change rendered, a UI bug
reproduced by following its report. `browser` cannot do that -- it is a
scripted runner that plays `input.url` or `input.actions` and has no agent
([what it waits for and what it reports](browser-scripted-runner.md)) --
and `claude-code` cannot either, because `agent-runtime-base` has no Chromium.
It runs on the **`browser` resource class**, 4 vCPU and 8 GiB at 1 unit
against the `resource:browser` pool it shares with `browser`: the same size and
weight as a `standard` claude-code step since contract request 79 (owner,
2026-10-14). It was 8 vCPU / 16 GiB at 2 units, sized for the 2 GiB
memory-backed `/dev/shm` every GKE worker pod mounts, which is charged to the
memory limit; four browser pods then peaked at 2.13 cores and 1.85 GiB, shm
included ([browser-startup-speed.md §11](browser-startup-speed.md)). What it
still costs that claude-code does not is a GKE pod and a place in the
`resource:browser` pool, which caps browser agents separately. Pick `claude-code` for work that never renders a
page. Chromium's own sandbox stays off (the pod refuses user namespaces and
drops every capability; the pod is the isolation), so the agent launches it
with `chromium_sandbox=False`, and the runner says so in its instructions.

`claude-code-lightpanda` (contract request 127, accepted by the owner
2026-10-31; [design](design/lightpanda-runtime.md)) is `claude-code` on
`agent-runtime-lightpanda`, which is `agent-runtime-base` plus the Lightpanda
headless browser and no Chromium. It runs on the **`standard` class**, not
`browser`: Lightpanda draws no pixels, so the pod needs neither the display
container nor the memory a Chromium needs, and the agent is charged to
`resource:standard` and its own `runner:claude-code-lightpanda` pool rather
than to the `resource:browser` pool that caps live browsers. That is the
point of it: DOM-only reading, scraping and link checks stop taking a place
a live browser could use. It is on GKE for the same reason `claude-code` is
(start latency, §4) and because `terraform/infra/locals.tf` leaves it out of
`cloud_run_fallback_profiles`, so no Cloud Run Job exists for it to fall back
to. Which profile to pick for which page is
[browser-qa.md §2](browser-qa.md#2-which-one-for-which-job).

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
the correct behaviour is for the agent to park as `CREDENTIAL_MISSING` and cost
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
browser    4 vCPU   8 GiB   ~4 GiB workspace (tmpfs)   (GKE only)
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
6. **`generic`, `codex` and `merge`, for the same internet path** (contract
   requests 64, 65 and 66, owner 2026-10-10, #939 option A, merged only after
   the indexer canary is measured). Each clones or calls GitHub or a provider
   API first; each keeps its Cloud Run Jobs, idle, as the rollback, and rolls
   back as `indexer` does. `merge` can move because it runs as the tenant's
   worker account on either backend (contract request 47), which is the
   identity the GKE pod's KSA is bound to. `post-verdict` and
   `claude-code-review` cannot: each was designed to run as an account of its
   own (`swarm-<tenant>-post-verdict`, `swarm-<tenant>-review`), and
   `post-verdict` also reads `FORGE_HOST`/`FORGE_OWNER`/`FORGE_REPO` from its
   Job's Terraform-rendered environment, while a GKE pod runs as the tenant's
   one worker KSA and carries neither. Both are disabled, so nothing waits on
   them. `mock` reaches no internet and stays.
7. **`claude-code-large` and `claude-code-lightpanda`, born here** (contract
   requests 113 and 127). Each is `claude-code` with one thing changed (the
   `large` class; the Lightpanda image), so each carries `claude-code`'s
   reasons and costs above. Neither ever had a Cloud Run Job, so neither has
   a rollback to one: rolling either back means withdrawing the profile, or
   for `claude-code-lightpanda` setting `runner_profiles.claude-code-lightpanda`
   to 0 in the environment's `pool_limits` so nothing new is leased.

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
  is the WHOLE pod's local disk, because kubelet adds the workspace, cache,
  `/tmp` and HOME emptyDirs together and evicts the pod (no checkpoint, no
  park) when their total passes it. On Cloud Run only the workspace is a capped
  tmpfs, and `/tmp` (where the worker builds checkpoint archives of up to
  2 GiB) and HOME are outside that cap. So a `standard` pod on GKE gets 10 GiB,
  split 5 for the workspace + 3 for `/tmp` + 2 for HOME (CACHE-PVC,
  2026-10-15; it was 5 + 2 cache + 2 `/tmp` + 1 under CACHE-SCRATCH the same
  day, 5 + 4 `/tmp` + 1 under #893, and 4 + 2 + 4 under contract request 53).
  10 GiB is also the most Autopilot accepts for a general-purpose pod.
  `browser` gets 10 GiB: 4 workspace + 3 `/tmp` + 3 HOME (CACHE-PVC; the
  owner's 4 + 2 + 2 of 2026-10-07 plus the cache's 2 GiB, given back one each
  to `/tmp` and HOME once the cache left the local disk). `/tmp` must hold a
  2 GiB checkpoint archive on every class. Its 8 GiB before the cache was also
  its `disk_gib` until contract request 79 made `disk_gib` 4; `disk_gib` sizes
  Cloud Run's memory tmpfs, and these GKE volumes are disk outside the memory
  limit, so the request left the layout alone. Before 2026-10-07 its volumes
  were 8 + 2 + 4 = 14 GiB under an 8 GiB limit, so the pod could be evicted
  before any one volume was full. The per-class layout is `GkeDisk`
  in `scheduler/dispatch.py`, and a unit test holds every class to
  sum(sizeLimits) <= ephemeral-storage <= 10 GiB.
  A step evicted for a full volume now has that volume's largest paths in
  its reason (`evicted: EmptyDir volume "tmp" exceeds 3Gi; largest at
  10:41:12Z: /tmp/pgdata 2.1 GiB, ...`), and so in the agent's last_error and
  the attempt's error. They come from the worker's last scratch reading
  (`reconciler/detect.py` `scratch_report`, issue #2436), which is at most one
  heartbeat old, because kubelet evicts with no grace period and the worker
  cannot report at the moment of eviction. Big scratch data -- databases,
  test logs, unpacked browsers, downloads -- belongs under `$TMPDIR`, on the
  cache volume below, and not under literal `/tmp` or the workspace;
* a `cache` volume of its own, mounted at `/cache` and named to the worker
  as `SWARM_CACHE_ROOT`, for the agent's tool caches and TMPDIR: since
  CACHE-PVC a 20 GiB pd-balanced network disk (a generic ephemeral volume,
  created and deleted with the pod), not local disk and not ephemeral-storage,
  because the 2 GiB emptyDir it was evicted twice and the 10 GiB cap had no
  more to give it. Its attach adds start-up time, unmeasured as of the change
  ([browser-startup-speed.md](browser-startup-speed.md) §11.1) -- see
  [worker-images.md](worker-images.md#where-an-agents-caches-go-cache-scratch);
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

### The live browser's display sidecar

A pod of the one `live_browser` profile, `claude-code-browser` (contract
request 71), is the only pod with two containers
([`docs/design/live-browser.md`](design/live-browser.md) §3.1, §8.2, §8.3;
lane L5 of #1030). The second, `display`, runs Xvfb, the platform's headed
Chromium and the live gate, which a person in the console watches through and
takes control with. `GkeJobDispatcher._manifest` builds every pod as above and
then, for that profile only, hands it to `with_live_display`
(`apps/scheduler/scheduler/dispatch.py`), so any other profile's pod is the
object it always was; a unit test holds that byte for byte.

**It is rendered only while `LIVE_BROWSER_SIDECAR=true`** on the scheduler
(`SchedulerSettings.live_browser_sidecar`, default off). The sidecar runs the
gate, `python -m agent_worker.livegate`, which lanes L1-L4 build. A native
sidecar whose entrypoint does not exist yet restarts forever, and every
`claude-code-browser` agent would pay for it. Off, `claude-code-browser` keeps
the single-container pod and the agent launches its own headless Chromium, as
before. Turn it on once the dispatched image carries the gate.

What the switch adds, and why each piece is shaped this way:

* **A Kubernetes native sidecar**: an init container with
  `restartPolicy: Always`. A Job's pod completes when its containers exit, so
  an ordinary second container that keeps running would hold every attempt
  open. A native sidecar starts before the worker and stops after it. Its
  startup probe (`test -S /run/swarm-gate/ctl.sock`) keeps the worker from
  starting until the gate listens. The worker's first registration on that
  socket has to come before the agent exists (§5.4.2). GKE's support for
  native sidecars on `swarm-autopilot` is assumed from current Autopilot
  versions and has not been measured here (§11).
* **Its own uid, 10002 (`swarm-display`)**, under the same image and every
  other field of the worker's hardening (non-root, no privilege escalation,
  all capabilities dropped, seccomp `RuntimeDefault`, read-only root). Inside
  one pod, a separate uid is the only boundary available without gVisor. The
  agent runs as 10001 in the worker container. The X socket (in the
  sidecar's own memory `/tmp`) and the browser profile (cookies) are on
  volumes the worker container does not mount. The pod's `fsGroup` (10001) is
  a supplementary group of both containers. That is how the two exchange
  files on the shared volumes. It opens nothing else, because a volume that
  is not mounted cannot be reached whatever its mode bits say.
* **The class is split, not grown.** Each container has `requests == limits`
  (invariant 7), and together they sum to the task's class. Admission's
  units, the pool reservation and the Autopilot bill are therefore what they
  were. For `browser` (4 vCPU / 8 GiB since contract request 79), the worker
  gets 1 vCPU / 3 GiB and the sidecar 3 vCPU / 5 GiB, sized from the
  measurement in [browser-startup-speed.md §11](browser-startup-speed.md):
  the sidecar peaked at 1.90 cores and 1.48 GiB, the worker at 0.55 cores
  and 0.37 GiB. The sidecar's resident tmpfs (`/dev/shm` 2, profile 1, `/tmp`
  0.25 GiB) plus that peak is 4.73 of its 5 GiB, so a page heavier than any
  sampled, or a download in flight at the peak, is an OOM kill; the periodic
  checkpoint (invariant 8) bounds what it costs. It was the design's starting
  split, worker 3 / 6 and sidecar 5 / 10 of 8 / 16. A step shrunk to
  `standard` splits 2/4 and 2/4.
  Ephemeral storage gives the sidecar 1 GiB (its logs, since every volume it
  mounts is memory) and the worker the rest, so the pod's sum is still the
  `GkeDisk` total. The per-class table is `LIVE_DISPLAY_SHARE`, and a class
  without a row is refused at dispatch.
* **`/dev/shm` moves to the sidecar**, because that is where Chromium runs.
  It is the same 2 GiB memory `emptyDir`.
* **Three shared memory volumes, and only these**: `live-gate`
  (`/run/swarm-gate`, `ctl.sock`), `live-downloads` (the sidecar writes, the
  worker moves files into `work/`) and `live-uploads` (the worker writes, the
  sidecar mounts it read-only). None is under `/workspace`, so none is
  checkpointed. The sidecar's private volumes are `display-tmp` and
  `browser-profile`. All live volumes are memory, sized inside the container
  that fills them.
* **The label `swarm-live: enabled`** on the pod. The live egress policy
  (§8.3, lane L6) selects it, so only a live-browser pod can reach the relay
  on TCP 8443.
* **Paths in the environment, and nothing else.** The worker gets
  `SWARM_GATE_CTL`, `SWARM_CDP_ENDPOINT` (`http://127.0.0.1:9222`, the gate's
  CDP proxy) and the two shared directories, but **no `DISPLAY`**. A headed
  launch in the worker container therefore fails loudly instead of drawing
  where nobody sees. The sidecar gets those paths, its profile directory and
  `HOME=/tmp`. It gets no agent, tenant or attempt identifier and no secret,
  because the tunnel ticket arrives over `ctl.sock` from the worker (§3.3).
* **The image** (`images/agent-runtime-browser/Dockerfile`) adds Xvfb, the
  `swarm-display` user and the §5.6 Chromium policy
  (`images/agent-runtime-browser/chromium-policy.json`). The policy is
  installed read-only in both `/etc/opt/chrome/policies/managed/` (Google
  Chrome's directory, the one the design names) and
  `/etc/chromium/policies/managed/` (Chromium's, which is what Playwright
  bundles). Chromium silently ignores a policy in the wrong directory.

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

A GKE attempt's `execution_name` is `<namespace>/<job>`
(`swarm-tenant-eng/swarm-918a848f969e406389fd-1`), and it is that on both
writes: the dispatcher records the string it rendered, and the worker's
`record_attempt_start` writes the same string, which the Job hands it as
`SWARM_EXECUTION_NAME`. GKE injects no Job name into a pod, so before #1804 a
GKE worker recorded its pod's HOSTNAME (`swarm-<task>-<gen>-<5 chars>`, no
namespace) over the dispatcher's record; the gate then asked the cluster for a
Job of the pod's name and called live agents gone, and cancel's probe could not
read the name at all. A pod name is now a **legacy** form: it is mapped to its
Job, `swarm-<task>-<gen>` in the tenant's namespace, only when it is exactly
that attempt's Job or pod (by task id and generation), and anything else stays
unreadable or unchecked -- never "gone", which would release a slot a live
agent holds. The gate's [10] (`scripts/e2e-test.sh` `capacity_holder_verdict`)
always maps it; the reconciler's `GkeBackend.probe` and `namespace_of` map it
when their caller passes the attempt's `task_id`, `tenant_id` and generation, and a
mapped Job must still be a platform-managed Job of that agent and generation.

Any failure in that sequence releases the lease and returns the agent to `READY`.
A slot held by a container that will never exist is a permanent capacity leak
until the reconciler notices; releasing immediately is free.

The reconciler covers what dispatch cannot:

| Disagreement | Cost if ignored |
|---|---|
| stale lease | capacity leaks; the platform slowly stops admitting |
| missing execution | the agent is stuck forever holding a slot |
| orphan execution | money, plus a second agent on someone's repository |
| orphan lease | capacity leaks |

Note what is **not** a finding: an agent in `LEASED` with a fresh lease and no
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
