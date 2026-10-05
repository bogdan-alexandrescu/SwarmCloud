# SwarmCloud v2 — build prompt

**Status:** built in part, and amended to what is built (owner decisions,
2026-10-01). The control plane, the account pool (`apps/quota-broker`), the
`/swarm` bridge (`apps/swarm-mcp`) and the dashboard (`apps/swarm-ui`) exist in
this repository. The substrate did NOT move: Cloud Run Jobs stay the primary
execution backend and GKE Autopilot runs only the browser runner — §2.1 says
why. What is deployed is a different, dated question: read
[`DEPLOY_STATE.md`](DEPLOY_STATE.md), not this line.
**Supersedes:** `gcp_zero_idle_agent_swarm_build_prompt.md` (v1). v2 keeps v1's
control plane and its execution substrate, and adds to them.

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

### 2.1 Substrate: Cloud Run Jobs primary, GKE Autopilot for the browser runner

**Amended 2026-10-01 by owner decision; the spec moved to match the code, not
the other way round.** This section originally made GKE Autopilot the sole
execution substrate, with every task a pod, and retired Cloud Run Jobs. That was
never built. What runs is (the last three are #295's, accepted 2026-10-01 and
disabled for every tenant; no Job exists for them yet):

| profile | backend | where the catalogue says so |
|---|---|---|
| `mock` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1125` |
| `generic` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1032` |
| `claude-code` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1141` |
| `codex` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1158` |
| `browser` | GKE Autopilot | `apps/common/swarm_common/profiles.py:1187` |
| `merge` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1209` |
| `post-verdict` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1228` |
| `claude-code-review` | Cloud Run Jobs | `apps/common/swarm_common/profiles.py:1253` |

`BackendRouter.for_backend` (`apps/scheduler/scheduler/dispatch.py:1696`) sends
`CLOUD_RUN_JOB` to `CloudRunJobDispatcher`
(`apps/scheduler/scheduler/dispatch.py:689`) and `GKE_AUTOPILOT` to
`GkeJobDispatcher` (`apps/scheduler/scheduler/dispatch.py:1334`); the module
header (`apps/scheduler/scheduler/dispatch.py:8`) states the same split. No
profile is `AUTO`, so `resolve_backend`
(`apps/common/swarm_common/profiles.py:1267`) only passes the declared backend
through. `tests/unit/scripts/test_docs_spec_amendments.py` reads the catalogue
and fails when this table stops matching it.

**Why Cloud Run Jobs stay primary.** The v1 rationale in
`docs/architecture.md` §5 ("Cloud Run Jobs is the primary backend, not GKE
Autopilot") still holds: Autopilot has nodes, and nodes have autoscalers,
upgrades, repairs and pressure eviction — each a way to end a running agent for
a reason unrelated to the agent. Cloud Run Jobs has no nodes to upgrade, no
autoscaler to compact work onto fewer machines and no node pool to repair. The
GKE-only plan would have traded that for `kubectl exec`, `kubectl logs -f` and
bigger pods, and taken on a cluster fee, a 0.25 vCPU / 0.5 GiB / one-minute
minimum per pod, and a second rewrite of a dispatcher that is proven. The owner
chose to keep the proven path.

**Why the browser runner is on GKE.** Chromium needs a large `/dev/shm`, and GKE
gives direct control over it (`apps/scheduler/scheduler/dispatch.py:8`). That is
the only profile on GKE today. GPU work and anything over 32 GiB would also need
GKE, but no such profile exists: `ResourceClass` refuses more than 8 vCPU or
32 GiB at import (`apps/common/swarm_common/profiles.py:65`).

**What this costs, plainly.** Two backends means two dispatchers, two reap
paths and two log paths, and a dashboard that has to say which one a task ran
on. That is the "which backend" branching this section originally set out to
delete, kept on purpose. `kubectl exec` and `kubectl logs -f` exist for browser
tasks only; a Cloud Run task is read through Cloud Logging and the structured
events. The gVisor shape in §2.2 is a render option for GKE pods
(`kubernetes/render.py:390`, `--runtime gvisor`), not the default, and does not
apply to Cloud Run Jobs at all.

**Workspace storage: memory, GA, live migration.** Cloud Run's disk-backed
ephemeral volume is **Preview** and **disables live migration**, which would
partially undermine the reason Cloud Run was chosen. This platform does not use
it: the Terraform google provider cannot express it (`empty_dir.medium` accepts
only `"MEMORY"`, `terraform/modules/cloud_run_jobs/main.tf:107`), so the
workspace is a memory-backed **tmpfs** carved out of the container's memory
limit (`apps/common/swarm_common/profiles.py:68`,
`apps/scheduler/scheduler/dispatch.py:153`), and every Job is created on launch
stage **GA** (`terraform/modules/cloud_run_jobs/main.tf:53`,
`apps/scheduler/scheduler/dispatch.py:827`). The GA path **does** support live
migration. That is the CLAUDE.md "Correction (workspace storage)", and the
consequence is that `disk_gib` is a slice of `memory_gib`, not extra capacity.

**Checkpointing is still mandatory**, but not because of live migration. A
worker can still lose its attempt to a quota park-and-exit, a cancellation, a
reconciler reclaim of a stale generation, or an ordinary crash; checkpointing is
what makes any of those cost minutes instead of the whole attempt. Live
migration covers infrastructure moves, not those application-level
interruptions, so it is no ground for relaxing checkpointing.

**Spot stays off on both backends.** Spot Pods cannot use Autopilot extended run
time, so on the GKE side "Spot preferred" is impossible here rather than merely
undesirable (CONTRACT.md invariant 6).

**Zero-idle stays a property of the workload, and mostly of the bill too.**
Cloud Run Jobs costs nothing between executions. The GKE side carries the
cluster management fee (~$0.10/hr list) whether or not a browser task runs;
verify that against the actual bill rather than trusting this sentence.

### 2.2 Isolation: root inside the pod, gVisor underneath

> **Amended 2026-10-01: this section is unbuilt v2 design, not what runs.**
> No profile runs as root and no profile runs under gVisor by default. The agent
> image drops to `USER swarm:swarm`, uid 10001
> (`images/agent-runtime-base/Dockerfile:810`), on both backends, and an agent
> installs into its own user paths (§2.12). gVisor is the `--runtime gvisor`
> render option for GKE pods (`kubernetes/render.py:390`), not the default, and
> Cloud Run Jobs has no runtime class at all. So the boundary this section
> describes, root made safe by a sandbox underneath it, does not exist: today's
> boundary is an unprivileged user inside the backend's own isolation. Do not
> grant root on the strength of the reasoning below; it holds only once a
> profile actually renders the gVisor template.

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

* **MEASURED, and the earlier estimate was wrong by an order of magnitude.**
  This said "~10–15%", quoted from documentation. On our own image, our own
  workloads, same CPU and memory, differing only in `runtimeClassName`:

  | phase | standard | gVisor | cost |
  |---|---:|---:|---:|
  | `find / -type f` | 1,144 ms | 8,932 ms | **7.8×** |
  | `git clone` (depth 50) | 1,297 ms | 7,315 ms | **5.6×** |
  | `npm install` (3 pkgs) | 7,415 ms | 11,826 ms | 1.6× |
  | `uv venv` + 3 installs | 1,991 ms | 2,691 ms | 1.35× |
  | python import | 125 ms | 175 ms | 1.4× |
  | tar round-trip | 586 ms | 672 ms | 1.15× |
  | 2,000 small writes | 131 ms | 163 ms | 1.24× |
  | **total** | **12.7 s** | **31.8 s** | **2.5×** |

  The shape matters more than the total: **~15–25% on bulk I/O, 5–8× on
  metadata-heavy traversal**. Many syscalls moving little data is exactly
  gVisor's weak point, and it is not an edge case here — `find`, `rg`, `fd` and
  `git status` are what an agent runs constantly between model calls.

  **The decision stands, provisionally.** An agent's wall clock is dominated by
  inference, so 19 extra seconds of file work against minutes of waiting may be
  noise. That is a hypothesis, not a finding. **The number that settles it is a
  real `claude-code` agent doing a real task, sandboxed and not** — until that
  exists, this is a known risk carried deliberately rather than a solved
  problem.

* **Kernel confirmed in effect**: `4.19.0-gvisor` against `6.12.94+`. The
  sandbox is not being silently ignored.

* **Nothing in the toolbox broke.** git clone, npm install, uv venv, tar and
  2,000 small writes all returned rc=0 under gVisor. §8.3's question is answered.

* **`microvm` (kata-clh) is advertised by the cluster but will not schedule**:
  `NotTriggerScaleUp`, "20 node(s) didn't match Pod's node affinity/selector".
  Autopilot declares the RuntimeClass and provisions no capacity for it with a
  plain pod spec. Advertised is not usable. Given the 2.5×, this is worth
  revisiting with a compute class — Kata runs a real kernel and should not carry
  this penalty.
* **No GPU under gVisor.** A GPU profile would need a separate non-sandboxed node
  pool and its own trust decision.
* **Docker-in-pod does not work under gVisor.** The `docker` CLI is still in the
  image, but an agent that needs to *build* an image submits to **Cloud Build** —
  which is what this repository already does for its own images, and which needs
  no daemon and no privilege.

### 2.3 Storage: ephemeral per agent, checkpointed to GCS

> **Amended 2026-10-02: what runs.** On Cloud Run Jobs the scratch volume is a
> memory-backed tmpfs carved out of the container's memory limit
> (`apps/scheduler/scheduler/dispatch.py:153`), so `disk_gib` is a slice of
> `memory_gib`, not extra capacity (§2.1 says why). On GKE it is an `emptyDir`
> sized to the resource class (`apps/scheduler/scheduler/dispatch.py:1572`).
> The per-tenant shared cache in the last paragraph is still not built: no
> volume, quota or cleanup policy for it exists in `terraform/` or in either
> dispatcher.

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

> **Amended 2026-10-02 (S7, owner decision): a remote step is an `agent()`
> call.** The form this section rejected "for now" is the one that was built,
> and it is S7's form:
>
> ```js
> phase('Review')
> const results = await parallel(DIMENSIONS.map(d => () =>
>   agent(d.prompt, { agentType: 'sc:remote', label: d.key, schema: FINDINGS })))
> ```
>
> `sc:remote` is the sc plugin's agent (`plugin/agents/remote.md`; usage at
> `plugin/README.md:622`). It dispatches its prompt as one `claude-code` task on
> the session's repository and pushed branch, follows it, and returns the remote
> agent's answer, or, given a `schema`, the JSON object parsed from the end of
> that answer. A schema-mode call can throw when the task does not succeed, so
> give the schema a nullable `state`/`error` or catch it; the plugin README says
> how. Remote steps still mix freely with local `agent()` steps, and each one is
> an ordinary row in `/workflows`. It stays visibly remote: the step names
> `sc:remote`. The plugin-API behaviour this section called unverified is what
> that agent runs on today.
>
> The `swarm.dispatch` / `swarm.collect` shape below survives as the bridge's
> MCP tools for a session that is not running a workflow script:
> `swarm_dispatch` takes one task, or a `tasks` list checked in full and sent as
> one request (`apps/swarm-mcp/swarm_mcp/server.py:1520`), and `swarm_collect`
> gathers the results.

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

> **Amended 2026-10-02: claudeswitch is a reference, not a component.** No pod
> and no service runs claudeswitch. It is not in the agent image, and nothing
> under `apps/` calls it. What the platform took from it is measured knowledge,
> not code: the token endpoint that actually answers a refresh
> (`apps/quota-broker/quota_broker/oauth.py:54`, "verified against
> claudeswitch") and the onboarding constraints recorded in its ground-truth
> notes (`apps/quota-broker/quota_broker/oauth.py:337`). The mechanism column of
> the table below is split between two services. The broker signs accounts in
> (§2.6.1) and refreshes them as the single writer (§7.3). The worker reads an
> account's access token by secret name and gives it to the agent in one
> environment variable (§2.6.3). The code gives two reasons. The worker's
> credential path must be the same on a Cloud Run Job execution and on a GKE
> pod, so it lives in the worker and not in a pod spec. And a token handed over
> in the environment leaves no credential file for claudeswitch's `use` to
> install. The rule against claudeswitch's daemon in a pod still stands; it now
> holds because nothing in a pod runs claudeswitch at all.

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

> **Amended 2026-10-02 (S10): `account add` is the CLI driving the API's OAuth
> flow, not claudeswitch plus a vault upload.** The verbs are
> `sc account add --label <label> [--lend-to <tenant>]`,
> `sc account pause|resume|drain <label>` and `sc account remove <label>`, which
> asks for the label typed back. `sc accounts` is the read-only list. `add`
> (`apps/swarm-mcp/swarm_mcp/sc.py:1112`) runs four steps:
>
> 1. It calls `POST /v1/accounts/authorize`
>    (`apps/swarm-api/swarm_api/routes/accounts.py:283`), which returns the
>    Anthropic sign-in URL and the `state` that keys it.
> 2. It opens the operator's browser at that URL.
> 3. It reads the code the callback page shows, without echoing it.
> 4. It sends that code once to `POST /v1/accounts/exchange`
>    (`apps/swarm-api/swarm_api/routes/accounts.py:310`).
>
> The broker exchanges the code and writes the credential to Secret Manager
> itself. It files the account under the tenant recorded when the sign-in
> began, taken from the verified token and never from the request. The
> operator's machine reads and writes no credential file, and the code and the
> `state` are printed nowhere. The reason is recorded in `cmd_account_add`'s
> docstring (owner decision 2026-10-01): this is the flow the console already
> uses. That leaves one onboarding path, and the broker is the single writer
> from the first token on. The v1 rules in the last paragraph hold unchanged.
> This also answers §8 question 10: what the operator pastes is a one-time code,
> not a token. `drain` sets the account `DRAINING`, so it gets no new
> assignments; moving running agents off it is §2.6.4's question.

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

> **Amended 2026-10-01: there is no init container.** The flow below is unbuilt
> v2 design. What runs: the worker process itself asks the broker for an
> account at start (`apps/agent-worker/agent_worker/lifecycle.py::Worker._lease_account`), gets a
> Secret Manager secret NAME back, reads the value under its own service
> account and shapes it into the agent child's environment
> (`apps/agent-worker/agent_worker/lifecycle.py::Worker._account_credential_env`,
> `apps/agent-worker/agent_worker/accountlease.py::credential_env_from_account`). That is the same on a
> Cloud Run Job execution and on a GKE pod, which is why it lives in the worker
> rather than in a pod spec only one backend has. The broker stays the single
> writer (§7.3); the worker never refreshes.

> **Amended 2026-10-02: the assignment is a broker HOLD, not a field on the
> lease, and there is no credential file.** The broker records each assignment
> as a hold on the account document
> (`apps/quota-broker/quota_broker/accounts.py:352`). A hold carries its own
> id, which the release must name, and it expires on its own, so a SIGKILLed
> worker costs a few stale minutes rather than a count that stays inflated for
> good (`apps/agent-worker/agent_worker/accountlease.py:66`). It is not on the
> LEASE because `Lease` is frozen and has no account field. Recording which
> account an attempt ran on is contract change request 13, still open. The
> token reaches the agent as one environment variable, `CLAUDE_CODE_OAUTH_TOKEN`
> (`apps/agent-worker/agent_worker/accountlease.py:131`), and is never written
> to a credentials file. So the `CLAUDE_CONFIG_DIR` and `MergeForSwap` paragraphs
> below have nothing to act on today.

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

> **Amended 2026-10-02: what was built.** The dashboard is its own Cloud Run
> service, `swarm-ui` (`terraform/infra/main.tf:371`). It differs from this
> section in three places.
>
> * **It reads swarm-api only.** It never reads Firestore directly, nor the
>   Kubernetes API. swarm-api is where the caller's token is verified and
>   every read is scoped to the caller's tenant. A UI holding a privileged
>   identity would move invariant 9 into new code
>   (`terraform/modules/frontend/main.tf:21`).
> * **It polls, with no server-sent events.** The capacity screens re-read
>   every 30 or 60 seconds and pause while the tab is hidden
>   (`apps/swarm-ui/src/capacityPoll.ts:15`).
> * **The front door is an external Application Load Balancer with IAP**
>   (`terraform/modules/frontend/main.tf:1`), not an internal one.
>   `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` is exactly the ingress setting an
>   external ALB in front of Cloud Run needs, so the wall described below was
>   never there. The module is created where `enable_frontend` is set
>   (`terraform/infra/main.tf:492`).
>
> The table below is the design. `docs/web-ui/README.md` records which screens
> exist.

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

> **Amended 2026-10-02: the pool counters are not sharded.** Admission still
> reads and writes whole pool documents in one transaction, the pools named by
> `pool_names_for` (`apps/common/swarm_common/models.py:82`), so invariant 2
> holds exactly as v1 built it. Sharding was deferred, not designed.
> `docs/scaling.md` §5 ranks it third, behind fewer and longer admissions and
> the per-tenant and per-provider pools that already spread the writes, and
> calls for it only "if `global` genuinely saturates". §8 question 6, which
> would show that, has not been measured. Sharding `global` changes the frozen
> `pool_names_for`, so it arrives as a contract change request, not an edit.

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

Added (S34, 2026-10-01): `gh`, `gcloud` (with `gke-gcloud-auth-plugin`),
`kubectl`, `terraform`, `checkov`, `shellcheck`, `make` and the `docker` CLI are
in the default build. `tofu`, `tflint` and `trivy` are installed only with
`--build-arg INSTALL_TOFU_TFLINT_TRIVY=1`: no published release of them passed
the promote gate's fixable-HIGH/CRITICAL scan that day. The pinned versions,
and why two of them differ from the operator pins quoted below, are in
[`versions.md`](versions.md#agent-image-toolbox). The image still runs every
tool as uid 10001; it does not provide root.

Specified (the original 2026 specification, kept as written; where it disagrees
with the paragraph above -- the terraform and tofu versions, and root -- the
paragraph above and `versions.md` are what the image does):

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
(Superseded: the image runs as uid 10001 and provides no root, so an agent
installs into its own user paths -- `uv tool`, `npm` prefix, `pip --user`.)

---

## 3. Architecture

> **Amended 2026-10-01.** The diagram is the v2 design and draws one execution
> box. What is built has two (§2.1): Cloud Run Jobs for `mock`, `generic`,
> `claude-code` and `codex`, and GKE Autopilot for `browser`, routed by
> `BackendRouter.for_backend` (`apps/scheduler/scheduler/dispatch.py:1696`).
> Neither box is "root, sandboxed": the worker runs as uid 10001
> (`images/agent-runtime-base/Dockerfile:810`) and gVisor is opt-in
> (`kubernetes/render.py:390`), see §2.2. There is no credential sidecar; the
> worker leases its account itself (§2.6.3).

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

> **Amended 2026-10-02.** Two items were not built as written.
>
> * **Item 5: there is no sidecar** in either backend's pod. The worker process
>   itself leases the account (§2.6.3) and runs the checkpoint timer
>   (`apps/agent-worker/agent_worker/lifecycle.py::Worker._run_child_supervised`). The reason is §2.6.3's:
>   one worker runs unchanged on a Cloud Run Job execution and on a GKE pod.
>   How events are published is §2.7's question.
> * **Item 12 is not configuration.** The catalogue is the frozen
>   `apps/common/swarm_common/profiles.py`, so a new profile is a contract
>   change request. Requests 33, 35 and 36 added `merge`, `post-verdict` and
>   `claude-code-review` that way. Invariant 10 is the reason: a profile fixes
>   the image, command and resources a caller can never send, so adding one is
>   a reviewed decision, not a setting.

1. **GKE Autopilot substrate, for the browser runner** — cluster, per-tenant
   namespaces, NetworkPolicies, the pod spec, RBAC. gVisor is a render option,
   not the default (§2.1).
2. **Pod dispatcher** beside the Cloud Run Jobs dispatcher, not replacing it
   (amended 2026-10-01, §2.1): `GkeJobDispatcher` takes `GKE_AUTOPILOT`
   profiles and `CloudRunJobDispatcher` keeps the rest.
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

Nothing, as amended 2026-10-01. This section listed the Cloud Run Jobs backend
for deletion: its Terraform module, its scheduler dispatch path, the
`swarmJobDispatcher` and `swarmJobReaper` custom roles, and the `Backend`
enum's Cloud Run member with every branch on it. The owner kept Cloud Run Jobs
as the primary backend instead (§2.1), so all of those stay, and are NOT
deleted. They carry four of the five profiles.

The original argument — a backend nobody exercises is a backend that rots —
still holds, and is why the GKE path is not dormant either: the `browser`
profile exercises it on every browser task.

The control-plane services stay on Cloud Run, and so does most execution.

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

> **Amended 2026-10-02 (S35, S36, owner decision): rows 3b, 4 and 5 are not
> planned.** Each one measures something only an agent fleet on GKE needs:
>
> * row 3b: whether `microvm` beats gVisor;
> * row 4: whether a real `claude-code` agent pays gVisor's 2.5×;
> * row 5: Autopilot cold start for short agent tasks.
>
> The work to take them, the GKE benchmark lane, was removed when the owner
> kept Cloud Run Jobs primary (2026-10-01, §2.1). `claude-code` runs on Cloud
> Run Jobs, and no profile renders the gVisor template by default
> (`kubernetes/render.py:390`). Row 5 is scoped to `claude-code` and the agent
> fleets: the `browser` profile does run on Autopilot today
> (`apps/common/swarm_common/profiles.py:1187`), for its `/dev/shm`, and accepts
> its cold start, which is not what row 5 asked. If an agent profile ever moves
> to GKE, these rows come back with it.

| # | Question | Invalidates if wrong |
|---|---|---|
| 1 | ~~Where does the CLI read credentials on Linux?~~ **ANSWERED:** `~/.claude/.credentials.json` (relocatable via `CLAUDE_CONFIG_DIR`), 0600, plaintext JSON | §2.6.3 resolved |
| 1b | ~~Is an external rewrite picked up mid-session?~~ **ANSWERED: YES.** claudeswitch sends no signal at all and the CLI picks it up; the plaintext store re-reads the file on every read | §2.6.4 hot-swap confirmed; checkpoint-and-resume demoted to fallback |
| 2 | ~~What is `stream-json`'s schema?~~ **ANSWERED:** captured in full — see §2.7 | §2.7, §2.9 signals 2 and 4, the dashboard |
| 3 | ~~Does the sandbox break any tool?~~ **ANSWERED: no.** Every phase rc=0 under gVisor | §2.2 resolved |
| 3c | ~~Real gVisor overhead?~~ **ANSWERED: 2.5× overall, 5–8× on file traversal** — not the 10–15% assumed | §2.2 — decision held, but now on an explicit hypothesis that agents are inference-bound |
| 3b | **NEW: `microvm` (kata-clh) is also on the cluster.** A real VM with a real Linux kernel: a STRONGER boundary than gVisor, usually FASTER on syscall-heavy work, at a declared 250m CPU + 130Mi per pod | §2.2 was decided before anyone knew this existed. The benchmark settles it |
| 4 | Does a REAL agent actually pay that 2.5×, or is it lost in inference latency? | §2.2 — the only measurement that settles root-in-pod |
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

* ~~**Cloud Run Jobs as the primary backend** → GKE Autopilot as the only
  backend.~~ **Declined 2026-10-01 (owner):** Cloud Run Jobs stay primary and
  the contract's "Primary backend" line stands, now naming each profile (§2.1).
* ~~**"No nodes, no autoscaler, no node upgrades to evict"** → false under v2.~~
  Still true for the four Cloud Run profiles; it was only ever false for the
  browser runner on GKE, where the guarantee is extended run time, a weaker and
  more operationally demanding promise.
* **Zero idle cost** → zero idle *workload*. A cluster fee exists for the GKE
  side (§2.1).
* ~~**Non-root, read-only rootfs, dropped capabilities** → root, writable rootfs,
  gVisor. The defence moved down a layer; it did not disappear, and it is not the
  same defence.~~ **Not requested (amended 2026-10-02):** nothing runs as root,
  so the contract line stands. The agent image drops to `USER swarm:swarm`,
  uid 10001 (`images/agent-runtime-base/Dockerfile:810`). The GKE pod and its
  container keep non-root, a read-only root filesystem and every capability
  dropped (`apps/scheduler/scheduler/dispatch.py:192`,
  `apps/scheduler/scheduler/dispatch.py:205`). gVisor is the opt-in
  `--runtime gvisor` render (`kubernetes/render.py:390`). The renderer refuses
  that render for a namespace still at Pod Security `restricted`
  (`kubernetes/render.py:826`), because restricted forbids root, so §2.2's
  trade is not made anywhere today.
* **One credential per tenant per provider** → a pool of accounts per tenant, one
  held per agent at a time, swappable mid-run. **Amended 2026-10-02:** the pool
  is built, without editing the contract, and the per-tenant credential is
  still the floor.
  * The pool is per tenant, with explicit lending to named tenants
    (`apps/quota-broker/quota_broker/accounts.py:4`).
  * The worker holds one account per attempt
    (`apps/agent-worker/agent_worker/accountlease.py:3`). When the broker cannot
    be reached, the worker falls back to the tenant's own
    `swarm-tenant-<tenant>-<provider>` secret, so the platform never needs this
    amendment to run.
  * Nothing in `swarm_common` was edited. The assignment lives in the broker's
    holds (§2.6.3), and recording it on the attempt is request 13, still open.
  * No mid-run swap exists in the worker today (§2.6.4).

Nothing in `apps/common/swarm_common/` is edited by this document.

---

## 10. Sequencing

Ordered by what unblocks what, not by what is most interesting.

1. **Spikes 1, 2, 3** from §8. Two of them can invalidate whole components.
2. **Autopilot substrate** — cluster, gVisor, namespaces, RBAC, NetworkPolicies.
3. **Pod dispatcher**, with the existing profile catalogue unchanged.
4. ~~**One real `claude-code` agent in a pod**, end to end.~~ Withdrawn
   2026-10-01: `claude-code` stays on Cloud Run Jobs, where v1 already reached
   this milestone (§2.1).
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
