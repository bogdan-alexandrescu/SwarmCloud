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

Sections 7 to 12 were added by the sweep. Two properties of all of them are
deliberate and should not be relaxed:

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

## Covered elsewhere, deliberately not moved here

These are compared, just not by `check-contract-parity.sh`. Each is listed so
that "not in the parity checker" is never read as "not covered".

| value | copies | where the assertion lives |
|---|---|---|
| the runner catalogue and resource classes | `terraform/infra/locals.tf` mirrors `swarm_common.profiles` | `tests/terraform/catalogue_mirror/` parses the Python and compares field by field |
| the credential redaction families | `swarm_api.redaction.RULES` mirrors `redact()` in `common.sh` | `tests/unit/control_plane/test_log_redaction.py`, including the direction that matters — a rule added to the shell filter and not to Python |
| `sanitize_name` | `kubernetes/render.py` reproduces the dispatcher's body | `tests/unit/worker/test_kubernetes_manifests.py`, over a matrix including truncation and the leading-digit branch; the character class itself is imported, not copied |
| the tenant KSA the renderer creates | `render.py` vs `GkeJobDispatcher.ksa_for` | the same file |
| the Cloud Run Job id | `dispatch.py` vs `job_matrix.key` in terraform | `tests/unit/control_plane/test_job_name_matches_terraform.py` |
| every environment variable the control plane reads | the readers vs what terraform sets | `scripts/lib/check-env-parity.sh` |
| the shared deny-list and the unlabelable-type list | `SHARED_DENY_LIST` in `common.sh`, and `unlabelable-types.json` | nothing — both were restated in `tests/integration/test_destroy_guard.py` and neither was compared; now derived there, with the derivation itself asserted. See below |

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
