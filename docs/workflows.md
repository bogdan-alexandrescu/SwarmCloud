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

`input_from` stages an upstream step's artifact into the downstream step's
workspace. The file travels through **GCS**, never inline through Firestore —
Firestore has a 1 MiB document limit, and an agent's output is routinely larger.
The tenant's GCS prefix applies, so a workflow cannot stage another tenant's
artifact even by naming it.

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

## Failure behaviour

| `on_step_failure` | Intended effect |
|---|---|
| `fail_workflow` (default) | a failed step fails the workflow; steps not yet started do not start |
| `continue` | independent branches keep going; only dependents of the failed step are blocked |

**`on_step_failure` IS NOT HONOURED TODAY, and the two rows behave identically.**
This is stated rather than softened because a runbook that promises
`fail_workflow` will stop a workflow is one somebody follows at 3am. The field is
validated (`schemas.py:87`), stored (`service.py:388`) and echoed back
(`codec.py:506`); nothing reads it. `grep -rn on_step_failure apps/` finds no
reader outside the codec and the schema.

What actually happens, under both settings:

* the dependents of a failed step are CANCELLED by the scheduler's dependency
  sweep, with `last_error` naming the failed parents
  (`scheduler/loop.py:295,494`);
* every independent branch keeps running to completion.

That is the `continue` row. `fail_workflow` selects it too. Cancel explicitly if
you need a workflow stopped:

```bash
./scripts/api.sh POST "/workflows/$WORKFLOW_ID/cancel"
```

Steps already `RUNNING` are **not** killed when a sibling fails, under either
setting. They hold leases and partial work; killing them wastes what
checkpointing exists to preserve. This is also why the derived workflow state
below reports RUNNING rather than FAILED while a sibling is still live: the
workflow is not over and a container is still costing money.

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
make lint test           # shellcheck, terraform fmt/validate, tflint, unit tests
make build push deploy   # to dev
make smoke
git push -u origin feature/thing && gh pr create
```

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

Production applies require **manual approval** through a GitHub Environment.
`plan` runs on the PR so the diff is reviewable before anyone approves an apply
into a project that hosts another team's production.

## Release flow

```
PR  -> checks -> review -> merge to main
        |
        v
    release.yml: build -> immutable :<sha> tags -> trivy -> promote digest
        |
        v
    dev apply + deploy + smoke
        |
        v
    prod: environment approval -> apply -> deploy -> smoke
```

Nothing is ever deployed by a mutable tag. `build/deployed-images-<env>.json`
records the digest of every promotion, which is also what makes a rollback a
lookup rather than an archaeology exercise.
