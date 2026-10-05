# Child tasks: a running agent submits helpers through its worker

**Status: design, phase 1 of B15 (2026-10-02); BUILT by phase 2 (lane B15b,
2026-10-02), deployment switch off -- see "Where phase 2 stands" in section
9.** It answers the owner's decisions OD-B15-1 to OD-B15-4 and is the brief
phase 2 was built from. The frozen-contract half was a request:
[contract request 14](../contract-change-requests.md#14-modelspy-a-sub-agent-has-nowhere-to-name-its-parent)
and the requests 40-43 its amendment files, all accepted by the owner on
2026-10-02 and APPLIED there (CLAUDE.md rule 1).

Every `file:line` below was read on 2026-10-02 at `b2becab`. Lines move; the
symbol named beside each one is what to search for when they have.

---

## 1. Why

The owner named three dispatch scales on 2026-09-21
(`docs/design/dispatch-and-integration.md` (`### 4.1 Dispatch scales`)): a single agent, a **main
agent with helpers**, and a workflow. The middle one is the one that makes a
remote run feel like a local Claude Code session that spawns subagents, and it
is the one the cluster does not have. Section 5 of that document lists it last
(`docs/design/dispatch-and-integration.md` (`## 5. What would have to be built`)) because it depends on
everything else.

What exists today, verified:

* **No relationship between tasks except order.** A workflow step is a `Task`
  with `workflow_id`, `step_id` and `depends_on`
  (`apps/common/swarm_common/models.py::Task`). `depends_on` is ORDER, not
  parentage: the step that produced your input did not create you, cannot
  cancel you and is not waiting on you. S1 forbids drawing a tree from it.
* **No route, no credential, no worker path.** The agent's environment is built
  rather than inherited (`Workspace.child_env`,
  `apps/agent-worker/agent_worker/workspace.py::Workspace.child_env`) and carries its own ids
  (`_build_child_env`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`; the
  three ids at `apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`), but no
  API address and no credential. The planner's notes cited lines 3101-3117 for
  this; those lines are `_git_token` today, and the environment moved down.
* **A free `metadata` convention would be worse than nothing.**
  `TaskCreate.metadata` (`apps/swarm-api/swarm_api/schemas.py::TaskCreate.metadata`) is a free
  dict: any caller could claim any parent, including one in another tenant,
  and "this task's children" would be a scan.

So the feature is four things at once, and building any one of them alone is
how it goes wrong: a typed parent the API sets (CR 14), a submission path that
gives the agent no credential (OD-B15-2), a way for the parent to wait that
costs nothing (OD-B15-3), and a cancel rule (OD-B15-4). CR 14 itself said the
cancellation and capacity rules must be decided WITH the field, not after it;
this document is where they are decided.

### Why not let the agent poll

An agent can already submit work and poll for it if someone hands it a
credential. That is the shape this design exists to prevent. A parent that
polls holds its lease for the whole wait (invariant 4 forbids exactly that);
on a narrow pool it holds the slot its own children need, and the tree
deadlocks with every member waiting and none running. So the agent is given
**no channel that reports a child's progress while it runs**. The only way to
learn what a child did is to `await`, and `await` gives the slot back.

---

## 2. The owner's decisions

| Decision | What was decided | Where this document answers it |
|---|---|---|
| **OD-B15-1** | Accept CR 14 WITH the cancel and capacity rules added. | §4 (capacity: invariants 1-4), §3.4 (cancel), the CR 14 amendment |
| **OD-B15-2** | The agent submits a child THROUGH ITS WORKER (spool or localhost endpoint). The worker validates and calls a route that accepts only the worker identity and checks it against the live lease: task, attempt, generation. The agent never holds a credential. | §3.1, §3.2, §6.1, invariant 5 and 9 |
| **OD-B15-3** | The parent submits, then `await` checkpoints and parks holding no capacity, and is promoted when every child is terminal with its outputs staged. Depth 1 and a fan-out cap. | §3.3, §7, invariants 1, 4 and 8 |
| **OD-B15-4** | Cancelling a parent cancels its children. | §3.4, §5 F5-F7 |

**Spool, not a localhost endpoint (OD-B15-2 left the choice open).** The
worker already takes signals from its agent as files in `work/`: the quota
signal (`Workspace.quota_path`,
`apps/agent-worker/agent_worker/workspace.py::Workspace.quota_path`) and the refused-credential
signal beside it. A file spool:

* adds **no listening socket**. A Cloud Run execution has no inbound surface
  today ([security.md](../security.md#worker-to-worker-attacks)); a localhost
  server would be reachable by every process in the container, which is the
  agent's own reach anyway, and would add a parser running inside the
  non-dumpable worker process that holds the attempt key (§3.2);
* **survives a checkpoint.** `work/` is archived every
  `checkpoint_interval_seconds`, so a resumed attempt knows which requests were
  already answered without asking anyone;
* needs nothing from the runner image: any agent that can write a file can use
  it, the mock and the generic runner included.

---

## 3. The flows

### 3.1 Submitting a child

```
agent                       worker (same container,          swarm-api                Firestore
                            non-dumpable process)
  |  write requests/<rid>.json   |                                |                        |
  |----------------------------->|  next control poll (10 s)      |                        |
  |                              |  validate shape and size       |                        |
  |                              |  POST /v1/tasks/{parent}/children                       |
  |                              |   Authorization: ID token (tenant worker GSA)           |
  |                              |   X-Swarm-Attempt-Proof: <proof>                        |
  |                              |------------------------------->| verify token + proof   |
  |                              |                                | ONE transaction:       |
  |                              |                                |  read parent + lease ->|
  |                              |                                |  fence, depth, fan-out |
  |                              |                                |  dedupe on request id  |
  |                              |                                |  create child (QUEUED, |
  |                              |                                |  signed spec)        ->|
  |                              |<-------------------------------| 201 {task_id}          |
  |  responses/<rid>.json        |                                |                        |
  |<-----------------------------|                                |                        |
```

1. **The agent writes one request per file** into `$SWARM_CHILDREN/requests/`
   (`work/.swarm-children/requests/<request_id>.json`), atomically (write
   `.tmp`, rename). A request names a `request_id` the agent chooses
   (`[a-z0-9-]{1,64}`) and the child: `runner_profile`, `resource_class`,
   `input`, optionally `provider`, `model`, `timeout_seconds`,
   `repository_ref`. Nothing else is accepted (§6.1).
2. **The worker picks requests up on its control poll**, the tick that already
   reads `cancel_requested` (`control_poll_seconds`, the run loop at
   `apps/agent-worker/agent_worker/lifecycle.py::Worker._run_child_supervised`), at most
   `max_child_requests_per_tick` per tick. It refuses locally what the route
   would refuse anyway (size, unknown keys, a worker-action profile, depth) so
   the common mistake costs no network round trip and writes a refusal the
   agent can read at once.
3. **The worker calls the route** with two things the agent does not have: an
   ID token it mints for the tenant's worker service account, and the
   **attempt proof**, a signature over the request made with the attempt key
   it registered before the agent existed (§3.2). The token and the private
   key stay in the worker's heap; neither is written to `work/`, a log line,
   an event or a response.
4. **swarm-api checks everything in one Firestore transaction** (§6.1) and
   creates the child in `QUEUED`, with `parent_task_id` and
   `parent_attempt_id` set from the attested registration the proof verified
   against, never from the child body. The child goes
   through admission exactly like any other task.
5. **The worker writes the answer** to `responses/<request_id>.json`: the child
   task id, or a refusal with a machine-readable `code` and a sentence. The
   request file is removed only after the response is written, so a crash
   between the two leaves the request to be submitted again, which the route
   answers idempotently (§5 F1).

A child's own worker does not create `$SWARM_CHILDREN` and does not set the
variable: it knows its task has a `parent_task_id`, and depth is 1.

### 3.2 What makes "only the worker" true: the attempt key

**The agent can mint the same ID token the worker mints.** It runs in the
worker's container as the worker's uid, and nothing filters
`169.254.169.254` ([security.md](../security.md#cloud-metadata-abuse);
[merge-step.md §0](../merge-step.md#0-the-constraint-that-shapes-everything-else),
`docs/merge-step.md` (`Any agent can act as its tenant's service account.`)).
A route that accepted "the tenant's worker service account" would therefore
accept the agent calling it directly with `curl`, past every check the worker makes. Calling that identity "the worker
identity" would be a name for a hope.

**The agent can also read the whole container environment.** `tini` is PID 1
(`images/agent-runtime-base/Dockerfile` (`ENTRYPOINT ["/usr/bin/tini"`)), it holds every variable the
execution was started with, and it is not non-dumpable, so the agent, as the
same uid, reads `/proc/1/environ`. The worker's own `prctl` changes nothing
there: `apps/agent-worker/agent_worker/hardening.py` (`WHAT IT DOES NOT COVER`) says so, and it is
why the worker keeps no credential in its environment. The same holds for a
file mounted into the container, which the agent opens as the same uid, and
for anything under `work/`. **So nothing that authorises a child submission may
arrive through the environment, a mounted file or the workspace, and live
there while the agent runs.** An earlier draft of this design passed an HMAC
"attempt proof" as an execution environment override beside `LEASE_ID`; that
proof was readable by the agent and is withdrawn.

What the agent cannot read is the worker's heap after `make_non_dumpable`
succeeds, and what the agent cannot do is act before it exists. The design
uses both:

```
scheduler (dispatch)        worker (before the agent exists)        swarm-api
  nonce = HMAC(k, "swarm-child-nonce/v1" | tuple)
  -- env SWARM_CHILD_NONCE -->|
                              | make_non_dumpable() == PROTECTED
                              | generate Ed25519 keypair (heap only)
                              | POST /v1/attempts/{attempt_id}/child-key
                              |   ID token + tuple + nonce + public key ->| verify nonce
                              |                                           | task STARTING, lease live
                              |                                           | ONE transaction, first wins:
                              |                                           |  write registration, attested
                              |<------------------------------------------| 201
                              | STARTING -> RUNNING, then spawn the agent
```

The tuple is `(tenant_id, task_id, attempt_id, lease_id, generation)`.

1. **The scheduler mints a one-use registration nonce at dispatch**:
   `HMAC-SHA256(child_key, "swarm-child-nonce/v1\n" tuple)`, passed as one
   more execution override beside `LEASE_ID` and `GENERATION` (which the
   worker already reads, `apps/agent-worker/agent_worker/config.py::WorkerConfig.from_env`).
   The agent WILL be able to read it later from `/proc/1/environ`. That is
   acceptable only because of step 3: by the time the agent exists, the nonce
   has been spent.
2. **The worker generates an Ed25519 keypair after `make_non_dumpable` reports
   `PROTECTED`**, in its heap, and never writes the private key anywhere: not
   to the environment, a file, `work/`, a log line or an event. The worker
   already depends on `cryptography` (`apps/agent-worker/pyproject.toml` (`"cryptography>=42",`)).
3. **It registers the public key before the agent exists.** Between lifecycle
   step 1 (the fence) and step 2 (`STARTING -> RUNNING`,
   `apps/agent-worker/agent_worker/lifecycle.py::Worker._prepare`), the worker calls
   `POST /v1/attempts/{attempt_id}/child-key` with its ID token, the tuple, the
   nonce and the public key (§6.1a). swarm-api verifies the nonce, re-reads the
   task and its lease in one transaction, accepts only while the task is
   `STARTING` with this live lease, and **accepts once per generation**: a
   second registration for the same tuple, from anyone, is `409
   child_key_taken`. The agent is spawned at lifecycle step 7, after `RUNNING`,
   so it can neither win the race nor register later: the slot is taken and
   the task is no longer `STARTING`.
4. **swarm-api attests the registration with its own key, not with a field a
   tenant can write.** The registration is stored at
   `child_keys/<HMAC(child_key, "swarm-child-reg-id/v1\n" tuple)>`, a document
   id no tenant identity can compute and, lacking `datastore.entities.list`,
   cannot enumerate ([multi-tenancy.md](../multi-tenancy.md): "It cannot
   enumerate anything", "It cannot delete anything"). Its body carries the
   tuple, the public key and
   `attestation = HMAC(child_key, "swarm-child-reg/v1\n" tuple "\n" public_key)`.
   swarm-api trusts a registration only when the attestation verifies, so a
   tenant identity that somehow found and rewrote it produces a registration
   the route refuses -- a denial of child submission to that one attempt,
   never a key of the writer's choosing.
5. **Every child request is signed with the private key.** The worker signs
   `"swarm-child-req/v1\n" method "\n" path "\n" sha256(body) "\n" timestamp`
   and sends it as `X-Swarm-Attempt-Proof` with `X-Swarm-Attempt-Timestamp`.
   This signature is the **attempt proof** the rest of this document names.
   swarm-api verifies it against the attested public key for the body's tuple,
   refuses a timestamp more than `child_proof_skew_seconds` from its clock,
   and the transaction's `request_id` dedupe makes a replay inside that window
   answer with the child it already made (§5 F1), never a second one.

**When the worker cannot protect its heap, it spends the nonce anyway.** If
`make_non_dumpable` does not report `PROTECTED`, the worker registers a
**tombstone** (`"key": null`, `"refused": "worker_unprotected"`) in step 3's
slot, so the nonce the agent will read is worthless, and offers no child path
(§5 F12), exactly as it refuses to hold the tenant git token
(`_git_token_refusal`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._git_token_refusal`).

**The residual, stated plainly.** If neither a key nor a tombstone could be
registered (swarm-api unreachable at startup past
`child_submit_retry_seconds`), the worker starts the agent without a child
path, and the slot stays empty. The task is then `RUNNING`, so the route
refuses a registration; an agent that rewrites its OWN task document back to
`STARTING` (its tenant identity can write a document whose id it knows) could
then register a key of its own. What that buys is child submission for its
own task, under every limit the route enforces -- the power its worker would
have exercised for it, minus the worker's local checks, which are a
convenience. It buys nothing for any other task: another task's nonce is in
another container's environment. Phase 2 measures the empty-slot rate as
an event (`child_key_unregistered`) so the residual is visible, not assumed.

* **The key `child_key` is held by two platform identities only**:
  `swarm-scheduler` (mints nonces) and `swarm-api` (verifies nonces, derives
  registration ids, attests), as Secret Manager secret `swarm-child-key`,
  value created by `scripts/create-secrets.sh`, never a Terraform secret
  version. No tenant service account is an accessor. Every use is
  domain-separated by its prefix, so a nonce is never a registration id or an
  attestation.
* **It is bound to the generation.** A re-dispatch mints a new generation, a
  new nonce and a new registration slot; the old worker's key is registered
  under a tuple the lease no longer carries, and the route refuses it
  (invariant 5).
* **A leaked private key is worth exactly one attempt.** It authorises child
  submission for one attempt of one task while its lease is live, under the
  route's limits, and nothing after.

**And every limit is enforced at the route anyway.** Depth, fan-out, the
profile allowlist, the repository rule and the input bounds are enforced at the
route, from documents only swarm-api writes or from the attested registration,
never from the worker's word. The worker's checks are a convenience. If the
attempt key were ever exposed, an agent would get the power its worker already
has, bounded the same way, and not one child more.

### 3.3 Awaiting children

```
agent writes await, exits 0
worker: answer every outstanding request (bounded retry, §7)
        -> no live child?  ignore the await, finish normally
        -> checkpoint("child-await")
        -> upload outputs (as the quota park does)
        -> park(CHILDREN_INCOMPLETE), refund the attempt (bounded), release lease, exit
scheduler drain: PARKED(CHILDREN_INCOMPLETE) sweep
        -> every child terminal, every SUCCEEDED child's manifest present -> READY
        -> past child_await_max_seconds -> cancel the outstanding children (they become terminal)
admission: READY -> LEASED (new attempt, new generation, new nonce, new key)
worker: restore checkpoint, stage children's results, start the agent again
```

1. **The agent asks to await** by writing `$SWARM_CHILDREN/await` and exiting
   0. Its process ends: the CLI runners have no conversation resume
   (`runners/cliagent.py` has none), so the agent must have written whatever
   plan it needs into `work/` before it awaits. The checkpoint carries it.
2. **The worker answers every outstanding request first**, so a park never
   leaves a request half-submitted (§5 F3).
3. **No live child, no park.** If every child is already terminal the await is
   logged and ignored, and the attempt ends as the agent's exit says.
   Otherwise the worker parks exactly as the quota path does
   (`_park_for_quota`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._park_for_quota`;
   `ControlPlane.park`, `apps/agent-worker/agent_worker/control.py::ControlPlane.park`):
   checkpoint, upload outputs, park, release, exit. `RUNNING -> PARKED` is a
   legal transition already
   (`apps/common/swarm_common/states.py::_ALLOWED`).
4. **The park has its own reason, `ParkReason.CHILDREN_INCOMPLETE`**
   (request 40). It cannot reuse `DEPENDENCY_INCOMPLETE`: the scheduler's
   dependency sweep promotes a `DEPENDENCY_INCOMPLETE` task whose `depends_on`
   is empty at once, with reason `no_dependencies`
   (`apps/scheduler/scheduler/loop.py::Scheduler._promote_dependencies`). A parent awaiting children has an
   empty `depends_on`, so it would be promoted on the next drain, re-run, await
   again, and loop -- a container start per drain for as long as the children
   run. Reusing it would also be the hierarchy-from-`depends_on` S1 forbids, in
   reverse.
5. **The await does not spend an attempt, up to a bound.** Admission increments
   `attempt_count` on every lease (`apps/common/swarm_common/admission.py::acquire_lease_in_transaction`),
   so a parent that awaits twice under `max_attempts = 3` would be failed for
   waiting. The park's own fenced transaction decrements `attempt_count` by one
   and increments `metadata.child_await_resumes`, while fewer than
   `max_child_await_resumes` are used -- the same bounded-refund shape as the
   reconciler's startup refund (`count_startup_end`,
   `apps/reconciler/reconciler/model.py::count_startup_end`, #67). Past the bound the park
   counts like any attempt, so a parent that awaits forever still ends at
   `max_attempts`. `child_await_resumes` joins `RESERVED_METADATA_KEYS`
   (`apps/swarm-api/swarm_api/validation.py::RESERVED_METADATA_KEYS`): a caller may not set it.
6. **Promotion is the scheduler's**, in a sweep beside `_promote_dependencies`
   (`apps/scheduler/scheduler/loop.py::Scheduler._promote_dependencies`), for the reason that sweep gives: a
   worker that dies right after its last child's success cannot strand the
   parent, because nothing waits for the child to announce itself. The sweep
   lists each awaiting parent's children with a tenant-scoped query (the
   scheduler can query; a tenant identity cannot,
   [multi-tenancy.md](../multi-tenancy.md)), and promotes when every child is
   in `TERMINAL_STATES` and every `SUCCEEDED` child's artifact manifest is
   written. That is "terminal with their outputs staged" in OD-B15-3: the
   outputs are durable in the tenant's GCS prefix; the worker stages them into
   the workspace on resume (step 7).
7. **The resumed worker stages the results before the agent starts.** It asks
   `GET /v1/tasks/{parent_task_id}/children` under the same worker
   authentication (the worker cannot list Firestore), writes
   `$SWARM_CHILDREN/results/children.json` (each child's id, `request_id`,
   state, `end_cause`, `parent_attempt_id`, and the paths below) and stages
   each `SUCCEEDED` child's artifacts under
   `$SWARM_CHILDREN/results/<child_task_id>/` through the same code that
   stages `input_from` (`stage_inputs`,
   `apps/agent-worker/agent_worker/inputs.py::stage_inputs`), which builds every object
   key from THIS attempt's tenant (`fetch_upstream_task`,
   `apps/agent-worker/agent_worker/inputs.py::fetch_upstream_task`). It then starts the agent
   again with the same task input; the agent knows it is resuming because
   `children.json` exists.
8. **An agent that exits 0 with live children and no `await` is awaited
   anyway.** A parent never reaches `SUCCEEDED` over children that are still
   running: their outputs would land after the only consumer had gone, and
   nothing would ever read them. An agent that exits non-zero fails its attempt
   in the ordinary way; its children keep running, and the retried attempt
   finds them in `children.json` (§5 F4). An attempt with NO child path (its
   registration failed, or its heap was unprotected) whose restored spool
   records children cannot list them, so it parks conservatively rather than
   skip this await; the scheduler's sweep, which reads the children itself,
   promotes it once they are done.

### 3.4 Cancelling

OD-B15-4: **cancelling a parent cancels its children.** Settled further here,
because the first screen that draws the tree will be asked:

| Event on the parent | Its non-terminal children |
|---|---|
| a person cancels it (`POST /v1/tasks/{id}/cancel`, or its workflow is cancelled) | cancelled, `why: parent_cancelled` |
| it ends `FAILED` or `DEAD_LETTERED` (retries spent, a lost worker, a dispatch failure) | cancelled, `why: parent_ended` |
| its attempt fails and it is retried (`FAILED -> READY`, `RUNNING -> READY`) | **kept**: they belong to the task, and the next attempt finds them |
| its attempt is fenced (the reconciler took the generation back) | **kept**, for the same reason |
| it awaits past `child_await_max_seconds` | the outstanding ones cancelled, `why: await_expired`; the parent is then promoted |
| a child is cancelled, fails or dead-letters | nothing happens to the parent: the parent reads the end in `children.json` and decides |

How it is done:

1. **The API's cancel cascades at once.** After `request_cancel`'s own
   transaction commits on the parent (`apps/swarm-api/swarm_api/store.py::Store.request_cancel`),
   the API lists the parent's non-terminal children (tenant-scoped query on the
   new index) and runs the same `request_cancel` transaction on each, recording
   `by: cascade:<parent_task_id>`. A child that holds no capacity goes straight
   to `CANCELLED`; one that does is flagged and keeps its lease until its
   worker or the reconciler releases it, exactly as a direct cancel does today.
   The API never releases capacity (invariant 1).
2. **The scheduler makes it certain.** A drain sweep cancels any non-terminal
   child whose parent is terminal or carries `cancel_requested`. The API's
   cascade is for responsiveness; the sweep is what guarantees it, so an API
   that died between the parent and its third child strands nothing. Latency
   is bounded by the 1-minute safety tick.
3. **The cause is typed.** A cascaded child ends with
   `EndCause.CHILD_CASCADE` (request 41) and the `why` above in its event
   detail. It cannot be `CANCELLED_PARENT`, which already means "an UPSTREAM
   workflow step was cancelled" (`EndCause`,
   `apps/common/swarm_common/models.py::EndCause.CANCELLED_PARENT`), and folding a cascade into
   `CANCEL_REQUESTED` would make the outcome ledger count a parent's failure as
   a cancel somebody pressed -- the confusion request 23 was accepted to end.
4. **Tenant first.** Both the cascade and the sweep act only on a child whose
   `tenant_id` equals its parent's. A child document whose `parent_task_id` was
   rewritten to name another tenant's task (§5 F11) is logged and left alone.

---

## 4. The invariants

### Invariant 1 -- only LEASED/DISPATCHED/STARTING/RUNNING create demand

A child is created `QUEUED` and costs nothing until admission leases it, like
any task. An awaiting parent is `PARKED`, holds no lease and has no container:
the await is a park, not a wait. Nothing about a tree is expressed as pending
pods or pending executions; the backlog stays in Firestore. The cancel cascade
never decrements a pool: a child that holds capacity is flagged and released
by its worker or the reconciler, as every cancel is today
(`request_cancel`, `apps/swarm-api/swarm_api/store.py::Store.request_cancel`). The `children`
route creates documents, not infrastructure, so a burst of submissions cannot
start a single container on its own.

### Invariant 2 -- capacity is reserved all-or-nothing, in one transaction

Each child acquires its own lease through the unchanged
`acquire_lease_in_transaction`, all-or-nothing across every pool
`pool_names_for` names for it, in one transaction. The parent reserves nothing
on its children's behalf. A gang reservation for a whole fan-out was
considered and refused: it would hold capacity for children still `QUEUED`
(an invariant 1 violation), and it would need an admission path across N
tasks that the frozen `admission.py` does not have. Without it, children start
as capacity allows, and a tree on a narrow pool runs its children a few at a
time rather than deadlocking.

### Invariant 3 -- concurrency counts from LEASED

Children are counted from `LEASED` like every task, against the same tenant,
runner, resource, backend and provider pools as their parent. The fan-out cap
is not a concurrency control and does not replace one: sixteen children of one
parent on a tenant whose limit is four run four at a time, the rest wait in
`READY` costing nothing. The parent's own slot is given back when it awaits,
so the slot a child needs is never held by the parent waiting for it.

### Invariant 4 -- workers never sleep through a long wait

Waiting for children is the longest wait this platform will have, and the
worker never sleeps through any of it: there is no in-worker wait for a child
at all, of any length. `await` checkpoints, parks, releases and exits. The
only bounded wait inside the worker is answering outstanding submissions
before a park, capped by `child_submit_retry_seconds`, which is below
`max_in_worker_retry_delay_seconds` (45 s,
`apps/common/swarm_common/config.py::Settings.max_in_worker_retry_delay_seconds`).
And because the agent gets no
progress channel for a running child (§1), an agent that tries to busy-wait
has nothing to wait on.

### Invariant 5 -- every attempt carries a fencing generation

The attempt key is registered under, and the nonce that registered it is
bound to, tenant, task, attempt, lease and generation, and the
route re-reads the parent task and its lease inside its transaction: the
task's `current_lease_id` and `current_generation`, the lease's `task_id`,
`attempt_id` and `generation`, the lease not released and not expired, the
task `RUNNING` and not `cancel_requested`. Any mismatch is `409
child_submit_fenced` and creates nothing. A worker that receives it is a
superseded worker: it ends the way a fenced worker does mid-run
(`_exit_fenced_mid_run`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._exit_fenced_mid_run`),
without touching the lease. The await park is a fenced transition like the
quota park, so a stale worker cannot park the task or refund an attempt.
Registration is fenced the same way: accepted only while the task is
`STARTING` with this live lease, once per generation (§3.2).

### Invariant 6 -- Spot is disabled platform-wide

Unchanged. A child runs on its runner profile's backend through the same
dispatcher and the same Job and pod templates as any task. Nothing in this
design names a node, a provisioning model or a backend parameter, and nothing
a child request carries can reach one: the request schema has no field for it
(§6.1), and `runner_profile` resolves to a backend only through the frozen
catalogue. A child is never a reason to relax the no-preemption requirement,
because a parent waiting on a preempted child is just a longer wait.

### Invariant 7 -- requests == limits

Unchanged. A child names a `resource_class` by name; the catalogue turns it
into equal requests and limits as for any task. The child request carries no
CPU, memory or disk figure, and the worker's local check and the route both
refuse one by schema (unknown keys are refused). A child's timeout is clamped
to the parent's own `timeout_seconds`, so a child cannot buy a longer
execution than the task that asked for it, and `input` can lower the runner's
ceilings and never raise them, as `runners/limits.py` already enforces.

### Invariant 8 -- mandatory periodic checkpointing

The parent keeps its periodic checkpoints while it runs, and `await` adds one
more, `child-await`, before the park: a parent that parked without one would
resume from its last periodic checkpoint and lose the work between it and the
await, including the plan the agent wrote for its own resumption. The spool
lives in `work/`, so the checkpoint carries which requests were answered; a
resumed attempt never resubmits a request it already has an answer for, and
one it lost is answered idempotently (§5 F1). Children checkpoint as every
task does; nothing about being a child relaxes it.

### Invariant 9 -- per-tenant isolation

A child is always created in its parent's tenant, which the route takes from
the attested registration, never from the body. The ID token must belong to that tenant's
worker service account, and the route derives the expected email from the
tenant id by the same rule Terraform and `register-tenant.sh` name it
(`swarm-agent-worker-<tenant>`: `apps/common/swarm_common/identity.py::worker_service_account_id`,
`scripts/register-tenant.sh` (`GSA_PREFIX="swarm-agent-worker-"`),
`terraform/modules/tenancy/main.tf` (`sa_account_id = { for t, _ in var.tenants`)) --
**never** from `tenants/<id>.service_account`, a document any tenant identity
can rewrite ([multi-tenancy.md](../multi-tenancy.md), "It CAN read and write a
document whose id it can guess"). Request 43 asks for that rule to have one
public home. Staged child outputs come from the attempt's own tenant prefix by
construction (`stage_inputs`). The cascade, the sweeps and the listing are all
tenant-scoped, and a child's spec signature covers its parent fields
(request 42), so a cross-tenant rewrite of `parent_task_id` is refused by the
child's own worker. Child ids are random `new_id("task")` ids, not derived from
the parent: a derivable id would be guessable, and an unguessable id is the
only thing between a tenant identity and another tenant's document.

### Invariant 10 -- callers pick a runner_profile by name

A child request names `runner_profile`, `resource_class`, `provider` and
`model` by name and is validated by the same code `POST /v1/tasks` uses. It
carries no image, command, resource figure or backend parameter, and it never
supplies `parent_task_id` or `parent_attempt_id`: those are the platform's
decision, set from the attested registration (CR 14's own rule). `POST /v1/tasks` keeps
refusing both fields, because `TaskCreate` is a strict model
(`apps/swarm-api/swarm_api/schemas.py::StrictModel.model_config`). Worker-action profiles (`merge`,
`post-verdict`) are refused as children: they act on a forge with a
credential no agent may steer. `repository_url` is copied from the parent and
never accepted, so a child cannot point the tenant's git credential at a
repository the person who started the parent did not choose.

---

## 5. Failure cases

| # | What happens | What the design does |
|---|---|---|
| F1 | The worker crashes after the route created the child and before it wrote the response. | The request file is still there. The resumed attempt submits it again; the route finds the existing child by `(parent_task_id, request_id)` in the same transaction and answers `200` with it. No duplicate. |
| F2 | swarm-api is unreachable or answers 5xx. | The worker retries within `child_submit_retry_seconds`, never longer. It then writes a refusal with `code: api_unavailable, retryable: true`; the agent may write the request again with the same `request_id`, which stays idempotent. |
| F3 | The agent awaits while requests are unanswered. | The worker answers each first (F2's bound), then parks. A park never carries a request it did not answer. |
| F4 | The parent's attempt fails and is retried, or its attempt is fenced, while children run. | The children are kept: they belong to the task. The next attempt finds them in `children.json` and may await them. The fan-out cap counts per task across attempts, so a retry loop cannot multiply children. |
| F5 | A person cancels the parent while children run. | The API cascades at once; the scheduler sweep makes it certain (§3.4). Running children are flagged and stop through the ordinary cancel path. |
| F6 | The parent ends `FAILED` or `DEAD_LETTERED` (retries spent, a lost worker, a dispatch failure) with live children. | The scheduler sweep cancels them with `why: parent_ended`. Nothing is left to consume their outputs. |
| F7 | A child never ends: parked on a credential nobody registers, or a provider outage. | At the await deadline, `child_await_max_seconds`, the awaiting parent's outstanding children are cancelled with `why: await_expired`; they become terminal and the parent is promoted with their ends in `children.json`. |
| F8 | A child fails. | Reported in `children.json`. The parent decides; there is no upward cascade and no automatic re-submission beyond the child's own `max_attempts`. |
| F9 | A `SUCCEEDED` child's artifact has expired or cannot be staged on resume. | Listed in `children.json` with `outputs: unavailable` and the reason, and the agent still starts. Unlike a declared `input_from`, a helper's lost output is information for the main agent, not a reason to fail it. |
| F10 | The fan-out cap or depth is reached. | `409 child_fan_out_exceeded` or `409 child_depth_exceeded`, written to the response file. Not 429: retrying would not change the answer. |
| F11 | Some tenant's agent rewrites a child's `parent_task_id` to name another tenant's task, or a task in its own tenant. | The cascade and the sweeps act only on a child in its parent's tenant. Request 42 puts the parent fields inside the signed spec, so the rewritten child's own worker refuses to run it (`SPEC_SIGNATURE_INVALID`). Request 42 does NOT stop the scheduler's cascade sweep: the sweep reads `parent_task_id` and does not verify signatures, so it still cancels a same-tenant task whose `parent_task_id` was rewritten to name a cancelled parent. That grants nothing new -- a tenant identity can already set `cancel_requested` on any task of its tenant whose id it knows -- but it is not closed by request 42, and the sweep's `why: parent_cancelled` event on such a task is the only trace. |
| F12 | The worker's memory protection was not established, so an attempt key would sit in a readable heap. | The worker generates no key. It registers a tombstone in the attempt's slot before the agent exists, so the nonce the agent can read in `/proc/1/environ` is spent; it does not create `$SWARM_CHILDREN` and refuses every request with `code: worker_unprotected`, as it refuses to read the git token (`_git_token_refusal`). |
| F13 | `swarm-child-key` is rotated while attempts run. | swarm-api accepts a nonce or an attestation under the current and the previous secret version and derives registration ids under both; the scheduler mints with the current one. A rotation is two steps a dispatch window apart. |
| F14 | The parent has used `max_child_await_resumes` and awaits again. | The park counts the attempt; at `max_attempts` the scheduler's await sweep dead-letters the parent as soon as it finds it -- it does not wait for the children, because no resume is coming to read them -- and F6 cancels its children as `parent_ended`. |
| F15 | The agent awaits with no children. | Logged and ignored; the attempt ends as the agent's exit says. No park, no container restart. |
| F16 | A tenant is disabled while its parent awaits. | The sweep promotes the parent; admission refuses to lease a disabled tenant's work, as today. The children are unaffected by the sweep. |
| F18 | The agent reads `SWARM_CHILD_NONCE` from `/proc/1/environ` and calls the registration route, or calls the children route with the tenant ID token and no key. | Registration: `409 child_key_taken`, because the worker spent the slot before spawning it, and `409 child_key_window_closed` anyway, because the task is `RUNNING`. Children route: `403 child_submit_unproven`, because nothing it holds verifies against the attested key. The only residual is §3.2's empty slot. |
| F19 | The worker crashes after registering and the same generation's container restarts. | The slot is taken by the first key, which died with the first process. The restarted worker's registration is `409 child_key_taken`; it offers no child path for this attempt (`child_key_unregistered`) and runs the agent otherwise unchanged. The next attempt has a new generation and a new slot. |
| F17 | The child's own worker sees a task with a `parent_task_id` and the agent writes requests anyway. | The variable and directory do not exist for a child; a request written by hand is never read. The route refuses it too (depth). |

---

## 6. Routes and fields

### 6.1 `POST /v1/tasks/{parent_task_id}/children` (worker only)

Authentication, all required, checked in this order:

1. A Google ID token whose audience is swarm-api, `email_verified` exactly
   `True`, and whose email and subject belong to
   `swarm-agent-worker-<tenant_id>@<project>.iam.gserviceaccount.com`, where
   `tenant_id` is the body's. Human callers are refused here (`403`), tenant
   members and admins included: a person has no attempt.
2. `X-Swarm-Attempt-Proof` and `X-Swarm-Attempt-Timestamp`: an Ed25519
   signature (§3.2 step 5) verified against the public key of the
   registration for the body's `tenant_id`, `task_id` (the path's
   `parent_task_id`), `attempt_id`, `lease_id` and `generation`. The
   registration is fetched at its derived id and trusted only if its
   attestation verifies under the current or previous `swarm-child-key`
   version. A missing registration, a tombstone, a bad attestation, a bad
   signature or a stale timestamp is `403 child_submit_unproven`.

Body:

```json
{
  "tenant_id": "eng",
  "attempt_id": "att_…",
  "lease_id": "lease_…",
  "generation": 3,
  "request_id": "split-tests-2",
  "child": {
    "runner_profile": "claude-code",
    "resource_class": "small",
    "input": {"prompt": "…"},
    "provider": "anthropic",
    "model": null,
    "timeout_seconds": 1800,
    "repository_ref": null
  }
}
```

In ONE Firestore transaction: read the parent task and its lease; check the
fence (invariant 5); check the parent has no `parent_task_id` (depth); query
the parent's children (tenant-scoped, at most `max_children_per_task` rows)
for this `request_id` and the count; validate the child with the
`POST /v1/tasks` validators; create it.

| Answer | When |
|---|---|
| `201 {"task": {…}, "created": true}` | created |
| `200 {"task": {…}, "created": false}` | this `request_id` already made a child of this parent |
| `403 child_submit_unauthenticated` / `child_submit_unproven` | token or proof (§6.1 steps 1-2) |
| `409 child_submit_fenced` | the lease is not this attempt's live lease, or the parent is not `RUNNING`, or it has `cancel_requested` |
| `409 child_depth_exceeded` | the parent is itself a child |
| `409 child_fan_out_exceeded` | `max_children_per_task` reached |
| `422` | the child fails validation, names a worker-action or disabled profile, or carries an unknown key |

### 6.1a `POST /v1/attempts/{attempt_id}/child-key` (worker only, before the agent exists)

The registration of §3.2 step 3. Authentication: the ID token of §6.1 step 1
and the nonce, verified as `HMAC(swarm-child-key, "swarm-child-nonce/v1\n"
tuple)` under the current or previous version. Body: the tuple, and either
`{"public_key": "<base64url Ed25519>"}` or `{"key": null, "refused":
"worker_unprotected"}`. In ONE transaction: read the task and its lease, check
the fence and that the task is `STARTING`, read the slot at its derived id,
and create it only if it does not exist.

| Answer | When |
|---|---|
| `201` | registered (a key or a tombstone) |
| `403 child_key_unproven` | the token or the nonce does not verify |
| `409 child_key_taken` | the slot for this generation is already filled, by anyone |
| `409 child_key_window_closed` | the task is not `STARTING`, or the lease is not this attempt's live one |

The response carries nothing secret: not the nonce, not the attestation, not
the registration id.

### 6.2 `GET /v1/tasks/{parent_task_id}/children` (worker only)

The same authentication as §6.1, with the proof in the header and the tuple in
query parameters. Returns every child of the parent (bounded by the fan-out
cap): id, `request_id`, state, `end_cause`, `parent_attempt_id`, and the
artifact manifest of a `SUCCEEDED` child. Used by the resumed worker to build
`children.json`, because a tenant identity cannot list Firestore.

### 6.3 `GET /v1/tasks?parent_task_id=` (people and clients)

A new filter on the existing list route
(`apps/swarm-api/swarm_api/routes/tasks.py::list_tasks`), under the same
`tenant_scope` and `submission_scope`, backed by a composite index
`(tenant_id, parent_task_id, created_at)` (`tenant_id` and `parent_task_id`
ascending, `created_at` descending), beside `tasks-tenant-workflow-created`
(`terraform/modules/firestore/indexes.tf` (`"tasks-tenant-workflow-created" = {`)).
`GET /v1/tasks/{id}` gains the
two fields in its body. The console can draw a tree from this; nothing else
is a source for one.

### 6.4 Fields

| Field | Where | Written by | Meaning |
|---|---|---|---|
| `parent_task_id` | `Task` (CR 14) | swarm-api, at creation, from the attested registration | the task whose agent submitted this one; `None` for everything else |
| `parent_attempt_id` | `Task` (CR 14) | swarm-api, at creation, from the attested registration | the parent's attempt that submitted it |
| `metadata.child_request_id` | task metadata, reserved | swarm-api | the agent's `request_id`, the idempotency key |
| `metadata.child_await_resumes` | parent's metadata, reserved | the worker's await park | awaits refunded so far |
| `park_reason = CHILDREN_INCOMPLETE` | parent | the worker's await park | request 40 |
| `end_cause = CHILD_CASCADE` | child | the API cascade, the scheduler sweep, the worker or reconciler ending a flagged child | request 41 |
| `submitted_by` | child | swarm-api | the PARENT's `submitted_by`: the person behind the tree, so `submission_scope` shows a person their own children. The worker identity that made the call is recorded in the child's `submitted` event as `via: worker`. |
| `priority` | child | swarm-api | the parent's; a child cannot outrank its parent |
| `repository_url` | child | swarm-api | the parent's; never accepted |
| `workflow_id`, `step_id`, `depends_on` | child | swarm-api | always empty: a child is not a step, and the workflow rollup (request 7) does not count it |
| `child_keys/<derived id>` | its own collection, not frozen | swarm-api, at registration | the tuple, the public key or a tombstone, and swarm-api's attestation over them (§3.2 step 4); the id is an HMAC no tenant can compute |

The spool, under `$SWARM_CHILDREN` (`work/.swarm-children/`, added to
`Workspace.control_file_names`):

| Path | Written by | Contents |
|---|---|---|
| `requests/<request_id>.json` | agent | one child request (§3.1 step 1) |
| `responses/<request_id>.json` | worker | `{"task_id": …}` or `{"refused": {"code": …, "message": …, "retryable": bool}}` |
| `await` | agent | presence is the request; contents ignored |
| `README.md` | worker, every attempt with a child path | the agent's guide to this table and §3.1-§3.3 (`agent_worker.children.AGENT_GUIDE`, [child-tasks-for-agents.md](../child-tasks-for-agents.md)); the CLI runners add one prompt line naming it |
| `results/children.json`, `results/<child_task_id>/…` | worker, on resume | §3.3 step 7 |

No response, result or event ever carries the token, the nonce, the private
key, the attestation or `swarm-child-key`.

---

## 7. Limits and their defaults

Every limit is a swarm-api or scheduler setting (not frozen), except depth,
which is a constant: a deeper tree would need a different cascade and await
design, not a bigger number.

| Limit | Default | Enforced at | Why this value |
|---|---|---|---|
| `max_child_depth` | 1 | route (authority), worker (convenience) | OD-B15-3. One level keeps the cascade, the await and the console's tree a single hop, and keeps a tree's size linear in the fan-out cap instead of exponential in depth. |
| `max_children_per_task` | 16 | route, inside the creating transaction | Sixteen children plus the parent fit inside a new tenant's `default_tenant_max_active` of 20 (`apps/common/swarm_common/config.py::Settings.default_tenant_max_active`), so a fresh tenant can run one full fan-out without an admin; it is about a third of `max_workflow_steps` (50), because these DAGs are written by an agent, not reviewed by a person. Counted per task across attempts, so retries cannot multiply it. |
| `max_child_await_resumes` | 4 | the worker's await park transaction | A main agent with helpers fans out in a few rounds; four refunded awaits allow that, and the fifth counting as an attempt means a parent that awaits in a loop still ends at `max_attempts` rather than running forever. |
| `child_await_max_seconds` | 86400 | scheduler await sweep | A child can sit parked on a provider's quota reset for hours; a day covers a full daily reset with margin, and past it a parent waiting on a child nobody will ever unblock gets its answer instead of waiting indefinitely. |
| `max_child_request_bytes` | 262144 | worker (refuses to read), route (refuses to accept) | Equal to `max_input_bytes` (`apps/common/swarm_common/config.py::Settings.max_input_bytes`): the route would refuse a larger `input` anyway, and the worker must never read an unbounded file an agent wrote into its own memory. |
| `max_child_requests_per_tick` | 4 | worker control poll | Four per 10-second poll keeps one worker far below `requests_per_second` (20) at the API, so one busy parent cannot starve every other caller of its tenant's rate limit. |
| `child_submit_retry_seconds` | 30 | worker | Below `max_in_worker_retry_delay_seconds` (45), so answering a submission can never become the long in-worker wait invariant 4 forbids; past it the agent gets a retryable refusal. |
| `child_proof_skew_seconds` | 120 | route, on every signed request | Wide enough for an ordinary clock difference between a worker and swarm-api, narrow enough that a captured signature is useless soon after; a replay inside the window is answered by the `request_id` dedupe with the child already made, so the window bounds exposure, not correctness. |
| `child_staged_bytes` | `max_artifact_bytes` | worker staging on resume | The same total the worker already gives `input_from` staging (`stage_inputs`, called with `max_total_bytes=self.cfg.max_artifact_bytes`), because the workspace is memory-backed and every staged byte is RAM the parent's resource class pays for. |

---

## 8. The frozen contract

Phase 2 needs these changes under `apps/common/swarm_common/`, all filed as
requests filed with the CR 14 amendment, none made:

| Request | File | Change | Why it cannot live elsewhere |
|---|---|---|---|
| 14 (accepted, OD-B15-1) | `models.py` | `Task.parent_task_id`, `Task.parent_attempt_id` | every service reads tasks through `Task` |
| 40 | `states.py` | `ParkReason.CHILDREN_INCOMPLETE` | `DEPENDENCY_INCOMPLETE` is promoted at once when `depends_on` is empty (§3.3 step 4) |
| 41 | `models.py` | `EndCause.CHILD_CASCADE` | `CANCELLED_PARENT` already means an upstream step; `CANCEL_REQUESTED` would misfile a failure as a cancel |
| 42 | `specsign.py` | the parent fields inside `canonical_step_spec`, `SPEC_FORMAT = 2` | without it a rewritten parent is undetectable by the child's worker (§5 F11) |
| 43 | `identity.py` | a public `worker_service_account_id(tenant_id)` | the route must derive the worker identity without trusting `tenants/<id>`; today the prefix is private (`_GSA_PREFIX`) |

The phase-2 pull request that edits `swarm_common` passes
`scripts/lib/check-frozen-contract.sh` only if it ADDS a line to
`docs/contract-change-requests.md` containing the acceptance phrase that
script looks for; the acceptance recorded by this phase-1 amendment does not
carry over to it.

---

## 9. Phase 2

In order, each a lane, after its blockers merge (B10, B19, B20, M4, B4, B11,
S1 per the planner):

1. **Contract** (after the owner accepts 40-43): CR 14 and requests 40-43
   applied; `apps/swarm-ui/src/types.ts` gains the two fields and the two enum
   values; `scripts/lib/check-contract-parity.sh` section 5 holds them;
   `swarm_mcp` gains the park reason's sentence beside
   `DEPENDENCY_INCOMPLETE` (`apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE`).
2. **The registration key** (Track C and D): `swarm-child-key`, accessor
   bindings for `swarm-scheduler` and `swarm-api` only, `create-secrets.sh`
   support, the scheduler passing the one-use nonce as an execution override
   on both backends, and the `child_keys` collection. swarm-api gains the
   `cryptography` dependency for Ed25519 verification. Verify then, not
   assumed here: that a tenant service account cannot read a Cloud Run
   execution's overrides (`run.executions.get`) or a GKE pod spec in its
   namespace. The agent in the SAME container reading the nonce is expected
   and harmless (§3.2: it is spent before the agent exists); an agent in
   ANOTHER container of the tenant reading it could race the worker's
   registration in the window between dispatch and lifecycle step 2. If that
   read is possible, phase 2 stops and returns to the owner with the
   measurement rather than shipping the race; a mounted file does not help,
   because the same uid reads it.
3. **swarm-api**: the two worker routes, the list filter, the cascade in the
   cancel path, the reserved metadata keys, the worker-identity dependency.
   The index in `terraform/modules/firestore/indexes.tf`. The network path
   from a worker to swarm-api (VPC egress and invoker grants), which no worker
   uses today.
4. **Worker**: the spool, the await park with its refund, the staging on
   resume, the implicit await, the `worker_unprotected` refusal.
5. **Scheduler**: the await sweep, the cascade sweep, the await deadline.

**Where phase 2 stands (2026-10-02).** Items 1, 3, 4 and 5 are built, and
item 2's deployment wiring is in `terraform/infra/child_tasks.tf`: the
`swarm-child-key` secret (no version in terraform; `scripts/create-secrets.sh
--child-key` generates and adds one), its authoritative accessor binding for
`swarm-scheduler` and `swarm-api` alone, both services reading it as
`SWARM_CHILD_KEY` by Secret Manager reference, the scheduler handing workers
`SWARM_API_URL` and `SWARM_API_AUDIENCE`, a custom audience on swarm-api that
its child routes pin a worker's token to, and `run.invoker` on swarm-api for
every tenant's worker account. The workers already egress `ALL_TRAFFIC`
through the VPC, the path they reach the internal-ingress quota broker by.
**All of it is off until `enable_child_tasks` is set**, and item 2's
measurement -- whether another container of the tenant can read
`SWARM_CHILD_NONCE` -- has NOT been made; it is deferred to #476 with the
other security findings, and the switch must stay off until it is. CLI agents
are told how to use the path by the guide the worker writes into the spool
([child-tasks-for-agents.md](../child-tasks-for-agents.md)).

Tests (phase 2), first red then green:
`uv run pytest tests/unit/common tests/unit/control_plane tests/unit/worker -k 'child or parent or cancel' -q`
and `scripts/lib/check-contract-parity.sh`. Security review: required (S0 --
invariants 9 and 10, and a new credential path).
