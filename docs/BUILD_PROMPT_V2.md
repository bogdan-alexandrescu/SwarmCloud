# SwarmCloud v2 — build prompt

**Status:** specification, not yet built.
**Supersedes:** `gcp_zero_idle_agent_swarm_build_prompt.md` (v1), which is built,
deployed and running. v2 keeps v1's control plane and replaces its execution
substrate.

Read `CONTRACT.md` for the invariants v1 froze. §9 of this document lists the
ones v2 changes and why; everything not listed there still holds.

---

## 1. What this is for

v1 answered *"can work be admitted, dispatched and completed with no idle
cost?"* — yes, proven end to end.

v2 answers a different question: **can a Claude Code workflow on a laptop push
its entire agent fleet into a cluster, watch every one of them work, and get the
results back?** Everything below follows from that sentence.

The thing being built is not a job runner. It is the remote half of an ultracode
workflow — so a step that would have run as a local subagent instead runs as a
pod, with its own filesystem, its own memory, its own credential, and its own
live output, and the workflow script barely notices.

---

## 2. Decisions

Every one of these was made deliberately. Where a decision costs something, the
cost is written down next to it, because the reason a decision looks wrong in
six months is usually that its cost was never recorded.

### 2.1 Substrate: GKE Autopilot, and only GKE Autopilot

Everything is a pod. One scheduler, one reaper, one log path, one resource
model, no "which backend" branching anywhere in the code or the dashboard.

**What this buys:** `PersistentVolumeClaim` if ever needed, `kubectl exec` into a
running agent, `kubectl logs -f`, up to ~28 vCPU / 80 GiB per pod, extended run
time so a node upgrade does not evict a working agent.

**What it costs, plainly:**

* **The Cloud Run Jobs backend is retired.** That is working, proven, tested code
  — the dispatcher, the per-tenant Job resources, the custom `swarmJobDispatcher`
  and `swarmJobReaper` roles, and the terraform module that builds them. It is
  deleted, not left dormant: a second backend nobody exercises is a second
  backend that silently rots.
* Autopilot bills a **minimum 0.25 vCPU / 0.5 GiB and one minute per pod**. A
  ten-second mock task now costs what a sixty-second one does.
* **Cold start on every task.** There is no warm path any more.
* A cluster management fee (~$0.10/hr list) replaces v1's literal zero. The GKE
  free tier credit covers roughly one cluster; verify against the actual bill
  rather than trusting this sentence.

**Zero-idle is now a property of the WORKLOAD, not of the bill.** Say it that way
in the README. v1's headline claim does not survive this decision intact, and
pretending otherwise would be the kind of thing that gets discovered by an
invoice.

### 2.2 Isolation: root inside the pod, gVisor underneath

Agents run as **root with a writable root filesystem** and can `apt-get install`
anything. This is required: an agent that cannot install the tool it needs is an
agent that fails at the first unanticipated task.

Root removes most of the in-container defence — read-only rootfs and dropped
capabilities were doing that work. So the boundary moves down a layer:
**`runtimeClass: gvisor`** on every agent pod. Syscalls hit gVisor's user-space
kernel rather than the host's, so an escape has to break gVisor before it reaches
a node shared with other tenants.

**Verified on the live cluster:** Autopilot pre-creates the `gvisor`
RuntimeClass, and the RuntimeClass carries its own `nodeSelector` and
`toleration` (`sandbox.gke.io/runtime`) — so the pod spec does not add them, and
the platform's Spot policy does not refuse them, because that policy refuses
`gke-spot`/`gke-preemptible` specifically rather than tolerations in general.

**Also on the cluster, and not considered when this was decided:** a `microvm`
class with the `kata-clh` handler — Kata Containers on Cloud Hypervisor. That is
a real VM with a real Linux kernel, so it is a *stronger* boundary than gVisor,
and running a real kernel it is usually *faster* on precisely the syscall-heavy
work gVisor taxes most. It declares a fixed overhead of 250m CPU and 130Mi per
pod, which gVisor does not. Whether that fixed cost beats gVisor's variable one
is a measurement, not an argument, and §8.3b is taking it.

**Consequences that are not optional:**

* **~10–15% slower on syscall-heavy work** — compiles, large file trees, heavy
  `git` operations. Measure it; do not assume it is negligible.
* **No GPU under gVisor.** A GPU profile would need a separate non-sandboxed node
  pool and its own trust decision.
* **Docker-in-pod does not work under gVisor.** The `docker` CLI is still in the
  image, but an agent that needs to *build* an image submits to **Cloud Build** —
  which is what this repository already does for its own images, and which needs
  no daemon and no privilege.

### 2.3 Storage: ephemeral per agent, checkpointed to GCS

Each agent gets its own scratch volume sized by resource class. Mandatory
periodic checkpointing to the tenant's GCS prefix continues unchanged from v1.

Rejected: a PersistentVolume per agent. It adds 30–60s to every start, pins the
pod to one zone, and a leaked PV is a bill that keeps arriving long after the
agent is gone — the reaper becomes load-bearing for cost, not just for
correctness.

**Open:** a per-tenant shared cache volume (cloned repos, `~/.cache`, npm and uv
caches) would be a large cold-start win. Deferred, not rejected. It needs its own
quota, cleanup policy, and an answer for cross-agent interference *within* a
tenant.

### 2.4 Dispatch: an explicit `swarm` skill, not interception

```js
phase('Review')
const ids = await swarm.dispatch(DIMENSIONS.map(d => ({
  profile: 'claude-code',
  prompt:  d.prompt,
  schema:  FINDINGS,
})))
const results = await swarm.collect(ids)
```

Nothing undocumented, nothing magic, and remote steps are visibly remote in the
script. Local `agent()` calls and remote `swarm.dispatch` calls mix freely in one
workflow.

Rejected **for now**: a custom `agentType: 'swarmcloud'` that makes `agent()`
transparently remote. It is the nicer end state and it depends on plugin-API
behaviour nobody here has verified. The skill's API is what such a plugin would
call anyway, so building the skill first wastes nothing.

### 2.5 Routing: per-invocation, with a session default

```
/swarm target cloud | local | hybrid      # session default
swarm.dispatch(tasks, {target: 'local'})  # per-call override
```

`hybrid` is an explicit rule, not a heuristic: anything naming a `runner_profile`
goes to the cluster; anything needing your filesystem, your keychain or
interactivity stays local. Placement is never a surprise, and "why did that run
locally?" is never a debugging question.

### 2.6 Accounts: claudeswitch is the mechanism, the broker is the policy

**Do not rebuild this.** `claudeswitch` already solves the per-machine half of
this problem, in production, with five accounts and an audit trail of real
rotations. v2 reuses it rather than reimplementing it badly.

The split is the same one claudeswitch already draws internally:

| | |
|---|---|
| **claudeswitch, in the pod** | the *mechanism* — install a credential, refresh it, merge it correctly, verify it took |
| **the quota broker, fleet-wide** | the *policy* — which account this pod gets, when to move it, who else is on it |

What claudeswitch already provides, verified by reading the source at
`~/claudespace/claudeswitch`:

* `add`, `login`, `use`, `accounts`, `remove`, `rename`, `refresh`, `status`,
  `audit`, `history`, `why` — a complete non-interactive CLI surface.
* **A Linux credential store** (`internal/credstore/store_linux.go`) that already
  knows the live credential is `~/.claude/.credentials.json`, written atomically
  at 0600 inside a 0700 directory and verified by reading it back.
* A vault of stored accounts kept deliberately *beside* the live credential,
  "so that a stray claudeswitch file can never be mistaken for the credential
  Claude Code reads".
* A poller, a policy engine with hysteresis and cooldowns, and an audit log.
* Go, so it cross-compiles into the agent image with no runtime to install.

**What the pod must NOT run is claudeswitch's daemon.** Its policy is written for
one machine choosing among its own accounts. N pods each rotating autonomously
would collide — two pods jumping to the same account at once would burn it
together, and independent refreshes across pods reintroduce exactly the rotating
refresh-token hazard v1 spent effort eliminating. The broker owns policy; the pod
runs the mechanism.

#### 2.6.1 Onboarding — one account, one command

```
/swarm account add --label personal     → claudeswitch login, then vault → Secret Manager
/swarm account list                     → headroom per window, who holds it
/swarm account pause  <label>           → stop assigning new agents
/swarm account drain  <label>           → move every running agent off it
/swarm account remove <label>
```

`add` is the only interactive one and has to be — the OAuth flow opens a browser,
which only works on the operator's machine. It delegates to `claudeswitch login`
for the flow, then uploads the resulting vault entry to Secret Manager on STDIN.

The v1 rules hold without exception: never an argv argument (`argv` is
world-readable through `ps`), never in terraform state, never in git, never in a
log line.

#### 2.6.2 Refresh — the broker stays the single writer

Unchanged from v1 in principle, extended from one credential to N. The broker
refreshes on its existing sweep, persists the rotated refresh token **before**
publishing the access token, and treats `invalid_grant` as terminal.

claudeswitch will also refresh a credential it holds. **In a pod it must not**,
or there are two writers again. Run the pod-side `use` mechanism only, with
refresh disabled, and let the broker own it.

§7.3 applies with full force: one-writer must be **enforced**, not asserted.

#### 2.6.3 Dispatch — the pod starts already logged in

```
admission  → broker picks the account with the most headroom
           → records the assignment on the LEASE, not just in memory
pod start  → init container pulls that account from Secret Manager
           → claudeswitch installs it as the live credential
           → main container starts; claude is already authenticated
```

`CLAUDE_CONFIG_DIR` points at the pod's own ephemeral directory, so the
credential has exactly one owner and dies with the pod.

**Merge, do not overwrite.** `credstore.MergeForSwap` keeps the live blob's
`MCPOAuth` section and replaces only `claudeAiOauth`. An implementation that
writes the whole file wholesale silently destroys a pod's MCP server logins on
every swap — and would look like the MCP servers spontaneously failing, with
nothing pointing at the credential rotation that caused it.

#### 2.6.4 Swap — no signal required

**ANSWERED by spike 1.** claudeswitch writes the live credential and sends the
running `claude` process **no signal at all** — no SIGHUP, no restart, nothing.
The CLI picks it up on its own. This is corroborated in the Claude Code 2.1.274
binary: the plaintext credential store performs a fresh `readFileSync` on every
read unless a caller explicitly requests the cached copy.

So hot-swap works, and it is already proven in production by claudeswitch's own
rotation history, including forced mid-turn swaps at 100% weekly utilization.

```
reactive   sidecar sees utilization crossing the floor, or a 429
           → asks the broker for an account with headroom
           → broker assigns and records the swap on the lease
           → claudeswitch installs it; the agent never stops

proactive  /swarm account drain <label>
           → no new assignments
           → every RUNNING agent holding it is swapped in place
           → the account is idle when the last one finishes
```

Draining is what makes an account safely removable, and it is the same code path
the broker uses on its own when an account crosses a floor — so the rarely-used
human command stays working because the automatic one exercises it.

**One nuance to carry over:** claudeswitch prefers to swap while the CLI is
*idle* and only forces mid-turn above a hard floor (99% by default), with a 30s
cap on how long it will hold out. That preference exists for a reason — an
in-flight request holding the old token can still fail — so the pod-side policy
should inherit it rather than swapping the instant it is told to.

**Build checkpoint-and-resume anyway.** It is the handler for a swap that does
not take effect, and a swap mechanism with no fallback is a single point of
failure for every agent at once.

#### 2.6.5 Headroom — the API reports it directly

**ANSWERED by spike 2.** `rate_limit_event` is a first-class event in the
`stream-json` output, emitted proactively rather than only on failure:

```json
{"type":"rate_limit_event","rate_limit_info":{
  "status":"allowed","resetsAt":1789671000,"rateLimitType":"five_hour",
  "overageStatus":"rejected","isUsingOverage":false,
  "unifiedWindows":{
    "five_hour":{"utilization":0.13,"resetsAt":1789671000},
    "seven_day":{"utilization":0.61,"resetsAt":1789977600}}}}
```

`utilization` is a 0–1 float **per window**, with the exact reset time. Headroom
is therefore a reading, not an inference, and a drained account can be scheduled
back into the pool at a known instant rather than probed.

The broker consumes these from the event stream (§2.7) and must still:

* **distinguish a 429 from a revoked token.** The CLI separates them (403,
  `"OAuth token has been revoked"`); one account is throttled and will return,
  the other is dead and needs a human.
* **track both windows.** claudeswitch's own thresholds differ per window — 85%
  session, 98% weekly — because a five-hour window recovers on its own and a
  seven-day one does not.
* **stay conservative.** Treating an exhausted account as available costs a
  failed dispatch and a swap; treating an available one as exhausted costs only
  some idle capacity. Those are not symmetric.

### 2.7 Live output: structured events

The worker runs `claude --output-format stream-json` and parses each event into a
structured record — tool call started, tokens consumed, file written, turn
finished — rather than shipping raw text.

```
"Editing apps/quota-broker/main.py"
"14,203 in / 2,108 out · $0.47"
"Read 31 files · 7 edits · 2 bash"
```

Small, queryable, aggregatable, and it survives the pod. Raw logs stay in Cloud
Logging, reachable per agent on demand via `/swarm logs <id> --follow`, which is
also what live debugging needs.

**The schema, captured from Claude Code 2.1.274** (`--print --output-format
stream-json --verbose`). Every event carries `session_id` and `uuid`:

| `type` | carries | used for |
|---|---|---|
| `system` / `init` | cwd, model, tools, MCP servers, `claude_code_version`, `permissionMode` | provenance on every agent; which CLI version produced the run |
| `system` / `hook_started`, `hook_response` | hook name, stdout, exit code, outcome | a failing hook is otherwise invisible |
| `system` / `thinking_tokens` | running estimate and delta | progress signal DURING a long turn, before any tool call |
| `rate_limit_event` | `utilization` and `resetsAt` per window, overage status | §2.6.5 — the whole account pool |
| `assistant` | content blocks, and `usage` per message: input, output, `cache_creation_input_tokens`, `cache_read_input_tokens` | live token counts, cache effectiveness |
| `user` | tool results | the other half of the tool-call record |
| `result` | `total_cost_usd`, `duration_ms`, `duration_api_ms`, `ttft_ms`, `num_turns`, `modelUsage` per model, `permission_denials`, `subagent_stats`, `is_error` | terminal record, spend, the terminated-agents view |

Three of these were not anticipated and each earns its place:

* **`thinking_tokens`** ticks during a turn with no tool call in sight. Without it
  §2.9's "no progress" signal would kill an agent that is thinking hard about one
  hard problem — the exact false positive that makes a stall guard get disabled.
* **`total_cost_usd` and `modelUsage`** make per-agent spend a read rather than an
  estimate, including the Haiku calls made on the agent's behalf.
* **`permission_denials`** shows an agent repeatedly attempting something it is
  not allowed to do — which looks like a loop and is really a misconfiguration.

**The parser must degrade to "unknown event" rather than crash.** A CLI upgrade
adds event types, and a platform that falls over when Claude Code ships a release
is a platform that blocks its own upgrades.

### 2.8 Dashboard: self-hosted Cloud Run service

Reads Firestore and the Kubernetes API, pushes updates over server-sent events.

**This makes the internal load balancer and IAP a PREREQUISITE, not a nicety.**
The dashboard hits exactly the ingress wall the API already does: ingress is
`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` and the terraform module *validates*
against `INGRESS_TRAFFIC_ALL`, so neither is reachable from a laptop today. The
LB and IAP work is now on the critical path for a headline feature, and must be
sequenced accordingly.

It must show:

| | |
|---|---|
| **Running** | pod, tenant, profile, uptime, node, runtime class |
| **Per agent** | tokens in/out, spend, CPU / memory / disk against limit, current tool call, last log line, checkpoint age |
| **Workflow** | the DAG, which step is where, what is blocked on what |
| **Accounts** | headroom per subscription account, who holds which, recent swaps |
| **Terminated** | exit reason, duration, peak memory, PR link, deploy status |

That last column is the one that makes the platform legible after the fact, and
it is the one most likely to be cut for time. Do not cut it.

### 2.9 Safety: four stall signals, plus liveness

All four fire, each with its own threshold **per runner profile** rather than one
global number:

1. **No progress** — no tool call, no file write, no tokens, for N minutes.
   Catches a hung network call, a deadlocked process, a CLI waiting on a prompt
   nobody will answer.
2. **Token burn with no file changes** — the expensive loop. A research or review
   task looks *exactly* like this, which is why the threshold is per profile and
   the first action is a warning, not a kill.
3. **Hard ceilings** — wall clock, tokens, spend. Blunt, predictable, and
   impossible to argue with. Per profile, overridable per task.
4. **Repetition** — identical tool calls in sequence, or a file edited back and
   forth. Precise, few false positives, and only detectable because §2.7 gives
   structured events.

**Plus liveness and readiness probes.** A pod that is dead-but-present is a
different failure from an agent that is alive-but-idle, and the four signals
above only catch the second. Without probes, a crashed process inside a running
pod holds its slot until a ceiling expires.

Every action is: **checkpoint, terminate, record the reason.** Never terminate
without a checkpoint; never terminate without recording which rule fired.

### 2.10 PR merge: a separate agent

The work agent opens the PR and **exits**, releasing its pod, its memory and its
slot. A small dedicated merge agent watches CI, merges when green, and watches
the deploy. On red, it reopens the task with the failure as new input.

The reason is resource shape, not tidiness: CI can take twenty minutes, and that
would otherwise be twenty minutes of an 8 GiB pod doing nothing. Merge policy
also ends up in one place instead of duplicated into every work agent.

**A human-gate toggle, per repository and per tenant, exists regardless.** It is
the right posture while trust is new, and it is a one-line policy rather than an
architecture.

### 2.11 Scale: 50–100 concurrent agents

This is a design constraint with teeth, not a hope:

* **Sharded pool counters.** v1 admits work in one Firestore transaction against
  shared pool documents. At 100 concurrent admissions those documents are the
  contention point — Firestore permits roughly one write per second per document.
  The all-or-nothing multi-pool property in `CONTRACT.md` invariant 2 must
  survive sharding; that is the hard part and it needs designing, not assuming.
* **GCP quota increases are required**, not optional: CPU, in-use IP addresses,
  and Autopilot pod limits. Request them before the first load test, because the
  lead time is days.
* **~400 vCPU / ~800 GiB at peak**, ~$15–30/hr while running, ~0 when idle.

### 2.12 Toolbox

Already in the image: `git`, `jq`, `ripgrep`, `fd`, `curl`, `wget`, `node`/`npm`,
`python`/`uv`, `pytest`, `claude`, `codex`, `build-essential`.

Added:

* **`gh`** — required for "the agent opens a PR" to work at all.
* **`gcloud`, `kubectl`** — an agent can inspect the infrastructure it works on.
  Note what this implies: the agent acts as its *tenant* service account, so its
  reach is bounded by that account and by nothing else. That is a deliberate
  widening, not an accident.
* **`terraform` 1.16.2, `tofu` 1.12.6, `tflint` 0.64.0, `checkov` 3.3.17,
  `trivy` 0.74.0** — pinned to exactly what `scripts/prerequisites.sh` enforces.
  Without these an agent cannot run `make lint` or `make test` on this repository,
  which is most of what it would be asked to do.
* **`shellcheck`, `make`** — `CLAUDE.md` requires every script to be
  shellcheck-clean, and `make` is the documented entry point for every check. An
  agent that cannot verify its own work against the repo's own definition of done
  is an agent that reports success it has not earned.
* **`docker` CLI** — present, but see §2.2: image *builds* go to Cloud Build.

Agents install whatever else they need. Root is available; the boundary is gVisor.

---

## 3. Architecture

```
  laptop                          GCP
  ──────                          ───
  Claude Code
   └ /swarm skill ─────────┐
   └ workflow script       │      swarm-api ──────► Firestore
       swarm.dispatch() ───┤        (tasks, leases, pools, accounts, events)
       swarm.collect()  ◄──┘           ▲
                                       │
   dashboard (browser) ◄── IAP ── internal LB
                                       │
                              swarm-scheduler
                                       │ admits (sharded, all-or-nothing)
                                       ▼
                            GKE Autopilot · runtimeClass: gvisor
                             ┌──────────────────────────────┐
                             │ agent pod (root, sandboxed)  │
                             │  claude --output-format      │
                             │         stream-json          │
                             │  ephemeral scratch           │
                             │  sidecar: credential swap,   │
                             │           event publish,     │
                             │           checkpoint timer   │
                             └──────────────────────────────┘
                                       │
                          swarm-quota-broker
                            account pool · headroom · AIMD
                                       │
                          swarm-reconciler
                            stalls · liveness · leases · reaping
                                       │
                          merge agent (tiny pod)
                            CI watch → merge → deploy watch
```

---

## 4. What carries over unchanged

v1 built these and they are correct. They are not rewritten:

* Durable task state in Firestore; infrastructure follows admitted work.
* Execution leases with fencing generations. A stale worker exits without running
  the agent and without touching the lease.
* Concurrency counted from `LEASED`, never from `RUNNING`.
* `READY → RUNNING` structurally impossible in the transition map.
* Mandatory periodic checkpointing.
* Per-tenant isolation: own service account, own secrets, own GCS prefix, own
  namespace, own slot pools.
* Google ID token auth restricted by hosted domain; tenant derived from the
  caller, never taken from the request.
* AIMD adaptive provider concurrency; `PARKED` for quota exhaustion.
* The runner-profile catalogue: a caller picks a profile **by name** and never
  supplies an image, command, resource spec or backend parameter.

---

## 5. What gets built

1. **GKE Autopilot substrate** — cluster, gVisor runtime class, per-tenant
   namespaces, NetworkPolicies, the pod spec, RBAC.
2. **Pod dispatcher** replacing the Cloud Run Jobs dispatcher.
3. **Sharded pool counters** preserving all-or-nothing admission.
4. **Account pool** — broker-side policy (assignment, headroom, drain, refresh
   for N, still one writer) over claudeswitch as the pod-side mechanism.
5. **Agent sidecar** — credential swap, structured event publishing, checkpoint
   timer.
6. **Structured event pipeline** — `stream-json` parser, transport, storage,
   retention.
7. **Internal LB + IAP** — the prerequisite for both the API and the dashboard.
8. **Dashboard service** — everything in §2.8.
9. **`/swarm` skill** — `dispatch`, `collect`, `watch`, `logs`, `status`,
   `capacity`, `target`, `debug`, `config`, and the `account` verbs in §2.6.1
   (`add`, `list`, `pause`, `drain`, `remove`).
10. **Stall guard** — four signals, per-profile thresholds, plus probes.
11. **Merge agent** — CI watch, merge, deploy watch, human-gate toggle.
12. **New-profile definition path** — so a specialised pod shape is configuration,
    not a code change.

## 6. What gets deleted

Deleted, not deprecated. A backend nobody exercises is a backend that rots.

* `terraform/modules/cloud_run_jobs/` and its tests.
* The Cloud Run Jobs dispatch path in the scheduler.
* `swarmJobDispatcher` and `swarmJobReaper` custom roles.
* The `Backend` enum's `CLOUD_RUN_JOB` member and every branch on it.

The control-plane services stay on Cloud Run. Only the *execution* substrate
moves.

---

## 7. Properties that must survive

Each of these was expensive to get right in v1 and each is easy to lose in v2.

### 7.1 All-or-nothing admission, under sharding

Capacity is reserved across every pool in one transaction or not at all. Sharded
counters make this harder, not easier. A design that reserves shard-by-shard and
rolls back on failure is **not** equivalent — rollback is a second transaction,
and v1 already produced a permanent capacity leak from exactly that shape.

### 7.2 Concurrency counted from LEASED

Pod scheduling latency is longer and more variable than Cloud Run's. Counting
from `RUNNING` would let admission over-commit for the entire scheduling window.

### 7.3 One writer of credentials

Refresh tokens rotate; two writers invalidate each other. The account pool
multiplies the number of credentials but must not multiply the number of writers.
The broker runs on Cloud Run with `max_instances > 1` — so "one writer" is a
property that has to be *enforced*, by a Firestore transaction or a lease, and
not merely asserted in a comment. v1 asserted it in a comment. An adversarial
review found that the comment was the only thing enforcing it.

### 7.4 Callers name a profile, never a spec

Root inside the pod makes this sharper, not softer. A caller who could supply an
image would be supplying a root-capable image.

### 7.5 Checkpoint before every termination

Every stall action, every swap fallback, every reap. A termination without a
checkpoint discards work that was already paid for.

---

## 8. Verify before building

Each of these can invalidate a decision above. Spike them first; they are cheap
compared to discovering the answer halfway through.

| # | Question | Invalidates if wrong |
|---|---|---|
| 1 | ~~Where does the CLI read credentials on Linux?~~ **ANSWERED:** `~/.claude/.credentials.json` (relocatable via `CLAUDE_CONFIG_DIR`), 0600, plaintext JSON | §2.6.3 resolved |
| 1b | ~~Is an external rewrite picked up mid-session?~~ **ANSWERED: YES.** claudeswitch sends no signal at all and the CLI picks it up; the plaintext store re-reads the file on every read | §2.6.4 hot-swap confirmed; checkpoint-and-resume demoted to fallback |
| 2 | ~~What is `stream-json`'s schema?~~ **ANSWERED:** captured in full — see §2.7 | §2.7, §2.9 signals 2 and 4, the dashboard |
| 3 | Does the sandbox break any tool in §2.12? | §2.2 — benchmark running now, three arms |
| 3b | **NEW: `microvm` (kata-clh) is also on the cluster.** A real VM with a real Linux kernel: a STRONGER boundary than gVisor, usually FASTER on syscall-heavy work, at a declared 250m CPU + 130Mi per pod | §2.2 was decided before anyone knew this existed. The benchmark settles it |
| 4 | Real gVisor overhead on our actual workloads? | the 10–15% estimate is from documentation, not measurement |
| 5 | Autopilot pod cold start, p50 and p99? | if p99 is minutes, short tasks need a different answer |
| 6 | Firestore contention at 100 admissions/min? | §2.11 — measure before designing the sharding |
| 7 | Does IAP in front of an internal LB actually give the API a usable `hd` claim? | §2.8 and every multi-user claim |
| 8 | Do multiple subscription accounts, pooled this way, sit right with Anthropic's terms? | §2.6 entirely — worth answering before building on it |
| 9 | ~~Does the CLI surface rate-limit headers?~~ **ANSWERED:** yes — `anthropic-ratelimit-unified-{representative-claim,reset,overage-status}`, parsed into `{rateLimitType, resetsAt}` | §2.6.5 — headroom is predictive, not inferred |
| 10 | Does `claude setup-token` work non-interactively enough to script `/swarm account add`? | §2.6.1 ergonomics; falls back to a two-step paste |

---

## 9. CONTRACT.md amendments required

v2 contradicts the frozen contract in specific places. These are **requests**, per
`CLAUDE.md`, not changes made unilaterally:

* **Cloud Run Jobs as the primary backend** → GKE Autopilot as the only backend.
* **"No nodes, no autoscaler, no node upgrades to evict"** → false under v2. The
  replacement guarantee is extended run time plus gVisor, and it is a weaker,
  more operationally demanding promise. Say so.
* **Zero idle cost** → zero idle *workload*. A cluster fee exists.
* **Non-root, read-only rootfs, dropped capabilities** → root, writable rootfs,
  gVisor. The defence moved down a layer; it did not disappear, and it is not the
  same defence.
* **One credential per tenant per provider** → a pool of accounts per tenant, one
  held per agent at a time, swappable mid-run.

Nothing in `apps/common/swarm_common/` is edited by this document.

---

## 10. Sequencing

Ordered by what unblocks what, not by what is most interesting.

1. **Spikes 1, 2, 3** from §8. Two of them can invalidate whole components.
2. **Autopilot substrate** — cluster, gVisor, namespaces, RBAC, NetworkPolicies.
3. **Pod dispatcher**, with the existing profile catalogue unchanged.
4. **One real `claude-code` agent in a pod**, end to end. Same milestone v1
   reached with Cloud Run Jobs, re-proven on the new substrate.
5. **Structured events** — the dashboard and two stall signals both need them.
6. **Internal LB + IAP** — prerequisite for the dashboard and for any second user.
7. **Dashboard.**
8. **Account pool** — onboarding first (§2.6.1), then refresh for N, then
   headroom, then assignment and drain, then hot-swap with checkpoint-and-resume
   as its fallback.
9. **Sharded counters**, once §8.6 has measured the real contention.
10. **Stall guard and probes.**
11. **`/swarm` skill.**
12. **Merge agent.**

Steps 1–4 are the ones that decide whether v2 works. Everything after is
additive.
