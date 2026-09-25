# Workflows

Two different things share this name, and both are documented here:

* **Part 1 — task workflows**: multi-step agent DAGs submitted to the API.
* **Part 2 — development workflows**: the local loop, and the CI/CD pipeline in
  `.github/workflows/`.

---

# Part 1 — Task workflows (DAGs)

A workflow is a set of steps with dependencies. Each step becomes an ordinary
task, so everything true of a task is true of a step: it is admitted through the
same transaction, counts against the same pools, checkpoints the same way, and
costs nothing while it waits.

```bash
./scripts/api.sh POST /workflows '{
  "priority": 10,
  "on_step_failure": "fail_workflow",
  "steps": [
    {"step_id": "analyse",
     "runner_profile": "claude-code",
     "input": {"prompt": "Summarise the failing tests in this repo"}},

    {"step_id": "fix",
     "runner_profile": "claude-code",
     "depends_on": ["analyse"],
     "input_from": {"analyse": "summary.md"},
     "input": {"prompt": "Fix the failures described in summary.md"}},

    {"step_id": "verify",
     "runner_profile": "generic",
     "depends_on": ["fix"],
     "input_from": {"fix": "patch.diff"},
     "input": {"command": "pytest"}}
  ]
}'
```

## Step fields

| Field | Notes |
|---|---|
| `step_id` | unique within the workflow; `^[A-Za-z0-9][A-Za-z0-9_\-.]*$` |
| `runner_profile` | a **name** from the frozen catalogue — never an image or command |
| `input` | the step's own payload, bounded by `max_input_bytes` |
| `depends_on` | upstream `step_id`s, up to 50 |
| `input_from` | `{upstream_step: artifact_filename}` staged into this step's workspace |
| `resource_class` | optional named class, no larger than the profile's own |
| `timeout_seconds` | may only **shorten** the profile's timeout |

`max_workflow_steps` (default 50) bounds the whole thing.

## Artifacts pass by reference

**An artifact is a file the upstream step wrote into `$SWARM_ARTIFACTS_DIR`, and
nothing else is.** Only that directory is uploaded when an attempt ends. It sits
outside the agent's working directory and outside the repository checkout, so a
file written anywhere else is never uploaded and no later step can stage it,
however exactly its name matches. Until #149 this failed late and cost money:
the upstream step SUCCEEDED, and only the dependant failed, at staging, with
`upstream task … did not produce an artifact named 'scan-01.md'`. By then every
upstream step had spent its compute and its provider quota. That is what
happened to workflow `wf_73946ff4a32a4f99b3a4` on 2026-09-25. Its eight scan
steps were prompted "write it to scan-01.md", wrote into their working
directories and succeeded. All four merges then failed, and seventeen steps were
cancelled.

**So the platform tells the upstream agent, catches its likeliest wrong guess,
and fails the upstream attempt when neither worked.** Three layers, because the
first alone was measured not to be enough.

1. **Instructions.** At submission the API inverts every `input_from` edge. It
   records on each upstream step's task the filenames its dependants will
   stage, as `metadata.expected_outputs`, in the same write that creates the
   task. The worker passes that list to the runner. Only the `claude-code` and
   `codex` runners act on it: they append the names to the prompt they give
   the agent, with the **absolute** path of `$SWARM_ARTIFACTS_DIR` and the
   statement that files written anywhere else, the repository included, do not
   reach later steps. The `generic`, `mock` and `browser` runners receive the
   list in `input.json` and change nothing. A name the platform writes itself
   is never in the instructions: not the worker's `swarm-work.patch`, which is
   written after the agent exits, and not the runner's own
   `<runner>.stdout.log`, `<runner>.stderr.log` or transcript, which it writes
   while the agent runs.
2. **`./artifacts` is the artifacts directory.** On the third run,
   `wf_06a3a949d2c242c3b0e9` (2026-09-25), every prompt named
   `$SWARM_ARTIFACTS_DIR` and told the agent to `echo` it. scan-02 echoed
   `/workspace/att_…/artifacts`, which is right, then wrote
   `/workspace/att_…/work/artifacts/scan-02.md`, and reported that it had
   written the file to `$SWARM_ARTIFACTS_DIR`. The artifacts directory is the
   working directory's sibling, so `./artifacts` is the natural wrong guess. So
   before every agent starts, workflow step or not, the worker makes
   `work/artifacts` a symlink to the artifacts directory, and a file written to
   `./artifacts/<name>` lands in the directory that is uploaded. The link is
   never checkpointed: it points outside `work/`, which a restore refuses, and
   it names this attempt's directory, so a resumed attempt makes its own. If
   `work/artifacts` already exists (a declared input staged as `artifacts/…`,
   or a directory a restored checkpoint brought back), it is left alone and
   one WARNING says so. Files written under it are then not uploaded, exactly
   as before.
3. **A missing expected output fails the attempt, retryably** (owner decision
   on #149, 2026-09-25). If an attempt's runner finishes cleanly and one of its
   expected outputs was not uploaded, the attempt FAILS with the missing names
   as its cause, in `last_error`, in `result_summary.expected_outputs_missing`,
   in one log line and in a `retrying` event. The cause says whether the file
   was never written, or was written and not uploaded (the artifact cap). The
   task goes back to READY and runs again as a new attempt while it has
   attempts left, and ends FAILED once `max_attempts` is spent, which cancels
   its dependants as any failed parent does. A requested cancel still ends it
   CANCELLED. **A dependant therefore never starts on a parent that did not
   write what it promised.** A runner that fails, times out or is stopped
   keeps its own cause, and the missing names are recorded next to it.

None of this makes an agent write the file; the third layer makes it cost at
most `max_attempts` upstream attempts instead of the rest of the workflow. A
prompt that names the file and the directory as well does no harm.

**A retry starts with an empty artifacts directory.** It resumes `work/` from
the checkpoint the failed attempt took as it ended, and only `work/` is
checkpointed. So the retry must write every expected output again, not only the
one that was missing. The agent is given the same instructions, which list every
name. **An attempt that is going to be retried publishes nothing**, as a parked
attempt does not: its work is not finished. Published, the retry's own push
could be refused as a non-fast-forward, because the final checkpoint is taken
before the publish step auto-commits the agent's changes. The last attempt
publishes like any other failed one. A file that was written and then not
uploaded (the cap, an upload error) is found missing only after the publish
step, so that attempt has published and is still retried.

**`metadata.expected_outputs` belongs to the service.** A caller cannot set it:
`POST /v1/tasks`, `POST /v1/tasks/batch`, or a workflow whose own `metadata`
carries it, is refused with 422 `invalid_dispatch` and
`detail.reserved_metadata_keys: ["expected_outputs"]`, and nothing is created.
This is the reservation `metadata.dispatch` already has. Accepted, the key
would reach the agent as "later steps of this workflow need these files", in
the platform's voice, on a task no step stages from. For the same reason the
worker drops a caller's own `input.expected_outputs` before the runner sees it,
and logs that it did.

What this deliberately does not do:

* **It does not copy a file out of the working directory.** The worker does not
  go looking for a same-named file in the working directory and upload it. That
  was option (c) on #149, and the owner rejected it. The link is not a copy: a
  file written through it is written into the artifacts directory in the first
  place. A file left anywhere else, in the repository or elsewhere in the
  working directory, stays there.
* **It does not carry the artifacts directory across a park or a retry.** Only
  `work/` is checkpointed, and a resumed attempt starts with an empty artifacts
  directory. A dependant stages from the attempt that SUCCEEDED, so a file
  written before a park and not written again after the resume is missing, and
  that attempt fails for it, retryably. That was already true of
  `$SWARM_ARTIFACTS_DIR`; the link extends it to `./artifacts`.
* **It does not tell a retry which file it left out.** The retried agent gets
  the same prompt and the same list, which names every expected output.

`input_from` stages an upstream step's artifact into the downstream step's
workspace. The file travels through **GCS**, never inline through Firestore —
Firestore has a 1 MiB document limit, and an agent's output is routinely larger.
The tenant's GCS prefix applies, so a workflow cannot stage another tenant's
artifact even by naming it.

The filename is both the artifact's name in the upstream step and the path it
lands at downstream. So within one step each parent must stage a **distinct**
relative filename, with no empty, `.` or `..` segment, and the API refuses any
other shape at submission (HTTP 422 `invalid_dag`, naming the step, the parents
and the file) rather than letting the worker refuse it after every parent has run.

## Dependencies and state

A step with unmet dependencies is `PARKED(DEPENDENCY_INCOMPLETE)`. Parked costs
nothing (invariant 1), so a hundred-step workflow with a two-hour first step
occupies exactly one slot, not a hundred.

The scheduler's dependency sweep promotes steps whose upstreams have succeeded,
bounded by `dependency_sweep_size` per run so one enormous workflow cannot
monopolise a pass.

**Cycles are rejected at submission**, with the exact cycle named. A cyclic DAG
would otherwise sit in `DEPENDENCY_INCOMPLETE` forever — holding no capacity, but
never completing and never erroring, which is the worst kind of failure because
nothing alerts on it.

## `metadata.input_from` belongs to the service, not the caller

The worker stages a task's inputs from `metadata.input_from`, a map of
`{upstream TASK id: filename}`. **Only workflow expansion writes that key.** It
rewrites each step's `input_from`, keyed by step id, to the ids of the upstream
tasks it has just created. That rewrite happens after the declaration has passed
the checks above, including the rule that every `input_from` source is also a
`depends_on`.

So the API refuses the key from callers, the same way it refuses
`metadata.dispatch` (owner decision on #151, 2026-09-25):

| request | answer |
|---|---|
| `POST /v1/tasks` or `POST /v1/tasks/batch` with `metadata.input_from` | 422 `invalid_dispatch`, `detail.reserved_metadata_keys: ["input_from"]`, nothing created |
| `POST /v1/workflows` whose own `metadata` has `input_from` | the same 422, nothing created |
| `POST /v1/workflows` whose steps declare `input_from` | accepted; each declaring step's task gets `metadata.input_from` |

The key is refused whatever its value, `{}` and `null` included. It is
reserved, not validated. A value on a plain task arrived with no dependency
edge, so nothing guaranteed the upstream task had run. A bad declaration was
refused only by the worker, after the task had been admitted and had held
capacity. The workflow's own `metadata` is copied onto every step's task, so a
value there reached every root step verbatim and was silently replaced on any
step that declared its own. To stage an artifact, declare `input_from` on the
workflow step that needs it.

A batch is refused whole: one task carrying the key means none of the batch is
created.

**Three metadata keys are reserved, and one refusal names all of them.**
`dispatch`, `input_from` and `expected_outputs` (above) are the keys the
service writes into `task.metadata`. They are one tuple,
`validation.RESERVED_METADATA_KEYS`, checked by one function. A caller who sends
more than one gets a single 422 whose `detail.reserved_metadata_keys` lists each
of them, in that order, and whose message says why each is reserved and what to
send instead. `expected_outputs` was first reserved by a check of its own (#153),
so as not to collide with the `input_from` change. That check heard about one
key per round trip, and it was folded into the tuple once both had merged.

## Failure behaviour

| `on_step_failure` | Effect when a step is FAILED or DEAD_LETTERED |
|---|---|
| `fail_workflow` (default) | every step of the workflow that has not started (SUBMITTED, QUEUED, READY, PARKED) is CANCELLED, dependent on the failure or not |
| `continue` | only the transitive dependents of the failed step are CANCELLED; independent branches keep starting |

The scheduler honours the field from the release that carries PR #42. Before
that nothing read it, and both rows behaved like `continue`. The rules that
are not obvious:

* **Workflows submitted before that release are covered too, unless a cutoff
  is set.** Every one of them stores `fail_workflow`, because that was the API
  default, and each was submitted while this page said the setting was not
  honoured. With `ON_STEP_FAILURE_ENFORCED_SINCE` unset (the default), the
  first drain after the deploy cancels the not-started steps of any in-flight
  workflow that already has a FAILED or DEAD_LETTERED step. A cancelled step
  cannot return to READY, so its checkpoint becomes reclaimable and its partial
  work is gone. Set `ON_STEP_FAILURE_ENFORCED_SINCE` on the scheduler (ISO-8601
  with a UTC offset, for example `2026-09-25T14:00:00Z`) and only workflows
  created at or after that instant get the rule. Older ones keep the
  dependency rule they were submitted under. A value without an offset refuses
  to start. To see what the next drain would cancel, run the read-only audit
  before deploying. It exits 3 if a page came back full:

  ```bash
  PROJECT_ID=saga-agents-staging FIRESTORE_DATABASE=swarm \
    uv run --project . python -m scheduler.on_step_failure_audit
  ```
* **It happens on the scheduler's next drain, not at the instant of failure.**
  That is a Pub/Sub push or the one-minute safety tick. A failure the drain
  itself writes, when a step exhausts its attempts on a failed dispatch, stops
  its siblings in that same drain. A failure a worker or the reconciler writes
  while a drain is running is seen by the next drain, because the verdict is
  read once per drain. So a sibling can still START after the failure: one
  that drain admits in the meantime. The window is the rest of that drain,
  which `MAX_RUN_SECONDS` bounds (45 s by default). No read closes it
  completely, because a failure can land between reading the verdict and
  taking the lease. A step that starts in that window holds capacity, so it
  runs to completion like any other running step.
* **Every cancel names the failure.** The step's CANCELLED event carries
  `workflow_id`, `on_step_failure` and `failed_steps` (step id, task id,
  state), and its `last_error` names the failed step. The metric is
  `swarm_scheduler_cancelled_total{reason="workflow_failed"}`, and the drain
  summary counts the cancels in `cancelled` and the workflows in
  `failed_workflows_swept`.
* **CANCELLED is not a failure, under either setting.** A step stopped by hand
  takes its own dependents (`last_error` "an upstream workflow step did not
  succeed") and nothing else. Otherwise pressing stop on one agent would end
  the whole run under the default.
* **A step that has not started is found through the scheduler's existing
  touch points,** not by scanning failures: the dependency sweep, the
  credential sweep, the prewarm sweep, and admission, which every step passes
  through before it can start. A step parked for a missing key is cancelled
  where it is parked, by the credential sweep. So is one parked for provider
  quota, cooldown or outage, by the prewarm sweep, while prewarm is enabled
  (the default; with it off, those reasons join the list below). A
  workflow whose only remaining steps are PARKED on a reason the scheduler never
  reads (MANUAL_PAUSE, BUDGET_EXHAUSTED, SCHEDULED_RETRY) is swept when one of
  them is promoted. Until then it holds no capacity, and its derived state reads
  PARKED rather than FAILED.
* **Each cancel is re-checked inside a transaction**
  (`SchedulerStore.cancel_if_not_started`). A step that a concurrent drain
  leased after it was read is left to run.

Cancel explicitly if you need a workflow stopped, running steps included:

```bash
./scripts/api.sh POST "/workflows/$WORKFLOW_ID/cancel"
```

Steps already holding capacity (LEASED, DISPATCHED, STARTING, RUNNING) are
**not** killed, flagged or written to when a sibling fails, under either
setting. They hold leases and partial work. Killing them wastes what
checkpointing exists to preserve, and releasing their capacity from the
scheduler would decrement pools a live container still occupies (invariant 1).
They run to completion. This is also why the derived workflow state below
reports RUNNING rather than FAILED while a sibling is still live: the workflow
is not over and a container is still costing money. Under `fail_workflow` it
reads FAILED once they finish.

A step that was running when its sibling failed, and that later returns to a
not-started state (a quota park, or a reconciler reclaim back to READY), is
PARKED or READY when the scheduler next meets it. Under `fail_workflow` it is
then cancelled like any other step that has not started.

## Workflow state

`workflow.state` is **derived from the step tasks on every read**, and the stored
copy is written back when the two disagree. Both halves matter and they are
different halves:

* **derived**, so the value a reader sees cannot go stale. Until 2026-09-22
  nothing ever advanced the stored field — `create_workflow` set it once and
  `cancel_workflow` only touched `cancel_requested` — so every workflow read
  `QUEUED` for its whole life. `wf_bcdc9180e4fb4a209f31` read QUEUED while its
  three steps were SUCCEEDED, FAILED and CANCELLED.
* **written**, so it is queryable. A purely derived field cannot answer "list my
  failed workflows" without loading every workflow's tasks.

The rule, in `apps/swarm-api/swarm_api/rollup.py`, in the order it is applied:

| condition | derived state |
|---|---|
| any step's task could not be read | `UNKNOWN` |
| any step holds capacity (`LEASED`/`DISPATCHED`/`STARTING`/`RUNNING`) | `RUNNING` |
| every step terminal | the worst present: `DEAD_LETTERED` > `FAILED` > `CANCELLED` > `SUCCEEDED` |
| otherwise | the most advanced pending: `READY` > `PARKED` > `QUEUED` |

`UNKNOWN` is not a `TaskState` and is never written to Firestore. It is what a
read says when it could not establish an answer, and it exists so that a
derivation over a partial read cannot come back as "SUCCEEDED because the
failures did not load".

`cancel_requested` stays a separate boolean and is **not** folded into the state.
Cancelling is a request: a step holding a lease keeps it until the worker or the
reconciler releases it, so between the request and the release the workflow
really is still running.

### The drift check

The stored copy and the derived one are two records of one fact, which is the
defect shape this platform has had elsewhere (a pool counter versus a lease sum).
Every workflow read therefore carries a `drift` block:

```json
{"stored": "QUEUED", "derived": "FAILED", "agrees": false, "repaired": true}
```

`agrees` is three-valued. `null` means the two were **not compared**, because a
step could not be read — the same caution the capacity-holders screen carries
about a delta computed over a truncated page. A disagreement stays reported as a
disagreement after the write-back has fixed it, and moves
`swarm_api_workflow_state_drift_total`, because a repair that leaves no trace is
a silent resolution.

Workflows nobody reads are converged by an explicit sweep:

```bash
./scripts/api.sh POST "/admin/workflows/rollup?tenant_id=$TENANT"
```

There is no periodic caller for that route yet; see request #7 in
[contract-change-requests.md](contract-change-requests.md).

## Inspecting

```bash
./scripts/api.sh GET "/workflows/$WORKFLOW_ID" | jq '{
  state: .workflow.state,
  stored: .workflow.stored_state,
  drift: .workflow.drift,
  steps: [.tasks[] | {step_id, state, park_reason, blocked_by}]
}'
```

A step stuck at `DEPENDENCY_INCOMPLETE` whose upstream shows `SUCCEEDED` means
the sweep has not run yet — wake the scheduler, or wait for the 1-minute tick.

## When not to use a workflow

If steps do not exchange artifacts and do not depend on each other, submit a
**batch** (`POST /v1/tasks/batch`, up to `max_batch_size`). Independent tasks
interleave across tenants under round-robin; a workflow adds dependency
bookkeeping you are not using.

---

# Part 2 — Development workflows

## Local loop (primary): the Firestore emulator

```bash
make dev                      # emulator + seeded pools + the API on :8000
make dev TARGET=scheduler     # the drain loop instead
make dev TARGET=quota-broker
make dev TARGET=emulator      # emulator only; point your own process at it
```

`scripts/lib/dev.sh` starts `gcloud emulators firestore`, waits for it, seeds the
slot pools the scheduler needs, and runs the service with
`FIRESTORE_EMULATOR_HOST` set. No cloud credentials are used and nothing touches
`saga-agents-staging`.

The emulator is used rather than a mock because the admission path depends on
**real transaction semantics** — the last-slot race resolves the way it does
because Firestore aborts and retries a transaction whose read set changed. A mock
that returns canned documents cannot exercise that.

Requirements: `gcloud components install cloud-firestore-emulator`, and a JRE
(the emulator is a Java program). On a machine without one, `dev.sh` says so and
points at the container path below.

## Local loop (secondary): docker compose

```bash
docker compose up api            # emulator + seeded pools + the API
docker compose up scheduler
docker compose run --rm tests    # unit tests, no cloud credentials at all
docker compose down -v
```

This is the **optional** path for operators whose Docker works. It is not
primary because the Docker daemon on the reference workstation is broken, and
because this Mac is arm64 while every target is amd64.

Nothing in `docker-compose.yml` builds a deployable artifact: it mounts the
source into a stock Python image. Images are always built by Cloud Build.

Every port binds to `127.0.0.1`. Authentication is **not** disabled — the API
has no switch for that, and refuses `REQUIRE_AUTH=false` rather than ignoring
it — but the emulator behind it is an unauthenticated database, so the stack
must never be exposed beyond loopback.

## Tests

```bash
make test                # unit tests + the destroy-guard self-test. No cloud.
uv run pytest tests/unit -q
uv run pytest tests/unit/control_plane -q -k admission
```

`tests/unit` needs no emulator and no credentials: the admission logic is pure
(`evaluate_capacity`) and the rest uses in-memory fakes. That is deliberate — the
concurrency invariant is the thing most worth testing exhaustively, so testing it
must be fast.

`make test` also runs `destroy.sh --self-test`, which verifies the teardown guard
still catches deny-listed and unlabelled resources. That guard is the only thing
standing between `make destroy` and another team's production cluster, so it is
checked on every test run rather than only before a teardown.

Cloud-backed suites are separate because they cost money and need a deployment:

```bash
make smoke concurrency-test race-test quota-test failure-test load-test
```

## Making a change

```bash
git checkout -b feature/thing
# edit
git commit && git push -u origin feature/thing
gh pr create
gh run list --branch feature/thing   # read the run
gh run view <id> --log-failed        # read the failure
```

**The lint, the tests, the build and the deploy are all CI's**, not this
machine's: `application.yml` and `terraform.yml` gate the pull request, and
`release.yml` builds, applies and deploys on the way to an environment. See
[where the gates run](ci.md). The `make` targets above this section are how
those workflows invoke each suite and how an operator drives a deployment they
already have credentials for — they are not steps in authoring a change.

**Never edit `apps/common/swarm_common/`.** It is frozen by `CONTRACT.md`. If a
change is genuinely needed there, say so in the PR description rather than making
it — every other component was written against those types, and a quiet change
breaks them all at once.

## CI/CD

| Workflow | Trigger | Does |
|---|---|---|
| `terraform.yml` | PR touching `terraform/**` | fmt, validate, tflint, checkov, `plan` posted to the PR |
| `application.yml` | PR touching `apps/**`, `scripts/**`, `tests/**` | shellcheck, unit tests, integration tests, build |
| `security.yml` | every PR + weekly schedule | trivy (repo + images), checkov, secret scan |
| `release.yml` | push to `main`, or manual | build, push immutable SHA tags, plan/apply with environment approval, deploy, smoke |

All four authenticate to GCP with **Workload Identity Federation**. There are no
downloadable service account keys anywhere in this repository or its secrets —
see `terraform/bootstrap/wif.tf`, where the attribute condition pins both the
repository and the allowed refs. Without the ref pin, a workflow triggered from a
fork could mint a deploy token.

Production releases require **manual approval** through a GitHub Environment,
before `:prod` is promoted and before the apply
([ci.md](ci.md#a-prod-release-waits-for-approval-before-anything-prod-facing)).
`plan` runs on the PR so the diff is reviewable before anyone approves an apply
into a project that hosts another team's production.

## Release flow

```
PR  -> checks -> review -> merge to main
        |
        v
    release.yml: build (or reuse CI's) -> immutable :<sha> tags -> trivy
        |
        v
    dev: promote digest -> apply + deploy + smoke          (no approval)
        |
        v
    prod (dispatched): trivy -> APPROVAL -> promote digest -> apply -> deploy -> smoke
```

Nothing is ever deployed by a mutable tag. `build/deployed-images-<env>.json`
records the digest of every promotion, which is also what makes a rollback a
lookup rather than an archaeology exercise.
