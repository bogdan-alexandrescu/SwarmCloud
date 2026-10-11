# Live browser: watch a browser agent work, and take control to sign in

**Status: design (2026-10-11). Nothing in it is built.** It answers the owner's
request of 2026-10-11: click a running browser agent in the console, see its
browser as it moves across pages, and **take control** -- chiefly so a person
can step in when the agent needs someone to sign in -- then hand control back
so the agent carries on. The owner decided four things in the same request
(section 2). The rest of this document is the design built on them, with the
options turned down kept on record, and with the open decisions written to
`questions.json` (section 10).

It builds on the `claude-code-browser` runner profile that lane PROFILE
(wf_f4fbad03) is adding in parallel: the claude-code agent on the
`agent-runtime-browser` image (Playwright and Chromium), GKE Autopilot only.
That profile is not on main at `124a1e3`, where every citation below was read.
Where this design needs something from it, section 8 says so.

Citations name a symbol as `path::qualname`. Lines move; symbols are what to
search for.

---

## 1. Why this is harder than a screencast

Showing a picture of a browser is easy. Three facts about this platform make
the requested feature, an **interactive** browser that a person types a
password into, a security design first and a streaming design second:

* **The agent and the worker are the same uid, in the same pod.** The worker
  says so where it reaps escaped processes before a publish
  (`apps/agent-worker/agent_worker/procman.py` (`The agent and the worker run as the`)), and the egress policy says the network namespace is shared too
  (`kubernetes/network-policies/allow-egress.yaml` (`WHAT RULE 2 COSTS`)).
  Anything the platform runs beside the agent -- an X server, a VNC server, a
  debugging port -- is reachable by the agent and by any process it escapes
  into, unless the design puts a real boundary between them. A localhost-only
  VNC server is **not** a boundary in a pod: localhost is the agent's too.
* **X11 is a keylogger's dream.** Any client that can connect to an X display
  can read every key pressed on it (XRecord, XInput2) and copy every pixel.
  If the agent can reach the display while a person types a password, the
  agent has the password. So the agent must not be able to reach the display at
  all, which is why Chromium cannot simply be launched by the agent's
  Playwright on a `DISPLAY` the agent can see (section 3.2).
* **The Chrome DevTools Protocol (CDP) tells its client everything.** The agent
  drives the browser over CDP. CDP delivers network events with POST bodies (a
  login form's password among them), lets a client read any input's value,
  and lets it install script on every page. An agent that keeps its CDP
  connection while a person signs in can read the password after the fact,
  even if it saw no frame. So the agent's CDP connection has to pass through a
  gate the platform owns, and that gate has to change behaviour while a person
  is in control (section 5.4).

And one fact about the product: **the person typing is trusting the agent's
browser with their own credentials, on a page the agent chose.** A prompt
injection that sends the agent to a look-alike sign-in page and then asks for
help is phishing with the platform's console as the delivery mechanism.
Section 5.7 is about that.

---

## 2. The owner's decisions (2026-10-11)

| Decision | What was decided | Where this document answers it |
|---|---|---|
| **LB-1** | An **interactive** remote browser: headed Chromium on a virtual display, VNC proxied through the platform into the console via noVNC, with a "take control" button. Not a view-only screencast. | §3, §4, §6 |
| **LB-2** | Frames are visible **only to members of the task's tenant**, checked on every connection (invariant 9). | §5.1, §5.2 |
| **LB-3** | Password and secret fields are **masked before anything leaves the pod**. | §5.3 |
| **LB-4** | Frames and recordings are kept **7 days**, under the tenant's GCS prefix. | §7 |
| **LB-5** | The console presentation is **a Browser tab on the agent page, with theater mode available from it** (owner's choice, 2026-10-11, from five in-console alternatives; recorded on #1030). This replaces the earlier phased plan of separate split, picture-in-picture and wall views. | §6 |
| **LB-6** | The browser can run **as a mobile device, not only desktop**, and a person in control works it **by touch**. Presets: Desktop 1440×900, iPhone 15 393×852, Pixel 8 412×915, iPhone SE 375×667, iPad (gen 7) 810×1080, with rotate. | §6.1 |
| **LB-7** | **Signed-in sessions are reusable, but only inside a person's own personal workspace** (2026-10-11, revising Q7). In a personal tenant (`u-<user>`), a session signed in once carries to that workflow's later browser steps and is destroyed when the workflow ends. The person can also save it as a **named browser login** of that workspace, and a task in that workspace opts in by name. **Group tenants (e.g. `eng`) never store or carry a session**: every browser step there signs in fresh. The person's own workspace identity enforces this by IAM, because every worker of a group tenant shares one identity and could otherwise read another member's session. Any account may be saved, at the person's judgement. | §5.5, §5.5.1 |

The brief also described a shape: "a VNC server bound to localhost only" and
"Chromium launched by the agent's Playwright on that display", with "swarm-api
proxying per-task websockets". This design keeps the intent of each and changes
the mechanism of the first two, for the reasons in section 1. Each change is
in section 3 with the option it replaced, and the one the owner may want to
overrule is a question (Q9, Q12).

---

## 3. Architecture

```
 GKE pod (tenant namespace swarm-tenant-<t>)                                   swarm-system            Cloud Run          browser
 +-------------------------------------------------------------+
 | container `worker` (uid swarm)  | native sidecar `display`  |
 |                                 | (uid swarm-display)       |
 |  worker (non-dumpable)          |  Xvfb :0 (unix socket,    |
 |   |  attempt key (heap)         |    sidecar-private /tmp)  |
 |   |                             |  Chromium (headed, on :0, |
 |   |  ctl.sock (MAC'd, first     |    profile on a private   |
 |   +---registration wins)------->|    memory emptyDir)       |
 |                                 |  live gate:               |
 |  agent (claude-code +           |   capture -> MASK -> encode|
 |  Playwright, connectOverCDP)    |   input injection (XTEST) |    outbound WSS,
 |   +--- 127.0.0.1:9222 --------->|   CDP proxy  <-pipe-> Chromium   ticket from      +-----------+      +-----------+     +---------+
 |                                 |   tunnel client ---------------- swarm-api ----->| swarm-live|<---->| swarm-api |<--->| console |
 |                                 |                           |    (TCP 8443)        | relay     |      | (IAP, per-|     | noVNC   |
 +-------------------------------------------------------------+                      | rendezvous|      | connection|     +---------+
                                                                                      | + recorder|      | tenant    |
                                                                                      +-----------+      | check)    |
                                                                                                         +-----------+
```

### 3.1 In the pod: a display sidecar with its own uid

The `display` container is a Kubernetes **native sidecar** (an init container
with `restartPolicy: Always`) running the same `agent-runtime-browser` image
with a different entrypoint and a **different uid**. It holds three things:

1. **Xvfb** on display `:0`, listening on a unix socket in the sidecar's own
   `/tmp` only (`-nolisten tcp`), at a fixed 1280x800x24. The worker
   container does not mount that directory, so nothing in it, the agent
   included, can open the display.
2. **Chromium, headed, on that display**, launched by the gate with
   `--remote-debugging-pipe` (no debugging port at all), a user-data directory
   on a memory-backed `emptyDir` mounted into the sidecar only, and the
   enterprise policies of section 5.6. One window, maximised, with its address
   bar visible (no `--kiosk`, no `--app`): the address bar is part of the
   anti-phishing design.
3. **The live gate**, one process that: captures the framebuffer, masks it
   (section 5.3), encodes it as RFB, injects input when and only when a person
   holds control, exposes a **CDP proxy** on `127.0.0.1:9222` for the agent,
   and opens the outbound tunnel. Frames are produced inside the sidecar and
   leave it only through the tunnel, already masked. They never pass through
   the worker container.

**Why a sidecar and not processes in the worker container.** A separate uid is
the only boundary available inside one pod without gVisor or a second pod.
Same-uid processes can read each other's `/proc/<pid>/environ`, signal each
other, and open each other's sockets and files; a different uid in a different
container, with no shared mount for the display, cannot. The worker container
keeps its existing `securityContext`; the sidecar runs as a second non-root uid
(`runAsUser` distinct from the worker's), drops all capabilities, and mounts
only: its own `/tmp`, the browser profile volume, `/dev/shm`, and the two
shared volumes below.

**Why native and not an ordinary second container.** A Job's pod completes when
its containers exit. An ordinary second container that keeps running holds the
Job open after the worker exits, so the attempt would never end. A native
sidecar is terminated after the main container exits, and starts before it, so
the gate is up before the worker's lifecycle begins (and therefore before the
agent exists -- section 5.4 depends on that ordering).

**Shared volumes, and only these.** `downloads` (sidecar writes, worker reads:
Chromium saves downloads there and the worker moves them into `work/`) and
`uploads` (worker writes, sidecar reads: `setInputFiles` paths must exist on
Chromium's side), plus `/run/swarm-gate` holding `ctl.sock`. All three are
memory `emptyDir`s, so they are inside the pod's memory limit, and none is
under `work/`, so none is checkpointed.

### 3.2 Chromium is launched by the platform, and the agent connects to it

The brief said "Chromium launched by the agent's Playwright on that display".
The design launches Chromium from the gate and has the agent's Playwright
**connect** to it (`chromium.connectOverCDP("http://127.0.0.1:9222")`, or the
Playwright MCP server's `--cdp-endpoint`, whichever lane PROFILE settles on).
Four reasons, each a requirement elsewhere in this document:

* **The agent must not reach the display** (section 1). An agent that launches
  a headed Chromium needs `DISPLAY` and the X socket, which is exactly the
  keylogging channel.
* **The mask needs its own CDP session** to learn where the password fields
  are (section 5.3). The gate holds Chromium's pipe; the agent gets the proxy.
* **The pause needs a choke point** (section 5.4). With the proxy between the
  agent and Chromium, "the agent's actions are paused" is enforced, not asked.
* **The browser profile must live where the agent's files do not** (section
  5.5): a sidecar-only volume, never in `work/`, wiped at attempt end.

What the agent loses is the choice of browser flags. `claude-code-browser`
does not need them; the profile's agent instructions point Playwright at the
CDP endpoint. **A Chromium the agent starts itself, headless, is not shown and
cannot be prevented**: the live view shows the platform's browser. The worker
sets no `DISPLAY` in the agent's environment (`Workspace.child_env`,
`apps/agent-worker/agent_worker/workspace.py::Workspace.child_env`), so a headed launch fails
loudly rather than drawing somewhere nobody sees.

### 3.3 The path to the console: outbound only, through a relay

No inbound port on the pod and no public endpoint: the gate **dials out**.

```
gate --(WSS, TCP 8443, in-cluster)--> swarm-live <--(WSS, internal)-- swarm-api <--(WSS via IAP LB)-- console
```

1. **The worker asks swarm-api for a tunnel ticket** (`POST
   /v1/attempts/{attempt_id}/live-session`), signed with the attempt key
   (section 5.4.1). swarm-api checks the fence -- the task is `RUNNING` under
   this lease and generation -- that the task's profile is a live-browser
   profile, and that the tenant is the task's, then writes
   `live_sessions/{session_id}` and returns a one-use ticket, an HMAC under
   `swarm-live-key` (held by `swarm-api` and `swarm-live` only, value created by
   `scripts/create-secrets.sh`, never a Terraform secret version) over
   `(session_id, tenant_id, task_id, attempt_id, generation, role=tunnel, exp)`,
   valid 60 seconds.
2. **The worker hands the ticket to the gate** over `ctl.sock` (section 5.4.2),
   and the gate opens the tunnel to `swarm-live` with the ticket as its first
   message. The ticket never sits in an environment variable, a file or
   `work/`.
3. **The console never talks to the relay directly.** It opens
   `wss://<console host>/v1/tasks/{task_id}/live` through the existing IAP
   load balancer (the same origin the console already uses for `/v1`;
   `apps/swarm-ui/README.md`: "IAP sits in front"). swarm-api authenticates the
   connection exactly as every route does (`Authenticator.authenticate`,
   `apps/swarm-api/swarm_api/auth.py::Authenticator.authenticate`), checks the tenant and the role
   (section 5.1), mints a viewer ticket for itself, and proxies the websocket
   to `swarm-live` over the internal path.

#### Why a relay at all, and why this shape

The meeting point cannot be swarm-api alone. swarm-api is a Cloud Run service
with `max_instances` above one (`terraform/infra/main.tf`, the `swarm-api`
service), a 300-second default request timeout
(`terraform/modules/cloud_run/variables.tf` (`request_timeout`)) and one CPU. The
gate's tunnel lands on one instance and the viewer's websocket on another, and
Cloud Run instances are not addressable, so the two cannot be joined there.

| Option | What it is | Verdict |
|---|---|---|
| **R1** relay inside swarm-api | both ends connect to swarm-api | **Rejected.** No rendezvous across instances; frames on a 1-CPU API. |
| **R2** swarm-api fronts, `swarm-live` rendezvous | console -> IAP -> swarm-api (auth, tenant, role) -> `swarm-live`; gate -> `swarm-live` | **Recommended (Q9).** One public surface, and the one authenticator, so the tenant check is not restated in a second service. Costs a second hop and a swarm-api instance held per viewer; a viewer reconnects when Cloud Run's request timeout ends the socket (noVNC reconnects on its own; the `/live` route asks for the 3600-second maximum). |
| **R3** console -> `swarm-live` directly | its own IAP backend, swarm-api only mints tickets | Kept for scale: no Cloud Run hop. Costs a second IAP backend and an IAP-assertion verifier in `swarm-live`, a second copy of the auth rule this repository has learned not to keep twice. |
| **R4** Pub/Sub or Redis fan-out between swarm-api instances | | Rejected: latency on keystrokes, a new stateful dependency. |

**`swarm-live` is a single-replica StatefulSet in `swarm-system` on the GKE
cluster, not a Cloud Run service.** The relay's one job is that both ends of a
session meet in the same process. A StatefulSet of one replica guarantees at
most one pod; a Cloud Run service with `max_instances = 1` does not (two
revisions overlap during a rollout, and the two ends can land on different
revisions). A relay restart drops every live session for the seconds it takes
to come back; both ends reconnect, and nothing about the task changes because
the relay holds no task state. Scale-out, when measured to be needed, is sharding by
`session_id` across named replicas with the shard written on the session
document.

**What the relay is not.** It is not trusted with authorisation: it verifies
tickets it did not mint against a key it shares only with swarm-api, and it
refuses a ticket whose tenant, task, attempt or generation does not match the
session's. It has no Firestore access except the session document and the
audit collection (section 5.8), and its only GCS grant is object creation under
the `live/` key of an attempt (section 7).

### 3.4 In the console: noVNC on the agent's page

The agent's detail view (`apps/swarm-ui/src/AgentDetail.tsx`) gains a **Live
browser** panel for a task whose profile is a live-browser profile and whose
state is `RUNNING`. It embeds noVNC (`@novnc/novnc`, pinned), which draws
**pixels into a canvas**. Above the canvas, outside it, the console draws a
strip the page cannot influence: the top frame's real origin as Chromium
reports it to the gate over CDP, the handoff reason, who holds control, and the
buttons (Take control / Return control / Let any member take control).

Pixels are a deliberate choice. A DOM-streaming viewer (rrweb and the like)
would render the agent's page's HTML inside the console's origin: an XSS
surface for every page an agent visits, and a DOM snapshot that carries input
values. noVNC renders nothing a page wrote.

---

## 4. The hand-off protocol

### 4.1 The agent asks for help

```
agent: swarm-handoff request --reason "Sign in to GitHub as the bot account" --expect github.com
agent: swarm-handoff wait            # blocks up to 9 minutes, then says "still waiting" (exit 75)
```

`swarm-handoff` is a small command in the browser image. Like the child-task
spool (`docs/design/child-tasks.md` (`**Spool, not a localhost endpoint`)) it
**writes a file** -- `$SWARM_HANDOFF/requests/<id>.json`, atomically -- and
opens no socket, for the same reasons: no new listener in the agent's reach,
and the request survives a checkpoint. `wait` blocks for at most 9 minutes
because claude-code's Bash tool kills a command at 10 (`#736`, recorded in
`apps/agent-worker/agent_worker/runners/stdin_hook.py`); it exits

| exit | meaning | what the agent should do |
|---|---|---|
| 0 | control was returned; a JSON summary is on stdout (section 5.9) | look at the page and continue |
| 75 | still waiting | call `wait` again |
| 3 | the wait expired; the worker is checkpointing and parking | write its notes into `work/` and stop; the worker ends it either way |
| 4 | the task is being cancelled | stop |
| 5 | refused (rate limit, no live path, reason invalid) | carry on without help, or fail with that reason |

The **worker** picks the request up on its control poll (10 seconds,
`WorkerConfig.control_poll_seconds`,
`apps/agent-worker/agent_worker/config.py::WorkerConfig`) or sooner by inotify, and:

1. validates it: `reason` at most 200 characters, passed through the
   redaction filter before it is stored or shown (`redact`,
   `apps/redaction/swarm_redaction/rules.py::redact`); `expect` a host that
   `url_refusal` (`apps/common/swarm_common/profiles.py::url_refusal`) accepts;
   at most `max_handoffs_per_attempt` (default 3);
2. tells the gate to **pause the agent's CDP** (section 5.4.3), so from this
   moment the agent cannot act on the browser, cooperative or not;
3. records the wait through a worker route signed with the attempt key, `POST
   /v1/tasks/{task_id}/handoff`. swarm-api sets the task's `human_wait`
   (`{reason, expect, requested_at, deadline, controller: null}`), writes a
   `human_handoff_requested` event, and raises the console banner.

The task **stays `RUNNING`** (section 4.3 says why that is the recommendation,
and Q2 asks the owner).

**Notification.** Phase 1 notifies in the console only: a banner on the
Overview's "Needs a look", a badge on the agent's row, and the browser tab's
title. The platform has no outbound notification channel today (no mail or
chat integration in `apps/swarm-api` or `apps/reconciler`), so "optional
notification" is a new integration, and Q8 asks whether it is wanted.

### 4.2 Take control, sign in, return control

1. **Take.** An eligible person (section 5.1) presses Take control. swarm-api
   runs one Firestore transaction on `live_sessions/{session_id}`: no current
   controller (or the caller may preempt, section 5.1), then sets
   `controller = {email, since, renew_by}` and appends to `live_audit` in the
   same transaction. It tells the relay, which tells the gate. The gate enters
   **control mode**: input from that one viewer connection is injected through
   XTEST; every other viewer's input is dropped; the agent's CDP stays paused.
   One controller at a time, always.
2. **Sign in.** The person sees the address bar and the console's origin strip
   (section 5.7), types, and completes the sign-in. Keys travel console ->
   swarm-api -> relay -> gate -> Xvfb, inside TLS on every hop, and no hop logs
   their content (section 5.3.3).
3. **Return.** The person presses Return control. Before the gate resumes the
   agent's CDP it **clears every password input** in every frame through its
   own CDP session (a page left on a half-submitted form must not hand the
   value to the agent), restores the agent's page scripts (section 5.4.3),
   and then lets the agent's queued commands through. swarm-api clears
   `human_wait`, writes `human_control_returned` and `human_handoff_resolved`,
   and the agent's `wait` exits 0.

Control also ends without a return press, each case audited with its cause:
the controller's console stops renewing for 60 seconds (`renew_by` passed),
the controller is idle for `control_idle_seconds` (default 300; no input
event), the controller is preempted (section 5.1), their membership lapses
(section 5.2), the task is cancelled, or the hold expires.

**A person may also take control when the agent did not ask** (Q6). The gate
pauses the agent's CDP the same way. The agent's in-flight Playwright calls
block, and may time out and fail; on return the agent sees a failed or slow
action and looks again. It is not told why unless it calls `swarm-handoff
status`, which reports the last control episode in the same shape as `wait`.

### 4.3 The bounded wait, and what it costs

There are two phases, and the boundary between them is where capacity is
given back.

```
RUNNING (human_wait set, lease HELD) --hold expires, nobody in control-->
   worker: checkpoint("human-wait"), upload outputs, wipe browser,
           park(HUMAN_REQUIRED), bounded attempt refund, release lease, exit
PARKED (HUMAN_REQUIRED, NO capacity) --a person presses "I'm ready to sign in"-->
READY --admission--> LEASED -> ... -> RUNNING (new attempt, new generation)
   worker restores work/, starts the agent; console shows "starting your browser"
   agent re-opens the page and asks again; the person is already there
PARKED (HUMAN_REQUIRED) past human_park_max_seconds --> DEAD_LETTERED, end_cause human_wait_expired
```

**Phase H, the hold: `RUNNING` with `human_wait` set, lease held, default 10
minutes (`human_hold_seconds`, Q3).** The browser and its half-finished state
are alive, which is the whole point of the feature: a sign-in has to happen in
the browser that will use the session. Capacity is exactly what it is for any
`RUNNING` browser task: the lease holds the `browser` resource class's 2 units
and the backend and provider pools
(`apps/common/swarm_common/profiles.py::RESOURCE_CLASSES`), and the pod costs 8
vCPU and 16 GiB per minute, idle or not, because `requests == limits`
(invariant 7). The hold is bounded so that an unattended request costs ten
pod-minutes, not an afternoon. A person who is **in control** extends nothing
past the hold except while they are actively in control (renewed, not idle),
up to the attempt's own timeout: the hold's deadline is
`min(requested_at + human_hold_seconds, attempt deadline)` and taking control
moves it to `min(now + control_idle_seconds, attempt deadline)` on every input,
so a person mid-sign-in is not parked under their fingers, and a person who
walks away is.

**Phase P, the park: `PARKED` with `ParkReason.HUMAN_REQUIRED`, no capacity.**
Invariant 4 forbids sleeping through a long wait, and a person's lunch is a
long wait. The worker parks exactly as the quota path does
(`Worker._park_for_quota`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._park_for_quota`;
`ControlPlane.park`, `apps/agent-worker/agent_worker/control.py::ControlPlane.park`): checkpoint,
upload, park, release, exit. `RUNNING -> PARKED` is already legal
(`apps/common/swarm_common/states.py::_ALLOWED`). The park cannot reuse an
existing reason: the scheduler promotes the existing ones by its own rules (a
cooldown ends, a dependency finishes), and nothing but a person should promote
this one. `MANUAL_PAUSE`, the obvious candidate, was considered and rejected
for exactly that reason: the scheduler returns it to `READY` once what paused
it clears (`Scheduler._promote_manual_pauses`,
`apps/scheduler/scheduler/loop.py::Scheduler._promote_manual_pauses`), so a hand-off parked under it
would be re-admitted with nobody there to sign in. So it is a new `ParkReason` (contract request LB-A, section 8).

**What the park costs.** The browser is gone: its session, its tabs, its
history (section 5.5 says why they are not checkpointed). The claude-code
agent has no conversation resume (`docs/design/child-tasks.md` (`the CLI runners have no conversation resume`)), so the next attempt starts the agent again on
the restored `work/`; the agent must keep its plan there, which
`claude-code-browser`'s instructions say. And the person waits for a cold start
-- admission plus a GKE pod start -- before the browser is back. That is the
price of giving the capacity back, and it is paid only when nobody came within
the hold.

**Promotion is a person's.** `POST /v1/tasks/{task_id}/human-ready`
(eligible as for taking control, section 5.1) moves `PARKED(HUMAN_REQUIRED) ->
READY` and records that a person is waiting, so the next attempt's worker opens
the hand-off immediately and the console says "starting your browser". No
sweep promotes it. A sweep beside the scheduler's other park sweeps
(`Scheduler._promote_dependencies`,
`apps/scheduler/scheduler/loop.py::Scheduler._promote_dependencies`) dead-letters one parked
past `human_park_max_seconds` (default 24 hours, Q4) with `end_cause:
human_wait_expired`. `PARKED -> DEAD_LETTERED` is already legal; `PARKED ->
FAILED` is not, and adding it is not worth a contract change for a label.

**The park does not spend an attempt, up to a bound.** Admission increments
`attempt_count` on every lease
(`apps/common/swarm_common/admission.py::acquire_lease_in_transaction`). The park's fenced
transaction refunds one and counts `metadata.human_wait_resumes`, while fewer
than `max_human_resumes` (default 2) are used -- the shape the child-task await
uses (`docs/design/child-tasks.md` (`The await does not spend an attempt, up to a bound.`)). `human_wait_resumes` joins `RESERVED_METADATA_KEYS`
(`apps/swarm-api/swarm_api/validation.py::RESERVED_METADATA_KEYS`).

**Why not fail on timeout.** It is simpler, and it throws away the agent's work
because a person was in a meeting. **Why not hold indefinitely.** Invariant 4,
and an idle 8-vCPU pod.

#### Invariants 1-3, stated for the waiting pod

* **Invariant 1.** A holding pod is `RUNNING` and is real demand: it exists, so
  it is counted. A parked task is `PARKED` and creates none. No new state
  creates demand.
* **Invariant 2.** Unchanged. The re-admission after `human-ready` reserves
  every pool in one transaction, like any other.
* **Invariant 3.** Unchanged. The hold is counted from `LEASED` because it is
  inside an attempt that was leased; nothing about it is counted from
  `RUNNING`.

#### Why a field and not a new task state (Q2)

The brief suggested a state, `WAITING_FOR_HUMAN`. A state that holds a pod
must join `CONCURRENCY_STATES`
(`apps/common/swarm_common/states.py::CONCURRENCY_STATES`), get transitions to and from
`RUNNING`, `PARKED`, `READY`, `FAILED` and `CANCELLED`, and then every reader
that asks "is this attempt running?" must learn the new answer: the
reconciler's heartbeat and stale-generation checks, the worker's fence checks
in `control.py`, release, the UI's state lists and the contract-parity script
that holds shell restatements to the Python
(`scripts/lib/check-contract-parity.sh`). A reader that is missed treats a
holding task as not running and reclaims it, or as not holding capacity and
oversubscribes the pool -- invariant 3's exact failure. The field
(`Task.human_wait`) carries the same information to the console and to `sc
status`, and changes no reader that does not want it. The console shows
"Waiting for you" either way.

### 4.4 Cancellation while waiting

* **During the hold.** `request_cancel`
  (`apps/swarm-api/swarm_api/store.py`) sets `cancel_requested` as for any
  capacity-holding task. The worker sees it on its next control poll
  (`ControlSignals.cancel_requested`,
  `apps/agent-worker/agent_worker/control.py::ControlSignals`), tells the gate to end control
  mode, close the tunnel and wipe the browser; swarm-api closes the session
  (viewers see "cancelled"), revokes the controller with cause `cancelled`,
  and the worker takes its ordinary cancellation path. The agent's `wait`
  exits 4.
* **During the park.** `PARKED -> CANCELLED` is a direct API transition today.
  The session is already closed; nothing else holds anything.
* **A person in control when the cancel lands** loses control at once, and the
  console says why.

---

## 5. Security

### 5.1 Who may watch, and who may control

| Who | Watch | Take control | Why |
|---|---|---|---|
| A member of the task's tenant | **yes** | only if the submitter allowed it for this task | LB-2. Watching shows masked pixels of work the tenant already owns. |
| The task's submitter (`Task.submitted_by`, `apps/common/swarm_common/models.py::Task`) | yes | **yes**, and may preempt another controller | The person whose task it is; the credentials typed are theirs or their team's. |
| A workflow step's or schedule's task with a non-human submitter | yes | the workflow's or schedule's human owner; if there is none, any tenant member | A service account cannot press a button. |
| A platform admin who is not a tenant member | **no** | no | Invariant 9 and LB-2 say tenant members only. No break-glass in phase 1 (Q5). |
| Anyone else | no | no | |

**Recommendation (Q1): control defaults to the submitter.** A tenant may be a
whole team; a teammate taking control of someone else's agent and typing
credentials into it is a decision the submitter should make. The submitter can
open control to any member of the tenant for this task with one toggle,
audited. "Any member may control" as the default is simpler and is the
alternative offered.

### 5.2 Every connection is checked, and keeps being checked

On every websocket connection to `/v1/tasks/{task_id}/live`, swarm-api:

1. verifies the IAP assertion and resolves the tenant with the same
   `Authenticator` every route uses, so the Cloud Identity constraint applies
   unchanged: `searchTransitiveGroups` 403s in this project, so membership is
   checked **per registered group**, with `x-goog-user-project` on every call
   (`apps/swarm-api/swarm_api/groups.py::CloudIdentityGroups`);
2. reads the task and refuses unless `task.tenant_id == ctx.tenant_id` **and**
   the tenant document's principal matches `ctx.tenant_principal` (two groups
   with the same local part slug to the same `tenant_id`;
   `AuthContext.tenant_principal`, `apps/swarm-api/swarm_api/auth.py::AuthContext`). A
   refusal is a 404, the same answer as a task that does not exist, so a
   foreign task id is not confirmed;
3. mints a one-use viewer ticket (`role = viewer`, the caller's email, the
   session, the generation, 30-second expiry) and presents it to the relay,
   which refuses a ticket used twice, expired, or naming another session.

**Membership is re-checked while the socket is open.** swarm-api re-runs step 1
every 5 minutes for each proxied socket and closes it when the answer changes.
Group answers are cached for 120 seconds (`CloudIdentityGroups`, `ttl_seconds`),
so a person removed from the tenant's group loses the view within about 7
minutes, and loses control at the same moment. A taking of control always runs
a fresh check, not the cached one.

**Nothing secret travels in a URL.** Load balancer, IAP and Cloud Run request
logs record URLs. Tickets travel as the first websocket message; the URL
carries only the task id.

### 5.3 Masking before anything leaves the pod (LB-3)

#### 5.3.1 What is masked

The gate paints an opaque box, in the framebuffer, before encoding, over:

* every `input` whose type is or **has ever been** `password` on this page (a
  "show password" toggle switches the type to `text`; the set is sticky), and
  every input with `autocomplete` of `current-password`, `new-password`,
  `one-time-code`, `cc-number` or `cc-csc`;
* every element whose visible text matches a rule of the platform's redaction
  set (`apps/redaction/swarm_redaction/rules.py::RULES`): an API key on a dashboard, a
  token in a page;
* the address bar, when the top frame's URL matches a redaction rule (a token
  in a query string).

Password characters are already drawn as dots; the mask hides what dots do
not: the length, a revealed value, and secrets that are not in password fields
at all.

#### 5.3.2 How, and why it fails closed

The gate's own CDP session (on the pipe, invisible to the agent) creates an
**isolated world** in every frame (`Page.createIsolatedWorld`), which page
scripts and the agent's own world cannot see. A script there watches the
sensitive elements (mutation and resize observers) and reports their rectangles
and the visible text nodes to the gate. The gate runs the redaction rules on
that text **in Python, by importing `swarm_redaction`** -- not a JavaScript or
Go restatement of the rules, which is exactly the kind of second copy that has
drifted here before. It maps page rectangles to screen coordinates from the
window's bounds, and paints.

A rectangle report is always slightly older than the frame it is painted on.
So the gate **fails closed**:

* a frame is masked only with a report taken after the last layout-changing
  event (scroll, resize, navigation, a mutation inside a tracked element);
  otherwise the whole page area below the address bar is painted out for that
  frame. Scrolling flickers. That is the cost;
* while the gate's CDP session is down, or no report has arrived since a
  navigation, every frame is a blank "masking unavailable" frame;
* every box is dilated by 8 pixels.

**The mask is applied once, at the source.** The relay, the recorder and the
viewers all receive the masked frames; there is no unmasked stream anywhere to
leak.

#### 5.3.3 Keys never logged, anywhere

Typed characters exist in four places: the console's memory, swarm-api's and
the relay's buffers, and the gate on its way to XTEST. **No hop logs RFB
payloads**: every log line on the path carries byte and event counts only, and
a unit test per hop captures logs while a key event passes and asserts no
keysym appears. The audit records "control held 3m12s, 41 key events, 9
pointer events", never which keys.

#### 5.3.4 What the mask does not cover, stated plainly

* A secret drawn as an image (a QR code for 2FA setup, a screenshot of a key)
  is not text and is not masked.
* A secret in a canvas or a closed shadow root is not visible to the isolated
  world and is not masked.
* An ordinary field the person types a secret into, which is not a password
  field, shows what they type. The console's take-control dialog says so.
* A viewer can photograph their screen. Masking reduces what a viewer sees; it
  is not DRM.

### 5.4 Keeping the agent out of the person's session

#### 5.4.1 The worker proves it is the worker: the attempt key

The agent can mint the tenant's ID token and read the container's environment,
so neither can authorise a worker-only route
(`docs/design/child-tasks.md` (`### 3.2 What makes "only the worker" true: the attempt key`)). The live session and the hand-off routes **reuse that
attempt key**: the Ed25519 key the worker generates after `make_non_dumpable`
(`apps/agent-worker/agent_worker/hardening.py::make_non_dumpable`) and registers before the agent
exists. They sign domain-separated messages (`swarm-live-session/v1`,
`swarm-handoff/v1`) so a signature for one is never a signature for another.
Today the key is registered only when the child path is configured
(`worker_env`, `apps/scheduler/scheduler/dispatch.py::worker_env`, mints the
nonce only when `child_key and api_url and not task.parent_task_id`); lane L4 makes it registered for every
live-browser attempt. **A worker that cannot protect its heap gets no live
path**: no tunnel ticket, no hand-off; the console says "live view unavailable
for this attempt" and the agent's `swarm-handoff` exits 5.

#### 5.4.2 The worker talks to the gate on a channel the agent cannot use

`ctl.sock` sits on a volume both containers mount, so the agent can connect to
it. The gate therefore accepts **one registration only**: the worker, after
`make_non_dumpable`, generates a key in its heap and registers it; the native
sidecar is up first, and the agent is spawned later
(`apps/agent-worker/agent_worker/lifecycle.py::Worker._prepare`), so the slot is taken before the
agent exists -- the same first-registration-wins argument as the attempt key.
Every later message is MAC'd with that key and carries a counter. A gate that
receives a second registration refuses it and records `gate_ctl_refused`.

**A gate that restarts refuses every registration.** A native sidecar has
`restartPolicy: Always`, so the gate can restart mid-attempt while the agent
is alive, and a fresh gate process would otherwise reopen the slot for
whoever connects first. The gate therefore writes a marker to its own
sidecar-only volume on its first start; a gate that finds the marker is a
restart, accepts no registration at all, keeps Chromium closed and records
`gate_restarted`. The live path ends for that attempt: the worker sees its MAC'd
channel drop, treats it as "live view unavailable for this attempt", and an
open hand-off ends as if the hold had expired (checkpoint, park,
section 4.3), so the next attempt gets a fresh pod and a fresh gate. The worker
does not re-key: re-keying would need the worker to prove itself to a gate
that cannot tell it from the agent, which is the problem the first
registration exists to avoid. The exposure in between is small anyway, since
the agent holds no tunnel ticket, but a slot that silently reopens is the
kind of gap this section exists to close.

#### 5.4.3 While a person is in control, the agent's CDP is held

The CDP proxy is the only way the worker container reaches Chromium. In
control mode it:

* **holds** every command from the agent side (the agent's Playwright calls
  block; nothing reaches Chromium), and refuses new CDP connections;
* **drops** every event to the agent side, and after return **strips
  `postData`** from any network event about a request that started during
  control, and refuses `Network.getRequestPostData` and
  `Network.getResponseBody` for those request ids. The sign-in POST is never
  shown to the agent;
* **answers `Fetch.requestPaused` itself** with `Fetch.continueRequest`: an
  agent that had enabled request interception would otherwise freeze the
  person's sign-in waiting for a handler that is held;
* **removes the agent's page scripts** -- every
  `Page.addScriptToEvaluateOnNewDocument` and `Runtime.addBinding` it has seen
  the agent make, tracked by identifier -- and restores them on return, so a
  page loaded during control runs no agent-installed listener;
* offers the person **"start from a fresh page"**, the default for a
  hand-off: the gate creates **its own browser context**
  (`Target.createBrowserContext` on the gate's session, with no proxy
  parameters and downloads set to `downloads/`) and opens the hand-off's
  `expect` host in it, so the sign-in happens on a document no agent script
  has touched, in a context whose network settings no agent call set. On
  return the gate hands that context to the agent (the proxy then forwards the
  agent's attach to its targets), and the agent continues there, signed in. A
  page loaded before control, in the agent's own context, may carry listeners
  the agent installed; the console says so, and recommends the fresh page.

Holding commands during control does not undo state the agent set **before**
it asked for help, so the proxy also enforces section 5.4.4 at all times.

This is enforcement, not a request. An agent that ignores `swarm-handoff wait`,
or an escaped agent process, still cannot act on the browser or observe the
person's session while control is held, because nothing it can reach leads to
Chromium except the proxy, and the proxy never let it set up the state that
would observe the session later (section 5.4.4).

#### 5.4.4 State the agent sets before control: a method policy, always on

The attack: before calling `swarm-handoff`, the agent calls
`Target.createBrowserContext({proxyServer: <its own proxy>})` and
`Security.setIgnoreCertificateErrors({ignore: true})`, navigates to the sign-in
page in that context, then asks for help. Every hold and strip in section 5.4.3
applies only from the moment control starts; the sign-in POST still travels
through the agent's proxy, certificate errors are ignored, so the proxy can
terminate TLS with its own certificate and read the password. The same shape
works with `Page.setBypassCSP` (a page's own defences off) or
`Network.setExtraHTTPHeaders`. So the defence cannot be "pause the agent
during control"; it has to be "the agent can never set that state".

**The proxy carries a method policy, applied to every agent command from the
first one, not only in control mode.** It is an allowlist of the CDP domains
and methods Playwright's `connectOverCDP` uses (`Page`, `Runtime`, `DOM`,
`Input`, `Emulation`, `Accessibility`, `Target` attach and create-target, and
the read side of `Network`, `Fetch` and `Storage`), and anything not on it is
refused with a CDP error and recorded as `cdp_method_refused` with the method
name (never the parameters, which may carry page text). Inside the allowlist a
**named denylist** is refused regardless, because each one changes how the
browser trusts or routes traffic:

| Refused | Why |
|---|---|
| `Target.createBrowserContext` with `proxyServer` or `proxyBypassList` | routes the context's traffic, a sign-in included, through a server the agent chose |
| every `Security.*` method (`setIgnoreCertificateErrors`, `handleCertificateError`, `setOverrideCertificateErrors`) | lets a man-in-the-middle certificate pass, so TLS no longer protects what the person types |
| `Page.setBypassCSP` | turns off the site's own script defences on the page the person will type into |
| `Network.setExtraHTTPHeaders` | adds headers the agent chose to every request, the person's included |
| `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` with a path outside `downloads/` | writes files where the gate, not the worker, reads them |

The allowlist is the control and the denylist is its documentation: a method
Chromium adds next year is refused because it is not on the list, not because
someone remembered to deny it. A Playwright call that needs a refused method
fails loudly in the agent, which is the intended outcome; L2 lists the
methods `claude-code-browser`'s own tests use, and nothing else is opened
without a change to this table.

**The gate refuses control on a certificate error.** The gate's own CDP
session watches `Security.visibleSecurityStateChanged` (the gate's session,
not the agent's, so the denylist above does not apply to it). Control is not
entered, and ends if already held, while the page being controlled is not
`secure`: a certificate error, an interstitial, or a plain-`http:` page on the
hand-off's `expect` host. The console says why ("this page's connection is not
secure; control was refused") and the episode is audited with that cause.
The Chromium policy `SSLErrorOverrideAllowed: false` (section 5.6) means the
person cannot click through an interstitial either. With the denylist in
force a certificate error should not be the agent's doing; the refusal is for
the case where it is the network's, or where a refused-method check was
missed.

**What this does not cover.** The agent still sees the session after return,
by design (below), and still chose where the person was sent, which is
section 5.7's problem, not this one's.

**After return the agent has the session, by design.** It can read the site's
cookies through CDP and act as the signed-in user. That is the delegation the
person is making, and the take-control dialog says it in one sentence: "The
agent will be able to act as the account you sign in with, on this site, until
the attempt ends."

### 5.5 The session left behind

* **Cookies and storage are never checkpointed.** The profile lives on a
  sidecar-only memory volume, never under `work/`, which is all the checkpoint
  archives (`CheckpointManager.create`,
  `apps/agent-worker/agent_worker/checkpoint.py::CheckpointManager.create`).
* **They are wiped at attempt end.** On every exit -- success, failure, park,
  cancel, fence -- the worker tells the gate `wipe` before the final
  checkpoint: Chromium is closed and the profile directory removed. A pod that
  dies without that loses the volume with the pod anyway.
* **An agent that copies the session into `work/` is refused.** An agent can
  write `storageState()` JSON or copy a cookie database into its workspace,
  where the checkpoint and the outputs would pick it up. The checkpoint filter
  and the output upload drop files that have the shape of a Playwright storage
  state (a JSON object with `cookies` entries carrying `name`, `value` and
  `domain`) or of a Chromium `Cookies` SQLite database, and record
  `browser_state_refused` with the path. That is a shape check, not a proof;
  an agent determined to smuggle a cookie out in another encoding can. Its
  agent instructions forbid it, and the residual is stated here rather than
  hidden.
* **The checkpoint still never holds a session.** LB-7 reuses sessions
  through the gate and Secret Manager (§5.5.1), never through `work/`, the
  checkpoint or the outputs. The two bullets above hold unchanged.
* **Encrypting the session into the checkpoint, so it survives a park, is the
  option turned down for phase 1 (Q7).** It would make a park cheap -- no
  second sign-in -- at the price of a long-lived session stored in the tenant's
  bucket, released to whichever attempt can present the attempt key. Most
  sign-ins worth automating (GitHub, Google) also bind sessions to device and
  address, so a restored jar often fails anyway.

### 5.5.1 Reusing a signed-in session (LB-7)

The owner revised Q7 on 2026-10-11. Prompting a person on every browser step
made a seven-step visual QA run against a signed-in site ask seven times. The
owner then scoped reuse to the person (same day). **A session is stored or
carried only in a personal tenant**, `u-<user>`, whose provisioning record is
the person's workspace ([workspaces.md](../workspaces.md)). The reason is
identity: every worker of a group tenant such as `eng` runs as the same
service account (`swarm-agent-worker-eng`), so no IAM grant can let Alice's
agents read a secret while refusing Bob's. A personal tenant's worker identity
is that person's alone, so IAM is the whole boundary, with no broker to trust.

The session is a cookie jar plus origin storage. It moves **gate to gate**: the
gate reads it on its own pipe session (§3.2) and writes it into the next gate's
context before the agent connects. The agent never holds it.

**In a group tenant: nothing is kept.** The gate wipes at attempt end (§5.5).
No secret is written, and "Save as login" is not offered. A task in a group
tenant that names `input.browser_login` is refused at submission with
`browser_login_personal_only`.

**Workflow carry (automatic, personal tenants only).**
* When a browser step of a workflow in `u-<user>` ends with a session its
  hand-off produced, the gate exports the jar with `Storage.getCookies` and the
  local storage of the signed-in origins. It does this before the `wipe` of
  §5.5, on its own pipe session.
* The worker stores the jar as one Secret Manager secret,
  `swarm-tenant-u-<user>-browser-wf-<workflow>`, with a 24-hour TTL. Only that
  tenant's worker identity can read it.
* The next browser step of the same workflow imports it before the agent's
  first CDP connection.
* swarm-api deletes the secret when the workflow reaches a terminal state. The
  TTL is the backstop.

**Named logins (deliberate, personal tenants only).**
* After the person returns control on a task in their own workspace, the
  Browser tab offers "Save as login", with a name (for example `github-qa`) and
  an expiry (default 14 days, at most 30).
* The worker stores the jar as `swarm-tenant-u-<user>-browser-login-<name>`.
  swarm-api keeps its metadata (site, saved at, expires, last used) in its own
  collection `browser_logins`, keyed by tenant. That collection belongs to
  swarm-api, as `live_sessions` does, not to the frozen models.
* A task in that workspace opts in with `input.browser_login: "<name>"`: a
  name, never cookies, as invariant 10 asks of every caller choice. An unknown,
  expired or revoked name fails the task before the agent runs.
* Access › Browser logins lists only the signed-in person's own logins, never
  with a value, with Revoke. Revoke destroys every secret version.
* When a login's site answers with a sign-in page anyway, the gate raises a
  normal hand-off. The new sign-in refreshes the stored login (a new version),
  so an expiry costs one prompt, not one per task.

**Whose account (owner's answer).** Any account may be saved, at the saving
person's judgement, including a personal one. The save dialog states that every
agent the person runs in their workspace can act as that account on that site
until the login expires or is revoked.

**What protects it, and what does not.**
* IAM. The worker's grant on `swarm-tenant-u-<user>-browser-*` is held by
  `swarm-agent-worker-u-<user>` alone (lane L8b), and no group tenant's
  identity holds any `-browser-` grant. Platform admins with project-level
  Secret Manager roles can still read these secrets. That is the same residual
  as every other secret in the project.
* The agent's CDP cannot read or plant cookies. The method policy of §5.4.4
  refuses:
  - `Network.getCookies`, `Network.getAllCookies` and `Storage.getCookies`;
  - `Network.setCookie(s)` and `Storage.setCookies`;
  - `Network.clearBrowserCookies`;
  - `Storage.clearDataForOrigin`.

  Each refusal records `cdp_method_refused`.
* **Not covered, stated plainly:**
  - A cookie without `HttpOnly` is readable by page script, which the agent
    can run with `Runtime.evaluate`.
  - An agent driving a signed-in browser can act as the account whatever
    happens to the cookies.
* The values never reach a log, an event, a checkpoint, an output or the
  console. The audit (§5.8) records `browser_login_saved`, `_used`, `_refreshed`
  and `_revoked`, with names only.
* Secrets follow the forge-token rule: Secret Manager only, read at run time.

User-scoped forge tokens and provider keys are a separate question (they are
per tenant today), filed on its own issue.

### 5.6 Keyboard, clipboard and the browser itself

* **Clipboard.** Pod-to-console clipboard is **off**, always: it would hand a
  viewer whatever the agent copied, unmasked. Console-to-pod paste is **on
  only in control mode** (pasting a password from a password manager is how
  most people sign in), and is injected as typed keys, so it is masked and
  unlogged like typing (Q10).
* **Keyboard.** Input is injected only from the controller's connection and
  only in control mode; key events from viewers are discarded at the gate.
  The console captures keys only while the canvas has focus, so a person's
  typing elsewhere in the console never reaches the pod.
* **Chromium policy** (`/etc/opt/chrome/policies/managed/` in the sidecar):
  `URLBlocklist` for `file://*`, `chrome://*` (but `chrome://newtab`),
  `devtools://*`, `view-source:*` and the cluster-internal names
  (`*.svc.cluster.local`, `swarm-system`); `DeveloperToolsAvailability: 2`;
  `PasswordManagerEnabled: false`; `AutofillAddressEnabled` and
  `AutofillCreditCardEnabled: false`; `FullscreenAllowed: false` (section 5.7);
  `SSLErrorOverrideAllowed: false` (section 5.4.4);
  `BrowserSignin: 0`; `SyncDisabled: true`. A person in control therefore
  cannot read the sidecar's files, open devtools on the agent's pages, or
  save a password into a profile that is about to be wiped anyway.
* **Network reach** is the pod's: the internet minus every private range, and
  the metadata server, which answers only requests carrying
  `Metadata-Flavor: Google`, a header a navigation cannot set
  (`kubernetes/network-policies/allow-egress.yaml`). The new rule of section 8.3 opens
  only the relay's port; the relay answers nothing without a ticket.

### 5.7 Phishing through the agent's browser

The attack: a page the agent read tells it to go to `github-login.example`,
which looks like GitHub, and to ask for help signing in. The person sees a
GitHub sign-in page in their own trusted console, and types their password.

What stands in the way:

1. **The origin is shown from Chromium, not from the frame.** The console's
   strip shows the top frame's origin as the gate reads it over CDP, outside
   the canvas, with the registrable domain emphasised and any punycode shown
   decoded and flagged. A page cannot draw into that strip.
2. **The hand-off names the site it expects.** `--expect github.com` is stored
   with the request. When the top frame's registrable domain is not the
   expected one, the strip turns red and Take control asks for a second
   confirmation that names both domains. A hand-off with no `expect` shows a
   warning that the agent did not say which site it means.
3. **The address bar is real and stays on screen.** Chromium draws it, and
   `FullscreenAllowed: false` stops a page from going fullscreen to draw a
   fake one.
4. **Not over plain HTTP, not to an address.** The gate refuses to enter
   control mode while the top frame is `http:` or its host is an IP literal;
   the console says why.
5. **A submitter may pin allowed sign-in hosts** for the task (a list on the
   task, optional): a hand-off whose `expect` is not on it is refused before
   anyone is asked.

What remains: a person who confirms past a red warning. The take-control dialog
recommends a purpose-made account for any site an agent signs in to.

### 5.8 The audit trail

`live_audit/{id}`, append-only, written by swarm-api in the **same
transaction** as the change it records, as `admin_audit` is
(`apps/swarm-api/swarm_api/admins.py`, "THE AUDIT IS APPEND-ONLY"). Entries:
`session_opened`, `viewer_joined`, `viewer_left`, `handoff_requested` (reason,
redacted), `control_taken`, `control_returned`, `control_ended` (cause:
`idle`, `renew_lapsed`, `preempted`, `membership_lost`, `cancelled`,
`hold_expired`), `control_opened_to_members`, `handoff_resolved`,
`session_closed`. Each names the task, attempt, generation, the person's email,
and counts (keys, pointer events, duration), never content. The task's own
event stream carries the hand-off and control transitions so the console's
timeline shows them; that needs new `EventType` members (request LB-B). The
audit is readable by members of the task's tenant through the task routes, and
by nobody else.

### 5.9 What the agent sees of what the person did

`swarm-handoff wait` exits 0 with:

```json
{"outcome": "returned", "held_seconds": 192, "url": "https://github.com/", "title": "GitHub"}
```

`url` is the top frame's origin and path, redacted, with no query. Not who
the person was, not what they typed, not a frame. The agent can look at the
page itself afterwards, which is the point; it cannot see the sign-in that put
the page there (section 5.4.3).

### 5.10 Rate limits

| What | Limit (default) | Where enforced |
|---|---|---|
| Viewer tickets per person | 30 a minute | swarm-api, per principal (`TokenBucketLimiter`, `apps/swarm-api/swarm_api/ratelimit.py::TokenBucketLimiter`; in-process, so N instances allow N times this, as that module records) |
| Concurrent viewers per task | 10 | relay (single replica, so the count is global) |
| Concurrent viewers per tenant | 50 | relay |
| Take-control attempts per person per task | 6 a minute | swarm-api |
| Hand-off requests per attempt | 3 | worker, and swarm-api from the task document |
| Controller input events | 200 a second | gate (drops above) |
| Frame rate | 10 fps with a viewer, 1 fps recording-only | gate |

### 5.11 Abuse cases

| Case | What stops it |
|---|---|
| A member of tenant A watches or controls tenant B's agent | §5.2 step 2 on every connection; the relay refuses a ticket whose tenant is not the session's; a refusal is a 404. |
| Replaying a captured stream or ticket | TLS on every hop; tickets one-use, 30-60 s, bound to session, person, role and generation. A recording is a stored object behind the same tenant check, not a re-stream. |
| A stale worker (old generation) keeps streaming | The ticket carries the generation; swarm-api closes the session when the lease's generation moves (invariant 5), and the relay drops the tunnel. |
| The agent keylogs the person | No display access (§3.1); CDP held and scripts removed during control (§5.4.3); postData stripped after. |
| The agent sets traffic-routing or trust state before asking for help (a context with its own `proxyServer`, `Security.setIgnoreCertificateErrors`, `Page.setBypassCSP`, extra headers), so the sign-in passes through it | The proxy's method policy refuses those calls from the first command, not only in control (§5.4.4); the fresh page opens in a gate-owned context; the gate refuses or ends control while the page has a certificate error. |
| The gate restarts mid-attempt and the agent registers on `ctl.sock` first | A restarted gate accepts no registration; the live path ends for that attempt (§5.4.2). |
| The agent or an escaped process drives the browser during control | The proxy holds every command; there is no other path. |
| The agent spams hand-offs to pester people or hold the pod | 3 per attempt; each hold bounded; the park gives capacity back. |
| A controller sits on control to hold capacity | Idle 300 s and renewal 60 s end control; the hold's deadline then applies. |
| Phishing through the agent's browser | §5.7. |
| A person in control reads the pod's files or the agent's environment | `file://`, devtools and `chrome://` blocked; Chromium runs as a uid that cannot read the worker's `/proc` or `work/`. |
| A person exfiltrates data through the pod's network | They are a member of the tenant whose agent it is, with the egress any of its agents has. Not a boundary this feature moves. |
| A hostile page attacks the console | noVNC draws pixels; no page content runs in the console's origin (§3.4). |

---

## 6. The console

The owner chose this presentation (LB-5) from five in-console alternatives. The
mockup of the chosen design is the owner's private artifact "Browser Tab and
Theater", linked from #1030. In summary:

* **A Browser tab on the agent's view** (`AgentSplit.tsx`, the underline tabs
  Details, Logs, Changes and so on). It sits before Logs, and it is the tab a
  running browser agent opens on, as Logs is today. It holds the noVNC canvas
  (§3.4), the origin strip and the controller. Its toolbar holds the device
  switcher (§6.1), rotate, touch, a **logs toggle** that splits the tab into
  logs beside the canvas, and **⤢ Theater**. Take control sits in the agent
  header beside Stop. The view is read-only until Take control.
* **Theater mode**, opened from ⤢ (or the key `T`), from a graph card, or from
  the hand-off banner. It shows the same session full screen:
  - a left column that switches between Timeline (the agent's actions) and Logs;
  - the canvas in the middle;
  - a right column with the device picker and the other live agents, with
    prev and next;
  - a bottom bar with replay over the session's recording (§7) and Take control.

  Esc returns to the tab at the same scroll. Theater is a view, not a second
  session: it reuses the tab's tunnel and ticket. On a phone, ⤢ goes native
  full screen, landscape preferred.
* **The hand-off** when `human_wait` is set:
  - a yellow banner across the Browser tab, with the reason, the expected site,
    the time left, Take control, and Open in theater;
  - a dot on Agents in the rail and a yellow row in the agent list;
  - the banner on Overview's "Needs a look" (`apps/swarm-ui/src/Overview.tsx`).

  On a parked hand-off the banner reads "I'm ready to sign in", which calls
  `human-ready` and opens the tab. In the theater, the left column becomes the
  hand-off checklist.
* **The workflow graph** (`WorkflowViews.tsx`). A running browser step's node
  expands an inline card with a low-rate (about 1 fps) live thumbnail. The card
  carries Open Browser tab and ⤢ Theater. In the theater, prev and next step
  through that workflow's live steps.
* **All live browsers**: a **Live** filter on the Agents list, with a device
  thumbnail per row and needs-you first. "Theater all" opens the theater with
  prev and next across them. There is no new nav tab.
* **On the person's own phone**, the console's existing two-page phone layout
  carries the same Browser tab. Take control works by touch, and the device
  switcher sits under the tab's ⋯ menu.
* **Recordings** (§7) on the attempt's view, as a player over the masked
  segments, for tenant members. Theater replay reads the same segments.
* No new nav tab: everything lives inside existing views. So the issue forms'
  "Where" list (`tests/unit/scripts/test_issue_forms.py`, reading `SECTIONS` in
  `apps/swarm-ui/src/App.tsx`) does not change.

### 6.1 Mobile devices (LB-6)

The browser can be a phone or a tablet, for visual QA as a mobile browser and
for sign-ins that only work on mobile.

* **The gate applies the device, not the agent.** The gate owns Chromium's pipe
  (§3.2), so it sets the device on the gate-owned context with
  `Emulation.setDeviceMetricsOverride` (width, height, device scale factor,
  `mobile`), `Emulation.setUserAgentOverride` (the preset's user agent and
  client hints) and `Emulation.setTouchEmulationEnabled`. It also resizes the
  virtual display to the preset's frame, so the frames carry no letterbox. The
  agent's own CDP calls to these methods go through the method policy (§5.4.4).
  An agent picks a preset through the gate (`swarm-handoff device <preset>`),
  never by sending metrics of its own.
* **The presets** are a fixed, named list in the gate:
  - `desktop` 1440×900
  - `iphone-15` 393×852 @3
  - `pixel-8` 412×915 @2.625
  - `iphone-se` 375×667 @2
  - `ipad-7` 810×1080 @2

  Each has a portrait and a landscape orientation. A name outside the list is
  refused. A caller never sends dimensions, which keeps invariant 10's spirit:
  a choice by name, never a parameter.
* **Touch from the console.** When the preset is mobile and the person in
  control has touch on:
  - the gate turns the controller's pointer events into
    `Input.dispatchTouchEvent`: press, move and release become touchStart,
    touchMove and touchEnd;
  - a two-finger gesture on a phone becomes a pinch;
  - a wheel becomes a touch scroll.

  The RFB key rules (§5.6) are unchanged. In view mode, no input is sent at all.
* **Masking (§5.3) is device-independent.** It reads element boxes from the
  page in CSS pixels and scales them by the device scale factor. A test runs
  every preset (lane L3b).
* **Who switches the device** is question Q13. The recommendation is the agent,
  plus the person in control. A viewer's switcher shows the current device and
  is disabled.

---

## 7. Recording and replay (LB-4)

**What is recorded.** The masked frames, and nothing unmasked, because nothing
unmasked exists outside the sidecar. The **relay** records, not the pod: it
already receives every masked frame, and a recording written by a platform
identity cannot be forged or padded by the agent, which shares the tenant's GSA
with everything else in the pod.

* `tenants/<t>/tasks/<task>/attempts/<a>/live/rec-<seq>.live.webm`: one-minute
  VP8 segments, 1 fps when nobody is watching and up to 10 fps while someone
  is (Q11 asks whether to record when nobody watches at all).
* `tenants/<t>/tasks/<task>/attempts/<a>/live/actions-<seq>.live.jsonl`: one
  line per agent CDP **method** the proxy forwarded (`Page.navigate` with the
  redacted URL; `Input.insertText` as a length, never text), and one per
  control event from the audit. Never a key, never a parameter that could hold
  typed text.

**Retention: 7 days, by a lifecycle rule of its own.** The artifact bucket's
Delete rule is keyed on `customTime` and keeps artifacts
`artifact_retention_days` (180 in prod) (`terraform/modules/storage/main.tf`,
"Artifacts and logs expire on a clock; checkpoints do not"). A
`matches_suffix = [".live.webm", ".live.jsonl"]`, `age = 7` Delete rule
removes recordings first, whatever their `customTime`. A suffix, not a prefix,
because a prefix rule cannot match the middle of `tenants/<t>/.../live/`. The
relay's service account holds `storage.objects.create` on the artifact bucket
under an IAM condition limiting it to names that contain `/live/` and end in
those suffixes, and nothing else: no read, no delete.

**Viewers.** Members of the task's tenant, through a swarm-api route that
checks the tenant as §5.2 does and streams the object. Not admins outside the
tenant. The 7 days are the same for every tenant; a tenant-specific retention
is not in phase 1.

---

## 8. Platform fit

### 8.1 GKE only

`claude-code-browser` runs on `GKE_AUTOPILOT` (lane PROFILE). The browser image
does not run on Cloud Run here: the `browser` profile is GKE-only because
Chromium needs a sized `/dev/shm`
(`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`, the `browser` entry), and this design
adds a native sidecar and a second uid on top. swarm-api refuses a live session
for a task whose resolved backend is not `GKE_AUTOPILOT`, and
`claude-code-browser` must not be added to the Cloud Run fallback list
(`cloud_run_fallback_profiles`, `terraform/infra/locals.tf`), because a fallback would silently run a
profile whose hand-off cannot work.

### 8.2 Resource class

`browser`: 8 vCPU, 16 GiB, 2 units
(`apps/common/swarm_common/profiles.py::RESOURCE_CLASSES`). **The class does not change;
it is split between the containers**, each with `requests == limits`
(invariant 7), summing to the class, so admission's unit arithmetic is
untouched. Proposed starting split, to be measured by lane L5: worker 3 vCPU /
6 GiB (claude-code, Node, the worker), display sidecar 5 vCPU / 10 GiB
(Chromium, Xvfb, encoding), with the existing 2 GiB memory `/dev/shm`
(`GkeJobDispatcher._manifest`, `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`)
moved to the sidecar.

### 8.3 NetworkPolicy

* **Tenant namespaces: one new egress rule.** The existing egress policy cannot
  express it: its internet rule is an `ipBlock` that excepts the pod range, and
  "Pod traffic is never covered by an ipBlock rule"
  (`kubernetes/network-policies/allow-egress.yaml`). A second policy,
  `swarm-allow-live-egress`, selects pods labelled `swarm-live: "enabled"`
  (which the dispatcher sets only on live-browser pods) and allows TCP 8443 to
  pods `app.kubernetes.io/name=swarm-live` in namespace `swarm-system`.
  NetworkPolicies are unions, so it adds a path and removes nothing. A pod
  missing the label simply has no live path; unlike the trap that comment
  records, nothing else breaks, because DNS and the internet still come from
  the existing policy.
* **`swarm-system`: default-deny ingress, and allow** TCP 8443 to `swarm-live`
  from tenant namespaces (by the namespace label the tenant template sets) and
  from swarm-api's Direct VPC egress subnet range.
* The cluster's ranges are read from the live cluster, never written, as
  `kubernetes/apply.sh` and `kubernetes/network_parity.py` already require.

### 8.4 Frozen-contract touchpoints, as requests

Nothing in `apps/common/swarm_common/` is edited by this document. The requests
below are **not yet filed** in
[`docs/contract-change-requests.md`](../contract-change-requests.md): which of
them exist depends on the owner's answer to Q2, and lane PROFILE is filing its
own request for `claude-code-browser` in parallel, so numbers would collide.
They are filed, numbered, by lane L0 once the questions are answered.

| Request | File | The change | Invariants |
|---|---|---|---|
| **LB-A** | `states.py` | `ParkReason.HUMAN_REQUIRED`: parked by the worker when a hand-off's hold expires; promoted only by `human-ready`; never by a sweep. | 1 (costs nothing), 4 (the reason the park exists) |
| **LB-B** | `states.py` | `EventType` members `HUMAN_HANDOFF_REQUESTED`, `HUMAN_CONTROL_TAKEN`, `HUMAN_CONTROL_RETURNED`, `HUMAN_HANDOFF_RESOLVED`. | none |
| **LB-C** | `models.py` | `Task.human_wait: dict | None` (`reason`, `expect`, `requested_at`, `deadline`, `controller`), written only by swarm-api. **Or**, if the owner chooses the state (Q2), `TaskState.WAITING_FOR_HUMAN` in `CONCURRENCY_STATES` with transitions `RUNNING <-> WAITING_FOR_HUMAN`, `WAITING_FOR_HUMAN -> PARKED / CANCELLED / FAILED / READY`, and every reader of §4.3 updated in the same change. | 1-3: the state holds capacity and must be counted |
| **LB-D** | `profiles.py` | `RunnerProfile.live_browser: bool = False`; `True` on `claude-code-browser`. Validation: only on a `GKE_AUTOPILOT` profile. | 10: a caller still names a profile; whether it is live is the catalogue's, never the caller's |

The session and audit documents (`live_sessions`, `live_audit`) are
swarm-api's own, defined in swarm-api as `admin_roles` and `admin_audit` are,
not in the frozen models.

### 8.5 The ten invariants

1. Only leased tasks create demand: a holding task is `RUNNING`; a parked one
   is `PARKED(HUMAN_REQUIRED)` and creates none.
2. All-or-nothing reservation: unchanged; re-admission after `human-ready` is
   an ordinary admission.
3. Concurrency from `LEASED`: unchanged; no new capacity-holding state (unless
   Q2 chooses one, in which case LB-C adds it to `CONCURRENCY_STATES`).
4. No sleeping through a long wait: the hold is bounded at 10 minutes by
   default, then checkpoint, park, release, exit.
5. Fencing: the tunnel ticket, the gate's control channel and every worker
   route carry the generation; a stale generation's session is closed, and a
   stale worker's hand-off is refused.
6. No Spot: the pod is the same pod.
7. `requests == limits`: per container, summing to the class.
8. Checkpointing: unchanged and mandatory; a hold does not pause the periodic
   checkpoint, and the park writes one.
9. Tenant isolation: frames, control, recordings and the audit are served to
   the task's tenant only, checked on every connection; recordings live under
   the tenant's prefix.
10. Profiles by name: `live_browser` is a catalogue property; a caller cannot
    ask for a live browser on another profile, or pass display, browser or
    gate parameters.

---

## 9. Build plan

Lanes of at most ~6 files, split by territory, in phase order. A lane starts
only after the lanes it depends on have merged. Each lane pushes its tests
first, red, then the fix (CLAUDE.md, "Red first").

| Phase | Lane | Territory (files) | Depends on | Acceptance test |
|---|---|---|---|---|
| 0 | **L0 CONTRACT** | `apps/common/swarm_common/states.py`, `models.py`, `profiles.py`, `docs/contract-change-requests.md`, `scripts/lib/check-contract-parity.sh`, `tests/unit/common/test_live_browser_contract.py` | owner's answers; lane PROFILE merged | `HUMAN_REQUIRED` is not in any capacity set; `live_browser` is true only on `claude-code-browser` and refused on a non-GKE profile; parity script green |
| 1 | **L1 GATE-MASK** | `apps/agent-worker/agent_worker/livegate/mask.py`, `livegate/observer.js`, `livegate/capture.py`, `tests/unit/worker/test_live_mask.py` | L0 | a fixture frame plus rectangles comes out with those pixels painted; a field toggled from `password` to `text` stays masked; a stale report blanks the page area; a redaction-rule match in text is masked (fake built at runtime) |
| 1 | **L2 GATE-CDP** | `livegate/cdpproxy.py`, `livegate/cdppolicy.py`, `livegate/scripts.py`, `tests/unit/worker/test_live_cdp_proxy.py` | L0 | in control mode commands are held and released in order on return; events dropped; `postData` stripped for control-window requests; `Fetch.requestPaused` continued by the proxy; agent init scripts removed and restored; new connections refused; **outside control too**, `Target.createBrowserContext` with `proxyServer`, every `Security.*` method, `Page.setBypassCSP`, `Network.setExtraHTTPHeaders`, a download path outside `downloads/` and a method not on the allowlist are refused and `cdp_method_refused` recorded; the fresh page opens in a gate-owned context with no proxy; control is refused, and ended if held, while the page reports a certificate error |
| 1 | **L3 GATE-RFB** | `livegate/rfb.py`, `livegate/inject.py`, `livegate/ctl.py`, `tests/unit/worker/test_live_rfb.py` | L1 | key events from a viewer, or in view mode, are discarded; pod-to-console clipboard never sent; a second `ctl.sock` registration refused, and a restarted gate refuses even the first; captured logs contain no keysym |
| 1 | **L4 WORKER-HANDOFF** | `apps/agent-worker/agent_worker/handoff.py`, `lifecycle.py`, `workspace.py`, `images/agent-runtime-browser/swarm-handoff`, `tests/unit/worker/test_handoff.py` | L0, L3 | request -> gate paused -> signed route called; hold expiry -> checkpoint, wipe, park(`HUMAN_REQUIRED`), refund within the bound; cancel during hold -> exit code 4, wipe, cancel path; unprotected heap -> exit 5 and no tunnel |
| 1 | **L4b WORKER-STATE** | `apps/agent-worker/agent_worker/checkpoint.py`, `artifact_manifest.py`, `tests/unit/worker/test_browser_state_refused.py` | L0 | a storage-state JSON or a `Cookies` database under `work/` is left out of the checkpoint and the outputs, and `browser_state_refused` is recorded |
| 1 | **L5 DISPATCH** | `apps/scheduler/scheduler/dispatch.py`, `images/agent-runtime-browser/Dockerfile`, `images/agent-runtime-browser/chromium-policy.json`, `tests/unit/control_plane/test_dispatch_live_sidecar.py`, `docs/execution-backends.md` | L0 | a `claude-code-browser` manifest has a native sidecar with its own uid, `requests == limits` per container summing to `browser`, `/dev/shm` on the sidecar, the `swarm-live` label; every other profile's manifest unchanged |
| 2 | **L6 NETPOL** | `kubernetes/network-policies/allow-live-egress.yaml`, `kubernetes/network-policies/swarm-system-live.yaml`, `kubernetes/render.py`, `kubernetes/apply.sh`, `kubernetes/network_parity.py`, `tests/unit/scripts/test_live_netpol.py` | L5 | the rendered policy allows only TCP 8443 to `swarm-live` pods, only from labelled pods; the existing policy renders byte-identical |
| 2 | **L7 RELAY** | `apps/swarm-live/swarm_live/main.py`, `rendezvous.py`, `recorder.py`, `apps/swarm-live/pyproject.toml`, `tests/unit/live/test_relay.py` | L3 | a ticket used twice, expired, or for another tenant, session or generation is refused; one tunnel per session; viewer caps; segments written only under `.../live/` with the two suffixes |
| 2 | **L8 INFRA** | `terraform/infra/live.tf`, `terraform/modules/storage/main.tf`, `kubernetes/swarm-system/swarm-live.yaml`, `scripts/create-secrets.sh`, `tests/terraform/live.tftest.hcl` | L7 | `terraform test`: the 7-day suffix rule exists, the relay's grant is create-only and conditioned on `/live/`, every resource carries `managed-by=swarm-terraform`, no secret version |
| 2 | **L9 API-LIVE** | `apps/swarm-api/swarm_api/live.py`, `apps/swarm-api/swarm_api/routes/live.py`, `apps/swarm-api/swarm_api/settings.py`, `tests/unit/control_plane/test_live_api.py` | L7 | another tenant's member gets 404; a non-submitter can view, cannot take until opened; one controller at a time, preemption by the submitter only; the audit entry is in the control change's transaction; membership re-checked; no ticket in a URL |
| 2 | **L10 API-HANDOFF** | `apps/swarm-api/swarm_api/handoff.py`, `apps/swarm-api/swarm_api/routes/tasks.py`, `apps/swarm-api/swarm_api/validation.py`, `apps/scheduler/scheduler/loop.py`, `tests/unit/control_plane/test_handoff_api.py` | L0 | worker route refuses an unsigned or stale-generation call; `human-ready` moves `PARKED(HUMAN_REQUIRED) -> READY` for an eligible caller only; no sweep promotes it; past the maximum it is dead-lettered with `human_wait_expired`; `human_wait_resumes` is reserved |
| 1 | **L3b GATE-DEVICE** | `livegate/device.py`, `livegate/touch.py`, `livegate/presets.py`, `tests/unit/worker/test_live_device.py` | L2, L3 | each preset sets metrics, user agent and touch on the gate-owned context and resizes the display; an unknown preset name is refused; the agent's own `Emulation.*` calls are refused by the method policy; in control with touch on, a press/move/release arrives as touchStart/Move/End; in view mode no input is sent; the mask covers a password field on every preset at its device scale factor |
| 3 | **L11 UI-LIVE** | `apps/swarm-ui/src/LiveBrowser.tsx` (the Browser tab, device bar, logs toggle), `apps/swarm-ui/src/AgentSplit.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/package.json`, `apps/swarm-ui/test/LiveBrowser.test.tsx` | L9, L3b | vitest: the Browser tab is first for a running browser agent; the ticket goes in the first message, never the URL; no Take control for a non-eligible viewer; a viewer's device switcher is disabled; a mismatched origin turns the strip red and asks twice; keys captured only with canvas focus; touch events sent only in control with touch on |
| 3 | **L11b UI-THEATER** | `apps/swarm-ui/src/Theater.tsx`, `apps/swarm-ui/src/TheaterTimeline.tsx`, `apps/swarm-ui/src/styles/theater.css`, `apps/swarm-ui/test/Theater.test.tsx` | L11 | vitest: ⤢ and `T` open it on the same session (no second ticket); Esc returns to the tab; Timeline/Logs switch; prev/next walk the live agents in order; replay reads only the caller's tenant's segments |
| 3 | **L12 UI-HANDOFF** | `apps/swarm-ui/src/Overview.tsx`, `apps/swarm-ui/src/Agents.tsx` (the banner row, the Live filter), `apps/swarm-ui/src/Recordings.tsx`, `apps/swarm-ui/test/Handoff.test.tsx` | L10, L11 | the tab banner and Overview card show reason, expected site and time left; "I'm ready to sign in" calls `human-ready`; the Live filter lists only running browser agents, needs-you first; recordings list only the caller's tenant's |
| 3 | **L12b UI-GRAPH** | `apps/swarm-ui/src/WorkflowViews.tsx`, `apps/swarm-ui/src/LiveThumb.tsx`, `apps/swarm-ui/test/LiveThumb.test.tsx` | L11b | a running browser node expands a card with a thumbnail at ≤1 fps; Open Browser tab and ⤢ Theater navigate to the same session; no thumbnail stream for a finished step |
| 1 | **L4c SESSION-CARRY** | `apps/agent-worker/agent_worker/livegate/session.py`, `apps/agent-worker/agent_worker/browser_login.py`, `apps/agent-worker/agent_worker/livegate/cdppolicy.py`, `tests/unit/worker/test_browser_session.py` | L2, L4 | in a personal tenant a workflow step's jar is exported on the gate's pipe and written to `swarm-tenant-u-<user>-browser-wf-<wf>`, then imported by the next step before the agent connects; in a group tenant nothing is written and no carry happens; a step outside a workflow carries nothing; `input.browser_login` loads the named login, and an unknown or expired name fails before the agent runs; the agent's cookie-reading and cookie-writing CDP methods are refused; no cookie value in any log, event, checkpoint or output (fake jar built at runtime) |
| 2 | **L8b VAULT-IAM** | `terraform/infra/browser_logins.tf`, `tests/terraform/browser_logins.tftest.hcl` | L8 | only a personal tenant's worker holds a grant, on its own `swarm-tenant-u-<user>-browser-` prefix; no group tenant's worker holds any `-browser-` grant; swarm-api may destroy those secrets; no secret version in Terraform; `managed-by=swarm-terraform` on every resource |
| 2 | **L9b LOGIN-VAULT** | `apps/swarm-api/swarm_api/browser_logins.py`, `apps/swarm-api/swarm_api/routes/browser_logins.py`, `apps/scheduler/scheduler/reconcile.py`, `tests/unit/control_plane/test_browser_logins.py` | L9 | only the workspace's own person can list or revoke its logins; anyone else gets 404; a group-tenant task naming `browser_login` is refused with `browser_login_personal_only`; a value is never returned; save takes a name and an expiry of at most 30 days; revoke destroys every version; a workflow's carry secret is deleted at its terminal state; the audit records names only |
| 3 | **L12c UI-LOGINS** | `apps/swarm-ui/src/BrowserLogins.tsx`, `apps/swarm-ui/src/Access.tsx`, `apps/swarm-ui/src/LiveBrowser.tsx`, `apps/swarm-ui/test/BrowserLogins.test.tsx` | L9b, L12 | after Return control on a personal-workspace task the tab offers "Save as login" with the account warning, and never on a group-tenant task; Access › Browser logins lists name, site, expiry and last use, never a value; Revoke asks for a typed confirmation |
| 4 | **L13 ACCEPT** | `.github/workflows/accept.yml`, `tests/acceptance/test_live_browser.py`, `tests/acceptance/fixtures/login.html`, `docs/acceptance.md` | all | on dev: a `claude-code-browser` task asks for help on a fixture sign-in page; a scripted viewer takes control and types a password generated at run time; the recording's frames have the field's pixels painted; the transcript, logs, checkpoint and outputs do not contain the password; control returns and the task succeeds; a member of another tenant is refused |

L1-L3 share `livegate/`, a new directory; they are three lanes because their
files do not overlap, and L3 waits for L1 because it encodes L1's frames.

---

## 10. Questions for the owner

**Answered 2026-10-11, recorded on #1030.** The owner took every
recommendation below: Q1 the submitter, with a per-task "open to the tenant"
toggle; Q2 the field; Q3 10 minutes; Q4 24 hours; Q5 no; Q6 yes, the submitter
only; Q7 sign in again; Q8 console only in phase 1; Q9 R2; Q10 on; Q11 yes, at
1 fps; Q12 accept; Q13 the agent and the person in control. The owner also
approved the mockup of §6 as drawn.

The questions as they were asked, each written to `questions.json` with its
recommendation:

1. **Q1** Who may take control: the submitter (with a per-task "open to the
   tenant" toggle), or any tenant member. *Recommended: the submitter.*
2. **Q2** `Task.human_wait` while the pod holds, or a new task state
   `WAITING_FOR_HUMAN`. *Recommended: the field.*
3. **Q3** The hold before the park. *Recommended: 10 minutes.*
4. **Q4** How long a parked hand-off waits before it is dead-lettered.
   *Recommended: 24 hours.*
5. **Q5** May platform admins outside the tenant watch? *Recommended: no.*
6. **Q6** May a person take control when the agent did not ask?
   *Recommended: yes, the submitter only.*
7. **Q7** Keep the browser session across a park (encrypted), or sign in again.
   *Recommended: sign in again.* **Revised by LB-7**: sessions are reused
   through the gate (workflow carry, plus named tenant logins), still never
   through the checkpoint.
8. **Q8** Notify beyond the console? *Recommended: console only in phase 1.*
9. **Q9** The relay shape (R2 or R3). *Recommended: R2.*
10. **Q10** Console-to-pod paste in control mode. *Recommended: on.*
11. **Q11** Record when nobody is watching? *Recommended: yes, at 1 fps.*
12. **Q12** Chromium launched by the platform in a sidecar with its own uid,
    reached through a CDP proxy, with no VNC server listening even on
    localhost -- instead of the brief's agent-launched Chromium and localhost
    VNC (§3.1, §3.2). *Recommended: accept.*
13. **Q13** Who may switch the device (§6.1): the agent only; the agent and the
    person in control; or any viewer. *Recommended: the agent and the person in
    control.*

---

## 11. Not decided here, and not verified

* **No measurement backs the resource split, the frame rate or the relay's
  capacity.** They are starting values for L5 and L7 to measure.
* **Native sidecars on this cluster** are assumed from GKE's support for them
  on current Autopilot versions; L5 confirms on `swarm-autopilot` before it
  merges.
* **The path from swarm-api to the relay** (Direct VPC egress to an internal
  load balancer in front of `swarm-live`) is named, not measured; L8 measures
  it, as the verification job's egress setting was once measured the hard way
  (`terraform/infra/verify.tf`).
* **That Playwright's `connectOverCDP` picks up the gate-owned context** handed
  over on return (section 5.4.3), and that the method allowlist of section 5.4.4
  covers every call Playwright makes, are design; L2 proves both.
* **Playwright's `connectOverCDP` through the proxy**, downloads and file
  uploads through the shared volumes, are design; L2 and L5 prove them.
