# Model per step: a workflow step picks a cheaper model by naming a profile

**Status: designed 2026-10-17 (lane MODEL-STEP-DESIGN, part of #1435). Nothing
is built.** The owner reads this and answers §7 before any build lane starts.
The frozen-contract half is written as requests in §5. They are requests, not
changes: the operator files them in
[contract-change-requests.md](../contract-change-requests.md) once approved
(CLAUDE.md rule 1).

Every file and symbol cited below was read on 2026-10-17 at `35f2a61`. Lines
move, so search for the symbol named beside each one.

What the design settles, in one line each:

* **A caller still names a profile, and the profile still fixes the model.**
  Two new catalogue entries, `claude-code-sonnet` and `claude-code-haiku`,
  are `claude-code` in every field but their name and their model class
  (§3, §4.1). Nothing a caller sends can name a model (invariant 10).
* **The frozen catalogue pins the model CLASS (`opus`, `sonnet`, `haiku`);
  Terraform pins the model ID** (`claude-sonnet-5-5`), and the scheduler
  refuses to start if an ID does not belong to its profile's class (§4.2).
  The class is the promise a caller relies on; the ID changes with every model
  release, and that should not need a contract request each time.
* **An attempt records the model it ran** (`Attempt.model`), so cost by model
  is a query, not an inference from a profile name whose meaning can change
  (§4.3).
* **An account's model-scoped quota window binds only that model's
  attempts**, so a Sonnet step can still run on an account whose weekly Opus
  allowance is spent (§4.5).

---

## 1. The problem, as measured

### 1.1 What is true today

Since #226 (owner decision of 2026-09-26) the model a claude-code agent runs
is fixed per runner profile, and the profile is the only thing a caller picks:

* **One map states every model**: `runner_models` in
  `terraform/infra/locals.tf`. It has four entries, and all four are
  `claude-opus-5-5`: `claude-code`, `claude-code-review` (retired),
  `indexer` and `claude-code-browser`. No profile the platform can dispatch
  runs anything but Opus.
* That map becomes `MODEL` on each Cloud Run Job this root creates, and
  `WORKER_MODELS` on the scheduler, which sets `MODEL` on every GKE pod it
  renders (`apps/scheduler/scheduler/dispatch.py::profile_model`, used by
  `GkeJobDispatcher` beside the comment "THE PROFILE'S MODEL, as a Cloud Run
  Job carries it").
* The worker reads `MODEL` into `WorkerConfig.model`
  (`apps/agent-worker/agent_worker/config.py`), hands it to the runner child
  (`apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`), and the runner
  passes it as `--model`
  (`apps/agent-worker/agent_worker/runners/cliagent.py`, the block under
  "THE MODEL IS THE JOB'S, NEVER THE CALLER'S").
* A caller cannot change it, by design. `input.model` is refused at the API
  because `claude-code` declares no such input (`RunnerProfile.inputs`,
  contract request 25: "EMPTY IS A DECLARATION ... That is what closes
  `input.model` on `claude-code`"), the worker drops a stored one
  (`lifecycle.py`, "dropped the caller's own input.model"), and
  `tests/unit/control_plane/test_input_model_is_refused.py` holds both.
  `TaskCreate.model` is accepted but is attribution only, and
  `tests/unit/mcp/test_model_flag_is_attribution_only.py` is a deliberate
  brake on wiring it through.
* **The catalogue knows nothing about models.** `RunnerProfile` has no model
  field, and `runner_models`' own comment says so: "the catalogue has no
  model, and giving it one is a contract request." So `GET /v1/runtimes`
  (`apps/swarm-api/swarm_api/routes/platform.py::runtimes`) and
  `swarm_profiles` cannot say which model a profile runs.

### 1.2 What it costs

The issue (#1435, opened 2026-10-16) states the cost as a fact of the shape
rather than a measured bill: every claude-code step runs Opus, "including steps
that only reformat text". It quotes no spend figure, and this lane had no
Firestore access to read one, so this document does not invent one. What can be
stated:

* **List price.** `claude-opus-5-5` is $4 input / $20 output / $0.20 cache
  read per million tokens (`_LIST_PRICES` in
  `apps/agent-worker/agent_worker/lifecycle.py`). Neither `claude-sonnet-5-5`
  nor `claude-haiku-5-5` is in that table or in any price source this lane
  read. For scale only: the previous generation's Sonnet (`claude-sonnet-5`,
  $2 / $10) listed at half of Opus 5.5, and Haiku 4.5 ($1 / $5) at a quarter.
  If the 5.5 tier holds that ratio, a mechanical step on Sonnet costs half and
  on Haiku a quarter, per token. That is an assumption to be replaced with the
  dated figures (§7 Q9), not a measurement.
* **Subscription accounts.** Most claude-code attempts run on pool accounts
  (`apps/quota-broker/quota_broker/accounts.py`), where the cost is not
  dollars but the account's quota windows (`five_hour`, `seven_day`, and
  model-scoped ones such as `seven_day_opus`, `usage.py`'s docstring). Today
  `Account.headroom` takes the minimum over **every** window
  (`_binding_remaining`), so an account whose weekly Opus allowance is spent
  is refused to every attempt, including one that would not use Opus. How much
  of a shared window a Sonnet turn consumes relative to an Opus turn is the
  provider's accounting and was not measured here.
* **The platform's own mechanical steps run Opus too.** The scheduled types
  `epic_triage`, `docs_drift` and `observer`
  (`apps/swarm-api/swarm_api/schedtypes/`) each pin `PROFILE = "claude-code"`.
  Whether any of them should move is a product decision (§7 Q7), not part of
  this design.

### 1.3 What "done" lets someone do

A person writing a workflow names `claude-code-haiku` on the step that
classifies feedback, `claude-code-sonnet` on the step that summarises a source,
and `claude-code` on the steps that reason, and:

* each step's attempt runs, and records, the model its profile pins;
* `swarm_profiles`, `GET /v1/runtimes` and the console's Runtimes screen say
  which model class and which model each profile runs;
* the outcome ledger reports cost per model, from the attempts;
* a Sonnet step is not parked because an account's Opus-only window is spent;
* `input.model` is still refused, and nothing a caller sends can choose a
  model, an image, a command, resources or a backend.

Today none of that is possible: there is one model, and no way to name another.

---

## 2. Constraints that bind the design

### 2.1 The invariants it touches

**1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Preserved. A
tier profile is an ordinary profile: a QUEUED, PARKED or READY step on it holds
no lease and no pod. A step parked for want of an account (`no_account_available`)
is parked before any pod exists, exactly as today; §4.5 only changes *which*
accounts are eligible, never when a park happens.

**2. All-or-nothing reservation in one transaction.** Preserved, unchanged.
`pool_names_for` (`apps/common/swarm_common/models.py`) is not touched: a
`claude-code-sonnet` step acquires `global`, `tenant:<t>`, `resource:standard`,
`runner:claude-code-sonnet`, `backend:GKE_AUTOPILOT`, `provider:anthropic` and
`provider:anthropic:tenant:<t>` in the one admission transaction, as a
`claude-code` step acquires its seven. The only new thing is a pool document,
`runner:claude-code-sonnet`, which Terraform materialises from the catalogue
mirror (`pool_runner_profiles` in `terraform/infra/locals.tf`) so it is never a
pool "nobody chose" (that file's own warning). Invariant 3 follows: concurrency
still counts from LEASED.

**5. Fencing.** Preserved. The model is decided by the profile before the
lease exists and is fixed on the pod by the dispatcher, so there is nothing for
a stale generation to act on. A stale worker still exits before running the
agent (`lifecycle.py`, unchanged). The one new write, `Attempt.model` (§4.3),
is made by the worker *after* its generation check, in the same write as
`started_at`, so a fenced-out worker never records a model for an attempt it
did not run.

**8. Checkpointing.** Preserved, with one consequence stated. A tier profile
inherits `checkpoint_interval_seconds` (120) and `supports_checkpoint` from
`claude-code`. A retry of a `claude-code-sonnet` attempt restores its
checkpoint and resumes on the same profile, so the same model. **A checkpoint
never crosses a model**: a task's profile does not change between attempts, and
nothing in this design lets it. If it ever did, a Claude Code session resumed
under another `--model` would continue a conversation another model began,
which is worth a decision of its own and is not offered here.

**9. Tenant isolation.** Preserved. A tier profile uses the tenant's own GSA
and KSA, its own `-anthropic` credential or its own (or explicitly lent) pool
account, its own GCS prefix and its own namespace, as `claude-code` does. The
broker's choice still goes through `accounts_serving` and `Account.may_serve`
(`quota_broker/accounts.py`); §4.5 narrows which windows count, never which
tenants an account serves. The new `AccountAssign.model_class` field can only
change which windows are compared, never widen the set of accounts.

**10. Callers pick a runner_profile by name.** This is the design's point, and
it is preserved in its strictest reading: the caller's whole vocabulary stays
the profile name. No request field is added. `input.model` stays refused on
every tier profile (they declare `claude-code`'s `_CLI_AGENT_INPUTS`, which has
no `model`). The class lives in the frozen catalogue and the ID in Terraform;
neither is reachable from a request. `TaskCreate.model` is the one place a
caller can still write a model name, and §4.4 closes the gap between what it
says and what runs (§7 Q4).

Untouched: **4** (no new wait inside a worker; an unavailable account still
parks), **6** (both new profiles are on-demand GKE Autopilot; `SpotStrategy`
stays `ON_DEMAND_ONLY`, which `RunnerProfile.__post_init__` enforces), **7**
(`resource_class="standard"`, `requests == limits`, as `claude-code`).

### 2.2 What it needs from `apps/common/swarm_common/`

Three requests, all in §5:

* `profiles.py`: a `ModelClass` vocabulary and `RunnerProfile.model_class`
  (request A);
* `profiles.py`: the two catalogue entries (request B);
* `models.py`: `Attempt.model` (request C).

Not requested: `pool_names_for` (no `model:` pool, §3 design B explains why one
is not needed), `Task` (its `model` field already exists), `states.py` (no new
park reason: an unavailable account is the existing `no_account_available`
park), `specsign.py` (the signed spec already carries `runner_profile`, which
now determines the model, so the signature already covers the choice).

### 2.3 Other constraints found while reading

* **`check_worker_models` refuses a map entry for a profile the catalogue does
  not hold** (`apps/scheduler/scheduler/settings.py::check_worker_models`). So
  the contract change must be applied and deployed before Terraform adds the
  two `runner_models` entries. That fixes the lane order in §6.
* **The pinned Claude Code CLI must accept the new IDs.** The base image's
  Dockerfile records that CLI 2.1.273 refused the then-new `claude-opus-5-5`
  as `unrecognized_model` and every claude-code task failed
  (`images/agent-runtime-base/Dockerfile`, the comment above the CLI pin). The
  same can happen to `claude-sonnet-5-5` or `claude-haiku-5-5`, so the build
  includes a canary that runs one real step per tier (§6, lane MODEL-CANARY).
* **Several tables list claude-code's family by name** and would silently not
  know a new profile: `runner_spec`'s tuple in
  `apps/agent-worker/agent_worker/runners/streams.py`, `AGENT_STREAM_FILES` in
  `apps/swarm-api/swarm_api/agent_streams.py` (a profile missing there is
  "UNKNOWN", so the console would find no transcript), and
  `DEFAULT_ALLOWED_PROFILES` in `apps/swarm-api/swarm_api/repositories.py`.
  Each must learn the new names (§4.6).
* **Provider rate limiting is per provider and tenant, not per model.** The
  broker's AIMD state is keyed `/v1/quota/{provider}/{tenant_id}`
  (`quota_broker/main.py`), so a metered-key 429 on an Opus step throttles that
  tenant's Sonnet steps too, although the provider meters models separately.
  This design does not change that (it would need a `pool_names_for` change);
  it is recorded as a known limit in §4.5.

---

## 3. Designs that could work

### Design A: one profile per model tier (recommended)

`claude-code-sonnet` and `claude-code-haiku` join the catalogue, each
`claude-code` in every field but its name and a new `model_class`. A workflow
step already names its own `runner_profile`
(`apps/swarm-api/swarm_api/schemas.py::WorkflowStepCreate`), so per-step model
choice needs no new request field at all. This is the issue's proposal, with
the catalogue carrying the class.

* **Code.** Small. Two catalogue entries and one field (contract), two
  Terraform map lines and their mirrors, the scheduler's family check, the
  worker's attempt write and account-assign field, the broker's window filter,
  the API's runtimes route and ledger group, the console's columns, and the
  name tables in §2.3. No new route, no new request field.
* **Migration.** None. Existing tasks keep `claude-code`, which keeps Opus.
  Attempts written before the change have no `model` and read as "not
  recorded".
* **Latency.** None. Same image, same backend, same start path as
  `claude-code` (GKE, ~23 s p50, contract request 53).
* **Money.** The point: a mechanical step costs the cheaper model's tokens.
  Each new profile also gets its own `runner:` pool and its own ceiling, which
  is a feature: a flood of Haiku classification steps cannot take
  `claude-code`'s 80 slots (`dev.tfvars` `pool_limits.runner_profiles`), while
  `backend:GKE_AUTOPILOT` (100) still bounds the total.
* **What it breaks.** Nothing that runs. Its cost is **multiplication**: a
  tier is per image, so a Sonnet browser agent would be a third profile
  (`claude-code-browser-sonnet`), and a Sonnet indexer a fourth. Two images ×
  three tiers is six entries where today there are three. The owner decides
  which combinations exist (§7 Q3); the recommendation is the two the issue
  asks for and no more until a step needs one.

### Design B: a per-step `model_class`, chosen by name

The caller sends `model_class: "sonnet"` on a task or a step, from a frozen
`MODEL_CLASSES` vocabulary, and each profile declares the classes it accepts,
the way `resource_class` is "a NAMED class from the frozen catalogue, and only
one no larger than the profile's own" (`WorkflowStepCreate.resource_class`).
This is still choosing by name, so invariant 10 arguably holds, but it is a
second execution knob beside the profile.

* **Code.** Larger. `Task` gains `model_class` (contract, `models.py`);
  `TaskCreate`, `WorkflowStepCreate`, `RunCreate`, the child-task route and the
  plugin bridge all accept and validate it; the signed step spec must cover it,
  which is a spec format bump (`swarm_common/specsign.py`, `_FORMAT_2_FIELDS`)
  and a verifier change in every worker; the dispatcher must set `MODEL` per
  execution. On GKE that is a per-task manifest line; on the Cloud Run
  fallback Jobs, which carry `MODEL` on the Job, it needs a per-execution
  override, which `worker_env` deliberately refuses to carry
  (`test_model_flag_is_attribution_only.py`'s brake would have to be inverted
  on purpose).
* **Migration.** A spec-format bump: workers that cannot verify format 3 must
  be gone before any API writes it.
* **Latency, money.** As A.
* **What it breaks.** Per-tier ceilings: tiers share `runner:claude-code`, so
  a Haiku flood takes Opus's slots unless `pool_names_for` grows a
  `model:<class>` pool (a frozen admission change, invariant 2's own function).
  Outcome grouping by `runner_profile` no longer separates tiers. Its gain is
  composition: one field gives every claude-code-family profile every tier
  without new entries.

### Design C: the platform chooses

No caller input at all: a cheap classifier (or a rule on the prompt) picks the
model per step. Rejected. It adds a model call before every dispatch, its
choice cannot be reviewed before the run, and a wrong guess silently runs deep
work on Haiku. The person who wrote the workflow knows which step is
mechanical; the platform does not.

### Recommendation

**A.** It keeps the caller's vocabulary to one word, needs no new request
field, no spec-format bump and no admission change, and it reuses everything
already keyed by profile: validation, per-profile pools and ceilings, the
ledger's `runner_profile` grouping, the console's Runtimes screen and the
plugin's catalogue. Its one weakness, multiplication across images, is bounded
by deciding which combinations exist (§7 Q3), and B remains the upgrade path
if that ever passes a handful: B's `model_class` vocabulary is request A below,
so A builds half of B and nothing A builds has to be undone.

---

## 4. The recommended design

### 4.1 The catalogue

`RunnerProfile` gains `model_class: ModelClass | None` (request A). It is set
on every profile whose runner is `agent_worker.runners.claude_code`:
`claude-code`, `claude-code-review`, `indexer` and `claude-code-browser` are
`OPUS`, matching today's map, and the two new entries are `SONNET` and
`HAIKU`. It is `None` on `mock`, `generic`, `browser`, `merge`,
`post-verdict` and `codex` (an OpenAI CLI, where a Claude model name would fail
every run, as `runner_models`' comment already records).

The two entries (request B), each `claude-code` but for name and class:

| field | `claude-code-sonnet` | `claude-code-haiku` |
|---|---|---|
| `image` | `agent-runtime-base` | `agent-runtime-base` |
| `resource_class` | `standard` | `standard` |
| `backend` | `GKE_AUTOPILOT` | `GKE_AUTOPILOT` |
| `runner_argv` | `python -m agent_worker.runners.claude_code` | same |
| `provider`, `secrets`, `secrets_any_of` | `anthropic`, both names, `True` | same |
| `timeout_seconds` | 7200 | 7200 |
| `inputs` | `_CLI_AGENT_INPUTS` (no `model`) | same |
| `secret_slots` | `{"files": _FILES_SLOT}` | same |
| `model_class` | `SONNET` | `HAIKU` |

Neither goes in `cloud_run_fallback_profiles`: they start on GKE, as
`claude-code-browser` did, so `WORKER_MODELS` is their only `MODEL` path.

### 4.2 Where the model ID lives, and what holds it to the class

The ID stays in `runner_models` (`terraform/infra/locals.tf`), the one place
it is stated today, with two new lines:

```hcl
runner_models = {
  "claude-code"         = "claude-opus-5-5"
  "claude-code-review"  = "claude-opus-5-5"
  "indexer"             = "claude-opus-5-5"
  "claude-code-browser" = "claude-opus-5-5"
  "claude-code-sonnet"  = "claude-sonnet-5-5"
  "claude-code-haiku"   = "claude-haiku-5-5"
}
```

**Why the ID is not in the frozen catalogue.** The opus-5 to opus-5-5 move was
an edit to this map and a release. If the catalogue held the ID, every model
release would be a contract request for every tier. The catalogue holds what a
caller is promised (this profile is the Sonnet tier) and Terraform holds which
Sonnet that is this month. §7 Q1 offers the owner the alternative.

**What stops the two drifting apart.** Three checks, each reading the
catalogue rather than restating it:

1. `scheduler.settings.check_worker_models` refuses, at startup, an entry
   whose ID does not belong to its profile's class (`ModelClass.admits(id)`,
   request A: `claude-sonnet-5-5` belongs to `SONNET`; `claude-opus-5-5` does
   not), and an entry for a profile whose `model_class` is `None`. A
   `claude-code-haiku = "claude-opus-5-5"` typo stops the scheduler instead of
   quietly billing Opus.
2. `tests/terraform/runner_model.tftest.hcl` asserts each entry's prefix
   against the mirror's `model_class`.
3. The catalogue mirror (`runner_profiles` in `locals.tf`, held field for
   field by `tests/terraform/catalogue.tftest.hcl`) gains `model_class`, and
   `scripts/lib/check-contract-parity.sh` gains a section asserting every
   profile with a class has a `runner_models` entry and no other profile has
   one.

`dispatch.profile_model` and the worker's `MODEL` path are unchanged.

### 4.3 What an attempt records

`Attempt.model` (request C): the model ID the worker was started with
(`WorkerConfig.model`), or `None` for a profile that runs no model. Written by
the worker in the same merge as `started_at`, after its fencing check
(`agent_worker/control.py::ControlPlane.record_attempt_start`). It answers "what did this
attempt run" from the platform's own pin.

The stream's own record stays where it is: `_usage_summary` already collects
the `model` of every assistant message into the runner summary's
`usage.models` (`lifecycle.py::_usage_summary`). The two are
different facts. If they disagree (the pinned CLI substituted a model), the
worker logs `model_mismatch` with both values, and the console shows both
(§4.7). That check is the issue's "an attempt on `claude-code-sonnet` records
`claude-sonnet-5-5`", made a measurement instead of an echo.

`record_spend` writes only fields the frozen `Attempt` declares (its own
docstring), which is why this is a contract request rather than a worker
change.

Firestore, `attempts/{attempt_id}`, one new field:

| field | type | written by | meaning |
|---|---|---|---|
| `model` | string or absent | worker, at attempt start | the pinned model ID; absent on attempts before this change and on profiles with no model |

### 4.4 The API

**`GET /v1/runtimes`** (`apps/swarm-api/swarm_api/routes/platform.py::runtimes`)
serves two more fields per profile:

```json
"claude-code-sonnet": {
  "name": "claude-code-sonnet",
  "available": true,
  "model_class": "sonnet",
  "model": "claude-sonnet-5-5",
  "...": "every existing field, unchanged"
}
```

`model_class` is read from the catalogue. `model` is read from
`WORKER_MODELS`, which Terraform now also sets on swarm-api (one line beside
the scheduler's in `locals.tf`), so the route reports the map the scheduler
dispatches with rather than a copy of it. swarm-api checks each entry with
`ModelClass.admits` (request A), the same frozen rule the scheduler applies,
so there is no second statement of it to drift. `null` for a profile with no
model.

**`GET /v1/tasks/{id}` and `/v1/attempts`**: each attempt carries `model`
(`codec.attempt_from_dict`, `codec.attempt_to_api`). `attempt_from_dict` must
read it explicitly: that decoder has dropped written fields before (its own
comment on the five spend fields).

**`TaskCreate.model`** (recommended, §7 Q4): swarm-api sets `Task.model` to the
profile's pinned ID at submission. A caller who sends `model` equal to it is
accepted; one who sends a different value is refused with 422
`model_is_the_profiles`, naming the profile to pick instead (for example "to
run claude-sonnet-5-5, submit runner_profile claude-code-sonnet"). Today a
caller can write `model: "claude-haiku-5-5"` on an Opus task and the record
says Haiku; with tier profiles that record would be believed. The refusal is
in `apps/swarm-api/swarm_api/validation.py`, beside
`validate_runner_profile`; the brake test `test_model_flag_is_attribution_only.py`
keeps its worker-environment half unchanged (no `MODEL` from a task ever
reaches `worker_env`) and its help-text half is updated to say "names a model
only to check it matches the profile".

**Outcome ledger** (`apps/swarm-api/swarm_api/outcomes.py`): `GROUPS` gains
`model`, grouped by `Attempt.model` over started attempts, with "not recorded"
as its own row for attempts before the change. Grouping by `runner_profile`
already separates tiers; `model` is what keeps last month's figures right
after a profile's pinned ID changes.

### 4.5 Accounts and quota windows

**The assign request carries the class.** `AccountAssign`
(`apps/quota-broker/quota_broker/main.py`) gains
`model_class: str | None = None`, one of the `ModelClass` values. The worker
sends its profile's class, read from the catalogue by `RUNNER_PROFILE`
(`agent_worker/accountlease.py::AccountBroker.assign`), never from the task.
`None` (an old worker) means today's rule.

**Windows are scoped by name.** `Account.headroom(now, model_class=...)` and
`_binding_remaining` skip a window whose name ends `_<class>` for a different
class: `seven_day_opus` binds `opus` attempts only, `seven_day_sonnet`
`sonnet` only. A window with no class suffix (`five_hour`, `seven_day`) binds
every attempt. **An unrecognised name binds every attempt**: the usage
endpoint adds windows under names nobody has seen (`nimbus_quill`, `usage.py`'s
docstring), and treating an unknown window as irrelevant would send attempts at
an account the provider is about to refuse. `choose`, `eligibility` and
`next_reset` all take the same argument, so the "none available, and when"
answer cannot disagree with the choice (the bug `assign_account`'s docstring
records).

Readings are unchanged: the broker still records every window it can parse.

**What it cannot see.** Whether a cheaper model draws on a shared window at a
lower rate is the provider's accounting; the broker reads utilisation, so it
will observe the effect without modelling it. And the AIMD throttle stays keyed
by provider and tenant (§2.3): a metered-key 429 on Opus slows that tenant's
Sonnet steps. Splitting it needs `provider:anthropic:<class>` pools, a
`pool_names_for` change this design does not ask for.

**Is the class a trust boundary?** No. The agent runs as the same identity as
its worker and could call the broker itself, as it can today. Claiming
`haiku` only changes which windows are compared; `may_serve` still decides
which accounts it may have, and the provider still enforces its own windows.

### 4.6 The worker, the scheduler and the name tables

* **Worker.** `lifecycle.py`: write `Attempt.model` at start (§4.3); log
  `model_mismatch` at the end; and **fail closed** when a profile has a
  `model_class` but the pod has no `MODEL`: the attempt ends `model_not_pinned`
  before the CLI starts, because the alternative is the CLI's own default
  model, which is how the QA task of 2026-09-26 ran `claude-sonnet-5` on an
  Opus profile (`runner_models`' comment). `accountlease.py`: send `model_class`.
  `runners/streams.py::runner_spec`: replace the four-name tuple with "the
  profile's `runner_argv` names `agent_worker.runners.claude_code`", so a
  later tier needs no edit here. `_LIST_PRICES` in `lifecycle.py` gains
  `claude-sonnet-5-5` and `claude-haiku-5-5` with the dated list prices once
  known (§7 Q9). Until then a stopped attempt on those models prices nothing,
  which the table's own rule calls honest; a completed attempt still reports
  the CLI's own `total_cost_usd`.
* **Scheduler.** `settings.py::check_worker_models`: the class check (§4.2),
  and a refusal at startup when an available classed profile has no entry.
  `dispatch.py`: unchanged.
* **swarm-api name tables.** `agent_streams.py::AGENT_STREAM_FILES`: the two
  profiles write claude-code's three files. `repositories.py`:
  `DEFAULT_ALLOWED_PROFILES` becomes every available profile whose
  `model_class` is set, read from the catalogue, so a registered repository's
  default list offers the tiers. It is "not yet a submission check" (that
  module's docstring), so this changes what is listed, not what may run.

### 4.7 The console

* **Runtimes** (`apps/swarm-ui/src/Runtimes.tsx`, types in
  `apps/swarm-ui/src/types.ts`, the loader and its fixture in `api.ts`): a
  "Model" column showing the class as a chip and the ID beside it; `—` for a
  profile with no model.
* **Agent detail** (`AgentDetail.tsx`, `AttemptTimeline.tsx`): each attempt
  shows its `model`; when the stream's `usage.models` disagrees, both are
  shown with "the CLI ran a different model from the one pinned".
* **Submit picker** (`RunnerPicker.tsx`): each profile's option carries its
  model class, from `/v1/runtimes`, so "claude-code-haiku" reads as "Claude
  Code · Haiku" and the choice is visible as a cost choice.
* **Ledger** (`Ledger.tsx`): the `model` group (§4.4).

No new section, tab or route, so the issue forms' "Where" list
(`tests/unit/scripts/test_issue_forms.py`) is unaffected.

### 4.8 The plugin and MCP

* `swarm_profiles` (`apps/swarm-mcp/swarm_mcp/profiles.py::_served_entry`, and
  the bundled copy it falls back to) passes `model_class` and `model` through,
  so the issue's first acceptance check is met by the platform's answer, not a
  copy.
* `swarm dispatch --model` (`apps/swarm-mcp/swarm_mcp/cli.py`): help text says
  the profile fixes the model and names the tier profiles; the API's 422
  (§4.4) is passed through.
* `plugin/skills/delegate/SKILL.md` and `plugin/skills/sc/SKILL.md`: one
  paragraph on choosing a tier per step (mechanical work on `-haiku` or
  `-sonnet`, reasoning on `claude-code`), replacing "asking for one is not a
  thing a caller may do" with "pick the profile that runs it".

---

## 5. Frozen contract: change requests

Requests, not changes. Numbered here A to C; the operator gives them the next
numbers in [contract-change-requests.md](../contract-change-requests.md) (85 to
87 at this reading) once the owner approves.

### Request A. `profiles.py`: `ModelClass`, and `RunnerProfile.model_class`

**Why.** The catalogue is what a caller reads to learn what a profile name
means (`GET /v1/runtimes`' docstring), and today it cannot say which model a
profile runs, so the tier profiles of request B would differ only by a name
whose meaning lives in a Terraform file. The class is what the caller is
promised; it must be one spelling across the scheduler's check, the worker's
assign request, the broker's window filter, the API and the console, which is
the reason it is frozen.

**The change.**

```python
class ModelClass(str, Enum):
    """The model family a profile's agent runs (contract request A, #1435).
    The exact model ID is Terraform's (`runner_models`); this is the promise
    a caller picks by profile name. A caller never sends it (invariant 10)."""

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"

    def admits(self, model_id: str) -> bool:
        """True when `model_id` is a model of this class: `claude-<class>-`
        followed by a version, e.g. claude-sonnet-5-5 for SONNET."""
        return re.fullmatch(rf"claude-{self.value}-[0-9][0-9a-z-]*", model_id) is not None
```

and on `RunnerProfile`, after `secret_slots`:

```python
    #: Contract request A (#1435): the model family this profile's agent
    #: runs, or None for a profile that runs no Claude model. The ID is
    #: `runner_models` in terraform/infra/locals.tf, held to this class by
    #: scheduler.settings.check_worker_models.
    model_class: ModelClass | None = None
```

`__post_init__` refuses `model_class` on a `worker_action` profile and on a
profile whose `provider` is not `anthropic`. The four existing claude-code
family entries set `model_class=ModelClass.OPUS`.

**Rollback.** Delete the enum and the field. The tier profiles of request B
would have to go with it.

**What it would break.** Nothing that runs: a new field with a default, and
four entries gaining a value that matches today's map. The catalogue mirror
(`runner_profiles` in `locals.tf`) and `catalogue.tftest.hcl` must gain the
field in the same change. Red test: a `merge`-like profile with a class
raises; `ModelClass.SONNET.admits("claude-opus-5-5")` is false and
`admits("claude-sonnet-5-5")` true; every profile with `runner_argv` naming the
claude_code runner has a class.

**Invariants.** **10** is the reason: the class is catalogue data, never a
request field. 1-9 untouched.

### Request B. `profiles.py`: `claude-code-sonnet` and `claude-code-haiku`

**Why.** Per-step model choice by name (#1435). Each is `claude-code` in every
field but `name` and `model_class` (§4.1's table), on GKE Autopilot from the
start for claude-code's start latency (contract request 53).

**The change.** Two entries in `RUNNER_PROFILES`, built with
`dataclasses.replace(RUNNER_PROFILES["claude-code"], name=..., model_class=...)`
after the dict, so they cannot drift from `claude-code` in any other field.

**Rollback.** Delete the two entries and their mirrors. A queued task naming
one is then refused at dispatch as an unknown profile; to avoid stranding
work, roll back by `available=False` with a reason first (the `codex`
precedent), and delete only when nothing names them.

**What it would break.** Nothing that exists. In the same change or the one
after: the Terraform mirror and `runner_models` (§6 lane MODEL-TF), the name
tables (§4.6), and `tests/unit/control_plane/test_internet_profiles_on_gke.py`
if it lists the GKE profiles.

**Invariants.** **1-3**: own `runner:` pool, materialised by Terraform,
reserved all-or-nothing with the other six. **6, 7**: on-demand, `standard`,
`requests == limits`. **8**: inherits the 120 s interval. **9**: tenant's own
identity and credential. **10**: picked by name; `inputs` has no `model`.

### Request C. `models.py`: `Attempt.model`

**Why.** "What did this attempt run" must be an attempt fact. Grouping cost by
`runner_profile` is right only until a profile's pinned ID changes; after
that, history is misattributed. `control.record_spend` writes only fields the
frozen `Attempt` declares, so the worker cannot add it alone.

**The change.**

```python
    #: Contract request C (#1435): the model ID this attempt's agent was
    #: started with (the Job's MODEL), written by the worker at attempt start
    #: after its generation check. None: no model (mock, generic, browser,
    #: worker actions) or written before this field existed.
    model: str | None = None
```

**Rollback.** Delete the field; readers fall back to the runner summary's
`usage.models`.

**What it would break.** Nothing: an optional field with a default. Red test:
`attempt_from_dict` round-trips it through `attempt_to_api`.

**Invariants.** **5**: written after the fencing check, so a stale worker
never writes it. 1-4 and 6-10 untouched.

---

## 6. Build plan

Lanes split by territory: no two lanes edit the same file. Every lane pushes
its tests first to the PR branch and its fix to `<branch>-fix`, and runs
`scripts/changed-guards.sh` and `scripts/publish-scan.sh` before finishing.

| order | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **MODEL-CONTRACT** (owner applies) | `apps/common/swarm_common/profiles.py`, `models.py`, `docs/contract-change-requests.md`, `terraform/infra/locals.tf` (`runner_profiles` mirror gains `model_class` and the two entries; `runner_models` gains two lines), `tests/terraform/catalogue.tftest.hcl`, `tests/terraform/runner_model.tftest.hcl`, `terraform/environments/*/*.tfvars` (`pool_limits.runner_profiles`), `scripts/lib/check-contract-parity.sh` | `ModelClass.admits`; a worker-action profile with a class raises; the two entries equal `claude-code` but for name and class; the tftest counts six `worker_models` entries and each prefix matches its class; the parity script fails on a classed profile with no `runner_models` line. The `runner_models` lines and swarm-api's `WORKER_MODELS` line land in this lane, with the catalogue, on purpose: `check_worker_models` refuses an entry for a profile the catalogue does not hold, and a classed profile with no entry would dispatch with no `MODEL` and run the CLI's default. The release's infrastructure job applies the scheduler's new image and its new `WORKER_MODELS` in one revision (`terraform/infra/main.tf`, the scheduler's `image`), so neither half is ever live alone. |
| 1 | **MODEL-SCHED** | `apps/scheduler/scheduler/settings.py`, its tests | an ID of the wrong class, an entry for a classless profile, and an available classed profile with no entry each refuse at startup |
| 1 | **MODEL-WORKER** | `apps/agent-worker/agent_worker/lifecycle.py`, `control.py`, `accountlease.py`, `runners/streams.py`, their tests | a sonnet-profile attempt writes `model = claude-sonnet-5-5` with `started_at`; a stale generation writes none; a classed profile with no `MODEL` ends `model_not_pinned` without starting the CLI; a stream reporting another model logs `model_mismatch`; the assign body carries `model_class` from the catalogue and ignores any task field; `runner_spec` returns claude-code's spec for both new profiles |
| 1 | **MODEL-BROKER** | `apps/quota-broker/quota_broker/accounts.py`, `main.py`, their tests | an account with `seven_day_opus` spent and `seven_day` at 40 % is chosen for `sonnet` and refused for `opus`; an unknown window name binds every class; `None` keeps today's choice exactly; `eligibility` agrees with `choose` for each class |
| 1 | **MODEL-API** | `apps/swarm-api/swarm_api/routes/platform.py`, `codec.py`, `validation.py`, `outcomes.py`, `agent_streams.py`, `repositories.py`, `settings.py`; their tests | `/v1/runtimes` serves `model_class` and `model` for every profile; `attempt_to_api` serves `model`; `TaskCreate.model` different from the pin is 422 `model_is_the_profiles`, equal is accepted, absent is set from the pin; `input.model` on each tier profile is still refused (extend `test_input_model_is_refused.py`); the ledger's `model` group separates two tiers and lists "not recorded" |
| 2 | **MODEL-UI** | `apps/swarm-ui/src/Runtimes.tsx`, `RunnerPicker.tsx`, `AgentDetail.tsx`, `AttemptTimeline.tsx`, `Ledger.tsx`, `types.ts`, `api.ts`, their vitest files | the Model column renders class and ID, and `—` for none; a mismatch shows both models; the picker labels each tier |
| 2 | **MODEL-PLUGIN** | `apps/swarm-mcp/swarm_mcp/profiles.py`, `cli.py`, `plugin/skills/delegate/SKILL.md`, `plugin/skills/sc/SKILL.md`, `tests/unit/mcp/test_model_flag_is_attribution_only.py` | `swarm_profiles` lists both new profiles with their models, from the platform and from the bundled copy; the help text no longer says a model cannot be chosen; `worker_env` still carries no task model |
| 3 | **MODEL-CANARY** | the acceptance group that dispatches claude-code (`accept.yml`'s smoke), its script under `scripts/` | one step per tier runs and its attempt records the tier's model and its stream's `usage.models` agrees; red until the CLI in `agent-runtime-base` accepts both IDs |
| 3 | **MODEL-DOCS** | `docs/quota-management.md`, `docs/agent-output.md`, this file | `test_docs_describe_what_was_built` |

Order: 0, then the 1s in parallel (MODEL-BROKER must deploy before
MODEL-WORKER's `model_class` field reaches a broker, because `AccountAssign`
forbids unknown fields; the release orders control-plane services before
worker images today, so this holds if both merge in the same release), then 2,
then 3. MODEL-CANARY is the gate for marking the profiles available in
production if the owner chooses §7 Q8 (b).

The review CLAUDE.md requires for credentials and tenant isolation applies to
MODEL-BROKER (it changes which accounts an attempt is offered).

---

## 7. Owner questions

Each is also in this lane's `questions.json`. The recommendation is first.

**Q1. Where does the model pin live?**
(a) **The class in the frozen catalogue, the ID in Terraform, held together by
the scheduler's startup check** (recommended: a model release stays a
Terraform edit, and a caller is still promised a tier);
(b) the ID in the catalogue (one source of truth, but every model release is a
contract request for every tier);
(c) nothing in the catalogue, Terraform only (the issue's literal reading; no
contract change, but `/v1/runtimes` can only report what swarm-api's
environment says, and nothing frozen promises a tier).

**Q2. Tier profiles or a per-step field?**
(a) **Tier profiles, `claude-code-sonnet` and `claude-code-haiku`** (design A,
recommended); (b) a per-step `model_class` field (design B: composes across
images, but a new request field, a spec-format bump and shared pools);
(c) profiles now, and B only if a fourth image-by-tier profile is ever asked
for.

**Q3. Which images get tiers?**
(a) **`agent-runtime-base` only: the two profiles the issue names**
(recommended until a step needs another); (b) also `claude-code-browser`
(Sonnet and Haiku browser agents: two more profiles); (c) also `indexer`.

**Q4. What happens to a caller's `TaskCreate.model`?**
(a) **swarm-api sets it from the profile's pin; a different value is refused
with 422 naming the profile to pick** (recommended: the record cannot claim a
model that did not run); (b) overwritten silently with the pin; (c) left as
today, attribution only (the record can then say Haiku on an Opus run).

**Q5. Do model-scoped quota windows bind only their own model?**
(a) **Yes: `seven_day_opus` binds Opus attempts only; unscoped and unknown
windows bind everyone** (recommended: a Sonnet step can use an account whose
Opus week is spent); (b) no, every window binds every attempt, as today.

**Q6. The ceiling of each new `runner:` pool?**
(a) **The same as `claude-code`'s (80 in dev), with `backend:GKE_AUTOPILOT`
still bounding the total** (recommended); (b) 20 each until a week of use is
measured; (c) another figure the owner names.

**Q7. Do the platform's own mechanical scheduled types move to a cheaper
tier?** (a) **Not in this build: each type moves by its own decision after a
side-by-side run** (recommended); (b) move `epic_triage` and `docs_drift` to
`claude-code-sonnet` in MODEL-API; (c) never.

**Q8. When do the new profiles become available?**
(a) **On merge, with MODEL-CANARY as the acceptance check** (recommended:
`available=True` from the start; a red canary opens the usual `bug` issue);
(b) `available=False` until the canary has passed once in dev; (c) Sonnet on
merge, Haiku after its canary.

**Q9. List prices for `claude-sonnet-5-5` and `claude-haiku-5-5`?** Neither is
in `_LIST_PRICES` or any source this lane read.
(a) **Added by MODEL-WORKER only with dated figures the owner or operator
supplies; until then a stopped attempt on them prices nothing** (recommended:
the table's rule is that an unreported cost is honest and a guessed one is
wrong); (b) add the previous generation's prices as placeholders (a wrong figure
in the ledger).
