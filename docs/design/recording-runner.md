# A recording runner: 16 dedicated cores, 64 GiB, 24 hours, Docker, no agent

**Status: design only, waiting for the owner. Nothing is built.** The owner's
decision on the issue (part of #8) was "design first": this document and
contract request 159 in
[contract-change-requests.md](../contract-change-requests.md) are the whole of
the pull request that adds them. No runtime code, Terraform or image changes.
Section 11 holds the questions the owner answers before any lane in section 10
starts; the same questions are filed with the pull request as `questions.json`.

Every file and symbol cited below was read on 2026-11-24 at `0f256b6`. Lines
move, so the citations name the symbol; search for it.

## 1. What is asked, and why it does not fit today

A tenant records release-gate evidence: a fixed list of commands, run end to
end, that writes evidence files. One step runs about 500 test suites back to
back, each under its own fixed 600-second limit, for 10 to 18 hours. On 8
shared cores the suites hit that limit, so the step cannot finish in today's
largest profile, and the work stays serial on one machine. The issue asks for a
profile picked by name with:

| | Asked | Largest today |
|---|---|---|
| Cores | 16 dedicated (at least 14) | 8 requested, 16 limit (`large`) |
| Memory | 64 GiB | 32 GiB |
| Time per task | 86,400 s | 14,400 s (`claude-code-large`) |
| Docker daemon | yes | none |
| Agent | none | yes, except `generic` |
| Concurrency | four tasks at once | n/a |

and the same clone and artifact upload as `claude-code`.

The point of "dedicated" matters for the design: the suites are each under a
wall-clock limit, so the profile must give *predictable* CPU, not burst CPU. A
burst is throttled under contention (CONTRACT.md invariant 7), which is exactly
the failure the issue describes on shared cores.

## 2. What exists today

### 2.1 The largest profile

`claude-code-large` (`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`)
runs the claude-code agent on the `large` class on `Backend.GKE_AUTOPILOT`,
with `timeout_seconds=14400`. Its Terraform mirror
(`terraform/infra/locals.tf`, `runner_profiles`) repeats the 14,400.

### 2.2 The generic profile

`generic` (`profiles.py::_GENERIC_PROFILE`) is the only profile that starts no
agent and no provider (`provider=None`). It runs on `standard` (4 CPU, 8 GiB),
on GKE Autopilot since contract request 64, at the `RunnerProfile` default
`timeout_seconds` of 3,600. Its one required input, `command`, must be a name
in `_GENERIC_COMMANDS` (`make`, `npm-build`, `npm-ci`, `npm-test`, `pytest`,
`uv-sync`); `RunnerProfile.__post_init__` holds the profile's declared
`choices` equal to that tuple, swarm-api checks a step's input against the
declaration (`profiles.py::check_inputs`, called from
`apps/swarm-api/swarm_api/validation.py`), and the worker re-checks the name
(`apps/agent-worker/agent_worker/runners/generic.py`, `GENERIC_COMMANDS`).
The module docstring of `generic.py` explains why: an earlier version took a
caller's argv and script, and "that is an authenticated remote shell, however
the allowlist is phrased". **That reasoning is the constraint this design
keeps** (section 7).

### 2.3 The resource classes

`profiles.py::RESOURCE_CLASSES`:

| Class | CPU request | CPU limit | Memory GiB | `disk_gib` | Units |
|---|---|---|---|---|---|
| standard | 4 | 8 | 8 | 4 | 1 |
| browser | 4 | 8 | 8 | 4 | 1 |
| medium | 6 | 12 | 16 | 8 | 2 |
| builder | 8 | 12 | 16 | 8 | 2 |
| large | 8 | 16 | 32 | 16 | 4 |

Where each bound is enforced:

* `profiles.py::ResourceClass.__post_init__` refuses any class whose `cpu` is
  above 8 or `memory_gib` above 32 ("exceeds the Cloud Run Jobs ceiling"), and
  any `cpu_limit` outside `cpu .. CPU_BURST_MAX_RATIO × cpu`
  (`CPU_BURST_MAX_RATIO = 2`). It does so for every class, including ones only
  a GKE profile uses. CONTRACT.md's platform decisions say the same: "No GPU or
  >32 GiB profile exists: `ResourceClass` refuses one at import."
* The admin-editable size bounds `CPU_REQUEST_MAX = 8` and
  `MEMORY_GIB_MAX = 32` (`profiles.py`) are Cloud Run's ceiling, and the
  comment beside them says why: "every class can be reached by a Cloud Run
  profile's step or fallback Job". `profiles.py::check_size` judges every
  class in `CLASS_ORDER` against them.
* `profiles.py::units_for` charges one unit per 4 requested cores or 8 GiB,
  whichever is more, and an import-time loop holds every class's `units` to
  it.
* On GKE, the pod's ephemeral storage is not `disk_gib`: it is the class's row
  in `apps/scheduler/scheduler/dispatch.py::GKE_DISK` (10 GiB for every class),
  bounded by `AUTOPILOT_MAX_EPHEMERAL_GIB = 10`, Autopilot's general-purpose
  ceiling on a pod's ephemeral-storage request; `dispatch.py::gke_disk` refuses
  a class with no row.
* `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest` renders
  memory and ephemeral storage with request == limit and CPU at the class's
  request and limit (invariant 7).

### 2.4 The per-task time limit

* swarm-api's request schema caps `timeout_seconds` at 86,400
  (`apps/swarm-api/swarm_api/schemas.py`, `le=86_400`).
* `apps/swarm-api/swarm_api/validation.py::validate_timeout` returns the
  smaller of the request and `profile.timeout_seconds`, so **the profile is the
  ceiling**, and none is above 14,400.
* The frozen contract has no timeout bound of its own: `RunnerProfile`
  declares `timeout_seconds: int = 3600` and nothing caps it.
* The backend's deadline is the task timeout plus the dispatch timeout plus
  `WORKER_FINALISE_BUDGET_SECONDS = 300`
  (`apps/scheduler/scheduler/dispatch.py::backend_deadline_seconds`). For
  86,400 s with the default 480 s dispatch timeout that is 87,180 s.
* The worker enforces the task timeout itself (the comment above
  `WORKER_FINALISE_BUDGET_SECONDS` in `dispatch.py`), and Autopilot's extended
  run time, which this platform requests with
  `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`
  (`GkeJobDispatcher._manifest`), protects a pod from scale-down and node
  auto-upgrade for up to seven days (docs/execution-backends.md). 24 hours fits.

### 2.5 Pod security today

* `dispatch.py::POD_SECURITY_CONTEXT`: `runAsNonRoot`, the worker uid,
  `seccompProfile: RuntimeDefault`.
* `dispatch.py::CONTAINER_SECURITY_CONTEXT`: no privilege, no privilege
  escalation, read-only root filesystem, `capabilities: {drop: [ALL]}`.
* Tenant namespaces hold Pod Security Admission at `enforce: baseline`
  (`kubernetes/namespaces/tenant-namespace.yaml`, explained above
  `POD_SECURITY_CONTEXT`).
* docs/security.md: "Nothing talks to a container runtime — no Docker socket is
  ever mounted. An agent that could reach one would escape every boundary
  above it." `images/agent-runtime-base/Dockerfile` repeats it: the `docker`
  CLI is in the image "with no daemon and no socket behind it; image builds go
  to Cloud Build".
* docs/BUILD_PROMPT_V2.md measured GKE Sandbox (gVisor) on this platform's
  Autopilot cluster: it schedules ("Kernel confirmed in effect:
  `4.19.0-gvisor`"), costs 2.5× on a file-work micro-benchmark ("~15–25% on
  bulk I/O, 5–8× on metadata-heavy traversal"), and Kata (`microvm`) "is
  advertised by the cluster but will not schedule". The same document states
  "Docker-in-pod does not work under gVisor"; it gives no measurement for
  that sentence.

### 2.6 Capacity

* A task acquires `global`, `tenant:<id>`, `resource:<class>`,
  `runner:<profile>` and `backend:<backend>`, plus two provider pools when its
  profile has a provider (`apps/common/swarm_common/models.py::pool_names_for`).
* All of them are read, checked and incremented in one Firestore transaction
  (`apps/common/swarm_common/admission.py::acquire_lease_in_transaction`, with
  the pure check in `admission.py::evaluate_capacity`); a missing pool document
  is unlimited.
* A compute pool charges the class's units; a provider pool charges 1
  (`models.py::PROVIDER_POOL_UNITS`).
* Concurrency counts from LEASED: `CONCURRENCY_STATES` in
  `apps/common/swarm_common/states.py`.
* Terraform makes the pool documents from `var.pool_limits`
  (`terraform/infra/variables.tf`), with a profile that has no entry falling
  back to the global limit (`terraform/infra/locals.tf`, `pool_runner_profiles`).
  The example production values
  (`terraform/environments/prod/prod.tfvars.example`) are `global = 100`,
  `default_tenant = 20`, `large = 8`, `GKE_AUTOPILOT = 20`. A deployment's
  live values are its own and were not read for this document.

### 2.7 Tenant isolation, and who may use a profile

* Service account: `dispatch.py::assert_tenant_identity` derives it from the
  tenant id and refuses rather than fall back to a shared one.
* Namespace: `GkeJobDispatcher.namespace_for`, `swarm-tenant-<tenant>`; the
  Kubernetes service account is bound to the tenant's Google service account
  by Workload Identity (`GkeJobDispatcher.ksa_for`).
* Storage prefix and secrets: `tenants/<id>/` with an IAM condition on the
  prefix, and one accessor per secret (docs/multi-tenancy.md, the attack table).
* Network: a default-deny NetworkPolicy per namespace with a worker egress
  allowance (`kubernetes/network-policies/allow-egress.yaml`).
* The workload's metadata token is reachable from inside the pod by design,
  and is made worth little: it is the tenant's own account (docs/security.md,
  "Cloud metadata abuse").
* **There is no per-tenant profile allow-list.** A profile is on or off for
  everybody (`RunnerProfile.available`, `disabled_reason`), a provider
  profile needs the tenant's credentials, and a `provider=None` profile such as
  `generic` is open to every tenant. The `allowed_profiles` names that exist
  are on repositories (`apps/swarm-api/swarm_api/repositories.py`, "not yet a
  submission check") and MCP servers, not tenants.

## 3. Why no existing profile can be stretched

* **Bigger `large`:** 16 CPU and 64 GiB are above the Cloud Run ceiling that
  `ResourceClass.__post_init__` applies to every class, and resizing `large`
  through the admin route is bounded by `CPU_REQUEST_MAX` and `MEMORY_GIB_MAX`.
  Either way it is a frozen-contract change, and resizing `large` would also
  move every `claude-code-large` task.
* **Longer `generic`:** `generic` runs a named catalogue command, not a list,
  on 4 CPU, and changing its timeout or class changes it for every tenant.
* **The CPU limit of `large` is already 16**, but that is burst, not
  dedicated: Autopilot bills and guarantees the 8-core request, and a burst is
  throttled under contention (invariant 7). The issue's failure is a
  contention failure.

So: a new class above the Cloud Run ceiling, GKE only; a new profile; and a
container daemon, which no profile has.

## 4. Where it runs

Two backends can hold 16 cores and 64 GiB for 24 hours:

* **GKE Autopilot**, today's GKE backend, with a new class and a pod
  `request == limit == 16` CPU. With CPU request equal to limit and memory
  request equal to limit, the pod's QoS class is Guaranteed, so its 16 cores
  are reserved, not burst. Autopilot's per-pod maximum on the general-purpose
  compute class is **not in this repository**; the spike (lane 0) reads it.
  "Dedicated physical cores" in the stronger sense (no neighbour on the node)
  is Autopilot's Performance compute class, which this platform does not use
  (docs/design/runner-sizes.md: "Compute class: none"); whether GKE Sandbox
  runs on it is not in this repository either.
* **A new Compute Engine backend**: one on-demand VM per task. Not a backend
  this platform has (`profiles.py`, `Backend` has `CLOUD_RUN_JOB`,
  `GKE_AUTOPILOT` and `AUTO`).

Cloud Run Jobs cannot: "a hard ceiling of 8 vCPU and 32 GiB per execution"
(docs/execution-backends.md).

The choice is driven by the Docker daemon (section 5), so it is made there.

## 5. The Docker daemon

### 5.1 What the commands need

"Docker available to those commands for container checks": the recording's
commands start containers (`docker run`, `docker compose`, building small test
images). That needs a running daemon, a writable image store, and network to
pull images. It does **not** need the node's container runtime, and must never
get it: docs/security.md's sentence about the Docker socket is about exactly
that, and stays true under every option below except A.

### 5.2 The options

**A. A privileged Docker-in-Docker sidecar.** The standard answer elsewhere.

* Cost: smallest build.
* Risk: a privileged container is root on the node, shared with other tenants'
  pods; it breaks `CONTAINER_SECURITY_CONTEXT`, Pod Security Admission
  `baseline` refuses it, and Autopilot refuses privileged pods outside its own
  allowlist.
* **Rejected.** It cannot be admitted, and if it could, it would be the escape
  docs/security.md describes.

**B. A rootless daemon in the worker pod, under the normal runtime (runc).**

* Cost: a new image with rootless `dockerd`, `newuidmap` and a storage driver;
  no new backend.
* Risk: rootless Docker needs user namespaces (`unshare`), setuid helpers
  (so `allowPrivilegeEscalation: true`), and in practice a seccomp and
  AppArmor profile looser than `RuntimeDefault`. `CONTAINER_SECURITY_CONTEXT`
  forbids escalation; PSA `baseline` forbids an `Unconfined` seccomp profile.
  And it runs the inner containers on the **node's shared kernel**: the
  isolation between this tenant's containers and another tenant's pod is the
  kernel, through a much wider syscall surface than today's.
* **Not recommended.** It weakens the pod posture every other profile has,
  for the profile that runs the most untrusted work.

**B′. B as a sidecar container in the same pod.** Same constraints as B: a
sidecar changes the lifecycle (the daemon is not the worker's child) but not
what the daemon needs from the kernel. Not separately viable.

**C. The worker pod in a sandboxed runtime (GKE Sandbox, gVisor), with
`dockerd` inside the sandbox.**

* How it runs: the pod sets `runtimeClassName: gvisor`; the worker starts
  `dockerd` as a child process inside the pod, its socket on a pod-local
  `emptyDir`, and the recording's commands reach it with `DOCKER_HOST`. Root
  and capabilities exist only inside the sandbox's user-space kernel; the
  node's kernel sees one sandbox process, as for any gVisor pod. The node's
  container runtime is never visible.
* What it reaches: everything the inner containers send leaves through the
  pod's own network interface, so Kubernetes sees only the pod's IP and the
  tenant namespace's NetworkPolicy applies to all of it. They can reach the
  internet as far as the egress policy allows (image pulls), the pod's own
  metadata server path (as the worker can today; section 5.4), and nothing in
  another namespace.
* Cost: a new image (`recording-runtime`); the dispatcher renders the runtime
  class and a different security context for this profile only; Docker's image
  store needs a volume larger than Autopilot's 10 GiB ephemeral-storage cap
  (section 5.3).
* Risks, each unmeasured here and each a spike check:
  1. **Admission.** Whether Autopilot admits a GKE Sandbox pod with the
     capabilities `dockerd` needs inside the sandbox, under PSA `baseline`.
     If it does not, option C is closed.
  2. **It works.** docs/BUILD_PROMPT_V2.md says Docker-in-pod does not work
     under gVisor, without a measurement; gVisor's own project documents
     running Docker inside a sandbox with root in the sandbox. The spike starts
     `dockerd` and runs a container.
  3. **Speed.** The measured gVisor penalty (2.5× on file work, 5–8× on
     metadata-heavy traversal) lands on test suites that each have a 600 s
     limit. The issue says 8 shared cores already miss that limit. The spike
     runs one representative suite sandboxed and not, and C passes only if the
     sandboxed run keeps a margin under 600 s.

**D. A new backend: one Compute Engine VM per task.**

* How it runs: the dispatcher creates an on-demand VM from a platform-owned
  instance template and image; the worker runs on it as on a pod, and starts
  `dockerd` natively. The VM is deleted when the attempt ends.
* What it reaches: the VM runs as the tenant's own service account (the same
  identity Workload Identity gives the pod today). Network isolation is no
  longer a Kubernetes namespace: it would be a per-tenant firewall rule set on
  the VM's network tag, or a separate subnet.
* Cost: the largest build in this document. A new `Backend` value; a new
  dispatcher, reconciler path and fencing path; Terraform for the template,
  the image pipeline, firewall and IAM; and a VM image with the worker. The
  machine itself is also billed at Compute Engine rates, which are not in this
  repository.
* Strengths: a hardware-virtualised boundary (stronger than gVisor), native
  Docker speed, real dedicated cores, and a boot disk of any size, so no
  ephemeral-storage cap.
* Risks: **invariant 9's "own namespace" has no literal meaning on a VM**;
  the owner would have to accept the firewall rule as its replacement. Host
  maintenance is handled by live migration on on-demand VMs, which is
  Compute Engine's documented default but not something this repository
  records.

**E. A remote builder (Cloud Build), as for this platform's own images.**
Cloud Build builds images; it does not give a running daemon that a test
suite can start containers against for the length of a suite. **Does not
meet the requirement.**

### 5.3 The image store

Docker's image store is the bulk of what the daemon writes. On Autopilot
general-purpose a pod's ephemeral storage stops at 10 GiB
(`dispatch.py::AUTOPILOT_MAX_EPHEMERAL_GIB`), and the worker already uses all
10 for workspace, `/tmp` and HOME (`dispatch.py::GKE_DISK`). Two places to put
it under option C:

* **A per-pod generic ephemeral volume (a persistent-disk claim)**, created
  with the pod in the tenant's namespace and deleted with it, as the cache's
  network disk already is a claim and "not ephemeral-storage"
  (`dispatch.py::GkeDisk`'s docstring, `CACHE_VOLUME_GIB`). Recommended: its
  size is fixed by the claim, so there is no request/limit gap for invariant 7
  to forbid, and it is the tenant's own, in the tenant's namespace.
* **Memory-backed (tmpfs)**, charged to the 64 GiB. Simple, but it competes
  with the suites for memory, which is the resource they need.

Under option D the VM's boot or data disk holds it.

### 5.4 Per-tenant isolation (invariant 9) with a daemon in the pod

| Element | How it is kept under option C |
|---|---|
| Own service account | The pod runs as the tenant's Kubernetes service account bound to the tenant's Google account, from `assert_tenant_identity`, unchanged. The inner containers can reach the metadata path the pod can, as the worker's own child processes can today; what they get is the tenant's own token (docs/security.md, "Cloud metadata abuse"). As defence in depth, the daemon's bridge network drops traffic to the metadata address; a container started with host networking shares the pod's network and is not stopped by that rule, which is the same exposure the commands already have. |
| Own secrets | The profile has no provider and no secrets of its own. The forge token is handled by the worker's clone exactly as for `generic`; the recording's commands never receive more than `generic`'s commands do. |
| Own storage prefix | Checkpoints and artifacts go to `tenants/<id>/...` under the tenant's own account, as for every profile. The image store is a disk in the tenant's namespace, deleted with the pod. |
| Own namespace | The pod is in `swarm-tenant-<tenant>`, under its NetworkPolicy; the inner containers are inside the pod's network namespace and the sandbox, so they cannot be addressed from, or reach, another namespace except as the pod can. |

Under option D the first three are the same; the fourth becomes a firewall
rule per tenant, as said above.

### 5.5 Recommendation

**Option C, behind a spike, with D as the fallback.** C keeps every isolation
mechanism the platform has, literally (namespace, NetworkPolicy, Workload
Identity, the dispatcher, the fencing and reconciler paths), and puts root only
inside a user-space kernel; its build is one new runtime class, one image and
one runner. Its risks are concrete and cheap to test: whether Autopilot admits
the pod, whether `dockerd` runs, and whether the suites keep their 600 s
margin under the gVisor penalty. If any of the three fails, D is the answer,
and contract request 159 is amended before it is applied (it says how).

The one argument for D first: the issue's own failure is a speed failure, and
gVisor's measured penalty is largest on exactly the metadata-heavy work test
suites do. The spike settles it with a number; this document does not guess
it.

## 6. The CONTRACT.md invariants

**1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Unchanged: a
queued recording task is a Firestore document, not a pending pod or VM. Under
D, the VM is created at dispatch, never before.

**2. Capacity all-or-nothing, in one transaction.** The task adds
`resource:xlarge` and `runner:recording` to `pool_names_for`'s list, and
`acquire_lease_in_transaction` reserves them with the others in the same one
transaction. Nothing new reserves anything outside it.

**3. Concurrency counts from LEASED.** A recording task charges 8 units
(`units_for(16, 64)`) in every compute pool from the moment it is LEASED.
Four at once is 32 units in each of `global`, `tenant:<id>`,
`resource:xlarge`, `runner:recording` and `backend:GKE_AUTOPILOT` (section 9
sets the limits).

**4. No sleeping through a provider wait.** No provider: not touched.

**5. Fencing.** Unchanged: the worker's lifecycle checks its generation before
the runner starts and exits fenced without running it
(`apps/agent-worker/agent_worker/lifecycle.py::Worker._exit_fenced`). The
recording runner is a child of the lifecycle like every runner, and also
re-checks before each command of the plan, so a stale attempt stops within one
command.

**6. No Spot.** `spot` stays `ON_DEMAND_ONLY`, which
`RunnerProfile.__post_init__` enforces. Under D, the VM is on-demand.

**7. Memory and ephemeral storage request == limit.** Memory 64 GiB request
and limit; ephemeral storage the `GKE_DISK` row's sum, request and limit; CPU
request == limit == 16 (no burst, so the pod is Guaranteed). The image store
is a persistent-disk claim of a fixed size, not ephemeral storage (5.3).

**8. Mandatory periodic checkpointing.** What a 24-hour job with no agent
checkpoints:

* The worker's periodic checkpoint runs for every runner: the lifecycle calls
  `self._checkpoint("periodic")` from `Worker`'s main loop
  (`apps/agent-worker/agent_worker/lifecycle.py`), every
  `checkpoint_interval_seconds` (120 by default, `RunnerProfile`). It archives
  the attempt's `work/` tree to `tenants/<tenant>/tasks/<task>/attempts/
  <attempt>/checkpoints/...` (docs/checkpointing.md §1), up to
  `max_checkpoint_bytes`, 2 GiB (`apps/agent-worker/agent_worker/config.py`).
* `artifacts/` is **not** checkpointed (docs/checkpointing.md §1): it is
  uploaded on exit. So the recording runner writes evidence under `work/`
  while it runs, and copies it to `artifacts/` at the end.
* The runner keeps a **journal** in `work/`: for each command of the plan, its
  index, start and end time, exit code and the digests of the evidence it
  wrote. The journal is what makes a checkpoint useful: a retry restores
  `work/`, reads the journal, skips every command marked done, and runs the
  interrupted one again from its start. So an interruption loses at most one
  command plus one interval, not the 24 hours.
* **Evidence is bounded below the checkpoint cap.** `work/` is memory-backed
  tmpfs (`checkpoint.py`), so evidence in `work/` is charged to the same 64 GiB
  the suites need, and `checkpoint.py` refuses a checkpoint over
  `max_checkpoint_bytes` (2 GiB), after which every later periodic checkpoint
  would fail and invariant 8 would stop holding for the rest of the job. The
  plan format therefore caps the total evidence kept in `work/` (journal
  included) at 1 GiB, half of the checkpoint cap. Larger evidence (full logs,
  recordings) goes to GCS per command, under the tenant's prefix, and `work/`
  keeps only its digests in the journal. The runner checks the cap after each
  command; when a command's evidence would exceed it, the runner **fails the
  task loudly** (non-zero exit, reason in the status) rather than continue
  without checkpoints. A failed periodic checkpoint is never skipped silently.
* What is not checkpointed: the Docker image store and running containers. A
  retry pulls images again. A plan's command must therefore be restartable
  from its start, which the plan format states (section 7).
* Node upgrades and scale-down are held off by extended run time for up to
  seven days (section 2.4). What still ends a pod (node repair, memory
  pressure, a reconciler reclaim, a cancel) ends at the last checkpoint, which
  is the point of invariant 8 and the reason it is not relaxed here.

**9. Per-tenant isolation.** Section 5.4.

**10. Callers pick a profile by name.** The caller sends `recording` and a
plan name. No image, argv, resources or backend parameter (section 7).

## 7. The command list, and invariant 10

The issue asks for "the task's input being the command list to run". Taken
literally, that is a caller-supplied argv, which invariant 10 forbids and which
`generic.py`'s docstring calls an authenticated remote shell. Three shapes:

1. **The caller sends the list.** An explicit exception to invariant 10, for
   one profile. Simple for the caller; it turns the profile into a 24-hour,
   16-core remote shell with a container daemon, holding the tenant's identity.
2. **A named plan stored in the repository.** The caller sends
   `{"plan": "release-gate"}`; the runner reads
   `.swarm/recordings/release-gate.json` from the repository at the pinned
   commit. The file lists the commands (argv arrays, no shell), each one's
   wall-clock limit, the evidence paths it writes, and whether it needs
   Docker. This is the trust boundary `generic` already has: `npm test` runs
   whatever the cloned repository's `package.json` says, and the platform
   chose to run it. The caller names data; the repository's own reviewed
   history owns the commands.
3. **A plan registered with the platform by an admin.** Stronger review, but
   every change to a recording needs an admin action, and the platform stores
   tenant commands.

**Recommended: 2.** It keeps invariant 10 without an exception, and the
recording is versioned with the code it records. Its bounds are the
platform's: the plan's total and per-command limits may only be lower than
the profile's, and the plan's paths must resolve inside the workspace (the
rules `generic.py` already applies to `paths` and `working_directory`).

## 8. Cost

The repository's own rates are Autopilot general-purpose list prices in
`us-central1`: $0.0445 per vCPU-hour, $0.0049225 per GiB-hour, and
$0.0000548 per GiB-hour of ephemeral storage (docs/browser-startup-speed.md,
"Cost at list price"; the same vCPU and memory figures are
`apps/swarm-api/swarm_api/runnersizes.py::PRICES`). That document says the
vCPU and memory rates were confirmed on 2026-10-13 only through secondary
sources quoting Google's list. Autopilot bills requests, so with request ==
limit the arithmetic is exact at those rates:

| Item | Per hour | 24 hours |
|---|---|---|
| 16 vCPU × $0.0445 | $0.7120 | $17.088 |
| 64 GiB × $0.0049225 | $0.31504 | $7.561 |
| 10 GiB ephemeral × $0.0000548 | $0.00055 | $0.013 |
| **One task** | **$1.0276** | **$24.66** |
| Four at once | $4.110 | $98.65 |

A 10-hour step is about $10.28 and an 18-hour step about $18.50 per task. If
four ran continuously for a 730-hour month: about $3,000.

**Not in the repository, so not priced here:**

* GKE Sandbox (gVisor) surcharge, if any;
* the Performance compute class, if "dedicated" is read as one pod per node;
* the persistent-disk claim for the image store (section 5.3);
* network egress for image pulls through NAT;
* Compute Engine VM rates (option D);
* whether the project's regional CPU quota has 64 more on-demand vCPUs free.

The spike lane reads each from the provider's pricing and the project's
quota, and records it beside the numbers above.

## 9. Who may use it, and its pool limits

### 9.1 Which tenants, behind which switch

There is no per-tenant profile allow-list today (section 2.7), and a
`provider=None` profile is open to every tenant. A 16-core, 24-hour profile
with a daemon must not be. Options:

* **a. A `Tenant` field in the frozen contract** (`allowed_profiles` on
  `Tenant` in `models.py`). Durable and visible, but a second contract change,
  and a field the tenant's worker can rewrite until the tenant-settings
  integrity work closes that (docs/multi-tenancy.md, "Have work dispatched
  under another tenant's identity").
* **b. A deployment setting: a Terraform variable `recording_tenants`
  (default empty) passed to swarm-api and the scheduler.** swarm-api refuses
  `recording` at submit for a tenant not in it, and the scheduler refuses it
  again at admission (a task already queued when a tenant is removed is
  refused, not leased). No contract change; changed by the owner through a
  reviewed Terraform change.
* **c. `available=False` only, turned on for everybody when ready.** No
  per-tenant control at all.

**Recommended: b**, with the profile shipped `available=False` (contract
request 159) so that turning it on is a second deliberate step. The first
tenant is the one that asked; nobody else until the owner adds them.

### 9.2 The pool limits

One task is 8 units. Four at once is 32 units in every compute pool it
touches, so:

* `runner:recording` hard limit **32** (four tasks), in
  `pool_limits.runner_profiles`;
* `resource:xlarge` hard limit **32**, in `pool_limits.resource_classes`;
* the allowed tenant's `tenant:<id>` pool must have 32 free for the four. At
  the example's `default_tenant = 20`, only two would ever lease, and they
  would take the tenant's whole budget from its other work;
* `backend:GKE_AUTOPILOT` must have 32 free beside the other GKE profiles. At
  the example's `GKE_AUTOPILOT = 20`, the same two-task stop applies, and the
  recordings would starve every other GKE profile while they ran.

The recommendation (question Q5): the runner and class pools at 32; the
allowed tenant's own pool raised by 32 above its present limit, so the
recordings do not take its other work's budget; the GKE backend pool raised
by 32. Every raise is a Terraform change the owner reviews, and the operator
can still set `runner:recording` to 0 at any time through
`PUT /v1/admin/limits/runner/{runner_profile}` (docs/security.md).

## 10. The build, once the owner accepts

Lanes in order; each names its files. Lanes 1–2 are the contract and its
mirror; lanes 3–7 can run in parallel after them; lane 8 turns it on.

| Lane | What | Files |
|---|---|---|
| 0. REC-SPIKE | Answers section 5.2 C's three checks on a dev cluster and the section 8 prices; no merge to main beyond its findings document. | `docs/design/recording-runner.md` (findings appended), a throwaway manifest under `scratchpad/` |
| 1. REC-CONTRACT | Applies contract request 159 (amended for D if the spike chose it). | `apps/common/swarm_common/profiles.py`, `docs/contract-change-requests.md`, `tests/unit/common/test_recording_profile.py` |
| 2. REC-MIRROR | The Terraform catalogue mirror and the pool limits. | `terraform/infra/locals.tf` (`resource_classes`, `runner_profiles`), `terraform/infra/variables.tf` (`recording_tenants`), `terraform/environments/*/*.tfvars.example` (`pool_limits`), `tests/terraform/catalogue_mirror/` |
| 3. REC-DISPATCH | The `xlarge` `GKE_DISK` row; `runtimeClassName`, the sandbox security context and the image-store claim for a `container_daemon` profile only; the parity test against the YAML templates. | `apps/scheduler/scheduler/dispatch.py`, `kubernetes/render.py`, `kubernetes/worker-templates/`, `tests/unit/scheduler/` |
| 4. REC-IMAGE | `recording-runtime`: the worker plus `dockerd` for gVisor; scanned like every image. | `images/recording-runtime/Dockerfile`, `scripts/build-images.sh`, `scripts/push-images.sh` |
| 5. REC-RUNNER | The runner: read and validate the plan, start `dockerd`, run commands in order with their limits, keep the journal in `work/`, resume from it, re-check the fence per command, copy evidence to `artifacts/` at the end. | `apps/agent-worker/agent_worker/runners/recording.py`, `apps/agent-worker/agent_worker/runners/__init__.py`, `tests/unit/worker/test_recording_runner.py` |
| 6. REC-GATE | The tenant gate at submit and at admission. | `apps/swarm-api/swarm_api/validation.py`, `apps/scheduler/scheduler/loop.py` and `apps/scheduler/scheduler/store.py` (the admission callers), `tests/unit/scheduler/`, `tests/unit/control_plane/` |
| 7. REC-DOCS | docs/security.md's Docker-socket paragraph amended to say what is and is not allowed; capacity pages; the user guide's plan format. | `docs/security.md`, `docs/concurrency.md`, `docs/execution-backends.md`, `docs/user-guide/` |
| 8. REC-ENABLE | `available=True`: itself a contract change (a further request, as request 151 was for `fix-loop`). | `apps/common/swarm_common/profiles.py`, `docs/contract-change-requests.md` |

Under option D, lane 3 becomes a new Compute Engine dispatcher with its
reconciler and fencing paths, and lane 4 a VM image; both are much larger, and
the spike would re-plan them.

A change to admission and dispatch also runs `make smoke concurrency-test
race-test` against a deployed environment (CLAUDE.md, "Before you say you are
finished").

## 11. Questions for the owner

Each has options and a recommendation; `questions.json` holds the same ones.

**Q1. How does the Docker daemon run?** A privileged sidecar (rejected: not
admitted, and a node escape); B rootless in the pod (weakens every pod
control); **C GKE Sandbox with `dockerd` inside (recommended, behind the
spike)**; D one VM per task (the fallback). Section 5.

**Q2. What is the task's input?** 1 the caller's command list (an invariant 10
exception); **2 a named plan in the repository (recommended)**; 3 a plan an
admin registers. Section 7.

**Q3. Who may use it?** a a frozen-contract tenant field; **b a deployment
setting naming the tenants, default empty, checked at submit and admission
(recommended)**; c on for everybody. Section 9.1.

**Q4. What does "16 dedicated cores" mean?** **Request == limit == 16 on the
general-purpose class, a Guaranteed pod (recommended)**; or one pod per node on
the Performance class, priced in the spike. Section 4.

**Q5. What are the pool limits?** **Runner and class pools at 32 (four
tasks), the tenant and GKE backend pools each raised by 32 (recommended)**; or
leave the tenant and backend pools as they are and accept that at most two run
and take the tenant's budget. Section 9.2.

**Q6. Where does Docker's image store live?** **A per-pod persistent-disk
claim deleted with the pod (recommended)**; or tmpfs out of the 64 GiB.
Section 5.3.

**Q7. Accept contract request 159?** Accept as written (after the spike, if it
chooses C); accept amended for D; or decline. Recommended: decide after the
spike, which this question does not block.

## 12. What this document did not verify

* Nothing was run against a cluster. Every Autopilot and gVisor claim beyond
  what the cited repository files record is a spike check, said where it is
  made.
* Autopilot's per-pod CPU and memory maximum, GKE Sandbox's surcharge, the
  Performance class, persistent-disk and Compute Engine prices, and the
  project's CPU quota are not in the repository.
* The pool values used are the example tfvars', not a deployment's.
