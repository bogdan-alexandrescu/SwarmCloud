# Human steps: a workflow waits for a person, at no cost, and resumes on their answer

**Status: design, lane HUMAN-WAIT-DESIGN (2026-10-17, part of #1436). Nothing
here is built.** The owner reads it and approves or redirects it before any
build lane starts. The frozen-contract half is written as requests in §5, not
as changes. The operator files them in
[contract-change-requests.md](../contract-change-requests.md) once they are
accepted. The decisions that are the owner's are in §7, each with a
recommendation first, and in this lane's `questions.json`.

Every file and symbol cited below was read on 2026-10-17 at `35f2a61`. Lines
move, so search for the symbol named beside each one.

What the design settles, in one line each:

* **A person's answer is a workflow step.** The caller names the catalogue
  profile `await-input` for the step, and adds an `await_input` block
  saying what to ask, who may answer, and when it expires. A caller never
  sends an image, a command or a resource spec (invariant 10).
* **Waiting is `PARKED(AWAITING_INPUT)`.** The step never holds a lease, a
  slot or a container. It gets no attempt, so nothing runs to be fenced and
  nothing has to be checkpointed (invariants 1-5, 8).
* **The answer becomes an artifact in GCS.** swarm-api writes the answer
  as the step's named artifact, under the step's own tenant prefix, and
  ends the step `PARKED -> SUCCEEDED`. Contract request 52 already allows
  that transition. Dependants stage the answer with `input_from` like any
  other artifact, and nothing about it passes inline through Firestore.
* **Resume takes one drain.** The answer rings `task_finished`, and the
  scheduler releases the dependants in that same drain, as it does for a
  MERGE verdict published without a worker
  (`apps/swarm-api/swarm_api/verdictpublish.py::on_task_finished`).
* **Expiry is declared.** `on_expire: "fail"` dead-letters the step.
  `on_expire: "default"` ends it with a default answer declared at
  submission, so a `when` gate downstream takes the branch the author chose.
* **People see it where they already look.** It appears as a pill on the
  workflow step, a "needs you" bell item, an inbox entry, a `/sc check`
  line, and a plugin progress sentence. Each one names who the step is
  waiting on.

---

## 1. The problem, as measured

### 1.1 What #1436 reports

The owner's issue (#1436, 2026-10-16) wants to run a **draft -> human review ->
revision** loop as one workflow: an agent writes a draft, a person reviews it
"minutes to days later", and the next agent revises it using the review.
Today:

* **A workflow cannot pause for anyone.** Each round is a new workflow, so
  an N-round review costs N submissions, N workflow documents and N
  separate places to look.
* **The earlier round's artifacts cannot follow.** `input_from` resolves
  only steps of the same workflow. The validator maps
  `{upstream_step: filename}` to task ids at expansion, and the worker
  stages only from `metadata.input_from`, which swarm-api writes
  (`apps/agent-worker/agent_worker/inputs.py::artifact_reference`). The
  caller therefore downloads the draft and pastes it into the next prompt.
  That prompt is bounded by `max_input_bytes`, which is **256 KiB**
  (`apps/common/swarm_common/config.py`, `Settings.max_input_bytes`). A
  staged input can be up to **16 MiB** (the staging cap in
  `agent_worker/inputs.py`), so a draft that stages fine cannot be re-sent
  inline once it passes 256 KiB. Below that size it still travels through
  Firestore as the task's `input`, which is what
  `WorkflowStep.input_from`'s own comment forbids: "Artifacts pass by GCS
  reference, never inline through Firestore"
  (`apps/common/swarm_common/models.py::WorkflowStep`).
* **The review lives outside the platform.** The caller gathers it in their
  own UI, so the platform keeps no record of who reviewed what, or when.

### 1.2 What exists today, and why none of it is this

The platform already has four ways to wait for a person. Each one is close to
this feature, and none of them is it:

| mechanism | what it waits on | why it is not a step that waits |
|---|---|---|
| Live-browser hand-off (`Task.human_wait`, contract requests 68-70) | a person signing in inside a running pod | The task **holds its lease** for the hold, 10 minutes (`human_hold_seconds`), then parks `HUMAN_REQUIRED`. Only `human-ready` promotes it, back to **READY**, which restarts the same agent. It is an interruption of an attempt, not a step with an output. |
| `questions.json` + `POST /v1/tasks/{id}/answers` (TASK-ANSWERS, 2026-10-13) | the owner answering an agent's questions | The answer is stored as `metadata.owner_answers`, and it is "**data, never a channel**" (`docs/workflows.md`, "An agent asks the owner"). No state, dispatch or step reads it. |
| The approvals inbox (`apps/swarm-api/swarm_api/approvals.py`) | a member approving a run, plan, merge or proposal | It decides whether work is **created**. Its decision is a yes or a no, never a payload that a later step stages. |
| Merge-step CI wait (`CI_PENDING`, contract request 49) | a pull request's checks | A sweep promotes it. No person answers it. |

The closest relative is the MERGE-verdict integrator that swarm-api publishes
without a worker (#748, contract request 52). It is a workflow step that sits
`PARKED`, holds nothing, and is ended `SUCCEEDED` by swarm-api in one guarded
transaction. Its dependants are then released by the `task_finished` wake.
This design copies that shape.

### 1.3 What "done" lets someone do that they cannot today

Submit this once:

```bash
./scripts/api.sh POST /workflows '{
  "steps": [
    {"step_id": "draft", "runner_profile": "claude-code",
     "input": {"prompt": "Draft the design. Write draft.md."}},

    {"step_id": "review", "runner_profile": "await-input",
     "depends_on": ["draft"],
     "input_from": {"draft": "draft.md"},
     "input": {"prompt": "Read draft.md. Approve it, or list what must change."},
     "await_input": {"form": "verdict", "artifact": "review.json",
                     "assignees": ["owner@saga.xyz"],
                     "expires_after_seconds": 259200, "on_expire": "fail"}},

    {"step_id": "revise", "runner_profile": "claude-code",
     "depends_on": ["review"],
     "input_from": {"draft": "draft.md", "review": "review.json"},
     "input": {"prompt": "Revise draft.md to answer every finding in review.json."}}
  ]
}'
```

Then:

1. `draft` runs and ends.
2. `review` parks. **No lease is held** while it waits, for minutes or days.
3. The person sees "Workflow wf_… is waiting for your review" in the bell,
   the inbox, `/sc check` and the plugin row. They read `draft.md` in the
   console and post a verdict from the console or the plugin.
4. `revise` is promoted in the drain that the answer's wake triggers. Its
   workspace contains `review.json`, exactly as posted.
5. If nobody answers in three days, `review` ends as declared:
   `DEAD_LETTERED`, and the workflow fails under `fail_workflow`.

Those three checks are the issue's own acceptance criteria. §6's HW-ACCEPT
lane turns them into a release acceptance group.

---

## 2. Constraints that bind the design

### 2.1 The invariants it touches

**Invariant 1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** This
is the reason the feature exists, and it decides everything else. A waiting
step is `PARKED`, which is in `PENDING_STATES` and not in
`CONCURRENCY_STATES` (`apps/common/swarm_common/states.py`). The step never
enters a concurrency state at any point: it is QUEUED or PARKED until its
dependencies end, briefly READY, then PARKED again, then SUCCEEDED, DEAD_LETTERED
or CANCELLED. The scheduler refuses to lease a `control_plane` profile on
every path (§4.4), so a bug that promoted the step to READY would park it
again before admission, and could never start a container. No pod exists to
sit pending, so pending pods cannot become the backlog.

**Invariant 2. All-or-nothing reservation in one transaction.** Nothing is
reserved, so nothing can be half-reserved. The answer transaction touches only
the step's own task document, never a pool. This is also why §3 rejects
designs B and C: both take a lease to deliver an answer, and B would put a
second admission writer beside the scheduler. Contract request 52 refused
that shape in its "If it is declined" paragraph.

**Invariant 3. Concurrency counts from LEASED.** The step never reaches
LEASED, so it never counts. A workflow with forty pending human reviews
occupies zero slots.

**Invariant 4. Workers never sleep through a long wait.** No worker waits
at all. The draft's worker ends when the draft is done, and the revise
step's worker starts only after the answer exists. Contrast the live-browser
hold, which is a worker waiting, bounded to 10 minutes because of this
invariant.

**Invariant 5. Every attempt carries a fencing generation.** The waiting
step has **no attempt**, so nothing runs that a generation could fence. The
same argument was accepted for request 52. What has to be fenced instead is
the race between the writers that can end the wait. There are four: a
person's answer, the expiry sweep, a workflow cancel, and the
failed-workflow sweep. Each is a Firestore transaction that re-reads the
step and commits only if it is still `PARKED` with `park_reason ==
AWAITING_INPUT` and has no recorded answer. Exactly one can commit, and every
other gets a 409 or a stale write (§4.6). Fencing on **resume** is the
dependant's own: `revise` is an ordinary task whose attempt carries
generation 1, and a stale worker of it exits without running the agent, as
every worker does. What it stages is immutable, because a SUCCEEDED task's
`result_summary` is never rewritten, and the answer's object is created
with a no-overwrite precondition by an identity that cannot delete (§4.3).

**Invariant 8. Mandatory periodic checkpointing.** It is unaffected and
still required. The step has no attempt and no workspace, so it has nothing
to checkpoint. The draft's artifacts are its outputs, not a checkpoint, and
the revise step checkpoints every `checkpoint_interval_seconds` as it does
today. **One new constraint follows.** A wait of days must not outlive the
artifacts it is about. The artifact bucket deletes objects
`artifact_retention_days` after their `customTime`: **14 days in dev**,
180 in prod (`terraform/environments/dev/dev.tfvars`,
`terraform/modules/storage/main.tf`). The maximum `expires_after_seconds`
must therefore stay below dev's retention with a margin, or a revise step
would fail to stage a draft that the bucket already deleted. §7 Q4 sets the
number.

**Invariant 9. Per-tenant isolation.** The answer's object key is built from
the **stored** task's `tenant_id` and id, never from the request:
`tenants/<tenant>/tasks/<task>/attempts/input-<nonce>/artifacts/<name>`.
Every segment passes `objects.safe_segment`. The new write grant is
conditioned to that shape (§4.3). A person in another tenant gets the same
404 as for a step that does not exist. Who may answer is decided against
the step's tenant (§4.2). The dependant's worker stages the object through
`artifact_key`, which refuses any URI outside the upstream's own tenant
prefix (`apps/agent-worker/agent_worker/inputs.py::artifact_key`).

**Invariant 10. Callers pick a runner_profile by name.** The step type is
the catalogue name `await-input`. Its `await_input` block carries only
names, bounded numbers and a declared answer shape: a form name from a
closed list, a filename, member addresses, durations, and `fail` or
`default`. It holds no image, command, resource class, backend or timeout
that reaches any compute. swarm-api refuses `resource_class`,
`timeout_seconds`, `builds_on` and `when` on an `await-input` step (§4.1).

Invariants 6 (Spot) and 7 (requests == limits) are untouched: nothing here
creates a pod.

### 2.2 What it needs from `apps/common/swarm_common/`

Three things, all in §5:

1. **A park reason**, `ParkReason.AWAITING_INPUT` (`states.py`). §3.4 says
   why `HUMAN_REQUIRED` cannot be reused without breaking the live
   browser's readers.
2. **A way for a catalogue profile to say it is never leased**,
   `RunnerProfile.control_plane` (`profiles.py`). Today
   `RunnerProfile.__post_init__` refuses an empty `runner_argv` on anything
   but a `worker_action` profile. A worker action is something the worker
   performs in a container, which is exactly what this is not.
3. **The `await-input` catalogue entry** itself, shipped
   `available=False`. A separate owner-applied request flips it on once
   acceptance is green.

What it needs **no** change to, and why:

* **`TaskState` and `_ALLOWED`.** Every transition it uses is legal today:
  `QUEUED -> PARKED`, `PARKED -> READY`, `READY -> PARKED`,
  `PARKED -> SUCCEEDED` (request 52), `PARKED -> DEAD_LETTERED`,
  `PARKED -> CANCELLED`.
* **`WorkflowStep` and `Task`.** The step's declaration rides in
  `metadata.dispatch.await_input`. `metadata.dispatch` is in
  `specsign.SIGNED_METADATA_KEYS`, so the declaration is covered by the
  step's spec signature (`apps/common/swarm_common/specsign.py`). The wait's
  live state (when it opened, the answer record) sits under a reserved
  platform metadata key, `metadata.await_input`. TASK-ANSWERS set the
  precedent with `metadata.owner_answers`, "because the frozen `Task` has no
  field for it" (`apps/swarm-api/swarm_api/validation.py`,
  `RESERVED_METADATA_KEYS`).
* **`EventType`.** The park writes `parked` with
  `detail.reason = "AWAITING_INPUT"`. The answer writes `succeeded` with
  `detail.published_by = "control_plane"` and `detail.source = "person"` or
  `"default"`. Expiry writes `dead_lettered` with `detail.cause =
  "await_input_expired"`. Readers that key on the event type need no new
  member. A reader that wants to know it was a person reads the detail.

---

## 3. Designs that could work

### 3.1 Design A: a control-plane step that swarm-api ends (recommended)

The step is a task under the catalogue profile `await-input`. When its
dependencies have succeeded, the scheduler parks it `AWAITING_INPUT` and
never leases it. swarm-api's new route takes the person's payload, checks it
against the declared form, writes it to GCS as the step's artifact, and ends
the step `PARKED -> SUCCEEDED` with a `result_summary.artifacts` manifest. The
manifest has the shape the worker writes, so `input_from` stages it unchanged.
A per-minute sweep on the existing schedule tick ends expired waits.

* **Code.** Two new swarm-api modules and a route. A guarded park and an
  admission refusal in the scheduler. One new submission field. A console
  card, a bell source and an inbox projection. An MCP tool and plugin
  wording. Roughly the size of #748's control-plane publish, which this
  copies.
* **Migration.** None. These are new fields on new tasks. Every existing
  document decodes as before.
* **Latency.** The answer to a promoted dependant is one GCS write, one
  transaction and one Pub/Sub wake, a few seconds. That is "within one
  scheduler drain", the issue's own bar. Expiry precision is the tick: one
  minute.
* **Money.** Zero while waiting. Each answer costs one GCS object of at most
  256 KiB and three Firestore writes: the task, its event, and the claim.
* **What it breaks.** swarm-api becomes a **writer** to the artifact bucket.
  Today its grant is `roles/storage.objectViewer` and its object module has
  "no upload, no delete and no copy" by design
  (`apps/swarm-api/swarm_api/objects.py`, the module docstring). §4.3
  narrows the new grant to create-only, on `attempts/input-*/artifacts/`
  keys, behind an IAM condition of the same form as the live relay's
  (`terraform/infra/live.tf`, `live_relay_object_condition`), in a separate
  write-only module. That is a security-posture change, so it is §7 Q10.

### 3.2 Design B: the answer is delivered by a worker action

The step parks the same way. The answer is stored in Firestore on the task,
and the step is promoted to READY. A new `WorkerAction.MATERIALISE_INPUT`
then runs in a container: it reads the answer, writes it as an artifact
through the worker's normal upload path, and ends the step. swarm-api keeps
its read-only grant.

* **Code.** About the same as A, plus a worker action in
  `agent_worker/lifecycle.py` and a Job or GKE path for a new profile.
* **Latency.** One container start per answer. Measured starts are a
  **23 s** p50 on GKE and a **128 s** median on Cloud Run (CONTRACT.md,
  request 53).
* **Money.** One lease and one execution per answer, for work a single
  transaction can do. That is the cost #748 was filed to remove.
* **What it breaks.** The payload travels **inline through Firestore** until
  the worker copies it out, which `WorkflowStep.input_from`'s comment
  forbids. The step also holds a lease to write a file. It respects
  invariants 1-3, but spends them for no reason.

### 3.3 Design C: the agent's own task pauses (the live-browser shape)

There is no step type. The drafting agent writes a request file. Its
worker checkpoints, parks the task `HUMAN_REQUIRED` and exits. The person
answers, and the same task is promoted, restored from its checkpoint, and
handed the answer, so the agent revises in its own context.

* **Code.** The most of the three: a spool request, a park path and a
  resume path in the worker, plus a restore that stages the answer. It
  touches the lifecycle, the riskiest file in the worker.
* **Latency.** A container start plus a checkpoint restore per round.
* **Money.** A lease for every resume, and an attempt from `max_attempts`
  for every round. Three review rounds would exhaust a task's default
  `max_attempts: 3`.
* **What it breaks.** The DAG no longer says where the person is, so the
  console cannot show "waiting on X" from the workflow, and `when` cannot
  gate on a person's verdict. It also needs a new resume path that the
  live browser already found hard: LB-RETURN, three race rules
  (`apps/swarm-api/swarm_api/handoff.py`, the module docstring). The
  invariants hold, but by the hardest route.

### 3.4 Within A: a new park reason, or `HUMAN_REQUIRED`

`HUMAN_REQUIRED` already means "only a person ends this". Reusing it would
avoid a contract change, as request 83 reused `CREDENTIAL_MISSING`. But its
readers already act on it, and each would do the wrong thing to an
`await-input` step:

* `Scheduler._expire_human_waits` dead-letters it after
  `human_park_max_seconds`, 24 hours, ignoring the step's own
  `expires_after_seconds` and `on_expire`
  (`apps/scheduler/scheduler/loop.py::Scheduler._expire_human_waits`).
* swarm-api's `human-ready` promotes it to **READY**, which for this
  profile means admission with nothing to run (`apps/swarm-api/swarm_api/handoff.py`).
* The bell titles it "Your agent parked until you take over its browser"
  and links to `/browser` (`apps/swarm-api/swarm_api/notifications.py::handoffs`).
* The plugin says "its agent asked a person to sign in"
  (`apps/swarm-mcp/swarm_mcp/progress.py`, `_PARKED_BECAUSE`).

Request 68 rejected reusing `MANUAL_PAUSE` for exactly this reason: another
reader's promotion rule would fire. A new member is held in place by the
parity script and the UI's exhaustive tables
(`scripts/lib/check-contract-parity.sh`, `apps/swarm-ui/src/types.ts`,
`PARK_REASONS`). A reader that forgets it fails a test. A reader that
forgets to branch on the profile fails nothing. **Recommended: a new
member** (§7 Q1).

### 3.5 Rejected without a full write-up

* **`input_from` across workflows.** This is smaller, but every round is
  still a submission by the caller, and nothing shows who the work is
  waiting on. It solves the re-staging and not the pause.
* **An approval record (`approvals/`) as the step's state.** An approval
  decides whether work is created, and holds no payload a step could stage.
  This design **projects** the waiting step into the inbox (§4.5) without
  moving its state there, as issue runs are projected (`approvals.py`,
  "ISSUE RUNS JOIN THE INBOX WITHOUT MOVING").

### 3.6 Recommendation

**Design A, with a new park reason.** It is the only design in which waiting,
answering and resuming all cost nothing beyond the answer itself. It keeps
artifacts in GCS. It reuses a transition the owner has already accepted
(request 52) and a wake that is already proven (#748). It makes the person
part of the DAG, so the console and `when` can see them. Its one real cost is
a narrow, create-only write grant for swarm-api. B avoids that grant by
spending a container start and passing the payload inline through
Firestore. C avoids a step type by spending an attempt per round.

---

## 4. The recommended design

### 4.1 Submission: the `await_input` block

`POST /v1/workflows` gains one optional step field, `await_input`. It is
valid only on a step whose `runner_profile` is `await-input`, and that
profile requires it.

```json
{
  "step_id": "review",
  "runner_profile": "await-input",
  "depends_on": ["draft"],
  "input_from": {"draft": "draft.md"},
  "input": {"prompt": "Read draft.md. Approve it, or list what must change."},
  "await_input": {
    "form": "verdict",
    "artifact": "review.json",
    "assignees": ["owner@saga.xyz"],
    "expires_after_seconds": 259200,
    "on_expire": "default",
    "default": {"verdict": "MERGE", "findings": []},
    "remind_after_seconds": 86400
  }
}
```

| field | rule | why |
|---|---|---|
| `form` | `"verdict"`, `"answers"` or `"text"`, required | The answer's shape is declared, so it is checked when it arrives and not when a dependant fails to read it. These are the only three shapes a console can render without a schema language. |
| `artifact` | a filename `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`. Default: `verdict.json`, `answers.json` or `input.json`, by form | Dependants stage it by this name. `expected_outputs` inversion does not apply: no agent writes it. |
| `assignees` | 1-10 addresses in the platform's hosted domain. Default: `[submitted_by]` | "Who it is waiting on" is shown everywhere (§4.5). Membership in the tenant is checked when they **answer**, not at submission (§4.2), because Cloud Identity is asked per group and per call (CONTRACT.md, "Cloud Identity group resolution"). |
| `expires_after_seconds` | 300 to the maximum (§7 Q4), required | A wait with no end is a workflow nobody is alerted about, the failure the cycle check exists to prevent (`docs/workflows.md`, "Dependencies and state"). |
| `on_expire` | `"fail"` or `"default"` | §7 Q5. |
| `default` | required if and only if `on_expire` is `"default"`. Checked against `form` at submission, at most 16 KiB | The default is part of the signed spec. It is written to GCS only when it is used (§4.6). |
| `remind_after_seconds` | `null`, or 3600 up to `expires_after_seconds` | §4.5. |

What swarm-api **refuses** on an `await-input` step, each with a 422 that
names the field: `resource_class`, `timeout_seconds`, `builds_on`, `when`,
`allow_empty_diff`, `pr_role` and `merges` (none of them means anything
without a container). Also refused: any `input` key other than `prompt`;
`await_input` on any other profile; `form: "answers"` without exactly one
`input_from` entry whose filename is `questions.json` (the questions the
person answers); and the step as a standalone task. The profile is valid
only inside a workflow.

Under `strategy: "integrate"`, an `await-input` step is never in an
integrator's `integrates`. It pushes no branch. That is the same rule that
leaves a gated integrator's review out (`docs/workflows.md`, "The review's
own branch is not merged").

**Stored** at expansion, in the same write that creates the task:
`metadata.dispatch.await_input` holds the block as normalised (defaults
filled in, addresses lower-cased). It is signed with the rest of
`metadata.dispatch`. `input_from` is expanded to task ids exactly as for any
step. On this step it is not staged: it is **what the person is shown**.

### 4.2 Who may answer

The route accepts a **person** and nothing else. The principal must be a
verified human ID token in the hosted domain. A service-account token is
refused with 403 `person_required`. That covers a tenant's worker GSA, the
scheduler, the schedule tick and the rollup sweeper, and so any agent that
can mint one of those. The refusal is what stops a workflow's own agent from
answering its own review. A child task submits through its worker with an
attempt proof (`docs/design/child-tasks.md` §3.2), and that path never
reaches this route.

The caller must be:

1. a member of the step's **stored** tenant, resolved the way every route
   resolves it (`deps.tenant_scope`). Another tenant's step is a 404; and
2. one of `assignees` (§7 Q2). A member who is not an assignee gets 403
   `not_an_assignee`, which names the assignees so they know whom to ask.
   A platform admin answers only as a member of the tenant, as with
   approvals.

The plugin and the MCP bridge call the route with the person's own ID token
(`apps/swarm-mcp/swarm_mcp/auth.py`). An answer from them is the person's,
recorded with `via: "mcp"`.

### 4.3 The answer reaches GCS: a narrow, create-only writer

**Object key**, built only from the stored task:

    tenants/<tenant_id>/tasks/<task_id>/attempts/input-<12 hex>/artifacts/<artifact>

* `input-<12 hex>` stands in for an attempt id. Worker attempt ids are
  minted by `new_id`, never with that prefix, so the namespaces cannot
  collide. `artifact_key` accepts it unchanged: it requires one non-empty
  attempt segment followed by `artifacts/<filename>`
  (`apps/agent-worker/agent_worker/inputs.py::artifact_key`). **No worker
  change is needed to stage it.**
* The nonce is fresh per write. A losing writer (§4.6) therefore leaves an
  object that **no manifest names**, so it is never staged, and it is
  deleted with the task's prefix by the audited history purge and by the
  retention rule.
* The object is created with `if_generation_match=0` (create, never
  overwrite) and `customTime` set to the write time, so the retention clock
  applies to it as to a worker's upload (`terraform/modules/storage/main.tf`,
  the `days_since_custom_time` rule).

**The grant** (lane HW-IAM, `terraform/infra/await_input.tf`, new) gives
swarm-api's service account `roles/storage.objectCreator` on the artifact
bucket, **bucket-scoped**, with this condition:

    resource.name.startsWith("projects/_/buckets/<artifact bucket>/objects/tenants/")
    && resource.name.extract("/attempts/input-{nonce}/artifacts/") != ""

`objectCreator` includes create and nothing else: no get, no list, no delete.
A create over an existing object needs delete, so it cannot overwrite either.
That is the same reasoning recorded for the live relay
(`terraform/infra/live.tf`, `live_relay_records`). It cannot reach the
other team's buckets in this shared project, because it is granted on one
bucket. `api_reader` stays as it is
(`terraform/modules/iam/bindings.tf`, `api_reader`).

**The code** that writes is a new module,
`apps/swarm-api/swarm_api/inputwriter.py`, with one method,
`create(key, data, content_type)`. `objects.py` stays read-only, so its
docstring's guarantee, "a method this class does not have is a method no
refactor can accidentally call", still holds for every reader.

### 4.4 The scheduler: park, never lease

Lane HW-SCHED, `apps/scheduler/scheduler/awaitinput.py` (new), with small
calls from `loop.py`.

1. **Dependencies satisfied.** `Scheduler._resolve_dependency` promotes the
   step to READY as it does today. When the step's profile is
   `control_plane`, it then parks it at once, in the same call: `READY ->
   PARKED(AWAITING_INPUT)`, guarded on the promotion it just wrote, with
   `next_eligible_at` set to the park instant. `HUMAN_REQUIRED` and
   `CHILDREN_INCOMPLETE` record their park instant in the same field. Both
   transitions are legal today. A guarded park needs `PARKED -> PARKED`,
   which `store.park` refuses through `assert_transition`, so the step
   passes through READY for one write. It holds nothing during it.
2. **Admission backstop.** `Scheduler._admit_one` checks
   `profile.control_plane` **before** any capacity evaluation or lease, and
   parks the task `AWAITING_INPUT`. A step submitted with no dependencies
   reaches READY this way, and so would anything an operator promotes by
   hand. This is the line that makes invariant 1 hold even if the first
   path is bypassed. It mirrors how a missing credential is parked before
   any lease (`apps/scheduler/scheduler/credentials.py`).
3. **No sweep promotes `AWAITING_INPUT`.** `_parked_window` callers ignore
   it. The scheduler ends it only through the failed-workflow rule:
   `Scheduler._stop_for_failed_workflow` cancels an `AWAITING_INPUT` step
   under `fail_workflow` exactly as it cancels a step that has not started.
4. **Release.** The answer's `task_finished` wake reaches
   `Scheduler.release_dependants`, which runs the same rule as the sweep.
   `revise` is promoted in that drain and admitted like any task.

The park writes `metadata.await_input.opened_at` and `expires_at`
(`opened_at + expires_after_seconds`), computed from the signed declaration
in the same guarded write. Readers then never re-derive the deadline.

### 4.5 How a person sees it

Every surface reads the same two facts: the task is
`PARKED(AWAITING_INPUT)`, and its `metadata.dispatch.await_input.assignees`.
None of them stores a copy.

* **Console, Work › Workflows (`/workflows`).** The step's pill reads
  "waiting for <first assignee> (+N)" (`StatePill.tsx`, `PARK_WORD`). Its
  row shows the deadline as "expires in 2 d 3 h". The step's card,
  `AwaitInputCard.tsx` (new), shows the prompt and links to each artifact
  in its `input_from` through the existing artifact viewer, with the
  tenant check and redaction that `/artifacts/content` applies. It then
  shows the form:
  * `verdict`: Approve (`MERGE`) or Request changes (`NOT_YET`), plus a
    findings list;
  * `answers`: the upstream agent's questions, through the existing
    `QuestionsCard.tsx` and `GET /v1/tasks/{id}/questions`;
  * `text`: a text box.

  `AWAITING_INPUT` joins `PARK_NEEDS_A_PERSON` (`apps/swarm-ui/src/types.ts`),
  so every screen that groups parks puts it with the waits that only a
  person ends.
* **The bell** (`apps/swarm-api/swarm_api/notifications.py`): a new source,
  `awaiting_input`, in the `task` "needs you" group. It derives one item per
  waiting step on which the reader is an assignee, titled "A workflow is
  waiting for your <form noun>" and linked to the step's card. **Reminders
  are derived too**, never sent or stored. The item id is
  `task:input:<task>:<k>`, where `k = floor((now - opened_at) /
  remind_after_seconds)`, so each reminder period derives a new, unread
  item. That is the same rule that makes "a new occurrence ... starts
  unread" (`docs/notifications.md`, "ids are stable").
* **The approvals inbox** (`approvals.py`): waiting steps are **projected**
  as kind `input`, with id `input:<task_id>`. The inbox's Answer button opens
  the step's card. The decision is never taken by the inbox, so the step
  has one writer.
* **`/sc check`** (`apps/swarm-mcp/swarm_mcp/check.py`): a new item kind,
  `awaiting_input`, listed **as information, not STUCK**: "review in
  wf_… waits for owner@saga.xyz since 14:02, expires in 2 d". It is STUCK
  only when `expires_at` has passed by more than two ticks, which means
  the expiry sweep is not running. `HUMAN_REQUIRED` stays STUCK, as today
  (`plugin/skills/sc/SKILL.md`, the `parked` row).
* **Plugin and MCP rows**: `_PARKED_BECAUSE["AWAITING_INPUT"]` reads
  "waiting for <assignee> to answer; resumes when they do". `_WAITS_FOR`
  reads "a person's answer". `swarm_workflow_status` returns
  `awaiting: {assignees, opened_at, expires_at, form, artifact}` on the
  step.

### 4.6 The answer, expiry and cancel: one winner

**`POST /v1/workflows/{workflow_id}/steps/{step_id}/input`**

Request:

```json
{"answer_id": "r1-owner", "payload": {"verdict": "NOT_YET", "findings": ["Section 3 omits the cost."]}}
```

* `answer_id`, `[a-z0-9-]{1,64}`, chosen by the client. It is the
  idempotency key: a retry after a lost response is answered with the
  original 200, not a 409.
* `payload` is a JSON object, at most 256 KiB serialised (§7 Q6), and
  checked against `form`:
  * `verdict`: `{"verdict": "MERGE" | "NOT_YET", "findings": [...]}`, read
    with the same parser a gate uses
    (`apps/swarm-api/swarm_api/verdictpublish.py::parse_verdict`) and held
    to the worker's bounds: at most 50 findings of at most 1,000 characters
    (`agent_worker/verdict.py`, `MAX_FINDINGS`, `MAX_FINDING_CHARS`). A
    downstream `when` therefore reads it exactly as it reads an agent's
    verdict;
  * `answers`: `{"answers": [{"question_index", "choice", "text"}]}`. It
    must answer every counted question of the staged `questions.json`,
    with `choice` one of that question's option labels (or null alongside
    `text`), and `text` of at most 2,000 characters. These are the
    TASK-ANSWERS rules (`docs/workflows.md`, "The owner answers on the
    task");
  * `text`: `{"text": "..."}`, at most 64 KiB of text.

  Every string in the payload is passed through swarm-api's redaction
  before it is written, as owner answers are, so a token pasted into a
  review never reaches a workspace.

What the route does, in order:

1. Read the step through the tenant-scoped store. If the workflow or step is
   unknown, or belongs to another tenant: **404**.
2. Check that the caller is a person (403 `person_required`) and an
   assignee (403 `not_an_assignee`).
3. If the step is not `PARKED(AWAITING_INPUT)`, return **409** with the step's
   state: `not_open` if its dependencies are still running, `answered` with
   who answered and when, `expired`, or `cancelled`. A replay of the
   same `answer_id` with the same payload digest returns the original 200.
4. Verify the step's spec signature, with the same check the
   control-plane publish uses
   (`apps/swarm-api/swarm_api/verdictpublish.py::spec_refusal`), before
   reading the `form`, `artifact` or `assignees` that the decision rests on.
   An unsigned or rewritten spec is refused with 409 `spec_refused`.
5. Validate and redact the payload (422 `invalid_payload`, naming the
   position and key and never quoting the payload; 413 `payload_too_large`).
6. Create the object (§4.3). If GCS refuses, return **503**
   `store_unavailable`. Nothing has been written to Firestore, so a retry is
   safe.
7. **One transaction.** Re-read the step. Commit only if it is still
   `PARKED`, `park_reason == AWAITING_INPUT`, has no
   `metadata.await_input.answer`, and has no `cancel_requested`. If any of
   these fails, return the 409 from step 3, and leave the object as an
   unreferenced orphan. Otherwise write:

   ```
   state: SUCCEEDED, park_reason: None, next_eligible_at: None,
   completed_at, updated_at,
   result_summary: {
     artifacts: [{name, bytes, uri, sha256}], artifact_bytes,
     published_by: "control_plane",
     input: {source: "person", answered_by, answered_at, via, wait_seconds}
   },
   metadata.await_input.answer: {answer_id, answered_by, answered_at, via, sha256, bytes}
   ```

   and append the `succeeded` event in the same transaction.
8. Ring `task_finished` for the step (`waker.ring`). It is the wake that
   releases the dependants (§4.4, step 4).

Response, **200**:

```json
{"workflow_id": "wf_…", "step_id": "review", "task_id": "task_…",
 "state": "SUCCEEDED", "artifact": {"name": "review.json", "bytes": 87, "sha256": "…"},
 "answered_by": "owner@saga.xyz", "answered_at": "2026-10-17T14:05:09Z",
 "replayed": false}
```

**`GET /v1/workflows/{workflow_id}/steps/{step_id}/input`** is what the card,
the bridge and the inbox read:

```json
{"status": "waiting" | "not_open" | "answered" | "expired" | "cancelled",
 "prompt": "...", "form": "verdict", "artifact": "review.json",
 "assignees": ["owner@saga.xyz"], "may_answer": true,
 "opened_at": "…", "expires_at": "…", "on_expire": "fail",
 "shows": [{"step_id": "draft", "task_id": "task_…", "name": "draft.md"}],
 "questions": null,
 "answer": null}
```

`questions` is filled in only for `form: "answers"`, read through
`agent_questions`. `answer` is the stored record once answered: who, when,
how, and the digest. The payload itself is read as the artifact.

**`GET /v1/inputs?status=waiting`** lists the caller's tenant's waiting
steps on which the caller is an assignee, newest first, paged. It feeds the
plugin's `swarm_inputs` tool and `/sc check`. It is one query on
`park_reason == AWAITING_INPUT` within the tenant, filtered by assignee.

**Expiry: the schedule tick.** `POST /v1/admin/schedules/tick` already runs
every minute as the tick's own identity
(`apps/swarm-api/swarm_api/routes/schedule_tick.py::schedule_tick`). After
`run_tick`, it calls `awaitinput.expire_due(ctx, limit=200)`. That function
reads waiting steps whose `metadata.await_input.expires_at <= now`. For each
one, after verifying the spec signature:

* `on_expire: "fail"`: one transaction with the step-7 guard,
  `PARKED -> DEAD_LETTERED`, `last_error: "await_input_expired: nobody
  answered within <n> seconds"`. Under `fail_workflow`, the scheduler's
  failed-workflow rule then cancels the rest, as for any dead-lettered
  step.
* `on_expire: "default"`: write the signed `default` as the artifact (§4.3),
  then run the step-7 transaction with `input.source: "default"` and
  `answered_by: null`.

Both ring `task_finished`. The tick runs swarm-api, not the scheduler,
because swarm-api is the bucket writer. That keeps every write that ends an
`await-input` step in one service.

**Cancel.** `POST /v1/workflows/{id}/cancel` already cancels every step
through `request_cancel_with_target` (`apps/swarm-api/swarm_api/store.py`,
`cancel_workflow`). That function ends a task that holds no capacity
directly, in a transaction on its current state. It needs no change: once
an answer has committed, the step is SUCCEEDED and the cancel has nothing
left to do.

**The race, written out.** A person, the tick and a cancel can all arrive in
the same second. Each one ends in a transaction that commits only on
`PARKED + AWAITING_INPUT + no answer`. Firestore serialises them, so exactly
one commits. The person's object may already exist when they lose, and it
stays an orphan that no manifest names. A dependant can only be promoted
by a SUCCEEDED step, and it then stages the object that the **winning**
transaction recorded.

### 4.7 Firestore shape

`tasks/{task_id}` for an `await-input` step. Fields not listed are the
ordinary task fields.

```
runner_profile: "await-input"
resource_class: "standard"          # the profile's; never reserved
input: {prompt}
state / park_reason: PARKED / AWAITING_INPUT  while waiting
next_eligible_at: <park instant>     # as HUMAN_REQUIRED and CHILDREN_INCOMPLETE use it
metadata:
  dispatch:                          # SIGNED (specsign.SIGNED_METADATA_KEYS)
    await_input: {form, artifact, assignees[], expires_after_seconds,
                  on_expire, default|null, remind_after_seconds|null}
  input_from: {upstream task id: filename}   # what the person is shown
  await_input:                       # RESERVED, platform-written, unsigned
    opened_at, expires_at
    answer: null | {answer_id, answered_by, answered_at, via, sha256, bytes}
result_summary (once ended): see §4.6 step 7
```

There is no new collection and no new index. `parked_tasks` already queries
on `state` and `park_reason`
(`apps/scheduler/scheduler/store.py::SchedulerStore.parked_tasks`). The
tick's expiry query adds an order on `metadata.await_input.expires_at`. That
needs one composite index, in `terraform/modules/firestore/indexes.tf`. §6
gives that file to HW-IAM, the Terraform lane, so each file still has one
lane.

### 4.8 Files, by service

| service | files | change |
|---|---|---|
| contract (owner applies) | `apps/common/swarm_common/states.py`, `profiles.py` | §5 |
| swarm-api, submission | `schemas.py` (`WorkflowStepCreate.await_input`), `validation.py` (the rules of §4.1, the reserved key `await_input`), `service.py` (expansion into `metadata.dispatch`) | |
| swarm-api, answer | `awaitinput.py` (new: read, validate, answer, `expire_due`), `inputwriter.py` (new), `routes/await_input.py` (new), `main.py` (include the router), `auth.py` (the routes are person-only), `routes/schedule_tick.py` (call `expire_due`) | |
| swarm-api, surfaces | `notifications.py` (the `awaiting_input` source), `approvals.py` (the `input` projection) | |
| scheduler | `awaitinput.py` (new), `loop.py` (§4.4 steps 1-3), `store.py` (the guarded re-park) | |
| worker | **none** | `artifact_key` already accepts the key |
| UI | `AwaitInputCard.tsx` (new), `awaitInput.ts` (new: API calls and types), `Workflows.tsx`, `RunSteps.tsx`, `Approvals.tsx`, `SubmitWorkflow.tsx` (compose the step) | |
| MCP and plugin | `swarm_mcp/awaitinput.py` (new), `server.py` (`swarm_workflow_input`, `swarm_inputs`, status rows), `workflows.py`, `check.py`, `plugin/commands/sc.md`, `plugin/skills/sc/SKILL.md`, `plugin/README.md` | |
| Terraform | `terraform/infra/await_input.tf` (new), `terraform/modules/firestore/indexes.tf` | §4.3, §4.7 |
| docs | `docs/workflows.md` (a section "Waiting for a person: `await-input`"), `docs/notifications.md`, this file | |

### 4.9 GitHub as an answer channel (later phase)

#1436 names console, plugin and GitHub. GitHub is the one channel with no
identity path today. A comment arrives as a GitHub login, and the platform
maps a login to a member only through that person's own
`forge_connections` document (`apps/swarm-api/swarm_api/forgeapp.py`,
`CONNECTIONS`). `swarm-github-hooks` handles no `issue_comment` event
today. A GitHub answer would be: a comment
`/swarm answer <workflow>/<step>` followed by a fenced JSON payload, on the
issue or pull request the workflow names. The hook resolves the commenter's
login to exactly one connected member, then calls the same answer function
as that member with `via: "github"`. An unconnected login, or one
connected to several members, gets one reply comment that says why, and
the answer is not taken. §7 Q3 decides whether this is in the first build.

---

## 5. Frozen contract: change requests

These are requests, not changes. Nothing here edits `apps/common/swarm_common/`.
Once accepted, the operator files them in
[contract-change-requests.md](../contract-change-requests.md) under the next
free numbers, in the shape below.

### Request A. `states.py`: `ParkReason.AWAITING_INPUT`, a workflow step waiting for a person's answer (HW-CONTRACT)

**Why.** An `await-input` step must be durably parked, at no cost, and ended
only by a person's answer, its declared expiry, or a cancel. No existing
reason fits. `HUMAN_REQUIRED` already has a promoter (`human-ready`, to
READY), a 24-hour expiry, a bell item and plugin wording, and all four are
wrong for this step (§3.4). Request 68 refused to reuse `MANUAL_PAUSE` for
the same reason.

**The change.**

```python
class ParkReason(str, Enum):
    ...
    #: A workflow step of a `control_plane` profile (`await-input`) waiting
    #: for a person's answer (docs/design/human-steps.md). Never promoted to
    #: READY: swarm-api ends it PARKED -> SUCCEEDED on the answer or on its
    #: declared default, PARKED -> DEAD_LETTERED on expiry, PARKED ->
    #: CANCELLED on a cancel. Holds no capacity (invariant 1).
    AWAITING_INPUT = "AWAITING_INPUT"
```

`_ALLOWED` is unchanged.

**Restatements in the same pull request**, because the parity script and the
UI's exhaustive tables hold every restatement to the enum:
`apps/swarm-ui/src/types.ts` (the union, `PARK_REASONS`,
`PARK_NEEDS_A_PERSON`, `REASON_COPY`),
`apps/swarm-ui/src/components/StatePill.tsx` (`PARK_WORD`),
`apps/swarm-mcp/swarm_mcp/progress.py` (`_PARKED_BECAUSE`) and
`apps/swarm-mcp/swarm_mcp/compact.py` (`_WAITS_FOR`).

**Rollback.** Delete the member and its restatements, but only when no task is
parked under it, because such a document would decode to an unknown reason.

**What it would break.** Nothing that exists: it is a new member, and
readers with exhaustive tables gain their row in the same change.

**Invariants.** **1**: a `PARKED(AWAITING_INPUT)` task holds no lease and
creates no demand, because `PARKED` is in `PENDING_STATES`. **4**: no worker
waits. 2, 3 and 5-10 are unchanged.

### Request B. `profiles.py`: `RunnerProfile.control_plane`, a catalogue profile the platform never leases (HW-CONTRACT)

**Why.** A step type is chosen by profile name (invariant 10), and every
reader (the console, the bridge, the outcome ledger) already labels a step
by its profile. But `RunnerProfile.__post_init__` allows an empty
`runner_argv` only on a `worker_action` profile, and a worker action is
performed by the worker, in a container, under a lease. The new property
says the opposite: **nothing ever runs this, anywhere**. The scheduler's
admission backstop (§4.4) reads it, so a step of this profile can never be
leased.

**The change.**

```python
    #: True when no container ever runs this profile: the control plane
    #: ends its tasks itself (docs/design/human-steps.md, `await-input`).
    #: The scheduler never leases one; admission parks it before any
    #: capacity check. Such a profile has no runner, no worker action, no
    #: provider, no secrets, no slots, no live browser and no declared
    #: inputs besides `prompt`, and has no Job or pod (Terraform's
    #: `profiles_without_a_job`).
    control_plane: bool = False
```

In `__post_init__`, the empty-`runner_argv` refusal admits `control_plane`.
A `control_plane` profile is refused if it sets `runner_argv`,
`worker_action`, `provider`, `secrets`, `secret_slots`, `inputs`, or
`live_browser`.

**Rollback.** Delete the field and its checks, once no profile sets it
(request C rolled back first).

**What it would break.** Nothing: the default is False, and every existing
profile is unchanged. The Terraform catalogue mirror (`runner_profiles` in
`terraform/infra/locals.tf`, held field by field by
`tests/terraform/catalogue.tftest.hcl`) gains the field in the same pull
request.

**Invariants.** **1, 2, 3**: the field exists to prevent a lease. **10**:
the caller still names a profile, and the field is the catalogue's own,
never a caller's.

### Request C. `profiles.py`: the `await-input` catalogue entry, shipped unavailable (HW-CONTRACT)

**The change.**

```python
    "await-input": RunnerProfile(
        name="await-input",
        image="agent-runtime-base",   # never pulled: control_plane
        resource_class="standard",    # never reserved: control_plane
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=(),
        control_plane=True,
        provider=None,
        secrets=(),
        timeout_seconds=300,          # unused; the wait is await_input's
        inputs={},
        available=False,
        disabled_reason="await-input steps are being built (docs/design/human-steps.md)",
    ),
```

`terraform/infra/locals.tf` adds it to `profiles_without_a_job` in the same
pull request, so no Job is ever created for it.

**Why unavailable.** Lanes HW-API and HW-SCHED read the field and the reason,
so the contract has to land first. Until both have merged, a submission
naming `await-input` must be refused, and `available=False` is the refusal
that already exists, with its reason served to the caller.

**Rollback.** Delete the entry. No task can name it while it is
unavailable.

**What it would break.** Nothing. Profile pickers list it as disabled, with
the reason shown, as they list `post-verdict`.

**Invariants.** As request B.

### Request D. `profiles.py`: `await-input` becomes available (owner applies, after HW-ACCEPT)

One word: `available=True`, and `disabled_reason` removed. It is applied by
the owner once the acceptance group of §6 has passed on dev. **Rollback**:
the word back. **What it would break**: nothing that exists. From then on,
callers can submit the step.

### Not requested

There are no changes to `TaskState`, `_ALLOWED`, `EventType`, `Task`,
`WorkflowStep`, `Workflow` or `specsign`. §2.2 says why for each.

---

## 6. Build plan

Each lane owns the files named, and no file is in two lanes. Every lane
pushes its tests first to the pull request branch and its fix to
`<branch>-fix`, and runs `scripts/changed-guards.sh` before it finishes
(CLAUDE.md, "Lanes"). The review that CLAUDE.md requires for credentials,
tenant isolation, redaction and IAM applies to **HW-IAM**, **HW-API-ANSWER**
and **HW-GITHUB**.

| phase | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **HW-CONTRACT** (owner applies) | `apps/common/swarm_common/states.py`, `profiles.py`, `docs/contract-change-requests.md`, `terraform/infra/locals.tf`, `tests/terraform/catalogue.tftest.hcl`, `apps/swarm-ui/src/types.ts`, `apps/swarm-ui/src/components/StatePill.tsx`, `apps/swarm-mcp/swarm_mcp/progress.py`, `apps/swarm-mcp/swarm_mcp/compact.py`, `tests/unit/common/test_await_input_contract.py` (new) | `AWAITING_INPUT` is a `ParkReason` and not in any capacity set; a `control_plane` profile with `runner_argv`, `worker_action`, `provider`, `secrets`, `inputs` or `live_browser` raises; `await-input` is `control_plane`, unavailable, and in `profiles_without_a_job`; the parity script passes |
| 1 | **HW-IAM** (Track C) | `terraform/infra/await_input.tf` (new), `terraform/modules/firestore/indexes.tf` (the `expires_at` index), `tests/terraform/await_input.tftest.hcl` (new) | the condition admits `tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json`; refuses `attempts/att_x/artifacts/…`, `attempts/input-x/checkpoints/…`, `attempts/input-x/live/…`, a key outside `tenants/`; the role is `objectCreator` only, bucket-scoped; `api_reader` is unchanged |
| 1 | **HW-SCHED** | `apps/scheduler/scheduler/awaitinput.py` (new), `loop.py`, `store.py`, `tests/unit/control_plane/test_await_input_scheduler.py` (new) | draft -> await -> revise: when `draft` succeeds the await step is `PARKED(AWAITING_INPUT)` with **zero leases** in the store; a READY `await-input` task is parked by `_admit_one` and never leased whatever the pool headroom; `_expire_human_waits` does not touch it at any age; under `fail_workflow` a failed sibling cancels it; its `task_finished` promotes `revise` within the same drain |
| 1 | **HW-API-SUBMIT** | `apps/swarm-api/swarm_api/schemas.py`, `validation.py`, `service.py`, `tests/unit/control_plane/test_await_input_submission.py` (new) | each refusal of §4.1 is a 422 naming its field; `await_input` lands in `metadata.dispatch` and changes the spec digest; a caller-sent `metadata.await_input` is refused as reserved; an await step is never in `integrates`; `form: "answers"` without a `questions.json` source is refused |
| 2 | **HW-API-ANSWER** | `apps/swarm-api/swarm_api/awaitinput.py` (new), `inputwriter.py` (new), `routes/await_input.py` (new), `main.py`, `auth.py`, `routes/schedule_tick.py`, `tests/unit/control_plane/test_await_input_api.py` (new) | a service-account caller is 403 and nothing is written; another tenant's step is 404; a non-assignee is 403; an answer before the park is 409 `not_open`; a second answer is 409, while a replay of the same `answer_id` is the original 200; a payload with a runtime-built fake token (`"sk-" + "x" * 40`) is stored redacted; a GCS failure writes nothing to Firestore; answer, expiry and cancel racing in one test commit exactly once; the recorded URI passes the worker's `artifact_key` unchanged (imported, not restated); `expire_due` honours `fail` and `default`; an unsigned spec is refused |
| 2 | **HW-NOTIFY** | `apps/swarm-api/swarm_api/notifications.py`, `approvals.py`, `tests/unit/control_plane/test_await_input_notifications.py` (new) | an assignee gets the item and a non-assignee does not; the item id advances once per `remind_after_seconds`; the inbox projects `input:<task>`, has no decide path, and the item is gone once answered |
| 3 | **HW-UI** | `apps/swarm-ui/src/AwaitInputCard.tsx` (new), `awaitInput.ts` (new), `Workflows.tsx`, `RunSteps.tsx`, `Approvals.tsx`, `SubmitWorkflow.tsx`, their vitest files | the card renders each form; Approve posts `MERGE`; the button is disabled with the reason for a non-assignee; an expired or answered step shows who, when and how and offers no form; the pill names the assignee |
| 3 | **HW-MCP** | `apps/swarm-mcp/swarm_mcp/awaitinput.py` (new), `server.py`, `workflows.py`, `check.py`, `plugin/commands/sc.md`, `plugin/skills/sc/SKILL.md`, `plugin/README.md`, `tests/unit/mcp/test_await_input_bridge.py` (new) | `swarm_workflow_input` refuses a payload over the bound before sending it; `swarm_inputs` lists only the caller's waits; `/sc check` lists a fresh wait as `awaiting_input` and not STUCK, and as STUCK two ticks past `expires_at` |
| 3 | **HW-DOCS** | `docs/workflows.md`, `docs/notifications.md`, this file ("What was built") | `test_docs_describe_what_was_built` (the 256 KiB bound and the form names read from code) |
| 4 | **HW-ACCEPT** | `scripts/acceptance/groups/workflow.sh` | on dev, with `mock` for draft and revise: zero leases while parked; `revise` RUNNING within one drain of the answer; `revise`'s workspace holds the posted bytes (sha256 equal); a second workflow with `expires_after_seconds: 300` ends as declared |
| 5 | **HW-ENABLE** (owner applies) | request D | none: it is one word, applied once HW-ACCEPT is green |
| later | **HW-GITHUB** (if §7 Q3 says so) | `apps/swarm-github-hooks/…`, the App's event subscription | an unconnected login is answered with a reply and no answer; a connected login answers as its member with `via: "github"` |

Order: HW-CONTRACT, then the four phase-1 lanes in parallel. HW-API-ANSWER
starts after HW-API-SUBMIT merges, because it reads the stored block. HW-NOTIFY
runs beside HW-API-ANSWER. HW-UI and HW-MCP start after HW-API-ANSWER,
because they call its routes. HW-ACCEPT starts after all of those are
deployed. Then HW-ENABLE.

---

## 7. Owner questions

Each one is also in this lane's `questions.json`. The recommendation comes
first.

**Q1. Which park reason does a waiting step use?**
(a) **A new `AWAITING_INPUT`** (recommended). Every reader that forgets it
fails a test, and `human-ready`, the 24-hour expiry and the browser bell
never touch it.
(b) Reuse `HUMAN_REQUIRED`, with no contract change. Then four readers have
to branch on the profile (§3.4), and one that forgets will promote or kill
the step.

**Q2. Who may answer?**
(a) **The step's `assignees`, defaulting to the workflow's submitter, each a
member of the tenant at the moment they answer** (recommended).
(b) Any member of the tenant.
(c) The submitter only.

**Q3. Which answer channels are in the first build?**
(a) **Console and plugin/MCP now, GitHub comments in a later lane**
(recommended). GitHub needs a login-to-member mapping and a new webhook
event (§4.9).
(b) All three at once.
(c) Console only.

**Q4. How long may a step wait?**
(a) **Up to 7 days, no default (`expires_after_seconds` is required)**
(recommended). That keeps every wait inside dev's 14-day artifact retention
with a margin (§2.1, invariant 8).
(b) Up to 24 hours, like `HUMAN_REQUIRED`.
(c) Up to 30 days. Dev's `artifact_retention_days` would have to rise
above 30, at the matching storage cost.

**Q5. What may happen at expiry?**
(a) **`fail` (DEAD_LETTERED) or `default` (a declared answer, so a `when`
gate takes the author's branch)** (recommended).
(b) `fail` only.

**Q6. What may an answer carry?**
(a) **A JSON object up to 256 KiB in one of three forms (verdict, answers,
text), redacted, and no files** (recommended). 256 KiB matches
`max_input_bytes`.
(b) The same, plus one text file up to 1 MiB, such as an edited draft.
(c) Files up to the 16 MiB staging cap.

**Q7. How are people reminded?**
(a) **A fresh, unread bell item every `remind_after_seconds`, derived and
never stored** (recommended).
(b) The same, plus a GitHub mention on the workflow's issue or pull request.
(c) No reminders. The first item stays until it is read.

**Q8. Can an answer be changed?**
(a) **No. The first answer ends the step, and a correction is a new round**
(recommended). Dependants may already be running on it.
(b) It can be amended until the first dependant is leased.

**Q9. How are several review rounds expressed?**
(a) **Unrolled in the DAG (review1 -> revise1 -> review2 -> revise2)**
(recommended). This needs no change to the DAG model. With a `verdict`
form, a `when` gate keeps a later round's agent from running after an
Approve. The gate works wherever it works today: the integrator under
`integrate`, and under `collect`. A gated step still takes a short lease
for its publish (`docs/workflows.md`, "`when` gates the AGENT, not the
step").
(b) A loop construct (`repeat_until`). That is a frozen change to
`WorkflowStep`, and a cycle the DAG validator refuses today.

**Q10. May swarm-api write to the artifact bucket?**
(a) **Yes: `objectCreator` only, bucket-scoped, conditioned on
`attempts/input-*/artifacts/`, from a separate write-only module**
(recommended).
(b) No. A worker action writes the answer (design B): one container start
per answer (23-128 s), and the payload passes through Firestore.

**Q11. May an upstream agent's `questions.json` be the form?**
(a) **Yes, when the await step stages it explicitly with `form: "answers"`.
The answers then become an artifact that the next step reads** (recommended).
Questions stay "data, never a channel" everywhere else.
(b) No. Forms are only verdict and text.
