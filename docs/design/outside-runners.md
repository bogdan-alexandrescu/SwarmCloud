# Outside runners: runner profiles on machines SwarmCloud does not start, starting with macOS

**Status: design (2026-11-13). Nothing here is built.** The frozen half is a
request, [section 11](#11-the-contract-change-request), not an edit
(CLAUDE.md rule 1). Every fact in section 2 was read on 2026-11-13 at
`11f8d83`, the commit that adds this file and changes no code; lines move, so each is cited by file and symbol, the thing to search
for when the line has gone. Provider prices in section 6 are list prices read
the same day and must be read again before any order.

---

## 0. Decision record

| # | Fork | Recommended | Rejected | Why, in one line |
| --- | --- | --- | --- | --- |
| D1 | Backend shape | **The outside worker PULLS offers of already-LEASED work from swarm-api over outbound HTTPS** | SwarmCloud pushes over SSH; a Pub/Sub pull subscription | Admission, the fence and release stay in the Firestore transactions that exist; the machine needs no inbound port and SwarmCloud holds no shell on it |
| D2 | Identity and credentials | **A per-host enrolment key (Ed25519, generated on the host) proves the HOST; a per-attempt, 15-minute access token for the TENANT's worker account is minted by a narrow broker, refreshed while the lease holds, and handed to that job's VM only** | Workload identity federation from the provider's instance identity; a service-account key file | Works at every provider, no long-lived Google credential exists outside Google Cloud, and a stolen host reaches at most the tenants of the two jobs it holds, for at most 15 minutes after suspension, which also bounds a lost host's retry delay |
| D3 | Per-job isolation | **A fresh macOS virtual machine per job, an APFS copy-on-write clone of a golden image (two per host)** | A fresh local user per job; one long-lived VM reset between jobs | A guest has its own kernel, users and network stack, so an agent cannot reach the host's key, and `sandbox-exec` behaves in it exactly as on a plain Mac |
| D4 | Provider for the first worker | **Scaleway Apple silicon, Mac mini M4 with 32 GB** | AWS EC2 Mac dedicated host (the fallback); MacStadium bare metal | Hourly billing with the licence's 24-hour minimum, an API to create and delete, enough memory for two guests, the lowest list price |
| D5 | Worker software | **A versioned golden image built by a script, pinned by digest in configuration, rolled out by draining; Node 24.22.1 and Python 3.12 installed by the job under `$HOME` from a read-only, checksum-verified cache the image carries** | Toolchains preinstalled system-wide; every job downloading from the internet | The image is reproducible and rolled like a container digest; the job's own toolchain stays the job's, and installs are offline and fast |
| D6 | Operations | **A worker registry in Firestore, written only by swarm-api: enrol, ACTIVE, DRAINING, DRAINED, LOST; a host heartbeat every 30 s; a vanished host is LOST after 3 missed beats and its jobs are reclaimed by the reconciler's existing stale-lease path once termination is confirmed (suspension plus access-token expiry)** | Operator-maintained pool limits; a second, outside-only reconciler | Capacity tracks the machines that are alive, and the fence plus retry that already recover a lost pod recover a lost Mac |

The profile is generic: `claude-code-macos` runs any tenant's macOS work. No
project, account, domain or machine name appears in the catalogue, the image or
this document; a deployment's provider account, swarm-api origin and fleet size
come from configuration (section 8.6).

---

## 1. What is asked

Jobs on a macOS (Apple silicon) runner profile run the way a `claude-code` step
runs today: a clean, **full** clone at a pinned commit; artifacts uploaded;
retries with fencing; workflow chaining; agents able to drive steps (child
agents). Inside a job the macOS sandbox, `sandbox-exec`, must work, because
callers use it to deny the network in clean-clone proofs. The machine carries
the Xcode command line tools and git, with room to install Node 24.22.1 and
Python 3.12 under `$HOME`; Docker is not needed. Steps run up to about two
hours. Credentials as on every other profile: no personal login on the worker,
secrets only from Secret Manager, per-tenant isolation (invariant 9).

Google Cloud has no macOS, so the first worker is a rented Mac outside it. The
design is for **outside runners** in general — any machine SwarmCloud does not
start — with macOS as the first profile.

---

## 2. The facts each part rests on

### 2.1 The frozen catalogue

* Backends are an enum of three: `CLOUD_RUN_JOB`, `GKE_AUTOPILOT`, `AUTO`
  (`apps/common/swarm_common/profiles.py::Backend`); `AUTO` resolves to one of
  the first two (`profiles.py::resolve_backend`). An outside machine is neither,
  so a new value is needed — a contract change.
* A profile is a frozen dataclass naming `image`, `resource_class`, `backend`,
  `runner_argv`, `secrets`, `supports_checkpoint`, `spot`, `timeout_seconds`
  and `checkpoint_interval_seconds` among others
  (`profiles.py::RunnerProfile`); the catalogue is `profiles.py::RUNNER_PROFILES`.
* `claude-code` is `backend=Backend.GKE_AUTOPILOT`, image
  `agent-runtime-base`, `runner_argv` `python -m agent_worker.runners.claude_code`,
  `timeout_seconds=7200` (`RUNNER_PROFILES["claude-code"]`). Two hours is
  already the ceiling the brief asks for.
* Resource classes are `standard`, `browser`, `medium`, `large`, each with
  `cpu`, `memory_gib`, `disk_gib`, `units` and `cpu_limit`
  (`profiles.py::ResourceClass`, `profiles.py::RESOURCE_CLASSES`).
* `CPU_BURST_MAX_RATIO = 2` (`profiles.py::CPU_BURST_MAX_RATIO`, invariant 7).
* `SpotStrategy` has `ON_DEMAND_ONLY` (`profiles.py::SpotStrategy`); Spot is
  disabled (invariant 6).
* Adding a backend is "not a configuration change — a code change": the enum
  value (contract request), dispatch in `scheduler/dispatch.py`, `terminate` and
  `list_executions` in `reconciler/backends.py`, the `backend:<NAME>` pool in
  `terraform/infra/locals.tf`, and the concurrency and race tests
  (`docs/execution-backends.md`, "## 6. Adding a backend"). "Step 3 is the one
  people skip": a backend the reconciler cannot terminate stalls repair.

### 2.2 Admission, demand and the lease

* `TaskState` holds `QUEUED`, `PARKED`, `READY`, `LEASED`, `DISPATCHED`,
  `STARTING`, `RUNNING` and the terminal states
  (`apps/common/swarm_common/states.py::TaskState`).
  `states.py::CONCURRENCY_STATES` is `{LEASED, DISPATCHED, STARTING, RUNNING}`
  (invariants 1 and 3); `states.py::holds_capacity` reads it;
  `states.py::WORKER_TRANSITIONS` and `worker_may_transition` bound what a
  worker may ask for.
* Admission is one Firestore transaction: re-read the agent as `READY`, read the
  pools, check capacity, increment `active`, write the lease, move the agent to
  `LEASED` (`apps/common/swarm_common/admission.py`, module docstring;
  `admission.py::acquire_lease_in_transaction`, batched
  `admission.py::acquire_leases_in_transaction`). The pools are
  `models.py::pool_names_for`: `global`, `tenant:<id>`, `resource:<class>`,
  `runner:<profile>`, `backend:<backend>`, plus provider pools (invariant 2).
* The scheduler calls it from `apps/scheduler/scheduler/store.py::SchedulerStore.acquire_lease`.
* Pool hard limits come from Terraform:
  `terraform/infra/locals.tf` (`pool_backends`, `"backend:${b}"`, `hard_limit`
  from `var.pool_limits.backends`).
* A lease carries `generation`, `dispatch_deadline`, `expires_at`,
  `heartbeat_at` (`apps/common/swarm_common/models.py::Lease`,
  `Lease.dispatch_overdue`, `Lease.is_expired`); the agent's `Task` document carries
  `current_generation`, `max_attempts=3` (`models.py::Task`); an attempt
  carries its `generation` and `backend` (`models.py::Attempt`).

### 2.3 Dispatch and fencing

* `BackendRouter.for_backend` returns `CloudRunJobDispatcher` or
  `GkeJobDispatcher`; `BackendRouter.dispatch` is the one entry
  (`apps/scheduler/scheduler/dispatch.py`).
* The generation reaches the worker as `GENERATION` in its environment, with
  `TASK_ID`, `ATTEMPT_ID`, `LEASE_ID`, `TENANT_ID`, `RUNNER_PROFILE`
  (`dispatch.py::worker_env`). GKE embeds the generation in the Job name so a
  stale generation is never adopted (`dispatch.py::GkeJobDispatcher.dispatch`).
* The worker's first act is the fence: "STEP 1: fencing, before anything else
  exists" — `ControlPlane.validate_generation` before the workspace, any
  credential or the runner exists; a `FencedError` exits 70 without running the
  agent (`apps/agent-worker/agent_worker/lifecycle.py::Worker._run`,
  `Worker._exit_fenced`; `apps/agent-worker/agent_worker/control.py::ControlPlane.validate_generation`).
* **The worker can already run with no Firestore client at all.**
  `RemoteControlPlane` speaks only to swarm-api's state routes; its first call,
  `state/open`, generates the attempt key in the process heap and registers it
  with the dispatcher's one-use nonce, and every later call is signed; a 409 is
  the fence (`control.py::RemoteControlPlane`; `__main__.py::build_worker`,
  `config.state_via_api`). The routes check the tenant's worker ID token, the
  attempt key's Ed25519 proof, then apply the fence in one transaction
  (`apps/swarm-api/swarm_api/attemptstate.py`, module docstring;
  `attemptstate.py` `heartbeat`, `park`). The attempt key is described in
  `apps/swarm-api/swarm_api/childkey.py` (module docstring). This is what makes
  an outside worker cheap: it needs HTTPS to swarm-api, not Firestore.

### 2.4 The reconciler

* `apps/reconciler/reconciler/detect.py::detect_stale_leases` finds leases past
  `dispatch_deadline` or `expires_at` plus `heartbeat_grace_seconds` (default
  90, `apps/reconciler/reconciler/config.py`), and obsolete generations
  (`detect.py::FindingKind.OBSOLETE_GENERATION`).
* `apps/reconciler/reconciler/store.py::fence_release_repair` fences, releases
  and repairs in one transaction; `store.py::invalidate_generation` bumps
  `current_generation`; `apps/reconciler/reconciler/repair.py::requeue_backoff_seconds`
  spaces retries; `repair.py::Reconciler.run_once` is the sweep.
* A backend for the reconciler is `list_executions` and `terminate`
  (`apps/reconciler/reconciler/backends.py::Backend`, `NamespacedBackend`).

### 2.5 How a claude-code step runs

* Entry: `apps/agent-worker/agent_worker/__main__.py::main` →
  `protect_memory` → `build_worker` → `Worker.run`.
* Clone: `apps/agent-worker/agent_worker/gitops.py::shallow_clone` (`--depth 1`)
  by default; `gitops.py::clone_at_commit` checks out an exact 40-character sha
  for chained steps, falling back to a branch fetch. **Neither is a full
  clone** — the macOS profile asks for one (section 7.3). Clone bundles are
  retired (`docs/clone-bundles.md`).
* Git token: `apps/agent-worker/agent_worker/secrets.py::resolve_git_token`
  reads `swarm-tenant-<tenant>-git` through `secrets.py::SecretManagerClient.access`
  with the ambient credential of the tenant's worker account; a person's own
  slot goes through `forgebroker.ForgeBroker.fetch` instead.
* Checkpoints: `apps/agent-worker/agent_worker/checkpoint.py::CheckpointManager`,
  periodic, to `tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/`
  (module docstring; invariant 8).
* Park and exit: `lifecycle.py::Worker._park_for_quota` — checkpoint, upload,
  `control.park`, release, exit 75 (invariant 4).
* Heartbeat: `lifecycle.py::Worker._heartbeat` → `ControlPlane.heartbeat`.
* Artifacts: `lifecycle.py::Worker._upload_outputs` walks `$SWARM_ARTIFACTS_DIR`
  and uploads under the attempt's `artifact_prefix` in the object store built
  by `__main__.py::build_worker`.
* Publish: `lifecycle.py::Worker._harvest_git`, `Worker._publish_checked`,
  `Worker._push_branch`.
* Chaining: `apps/agent-worker/agent_worker/finishwake.py::PubSubFinishAnnouncer`
  announces `task_finished`, which the scheduler's `Scheduler.release_dependants`
  resolves. Child agents: `apps/agent-worker/agent_worker/children.py` signs a
  request with the attempt key (`docs/child-tasks-for-agents.md`).

### 2.6 Identity and tenancy

* The tenant's worker account is derived, never read:
  `apps/common/swarm_common/identity.py::worker_service_account_id`
  (`swarm-agent-worker-<tenant>`); `models.py::Tenant.service_account` is record
  only.
* What a tenant owns: its account, the `tenants/<id>/` prefix, the
  `swarm-tenant-<id>-<provider>` secrets, the `swarm-tenant-<id>` namespace
  (`docs/multi-tenancy.md`, "What a tenant owns").
* Today the credential is ambient: workload identity on GKE, the Job's service
  account on Cloud Run (`docs/execution-backends.md`, sections 3 and 4). **An
  outside machine has neither**, which is fork 2.

### 2.7 macOS facts (outside the repository)

* The macOS licence lets a lessor rent a Mac to one lessee for a minimum of 24
  consecutive hours; every provider in section 6 enforces it as a 24-hour
  minimum allocation.
* The licence allows up to two additional macOS instances in virtual machines
  on each Apple-branded computer, and Apple's Virtualization framework refuses
  a third concurrent macOS guest: **two job slots per host**.
* A macOS guest on the Virtualization framework is a full macOS with its own
  kernel, users, keychain and network stack; its disk is a file the host can
  clone copy-on-write on APFS in about a second.
* `sandbox-exec` (marked deprecated in its manual page, still shipped and
  working) applies a Seatbelt profile to a process. A process that is already
  sandboxed cannot apply a second, broader profile: so **the worker must never
  wrap the agent in a sandbox of its own**, or the caller's `sandbox-exec`
  fails. This is the constraint fork 3 is judged by, and lane L4's red test
  proves it.

---

## 3. Fork 1: the backend shape

### 3.1 The choices

**A. Pull (recommended).** The machine opens an outbound HTTPS connection to
swarm-api and long-polls for offers of work that admission has **already
leased**. It claims one in a transaction, starts a job VM, and the worker in
that VM speaks the existing state routes (`RemoteControlPlane`).

**B. Push.** The scheduler's new dispatcher connects to the machine (SSH, or an
agent on an inbound port behind a VPN) and starts the job.

**C. Pull through Pub/Sub.** The scheduler publishes offers to a per-profile
topic; machines hold a pull subscription.

### 3.2 Why A

* **No inbound path.** B needs the Mac reachable from Google Cloud and gives
  SwarmCloud a shell on it — a credential that can run anything on every
  worker, held in the platform. A needs only outbound 443, works behind any
  provider's NAT, and the most a leaked swarm-api route can do is hand out an
  offer.
* **The transactions already exist.** The claim, every transition and the
  release run in swarm-api's state routes, which already re-read the agent and
  the lease and apply the fence in one transaction
  (`attemptstate.py`). C would put a second queue beside Firestore whose
  delivery (at-least-once, ack deadlines) must be reconciled with the lease;
  A keeps one source of truth.
* **The worker code is unchanged.** `RemoteControlPlane` is the state path every
  tenant is being moved to; an outside worker is a tenant worker that was never
  given a Firestore client.

### 3.3 How the invariants hold for a machine SwarmCloud does not start

**Capacity (invariant 1).** Only leased work is offered. `QUEUED`, `PARKED` and
`READY` agents are never offered, and nothing on the machine is a backlog: an
idle host holds no job and no claim. Demand on the fleet is exactly the leases
in `CONCURRENCY_STATES` whose backend is the outside one.

**All-or-nothing (invariant 2).** Admission is unchanged:
`acquire_lease_in_transaction` reserves `global`, `tenant:`, `resource:`,
`runner:` and the new `backend:OUTSIDE_PULL` pool in one transaction, or none.
What is new is that the `backend:OUTSIDE_PULL` pool's limit must follow the
fleet: it is the sum of the free slots of ACTIVE hosts (two per macOS host).
swarm-api's registry transaction (section 8) writes that limit when a host
becomes ACTIVE, DRAINING, DRAINED or LOST, reading `active` in the same
transaction. The field it writes is the pool document's `adaptive_target`,
never `hard_limit`: `SlotPool.effective_limit` (`models.py`) is the minimum of
`hard_limit`, `adaptive_target` and `quota_derived_limit`, so the fleet can
lower the limit but never raise it past the ceiling. Today only the quota
broker writes `adaptive_target`, and only on `provider:` pools (`aimd.py`), so
nothing else contends for it on `backend:OUTSIDE_PULL`; `quota_derived_limit`
stays unset. Lowering a limit below `active` admits nothing new until leases
drain; it never revokes a lease. Terraform keeps `hard_limit` as the
**ceiling** the registry may not exceed (`locals.tf`, `pool_backends`), which is
also the cost ceiling in slots (section 8.6). A Terraform apply does not
overwrite the registry's value: the pool document is created once with
`ignore_changes = [fields]` (`terraform/modules/firestore/bootstrap.tf`,
`google_firestore_document.pool`), so neither `adaptive_target` nor `active`
is ever reset by an apply; raising the ceiling is an operator's edit of
`hard_limit`, as for every pool today. One pool per machine class
(`resource:macos-vm`) separates macOS slots from any later outside class.

**Concurrency from LEASED (invariant 3).** The slot is taken at admission, before
any host has seen the offer, exactly as for GKE. A claim moves the lease
`LEASED → DISPATCHED`; the job VM's `state/open` and start move it to `STARTING`
and `RUNNING`. If no host claims before `Lease.dispatch_deadline`,
`detect_stale_leases` releases it as it releases a GKE Job that never started.

**Fencing (invariant 5).** The offer carries the tuple `(tenant_id, task_id,
attempt_id, lease_id, generation)` and the dispatcher's one-use nonce, exactly
what `worker_env` passes a pod. The claim is a compare on generation. The job
VM's worker runs `validate_generation` first and exits 70 without running the
agent when fenced; every later state call is fenced by swarm-api (409). A VM
name includes the generation, as the GKE Job name does, so a host never adopts
a stale VM.

**Park and exit (invariant 4).** Unchanged inside the job:
`Worker._park_for_quota` checkpoints, uploads, parks through `state/park`, and
the lease is released by that route's transaction. The worker exits 75; the
host sees the VM stop and **destroys it**; the slot is free in the pool from the
release, not from the VM's teardown. The parked agent is re-admitted later and
may run on any host: its workspace comes back from the checkpoint in GCS, never
from the host's disk.

**Termination for the reconciler.** `list_executions` reads the registry's
record of each host's running VMs (reported in its heartbeat);
`terminate` writes a kill request the host acts on at its next poll and
acknowledges. For a LOST host, `terminate` returns confirmed once the host's
credential is suspended **and** the longest-lived access token any of its jobs
was given has expired (section 4.2: 15 minutes from its last refresh): past
that point the VM can neither call swarm-api with effect (its attempt is fenced
and its host suspended) nor read a secret or an object, so releasing the slot
cannot double-run a job with effect. This is what `Reconciler._terminate`
(`repair.py`) already demands of every backend: no release before a confirmed
stop, except for a worker that never started. Section 8.4 gives the delay this
puts on a retry.

---

## 4. Fork 2: the worker's identity and credentials

There are two identities: the **host** (pulls offers, reports health, starts
VMs) and the **job** (the tenant's worker inside one VM for one attempt). They
must not be the same: a host serves every tenant.

### 4.1 The host

**A. Per-host enrolment key, short-lived host tokens from swarm-api
(recommended).** At enrolment the host generates an Ed25519 key in its own
keychain (not exportable by the job VMs, which never share the host's user).
An operator runs the enrolment script, which prints a one-use enrolment code
(`secrets.token_hex`-built, shown once, typed into the host); swarm-api records
the public key in the registry as PENDING; the operator confirms with a typed
confirmation and the host becomes ACTIVE. Afterwards the host signs a
challenge for a 15-minute host token, the same Ed25519 pattern as the attempt
key (`childkey.py`). Works at every provider; nothing long-lived from Google
exists on the host.

**B. Workload identity federation from the provider's instance identity.**
Where the provider has one (an AWS instance role), Google's workload identity
federation exchanges it for a Google token with no secret on disk. It exists at
one of the three providers in section 6, and it yields a Google credential,
not a swarm-api host identity, so A is still needed to bind offers to a host.
It stays an option to layer under A on that provider.

**C. A service-account key file on the host.** A long-lived, exportable Google
credential on a rented machine. Rejected.

### 4.2 The job

**Per-attempt tenant tokens minted by a narrow broker (recommended).** When a
host claims an offer, swarm-api asks a **broker** — a new, small service with
its own service account, the only principal holding Token Creator on the
tenants' worker accounts, scoped by an IAM condition to accounts named
`swarm-agent-worker-*` — for:

* a 15-minute **access token** for `worker_service_account_id(tenant)` (the
  IAM Credentials `generateAccessToken` call takes a `lifetime`; one hour is
  only its default), and
* an **ID token** for that account with swarm-api's audience (what the state
  routes' first check reads, `attemptstate.py`). Its one-hour life is fixed by
  `generateIdToken`, but it reaches nothing except swarm-api's state routes,
  which refuse a fenced attempt (409) and a suspended host's jobs, so it does
  not hold a release.

The access token is short on purpose: it is what reads secrets and objects, so
its lifetime is the wait between a host vanishing and its jobs' slots being
released (sections 3.3 and 8.4). Fifteen minutes keeps that wait small against
a two-hour step; the worker refreshes at ten, so a live job never sees it
lapse.

The broker mints only for a lease that is `DISPATCHED` to the asking host, at
the lease's generation, and records the mint on the attempt. The host passes
both tokens into the VM over the host-only channel (section 5) and holds them
in memory only. The worker in the VM refreshes them through the state routes
(signed by the attempt key it registered at `state/open`) while its lease
holds; a fenced or released attempt is refused a refresh.

With that credential the job is a tenant worker like any other:

* secrets come from Secret Manager through `SecretManagerClient.access`,
  `swarm-tenant-<tenant>-*` only, because that is all the tenant's account can
  read (`docs/multi-tenancy.md`); the git token through `resolve_git_token`;
* checkpoints and artifacts go to `tenants/<tenant>/` only;
* no personal login exists on the image or the host; a person's own forge slot
  still goes through `ForgeBroker.fetch`, never Secret Manager.

The other choice, **swarm-api serving the secret values itself**, puts every
tenant's secrets through one more service's memory and makes swarm-api a
secret store; rejected.

### 4.3 What a stolen worker could reach

| Stolen | Reaches | For how long | Stops when |
| --- | --- | --- | --- |
| A job VM | Its own tenant's secrets, `tenants/<id>/` and the state routes for its own attempt | At most 15 minutes after its last refresh | The attempt is fenced (refresh refused) and the access token expires |
| A host | The tokens of the at most two jobs it holds (two tenants); new offers for the outside profile | Its host token's 15 minutes, renewable only with the host key | An operator suspends the host in the registry (one command, section 8.2); its job access tokens then lapse within 15 minutes |
| The host key | The same as the host, from elsewhere | Until suspended | Suspension; the key is useless without the registry's ACTIVE row |

Not reachable from any of them: Firestore (no client, no role), other tenants'
secrets or prefixes (IAM), the broker's Token Creator role (it never leaves
Google Cloud), GKE (nothing points `kubectl` anywhere from an outside host),
and any deny-listed resource of a shared project.

A host serving several tenants is the residual risk. The owner may choose
**tenant-pinned hosts** (a host's registry row names the tenants it may claim
for), which caps a stolen host at those tenants at the price of idle slots; the
registry supports it from the first lane, off by default (owner decision 3,
section 12).

---

## 5. Fork 3: per-job isolation on macOS

### 5.1 The choices

**A. A fresh macOS VM per job, cloned from a golden image (recommended).** The
host keeps the golden image on local APFS. Per claimed offer it makes a
copy-on-write clone (seconds, no extra disk until written), boots it with a
fixed CPU and memory allocation, runs the worker as the guest's job user, and
deletes the clone when the worker exits — success, failure, park or fence. Two
slots per host (section 2.7).

**B. A fresh local user per job on the host.** `sysadminctl` creates a user,
the job runs as it, the user and its home are deleted afterwards. Up to the
host's capacity in slots, not two.

**C. One long-lived VM per slot, restored from a snapshot between jobs.** Saves
a boot per job; everything else is A.

### 5.2 How `sandbox-exec` behaves in each

* **A.** The guest is a whole macOS. The agent runs unsandboxed as the guest's
  job user, so a caller's `sandbox-exec -p '(version 1)(allow default)(deny
  network*)' ...` applies its profile exactly as on a developer's Mac, and a
  network-denied clean-clone proof fails to reach the network. Nothing the
  caller does in the sandbox can loosen the VM boundary.
* **B.** Also works — the job user is unsandboxed — but the only boundary
  between a job and the host is Unix permissions on a shared kernel. The job
  shares `/tmp`, the process table (it can list other jobs' commands and
  arguments), the network stack (it can reach the host's local control socket),
  and any local privilege escalation reaches the host key, which serves every
  tenant. To close those B would want the worker to wrap the job in a sandbox
  profile, which is exactly what breaks the caller's `sandbox-exec` (section
  2.7).
* **C.** As A, but a snapshot restore that misses a write leaves one tenant's
  data for the next; A's delete-the-clone cannot.

### 5.3 Why A

It is the only choice in which a job cannot touch the host's key or another
job, and in which `sandbox-exec` needs no exception. Its cost — two slots per
host and a VM boot (about 20-40 s from a cloned disk) — is small against a
two-hour step. The guest's resources are fixed at boot: memory and disk request
equal limit by construction, and the guest's CPU count is its request and its
limit (invariant 7 holds with a ratio of 1). Spot does not exist here
(invariant 6).

The host's VM tool is a lane decision (L3): a small, in-repository wrapper over
the Virtualization framework, or an existing open-source VM manager for it;
the licence of any third-party manager is an owner check before it ships.

### 5.4 Sizing two guests

A 32 GB, 10-core M4 host gives each guest 4 cores and 12 GB, leaving the host
2 cores and 8 GB; the guest disk is a clone with 100 GB of logical size, of
which a job's writes are the only real use. A 16 GB host leaves each guest
about 6 GB, which is too little for an agent plus a build; it is not
recommended. This is the resource class `macos-vm` in the request (section 11).

---

## 6. Fork 4: the provider for the rented Mac

List prices read 2026-11-13; read them again at order time.

| | AWS EC2 Mac dedicated host | MacStadium bare-metal Mac mini | Scaleway Apple silicon |
| --- | --- | --- | --- |
| Hourly | `mac2-m2.metal` (M2) $0.878/h; `mac-m4.metal` (M4) $1.23/h, cheapest region | none: monthly only | M4-S €0.22/h; M4-M €0.29/h |
| Monthly (730 h) | about $641 (M2), about $898 (M4); lower with a savings plan | M4 16 GB/256 GB $149; M4 24 GB/512 GB $249; M4 Pro 48 GB/1 TB $349 | about €160 (M4-S), about €212 (M4-M) |
| Minimum allocation | 24 h per dedicated host | one month (or annual) | 24 h |
| Apple silicon | M1, M2, M2 Pro, M4, M4 Pro | M4, M4 Pro (older M-series in catalogue) | M4 (16 or 32 GB), M2, M2 Pro |
| Provisioning by API | Yes: allocate host and run instance, with a mature Terraform provider; a host takes time to scrub after release | Ordering through a portal or sales; an API for its own VM orchestration product, not for ordering a bare host | Yes: an Apple silicon API (versioned `v1alpha1`) and a Terraform resource |
| Network egress | Metered to the internet per GB after the monthly free tier | Bandwidth included with the server (cap to confirm at order) | Public bandwidth included; a higher bandwidth is an option (to confirm at order) |
| macOS guests on it | Reported working on `mac-m4.metal` with the Virtualization framework | Bare metal: yes | Bare metal: yes |
| Region | Many | US and EU data centres | Paris only |

**Recommended for the first worker: Scaleway Apple silicon, M4-M (32 GB).** It
is the only one of the three that is hourly, creatable and deletable by API
**and** cheapest; the 24-hour minimum is the licence's, not the provider's, and
matches AWS. 32 GB is what two guests need (section 5.4). Its risks are named:
one region (latency to the deployment's Google Cloud region; egress from Google
Cloud to it is billed the same as to any internet destination) and an API that
is still versioned alpha.

**The fallback is AWS EC2 `mac-m4.metal`**: three to four times the price, a
mature API, many regions, and the instance identity that option 4.1 B layers
under the host key. **MacStadium** is the choice only if a fixed monthly cost
matters more than elasticity: it has no hourly billing and no ordering API, so
the fleet could not scale by day.

The backend does not depend on the provider: a host is a host once enrolled.
Only the provisioning script (lane L7) is provider-specific, behind one
interface (create, delete, list, with the provider credential in Secret
Manager, never in the repository).

---

## 7. Fork 5: the worker's software

### 7.1 What is where

* **Host (the rented Mac):** the provider's macOS, automatic updates off, the
  host agent (a small Python 3.11 process installed under a fixed directory
  with its own interpreter, run by `launchd` as an unprivileged user), the VM
  manager, the golden images, and nothing else. No forge, provider or Google
  credential; only the host key.
* **Golden image (the guest):** macOS at a pinned version; Xcode command line
  tools (which carry git); the agent worker and its pinned Python 3.11 runtime
  under `/opt/swarm` (owned by root, read-only to the job user); the Claude
  Code CLI and the Node it needs, under `/opt/swarm` as well; `uv`; a
  read-only cache under `/opt/swarm/toolchains` holding the Node 24.22.1
  darwin-arm64 tarball and a Python 3.12 build, each with its published
  checksum. The job user's `$HOME` is empty, with at least 60 GB free. No
  Docker. No personal login: the image is built with no keychain entries and
  no forge or provider credential.

### 7.2 How a job gets Node and Python

**A. The job installs them under `$HOME` from the image's checksum-verified
cache (recommended).** `uv python install 3.12` with the cache as its source,
and the Node tarball unpacked into `$HOME/.local/node-24.22.1` after a checksum
check; a helper on `PATH` does both in a few seconds, offline. The toolchain is
the job's, as the brief asks, and a clean-clone proof with the network denied
still installs.

**B. Preinstalled system-wide.** Faster still, but the job no longer controls
its own toolchain and every version bump is an image release.

**C. Downloaded per job from the internet.** Slow, metered egress, and fails
under a network-denied proof.

### 7.3 The clone

The macOS profile clones in full at the pinned commit: `git clone` without
`--depth`, then `git checkout --detach <sha>`, then `git clean -xdf` and a
check that `git rev-parse HEAD` is the pin. Today's `gitops.py::clone_at_commit`
is shallow by sha; the request (section 11) adds a profile field so the worker
picks the full path by contract, not by a profile name in code.

### 7.4 Keeping it current

A script builds the golden image from a pinned macOS restore image and the
pinned versions above, then runs a self-test in the guest (`python -m
agent_worker --self-test`, `xcode-select -p`, `git --version`, a
`sandbox-exec` network-deny probe, the toolchain checksums). Its output is
named by digest; the deployment's configuration pins the digest the hosts run,
exactly as an image digest is pinned for GKE. Releases: weekly, and within a
day of a macOS security update. Rollout: hosts drain one at a time (section
8.2), pull the new image, run the self-test, and return to ACTIVE; a failed
self-test leaves the host DRAINED and alerts.

---

## 8. Fork 6: operations

### 8.1 The registry

A Firestore collection `outside_hosts`, written only by swarm-api (hosts have
no Firestore access). A row holds the host id (random, never a provider name),
public key, machine class, slots, allowed tenants (empty: any), image digest,
state, last heartbeat and the VMs it reports. States:
`PENDING → ACTIVE ⇄ DRAINING → DRAINED`, and `LOST` from ACTIVE or DRAINING.
Every state change and the `backend:OUTSIDE_PULL` pool's `adaptive_target` are
written in one transaction (section 3.3); `hard_limit` is Terraform's and the
registry never writes it. Only ACTIVE hosts may claim.

### 8.2 Registering and draining

* **Enrol:** `scripts/outside-host-enrol.sh` (new) prints a one-use code; the
  host agent's first run takes it; the script then asks for a **typed**
  confirmation of the host id before ACTIVE (it ignores `SWARM_ASSUME_YES`).
* **Drain:** `scripts/outside-host-drain.sh <host-id>`; the host claims
  nothing new, its running jobs finish (two hours at most), then DRAINED. The
  pool limit drops at the start of the drain.
* **Suspend:** the same script with `--suspend`, typed confirmation: the host
  token is refused at once, job token refreshes are refused, the host is LOST
  for the pool.

### 8.3 Health and heartbeats

* The host beats every 30 s (`outside/heartbeat`, signed with its host token):
  free slots, running VMs with their tuples, image digest, free disk, uptime.
  A host with under 120 GB free claims nothing.
* Each job beats as every worker does, through `state/heartbeat`, extending its
  own lease (`Worker._heartbeat`); the reconciler judges the job by the lease,
  not by the host.

### 8.4 When a host vanishes mid-job

1. Its host beats stop. After 3 missed beats (90 s) swarm-api's sweep marks it
   LOST, lowers the pool limit and suspends its host token.
2. Its jobs' leases stop being extended. After `heartbeat_grace_seconds` (90),
   `detect_stale_leases` finds them, but finding is not releasing: the repair
   releases only after a confirmed terminate (`Reconciler._terminate`,
   `repair.py`), and for a LOST host terminate is confirmed only once the host
   is suspended **and** every access token its jobs held has expired (section
   3.3). Until then each pass records the finding as held
   (`termination_unconfirmed`) and the slots stay taken. A worker that never
   started is the one exception, released on that evidence as today.
3. Once confirmed, `fence_release_repair` bumps the generation, releases every
   pool the lease named and requeues the agent with `requeue_backoff_seconds`,
   within `max_attempts`.
4. The retry runs on any ACTIVE host from the last checkpoint in GCS.
5. If the vanished host comes back, its VMs' next state call meets the new
   generation and gets 409: the worker exits without touching the lease
   (invariant 5). Its job access tokens cannot be refreshed and expire within
   15 minutes. The host itself stays suspended until an operator re-enrols it.

**The retry delay.** Suspension comes at 90 s; the access token was refreshed
at most ten minutes before the host vanished and lives 15 minutes (section
4.2), so in the worst case the release comes about 15 minutes after the host
vanished, plus one reconciler pass, then `requeue_backoff_seconds`. Against a
two-hour step that is the cost of a lost host on top of the work since the last
checkpoint, and the retry is a new attempt with its own `timeout_seconds`. With
a one-hour token the same wait would be up to an hour, half the step, which is
why the token is short. Meanwhile the held leases still count in the pool's
`active` while the LOST transaction has already taken the host's two slots out
of `adaptive_target`, so for those minutes the fleet admits up to two fewer
jobs than it has free guests; it never oversubscribes.

### 8.5 Cost controls

A rented Mac costs by the day whether it runs jobs or not, so:

* **Fleet ceiling:** the deployment's configuration names the maximum hosts;
  Terraform's `hard_limit` for `backend:OUTSIDE_PULL` is twice that, so
  admission cannot exceed what is paid for.
* **Scale by whole days:** the provisioning job adds a host when leases wait on
  the outside pool for longer than a configured period, and deletes one only
  when it is DRAINED and past its 24-hour minimum; it never deletes a host
  with a running VM.
* **Report:** daily host-hours, slot-hours used and idle, per tenant by
  attempt; an alert when idle slot-hours pass a configured share for three
  days.
* **Budget:** the provider account carries its own budget alert at the owner's
  ceiling (owner decision 2).

### 8.6 What comes from configuration

The swarm-api origin the host calls, the provider and its region, the fleet
ceiling, the image digest, the heartbeat interval and the LOST threshold. None
is in the repository; placeholders only (`https://swarm.example.com`).

---

## 9. The invariants, one by one

1. Demand: only `LEASED`..`RUNNING` hold outside slots; nothing is offered
   before admission; an idle host holds nothing.
2. All-or-nothing: admission is the existing transaction with one more pool,
   whose limit the registry writes in its own transaction.
3. Concurrency from `LEASED`: the slot is taken at admission, before a claim.
4. Park and exit: unchanged in the worker; the VM is destroyed on exit.
5. Fencing: generation in the offer, the claim, the VM name and every state
   call; a stale VM exits 70 without running the agent.
6. Spot: not applicable; dedicated rented hosts.
7. Resources: a guest's memory, disk and CPU are fixed at boot; request equals
   limit.
8. Checkpointing: unchanged, periodic, to the tenant's prefix.
9. Tenant isolation: the job runs as the tenant's own account, reads its own
   secrets and prefix, in its own VM; the namespace's role is played by the VM.
10. Profiles by name: callers pick `claude-code-macos`; nothing about the host,
    image or VM comes from a caller.

---

## 10. Build lanes, in order

Each lane pushes its tests alone first and lets CI show them red.

| Lane | Files | Red-first test | Waits on |
| --- | --- | --- | --- |
| L0 Contract | `apps/common/swarm_common/profiles.py`, `docs/contract-change-requests.md` (applied by the owner) | `tests/unit/common/test_outside_backend_contract.py`: `Backend.OUTSIDE_PULL` exists; `claude-code-macos` names it, `macos-vm`, `full_clone=True`, `timeout_seconds=7200`, `ON_DEMAND_ONLY` | The owner accepting section 11 |
| L1 Registry and offers | `apps/swarm-api/swarm_api/outsidehosts.py` (new), its routes, `apps/scheduler/scheduler/dispatch.py` (`OutsidePullDispatcher`, `BackendRouter.for_backend`) | `tests/unit/control_plane/test_outside_hosts.py`: a claim of a lease at an old generation is refused; a DRAINING host cannot claim; a state change and the pool's `adaptive_target` commit together or not at all, and `hard_limit` is never written; an unclaimed offer is released by `dispatch_deadline` | L0 |
| L2 Reconciler | `apps/reconciler/reconciler/backends.py` (`OutsideBackend.list_executions`, `terminate`), `repair.py` | `tests/unit/reconciler/test_outside_backend.py`: a LOST host's lease is fenced, released and requeued; `terminate` is unconfirmed, and the slot held, until suspension plus access-token expiry | L1 |
| L3 Host agent and VM manager | `apps/outside-host/` (new): poll, claim, clone, boot, inject tokens, destroy, heartbeat | `tests/unit/worker/test_outside_host_lifecycle.py` against a fake VM manager: a VM is destroyed on every exit code (0, 70, 75, 1); a VM name carries the generation; nothing is claimed with a full disk | L1 |
| L4 Golden image | `images/macos-runner/` (new build script and self-test) | `tests/unit/scripts/test_macos_runner_image.py`: pinned versions and checksums are present and no credential-shaped line exists; the self-test script includes the `sandbox-exec` network-deny probe | L3 for the host side; a macOS build host |
| L5 Broker and job tokens | `apps/swarm-broker-tokens/` (new service), its Terraform under `terraform/infra/` with `managed-by=swarm-terraform`, the refresh route in `attemptstate.py` | `tests/unit/broker/test_outside_tokens.py`: no mint for a lease not DISPATCHED to the asking host, at another generation, or for an account outside `swarm-agent-worker-*`; an access token asks for a 15-minute lifetime; a fenced attempt's refresh is refused | L1; owner decision 3 |
| L6 Worker full clone | `apps/agent-worker/agent_worker/gitops.py` (`full_clone_at_commit`), the runner's clone call | `tests/unit/worker/test_full_clone.py`: on a local bare repository, the clone has full history, `HEAD` is the pin, the tree is clean | L0 |
| L7 Provider and operations | `scripts/outside-host-enrol.sh`, `scripts/outside-host-drain.sh`, `scripts/outside-fleet.sh` (provider create/delete/list), `docs/operations.md` | `tests/unit/scripts/test_outside_host_scripts.py`: `set -euo pipefail` first, typed confirmation required and `SWARM_ASSUME_YES` ignored, no provider credential read from anything but Secret Manager | L1; owner decisions 1 and 2 |
| L8 Live proof | `scripts/concurrency-test.sh`, `scripts/race-test.sh` extended for the backend | Run against one enrolled host: two jobs run, a third waits in `READY` holding no slot; a killed host's job retries elsewhere from its checkpoint; a network-denied `sandbox-exec` probe fails to connect | L1-L7 |

---

## 11. The contract change request

To be filed in `docs/contract-change-requests.md` as the next number:

> **`profiles.py`: a backend for machines SwarmCloud does not start, and the
> `claude-code-macos` profile (outside runners).**
>
> **Status:** open, raised 2026-11-13 by the outside-runners design
> (`docs/design/outside-runners.md`).
>
> **The requested change.**
>
> 1. `Backend.OUTSIDE_PULL = "OUTSIDE_PULL"`: work on this backend is offered
>    to enrolled outside hosts after admission and claimed by them; it is never
>    chosen by `AUTO` (`resolve_backend` unchanged).
> 2. `ResourceClass` `macos-vm`: `cpu=4`, `memory_gib=12`, `disk_gib=100`,
>    `units=1`, `cpu_limit=4`, `requires_preview_disk=False`. One unit is one
>    macOS guest; a host has two.
> 3. `RunnerProfile.full_clone: bool = False`: when true the worker clones the
>    full history at the pinned commit and verifies a clean tree. Default
>    false, so every existing profile is unchanged.
> 4. `RUNNER_PROFILES["claude-code-macos"]`: `backend=Backend.OUTSIDE_PULL`,
>    `resource_class="macos-vm"`, image the golden image's configured name
>    (the digest comes from deployment configuration), `runner_argv` as
>    `claude-code`, the same `secrets` and `secrets_any_of`,
>    `spot=SpotStrategy.ON_DEMAND_ONLY`, `timeout_seconds=7200`,
>    `supports_checkpoint=True`, the same `checkpoint_interval_seconds`,
>    `full_clone=True`, `available=False` until a host is ACTIVE.
>
> **What it costs.** Nothing for existing profiles: a new enum value, a new
> class, a defaulted field and a new profile. `BackendRouter.for_backend` and
> the reconciler's backend table must handle the value before the profile is
> made available (lanes L1, L2).
>
> **What it does not change.** `states.py`, `admission.py` and `models.py`:
> the claim is `LEASED → DISPATCHED`, already legal; the pools are those
> `pool_names_for` already derives.

---

## 12. Owner decisions

1. **Provider and account.** Recommended: Scaleway Apple silicon M4-M for the
   first host, AWS `mac-m4.metal` as the fallback (section 6). The account,
   its billing owner and where its API credential lives in Secret Manager are
   the owner's.
2. **Cost ceiling.** The maximum hosts (each a fixed daily cost with a 24-hour
   minimum) and the monthly budget alert. One host is two slots; the design
   starts at one.
3. **The security model.** (a) Accept the broker of section 4.2 — the only
   principal with Token Creator on tenant worker accounts, conditioned to
   their name prefix. (b) Shared hosts or tenant-pinned hosts (section 4.3).
   (c) The licence check on any third-party VM manager (section 5.3).
