# The register of mirrored values

Three outages inside 48 hours had one cause between them: a value written down
in two places with nothing asserting the copies agree.

| | what was mirrored | what it cost |
|---|---|---|
| 1 | the tenant namespace prefix — `kubernetes/render.py` said `swarm-`, `apps/scheduler/scheduler/dispatch.py` said `swarm-tenant-` | every GKE dispatch 403'd naming a permission, never a missing namespace. A test fixture restated the prefix too, so the suite agreed with the bug. [docs/gke-dispatch-403.md](gke-dispatch-403.md) |
| 2 | the RBAC subject — `kubernetes/rbac/dispatcher-rbac.yaml` named a Google service account "by its email", which is not what GKE presents for an access-token caller | the RoleBinding applied cleanly and authorised nobody |
| 3 | the GKE Job spec — `dispatch.py` builds it as a Python dict and its own comment says it "mirrors `kubernetes/worker-templates/worker-job-browser.yaml` field for field" | both omitted `SWARM_ARTIFACTS_DIR`, the runner fell back to `/artifacts`, and `readOnlyRootFilesystem` turned that into `OSError: [Errno 30]` |

None of the three was hard to compare. Nobody compared them. **Agreement between
two copies is not maintained by anything**, and each of these copies agreed for
long enough that the pair looked like one fact.

This file is the register that came out of sweeping the repository for the rest
of them. It exists so that the next person to add a second copy of a value can
see which column they are supposed to land in, and so that
`scripts/lib/check-contract-parity.sh` is not deleted as redundant by someone who
cannot see what it is holding up.

## The three possible answers

**(a) DERIVED.** One source, nothing to compare. Always preferred, and usually
possible: `swarm_common` is importable from every Python component,
`scripts/lib/common.sh` is sourced by every script, and a terraform value can be
passed into an environment rather than rebuilt from a pattern. The GKE artifacts
directory is derived this way —
`WORKER_ARTIFACTS_DIR = f"{WORKSPACE_MOUNT}/artifacts"` in `dispatch.py` is one
path, not two.

**(b) COMPARED.** Two copies are unavoidable because a boundary sits between
them — bash cannot import Python, terraform cannot import Python, a browser
cannot import Python, and a YAML manifest is read by an operator during an
incident and so has to say the thing out loud. Then the copies get an assertion,
and the assertion runs on every build.

**(c) NEITHER.** Recorded here with the reason, because an uncovered restatement
that nobody has written down is indistinguishable from one that does not exist.

## What is compared, and against what

`scripts/lib/check-contract-parity.sh` is the one place this lives. A parity
checker that existed twice would be its own punchline.

| # | value | authority | copies held to it |
|---|---|---|---|
| 1 | `SlotPool.effective_limit` | `swarm_common.models` | the jq copy in `common.sh` |
| 2 | `Tenant.secret_name()` | `swarm_common.models` | `create-secrets.sh`, `register-tenant.sh` |
| 3 | the task lifecycle | `swarm_common.states` | the shell arrays in `common.sh` |
| 4 | the tenant-id length budget | `swarm_common.identity` | `register-tenant.sh` |
| 5 | the frozen catalogue and lifecycle in TypeScript | `swarm_common` | `apps/swarm-ui/src/types.ts` |
| 6 | the tenant namespace prefix | the dispatcher, because it creates the object | every declared prefix and every namespace literal in the repository, test fixtures included |
| 7 | the worker KSA name | the dispatcher, same reason | `render.py`, both terraform `ksa_name` defaults, the `KSAS` array in `register-tenant.sh` |
| 8 | the worker GSA prefix | `swarm_common.identity._GSA_PREFIX` | terraform's tenant GSA id, the broker's env template, the API's prefix defaults, `WORKER_SA_PREFIXES` |
| 9 | the push endpoint paths | the FastAPI route decorators | `scheduler_push_path`, `reconciler_path`, `quota_broker_path`, and `health_check_path` against every app in the repository |
| 10 | the Artifact Registry repository | `swarm_common.config.Settings.artifact_registry` | both terraform variable defaults |
| 11 | the workspace mount path | `agent_worker.config.WorkerConfig.workspace_root`, because the worker is the process that writes | the dispatcher constant, terraform's env and mount-path default, and each worker template's `WORKSPACE_ROOT`, `SWARM_ARTIFACTS_DIR` and `volumeMount` |
| 12 | the tenant-id character class | `swarm_common.identity._TENANT_SAFE` | terraform's tenant-id validation regex |
| 13 | the runner inputs a caller may send | `swarm_common.profiles.RunnerProfile.inputs` (contract request 25) | every task input the operational scripts submit (`submit_task`, the workflow steps in `e2e-test.sh`, both branches of `profile_input`), and `SUGGESTED` in `apps/swarm-ui/src/Submit.tsx` |

Sections 7 to 12 were added by the sweep. Section 13 came with contract
request 25 (2026-09-25): once the API refused an input key the profile does not
declare, every input a script submits and every key the Submit form offers
became a restatement of the catalogue. Before, the scripts sent `message`,
`index` and `run_id`, which no runner read, and the form offered `model` to the
CLI profiles; each of those would have been a 422 against a deployment, found
only by running the suite there. A profile whose inputs are not declared yet
(`browser`, `generic`) is not compared, because the API bounds it by size
alone. Two properties of all of them are deliberate and should not be relaxed:

* **The authority is named, and it is the component that acts.** For the
  namespace and the KSA that is the dispatcher, because the dispatcher is what
  creates the object: whatever it spells has to exist. For the endpoint paths it
  is the route decorator, not the terraform default and not the test — see
  below.
* **A scan that finds fewer restatements than it did when it was written
  FAILS.** It reports that the copies moved, rather than passing because it can
  no longer see them. A check that silently stopped checking is worse than no
  check: it reports an agreement it never established.

### Two spellings of one rule, inside out

Section 12 is the one no plain grep would have paired. `swarm_common.identity`
holds the tenant-id rule as the **negated** class it substitutes away,
`[^a-z0-9-]+`; `terraform/modules/tenancy/variables.tf` validates the **positive**
form, `^[a-z0-9-]+$`, and its `error_message` says outright that the rule matches
the frozen slugging. Nothing compared them. Section 4 already asserts the *length*
budget the same pair shares — because the length once diverged and cost something
— and the character set had nothing, although it fails in exactly the same way: a
resolver minting ids terraform refuses is a tenant the API resolves and authorises
and nobody can provision, with no namespace, no service account and no secrets.

Only the class body is comparable, and it is the part that would change. The
validation is found by an `error_message` that names `swarm_common.identity`,
never by a line number and never by the regex, so a validation that stops
claiming parity stops being checked for it — loudly — rather than being quietly
compared against a rule it no longer cites.

### Why section 9 exists although a test already pinned those paths

`tests/terraform/endpoint_paths.tftest.hcl` asserts that `scheduler_push_path`
equals `"/pubsub/push"`. That string is written in the test. So the test and the
variable can agree with each other perfectly while `scheduler/main.py` serves
something else — which is precisely what happened twice:
`/pubsub/wake` against an app serving `/pubsub/push`, and `/refresh` against an
app whose sweep is `/v1/quota/sweep`. **An assertion between two copies is not a
comparison against the authority.** Section 9 reads the decorators.

The failure mode is the reason this ranks where it does: a Pub/Sub push
subscription treats 404 as a retryable delivery failure and a Cloud Scheduler
job logs it and moves on. Nothing alerts. No task is ever admitted, or provider
state simply goes stale, while every health check stays green.

### The fourth instance, found by the sweep

`kubernetes/worker-templates/worker-job-v2.yaml` carried `SWARM_ARTIFACTS_DIR`
on its **init container** — `install-credential`, which writes one credential
file and never touches an artifacts directory — while the `worker` container had
none. The commit that fixed defect 3 put it there, and the assertion added with
that fix read each template as one document, so it saw the name present and
passed.

"Set" and "set on the wrong container" are different facts. On v2 the second one
is not even loud: v2 leaves `readOnlyRootFilesystem` false on purpose, so
`mkdir /artifacts` succeeds, the artifact tree lands on the container's writable
layer instead of in the `workspace` emptyDir that carries
`sizeLimit: __DISK__`, and the first agent producing a large artifact set is
evicted mid-attempt for ephemeral-storage pressure — which reads as node disk
pressure, not as a missing variable.

Both the test and section 11 are now scoped to the `worker` container.

### The highest-blast-radius one: the shared deny-list, in the test that proves it

`make destroy` aborting on anything that belongs to another team is the control
standing between this repository and deleting a live GKE cluster it does not own.
CLAUDE.md states the rule plainly: "The deny-list lives once, in
`scripts/lib/common.sh`." Every consumer obeyed that — `destroy.sh`,
`purge-data.sh`, `plan-guard.sh`, `kubernetes/apply.sh` all read
`SHARED_DENY_LIST`.

The test proving the guard works did not. `tests/integration/test_destroy_guard.py`
carried its own hand-written list of "the real neighbours in the shared project"
and passed it to the guard as `--argjson deny`. It had **fourteen entries where
`SHARED_DENY_LIST` has twenty** — one cluster, one VPC, two subnets, the shared
default network, three buckets and twelve service accounts, counted on
2026-09-24 by walking the array rather than by restating a remembered number,
which is how this paragraph and the test's own docstring both came to say
twenty-one. Absent from it entirely: the three shared
buckets — `saga-agents-crawled-media-staging`, `saga-agents-files-staging`,
`saga-agents-terraform-state-staging` — and the other team's Compute Engine
default service account. A plan deleting any of those had never been shown to be
refused.

The shape was wrong too, and that is the more instructive half. The hand-written
list held service accounts by **short name**; `common.sh` holds them as **full
emails**, and that is what both callers pass. So the one matching behaviour
production depends on was the one the test did not exercise. Rebuilding an email
from a short name, which is what the test did, cannot even produce
`209012342332-compute@developer.gserviceaccount.com` — it is not on this
project's service-account domain.

The same file restated the unlabelable-type list as **seven of the forty** types
in `scripts/lib/unlabelable-types.json`, so the label-exemption logic was proved
over a sixth of its input. That file's own header records what happened the last
time this list was restated: an awk copy in the workflows exempted only
`google_project_iam*`, and a plan deleting a `google_storage_bucket_iam_member`
was blocked from production by a rule nobody had decided.

Both lists are now derived. The parametrised case runs over **every** entry
rather than a chosen four, feeding each entry exactly as written, and a separate
test asserts the derivation itself so a `split()` that stopped finding the array
fails on the count instead of going green over nothing.

And the transformation the guard's input needs — the bare `default` out, because
that string appears inside too many unrelated resource ids, and Firestore's
`(default)` in — was written out **three** times: in `scripts/destroy.sh` at the
real call site, in `scripts/lib/plan-guard.sh`, and a third time inside
`destroy.sh --self-test`. The deny-list obeyed the rule; its transformation did
not.

The third copy is the one that had already diverged. It omitted the `(default)`
entry the other two add — so the self-test of the guard that protects Firestore's
shared `(default)` database could not exercise that protection, because its own
list did not contain the name. Being on the unlabelable allow-list exempts
`google_firestore_document` from the *label* rule and from nothing else, so the
deny-list is the only thing standing between a destroy plan and another team's
`(default)` database, and it was the one branch the self-test could not reach.

All three now call `guard_deny_json` in `common.sh`, the self-test has a fixture
row for a document in `(default)` and asserts it lands in `denylist_hits`, and
`tests/integration/test_destroy_guard.py` asserts that both scripts still call
the shared function. A divergence there means CI passes a plan that `make
destroy` refuses, or — the direction that costs something — CI passes a plan that
touches another team's resource because its copy lost an entry the other kept.

### The same defect one level up: the guard's ARGUMENT LIST

The deny-list and its transformation are derived now. The next morning the thing
that broke was not a value inside the guard — it was the list of arguments the
guard has to be *called* with.

`scripts/lib/destroy-guard.jq` gained an ownership predicate, `is_ours($prefix)`,
on 2026-09-23. `scripts/lib/plan-guard.sh` was taught to pass
`--arg prefix "${SWARM_NAME_PREFIX:-swarm-}"`. The same filter is invoked from
four other places — twice in `scripts/destroy.sh`, once in
`scripts/verify-destroy-guard.sh`, twice in `tests/integration/test_destroy_guard.py`
— and none of them was. **jq refuses to compile a filter that references an
undefined variable**, so this is not a lenient default:

```
jq: error: $prefix is not defined at <top-level>, line 217
```

Exit 3, no verdict, no judgement. `destroy.sh --self-test` failed in CI
(run `35959558515`, 56 cases) and `make destroy` could not reach a safety
assertion at all — the teardown path was dead, and the one check that tells an
operator whether a plan touches another team's resources could not produce an
answer. It failed *closed*, which is the right direction and is not the same as
working.

The answer is (a) DERIVED, twice over: `guard_name_prefix` in `common.sh` is the
only spelling of the value, and `GUARD_ARGS` in
`tests/integration/test_destroy_guard.py` is the only spelling of the argument
list on the Python side. What holds it is
`test_every_caller_of_the_guard_passes_every_argument_it_requires`: it parses the
variables `destroy-guard.jq` declares and requires every caller — comments
stripped, so prose about the flag cannot stand in for the flag — to pass all of
them.

**The same outage was also fixed a second way, and the two fixes met in one
merge.** Main (#17) made the argument optional inside the filter:
`platform_prefix` reads `$ARGS.named.prefix // "swarm-"`, which compiles whether
or not a caller passes it. Both fixes stay. The filter's default means the next
caller to forget the argument still gets a verdict; the callers passing
`guard_name_prefix` mean nobody is judged by that default. But the default is a
second spelling of `guard_name_prefix`'s, so
`test_the_filters_fallback_prefix_is_guard_name_prefix` runs the filter over the
recorded real plan with and without `--arg prefix` and requires identical
verdicts — the `reason` of every foreign touch names the prefix, so any drift in
the value is a difference the comparison sees.

## Covered elsewhere, deliberately not moved here

These are compared, just not by `check-contract-parity.sh`. Each is listed so
that "not in the parity checker" is never read as "not covered".

| value | copies | where the assertion lives |
|---|---|---|
| the runner catalogue and resource classes | `terraform/infra/locals.tf` mirrors `swarm_common.profiles` | `tests/terraform/catalogue_mirror/` parses the Python and compares field by field |
| the credential redaction families | `swarm_api.redaction.RULES` mirrors `redact()` in `common.sh`; the key/value rule, which takes an escaped quote since #221, is one Python rule and two sed expressions | `tests/unit/control_plane/test_log_redaction.py`, including the direction that matters — a rule added to the shell filter and not to Python — and, for the key/value rule, both filters RUN over the same JSON-text lines with their output held equal |
| `sanitize_name` | `kubernetes/render.py` reproduces the dispatcher's body | `tests/unit/worker/test_kubernetes_manifests.py`, over a matrix including truncation and the leading-digit branch; the character class itself is imported, not copied |
| the tenant KSA the renderer creates | `render.py` vs `GkeJobDispatcher.ksa_for` | the same file |
| the Cloud Run Job id | `dispatch.py` vs `job_matrix.key` in terraform | `tests/unit/control_plane/test_job_name_matches_terraform.py` |
| every environment variable the control plane reads | the readers vs what terraform sets | `scripts/lib/check-env-parity.sh` |
| the pool-admin allow-list | each `POOL_ADMIN_ROUTES` entry in `swarm_api.auth` restates a route template from `swarm_api/routes/admin.py`, method included; a renamed route leaves the entry naming nothing, which fails closed — the gate is refused the route race-test needs. The template is the path in the *declaring* router, so an `include_router(prefix=...)` added in `main.py` has the same effect | `tests/unit/control_plane/test_pool_admin_is_narrow.py` requires every entry to be an admin-gated route a router serves and a path the app publishes in its OpenAPI document, the set to equal the owner's decision, and a pool admin's `PUT /v1/admin/limits/runner/mock` to succeed |
| the admin API's ceiling bound | `API_LIMIT_MAX` in `scripts/race-test.sh` mirrors the `le=` on `LimitRequest.limit` in `swarm_api.schemas`; the script cannot ask the API, and refuses before its first write to narrow a pool whose ceiling it could not restore | `tests/unit/scripts/test_race_test_limit_bound.py` imports `LimitRequest` and requires equality — higher strands a narrowed pool behind a 422, lower refuses pools the API could restore |
| the shared deny-list and the unlabelable-type list | `SHARED_DENY_LIST` in `common.sh`, and `unlabelable-types.json` | nothing — both were restated in `tests/integration/test_destroy_guard.py` and neither was compared; now derived there, with the derivation itself asserted. See below |
| the outcome ledger's choices, limits and vocabulary (#185) | `apps/swarm-ui/src/outcomes.ts` restates `swarm_api.outcomes`'s `SPANS`, `DEFAULT_SPAN`, `BUCKETS` (as `BUCKET_CHOICES`, with `auto`), `KINDS`, `GROUPS`, `MAX_BUCKETS`, `MONTH_MIN_DAYS`, `MAX_SPAN_DAYS` and `OUTCOMES_CACHE_S`, so the Timeline never offers what the route refuses and says truthfully how long a read is reused; and its unions `FailureClassKey`, `CancelCauseKey`, `LedgerBucketSize`, `GroupBy`, `UnreadReason` and `BucketState` restate the values the route sends. The browser cannot import Python, and the route cannot serve its own limits before the page has asked | `tests/unit/control_plane/test_outcomes_ui_field_contract.py` holds each constant equal and each union equal to the route's set, every parameter `outcomesQuery` sends to one `get_outcomes` declares, and every field of every `GET /v1/outcomes` payload, both directions and at every depth, to the types in `outcomes.ts` |
| the cluster's network in the tenant egress policy: pod range, service range, kube-dns Service IP, NodeLocal DNSCache address | every applied `swarm-allow-worker-egress` — its four `swarm.saga.xyz/*` network annotations and the rules that use the values — against the **live cluster**, which is the authority | `scripts/lib/check-cluster-network-parity.sh` with `kubernetes/network_parity.py`, **only with credentials**: CI has none, so there it skips with a `::notice`. `--require-live` is the gate, run after an apply. See [below](#the-one-whose-authority-is-a-live-cluster) |
| the variable a pool account's token fills, `CLAUDE_CODE_OAUTH_TOKEN` (#169) | `SUBSCRIPTION_TOKEN_ENV` in `scheduler/credentials.py`, which admission uses to decide whether a profile can run on a pool account at all, and `ACCOUNT_TOKEN_ENV` in `agent_worker/accountlease.py`, which the worker checks before asking the broker. The scheduler's image does not carry the worker. The rest of the rule is NOT restated: who an account serves is `quota_broker.accounts.accounts_serving`, which the scheduler imports and the broker's assign route calls. Contract request 22 asks for a home in `swarm_common.profiles` | `tests/unit/worker/test_pool_credential_parity.py` requires the two constants to be equal, and runs the REAL worker's credential resolution for every profile in the catalogue against the scheduler's `credential_for`: the set of profiles the worker asks the pool for must equal the set the scheduler expects a pool account for |
| the runner inputs a caller may send, which the bridge used to hold as its own table (`DECLARED_INPUTS` in `swarm_mcp/profiles.py`) | none now: the table is deleted and the bridge and swarm-api both read `RunnerProfile.inputs` and call `swarm_common.profiles.check_inputs`. What remains restated is what the declaration is ABOUT: each declared key names a `payload` read in its runner's source, and the mock's refused exit codes 77, 78 and 143 name `EXIT_*` constants in `agent_worker/runners/base.py`, which the catalogue cannot import | `tests/unit/mcp/test_runner_inputs.py` requires every declared key to be read by the runner the profile's `runner_argv` names, reads `base.py`'s `EXIT_*` constants and requires each non-failure code to be refused, and fails if the bridge grows a table of its own again; `tests/unit/control_plane/test_runner_inputs_by_declaration.py` changes the declaration under the API and requires the API's answer to change with it |
| the runner inputs' kinds and bounds as a reader sees them (#213 review) | the tables in `docs/workflows.md` and `plugin/README.md` between `runner-inputs:<profile>` markers, generated from `RunnerInput.describe()` and `means`. Until the review of #213 the docs table and the README stated `exit_code`'s and `retry_after_seconds`' bounds in their own words, and nothing compared them. Beside them, `SUGGESTED` in `Submit.tsx` for `browser` and `generic`, which is a census of what each runner reads because neither has declared yet | `tests/unit/mcp/test_runner_input_prose.py` requires each table to equal what the catalogue renders, no sentence in either file or in the delegate skill to state a number about a declared numeric key outside the table, and every example input to pass `check_inputs`; `tests/unit/control_plane/test_submit_offers_only_what_runners_read.py` requires every key the form offers a profile not declared yet to be read by that profile's runner |
| the `task.metadata` key naming the files a workflow step's dependants stage from it, `expected_outputs` (#149) | `EXPECTED_OUTPUTS_METADATA_KEY`, defined in `swarm_api/validation.py` beside the other reserved keys and re-exported by `swarm_api/expected_outputs.py`, which writes it at submission, and `METADATA_KEY` in `agent_worker/expected_outputs.py`, which reads it and tells the agent. The two packages never import each other, because a worker importing swarm-api would carry the control plane into every agent image | `tests/unit/control_plane/test_expected_outputs_seam.py` requires the two constants to be equal, and runs what `POST /v1/workflows` actually stores through the worker's reader and instruction builder |
| the words and exit codes the outcome ledger classifies a task's end by (#185) | `swarm_api.outcomes` restates: the worker's CANNOT-START exit 78 (`agent_worker.errors.ExitCode.CONFIG`, `reconciler.detect.WORKER_EXIT_CANNOT_START`); every worker exit it labels (`ExitCode`, `startup.EXIT_INTERRUPTED`); and the `last_error` texts of the worker's timeout and clean-exit-without-result, the reconciler's could-not-start and requeue, the worker's missing outputs, the opening of every `InputUnavailable` the worker raises while staging an input (#185 decision 4), the scheduler's dispatch failure, the three writers' "cancelled on request; " prefix, and the scheduler's failed-parent and fail_workflow sweep. The API image carries none of those packages. SINCE 2026-09-25 THE TEXT IS THE FALLBACK: contract request 23 was accepted and applied (PR #217), every terminal writer records a typed `Task.end_cause`, and the ledger reads it first, so these words classify only tasks that ended before the field existed. `DECLARED_COST_PROFILES` is no longer a restatement: it is derived from `RunnerProfile.cost_declared` (request 24) | `tests/unit/control_plane/test_outcomes_classifier_parity.py` holds 78 equal to both, the label set equal to the worker's exits, each f-string literal present in its writer's source, the rendered opening of EVERY `raise InputUnavailable(` in `inputs.py`, and calls the real `cannot_start_error` and `missing_error`; every dispatch code in `dispatch.py` must classify as `dispatch_failed`. `tests/unit/control_plane/test_outcomes_end_cause.py` holds the ledger's two cause maps to a partition of `EndCause`, and the module's source to naming no profile |
| what a CANCELLED parent's own end passes down to its dependant (#185 decision 2, the review of #217) | the scheduler (`_FAILURE_SENT_CAUSES` / `_CANCEL_SENT_CAUSES` in `scheduler/loop.py`, which pick a cascade's `failed_parent` or `cancelled_parent`) and the outcome ledger (`_SENT_BY_CANCEL_CAUSE` in `swarm_api.outcomes`, which splits a cascade that carries no cause). Separate images; neither can import the other, and the rule is product behaviour, not a contract type | `tests/unit/control_plane/test_outcomes_classifier_parity.py::test_the_scheduler_and_the_ledger_read_a_cancelled_parent_the_same_way` runs both over every `EndCause`, none, and the cancel flag, and holds them to one answer |

### The one whose authority is a live cluster

The tenant egress policy needs four values that belong to the cluster. The
ranges are carved out of the internet rule. The two DNS addresses are where a
pod's queries actually go on a Cloud DNS + NodeLocal DNSCache cluster.

**The render is (a) DERIVED.** `kubernetes/apply.sh` reads all four through
`kubernetes/cluster-network.sh`, the only reader. `check-cluster-network-parity.sh`
sources the same file, so the check and the render cannot read different fields.
`apply.sh` refuses the five network flags on its own command line. It refuses
them **in full or abbreviated**: argparse expands a prefix, and `--pod-cid`
forwarded after the read values used to replace them. `render.py` has no
defaults for them and refuses abbreviations itself.

Until 2026-09-24 this row would have read "not derived, not compared":

- The policy was rendered from `render.py`'s defaults, pods `10.0.0.0/8` and
  services `34.118.224.0/20`. swarm-autopilot uses `10.44.0.0/14` and `10.48.0.0/20`.
- The policy had no DNS address at all.
- A browser worker ran 390 s with every lookup silently dropped.

**The applied object is (b) COMPARED, and it is the exception to "on every
build".** A policy in the cluster is a copy that outlives the read that produced
it. It goes stale when:

- the cluster is recreated;
- the ranges change;
- NodeLocal DNSCache moves;
- someone applies a render by hand.

The authority is not in the repository, so no offline build can see a
difference. The check therefore has three modes:

- **CI:** skips with a `::notice`, so the run page says it compared nothing
  rather than passing.
- **Operator, with `--require-live`:** turns a skip into a failure.
  `kubernetes/README.md` ("Using it") runs it this way after every apply, and
  `apply.sh --confirm` prints the command.
- **Empty sweep:** refused, because no policy found is not agreement.

**Why the cluster, not terraform, is the authority.** The ranges start in
terraform: `pods_cidr` and `services_cidr` in each environment's tfvars become
the subnet's secondary ranges, and the cluster names those ranges. The
renderer still reads the cluster, for three reasons:

- `describe` holds the ranges the cluster **has**; tfvars holds the ranges it
  was **asked for**.
- Neither DNS address exists in terraform. They are only on `kube-system`
  objects.
- `apply.sh` must work against a cluster whose state file it cannot read. That
  is the same reason it resolves the RBAC uniqueIds from IAM.

Prod's tfvars ask for different ranges (`10.64.0.0/14`, `10.68.0.0/20`). That
is exactly why a typed dev value would be wrong the first time it was used
anywhere else.

**Prose that states the measured values.** These are (c), recorded, not inputs:

- the "measured" notes in `cluster-network.sh`;
- `allow-egress.yaml`'s comments;
- `kubernetes/README.md`.

Each is dated 2026-09-24 and says it was measured. None is read by anything.
The test fixture replaying the incident (`INCIDENT_CLUSTER` in
`tests/unit/scripts/test_cluster_network_is_read_not_typed.py`) is a record of
that day, not a copy to keep current. The tests that must fail on a typed value
use networks that appear nowhere else in the repository, so a hard-coded live
value cannot pass them.

## (c) Not compared, and why

Honest gaps. Each is a real restatement; none has an assertion.

**The worker's own environment contract.** `check-env-parity.sh` excludes
`apps/agent-worker` on purpose and says so: its environment is written by
`dispatch.py` and `render.py` at dispatch time, not by terraform, so it is a
different contract with a different writer. Section 11 now covers the three
variables in it that decide where the agent writes. **The rest of that pairing
is uncovered** — a worker setting read from the environment that no dispatcher
sets still fails open and silently, which is the exact shape
`check-env-parity.sh` was built for. This is the largest remaining gap and it is
a bounded piece of work, not a research problem.

**`WORKER_SA_PREFIXES` accepts a prefix nothing creates.** The quota broker
accepts `swarm-t` alongside the current prefix, and its comment says
`register-tenant.sh` writes `swarm-t-<tenant>`. It does not: `GSA_PREFIX` in
that script is the current prefix, asserted against the frozen module by
section 4, and `swarm_common.identity` records that the `swarm-t-` prefix "no
longer exists". So the broker accepts a tenant-identity shape no provisioning
path produces. Section 8 asserts containment rather than equality and reports
the extra entry instead of failing on it, because **narrowing an accepted
identity is a migration, not a parity fix** — it needs someone to confirm no
tenant registered under the old prefix is still live, and that is not a decision
a checker makes.

**`SCHEDULER_UID` and `RECONCILER_UID`.** `tests/unit/worker/` holds the control
plane's numeric IAM uniqueIds as literals. IAM assigns them; nothing in the
repository can derive them and nothing offline can verify them. They cannot be
derived and they cannot be compared — only re-read from IAM, which needs
credentials this suite deliberately does not have. The mitigation that exists
instead is the assertion that a *missing* uniqueId renders a duplicate subject
and never a fabricated numeric one.

**Prose that restates a value.** `terraform/modules/tenancy/variables.tf` says
"at most 11 characters" in an `error_message`, and the number is derived from the
GSA prefix length that section 4 asserts. The *condition* is checked; the
sentence is not, and a regex that policed English in error messages would fail
on rewording far more often than on drift. Left alone knowingly.

**The pod-security policy and the templates.** `policies/pod-security.yaml`
restates what `worker-job.yaml` sets — `requests == limits`, no Spot toleration,
no automounted token. This is a *policy* and its copy is the point: the manifest
declares the posture and the admission policy refuses anything that does not.
Two statements that must agree, where disagreement is caught at apply time by
the cluster itself rather than at build time by a checker, and
`tests/unit/worker/test_kubernetes_manifests.py` asserts the templates satisfy
the policy. Not a gap; recorded so it is not mistaken for one.
