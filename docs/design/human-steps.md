# Human steps: a workflow waits for a tenant member, at no cost, for up to a day, and resumes on their answer

**Status: design, revised by lane HW-DESIGN-B (2026-10-17, part of #1436) for
the owner's answers to the first draft's eleven questions. Nothing here is
built.** The first draft (lane HUMAN-WAIT-DESIGN, PR 1462) asked them in §7.
The owner answered on 2026-10-17, in PR 1462's comments. Four answers depart
from the draft's recommendations: Q2 (any tenant member may answer), Q4 (24
hours, not 7 days), Q6 (answers may carry files) and, by consequence, the
reminder audience. This revision rewrites every section that those answers
change, not only §7. §7 now records each decision and keeps only the one
question the answers leave open (Q12, how a dependant stages an answer's
files).

The owner reads this document and approves or redirects it before any build
lane starts. The frozen-contract half is written as requests in §5, not as
changes. The operator files them in
[contract-change-requests.md](../contract-change-requests.md) once they are
accepted (§5 says why this lane does not file request A itself).

Every file and symbol cited below was read on 2026-10-17 at `a3d428d`. Lines
move, so search for the symbol named beside each one.

What the design settles, in one line each:

* **A person's answer is a workflow step.** The caller names the catalogue
  profile `await-input` for the step, and adds an `await_input` block
  saying what to ask, what shape the answer takes, and when it expires. A
  caller never sends an image, a command or a resource spec (invariant 10).
* **Waiting is `PARKED(AWAITING_INPUT)`**, a new park reason (Q1). The step
  never holds a lease, a slot or a container. It gets no attempt, so nothing
  runs to be fenced and nothing has to be checkpointed (invariants 1-5, 8).
  The 24-hour `HUMAN_REQUIRED` sweep, `human-ready` and the browser bell
  never touch it, and a parity test fails any reader that forgets it.
* **Any member of the step's tenant may answer** (Q2), checked as a member
  at the moment they answer. Agents and service accounts are always
  refused, including an agent driving a person's own plugin session, with
  **one exception** (amendment of 2026-10-18, §4.2.1): in a service tenant,
  the tenant's own principal answers its own steps, and may label the
  person it answers for with `on_behalf_of`. Release acceptance answers that
  way, as swarm-verify in its own service tenant (amendment of 2026-10-19,
  §4.2.2).
* **A wait lasts at most 24 hours** (Q4), the same bound as
  `HUMAN_REQUIRED`. `expires_after_seconds` is required.
* **An answer is a JSON form and, optionally, files** (Q6). The form
  (verdict, answers or text) is redacted before it is written. Files, at
  most 16 MiB with the form, are scanned and refused on a credential, never
  rewritten. Everything is written to GCS through one create-only path
  (Q10), never into Firestore.
* **Resume takes one drain.** The answer rings `task_finished`, and the
  scheduler releases the dependants in that same drain, as it does for a
  MERGE verdict published without a worker
  (`apps/swarm-api/swarm_api/verdictpublish.py::on_task_finished`).
* **Expiry is declared** (Q5). `on_expire: "fail"` dead-letters the step.
  `on_expire: "default"` ends it with a default answer declared and signed
  at submission, so a `when` gate downstream takes the branch the author
  chose.
* **The first answer is final** (Q8). A second answer is 409
  `already_answered`. A correction is a new round, unrolled in the DAG
  (Q9).
* **People see it where they already look.** It appears as a pill on the
  workflow step, an inbox entry, a `/sc check` line and a plugin progress
  sentence, each saying it waits for a member of the tenant. A bell item,
  derived on read and never stored or sent, reminds the people the step
  names every `remind_after_seconds` (Q7).

---

## 1. The problem, as measured

### 1.1 What #1436 reports

The owner's issue (#1436, 2026-10-16) wants to run a **draft -> human review ->
revision** loop as one workflow: an agent writes a draft, a person reviews it
("minutes to days later", in the issue; at most 24 hours per round, by the
owner's answer to Q4), and the next agent revises it using the review.
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
  staged input is bounded only by the attempt's total,
  `max_artifact_bytes`, **512 MiB** (`apps/agent-worker/agent_worker/config.py`,
  checked in `inputs.py::stage_inputs`), so a draft that stages fine
  cannot be re-sent inline once it passes 256 KiB. Below that size it still
  travels through Firestore as the agent's `input`, which is what
  `WorkflowStep.input_from`'s own comment forbids: "Artifacts pass by GCS
  reference, never inline through Firestore"
  (`apps/common/swarm_common/models.py::WorkflowStep`).
* **A review that is a file cannot be sent at all.** A reviewer who edits
  the draft, or attaches a screenshot of what is wrong, has no way to hand
  either to the next round except pasting text into a prompt.
* **The review lives outside the platform.** The caller gathers it in their
  own UI, so the platform keeps no record of who reviewed what, or when.

The first draft of this document called 16 MiB "the staging cap in
`agent_worker/inputs.py`". It is not one: the only 16 MiB there is a
docstring (`inputs.py::_sha256`, "inputs reach 16 MiB"). The 16 MiB bound on
an answer's files is the owner's number for this feature (Q6), and it sits
far inside the real per-attempt staging bound of 512 MiB. §4.3 says what the
16 MiB protects instead: swarm-api's memory.

### 1.2 What exists today, and why none of it is this

The platform already has four ways to wait for a person. Each one is close to
this feature, and none of them is it:

| mechanism | what it waits on | why it is not a step that waits |
|---|---|---|
| Live-browser hand-off (`Task.human_wait`, contract requests 68-70) | a person signing in inside a running pod | The agent **holds its lease** for the hold, 10 minutes (`human_hold_seconds`), then parks `HUMAN_REQUIRED`. Only `human-ready` promotes it, back to **READY**, which restarts the same agent. It is an interruption of an attempt, not a step with an output. |
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
     "input": {"prompt": "Read draft.md. Approve it, or list what must change. Attach an edited draft if that is quicker."},
     "await_input": {"form": "verdict", "artifact": "review.json",
                     "expires_after_seconds": 86400, "on_expire": "fail",
                     "remind_after_seconds": 14400}},

    {"step_id": "revise", "runner_profile": "claude-code",
     "depends_on": ["review"],
     "input_from": {"review": "review.json"},
     "input": {"prompt": "Revise the draft to answer every finding in review.json. Files the reviewer attached are in review.files/."}}
  ]
}'
```

Then:

1. `draft` runs and ends.
2. `review` parks. **No lease is held** while it waits, for minutes or up to
   a day.
3. Every member of the tenant sees "review in wf_… waits for a member of
   <tenant>" on the workflow, in the inbox, in `/sc check` and in the
   plugin row. The submitter, whom `remind` names by default, also gets a
   bell item, and a fresh one every four hours until someone answers. A
   member reads `draft.md` in the console and posts a verdict, with or
   without an edited `draft.md` attached, from the console or the plugin.
4. `revise` is promoted in the drain that the answer's wake triggers. Its
   workspace contains `review.json`, exactly as posted after redaction, and
   `review.files/draft.md`, byte for byte as uploaded.
5. If nobody answers within 24 hours, `review` ends as declared:
   `DEAD_LETTERED`, and the workflow fails under `fail_workflow`.

`revise` needs `draft.md` only if the reviewer attached none, and it cannot
stage `draft.md` from `draft` as well as `review.json` from `review` and
still be told which one to prefer. The prompt says it; the platform does not
merge them. A workflow that always wants the original as well adds
`"draft": "draft.md"` to `revise`'s `input_from`: the two parents stage
distinct filenames (`draft.md` and `review.json`, with the attachment under
`review.files/`), which is the rule `docs/workflows.md` already states
("each parent must stage a distinct relative filename").

Those checks are the issue's own acceptance criteria, plus the file. §6's
HW-ACCEPT lane turns them into a release acceptance group.

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
sit pending, so pending pods cannot become the backlog. Receiving an answer's
files costs swarm-api memory for the length of one request, never a lease or
a pool slot (§4.3).

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
member's answer, the expiry sweep, a workflow cancel, and the
failed-workflow sweep. Each is a Firestore transaction that re-reads the
step and commits only if it is still `PARKED` with `park_reason ==
AWAITING_INPUT` and has no recorded answer. Exactly one can commit, and every
other gets a 409 or a stale write (§4.6). Two members answering in the same
second are two writers of the first kind, and the same guard picks one: the
loser gets 409 `already_answered` (Q8). Fencing on **resume** is the
dependant's own: `revise` is an ordinary agent whose attempt carries
generation 1, and a stale worker of it exits without running the agent, as
every worker does. What it stages is immutable, because a SUCCEEDED agent's
`result_summary` is never rewritten, and every object of the answer, the form
and each file, is created with a no-overwrite precondition by an identity
that cannot delete (§4.3).

**Invariant 8. Mandatory periodic checkpointing.** It is unaffected and
still required. The step has no attempt and no workspace, so it has nothing
to checkpoint. The draft's artifacts are its outputs, not a checkpoint, and
the revise step checkpoints every `checkpoint_interval_seconds` as it does
today; the answer's files are inputs it stages, which a resumed attempt does
not re-fetch once its checkpoint holds them (`inputs.py::stage_inputs`, pass
2). **The retention constraint the first draft derived is now slack.** The
artifact bucket deletes objects `artifact_retention_days` after their
`customTime`: **14 days in dev**, 180 in prod
(`terraform/environments/dev/dev.tfvars`, `terraform/modules/storage/main.tf`).
The first draft sized its 7-day maximum against that with a margin. At the
owner's 24 hours (Q4), one wait is a fourteenth of dev's retention, so no
single round can outlive the draft it is about. What remains true is a
property of long workflows, not of this step: a step that stages an artifact
written more than 14 days earlier fails in dev, whatever made the workflow
that long. A many-round review keeps each `revise` staging from the
round before it, not from the first draft (§4.1, "Rounds").

**Invariant 9. Per-tenant isolation.** Every object key of an answer is built
from the **stored** agent's `tenant_id` and id, never from the request:
`tenants/<tenant>/tasks/<task>/attempts/input-<nonce>/artifacts/<name>`, and
for a file `…/artifacts/<artifact stem>.files/<file>`. Every segment passes
`objects.safe_segment`. The new write grant is conditioned to that shape
(§4.3). A person in another tenant gets the same 404 as for a step that does
not exist. Who may answer is decided against the step's tenant, freshly, at
the moment of the answer (§4.2). The dependant's worker stages each object
through `artifact_key`, which refuses any URI outside the upstream's own
tenant prefix (`apps/agent-worker/agent_worker/inputs.py::artifact_key`),
and the attachments are held to the same check (§4.3, "Staging").

**Invariant 10. Callers pick a runner_profile by name.** The step type is
the catalogue name `await-input`. Its `await_input` block carries only
names, bounded numbers and a declared answer shape: a form name from a
closed list, a filename, a file count, member addresses to remind,
durations, and `fail` or `default`. It holds no image, command, resource
class, backend or timeout that reaches any compute. swarm-api refuses
`resource_class`, `timeout_seconds`, `builds_on` and `when` on an
`await-input` step (§4.1). An answer's files are data a later agent reads,
never something the platform runs.

Invariants 6 (Spot) and 7 (requests == limits) are untouched: nothing here
creates a pod.

### 2.2 What it needs from `apps/common/swarm_common/`

Three things, all in §5:

1. **A park reason**, `ParkReason.AWAITING_INPUT` (`states.py`), decided by
   the owner (Q1). §3.4 says why `HUMAN_REQUIRED` cannot be reused without
   breaking the live browser's readers.
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
  `specsign.SIGNED_METADATA_KEYS`, so the declaration, and with it a
  `default` answer, is covered by the step's spec signature
  (`apps/common/swarm_common/specsign.py`). The wait's live state (when it
  opened, the answer record) sits under a reserved platform metadata key,
  `metadata.await_input`. TASK-ANSWERS set the precedent with
  `metadata.owner_answers`, "because the frozen `Task` has no field for it"
  (`apps/swarm-api/swarm_api/validation.py`, `RESERVED_METADATA_KEYS`).
* **`WorkflowStep.input_from`'s shape**, `dict[str, str]`, one filename per
  parent. An answer's files therefore cannot be staged as separate
  `input_from` entries. They ride on the answer artifact's manifest entry
  and the worker stages them with it (§4.3, "Staging"; Q12). Widening
  `input_from` to a list per parent would be a frozen change to every
  workflow reader, for a need this one entry meets.
* **`EventType`.** The park writes `parked` with
  `detail.reason = "AWAITING_INPUT"`. The answer writes `succeeded` with
  `detail.published_by = "control_plane"` and `detail.source = "person"` or
  `"default"`. Expiry writes `dead_lettered` with `detail.cause =
  "await_input_expired"`. Readers that key on the event type need no new
  member. A reader that wants to know it was a person reads the detail.
* **`Settings`.** The 24-hour bound is the scheduler's own
  `HUMAN_PARK_MAX_SECONDS` (`apps/scheduler/scheduler/loop.py`), restated in
  swarm-api as `AWAIT_INPUT_MAX_SECONDS` and held equal by a test (§4.1),
  not a new contract setting.

---

## 3. Designs that could work

### 3.1 Design A: a control-plane step that swarm-api ends (recommended, and chosen: Q10)

The step is an agent under the catalogue profile `await-input`. When its
dependencies have succeeded, the scheduler parks it `AWAITING_INPUT` and
never leases it. swarm-api's new route takes the member's answer, checks it
against the declared form, writes the form and any files to GCS as the
step's artifacts, and ends the step `PARKED -> SUCCEEDED` with a
`result_summary.artifacts` manifest. The manifest has the shape the worker
writes, so `input_from` stages the form unchanged, and the worker stages the
files listed on its entry (§4.3). A per-minute sweep on the existing schedule
tick ends expired waits.

* **Code.** Two new swarm-api modules and a route. A guarded park and an
  admission refusal in the scheduler. One new submission field. A console
  card with an upload, a bell source and an inbox projection. An MCP tool
  and plugin wording. A small worker change to stage attachments. Roughly
  the size of #748's control-plane publish, which this copies, plus the
  multipart path.
* **Migration.** None. These are new fields on new agents. Every existing
  document decodes as before.
* **Latency.** The answer to a promoted dependant is up to eleven GCS writes
  (the form and at most ten files), one transaction and one Pub/Sub wake, a
  few seconds. That is "within one scheduler drain", the issue's own bar.
  Expiry precision is the tick: one minute.
* **Money.** Zero while waiting. Each answer costs at most 16 MiB of GCS
  objects, retained as every artifact is, and three Firestore writes: the
  agent, its event, and the claim.
* **What it breaks.** swarm-api becomes a **writer** to the artifact bucket.
  Today its grant is `roles/storage.objectViewer` and its object module has
  "no upload, no delete and no copy" by design
  (`apps/swarm-api/swarm_api/objects.py`, the module docstring). §4.3
  narrows the new grant to create-only, on `attempts/input-*/artifacts/`
  keys, behind an IAM condition of the same form as the live relay's
  (`terraform/infra/live.tf`, `live_relay_object_condition`), in a separate
  write-only module. The owner accepted that posture change (Q10). The
  control plane also starts accepting uploads of up to 16 MiB, which §4.3
  bounds per instance.

### 3.2 Design B: the answer is delivered by a worker action

The step parks the same way. The answer is stored in Firestore on the agent,
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
  forbids. With the owner's Q6 it is impossible outright: 16 MiB of files
  does not fit a task document's 1 MiB. B would need a staging bucket
  swarm-api writes anyway, which is A's grant with a container added.

### 3.3 Design C: the agent pauses itself (the live-browser shape)

There is no step type. The drafting agent writes a request file. Its
worker checkpoints, parks the agent `HUMAN_REQUIRED` and exits. The person
answers, and the same agent is promoted, restored from its checkpoint, and
handed the answer, so the agent revises in its own context.

* **Code.** The most of the three: a spool request, a park path and a
  resume path in the worker, plus a restore that stages the answer. It
  touches the lifecycle, the riskiest file in the worker.
* **Latency.** A container start plus a checkpoint restore per round.
* **Money.** A lease for every resume, and an attempt from `max_attempts`
  for every round. Three review rounds would exhaust an agent's default
  `max_attempts: 3`.
* **What it breaks.** The DAG no longer says where the person is, so the
  console cannot show "waiting on X" from the workflow, and `when` cannot
  gate on a person's verdict. It also needs a new resume path that the
  live browser already found hard: LB-RETURN, three race rules
  (`apps/swarm-api/swarm_api/handoff.py`, the module docstring). The
  invariants hold, but by the hardest route.

### 3.4 Within A: a new park reason, or `HUMAN_REQUIRED` (decided: a new reason, Q1)

`HUMAN_REQUIRED` already means "only a person ends this". Reusing it would
avoid a contract change, as request 83 reused `CREDENTIAL_MISSING`. But its
readers already act on it, and each would do the wrong thing to an
`await-input` step:

* `Scheduler._expire_human_waits` dead-letters it after
  `human_park_max_seconds`, 24 hours, ignoring the step's own
  `expires_after_seconds` and `on_expire`
  (`apps/scheduler/scheduler/loop.py::Scheduler._expire_human_waits`). The
  bound is now the same number (Q4), but the sweep would still ignore
  `on_expire: "default"` and a shorter declared expiry.
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
`PARK_REASONS`). What those tables do not catch is a reader that names
`HUMAN_REQUIRED` in a branch and silently skips the new member. So request A
ships with a **readers parity test**
(`tests/unit/common/test_awaiting_input_readers.py`, lane HW-CONTRACT): it
lists every source file under `apps/` and `scripts/` that names
`HUMAN_REQUIRED`, and fails unless each one also names `AWAITING_INPUT` or is
on the test's own list of files that must ignore it, each with its reason.
Three behavioural assertions sit beside it: `_expire_human_waits` leaves a
25-hour-old `AWAITING_INPUT` step alone, `human-ready` on one is 409, and the
`handoffs` bell source yields nothing for one. On 2026-10-17 the files that
name `HUMAN_REQUIRED` outside tests are `scheduler/loop.py`,
`agent_worker/handoff.py`, `agent_worker/lifecycle.py`,
`swarm_api/{validation,codec,live,handoff,notifications}.py`,
`swarm_api/routes/tasks.py`, `swarm_mcp/{check,progress,server,compact}.py`,
`swarm-ui/src/{types.ts,LiveBrowser.tsx,Handoff.tsx,AgentDetail.tsx}` and
`components/StatePill.tsx`. A new file that names it and not the new member
fails CI until someone decides which list it is on.

### 3.5 Rejected without a full write-up

* **`input_from` across workflows.** This is smaller, but every round is
  still a submission by the caller, and nothing shows who the work is
  waiting on. It solves the re-staging and not the pause.
* **An approval record (`approvals/`) as the step's state.** An approval
  decides whether work is created, and holds no payload a step could stage.
  This design **projects** the waiting step into the inbox (§4.5) without
  moving its state there, as issue runs are projected (`approvals.py`,
  "ISSUE RUNS JOIN THE INBOX WITHOUT MOVING").
* **Files through a signed upload URL.** swarm-api could hand the browser a
  V4 signed URL and let it upload straight to GCS, keeping 16 MiB off
  swarm-api. Signing needs `iam.serviceAccounts.signBlob` on swarm-api's
  own account, a second new grant, and the upload would land before
  swarm-api has seen the bytes, so the credential scan of §4.3 would run
  after the object already exists in a workspace's reach. A 16 MiB request
  fits Cloud Run's 32 MiB HTTP/1 request limit with room, so the upload
  goes through the route.

### 3.6 Recommendation

**Design A, with a new park reason.** The owner chose it (Q1, Q10). It is the
only design in which waiting, answering and resuming all cost nothing beyond
the answer itself. It keeps artifacts, and now files, in GCS. It reuses a
transition the owner has already accepted (request 52) and a wake that is
already proven (#748). It makes the person part of the DAG, so the console
and `when` can see them. Its costs are a narrow, create-only write grant for
swarm-api and a bounded upload path. B avoids the grant by spending a
container start, and cannot carry files at all. C avoids a step type by
spending an attempt per round.

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
    "max_files": 10,
    "expires_after_seconds": 86400,
    "on_expire": "default",
    "default": {"verdict": "MERGE", "findings": []},
    "remind_after_seconds": 14400,
    "remind": ["owner@example.com"]
  }
}
```

| field | rule | why |
|---|---|---|
| `form` | `"verdict"`, `"answers"` or `"text"`, required | The answer's shape is declared, so it is checked when it arrives and not when a dependant fails to read it. These are the only three shapes a console can render without a schema language. |
| `artifact` | a filename `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`. Default: `verdict.json`, `answers.json` or `input.json`, by form | Dependants stage it by this name. Its stem names the attachments' directory (`review.json` -> `review.files/`, §4.3). `expected_outputs` inversion does not apply: no agent writes it. |
| `max_files` | 0 to 10. Default 10 | Q6 lets an answer carry files. An author whose next prompt reads no attachments sets 0, and the console then offers no upload. Ten keeps the manifest entry small (§4.7) and the upload form legible. |
| `expires_after_seconds` | 300 to `AWAIT_INPUT_MAX_SECONDS` = **86,400** (24 h), required | The owner's Q4: the same bound as `HUMAN_REQUIRED` (`apps/scheduler/scheduler/loop.py`, `HUMAN_PARK_MAX_SECONDS = 86_400`). swarm-api cannot import the scheduler, so it restates the number in `swarm_api/awaitinput.py` and HW-API-SUBMIT's test reads both and fails if they differ. Required, because a wait with no end is a workflow nobody is alerted about, the failure the cycle check exists to prevent (`docs/workflows.md`, "Dependencies and state"). |
| `on_expire` | `"fail"` or `"default"`, required | The owner's Q5. |
| `default` | required if and only if `on_expire` is `"default"`. Checked against `form` at submission, at most 16 KiB, no files | The default is part of the signed spec, so the branch a `when` gate takes on expiry is the author's, unalterable after submission. It is written to GCS only when it is used (§4.6). It carries no files: an author who wants a default file stages it from an upstream step instead. |
| `remind_after_seconds` | `null`, or 3,600 up to `expires_after_seconds` | §4.5. With the 24-hour bound the useful values are hours: 14,400 gives four reminders before a full-day expiry, 3,600 gives an hourly one for a one-afternoon wait. |
| `remind` | 0-10 addresses in the platform's hosted domain. Default: `[submitted_by]` | **Not an authorisation.** The owner's Q2 lets any member answer, so this list only chooses whose bell is rung (§4.5). An address is not checked against the tenant at submission, and naming someone outside it rings nobody, because the bell is read per tenant (§4.5). |

There is no `assignees` field. The first draft used it to decide who may
answer. Q2 replaced that with tenant membership (§4.2), and a submission that
still sends `assignees` is refused with 422 naming the field and pointing at
`remind`, so a caller who meant it as a restriction learns it is not one
rather than believing it holds.

What swarm-api **refuses** on an `await-input` step, each with a 422 that
names the field: `resource_class`, `timeout_seconds`, `builds_on`, `when`,
`allow_empty_diff`, `pr_role` and `merges` (none of them means anything
without a container). Also refused: any `input` key other than `prompt`;
`await_input` on any other profile; `form: "answers"` without exactly one
`input_from` entry whose filename is `questions.json` (Q11, below); a
`default` whose shape does not match `form`; and the step as a standalone
agent. The profile is valid only inside a workflow.

**`form: "answers"` and `questions.json` (Q11).** An upstream agent's
`questions.json` becomes the form only when this step stages it explicitly:
`form: "answers"` and an `input_from` entry naming `questions.json`.
Nowhere else does a `questions.json` turn into a wait: an agent that writes
one in a step that is not followed by such an `await-input` step is still
"data, never a channel" (`docs/workflows.md`, "An agent asks the owner").
The questions are read at answer time from the staged upstream artifact, so
the agent cannot change them after the person has seen them: the upstream is
SUCCEEDED and its manifest is never rewritten.

**Rounds (Q9).** Several rounds are unrolled in the DAG: `draft -> review1
-> revise1 -> review2 -> revise2`. Each `review` is its own step with its own
24-hour bound, so a three-round loop can take up to three days of waiting
without any one wait passing the bound. With a `verdict` form, a `when` gate
on `revise2` keeps that round's agent from running after an Approve in
`review2`: the gate works wherever it works today, the integrator under
`integrate` and under `collect`, and a gated step still takes a short lease
for its publish (`docs/workflows.md`, "`when` gates the AGENT, not the
step"). `review2` stages `revise1`'s output, not `draft`'s, which is what
keeps a long loop inside the retention window (§2.1, invariant 8). A
correction to an answer already given is the same thing: a new round (Q8).

Under `strategy: "integrate"`, an `await-input` step is never in an
integrator's `integrates`. It pushes no branch. That is the same rule that
leaves a gated integrator's review out (`docs/workflows.md`, "The review's
own branch is not merged").

**Stored** at expansion, in the same write that creates the agent:
`metadata.dispatch.await_input` holds the block as normalised (defaults
filled in, `remind` addresses lower-cased). It is signed with the rest of
`metadata.dispatch`. `input_from` is expanded to task ids exactly as for any
step. On this step it is not staged: it is **what the person is shown**.

### 4.2 Who may answer

The owner's Q2: **any member of the step's tenant, checked as a member at the
moment they answer. Agents and service accounts are always refused.** That
replaces the first draft's per-assignee check, and this section is the threat
model that has to hold without it.

**What the check is, in order.**

1. **A person, not a service.** The principal must be a verified human ID
   token. Any `*.gserviceaccount.com` principal is refused with 403
   `person_required` (`apps/swarm-api/swarm_api/gitidentity.py::is_service_account`
   is the predicate; `handoff.py` already uses it). That covers a tenant's
   worker GSA, the scheduler, the schedule tick, the rollup sweeper, a
   service account listed as a tenant member (`AuthContext.member_scope`,
   contract request 30) and any CI identity. A child agent submits through its
   worker with an attempt proof (`docs/design/child-tasks.md` §3.2), and that
   path never reaches this route. So a SwarmCloud agent, whose MCP bridge
   speaks as its worker, cannot answer any step, including its own
   workflow's.
2. **The step's tenant, selected and re-checked.** The step is read through
   the tenant-scoped store under the tenant the request selected
   (`deps.tenant_scope`, `X-Swarm-Tenant`). A step in any other tenant is a
   404, the same as one that does not exist. Then membership in that tenant
   is **re-resolved for this request, bypassing the cache**:
   * **A group tenant** (`tenant_principal` is a registered group). The
     route calls Cloud Identity's `checkTransitiveMembership` for **that one
     registered group**, with `x-goog-user-project`, as every membership
     check here does: `searchTransitiveGroups` 403s in this project, so the
     platform never enumerates a caller's groups (`apps/swarm-api/swarm_api/groups.py`,
     the module docstring; CONTRACT.md, "Cloud Identity group resolution").
     The ordinary request path caches that answer per (caller, group) for
     `group_cache_ttl_seconds`, 120 s (`groups.py::CloudIdentityGroups.is_member`,
     `settings.py`). For an answer the cached answer is not good enough: a
     person removed from the group a minute ago would still pass. HW-API-ANSWER
     adds a `fresh=True` argument to `is_member` that skips the read and
     refreshes the entry, and the answer route uses it. A lookup failure is
     503 `membership_unavailable`, and nothing is written: the route fails
     closed, never open.
   * **A personal tenant** (`u-<slug>`). Its only member is the person whose
     verified email the tenant document names
     (`auth.py::Authenticator._with_own_workspace`, `PersonalTenants.ready`).
     The check is that equality, so only the owner of a personal workspace
     answers its steps.
   A caller who fails either is 403 `not_a_member`, and nothing is written.
3. **No admin bypass.** A platform admin answers only as a member of the
   tenant, as with approvals. `is_admin` is about the caller, not the tenant
   (`auth.py::_select_tenant`), and the route never reads it.

   > **Amended 2026-10-19 (ROLES-DOCS, part of #1518): step 3 is
   > superseded for group workspaces** by
   > [workspace-roles.md](workspace-roles.md) §4.2.3, owner answer Q5. A
   > platform admin is `admin` in every registered group workspace, member
   > or not, so they answer there as its admin; every write they make in a
   > group workspace they are not in is audited before it runs. Step 2's
   > fresh check is now `Authenticator.may_act_in`, which answers
   > `"platform_admin"` for them only in a group workspace and only while
   > `PLATFORM_ADMIN_REACH` is on. A personal workspace is unchanged: only
   > its person answers, and a platform admin cannot even select another
   > person's. Roles add one check after step 2: the answerer must hold
   > `member` or above, read fresh, or the answer is 403
   > `role_member_required` (behind that code's switch, off at ship). The
   > route runs all of it in
   > `apps/swarm-api/swarm_api/awaitinput.py::member_refusal`: a person,
   > then `may_act_in` fresh (`not_a_member` when it answers `""`), then
   > the role. Step 1 and the gate rules elsewhere in this section are
   > unchanged and still apply to a platform admin.

**What replaces the per-assignee check in the threat model.** The first draft
relied on `assignees` for one property: that the person whose judgement the
author wanted is the person who answered. Q2 drops that property on purpose,
and the design keeps what is left:

* **The boundary is the tenant, the same boundary as everything else the
  tenant's members can already do.** Any member can already cancel the
  workflow, read every artifact the step shows, and submit a workflow that
  does whatever the next step does. Answering a review gives a member no
  power they lack: the worst a hostile member can do is approve a draft,
  and that draft's next step runs in the same tenant, under the same
  GSA, as a workflow they could have submitted themselves.
* **Who answered is recorded and shown, not guessed.** `answered_by` (the
  verified email), `answered_at` and `via` are on the task document, the
  `succeeded` event and the result (§4.6), and the console, the inbox and
  `swarm_workflow_status` show them. An author who cares who reviewed reads
  it there, and an unexpected reviewer is visible.
* **Freshness is the check, not the submission.** Membership is read when the
  answer arrives, never inherited from when the workflow was submitted, so a
  wait of up to 24 hours cannot outlive a removal from the group by more
  than the one Cloud Identity round trip of §4.2 step 2.
* **An agent cannot be "a member".** Step 1 is what keeps a workflow's own
  agent from approving its own draft. On a laptop, an agent drives the
  person's own plugin session, with the person's own ID token, which the
  route cannot tell from the person. The bridge closes that gap the way it
  already does for `swarm_account_remove`: **the answer is typed by the
  person into the host's MCP elicitation prompt, never taken from the
  tool call's arguments** (`apps/swarm-mcp/swarm_mcp/server.py`, `_elicit`,
  "the model that made the tool call neither sees the request nor writes its
  answer"). A host that cannot elicit (`_HOST_CAN_ELICIT` unset) gets a
  refusal naming the console link, never a fallback that would let the
  agent supply the answer. The answer is recorded `via: "mcp"`.
* **What is no longer protected.** A step can no longer say "only Alice may
  approve this". If an author needs that, it is a product change the owner
  declined in Q2, not a gap in this design.

#### 4.2.1 Amendment, 2026-10-18: a service tenant's own principal (HW-SERVICE-ANSWER)

**Owner decision, 2026-10-18 (part of #1436).** Q2 said agents and service
accounts are always refused. It now has **one exception**: in a **service
tenant**, the tenant's own principal may answer the steps of workflows in
that tenant.

**Why.** A service tenant is a `kind: "user"` tenant whose principal is a
service account: `u-or-9a0792` for oracle-api, `u-sw-c90291` for
swarm-verify (docs/multi-tenancy.md, "Service tenants"). Its only member is
that service account. Under step 1, nobody could answer its steps, so a
client application that runs its own review loop through a workflow (the
case #1568 admitted oracle-api for) could never finish one. The application
collects the person's answer in its own UI and relays it. The rule is
generic, so it holds for **every** service tenant: swarm-verify's
`u-sw-c90291`, the acceptance identity's, gains it too, for its own
tenant's steps only.

**Who, exactly** (`apps/swarm-api/swarm_api/awaitinput.py::service_principal`).
All of these must hold, and each is read from the verified token or freshly
from Firestore, never from something the caller chose:

* the caller is a service account and **not** one of the platform's jobs
  (the schedule tick, the rollup sweeper) and **not** a listed,
  continuation-scoped account (`member_scope`, `tenant_member`, contract
  request 30). A listed account is refused even when its address is a
  tenant's principal;
* the caller is **not a worker identity**: no address under the frozen
  worker prefix (`swarm-agent-worker-<tenant>`) or the configured
  `TENANT_SERVICE_ACCOUNT_PREFIX`, and not the step tenant's derived worker. A workflow's agent speaks as its worker through the MCP bridge,
  and the worker GSA is a different account from the tenant's principal.
  A tenant identity can rewrite its own `tenants/<id>` document (contract
  request 87), so the worker check reads no stored field;
* the request acts in the step's tenant, reached by no platform-admin
  reach, and that tenant's id is the one the frozen `tenant_id_for_user`
  derives from the **caller's own email**. A rewritten tenant document
  naming another principal therefore cannot make a foreign tenant look like
  the caller's own;
* the tenant document, read for this request, is `kind: "user"` and its
  principal is exactly the caller's address (a blank principal is not a
  match).

Then the ordinary rest of §4.2 applies unchanged: the step is read through
the tenant-scoped store (another tenant's step is a 404), `may_act_in` is
asked freshly (a user tenant's principal is its only member), and the role
floor is the personal tenant's.

**Still refused, 403 `person_required`:** any other service account, in any
tenant; a service account in a group tenant (oracle-api is in none, and a
bot confirmed in eng is still refused there); a service tenant's principal
in another service tenant; every worker and agent identity; every listed
scoped identity; the platform's own jobs.

**`on_behalf_of`, a label.** The answer body takes one more optional field,
`on_behalf_of`: an email address of at most 254 characters of printable
ASCII, the whole value matched (no trailing newline, no look-alike letter,
no direction override), checked for an address **shape only**. **It is a label, not a verified identity.** Nothing
asks whether the address exists, or whether that person asked for the answer:
the platform knows only that the principal sent it. It is recorded as given
(lower-cased) beside `answered_by`, which is always the verified principal,
on the answer record, `result_summary.input` and the `succeeded` event (both
with `answered_as: "service"`, where a person's answer says `"person"`; the
`source` stays `"person"`, meaning "not the expiry default"), and every
response that reads the answer back,
each with `on_behalf_of_verified: false`. A person may not send it: 422
`invalid_payload`, `position: "on_behalf_of"`. A replay must carry the same
label to be a replay. Idempotency, 409 `already_answered`, the forms and
the file rules are unchanged.

**What this gives up.** The property step 1 protected -- "a workflow's own
agent cannot approve its own draft" -- now rests, in a service tenant, on
the service application: it holds the principal's credential, and whatever
it relays is the answer. That is the same trust the tenant already places in
it to submit the workflow at all. Its agents still cannot answer, because
they run as the worker, not the principal.

#### 4.2.2 Amendment, 2026-10-19: release acceptance answers in swarm-verify's own service tenant (HW-ACCEPT-ID)

**Owner decision, 2026-10-19 (part of #1436).** Release acceptance answers
its await-input step **as swarm-verify, in swarm-verify's own service tenant
`u-sw-c90291`**: the path oracle-api takes in `u-or-9a0792` (§4.2.1), so the
release check proves that path too. No new account, no new secret, and **no
exception to `person_required` in group tenants**.

**Why it was needed.** HW-ACCEPT (PR 1579, merged 2026-10-19 01:45Z) ran the
two await-input checks in the suite's tenant, the GROUP tenant `smoke`.
There swarm-verify is a service account answering for no person: 403
`person_required` on every dev run, as §4.2.1 says it must be. §4.2.1's
exception needed the request to act in the caller's own service tenant, and
swarm-verify could not select it: `Authenticator._with_own_workspace` offers
no service identity its own tenant, and `PersonalTenants.ready` wants a
`workspaces/` record that a Terraform-written service tenant never has (the
workspace gate never judges a service account, WD7).

**What changed** (`apps/swarm-api/swarm_api/auth.py`,
`Authenticator._with_own_service_tenant`). A service account may **name** its
own service tenant in `X-Swarm-Tenant`, and act in it, when all of these
hold, each from the verified token or a read, never from what the caller
sent:

* the named id is exactly `tenant_id_for_user(<the caller's verified
  email>)`;
* `tenants/<that id>`, read through `PersonalTenants`' cache, is `kind:
  "user"` and its principal is **exactly the caller's email**. The frozen
  slug drops the domain, so `ci-bot@one-project` and `ci-bot@another` derive
  one id, and the principal is what tells them apart;
* the caller is not a worker identity (the frozen prefix, the configured
  `TENANT_SERVICE_ACCOUNT_PREFIX`, or the tenant's own derived worker), not
  the schedule tick, not the rollup sweeper, and not a listed,
  continuation-scoped account. These are the conditions §4.2.1 applies,
  through one shared helper (`auth.own_service_tenant`), so the tenant a
  service account may select and the tenant it may answer in cannot drift
  apart.

No `workspaces/` record is asked for on this path. **A person's path is
unchanged**: their own tenant is still offered only when
`PersonalTenants.ready` finds the ready record. **The headerless default is
unchanged**: a service account in a group keeps that group as its default,
which the acceptance suite relies on for every other check. A failed read is
a 503 when the request names that tenant, never a fall-through to another.

**What acceptance does** (`scripts/acceptance/groups/workflow.sh`). It
derives its own tenant from the email `GET /v1/tenants/me` verified, with
`common.sh`'s `personal_tenant_id` (the frozen `tenant_id_for_user`,
imported; the swarm-verify image carries `identity.py` for it), and submits,
reads and answers the two await-input workflows with `X-Swarm-Tenant` set to
it. Their `mock` steps run on that tenant's own mock job. It asserts the
answer was taken with `answered_as: "service"`. Every other check stays in
`smoke`.

### 4.3 The answer reaches GCS: a narrow, create-only writer

**Object keys**, built only from the stored agent:

    tenants/<tenant_id>/tasks/<task_id>/attempts/input-<12 hex>/artifacts/<artifact>
    tenants/<tenant_id>/tasks/<task_id>/attempts/input-<12 hex>/artifacts/<artifact stem>.files/<file>

* `input-<12 hex>` stands in for an attempt id. Worker attempt ids are
  minted by `new_id`, never with that prefix, so the namespaces cannot
  collide. `artifact_key` accepts it unchanged: it requires one non-empty
  attempt segment followed by `artifacts/<filename>`, and a filename may
  have a directory segment (`apps/agent-worker/agent_worker/inputs.py::artifact_key`).
* One nonce per answer, fresh per attempt to answer. A losing writer (§4.6)
  therefore leaves objects that **no manifest names**, so they are never
  staged, and they are deleted with the agent's prefix by the audited history
  purge and by the retention rule.
* Every object is created with `if_generation_match=0` (create, never
  overwrite) and `customTime` set to the write time, so the retention clock
  applies to it as to a worker's upload (`terraform/modules/storage/main.tf`,
  the `days_since_custom_time` rule).

**The grant** (lane HW-IAM, `terraform/infra/await_input.tf`, new). Two
Terraform rows, both labelled as every resource here is
(`managed-by=swarm-terraform` on the resources that take labels):

1. `google_storage_bucket_iam_member.api_input_writer`: swarm-api's service
   account gets `roles/storage.objectCreator` on the artifact bucket,
   **bucket-scoped**, with the condition built in a local beside it,
   `await_input_object_condition`, in the form of
   `live_relay_object_condition` (`terraform/infra/live.tf`):

       resource.name.startsWith("projects/_/buckets/<artifact bucket>/objects/tenants/")
       && resource.name.extract("/attempts/input-{nonce}/artifacts/") != ""

   The test refuses `checkpoints/`, `live/` and worker attempt keys, so if
   IAM rejects a template that mixes a literal with `{nonce}` in one
   segment, the lane falls back to
   `resource.name.matches("^projects/_/buckets/<bucket>/objects/tenants/[^/]+/tasks/[^/]+/attempts/input-[0-9a-f]{12}/artifacts/.+$")`,
   and the same test cases decide it.
2. `google_firestore_index.await_input_expiry` (in
   `terraform/modules/firestore/indexes.tf`): the composite index the expiry
   sweep reads (§4.7).

Row 1 changes IAM, so the dev release that carries it **waits at
`dev-iam`** for the owner's approval (`docs/ci.md`, "A dev release that
changes IAM waits for the owner"); a held `dev-iam` plan cancels every dev
release behind it, so HW-IAM merges only once HW-API-ANSWER is ready to
follow. Row 2 does not touch IAM.

`objectCreator` includes create and nothing else: no get, no list, no delete.
A create over an existing object needs delete, so it cannot overwrite either.
That is the same reasoning recorded for the live relay
(`terraform/infra/live.tf`, `live_relay_records`). It cannot reach the
other team's buckets in this shared project, because it is granted on one
bucket. `api_reader` stays as it is
(`terraform/modules/iam/bindings.tf`, `api_reader`).

**The code** that writes is a new module,
`apps/swarm-api/swarm_api/inputwriter.py`, with one method,
`create(key, data, content_type)`. It refuses any key the condition would
refuse before it calls GCS, so a bug fails in a test and not as a 403 in
production. `objects.py` stays read-only, so its docstring's guarantee, "a
method this class does not have is a method no refactor can accidentally
call", still holds for every reader. The answer, its files and an expiry's
default all go through this one method. **Nothing of an answer is written
into Firestore except its record** (who, when, how, sizes and digests): a
task document holds 1 MiB, and an answer may be sixteen times that.

**Files: the limits, and why each is that number.**

| bound | value | why |
|---|---|---|
| whole answer, form plus every file | **16 MiB** | The owner's number (Q6). It is what swarm-api holds in memory for one answer (below), and well inside the worker's 512 MiB per-attempt staging total, so an answer never makes a dependant refuse its inputs. |
| the form | 256 KiB serialised | Unchanged from the first draft: `max_input_bytes`, the bound on an agent's own input. |
| one file | 16 MiB less the form | One large file is the common case (an edited document, a screenshot set as a PDF); a lower per-file bound would only push people to split one. |
| files per answer | `max_files`, at most **10** | Each file is an entry on the answer's manifest entry in Firestore (§4.7); ten keeps it a few KiB. |
| a file's name | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`, distinct, no directory | It becomes a path in the dependant's workspace, checked again there by `destination_for`. No slash means no tree to traverse. |

**Allowed content types.** A closed list, decided by **sniffing the bytes**,
never by the client's `Content-Type` or the extension alone:

* **text**: a file that decodes as UTF-8 and holds no NUL byte, with an
  extension from a list (`.md .txt .json .yaml .yml .csv .diff .patch .py
  .ts .tsx .js .go .rs .java .sh .html .css .sql .toml`). Stored as
  `text/plain; charset=utf-8` (or `application/json` for `.json`).
* **PDF**: starts `%PDF-`. Stored as `application/pdf`.
* **images**: PNG, JPEG or WebP by magic number. Stored as `image/png`,
  `image/jpeg` or `image/webp`.

Everything else is refused with 415 `file_type_refused`, naming the file.
Archives (zip, tar, gzip) are refused on purpose: the scan below cannot see
inside one, and an archive is how a whole credentials directory travels as
one innocent-looking file. Executables are refused because nothing
downstream should be handed a binary by a person's upload.

**Scanning and redaction.**

* **The form is redacted** before it is written, every string through
  `swarm_redaction.redact` (`apps/redaction/swarm_redaction/rules.py`, the
  rules the API masks with and the worker's publish scan matches), as owner
  answers are. A token pasted into a review becomes the mask, and the
  artifact holds the masked text. That is safe for the form because the form
  is the platform's own structure: masking a value inside a finding changes
  that finding and nothing else.
* **A text file is scanned with the same rules and refused on a match, never
  rewritten.** 422 `file_contains_credential`, naming the file, the line
  number and the rule name, never the match. Redaction cannot apply to an
  arbitrary file the way it applies to the form: a masked line inside an
  edited draft, a patch or a JSON document silently changes a file the
  person believes was passed through byte for byte, can break its syntax (a
  patch that no longer applies), and makes the sha256 the console shows
  disagree with the file the person has. Refusal tells the person exactly
  what to remove, which is what the worker's publish scan does with a diff.
* **A PDF or an image is not scanned.** Its text is compressed or is pixels,
  and a pattern scan over its bytes would pass a photographed key and fail on
  noise. It is accepted as is.
* **What the console warns, before the upload starts and again beside the
  Send button:** "Files go to the next agent byte for byte. Text files are
  checked for credentials and refused if one is found. PDFs and images cannot
  be checked, so never attach a screenshot or a document that shows a key,
  a token or a password." The MCP elicitation shows the same sentence.
* **Reading back.** The console shows an answered step's files through the
  existing artifact viewer, which redacts text at read time
  (`swarm_api/redaction.py`, "redaction at READ time, unconditionally"), and
  serves a PDF or image as a download, never inline, so a hostile file
  cannot render in the console's origin.

**The multipart answer endpoint and its size check.** The route of §4.6
accepts either `application/json` (a form, no files) or
`multipart/form-data` with one part named `answer` (`application/json`, the
same body as the JSON route) and up to `max_files` parts named `file`, each
with a filename. Before **any** write, in this order:

1. `Content-Length` is required on a multipart request (411 without it) and
   must be at most 16 MiB plus 64 KiB of multipart framing, or the request
   is refused 413 `answer_too_large` before the body is read.
2. The body is read through a counting reader that aborts at the same bound,
   because `Content-Length` is a claim (the same reasoning as `stage_inputs`'
   pass 4).
3. A per-instance semaphore admits **four** multipart answers at once; a
   fifth gets 429 `answer_busy` with `Retry-After`. swarm-api runs 1 GiB at
   concurrency 80 (`terraform/infra/main.tf`, `services["swarm-api"]`), and
   eighty 16 MiB bodies, each held while it is parsed and written, would
   pass that; four hold at most 128 MiB with the copy each part makes. The
   parser is `python-multipart`, which FastAPI's form parsing needs and
   swarm-api does not depend on today; HW-API-ANSWER adds it to
   `apps/swarm-api/pyproject.toml` and its lock.
4. The step, the caller and the membership are checked (§4.6 steps 1-4), so
   an outsider's 16 MiB is refused without a single GCS call.
5. The form is validated and redacted, every file's name, count, size and
   sniffed type is checked, and every text file is scanned. Any refusal
   here writes nothing.

Only then are the objects created: the files first, then the form, so that
an answer whose form exists always has its files. The manifest names them
only after the transaction commits.

**Staging: how the next step gets the files (Q12).** `input_from` is one
filename per parent (§2.2), so `{"review": "review.json"}` is all a
dependant can declare from the await step, and the files' names are chosen
by the person, after submission. The answer's manifest entry therefore lists
them:

```json
{"name": "review.json", "bytes": 87, "sha256": "…", "uri": "gs://…/artifacts/review.json",
 "attachments": [{"name": "review.files/draft.md", "bytes": 40211, "sha256": "…",
                  "content_type": "text/plain; charset=utf-8",
                  "uri": "gs://…/artifacts/review.files/draft.md"}]}
```

and the worker, when it stages an entry that has `attachments`, stages each
of them too (lane HW-WORKER, `apps/agent-worker/agent_worker/inputs.py`):

* every attachment's URI goes through `artifact_key` with its own name, so
  it must sit in the same tenant, the same upstream agent and an attempt of
  it, or staging fails with `InputUnavailable`, as a forged entry does today;
* every attachment's destination goes through `destination_for`, so
  `review.files/<file>` cannot traverse or land on a worker control file;
* every attachment has a whole `bytes`, refused when absent as an artifact's
  is, and the pass-3 total that bounds staging into the memory-backed
  workspace counts them. With at most 16 MiB per answer, the existing
  512 MiB total is the cap the next step stages them under, and **no new
  cap** is introduced;
* pass 4 re-checks the bytes that arrived, and the staged list (which the
  worker's prompt line names by absolute path) includes each file, so the
  agent is told where they are without the author's prompt having to guess.

Only entries written by this route carry `attachments`. The worker's own
upload never writes the key, so no existing manifest changes meaning. The
alternative that needs no worker change, a zip of the files, is Q12's option
(b): it keeps the form and the files in one staged file only by making the
form unreadable to a `when` gate, which parses the staged file as a verdict.

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
   parks the agent `AWAITING_INPUT`. A step submitted with no dependencies
   reaches READY this way, and so would anything an operator promotes by
   hand. This is the line that makes invariant 1 hold even if the first
   path is bypassed. It mirrors how a missing credential is parked before
   any lease (`apps/scheduler/scheduler/credentials.py`).
3. **No sweep promotes `AWAITING_INPUT`, and the human-wait sweep never ends
   it.** `_parked_window` callers ignore it. `_expire_human_waits` keeps its
   explicit `park_reason == HUMAN_REQUIRED` filter, which is the line request
   A's readers test holds (§3.4). The scheduler ends an `AWAITING_INPUT` step
   only through the failed-workflow rule: `Scheduler._stop_for_failed_workflow`
   cancels it under `fail_workflow` exactly as it cancels a step that has not
   started.
4. **Release.** The answer's `task_finished` wake reaches
   `Scheduler.release_dependants`, which runs the same rule as the sweep.
   `revise` is promoted in that drain and admitted like any agent.

The park writes `metadata.await_input.opened_at` and `expires_at`
(`opened_at + expires_after_seconds`, so at most 24 hours after the park),
computed from the signed declaration in the same guarded write. Readers then
never re-derive the deadline.

### 4.5 How a person sees it

Every surface reads the same facts: the agent is `PARKED(AWAITING_INPUT)`, its
tenant, its `expires_at`, and its `metadata.dispatch.await_input`. None of
them stores a copy.

* **Console, Work › Workflows (`/workflows`).** The step's pill reads
  "waiting for a member" (`StatePill.tsx`, `PARK_WORD`), and its tooltip
  names the tenant. Its row shows the deadline as "expires in 3 h 20 m".
  The step's card, `AwaitInputCard.tsx` (new), shows the prompt and links to
  each artifact in its `input_from` through the existing artifact viewer,
  with the tenant check and redaction that `/artifacts/content` applies. It
  then shows the form:
  * `verdict`: Approve (`MERGE`) or Request changes (`NOT_YET`), plus a
    findings list;
  * `answers`: the upstream agent's questions, through the existing
    `QuestionsCard.tsx` and `GET /v1/tasks/{id}/questions`;
  * `text`: a text box;

  and, when `max_files` is above 0, a file picker that enforces the limits of
  §4.3 in the browser (count, names, 16 MiB total) before it sends anything,
  with the warning of §4.3 above it. The server repeats every check; the
  browser's are for a fast refusal, not for safety. A member who is not
  signed into the step's tenant sees the tenant switch, not the form.

  `AWAITING_INPUT` joins `PARK_NEEDS_A_PERSON` (`apps/swarm-ui/src/types.ts`),
  so every screen that groups parks puts it with the waits that only a
  person ends.
* **The bell** (`apps/swarm-api/swarm_api/notifications.py`): a new source,
  `awaiting_input`, in the `task` "needs you" group. **Derived on read;
  nothing is stored or sent** (Q7). For a reader in the step's tenant whose
  address is on the step's `remind` list, it derives one item per waiting
  step, titled "A workflow is waiting for a <form noun>" and linked to the
  step's card. The item id is `task:input:<task>:<k>`, where `k =
  floor((now - opened_at) / remind_after_seconds)` (0 when
  `remind_after_seconds` is null), so each reminder period derives a new,
  unread item. That is the same rule that makes "a new occurrence ... starts
  unread" (`docs/notifications.md`, "ids are stable"). With the 24-hour
  bound, a step reminding every 4 hours yields at most six items, the last
  one four hours before expiry. The bell rings only `remind`, not every
  member, because a group tenant's every member being rung for every review
  is noise that teaches people to ignore the bell; everyone else sees the
  wait in the inbox and on the workflow. Once the step is answered, expired
  or cancelled, the source derives nothing for it.
* **The approvals inbox** (`approvals.py`): waiting steps are **projected**
  for every member of the tenant as kind `input`, with id `input:<task_id>`.
  The inbox's Answer button opens the step's card. The decision is never
  taken by the inbox, so the step has one writer.
* **`/sc check`** (`apps/swarm-mcp/swarm_mcp/check.py`): a new item kind,
  `awaiting_input`, listed **as information, not STUCK**: "review in
  wf_… waits for a member since 14:02, expires in 3 h". It is STUCK only when
  `expires_at` has passed by more than two ticks, which means the expiry
  sweep is not running. `HUMAN_REQUIRED` stays STUCK, as today
  (`plugin/skills/sc/SKILL.md`, the `parked` row).
* **Plugin and MCP rows**: `_PARKED_BECAUSE["AWAITING_INPUT"]` reads
  "waiting for a member of the tenant to answer; resumes when one does".
  `_WAITS_FOR` reads "a person's answer". `swarm_workflow_status` returns
  `awaiting: {tenant, opened_at, expires_at, form, artifact, max_files,
  remind}` on the step, and, once answered, `answered: {answered_by,
  answered_at, via, files}`.

### 4.6 The answer, expiry and cancel: one winner

**`POST /v1/workflows/{workflow_id}/steps/{step_id}/input`**

Request, as `application/json` or as the `answer` part of a
`multipart/form-data` body whose `file` parts carry the files (§4.3):

```json
{"answer_id": "r1-owner", "payload": {"verdict": "NOT_YET", "findings": ["Section 3 omits the cost."]}}
```

* `answer_id`, `[a-z0-9-]{1,64}`, chosen by the client. It is the
  idempotency key: a retry after a lost response, with the same payload
  digest and the same file digests, is answered with the original 200, not a
  409.
* `payload` is a JSON object, at most 256 KiB serialised, and checked
  against `form`:
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

  Every string in the payload is redacted before it is written (§4.3).

What the route does, in order:

1. Apply the size gate of §4.3 steps 1-3 (411, 413, 429).
2. Read the step through the tenant-scoped store. If the workflow or step is
   unknown, or belongs to another tenant: **404**.
3. Check that the caller is a person, or a service tenant's own principal
   in that tenant (§4.2.1) (403 `person_required`), and, freshly, a
   member of the step's tenant (403 `not_a_member`, 503
   `membership_unavailable`) (§4.2).
4. If the step is not `PARKED(AWAITING_INPUT)`, return **409** with the step's
   state: `not_open` if its dependencies are still running,
   `already_answered` with who answered and when (Q8), `expired`, or
   `cancelled`. A replay of the same `answer_id` with the same digests
   returns the original 200.
5. Verify the step's spec signature, with the same check the
   control-plane publish uses
   (`apps/swarm-api/swarm_api/verdictpublish.py::spec_refusal`), before
   reading the `form`, `artifact` or `max_files` that the decision rests on.
   An unsigned or rewritten spec is refused with 409 `spec_refused`.
6. Validate and redact the payload, and check and scan every file (§4.3):
   422 `invalid_payload` (naming the position and key, never quoting the
   payload), 422 `too_many_files`, 422 `invalid_file_name`, 415
   `file_type_refused`, 422 `file_contains_credential`, 413
   `answer_too_large`.
7. Create the objects (§4.3), files first. If GCS refuses, return **503**
   `store_unavailable`. Nothing has been written to Firestore, so a retry is
   safe; objects created before the failure are orphans no manifest names.
8. **One transaction.** Re-read the step. Commit only if it is still
   `PARKED`, `park_reason == AWAITING_INPUT`, has no
   `metadata.await_input.answer`, and has no `cancel_requested`. If any of
   these fails, return the 409 from step 4, and leave the objects as
   unreferenced orphans. Otherwise write:

   ```
   state: SUCCEEDED, park_reason: None, next_eligible_at: None,
   completed_at, updated_at,
   result_summary: {
     artifacts: [{name, bytes, uri, sha256, attachments: [{name, bytes, uri, sha256, content_type}]}],
     artifact_bytes,
     published_by: "control_plane",
     input: {source: "person", answered_by, on_behalf_of, answered_at, via, wait_seconds, files}
   },
   metadata.await_input.answer: {answer_id, answered_by, on_behalf_of, answered_at, via, sha256, bytes, files}
   ```

   `artifact_bytes` counts the form and every file. `files` is the count.
   The `succeeded` event is appended in the same transaction.
9. Ring `task_finished` for the step (`waker.ring`). It is the wake that
   releases the dependants (§4.4, step 4).

Response, **200**:

```json
{"workflow_id": "wf_…", "step_id": "review", "task_id": "task_…",
 "state": "SUCCEEDED",
 "artifact": {"name": "review.json", "bytes": 87, "sha256": "…"},
 "files": [{"name": "review.files/draft.md", "bytes": 40211, "sha256": "…"}],
 "answered_by": "owner@example.com", "answered_at": "2026-10-17T14:05:09Z",
 "replayed": false}
```

**`GET /v1/workflows/{workflow_id}/steps/{step_id}/input`** is what the card,
the bridge and the inbox read:

```json
{"status": "waiting" | "not_open" | "answered" | "expired" | "cancelled",
 "prompt": "...", "form": "verdict", "artifact": "review.json",
 "max_files": 10, "max_answer_bytes": 16777216,
 "tenant": "eng", "may_answer": true, "remind": ["owner@example.com"],
 "opened_at": "…", "expires_at": "…", "on_expire": "fail",
 "shows": [{"step_id": "draft", "task_id": "task_…", "name": "draft.md"}],
 "questions": null,
 "answer": null}
```

`may_answer` is the person-and-member check of §4.2, made from the cached
membership (this is a display, not the decision; the POST decides freshly).
`questions` is filled in only for `form: "answers"`, read through
`agent_questions`. `answer` is the stored record once answered: who, when,
how, the digests and the file names. The payload itself is read as the
artifact.

**`GET /v1/inputs?status=waiting`** lists the caller's tenant's waiting
steps, newest first, paged. It feeds the plugin's `swarm_inputs` tool and
`/sc check`. It is one query on `park_reason == AWAITING_INPUT` within the
tenant. It lists every waiting step of the tenant, because every member may
answer every one.

**Expiry: the schedule tick.** `POST /v1/admin/schedules/tick` already runs
every minute as the tick's own identity
(`apps/swarm-api/swarm_api/routes/schedule_tick.py::schedule_tick`). After
`run_tick`, it calls `awaitinput.expire_due(ctx, limit=200)`. That function
reads waiting steps whose `metadata.await_input.expires_at <= now`. For each
one, after verifying the spec signature:

* `on_expire: "fail"`: one transaction with the step-8 guard,
  `PARKED -> DEAD_LETTERED`, `last_error: "await_input_expired: nobody
  answered within <n> seconds"`. Under `fail_workflow`, the scheduler's
  failed-workflow rule then cancels the rest, as for any dead-lettered
  step.
* `on_expire: "default"`: write the signed `default` as the artifact (§4.3),
  with no attachments, then run the step-8 transaction with
  `input.source: "default"` and `answered_by: null`. A `when` gate
  downstream reads it exactly as it would read a member's verdict, so the
  author's declared branch is taken (Q5).

Both ring `task_finished`. The tick runs swarm-api, not the scheduler,
because swarm-api is the bucket writer. That keeps every write that ends an
`await-input` step in one service. The tick's identity is a service account,
which is why expiry is a separate function and never the answer route: the
route refuses every service account (§4.2), the tick included.

**Cancel.** `POST /v1/workflows/{id}/cancel` already cancels every step
through `request_cancel_with_target` (`apps/swarm-api/swarm_api/store.py`,
`cancel_workflow`). That function ends an agent that holds no capacity
directly, in a transaction on its current state. It needs no change: once
an answer has committed, the step is SUCCEEDED and the cancel has nothing
left to do.

**The race, written out.** Two members, the tick and a cancel can all arrive
in the same second. Each one ends in a transaction that commits only on
`PARKED + AWAITING_INPUT + no answer`. Firestore serialises them, so exactly
one commits. The losing member gets 409 `already_answered`, naming who won
(Q8). A loser's objects may already exist, and they stay orphans that no
manifest names. A dependant can only be promoted by a SUCCEEDED step, and it
then stages the objects that the **winning** transaction recorded.

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
    await_input: {form, artifact, max_files, expires_after_seconds,
                  on_expire, default|null, remind_after_seconds|null, remind[]}
  input_from: {upstream task id: filename}   # what the person is shown
  await_input:                       # RESERVED, platform-written, unsigned
    opened_at, expires_at
    answer: null | {answer_id, answered_by, on_behalf_of, answered_at, via, sha256, bytes, files}
result_summary (once ended): see §4.6 step 8
```

**Size.** The document holds the answer's record, never its bytes. At ten
attachments with 128-character names and full URIs, the manifest entry is
under 5 KiB, against the 1 MiB document limit, and the 16 KiB `default` is
the largest thing the declaration adds.

There is no new collection. `parked_tasks` already queries on `state` and
`park_reason` (`apps/scheduler/scheduler/store.py::SchedulerStore.parked_tasks`).
The tick's expiry query adds an order on `metadata.await_input.expires_at`.
That needs one composite index, `google_firestore_index.await_input_expiry`
in `terraform/modules/firestore/indexes.tf`. §6 gives that file to HW-IAM, the
Terraform lane, so each file still has one lane.

### 4.8 Files, by service

| service | files | change |
|---|---|---|
| contract (owner applies) | `apps/common/swarm_common/states.py`, `profiles.py` | §5 |
| swarm-api, submission | `schemas.py` (`WorkflowStepCreate.await_input`), `validation.py` (the rules of §4.1, the reserved key `await_input`, the refused `assignees`), `service.py` (expansion into `metadata.dispatch`) | |
| swarm-api, answer | `awaitinput.py` (new: read, validate, scan, answer, `expire_due`, `AWAIT_INPUT_MAX_SECONDS`), `inputwriter.py` (new), `routes/await_input.py` (new: the JSON and multipart route, the size gate, the semaphore), `main.py` (include the router), `auth.py` (the routes are person-only), `groups.py` (`is_member(fresh=True)`), `routes/schedule_tick.py` (call `expire_due`), `pyproject.toml` and `uv.lock` (`python-multipart`) | |
| swarm-api, surfaces | `notifications.py` (the `awaiting_input` source), `approvals.py` (the `input` projection) | |
| scheduler | `awaitinput.py` (new), `loop.py` (§4.4 steps 1-3), `store.py` (the guarded re-park) | |
| worker | `inputs.py` (stage `attachments`, §4.3) | |
| UI | `AwaitInputCard.tsx` (new, with the upload), `awaitInput.ts` (new: API calls, types and the browser-side limits), `Workflows.tsx`, `RunSteps.tsx`, `Approvals.tsx`, `SubmitWorkflow.tsx` (compose the step) | |
| MCP and plugin | `swarm_mcp/awaitinput.py` (new: the elicitation schemas per form and the file list), `server.py` (`swarm_workflow_input`, `swarm_inputs`, status rows), `workflows.py`, `check.py`, `plugin/commands/sc.md`, `plugin/skills/sc/SKILL.md`, `plugin/README.md` | |
| Terraform | `terraform/infra/await_input.tf` (new), `terraform/modules/firestore/indexes.tf` | §4.3, §4.7 |
| docs | `docs/workflows.md` (a section "Waiting for a person: `await-input`"), `docs/notifications.md`, this file | |

**The plugin's file path.** An MCP elicitation carries flat fields, not
files. `swarm_workflow_input` takes `files` as local paths in its arguments;
the bridge reads each one, shows the person the exact list (name, bytes,
sha256) and the §4.3 warning in the elicitation, and sends the files only if
the person accepts. The verdict, findings, answers or text are typed into
the elicitation. So the agent may propose which files to attach but cannot
attach one the person did not see, and cannot write the answer.

### 4.9 GitHub as an answer channel (later phase: lane HW-GITHUB)

The owner's Q3 puts GitHub comments in a later lane, HW-GITHUB, after the
console and plugin/MCP have shipped. GitHub is the one channel with no
identity path today. A comment arrives as a GitHub login, and the platform
maps a login to a member only through that person's own `forge_connections`
document (`apps/swarm-api/swarm_api/forgeapp.py`, `CONNECTIONS`).
`swarm-github-hooks` handles no `issue_comment` event today. A GitHub answer
would be: a comment `/swarm answer <workflow>/<step>` followed by a fenced
JSON payload, on the issue or pull request the workflow names. The hook
resolves the commenter's login to exactly one connected member, then calls
the same answer function as that member with `via: "github"`, so the
person-and-fresh-membership check of §4.2 applies unchanged. An unconnected
login, a login connected to several members, a bot account, or a member of
another tenant gets one reply comment that says why, and the answer is not
taken. A comment carries no files: a GitHub answer is a form only, and its
reply says to use the console to attach one.

---

## 5. Frozen contract: change requests

These are requests, not changes. Nothing here edits `apps/common/swarm_common/`.
Once accepted, the operator files them in
[contract-change-requests.md](../contract-change-requests.md) under the next
free numbers (85 onward on 2026-10-17; the last filed is 84), in the shape of
that file's entries: a status line, then *What is true today*, *The change*,
*Rollback*, *What it would break*, *Invariants*.

The owner's answer to Q1 asks for request A to be written in
`contract-change-requests.md`. This lane's territory is this one document,
and the lane brief says the requests go here and the operator adds them once
approved, so request A is written below, ready to paste as entry 85, and
lane HW-CONTRACT (§6) files it in that file as its first commit. The PR body
names the conflict.

### Request A. `states.py`: `ParkReason.AWAITING_INPUT`, a workflow step waiting for a person's answer (HW-CONTRACT)

**Status:** ACCEPTED in principle by the owner 2026-10-17 (Q1, on PR 1462);
to be filed as entry 85 by lane HW-CONTRACT.

#### What is true today

An `await-input` step must be durably parked, at no cost, and ended only by a
member's answer, its declared expiry, or a cancel. No existing reason fits.
`HUMAN_REQUIRED` already has a promoter (`human-ready`, to READY), a 24-hour
dead-letter sweep (`Scheduler._expire_human_waits`), a bell item
(`notifications.py::handoffs`) and plugin wording, and all four are wrong for
this step (§3.4). Request 68 refused to reuse `MANUAL_PAUSE` for the same
reason.

#### The change

```python
class ParkReason(str, Enum):
    ...
    #: A workflow step of a `control_plane` profile (`await-input`) waiting
    #: for a tenant member's answer (docs/design/human-steps.md). Never
    #: promoted to READY, and never read by the HUMAN_REQUIRED sweep,
    #: `human-ready` or the browser bell: swarm-api ends it PARKED ->
    #: SUCCEEDED on the answer or on its declared default, PARKED ->
    #: DEAD_LETTERED on expiry (at most 24 hours), PARKED -> CANCELLED on a
    #: cancel. Holds no capacity (invariant 1).
    AWAITING_INPUT = "AWAITING_INPUT"
```

`_ALLOWED` is unchanged.

**Restatements in the same pull request**, because the parity script and the
UI's exhaustive tables hold every restatement to the enum:
`apps/swarm-ui/src/types.ts` (the union, `PARK_REASONS`,
`PARK_NEEDS_A_PERSON`, `REASON_COPY`),
`apps/swarm-ui/src/components/StatePill.tsx` (`export const PARK_WORD`),
`apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE` and
`apps/swarm-mcp/swarm_mcp/compact.py::_WAITS_FOR`.

**The readers parity test in the same pull request**,
`tests/unit/common/test_awaiting_input_readers.py` (§3.4): every non-test
file under `apps/` and `scripts/` that names `HUMAN_REQUIRED` must name
`AWAITING_INPUT` or be on the test's ignore list with a reason; and the
human-wait sweep, `human-ready` and the `handoffs` bell source each leave an
`AWAITING_INPUT` agent untouched.

#### Rollback

Delete the member, its restatements and the readers test, but only when no
agent is parked under it, because such a document would decode to an unknown
reason.

#### What it would break

Nothing that exists: it is a new member, and readers with exhaustive tables
gain their row in the same change. A future file that branches on
`HUMAN_REQUIRED` without deciding about the new member fails the readers test
until it does, which is the intent.

#### Invariants

**1**: a `PARKED(AWAITING_INPUT)` agent holds no lease and creates no demand,
because `PARKED` is in `PENDING_STATES`. **4**: no worker waits. 2, 3 and
5-10 are unchanged.

### Request B. `profiles.py`: `RunnerProfile.control_plane`, a catalogue profile the platform never leases (HW-CONTRACT)

**Status:** proposed; the owner's Q10 accepted the design it serves.

#### What is true today

A step type is chosen by profile name (invariant 10), and every reader (the
console, the bridge, the outcome ledger) already labels a step by its
profile. But `RunnerProfile.__post_init__` allows an empty `runner_argv` only
on a `worker_action` profile, and a worker action is performed by the worker,
in a container, under a lease.

#### The change

The new property says the opposite of `worker_action`: **nothing ever runs
this, anywhere**. The scheduler's admission backstop (§4.4) reads it, so a
step of this profile can never be leased.

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

#### Rollback

Delete the field and its checks, once no profile sets it (request C rolled
back first).

#### What it would break

Nothing: the default is False, and every existing profile is unchanged. The
Terraform catalogue mirror (`runner_profiles` in `terraform/infra/locals.tf`,
held field by field by `tests/terraform/catalogue.tftest.hcl`) gains the field
in the same pull request.

#### Invariants

**1, 2, 3**: the field exists to prevent a lease. **10**: the caller still
names a profile, and the field is the catalogue's own, never a caller's.

### Request C. `profiles.py`: the `await-input` catalogue entry, shipped unavailable (HW-CONTRACT)

**Status:** proposed.

#### What is true today

No catalogue entry names a step that waits for a person.

#### The change

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
        timeout_seconds=300,          # unused; the wait is await_input's (at most 24 h)
        inputs={},
        available=False,
        disabled_reason="await-input steps are being built (docs/design/human-steps.md)",
    ),
```

`terraform/infra/locals.tf` adds it to `profiles_without_a_job` in the same
pull request, so no Job is ever created for it.

**Why unavailable.** Lanes HW-API-SUBMIT and HW-SCHED read the field and the
reason, so the contract has to land first. Until every build lane has merged,
a submission naming `await-input` must be refused, and `available=False` is
the refusal that already exists, with its reason served to the caller.

#### Rollback

Delete the entry. No agent can name it while it is unavailable.

#### What it would break

Nothing. Profile pickers list it as disabled, with the reason shown, as they
list `post-verdict`.

#### Invariants

As request B.

### Request D. `profiles.py`: `await-input` becomes available (owner applies, after HW-ACCEPT)

**Status:** to be applied by the owner once the acceptance group of §6 has
passed on dev. Filed as
[contract request 105](../contract-change-requests.md), ACCEPTED by the owner
2026-10-18 on PR 1570 and applied there. The owner turned it on in dev before
HW-ACCEPT, which cannot submit an await-input step while the profile is
unavailable; HW-ACCEPT then proves it on dev.

#### The change

One word: `available=True`, and `disabled_reason` removed.

#### Rollback

The word back.

#### What it would break

Nothing that exists. From then on, callers can submit the step.

#### Invariants

As request B.

### Not requested

There are no changes to `TaskState`, `_ALLOWED`, `EventType`, `Task`,
`WorkflowStep` (including `input_from`'s one-file-per-parent shape),
`Workflow`, `Settings` or `specsign`. §2.2 says why for each.

---

## 6. Build plan

Each lane owns the files named, and no file is in two lanes. Every lane
pushes its tests first to the pull request branch and its fix to
`<branch>-fix`, and runs `scripts/changed-guards.sh` before it finishes
(CLAUDE.md, "Lanes"). A test that needs a fake credential builds it at run
time (`"sk-" + "x" * 40`), never as one literal.

**The review lane, HW-REVIEW.** CLAUDE.md requires one review for
credentials, tenant isolation, redaction and IAM. Here that is **HW-IAM** (the
write grant and its condition), **HW-API-ANSWER** (person-only, fresh
membership, redaction, the file scan, the size gate, the create-only writer),
**HW-WORKER** (attachments staged across the tenant prefix check) and
**HW-MCP** (the elicitation that keeps an agent from answering). HW-REVIEW
reviews each of those four pull requests before it is labelled `ready`, and
reports blockers and majors on the PR; minors go to the wave epic.

| phase | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **HW-CONTRACT** (owner applies) | `apps/common/swarm_common/states.py`, `profiles.py`, `docs/contract-change-requests.md` (entries 85-87 from §5 A-C), `terraform/infra/locals.tf`, `tests/terraform/catalogue.tftest.hcl`, `apps/swarm-ui/src/types.ts`, `apps/swarm-ui/src/components/StatePill.tsx`, `apps/swarm-mcp/swarm_mcp/progress.py`, `apps/swarm-mcp/swarm_mcp/compact.py`, `tests/unit/common/test_await_input_contract.py` (new), `tests/unit/common/test_awaiting_input_readers.py` (new) | `AWAITING_INPUT` is a `ParkReason` and not in any capacity set; the readers test fails on a file that names `HUMAN_REQUIRED` and not `AWAITING_INPUT` and is not on its list, and its three behavioural cases (25-hour sweep, `human-ready` 409, no `handoffs` item) hold; a `control_plane` profile with `runner_argv`, `worker_action`, `provider`, `secrets`, `inputs` or `live_browser` raises; `await-input` is `control_plane`, unavailable, and in `profiles_without_a_job`; the parity script passes |
| 1 | **HW-IAM** (Track C; waits at `dev-iam`) | `terraform/infra/await_input.tf` (new: `google_storage_bucket_iam_member.api_input_writer`, local `await_input_object_condition`), `terraform/modules/firestore/indexes.tf` (`resource "google_firestore_index" "await_input_expiry" {`), `tests/terraform/await_input.tftest.hcl` (new) | the condition admits `tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json` and `…/artifacts/review.files/draft.md`; refuses `attempts/att_x/artifacts/…`, `attempts/input-0123456789ab/checkpoints/…`, `attempts/input-0123456789ab/live/…`, a key outside `tenants/`; the role is `objectCreator` only, bucket-scoped; `api_reader` is unchanged |
| 1 | **HW-SCHED** | `apps/scheduler/scheduler/awaitinput.py` (new), `loop.py`, `store.py`, `tests/unit/control_plane/test_await_input_scheduler.py` (new) | draft -> await -> revise: when `draft` succeeds the await step is `PARKED(AWAITING_INPUT)` with **zero leases** in the store; a READY `await-input` agent is parked by `_admit_one` and never leased whatever the pool headroom; `_expire_human_waits` does not touch it at 25 hours; under `fail_workflow` a failed sibling cancels it; its `task_finished` promotes `revise` within the same drain; `expires_at` is `opened_at` plus the declared seconds |
| 1 | **HW-API-SUBMIT** | `apps/swarm-api/swarm_api/schemas.py`, `validation.py`, `service.py`, `tests/unit/control_plane/test_await_input_submission.py` (new) | each refusal of §4.1 is a 422 naming its field, `assignees` included; `expires_after_seconds` of 86,401 is refused and 86,400 accepted; the API's bound equals the scheduler's `HUMAN_PARK_MAX_SECONDS` (both imported); `max_files` of 11 is refused; `await_input` lands in `metadata.dispatch` and changes the spec digest; a caller-sent `metadata.await_input` is refused as reserved; an await step is never in `integrates`; `form: "answers"` without a `questions.json` source is refused |
| 1 | **HW-WORKER** (Track B) | `apps/agent-worker/agent_worker/inputs.py`, `tests/unit/worker/test_input_attachments.py` (new) | an entry with two attachments stages them under `review.files/`; an attachment whose URI is in another tenant, another agent or outside `artifacts/` fails with `InputUnavailable` and stages nothing; an attachment with no `bytes` is refused; attachments count toward `max_total_bytes`; a resumed attempt with them in its checkpoint does not re-fetch; an entry without `attachments` stages exactly as today |
| 2 | **HW-API-ANSWER** | `apps/swarm-api/swarm_api/awaitinput.py` (new), `inputwriter.py` (new), `routes/await_input.py` (new), `main.py`, `auth.py`, `groups.py`, `routes/schedule_tick.py`, `apps/swarm-api/pyproject.toml`, `apps/swarm-api/uv.lock`, `tests/unit/control_plane/test_await_input_api.py` (new), `tests/unit/control_plane/test_await_input_files.py` (new) | a service-account caller is 403 and nothing is written; another tenant's step is 404; a member removed from the group after the cache filled is 403 (the fresh check); a lookup failure is 503 and nothing is written; a personal tenant's step refuses anyone but its owner; an answer before the park is 409 `not_open`; a second answer is 409 `already_answered`, while a replay of the same `answer_id` is the original 200; a form with a runtime-built fake token is stored redacted; a text file holding one is 422 `file_contains_credential` naming the line and rule and nothing is written; a zip or an executable is 415; a PNG named `.md` is refused; a 16 MiB + 1 byte body is 413 before any GCS call and a lying `Content-Length` is caught by the counting reader; a fifth concurrent multipart answer is 429; a GCS failure writes nothing to Firestore; answer, expiry and cancel racing in one test commit exactly once; every recorded URI, the attachments' included, passes the worker's `artifact_key` unchanged (imported, not restated); `inputwriter` refuses a key outside the condition's shape; `expire_due` honours `fail` and `default`; an unsigned spec is refused |
| 2 | **HW-NOTIFY** | `apps/swarm-api/swarm_api/notifications.py`, `approvals.py`, `tests/unit/control_plane/test_await_input_notifications.py` (new) | a reader on `remind` gets the item and another member does not; a reader in another tenant does not; the item id advances once per `remind_after_seconds` and nothing is written to Firestore by reading it; the inbox projects `input:<task>` for every member, has no decide path, and the item is gone once answered |
| 3 | **HW-UI** | `apps/swarm-ui/src/AwaitInputCard.tsx` (new), `awaitInput.ts` (new), `Workflows.tsx`, `RunSteps.tsx`, `Approvals.tsx`, `SubmitWorkflow.tsx`, their vitest files | the card renders each form; Approve posts `MERGE`; the picker refuses an eleventh file and a total over 16 MiB before sending; the warning is shown above the picker and beside Send; `max_files: 0` shows no picker; an expired or answered step shows who, when and how and offers no form; the pill reads "waiting for a member"; an image attachment is offered as a download, not rendered |
| 3 | **HW-MCP** | `apps/swarm-mcp/swarm_mcp/awaitinput.py` (new), `server.py`, `workflows.py`, `check.py`, `plugin/commands/sc.md`, `plugin/skills/sc/SKILL.md`, `plugin/README.md`, `tests/unit/mcp/test_await_input_bridge.py` (new) | `swarm_workflow_input` takes the answer only from the elicitation, so a tool call whose arguments carry a verdict sends nothing the person did not type; a host that cannot elicit is refused with the console link; files the person declines are not sent; a file list over the bound is refused before anything is sent; `swarm_inputs` lists the tenant's waits; `/sc check` lists a fresh wait as `awaiting_input` and not STUCK, and as STUCK two ticks past `expires_at` |
| 3 | **HW-DOCS** | `docs/workflows.md`, `docs/notifications.md`, this file ("What was built") | `test_docs_describe_what_was_built` (the 24-hour bound, the 16 MiB and ten-file bounds, the allowed types and the form names read from code) |
| 4 | **HW-ACCEPT** | `scripts/acceptance/groups/workflow.sh` | on dev, with `mock` for draft and revise: zero leases while parked; `revise` RUNNING within one drain of the answer; `revise`'s workspace holds the posted form and one attached file (sha256 equal for both); a second workflow with `expires_after_seconds: 300` and `on_expire: "default"` ends SUCCEEDED with the default and its `when`-gated step takes the declared branch |
| 5 | **HW-ENABLE** (owner applies) | request D | none: it is one word, applied once HW-ACCEPT is green |
| later | **HW-GITHUB** (Q3) | `apps/swarm-github-hooks/…`, the App's event subscription | an unconnected login is answered with a reply and no answer; a connected login answers as its member with `via: "github"` and passes the same fresh membership check; a bot login is refused; a comment is a form only |

**Order and dependencies.**

1. **HW-CONTRACT** first. Every other lane reads `AWAITING_INPUT`,
   `control_plane` or the `await-input` entry. The owner applies it.
2. **HW-IAM, HW-SCHED, HW-API-SUBMIT and HW-WORKER** in parallel, once
   HW-CONTRACT has merged. HW-IAM's dev release waits at `dev-iam` for the
   owner; it is dispatched last of the four so that the held plan does not
   cancel the others' releases for longer than it must.
3. **HW-API-ANSWER** after HW-API-SUBMIT merges, because it reads the stored
   block, and after HW-IAM is applied on dev, because without the grant its
   writes are 403s in every environment test. **HW-NOTIFY** runs beside it;
   it reads only the stored block and the park.
4. **HW-UI and HW-MCP** after HW-API-ANSWER, because they call its routes.
   **HW-DOCS** beside them.
5. **HW-REVIEW** on each of HW-IAM, HW-WORKER, HW-API-ANSWER and HW-MCP as
   it opens.
6. **HW-ACCEPT** after all of those are deployed to dev. Then **HW-ENABLE**.
7. **HW-GITHUB** after HW-ENABLE, as its own wave.

---

## 7. Owner questions

The owner answered Q1-Q11 on 2026-10-17 (PR 1462's comments carry the full
table). Each is recorded here as decided, with where the document applies
it. One question the answers leave genuinely open, Q12, is asked below and in
this lane's `questions.json`, with the recommendation first.

| # | question | decision (2026-10-17) | applied in |
|---|---|---|---|
| Q1 | Which park reason does a waiting step use? | A new `ParkReason.AWAITING_INPUT` (request A). The 24-hour sweep, `human-ready` and the browser bell never touch it, and a parity test fails any reader that forgets it. As recommended. | §2.2, §3.4, §4.4, §5 A, HW-CONTRACT |
| Q2 | Who may answer? | **Any member of the step's tenant**, checked as a member at the moment they answer; agents and service accounts always refused. **Departs** from the recommendation (assignees). `assignees` is removed; `remind` only chooses whose bell rings. **Amended 2026-10-18:** in a service tenant, its own principal answers its own steps, labelled `on_behalf_of` (§4.2.1). | §4.1, §4.2, §4.2.1, §4.5, §4.6 |
| Q3 | Which answer channels are in the first build? | Console and plugin/MCP now; GitHub comments later, in lane HW-GITHUB. As recommended. | §4.9, §6 |
| Q4 | How long may a step wait? | `expires_after_seconds` required, **at most 24 hours**, the same bound as `HUMAN_REQUIRED`. **Departs** from the recommendation (7 days). | §2.1 (invariant 8), §4.1, §4.4, §4.5, §1.3 |
| Q5 | What may happen at expiry? | `fail` (dead-letter) or `default` (an answer declared and signed at submission, so a downstream `when` gate takes the author's branch). As recommended. | §4.1, §4.6 |
| Q6 | What may an answer carry? | One of the JSON forms (verdict, answers, text), redacted before it is written, **and files up to 16 MiB in total**. **Departs** from the recommendation (no files). | §4.3, §4.6, §4.7, HW-API-ANSWER, HW-WORKER |
| Q7 | How are people reminded? | A bell item derived on read every `remind_after_seconds`; nothing stored or sent. As recommended. | §4.5 |
| Q8 | Can an answer be changed? | No. The first answer is final (409 `already_answered`); a correction is a new round. As recommended. | §4.6 |
| Q9 | How are several rounds expressed? | Unrolled in the DAG with `when` gates; no loop construct. As recommended. | §4.1 ("Rounds") |
| Q10 | May swarm-api write to the artifact bucket? | Yes: `roles/storage.objectCreator`, create-only, IAM-conditioned to `tenants/.../attempts/input-*/artifacts/`, from a separate write-only module; `objects.py` stays read-only. As recommended. | §3.1, §4.3, HW-IAM |
| Q11 | May an upstream agent's `questions.json` be the form? | Yes, only when staged explicitly (`form: "answers"` and `input_from` naming `questions.json`). As recommended. | §4.1 |

**Q12. How does the step after a review receive the answer's files?**
`input_from` stages one filename per parent, and the person names the files
after the workflow was submitted (§4.3, "Staging").
(a) **The worker stages them with the answer** (recommended): the answer's
manifest entry lists `attachments`, and the worker stages each into
`<artifact stem>.files/`, through the same tenant-prefix and destination
checks and inside the same 512 MiB total. It costs one small worker lane
(HW-WORKER, Track B), and a `when` gate still reads the form.
(b) **swarm-api writes the form and the files as one zip artifact**, with no
worker change. The next agent unzips it, but a `when` gate cannot read a
verdict inside a zip, so a verdict form with files would lose its gate.
(c) **Widen `input_from` to a list per parent** (a frozen change to
`WorkflowStep` and every workflow reader), with files declared by name at
submission. It changes the contract for every workflow to serve this one,
and the author must guess the names in advance.

---

## 8. What was built

**HW-DOCS (2026-10-18, part of #1436).** `docs/workflows.md` has a section,
"Waiting for a person: `await-input`", and a row for `await_input` in its
step-field table; `docs/notifications.md` has "A workflow step waiting for a
person" and names the source in its `task` row and its tenant-isolation
section. They describe what was on main at `f696290`: the contract entries
(HW-CONTRACT), the scheduler's park (HW-SCHED), the submission rules
(HW-API-SUBMIT), attachment staging (HW-WORKER), the answer, expiry and
listing routes (HW-API-ANSWER) and the bell and inbox items (HW-NOTIFY).
`tests/unit/scripts/test_docs_describe_what_was_built.py` reads every bound,
type and name they state from the code: the 24-hour bound (from both
swarm-api and the scheduler), the 16 MiB answer, the ten files, the 256 KiB
form, the closed type list, the three forms with their default artifacts,
the refusal reasons, the routes and the bell's titles.

Where the build differs from §4 and the docs follow the build: a refusal
carries the design's name in `detail.reason` under an established `code`
(`apps/swarm-api/swarm_api/awaitinput.py`, "THE CODES"), and the multipart
parser is swarm-api's own, bounded one, not `python-multipart`
(`apps/swarm-api/swarm_api/routes/await_input.py`, "THE MULTIPART PARSER").

Not built when this note was written, and so described in the docs only as
lanes still to land: the console card (HW-UI) and the plugin's
`swarm_workflow_input` and `swarm_inputs` (HW-MCP). Their sections in the
docs are theirs to write when they merge.

**HW-UI (2026-10-18, part of #1436).** The console's half, in
`apps/swarm-ui/src/AwaitInputCard.tsx` and `apps/swarm-ui/src/awaitInput.ts`.
An `await-input` step's node on Work › Workflows reads "waiting for a
member", its tooltip names the tenant, and its row says when it expires;
opening the workflow picks the waiting step, so its card is open on arrival.
The card shows the prompt and each artifact the step stages through the
existing artifact viewer, then the declared form (Approve sends `MERGE`,
Request changes `NOT_YET`; the upstream agent's questions; a text box) and,
when `max_files` is above 0, a file picker that refuses too many files, a
name or type the server refuses, and a whole answer over the bound before
anything is sent, with the §4.3 warning above it and again beside Send. An
answered, expired or cancelled step shows who, when and how and offers no
form; its files are offered as downloads, never drawn. The approvals inbox
lists a waiting step as an Answer, drawn from its row (the item has no
record to read) with no decide verbs, and its Answer opens the workflow. On
a run's Steps card the step reads what it waits for and leads to its card.
Submit › Workflow composes the `await_input` block for the step, and offers
the profile refused while the catalogue serves it unavailable.

Where the build differs from §4.5: the `answers` form is drawn from the
questions the step's own route serves, not through `QuestionsCard.tsx`,
whose Answer posts to the agent's answers route rather than the step's.

**ROLES-DOCS (2026-10-19, part of #1518).** §4.2's step 3 carries the
amendment above: workspace roles (`docs/design/workspace-roles.md`) let a
platform admin answer in a group workspace and require `member` of every
answerer. ROLES-GATES-DECIDE built it in the answer route, held by
`tests/unit/control_plane/test_roles_gates_answer.py`. The console's answer
card has no role gate of its own; the server decides.
