# Model per step: a workflow step picks its model class by name

**Status: designed 2026-10-17, revised the same day for the owner's answers
(lanes MODEL-STEP-DESIGN and MODEL-DESIGN-B, part of #1435), and built by
the lanes of §6. §8 records what each lane built and where the code departs
from this design: the class is signed at spec format 5, not 4, and there is
no `model_classes_enabled` flag (R4).** The first version recommended one profile per model tier (design A).
The owner answered its §7 on PR 1460 and chose **design B**: a per-step
`model_class` picked by name from a frozen vocabulary of four classes, on
every claude-code-family runtime. This revision describes that decision. §7
lists only the questions it raises. The frozen-contract half is written as
requests in §5. They are requests, not changes: the operator files them in
[contract-change-requests.md](../contract-change-requests.md) once approved
(CLAUDE.md rule 1).

Every file and symbol cited below was read on 2026-10-17 at `dd203ab`. Lines
move, so search for the symbol named beside each one.

What the design settles, in one line each:

* **A caller picks a profile by name, and optionally a model class by name.**
  `model_class` is one of `opus`, `fable`, `sonnet`, `haiku`, from the frozen
  `MODEL_CLASSES`. It is accepted on `TaskCreate`, `WorkflowStepCreate`,
  `RunCreate`, the child-task route and the plugin bridge (§4.4). A caller
  still never sends a model ID, an image, a command, resources or a backend
  (invariant 10).
* **The frozen catalogue holds the vocabulary and each profile's accepted and
  default classes. Terraform holds the model IDs**, in `runner_models` keyed
  by class. The scheduler refuses to start if a class's ID is not of that
  class (§4.1, §4.2).
* **The defaults.** `opus` is the default class, for coding and implementation
  steps. `fable` is for planning. `sonnet` and `haiku` are for mechanical
  steps. `claude-code`, `claude-code-browser` and `indexer` accept all four
  (§4.1).
* **The class is signed.** The step spec gains a format 4 that covers
  `model_class`, and the API writes no format-4 task until every worker that
  could receive one can verify it (§4.3).
* **The dispatcher sets `MODEL` per execution**: in the pod manifest on GKE,
  and in the per-execution override on the Cloud Run fallback Jobs. The value
  is the scheduler's own pin for the step's class, never a string from the
  caller (§4.5).
* **An attempt records the model it ran** (`Attempt.model`), so cost by model
  is a query (§4.6).
* **Unchanged:** pools, admission and quota windows. A step's class shares its
  profile's `runner:` pool, and every quota window binds every attempt, as
  today (§2.1, §4.7).

---

## 1. The problem, as measured

### 1.1 What is true today

Since #226 (owner decision of 2026-09-26), the model a claude-code agent runs
is fixed per runner profile, and the profile is the only thing a caller picks:

* **One map states every model**: `runner_models` in
  `terraform/infra/locals.tf`. It has four entries, and all four are
  `claude-opus-5-5`: `claude-code`, `claude-code-review` (retired),
  `indexer` and `claude-code-browser`. No profile the platform can dispatch
  runs anything but Opus.
* That map becomes `MODEL` on each Cloud Run Job this root creates, and
  `WORKER_MODELS` on the scheduler. The scheduler sets `MODEL` on every GKE
  pod it renders (`apps/scheduler/scheduler/dispatch.py::profile_model`, used
  by `GkeJobDispatcher` beside the comment "THE PROFILE'S MODEL, as a Cloud
  Run Job carries it"). The Cloud Run per-execution override does not carry
  `MODEL`: it carries `worker_env`, which holds identifiers and endpoints
  only.
* The worker reads `MODEL` into `WorkerConfig.model`
  (`apps/agent-worker/agent_worker/config.py`) and hands it to the runner
  child (`apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`).
  The runner passes it as `--model`
  (`apps/agent-worker/agent_worker/runners/cliagent.py`, the block under
  "THE MODEL IS THE JOB'S, NEVER THE CALLER'S").
* A caller cannot change it, by design. `input.model` is refused at the API
  because `claude-code` declares no such input (`RunnerProfile.inputs`,
  contract request 25: "EMPTY IS A DECLARATION ... That is what closes
  `input.model` on `claude-code`"). The worker also drops a stored one
  (`lifecycle.py`, "dropped the caller's own input.model"), and
  `tests/unit/control_plane/test_input_model_is_refused.py` holds both.
  `TaskCreate.model` is accepted but is attribution only.
  `tests/unit/mcp/test_model_flag_is_attribution_only.py` is a deliberate
  brake on wiring it through: its worker-env half asserts that `worker_env`
  carries no `MODEL`.
* **The catalogue knows nothing about models.** `RunnerProfile` has no model
  field, and the comment on `runner_models` says so: "the catalogue has no
  model, and giving it one is a contract request." So neither `GET
  /v1/runtimes` (`apps/swarm-api/swarm_api/routes/platform.py::runtimes`) nor
  `swarm_profiles` can say which model a profile runs.
* **The signed step spec already covers `Task.model`** (format 1's projection
  in `apps/common/swarm_common/specsign.py::canonical_step_spec`). It covers
  nothing that selects a model, because nothing does. `SPEC_FORMAT` is 3 and
  `SPEC_FORMATS` is `(1, 2, 3)`.

### 1.2 What it costs

The issue (#1435, opened 2026-10-16) states the cost as a property of the
design, not as a measured bill: every claude-code step runs Opus, "including
steps that only reformat text". It gives no spend figure. This lane had no
Firestore access to read one, so this document does not invent one. What can
be stated:

* **List prices.** `_LIST_PRICES` in `apps/agent-worker/agent_worker/lifecycle.py`
  (since #1481 the `model_prices` platform setting, §4.10) (rates cached 2026-06-24, checked 2026-10-05) has `claude-opus-5-5` at $4
  input / $20 output / $0.20 cache read per million tokens. It has
  **`claude-fable-5-1` at $10 / $50 / $0.25**, so Fable costs 2.5 times Opus
  per token. Planning on Fable is therefore a quality choice, not a saving.
  Neither `claude-sonnet-5-5` nor `claude-haiku-5-5` is in the table. The
  table's prefix match does not price them as `claude-sonnet-5` or
  `claude-haiku-4-5` either: `_MODEL_SUFFIX` admits only a date or a context
  marker after the priced name. For scale only, the previous Sonnet
  (`claude-sonnet-5`, $2 / $10) is half of Opus 5.5 and Haiku 4.5 ($1 / $5) is
  a quarter. That is not a measurement of the 5.5 tier (§4.10).
* **Subscription accounts.** Most claude-code attempts run on pool accounts
  (`apps/quota-broker/quota_broker/accounts.py`). On those, the cost is not
  dollars but the account's quota windows. `Account.headroom` takes the
  minimum over every window, model-scoped ones such as `seven_day_opus`
  included. By the owner's answer to Q5 it keeps doing so (§4.7). How much of
  a shared window a Sonnet turn consumes compared with an Opus turn is the
  provider's accounting, and was not measured here.
* **The platform's own mechanical steps run Opus too.** The scheduled types
  `epic_triage`, `docs_drift` and `observer`
  (`apps/swarm-api/swarm_api/schedtypes/`) each pin `PROFILE = "claude-code"`.
  By the owner's answer to Q7 they stay on `opus`: they send no `model_class`.

### 1.3 What "done" lets someone do

Someone writing a workflow can put `model_class: "haiku"` on the step that
classifies feedback, `model_class: "sonnet"` on the step that summarises a
source, `model_class: "fable"` on the step that plans, and nothing on the
steps that implement (which then run `opus`, the default). Then:

* each step's attempt runs, and records, the model its class is pinned to;
* `swarm_profiles`, `GET /v1/runtimes` and the console's Runtimes screen show
  which classes each profile accepts, its default, and the model ID behind
  each class;
* the outcome ledger reports cost per model, from the attempts;
* `input.model` is still refused, and the only selector a caller gets is a
  name from a frozen four-word vocabulary. It cannot choose a model ID, an
  image, a command, resources or a backend.

Today none of that is possible: there is one model, and no way to name
another.

---

## 2. Constraints that bind the design

### 2.1 The invariants it touches

**1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Preserved. A
step's class is a field on a task. A QUEUED, PARKED or READY step on any class
holds no lease and no pod. The class is resolved when the dispatcher renders
the execution, after admission, so it cannot create demand earlier.

**2. All-or-nothing reservation in one transaction.** Preserved, unchanged.
By the owner's decision, **the classes share their profile's pool**. A `haiku`
step on `claude-code` acquires the same seven pools as an `opus` step:
`global`, `tenant:<t>`, `resource:standard`, `runner:claude-code`,
`backend:GKE_AUTOPILOT`, `provider:anthropic` and
`provider:anthropic:tenant:<t>`. `pool_names_for`
(`apps/common/swarm_common/models.py`) is not touched, there is no
`model:<class>` pool, and admission does not change. The consequence is
stated rather than hidden: a flood of Haiku steps takes `runner:claude-code`
slots that Opus steps would otherwise use. This is the owner's choice. A
per-class ceiling would need a `pool_names_for` change, which nothing here
asks for. **Invariant 3** follows: concurrency still counts from LEASED,
whatever the class.

**5. Fencing.** Preserved. The class is fixed on the task at submission and
signed (§4.3). The dispatcher resolves it to a model ID when it renders this
generation's execution, so a stale generation has nothing to act on. A stale
worker still exits before it runs the agent and without touching the lease
(`lifecycle.py`, unchanged). The worker's new model check (§4.5) runs after
its generation check. `Attempt.model` is written in the same write as
`started_at`, after that check, so a fenced-out worker never records a model.

**8. Checkpointing.** Preserved, with one consequence stated. Every class
inherits the profile's `checkpoint_interval_seconds` (120) and
`supports_checkpoint`. **A checkpoint never crosses a class**: a task's
`model_class` is signed and does not change between attempts, and nothing here
lets it. A retry restores its checkpoint under the same class. It runs under
that class's *current* pin, so a Terraform re-pin between attempts (for
example opus-5-5 to a later Opus) resumes the session under the newer model of
the same class. That is true today for a re-pin of the profile's model, and
§7 R6 asks whether it should stay so.

**9. Tenant isolation.** Preserved, unchanged. A step of any class uses the
tenant's own GSA and KSA, its own `-anthropic` credential or its own (or
explicitly lent) pool account, its own GCS prefix and its own namespace. The
broker is not changed (§4.7), so the class cannot widen the set of accounts an
attempt is offered.

**10. Callers pick a runner_profile by name.** Preserved, read the way
`resource_class` already is: "a NAMED class from the frozen catalogue, and
only one no larger than the profile's own" (`WorkflowStepCreate.resource_class`
in `apps/swarm-api/swarm_api/schemas.py`). `model_class` is a NAME from a
frozen four-word vocabulary, accepted only if the profile declares it. The
model ID it stands for is Terraform's and reaches the worker only through the
scheduler's own settings. A caller cannot send a model ID that selects
anything: `input.model` stays refused on every profile, and
`TaskCreate.model` is filled by swarm-api from the pin and refused when it
differs (§4.4). The one thing that changes is what the brake test holds: the
scheduler now puts `MODEL` in the worker environment. §4.5 states exactly
what still cannot reach it.

Untouched: **4** (no new wait inside a worker), **6** (no profile changes
backend or Spot strategy; `SpotStrategy` stays `ON_DEMAND_ONLY`, which
`RunnerProfile.__post_init__` enforces), **7** (no resource change;
`requests == limits`).

### 2.2 What it needs from `apps/common/swarm_common/`

Five requests, all in §5:

* `profiles.py`: the `MODEL_CLASSES` vocabulary (request A);
* `profiles.py`: `RunnerProfile.model_classes` and
  `RunnerProfile.default_model_class`, set on the claude-code family
  (request B);
* `models.py`: `Task.model_class` and `WorkflowStep.model_class` (request C);
* `models.py`: `Attempt.model` (request D);
* `specsign.py`: format 4, covering `model_class` (request E).

Not requested: `pool_names_for` (the classes share the profile's pool),
`states.py` (no new park reason), `EndCause` (a worker that refuses its model
ends `cannot_start` with a code, §4.5), and new profiles (design A's
`claude-code-sonnet` and `claude-code-haiku` are not built).

### 2.3 Other constraints found while reading

* **`check_worker_models` refuses a map entry for a profile the catalogue does
  not hold** (`apps/scheduler/scheduler/settings.py::check_worker_models`). A
  class-keyed map would fail that check on a scheduler that has not been
  updated. So the class-keyed map ships under a **new** variable,
  `WORKER_CLASS_MODELS`, which an old scheduler ignores. `WORKER_MODELS` stays
  for one release as a rollback path (§4.2).
* **The pinned Claude Code CLI must accept every pinned ID.** The base image's
  Dockerfile records that CLI 2.1.273 refused the then-new `claude-opus-5-5`
  as `unrecognized_model`, and every claude-code task failed
  (`images/agent-runtime-base/Dockerfile`, the comment above
  `CLAUDE_CODE_VERSION`, now 2.1.283). The same can happen to any class's ID.
  The acceptance canary runs one real step per class (§4.9).
* **All three claude-code-family images share one CLI.**
  `images/agent-runtime-browser/Dockerfile` and
  `images/agent-runtime-indexer/Dockerfile` are built `FROM ${BASE_IMAGE}`,
  and only the base Dockerfile pins `CLAUDE_CODE_VERSION`. So an ID cannot be
  valid on one profile and invalid on another. This is why `runner_models` is
  keyed by class alone (§4.2).
* **The spec signer signs at the oldest format that covers what a task sets**
  (`specsign.signing_format`). A worker built before a format refuses that
  format as `unknown_format` (`agent_worker/specverify.py`, `KNOWN_FORMATS`).
  Format 3 shipped in that order, worker first and submission second
  (contract request 54, "the rollout order is the worker (OB5) first, then the
  submission (OB7)"). Format 4 reuses the rule, and §4.3 adds a gate so that
  the order does not depend on merge timing alone.
* **`tests/unit/common/test_specsign_covers.py` holds the signed field list
  honest.** A field the worker acts on and the spec does not cover fails it.
  The worker acts on `model_class` (§4.5), so format 4 must cover it.
* **The child-task field list is stated twice.** `CHILD_FIELDS` in
  `apps/agent-worker/agent_worker/children.py` restates the route's strict
  model in `apps/swarm-api/swarm_api/children.py`, and
  `tests/unit/worker/test_child_tasks_worker.py` holds the two equal. Both gain
  `model_class` together, in one lane (MODEL-API, §6): the test asserts
  `CHILD_FIELDS == set(ChildSpec.model_fields)`, and the route's model is
  `extra="forbid"`, so either file changing alone is a red run and a worker
  that sends a field the route refuses.
* **Provider rate limiting is per provider and tenant, not per model.** The
  broker's AIMD state is keyed `/v1/quota/{provider}/{tenant_id}`
  (`quota_broker/main.py`), so a metered-key 429 on an Opus step throttles that
  tenant's Haiku steps too. By the owner's answers on pools and Q5, this stays.

---

## 3. Designs that could work

### Design A: one profile per model tier

`claude-code-sonnet` and `claude-code-haiku` join the catalogue, each
identical to `claude-code` except for its name and a model class. A workflow
step already names its own `runner_profile`, so per-step choice needs no new
request field. This was the first version's recommendation.

* **Its cost is multiplication.** A tier is per image, so four classes on
  three images make twelve entries where today there are three. Each one has
  its own `runner:` pool, ceiling, Terraform mirror and name-table entry
  (`runner_spec`, `AGENT_STREAM_FILES`, `DEFAULT_ALLOWED_PROFILES`).
* **Not chosen.** The owner asked for four classes on every claude-code-family
  runtime (Q3), and asked for the classes to share their profile's pool. Both
  undo what A offered.

### Design B: a per-step `model_class`, chosen by name (decided)

The caller sends `model_class: "sonnet"` on a task or a step, from the frozen
`MODEL_CLASSES` vocabulary. Each profile declares the classes it accepts and
its default, in the same way it declares its `resource_class`.

* **Code.** Larger than A. `Task` and `WorkflowStep` gain `model_class`
  (contract). `TaskCreate`, `WorkflowStepCreate`, `RunCreate`, the child-task
  route and the plugin bridge accept and validate it. The signed step spec
  covers it (format 4) and every worker verifies that format. The dispatcher
  sets `MODEL` per execution on both backends.
* **Migration.** A spec-format bump, staged so that no worker sees a format it
  cannot verify (§4.3). Existing tasks have no `model_class`, so they run
  their profile's default, `opus`, which is what they run today.
* **Latency.** None. Same image, same backend, same start path.
* **Money.** A mechanical step pays the cheaper model's tokens. A planning
  step on `fable` pays more than on `opus` (§1.2).
* **What it does not give.** Per-class ceilings. The owner has chosen to share
  pools, and §2.1 states what follows from that.

### Design C: the platform chooses

No caller input: a classifier, or a rule on the prompt, picks the model per
step. Rejected, as before. It adds a model call before every dispatch, its
choice cannot be reviewed before the run, and a wrong guess silently runs deep
work on Haiku. The person who wrote the workflow knows which step is
mechanical; the platform does not.

### Recommendation

**B, as the owner decided on 2026-10-17.** It gives every claude-code-family
runtime every class without new catalogue entries. It keeps the caller's
selector a name from a frozen vocabulary, which is the reading invariant 10
already accepts for `resource_class`. And it keeps each pool, ceiling and
name table keyed by profile exactly as it is. What it costs is code and one
staged format bump, both laid out below.

**There is no platform-wide planning default.** No profile's
`default_model_class` is `fable`, and an issue run's planner defaults to
`opus`. The owner's rule that **design lanes default to fable** is the
operator's spec setting: the lane briefs and workflow specs the operator
launches put `model_class: "fable"` on a design step. The platform never
infers a class from a step's role or prompt (design C stays rejected).

---

## 4. The design

### 4.1 The catalogue

The vocabulary (request A) is four named classes, in a dict as
`RESOURCE_CLASSES` is:

| class | family word in the ID | pinned ID (Terraform, §4.2) | meant for |
|---|---|---|---|
| `opus` | `opus` | `claude-opus-5-5` | **the default**: coding and implementation steps |
| `fable` | `fable` | `claude-fable-5-1` | planning ("Fable 5+") |
| `sonnet` | `sonnet` | `claude-sonnet-5-5` | summarising, reformatting |
| `haiku` | `haiku` | `claude-haiku-5-5` | classifying, extracting |

Each class carries the rule that holds an ID to it. `ModelClass.admits(id)` is
true when the ID is `claude-<family>-<major>[-<minor>]`, optionally followed
by an eight-digit date, with `major` at least the class's `min_major`. That
floor is 5 for all four classes: the owner's "Fable 5+" applied to every
class, since all four pins are generation-5 models. So `claude-fable-5-1` and
a later `claude-fable-6` are `fable`. `claude-opus-5-5` is not `fable`, and
`claude-sonnet-4-6` is not `sonnet`. A re-pin to an older generation fails at
startup instead of shipping.

Each profile declares `model_classes` (the classes it accepts) and
`default_model_class` (request B):

| profile | `model_classes` | `default_model_class` | why |
|---|---|---|---|
| `claude-code` | all four | `opus` | Q3 |
| `claude-code-browser` | all four | `opus` | Q3 |
| `indexer` | all four | `opus` | Q3 |
| `claude-code-review` | `opus` only | `opus` | retired under contract request 50 (request 84 (b)); it keeps the pin it has today and gains nothing |
| `codex` | none | none | an OpenAI CLI, where a Claude model name would fail every run (the comment on `runner_models`) |
| `mock`, `generic`, `browser`, `merge`, `post-verdict` | none | none | they start no model |

`__post_init__` refuses: a class name not in `MODEL_CLASSES`; a default not in
the profile's own `model_classes`; classes without a default, or a default
without classes; and classes on a `worker_action` profile or on one whose
`provider` is not `anthropic`.

A frozen helper, `profiles.step_model_class(profile, requested)`, is the one
statement of the resolution rule that swarm-api, the scheduler and the worker
all apply. It returns `requested` if the profile accepts it, returns the
profile's default if `requested` is None, returns None for a profile with no
classes, and raises `ValueError` naming the accepted classes otherwise.

### 4.2 Where the model IDs live, and what holds them to their classes

The owner answered Q1 (a): the class vocabulary and each profile's accepted
classes are in the frozen catalogue, and the IDs are in Terraform.
`runner_models` in `terraform/infra/locals.tf` is now **keyed by class**:

```hcl
runner_models = {
  opus   = "claude-opus-5-5"
  fable  = "claude-fable-5-1"
  sonnet = "claude-sonnet-5-5"
  haiku  = "claude-haiku-5-5"
}
```

**Why by class and not by profile × class.** All three claude-code-family
images run one CLI, pinned once in the base Dockerfile (§2.3), so an ID
accepted on one profile is accepted on all three. A profile × class map would
be twelve lines that must change together at every model release. The only
thing it could express beyond the class map is one profile lagging another on
an older model. #226 removed exactly that, when the review agent was set to
"name the same model" so that it would not judge with a different model than
the one that wrote the change. If a profile ever needs to lag, a
`<profile>/<class>` override key can be added then, with its own check.

**Why the ID is not in the frozen catalogue** (unchanged from the first
version, and Q1 (a)): the opus-5 to opus-5-5 move was an edit to this map and
a release. If the catalogue held the IDs, every model release would be a
contract request.

**What it becomes.**

* `WORKER_CLASS_MODELS = jsonencode(local.runner_models)` on the scheduler
  and on swarm-api (a new variable, §2.3).
* `WORKER_MODELS`, the profile-keyed map that existing schedulers read, is
  derived for one release as `{profile: runner_models[default_model_class]}`
  over the catalogue mirror, so that rolling back the scheduler image alone
  still finds its map. It is removed by a follow-up lane once a release
  carrying MODEL-SCHED has applied (§6).
* Each Cloud Run fallback Job's own `MODEL` (`cloud_run_fallback_profiles`,
  today `claude-code` and `indexer` among the classed profiles) is
  `runner_models[<the profile's default_model_class>]`. The per-execution
  override replaces it with the step's class (§4.5).

**What stops the IDs drifting from their classes.** Three checks, each reading
the catalogue rather than restating it:

1. **The scheduler's startup check.** A new
   `scheduler.settings.check_worker_class_models` refuses a map whose keys are
   not exactly the `MODEL_CLASSES` names, or in which an ID fails its class's
   `admits`. A `haiku = "claude-opus-5-5"` typo stops the scheduler instead of
   quietly billing Opus, and a class with no pin cannot reach a dispatch.
   swarm-api applies the same check to its own copy, using the same frozen
   function.
2. **`tests/terraform/runner_model.tftest.hcl`** asserts the four keys and
   each ID's family word.
3. **The catalogue mirror.** `runner_profiles` in `locals.tf` (held field for
   field by `tests/terraform/catalogue.tftest.hcl`) gains `model_classes` and
   `default_model_class`. `scripts/lib/check-contract-parity.sh` gains a
   section asserting that the class names in `runner_models` are exactly
   `MODEL_CLASSES`.

### 4.3 The signed step spec: format 4, and how old workers are retired first

> **Amended 2026-10-19 (MODEL-DOCS), from the code as built.** The format that
> covers `model_class` is **format 5**, not 4: contract request 96
> (MULTI-REPO) took format 4 for `additional_repositories` first, so request
> 104 added format 5 (`SPEC_FORMAT = 5`, `SPEC_FORMATS = (1, 2, 3, 4, 5)` in
> `apps/common/swarm_common/specsign.py::SPEC_FORMAT`). Read "format 4" below
> as format 5 wherever it means the format that covers a class. Step 3 below
> was not built: there is no flag (owner decision R4). Lane order alone kept
> the classes unreachable until the worker, the scheduler and the catalogue
> had merged, and swarm-api's `model_class_not_yet_available` now means only
> that the deployment pins no model for the class (`WORKER_CLASS_MODELS`
> unset), not that a gate is closed.

**The format** (request E). Format 4 is format 3's projection plus
`model_class`. `SPEC_FORMAT` becomes 4 and `SPEC_FORMATS` becomes
`(1, 2, 3, 4)`. `signing_format` keeps its rule, the oldest format that covers
every field a task sets: it returns 4 only when `model_class` is set.
**Formats 1 to 3 refuse a document that sets `model_class`**
(`SpecNotCanonical`), as formats 1 and 2 refuse a forge field. The worker acts
on the class itself, so adding one to a task signed without it must fail
verification rather than fall outside the signed bytes.

**What swarm-api stores.** `model_class` is stored only when it differs from
the profile's default. Naming the default stores nothing, so the bytes are the
ones the task would have had before this feature. The result is that only a
step that asks for a non-default class is ever signed at format 4, and every
default step keeps format 1, 2 or 3 throughout the rollout. "Absent" is
signed too, in effect, because adding the field fails verification.

**How workers that cannot verify format 4 are gone before the API writes it.**

1. **The worker ships first.** MODEL-WORKER (§6) adds format 4 to
   `KNOWN_FORMATS` (which reads `specsign.SPEC_FORMATS`) and adds the model
   check (§4.5). The release that carries it moves `WORKER_IMAGE_REFS`
   to the new digests in the same apply as the images.
2. **Why that retires the old workers.** A worker's image is fixed when its
   attempt is dispatched. Every GKE attempt is a new pod rendered from the
   scheduler's current `WORKER_IMAGE_REFS`. A Cloud Run fallback Job is
   brought up to the current image by `CloudRunJobDispatcher.ensure_job`
   before its first execution in each scheduler process. So once that release
   has applied, no new attempt starts on an old worker. An old worker still
   running holds a task signed at format 3 or lower, which it can verify. A
   retry of any task runs in a new pod, on the new worker.
3. **The API is gated, not just ordered.** swarm-api refuses a non-default
   `model_class` with 422 `model_class_not_yet_available` until
   `MODEL_CLASSES_ENABLED` is true. That is a Terraform variable,
   `model_classes_enabled` (default `false`), wired by MODEL-CONTRACT and
   flipped in the tfvars by MODEL-ENABLE. MODEL-ENABLE is dispatched only
   after the release carrying MODEL-WORKER **and** MODEL-SCHED has applied.
   The flag makes the order hold even if lanes merge into the same release.
4. **What a rollback does.** If the scheduler's image refs are rolled back to
   a worker without format 4, that worker refuses a format-4 task as
   `unknown_format`, ending `spec_signature_invalid`. It fails closed: it
   never runs a step on the wrong model. Turning the flag off again stops new
   format-4 tasks being written, and default steps are unaffected throughout.

**Unsigned legacy tasks.** The worker's legacy window (`SPEC_LEGACY_UNTIL`,
2026-10-20 in `specverify.py`) admits a task created before signing shipped,
and no such task can carry a class. That window closes three days after this
design is written, so by the time MODEL-WORKER is built it has probably
closed and the worker refuses every unsigned task outright; the rule below
then costs nothing, and holds only if the window is ever extended. For an unsigned task the worker expects
the profile's default class and ignores any stored `model_class`. A class
written onto one therefore fails the model check (§4.5), and the attempt does
not run.

### 4.4 The API

**Where `model_class` is accepted.** On each of these it is
`str | None = Field(default=None, max_length=32)`, validated by one function
in `apps/swarm-api/swarm_api/validation.py`, `validate_model_class`, beside
`validate_runner_profile`. That function calls `profiles.step_model_class`.

| surface | file | what it sets |
|---|---|---|
| `TaskCreate` | `schemas.py` | the task's class |
| `WorkflowStepCreate` | `schemas.py` | the step's class, stored on `WorkflowStep.model_class` and copied to its task |
| `RunCreate` | `schemas.py`, `issueruns.py` | the planner's class. Each plan step may carry its own (§7 R1) |
| the child-task route | `children.py` (swarm-api) and `CHILD_FIELDS` (worker) | the child's class. If absent, the child **profile's** default, never the parent's (§7 R3) |
| the plugin bridge | `apps/swarm-mcp/swarm_mcp/client.py`, `server.py`, `cli.py` | passed through as sent (§4.8) |

**Refusals**, each a 422 with a stable `code`:

* a name not in `MODEL_CLASSES`: `unknown_model_class`, listing the four;
* a class the profile does not accept: `model_class_not_accepted`, naming the
  profile's accepted classes (for `codex` or `mock`, "this profile runs no
  Claude model");
* before the gate opens: `model_class_not_yet_available` (§4.3).

**`TaskCreate.model`** (Q4, decided). swarm-api fills `Task.model` from the
pin for the step's resolved (profile, model_class), using its own
`WORKER_CLASS_MODELS`. A caller who sends `model` equal to that pin is
accepted. Any other value is refused with 422 `model_is_chosen_by_model_class`,
and the message names the `model_class` to send. If the value is another
class's pin, the message says "to run claude-sonnet-5-5, send model_class:
\"sonnet\"". Otherwise it lists the profile's classes and their models. This
applies on every surface that accepts `model`: `TaskCreate`, the child-task
route's `model`, and the plugin's `--model`. `Task.model` is a format-1 field,
so filling it changes no signing format.

`Task.model` is the pin **at submission**. It is a record, not a selector.
The dispatcher resolves the class's pin again at dispatch (§4.5), and
`Attempt.model` records what ran (§4.6).

`input.model` stays refused on every profile. No profile's `inputs` gains
`model`, and `test_input_model_is_refused.py` is extended to every classed
profile.

**`GET /v1/runtimes`** (`routes/platform.py::runtimes`) serves three more
fields per profile:

```json
"claude-code": {
  "name": "claude-code",
  "available": true,
  "model_classes": ["opus", "fable", "sonnet", "haiku"],
  "default_model_class": "opus",
  "models": {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1",
             "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"},
  "...": "every existing field, unchanged"
}
```

The classes are read from the catalogue and `models` from swarm-api's
`WORKER_CLASS_MODELS`, so the route reports the map the scheduler dispatches
with. A profile with no classes serves `[]`, `null` and `{}`.

**`GET /v1/tasks/{id}`, workflows and `/v1/attempts`.** Each task and step
carries `model_class` (absent means the profile's default; the API serves the
resolved value, with `model_class_defaulted: true` when nothing was stored).
Each attempt carries `model` (`codec.attempt_from_dict`,
`codec.attempt_to_api`). `attempt_from_dict` must read it explicitly: that
decoder has dropped written fields before (its own comment on the five spend
fields).

**Outcome ledger** (`apps/swarm-api/swarm_api/outcomes.py`). `GROUPS` gains
`model`, grouped by `Attempt.model` over started attempts, with "not recorded"
as its own row for attempts from before the change. It also gains
`model_class`, grouped by the task's resolved class. Grouping by
`runner_profile` no longer separates tiers, so these two are how cost by model
is read.

### 4.5 The dispatcher and the worker: `MODEL` per execution

**One resolution, in the scheduler.** A new function in
`apps/scheduler/scheduler/dispatch.py`, `step_model(settings, profile, task)`,
replaces `profile_model`. It resolves the class with
`profiles.step_model_class(profile, task.model_class)` and returns
`settings.worker_class_models[class]`. It returns None only for a profile with
no classes. An unaccepted class, or a class with no pin, raises `DispatchError`
(code `model_not_pinned`), which returns the lease and the task. It never falls
through to the CLI's own default, which is how the QA task of 2026-09-26 ran
`claude-sonnet-5` on an Opus profile. The startup check (§4.2) makes the
missing-pin case unreachable. The dispatch-time refusal is the second line of
defence for a document written past the API.

**Both backends, from one place.** `worker_env` adds `MODEL = step_model(...)`
when it is not None. That is the only change to it:

* **GKE.** `gke_worker_env` is `worker_env` plus the metadata server, so the
  pod manifest carries the step's `MODEL`. The separate append in
  `GkeJobDispatcher` (under "THE PROFILE'S MODEL, as a Cloud Run Job carries
  it") is removed, so that there is one source.
* **Cloud Run fallback Jobs.** `CloudRunJobDispatcher.dispatch` already
  passes `worker_env` as the per-execution `ContainerOverride.env`, and an
  override merges with the Job's environment (`worker_env`'s own docstring,
  "OMITTED RATHER THAN SET EMPTY"). So the step's `MODEL` replaces the Job's
  default-class `MODEL` for that execution only. `ensure_job` keeps
  reconciling the Job's own `MODEL` to the default class's pin, so a Job run
  any other way runs `opus`, as today.

**The brake, deliberately inverted.**
`tests/unit/mcp/test_model_flag_is_attribution_only.py`'s worker-env half
(`test_worker_environment_carries_no_model_for_a_task_that_names_one`) is the
gate its own docstring describes: "If it ever fails because MODEL was wired
into `worker_env`, that is the contract change -- go and get the decision."
The owner's answer to Q2 is that decision, and MODEL-SCHED inverts the test on
purpose, rewriting its docstring to cite this design and the owner's answer.
The inverted test asserts that `worker_env` carries `MODEL`, and that its value
is **the scheduler's own `WORKER_CLASS_MODELS` entry for the task's resolved
class**. **What still cannot reach `worker_env`**, each asserted:

* **`Task.model`.** A task whose stored `model` is any other string (written
  to Firestore past the API) dispatches with the class's pin, and that string
  appears nowhere in the environment.
* **`input.model`, or any other `input` key**, and **any `metadata` key**:
  none of them becomes `MODEL` or any other variable.
* **A model ID in `model_class`.** `model_class: "claude-opus-5-5"` is not a
  class, so the dispatch raises rather than passing the string through.
* **A class the profile does not accept.** It raises. It does not fall back to
  the default, and it does not pass the name through.
* **Anything for a classless profile.** `codex`, `mock`, `generic` and the
  others get no `MODEL`, as today.

The test's help-text half is unchanged and stays true: `--model` still records
and does not select (§4.8).

**The worker checks what it was given** (`apps/agent-worker/agent_worker/lifecycle.py`).
After spec verification and the generation check, and before it reads a
credential or starts the CLI:

* it resolves the expected class with `profiles.step_model_class` from the
  **signed** `model_class`, or from the profile's default on an unsigned
  legacy task;
* if the profile has classes and `MODEL` is unset, it ends `cannot_start` with
  code `model_not_pinned`;
* if `MODEL` is set and the expected class does not `admit` it, it ends
  `cannot_start` with code `model_class_mismatch`, logging both the class and
  the ID (an ID is not a secret).

The worker deliberately does not require `MODEL == Task.model`. A Terraform
re-pin while a task is queued would then fail every queued task of that class,
and the class, which is what is signed and promised, is still honoured (§7
R6).

`runner_spec` (`runners/streams.py`), `AGENT_STREAM_FILES`
(`swarm_api/agent_streams.py`) and `DEFAULT_ALLOWED_PROFILES`
(`swarm_api/repositories.py`) need **no** change, because no profile is added.

### 4.6 What an attempt records

`Attempt.model` (request D) is the model ID the worker was started with
(`WorkerConfig.model`), or None for a profile that runs no model. The worker
writes it in the same merge as `started_at`, after its fencing check
(`agent_worker/control.py::ControlPlane.record_attempt_start`).

The stream's own record stays where it is. `_usage_summary` already collects
the `model` of every assistant message into the runner summary's
`usage.models` (`lifecycle.py::_usage_summary`). The two are different facts.
If they disagree (the pinned CLI substituted another model), the worker logs
`model_mismatch` with both values, and the console shows both (§4.8).

`record_spend` writes only fields the frozen `Attempt` declares (its own
docstring), which is why this is a contract request rather than a worker
change.

Firestore, `attempts/{attempt_id}`, one new field. `tasks/{id}` and the
workflow's steps gain `model_class`:

| document | field | type | written by | meaning |
|---|---|---|---|---|
| `attempts/{id}` | `model` | string or absent | worker, at attempt start | the model ID it ran. Absent on attempts before this change and on profiles with no model |
| `tasks/{id}` | `model_class` | string or absent | swarm-api, at submission | the class asked for. Absent means the profile's default. Signed at format 4 |
| `workflows/{id}`, `steps[]` | `model_class` | string or absent | swarm-api | the same, per step |

### 4.7 Accounts and quota windows: windows unchanged, classes served per account

By the owner's answer to Q5, **every quota window keeps binding every
attempt**, as today. The first version's `AccountAssign.model_class` field, the
broker's window filter and the MODEL-BROKER lane are dropped.
`Account.headroom` and `_binding_remaining` keep taking the minimum over every
window, so an account whose `seven_day_opus` window is spent is refused to a
Haiku step too. That is the stated cost of the decision.

**Which classes an account serves** is the owner's answer to R5 (b),
2026-10-17 on PR 1509, built by MODEL-ACCOUNTS: a plan that cannot run a class
(Fable is the expected one) must never be handed a step of it.

* `accounts/{id}.model_classes` lists the `MODEL_CLASSES` names the account
  serves, in the vocabulary's order. **Empty, the default, is every class**,
  so no existing account changes until its owner narrows it
  (`PUT /v1/accounts/{id}/model-classes` on the broker and on swarm-api, the
  Accounts screen's "classes" editor). A malformed value makes the document
  unreadable, out of the pool, never "every class".
* `Account.serves_class` is the one rule. `accounts_serving`, `choose` and
  `eligibility` take the step's class, and the assign and swap routes pass
  it. The broker **reads** the class (`quota_broker.main.step_class_for`):
  `tasks/{task_id}` in the caller's own tenant, resolved by the frozen
  `step_model_class`. No class travels in the request body. A class the
  broker cannot read (no task id from an older worker, or a task that is not
  the caller's) is served only by an account that serves every class.
* A class no account serves is `no_accounts_registered` to the worker, which
  falls back to the tenant's own key; a metered key serves every class.
  Admission asks the same question first (`scheduler.credentials._class_answer`)
  and parks a keyless tenant's step `CREDENTIAL_MISSING` with
  `account_pool: no_account_serves_class` and the class, **before any lease**.
  It holds nothing (invariant 1), nothing was reserved, so the all-or-nothing
  reservation is untouched (invariant 2), and the credential sweep promotes it
  once an account serves the class.

### 4.8 The console and the plugin

* **Runtimes** (`apps/swarm-ui/src/Runtimes.tsx`, types in `types.ts`, the
  loader and its fixture in `api.ts`): a "Model classes" column listing each
  accepted class as a chip with its ID, the default marked; `—` for a profile
  with none.
* **Submit** (`RunnerPicker.tsx`, `SubmitWorkflow.tsx`): a class select per
  task and per step, offering only the profile's accepted classes from
  `/v1/runtimes`, defaulting to the profile's default, each labelled with its
  model. The select is hidden for a classless profile.
* **Agent detail** (`AgentDetail.tsx`, `AttemptTimeline.tsx`): each attempt
  shows its `model` and the task's class. When the stream's `usage.models`
  disagrees, both are shown with "the CLI ran a different model from the one
  pinned".
* **Ledger** (`Ledger.tsx`): the `model` and `model_class` groups (§4.4).

No new section, tab or route, so the issue forms' "Where" list
(`tests/unit/scripts/test_issue_forms.py`) is unaffected.

**Plugin and MCP:**

* `swarm_profiles` (`apps/swarm-mcp/swarm_mcp/profiles.py::_served_entry`,
  and the bundled copy it falls back to) passes `model_classes`,
  `default_model_class` and `models` through.
* The bridge's dispatch, workflow and run tools (`server.py`, `client.py`)
  accept `model_class` on a task, on each workflow step and on a run, and
  pass the API's 422 through.
* `swarm dispatch --model-class` is new (`cli.py`). `--model` keeps its help
  text ("recorded for attribution; does not select the model"), which stays
  true, and gains "send --model-class to choose one".
* `plugin/skills/delegate/SKILL.md` and `plugin/skills/sc/SKILL.md`: one
  paragraph on the classes. Mechanical work goes on `haiku` or `sonnet`,
  implementation stays on the default, and planning is `fable` where the
  operator's spec asks for it. This replaces "asking for one is not a thing a
  caller may do".

### 4.9 Availability and the canary

By the owner's answer to Q8, **the classes are available on merge**, which in
practice means when the gate of §4.3 opens. Acceptance gains one canary step
per class: four steps on `claude-code`, `opus`, `fable`, `sonnet` and `haiku`,
each with a fixed prompt that ends quickly. For each one it asserts:

* `Attempt.model` equals the class's pin from `/v1/runtimes`;
* the runner summary's `usage.models` contains that ID and nothing of another
  class.

A red canary opens or updates the usual `bug` issue (`accept.yml`'s report).
It is red until the CLI in `agent-runtime-base` accepts every pinned ID
(§2.3).

### 4.10 Prices

**Moved, 2026-10-17 (#1481, lane PRICES-STORE).** The table below is no longer
code. List prices are the `model_prices` platform setting
(`control/settings`, served and set by `/v1/admin/settings`, PUT admin-only and
stamped with who changed it), seeded with exactly the figures `_LIST_PRICES`
held, `source: code`, `as_of: 2026-10-05`. `_LIST_PRICES` is gone from the
worker. A worker whose run stopped before it reported a cost sends the usage
per model to swarm-api (`POST /v1/attempts/{id}/spend-estimate`,
`swarm_api.spendestimate`), and swarm-api prices it from the setting as it reads
at that moment, so a price change applies to the next stopped attempt with no
deploy. The rule is unchanged: a model with no entry prices nothing, and the
attempt reads "cost not reported". The paragraphs below describe the table as
it was when it lived in code; read "the table" as the setting. The weekly
pricing-page refresh and the nightly observed-rate check (#1481) are not built
yet.

By the owner's answer to Q9, `_LIST_PRICES` gains only dated figures that the
owner or an operator supplies. **`claude-fable-5-1` is already in it** ($10 /
$50 / $0.25, cached 2026-06-24 and checked 2026-10-05, with the table's own
note), so a stopped Fable attempt is estimated today. Until figures are
supplied, a stopped attempt on **`claude-sonnet-5-5` or `claude-haiku-5-5`
prices nothing** and reads "cost not reported", which the table's own rule
calls honest. A completed attempt on any class still reports the CLI's own
`total_cost_usd`.

**Can prices be pulled automatically?** The owner asked. Three options, and
the choice is §7 R2:

1. **Observed per-token rates, derived on a schedule.** Completed attempts
   already carry the CLI's `total_cost_usd` and its token usage, split into
   input, output, cache read and cache write. A scheduled type could fit
   per-model rates from a week of completed attempts on metered keys and
   compare them with `_LIST_PRICES`. That is a measurement of what the
   provider actually billed. Its limits: it is only as good as the CLI's
   own figure; a model needs enough completed attempts first; and on
   subscription accounts the CLI's figure is an equivalent, not a charge. It
   would write a report and open a PR or an issue on disagreement. It would
   never edit the table itself.
2. **A scheduled read of the provider's pricing documentation, feeding a
   reviewable PR.** A scheduled step reads the published model and pricing
   pages, and opens a PR that adds or changes `_LIST_PRICES` lines, each
   dated and citing its source, for a person to merge. This keeps the table's
   "change a price only with the date it changed" rule, with a reviewer in
   the loop. Its limit is that it depends on the page's layout, so it can
   fail to parse and must then say so rather than guess.
3. **Third-party price trackers: rejected.** They are unaudited, they lag
   releases, and a wrong figure in the ledger is worse than none.

### 4.11 What the platform's own steps do

* **Scheduled types** (Q7). `epic_triage`, `docs_drift`, `observer` and every
  other type in `schedtypes/` send no `model_class`, so they run `opus`.
  `custom_prompt` schedules may name a class per step, validated like a
  workflow step.
* **Issue runs.** The planner (`issueruns.PLANNER_PROFILE`, `claude-code`)
  runs `RunCreate.model_class`, which defaults to `opus`. The compiled review
  and fix steps run `opus`.

---

## 5. Frozen contract: change requests

These are requests, not changes. They are lettered A to E here, in the shape
of [contract-change-requests.md](../contract-change-requests.md); the operator
gives them its next numbers (85 to 89 at this reading) once the owner
approves. Request E's format bump has the rollout of §4.3. None asks for new
profiles; the first version's request B (`claude-code-sonnet`,
`claude-code-haiku`) is withdrawn.

### Request A. `profiles.py`: the `MODEL_CLASSES` vocabulary

**Status:** proposed 2026-10-17 by lane MODEL-DESIGN-B, part of #1435.

#### Why

A step picks its model by a name that must have one spelling across
swarm-api's validation, the signed spec, the scheduler's dispatch, the
worker's check, the API, the console and the plugin. That is the reason the
`resource_class` vocabulary is frozen, and it applies equally here. The
vocabulary is what a caller is promised. The IDs behind it are Terraform's
(§4.2).

#### The change

```python
@dataclass(frozen=True)
class ModelClass:
    """A model family a step may name (contract request A, #1435).

    The ID it stands for is Terraform's (`runner_models`, keyed by class),
    held to the class by `admits` at the scheduler's and swarm-api's start.
    A caller names the class, never the ID (invariant 10)."""

    name: str
    #: The family word in a model ID: claude-<family>-<major>[-<minor>].
    family: str
    #: The oldest generation a pin of this class may name.
    min_major: int

    def admits(self, model_id: str) -> bool:
        m = re.fullmatch(
            rf"claude-{re.escape(self.family)}-(\d+)(?:-\d+)?(?:-\d{{8}})?", model_id
        )
        return m is not None and int(m.group(1)) >= self.min_major


#: The classes a step may name. `opus` is the default of every classed
#: profile: coding and implementation. `fable` is for planning ("Fable 5+"),
#: `sonnet` and `haiku` for mechanical steps. Owner decision 2026-10-17.
MODEL_CLASSES: dict[str, ModelClass] = {
    "opus": ModelClass("opus", "opus", 5),
    "fable": ModelClass("fable", "fable", 5),
    "sonnet": ModelClass("sonnet", "sonnet", 5),
    "haiku": ModelClass("haiku", "haiku", 5),
}
```

#### Rollback

Delete the dict and the class. Requests B, C and E depend on it and go with
it.

#### What it would break

Nothing that runs: it is a new name. The red test asserts that
`MODEL_CLASSES["fable"].admits("claude-fable-5-1")` is true;
`admits("claude-opus-5-5")` is false; `MODEL_CLASSES["sonnet"].admits("claude-sonnet-4-6")`
is false (below the floor); `admits("claude-haiku-5-5-20261001")` on `haiku`
is true; and `admits("claude-haiku-5-5[1m]")` is false.

#### Invariants

**10** is the reason: a closed vocabulary of names, not a model ID. 1 to 9
are untouched.

### Request B. `profiles.py`: `RunnerProfile.model_classes`, `default_model_class`, and `step_model_class`

**Status:** proposed 2026-10-17 by lane MODEL-DESIGN-B, part of #1435.

#### Why

Each profile must say which classes it accepts and which one a step gets when
it names none. That way swarm-api, the scheduler and the worker resolve a
step's class by one rule rather than three. This is the owner's Q3: every
claude-code-family profile accepts all four classes.

#### The change

On `RunnerProfile`, after `secret_slots`:

```python
    #: Contract request B (#1435): the MODEL_CLASSES names a step on this
    #: profile may ask for, and the one it runs when it asks for none. Both
    #: empty on a profile that runs no Claude model. The IDs are
    #: `runner_models` in terraform/infra/locals.tf.
    model_classes: tuple[str, ...] = ()
    default_model_class: str | None = None
```

`__post_init__` refuses: an unknown name; a default outside `model_classes`;
classes without a default, or a default without classes; and classes on a
`worker_action` profile or a non-`anthropic` provider. `claude-code`,
`claude-code-browser` and `indexer` set
`model_classes=("opus", "fable", "sonnet", "haiku"), default_model_class="opus"`.
`claude-code-review` (retired) sets `("opus",)` and `"opus"`.

And one function, the single statement of the resolution rule:

```python
def step_model_class(profile: RunnerProfile, requested: str | None) -> str | None:
    """The class a step on `profile` runs: `requested` if the profile accepts
    it, the profile's default if None, None for a profile with no classes.
    Raises ValueError naming the accepted classes otherwise."""
```

#### Rollback

Delete the two fields and the function. Request C's fields would have no rule
to resolve against, so C goes with it.

#### What it would break

Nothing that runs: two fields with defaults. The catalogue mirror
(`runner_profiles` in `locals.tf`) and `catalogue.tftest.hcl` must gain both
fields in the same change. The red tests: a `merge`-like profile with classes
raises; a default outside the list raises; every profile whose `runner_argv`
names `agent_worker.runners.claude_code` and is available has all four
classes; `step_model_class` returns `opus` for None on `claude-code`, returns
`haiku` for `haiku`, and raises for `haiku` on `claude-code-review` and for
anything on `codex`.

#### Invariants

**10**: the classes are catalogue data, and the request field names one of
them. **6, 7**: the profiles' backend, Spot strategy and resources are
untouched. 1 to 5, 8 and 9 are untouched.

### Request C. `models.py`: `Task.model_class` and `WorkflowStep.model_class`

**Status:** proposed 2026-10-17 by lane MODEL-DESIGN-B, part of #1435.

#### Why

The class a step asked for is a fact about the task. It must survive retries,
be signed, and be read by the scheduler at dispatch and by the worker at start.
`Task.model` cannot carry it: that is an ID, a record of a pin at submission
(§4.4).

#### The change

```python
# Task, after `model`:
    #: Contract request C (#1435): the MODEL_CLASSES name this step asked
    #: for. None: the profile's default_model_class. Written by swarm-api at
    #: submission, only when it differs from that default; signed at spec
    #: format 4 (request E). Resolved by profiles.step_model_class.
    model_class: str | None = None

# WorkflowStep, after `resource_class`:
    model_class: str | None = None
```

#### Rollback

Delete both fields. A stored value is then ignored, and every step runs its
profile's default. Request E's format 4 must go first, or a format-4 task
would fail verification on a worker whose `Task` lacked the field.

#### What it would break

Nothing that decodes today: two optional fields with defaults.
`codec.task_from_dict` must read `model_class` back, so that a task swarm-api
decodes and writes again keeps its signed field (request 54's precedent).
The red test: a task with `model_class="sonnet"` round-trips through the
codec, and a workflow step's class reaches its task.

#### Invariants

**1 to 3**: a field, not a pool, so admission is unchanged. **5**: signed,
and fixed across attempts. **8**: a checkpoint never crosses a class.
**10**: a name from the frozen vocabulary.

### Request D. `models.py`: `Attempt.model`

**Status:** proposed 2026-10-17 by lane MODEL-DESIGN-B, part of #1435
(carried over from the first version's request C, unchanged).

#### Why

"What did this attempt run" must be a fact about the attempt. Neither
`runner_profile` nor `model_class` answers it once a pin changes.
`control.record_spend` writes only fields the frozen `Attempt` declares, so
the worker cannot add it alone.

#### The change

```python
    #: Contract request D (#1435): the model ID this attempt's agent was
    #: started with (MODEL), written by the worker at attempt start after its
    #: generation check. None: no model (mock, generic, browser, worker
    #: actions) or written before this field existed.
    model: str | None = None
```

#### Rollback

Delete the field. Readers fall back to the runner summary's `usage.models`.

#### What it would break

Nothing: an optional field with a default. The red test: `attempt_from_dict`
round-trips it through `attempt_to_api`.

#### Invariants

**5**: written after the fencing check, so a stale worker never writes it. 1
to 4 and 6 to 10 are untouched.

### Request E. `specsign.py`: format 4 covers `model_class`

**Status:** proposed 2026-10-17 by lane MODEL-DESIGN-B, part of #1435.

#### Why

The worker acts on the class (§4.5), and the scheduler dispatches by it. A
field the worker acts on must be inside the signature
(`tests/unit/common/test_specsign_covers.py`, contract request 34's rule).
Without it, anyone who can write the tenant's task document could move a step
to another class. The step would run on a model nobody submitted, under a
signature that still verifies.

#### The change

```python
#: 4 is contract request E (#1435): format 3's fields plus `model_class`, so
#: a task whose model class was rewritten fails its worker's check.
SPEC_FORMAT = 4
SPEC_FORMATS = (1, 2, 3, 4)
_FORMAT_4_FIELDS = ("model_class",)
```

`signing_format` returns 4 when `model_class` is set, and otherwise falls
through to its existing rule. `canonical_step_spec` refuses
(`SpecNotCanonical`) a document at a format below 4 that sets `model_class`,
and includes the field in the projection at format 4.

#### Rollback

Stop writing format 4 (the gate of §4.3 off), then remove the format once no
stored task carries it. A worker that loses format 4 while such a task is
queued refuses that task, as `unknown_format`.

#### What it would break

Nothing for a task without a class: its format and bytes are unchanged.
`test_specsign.py` already pins the SHA-256 of formats 1 to 3 as computed
before; the same pins must hold after the change. A worker built before
format 4 refuses a format-4 task, which is why §4.3 retires those workers
before the gate opens. The red tests: a format-4 document verifies; flipping
its `model_class` fails; a format-3 document with `model_class` added is
`SpecNotCanonical`; and a task without a class signs at the format it signs at
today.

#### Invariants

**5**: the class is fixed with the attempt's other signed fields. **10**: the
signature is what makes the caller's named class the only one that can run.
1 to 4 and 6 to 9 are untouched.

---

## 6. Build plan

Lanes are split by territory: **no file is in two lanes**. Every lane pushes
its tests alone to the PR branch first and its fix to `<branch>-fix`, and runs
`scripts/changed-guards.sh` and `scripts/publish-scan.sh` before finishing.

| order | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **MODEL-CONTRACT** (owner applies) | `apps/common/swarm_common/profiles.py`, `models.py`, `specsign.py`; `tests/unit/common/test_specsign.py`, `tests/unit/common/test_model_classes.py` (new); `docs/contract-change-requests.md`; `terraform/infra/locals.tf` (mirror fields, `runner_models` by class, `WORKER_CLASS_MODELS` on scheduler and swarm-api, the derived `WORKER_MODELS`, the Jobs' default-class `MODEL`, `MODEL_CLASSES_ENABLED` from the variable); `terraform/infra/variables.tf` (`model_classes_enabled`, default `false`); `tests/terraform/catalogue.tftest.hcl`, `tests/terraform/runner_model.tftest.hcl`; `scripts/lib/check-contract-parity.sh` | requests A to E's red tests (§5); the tftest finds four class keys whose IDs carry their family word, and a `WORKER_MODELS` equal to today's; the parity script fails on a class name missing from `runner_models`. Signing is unaffected, because nothing sets `model_class` yet |
| 1 | **MODEL-SCHED** | `apps/scheduler/scheduler/settings.py`, `dispatch.py`; `tests/unit/mcp/test_model_flag_is_attribution_only.py` (the worker-env half inverted, §4.5); `tests/unit/scheduler/test_step_model.py` (new) | an ID of the wrong class, a missing class and an extra key each refuse at startup; GKE and Cloud Run renders of a `haiku` step carry the haiku pin, and of a classless step carry the opus pin; every "cannot reach `worker_env`" case in §4.5 |
| 1 | **MODEL-WORKER** | `apps/agent-worker/agent_worker/specverify.py`, `lifecycle.py`, `control.py`; `tests/unit/worker/test_model_check.py` (new) | a format-4 task verifies; a `sonnet` task given the opus pin ends `model_class_mismatch` before the CLI starts; a classed profile with no `MODEL` ends `model_not_pinned`; an unsigned legacy task with a class written onto it is refused; the attempt writes `model` with `started_at`, and a stale generation writes none; a stream reporting another model logs `model_mismatch` |
| 2 | **MODEL-API** | `apps/swarm-api/swarm_api/schemas.py`, `validation.py`, `service.py`, `children.py`, `codec.py`, `settings.py`, `routes/platform.py`, `routes/workflows.py`, `schedtypes/custom_prompt.py`; `apps/agent-worker/agent_worker/children.py` (`CHILD_FIELDS`, crossing into the worker because `tests/unit/worker/test_child_tasks_worker.py` holds it equal to the route's `ChildSpec`, so the two change in one commit); `tests/unit/worker/test_child_tasks_worker.py`; `tests/unit/control_plane/test_input_model_is_refused.py`, `tests/unit/control_plane/test_model_class_submission.py` (new) | each surface accepts the four classes on `claude-code` and refuses `unknown_model_class` and `model_class_not_accepted`; `model_class_not_yet_available` while the gate is closed; the default is not stored and signs at the old format; a `fable` step signs at format 4; `TaskCreate.model` is filled from the pin, another class's pin is refused naming that class, and any other value is refused; `input.model` is refused on all three classed profiles; `/v1/runtimes` serves the classes and models; `attempt_to_api` serves `model`; the child route accepts `model_class` and `CHILD_FIELDS` has it, equal to `ChildSpec` |
| 2 | **MODEL-RUNS** | `apps/swarm-api/swarm_api/issueruns.py`, `routes/runs.py`; `tests/unit/control_plane/test_run_model_class.py` (new) | `RunCreate.model_class` reaches the planner task; a plan step's class (if R1 (a)) is inside `plan_digest` and reaches its step; review and fix run `opus` |
| 2 | **MODEL-LEDGER** | `apps/swarm-api/swarm_api/outcomes.py`; `tests/unit/control_plane/test_outcomes_model.py` (new) | the `model` group separates two models and lists "not recorded"; the `model_class` group counts a defaulted step as `opus` |
| 3 | **MODEL-UI** | `apps/swarm-ui/src/Runtimes.tsx`, `RunnerPicker.tsx`, `SubmitWorkflow.tsx`, `AgentDetail.tsx`, `AttemptTimeline.tsx`, `Ledger.tsx`, `types.ts`, `api.ts`; their vitest files | the classes column renders the chips and `—`; the select offers only accepted classes and is hidden for `codex`; a mismatch shows both models |
| 3 | **MODEL-PLUGIN** | `apps/swarm-mcp/swarm_mcp/profiles.py`, `client.py`, `server.py`, `cli.py`; `plugin/skills/delegate/SKILL.md`, `plugin/skills/sc/SKILL.md`; `tests/unit/mcp/test_model_class_bridge.py` (new) | `swarm_profiles` lists the classes and models, from the platform and from the bundled copy; the bridge sends `model_class` on a task, a step and a run; `--model-class` exists, and `--model`'s help still says attribution and "not select" |
| 4 | **MODEL-ENABLE** | `terraform/environments/*/*.tfvars` (`model_classes_enabled = true`) | the tftest plan shows `MODEL_CLASSES_ENABLED=true` on swarm-api |
| 5 | **MODEL-CANARY** | the acceptance group that dispatches claude-code (`accept.yml`'s smoke) and its script under `scripts/` | four steps, one per class: each attempt records its class's pin, and `usage.models` agrees. Red until the CLI accepts every ID |
| 5 | **MODEL-DOCS** | `docs/agent-output.md`, `docs/workflows.md`, `docs/quota-management.md` (one paragraph: windows bind every class, Q5), this file | `test_docs_describe_what_was_built` |

**MODEL-ENABLE was dropped** by the owner's answer to R4 (2026-10-17: no
flag; lane order alone). No `model_classes_enabled` variable exists, so its
row had nothing to flip. MODEL-ACCOUNTS, added by the answer to R5, is not a
row here; §8 records it with the others.

**Order.** 0 first. Then the two lanes at 1 in parallel. They must deploy
before anything writes format 4, and since nothing writes it until the gate
opens, they may share a release. Then the three lanes at 2 in parallel, and
the two at 3. **MODEL-ENABLE is dispatched only after the release carrying
MODEL-SCHED and MODEL-WORKER has applied in dev** (§4.3, step 3). It is the
lane whose merge makes the classes available (Q8). Then MODEL-CANARY and
MODEL-DOCS. Removing the derived `WORKER_MODELS` (§4.2) edits `locals.tf`,
which MODEL-CONTRACT holds in this plan, so it is a follow-up lane, dispatched
once a release carrying MODEL-SCHED has applied, and not a row here.

Of the reviews CLAUDE.md requires, the credentials and tenant-isolation one
applies to MODEL-WORKER (it changes what a worker refuses before reading a
credential) and to MODEL-SCHED (it changes the worker environment).

---

## 7. Owner questions this revision raises

Q1 to Q9 were answered on 2026-10-17 and are folded in above. These are new,
and each is also in this lane's `questions.json`. The recommended option is
listed first.

**R1. On an issue run, who picks each plan step's class?**
(a) **`RunCreate.model_class` sets the planner's class. The planner may write
a `model_class` on each plan step. It is inside `plan_digest`, so the person
approving the plan sees and approves it, and a step without one runs `opus`**
(recommended: the planner knows which steps are mechanical, and the approval
already covers what runs);
(b) one `RunCreate.model_class` for the planner and every step;
(c) the planner and the steps get separate run-level fields
(`planner_model_class`, `step_model_class`), and plan steps name none.

**R2. Should prices be pulled automatically, and how?** (§4.10)
(a) **Both: a scheduled read of the pricing documentation opens a reviewable,
dated PR to `_LIST_PRICES`, and a scheduled derivation of observed per-token
rates from completed attempts checks it and opens an issue on disagreement**
(recommended: the PR keeps a reviewer and a date on every figure, and the
measurement catches a page that was misread);
(b) only the documentation read, feeding a PR;
(c) only the observed-rate derivation, as a report;
(d) neither: figures stay manual.

**R3. When a child task names no class, what does it run?**
(a) **Its own profile's default (`opus`)** (recommended: a child is a new
step, and inheriting would let a Haiku parent silently start Haiku children
for deep work);
(b) the parent's class.

**R4. Is the `model_classes_enabled` gate acceptable as how the classes become
available?** (§4.3)
(a) **Yes: a Terraform flag, default off, flipped by MODEL-ENABLE after the
worker and scheduler release has applied. "Available on merge" (Q8) means on
MODEL-ENABLE's merge** (recommended: the order holds even if lanes share a
release);
(b) no flag: lane order alone, with the API lane dispatched after the worker
release;
(c) swarm-api reads the deployed worker image's supported formats at startup
and opens itself.

**R5. What if a pool account's plan cannot serve a class (for example,
Fable)?** Not measured. **Decided (b), owner, 2026-10-17 on PR 1509; built by
MODEL-ACCOUNTS (§4.7).**
(a) **The attempt ends as the CLI reports it (`runner_error`), the canary
catches it, and no account filter is added until it is seen** (recommended:
nothing is known to need one);
(b) accounts gain a list of served classes and the broker filters on it (a
broker change, and the CLAUDE.md review);
(c) classes other than `opus` run on metered keys only.

**R6. A class's pin changes while a step is queued or between its attempts.
Which model runs?**
(a) **The class's current pin at dispatch. `Task.model` keeps the pin at
submission as a record, and `Attempt.model` records what ran** (recommended:
the class is the promise; requiring the submission's ID would fail every
queued step at each release, and keeping old IDs alive would need a pin per
task);
(b) the submission's `Task.model`, provided it is still of the class. This
makes a stored task field select the model, and the brake of §4.5 would then
have to admit it.

---

## 8. What was built

Read on 2026-10-19 from `main` at `6c38e9d`, by MODEL-DOCS. Each entry names
the code that holds the lane's rule, and the test that holds it there.

* **MODEL-CONTRACT (order 0, contract requests 100 to 104).** `ModelClass` and
  `MODEL_CLASSES` (`apps/common/swarm_common/profiles.py::MODEL_CLASSES`),
  `RunnerProfile.model_classes` and `default_model_class`, and the one
  resolution rule `apps/common/swarm_common/profiles.py::step_model_class`;
  `Task.model_class`, `WorkflowStep.model_class` and `Attempt.model`; and spec
  format 5 for `model_class` (request 104, numbered 5 because request 96 had
  taken 4). `runner_models` in `terraform/infra/locals.tf` is keyed by class,
  becomes `WORKER_CLASS_MODELS` on the scheduler and swarm-api, and is held
  by `tests/terraform/runner_model.tftest.hcl` and
  `scripts/lib/check-contract-parity.sh`. Tests:
  `tests/unit/common/test_model_classes.py`.
* **MODEL-SCHED (order 1).** `apps/scheduler/scheduler/settings.py::check_worker_class_models`
  refuses at start a map of the wrong keys or of an ID of another class;
  `apps/scheduler/scheduler/dispatch.py::step_model` resolves each step's
  pin into `worker_env`, the GKE pod's environment and the Cloud Run
  per-execution override, and refuses a class with no pin as
  `model_not_pinned`. Tests: `tests/unit/scheduler/test_step_model.py`, and
  the inverted worker-env half of
  `tests/unit/mcp/test_model_flag_is_attribution_only.py`.
* **MODEL-WORKER (order 1).** `apps/agent-worker/agent_worker/specverify.py::check_model`
  holds `MODEL` to the signed class before the CLI starts (`model_not_pinned`,
  `model_class_mismatch`); the attempt's first write carries `model`
  (`apps/agent-worker/agent_worker/control.py::ControlPlane.record_attempt_start`);
  a stream reporting another model logs `model_mismatch`. Tests:
  `tests/unit/worker/test_model_check.py`.
* **MODEL-API (order 2).** `model_class` on `TaskCreate`, `WorkflowStepCreate`
  and the child-task route, checked by
  `apps/swarm-api/swarm_api/validation.py::validate_model_class`
  (`unknown_model_class`, `model_class_not_accepted`) and
  `apps/swarm-api/swarm_api/validation.py::check_model_class_available`
  (`model_class_not_yet_available`, a class with no pin in
  `WORKER_CLASS_MODELS`: there is no flag, R4). `Task.model` is filled from
  the pin by `apps/swarm-api/swarm_api/validation.py::resolve_task_model`;
  its `model_is_chosen_by_model_class` refusal ships report-only behind
  `REFUSAL_MODEL_IS_CHOSEN_BY_MODEL_CLASS` (`refusals.SWITCHES`), because
  `swarm dispatch --model` sent free-text attribution before classes existed.
  `/v1/runtimes` serves `model_classes`, `default_model_class` and `models`.
  Tests: `tests/unit/control_plane/test_model_class_submission.py`.
* **MODEL-RUNS (order 2).** `RunCreate.model_class` is the planner's class
  (`apps/swarm-api/swarm_api/issueruns.py::refuse_planner_model_class`); a
  plan step's class is inside `plan_digest` (R1 (a)), and review and fix run
  `opus`. Tests: `tests/unit/control_plane/test_run_model_class.py`.
* **MODEL-LEDGER (order 2).** The outcome ledger's `model` and `model_class`
  groups (`apps/swarm-api/swarm_api/outcomes.py::GROUPS`). Tests:
  `tests/unit/control_plane/test_outcomes_model.py`.
* **MODEL-UI (order 3).** The Runtimes screen's classes column, the Submit
  forms' class select, the attempt's model beside the stream's, and the
  Ledger's groups, with vitest files `runtimes.modelclasses.test.tsx`,
  `submit.modelclass.test.tsx`, `submit.task.modelclass.test.tsx`,
  `attempt.model.test.tsx` and `ledger.modelgroups.test.tsx`.
* **MODEL-PLUGIN (order 3).** `swarm_profiles` lists the classes and models,
  the bridge sends `model_class` on a task, a step and a run, and `swarm
  dispatch --model-class` exists. Tests:
  `tests/unit/mcp/test_model_class_bridge.py`.
* **MODEL-ENABLE (order 4).** Dropped, not built: the owner's answer to R4
  removed the flag it would have flipped (§6).
* **MODEL-CANARY (order 5).** One step per class in the claude-code
  acceptance group (`scripts/acceptance/groups/claude-code.sh`), each judged
  on `Attempt.model` against `/v1/runtimes` and on `usage.models`. Tests:
  `tests/unit/scripts/test_model_class_canary.py`.
* **MODEL-ACCOUNTS (R5 (b)).** `accounts/{id}.model_classes`, the one rule
  `apps/quota-broker/quota_broker/accounts.py::Account.serves_class`, the
  broker's and swarm-api's `PUT /v1/accounts/{id}/model-classes`, and
  admission's `no_account_serves_class` park before any lease. Tests:
  `tests/unit/control_plane/test_account_model_classes.py`.
* **MODEL-DOCS (order 5).** This section, the amendments to §4.3 and §6,
  [agent-output.md](../agent-output.md), the `model_class` section of
  [workflows.md](../workflows.md#choosing-a-steps-model-model_class), and the
  quota paragraph of
  [quota-management.md](../quota-management.md#4-park-reasons). Each is held
  to the code by `tests/unit/scripts/test_docs_describe_what_was_built.py`.

**Still open.** The scheduler still receives the derived profile-keyed
`WORKER_MODELS`, kept for one release so a scheduler rolled back alone finds
its map (§4.2). Removing it is the follow-up lane §6 names, to be dispatched
once a release carrying MODEL-SCHED has applied. Prices for
`claude-sonnet-5-5` and `claude-haiku-5-5` are not in `_LIST_PRICES` until
an operator supplies dated figures (§4.10; R2 is #1481).
