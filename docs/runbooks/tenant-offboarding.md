# Runbook: offboard a tenant

**When to use this.** A tenant (a Google group, or a personal `u-<user>`
tenant) is leaving the platform and everything provisioned for it has to go:
its identity, its keys, its jobs, its namespace, its records and its artifacts.

**How long.** About an hour of commands, plus however long the tenant's running
work takes to finish (up to two hours for a `claude-code` or `codex` task).

**What it changes.** Resources named for one tenant id, in the swarm's own
project resources and on the swarm's own Autopilot cluster. Nothing shared by
other tenants is deleted. Six shared things are *edited*, each losing only this
tenant's entry: the project IAM policy, the artifact bucket's IAM policy, the
`swarm-images` repository's IAM policy and `swarm-quota-broker`'s invoker list
each lose the member `serviceAccount:swarm-agent-worker-<id>@...`; `swarm-api`'s
`TENANT_GROUPS` loses the tenant's group; and other tenants' `accounts/*`
documents lose `<id>` from their `lend_to` lists.

**The two decisions this runbook exists for.**

* Owner decision, 2026-09-24: the reconciler does **not** garbage-collect GKE
  tenant namespaces, and it is not getting a ClusterRole so that it could. PR
  #32 (commit `8a533b0`) removed the reconciler's cluster-scope list and delete
  calls; `GkeBackend.list_job_resources` in
  `apps/reconciler/reconciler/backends.py` now returns nothing, by design. A
  Namespace is a cluster-scoped object, so collecting one needs cluster-scope
  rights, and the policy that the platform's service accounts hold only
  namespaced Roles is kept. So deleting `swarm-tenant-<id>` is a person's job,
  and [step 9](#9-delete-the-gke-namespace-by-hand) is where it happens.
  Nothing else in this repository will ever do it.
* Owner decision, 2026-09-24: **everything the tenant left is deleted at
  offboarding** (its artifacts and checkpoints, its Firestore records, its
  pools and its tenant document), so that a later registration deriving the
  same tenant id can read nothing of the old tenant. Keeping the data, or
  letting it expire, is no longer an option. `scripts/offboard-tenant.sh` does
  the deleting in [step 6](#6-delete-everything-the-tenant-left), and proves it
  before it releases the id. Provider secrets stay terraform's to delete, when
  the tenant leaves tfvars in step 7; the owner chose not to add deletion
  protection to them.

> **The deny-list.** `saga-agents-staging` is a shared project. It holds
> another team's live GKE cluster `agents-staging`, their VPC, their buckets
> and twelve of their service accounts. The list of protected names lives
> once, in `SHARED_DENY_LIST` in `scripts/lib/common.sh`, and is not copied
> here (a second copy is how the two drift). Print the live list with:
>
> ```bash
> bash -c 'source scripts/lib/common.sh && printf "%s\n" "${SHARED_DENY_LIST[@]}"'
> ```
>
> **The rule you check each command against.** Every target this runbook
> deletes or edits has one of the shapes in the table below, with this
> tenant's id in the `<id>` position. **If a command is about to delete or edit
> something that fits none of these shapes, or anything on the deny-list,
> stop.**
>
> | What | The only shapes this runbook touches |
> |---|---|
> | Service account | `swarm-agent-worker-<id>@saga-agents-staging.iam.gserviceaccount.com` (deleted) |
> | Secrets | `swarm-tenant-<id>-<p>`, `swarm-tenant-<id>-<p>-refresh`, `swarm-account-<id>--<label>`, `swarm-account-<id>--<label>-refresh`, each also labelled `tenant=<id>` |
> | Cloud Run jobs | `swarm-job-<id>-<profile>` and `swarm-job-<id>-<profile>-<class>`, each also labelled `swarm-tenant=<id>` |
> | GKE | namespace `swarm-tenant-<id>` (or the older `swarm-<id>`, row B2), labelled `swarm-tenant=<id>`, through context `swarm-dev` to cluster `swarm-autopilot` only |
> | GCS | objects under `gs://swarm-artifacts-saga-agents-staging/tenants/<id>/`; never the bucket |
> | Firestore | documents in the database named `swarm` (never `(default)`): `tenants/<id>`, `pools/tenant:<id>`, `pools/provider:<p>:tenant:<id>`, `accounts/<id>:<label>`, `quota/<p>:<id>`, `credential_publications/<one of the secret names above>`, and the `tasks` (with their `events`), `attempts`, `leases`, `workflows` and `account_auth` documents whose `tenant_id` or `owner_tenant` field is `<id>` |
> | IAM policies (edited) | the project's, `swarm-artifacts-saga-agents-staging`'s, `swarm-images`' and `swarm-quota-broker`'s, and only by removing the member `serviceAccount:swarm-agent-worker-<id>@...` or its `deleted:serviceAccount:...?uid=<digits>` form |
> | Other tenants' accounts (edited) | the `lend_to` field of `accounts/<other>:<label>`, and only by removing `<id>` from it |
>
> Two things the shapes alone do not settle, and how the runbook settles them:
>
> * **A name is a prefix match, and prefixes collide between tenants.**
>   `swarm-tenant-eng-` is also the start of every secret tenant `eng-x` owns.
>   So wherever a resource carries a tenant label, the commands select by the
>   exact label (`tenant=<id>`, `swarm-tenant=<id>`, a `tenant_id` equality
>   filter) and check the name only as a second guard. A command that selects by
>   a name prefix alone is not one of this runbook's.
> * **The project id itself contains the string `agents-staging`**, so "the
>   command mentions agents-staging" is not the test. The test for a kube
>   context is the segment after its last `_`: ours is `swarm-autopilot`,
>   theirs is `agents-staging`
>   (`gke_saga-agents-staging_us-central1-a_agents-staging`, which is the
>   *current* context in a default kubeconfig on the reference workstation).

Background: [multi-tenancy](../multi-tenancy.md#5-tenant-lifecycle) for what a
tenant is, [the dispatch 403 note](../gke-dispatch-403.md) for why a missing
namespace reads as a permission error, and
[troubleshooting](../troubleshooting.md) for how the reconciler reads GKE one
namespace at a time.

---

## The inventory: everything a tenant has

Derived from the code, not from memory: `terraform/modules/tenancy/main.tf`,
`terraform/modules/secret_manager/main.tf`, `terraform/modules/firestore/bootstrap.tf`,
`terraform/modules/cloud_run_jobs/main.tf`, `terraform/infra/main.tf` and
`locals.tf` (every consumer of `var.tenants`), `kubernetes/render.py`
(`TENANT_FILES`), `scripts/register-tenant.sh`,
`apps/scheduler/scheduler/dispatch.py` and `apps/quota-broker/`. The label
filters and IAM shapes below were checked against the live dev project on
2026-09-24.

`<id>` is the tenant id, `<p>` a provider in the tenant's `providers` list,
`<project>` is `saga-agents-staging`.

### A. Removed by terraform when the tenant is dropped from tfvars

Only for a tenant declared in `tenants = { ... }` in
`terraform/environments/<env>/<env>.tfvars`. For a tenant terraform never knew,
every row here is manual: see
[the section at the end](#if-the-tenant-was-never-in-terraform).

| # | Resource | Terraform address |
|---|---|---|
| A1 | Worker GSA `swarm-agent-worker-<id>@<project>.iam.gserviceaccount.com` | `module.tenancy.google_service_account.worker["<id>"]` |
| A2 | Project IAM: custom role `swarmTenantWorkerFirestore` (unconditioned) | `module.tenancy.google_project_iam_member.worker_firestore["<id>"]` |
| A3 | Project IAM: `roles/logging.logWriter`, `roles/monitoring.metricWriter`, `roles/cloudtrace.agent` | `module.tenancy.google_project_iam_member.worker_telemetry["<id>:<role>"]` (three) |
| A4 | Artifact bucket IAM: `swarmBucketMetadataReader`, and `roles/storage.objectUser` conditioned on `tenants/<id>/` (condition title `swarm-tenant-prefix-<id>`) | `module.tenancy.google_storage_bucket_iam_member.worker_bucket_metadata["<id>"]`, `.worker_objects["<id>"]` |
| A5 | `roles/iam.serviceAccountUser` on the GSA for the scheduler, the reconciler and the CI deployer | `module.tenancy.google_service_account_iam_member.act_as["<id>:scheduler"]`, `["<id>:reconciler"]`, `["<id>:deployer"]` |
| A6 | Workload Identity: `<project>.svc.id.goog[swarm-tenant-<id>/swarm-agent-worker]` may act as the GSA | `module.tenancy.google_service_account_iam_member.workload_identity["<id>"]` |
| A7 | Provider-key secrets `swarm-tenant-<id>-<p>` and `swarm-tenant-<id>-<p>-refresh`, with their accessor, version-adder and refresh-accessor bindings | `module.secret_manager.google_secret_manager_secret.this["swarm-tenant-<id>-<p>"]`, `.refresh["swarm-tenant-<id>-<p>-refresh"]`, and the three `google_secret_manager_secret_iam_binding` sets |
| A8 | Cloud Run Jobs `swarm-job-<id>-mock`, `swarm-job-<id>-generic`, plus `swarm-job-<id>-claude-code` if the tenant holds `anthropic` and `swarm-job-<id>-codex` if it holds `openai` | `module.cloud_run_jobs.google_cloud_run_v2_job.this["swarm-job-<id>-<profile>"]` |
| A9 | Image pull: custom role `swarmImagePuller` on the `swarm-images` repository | `module.artifact_registry.google_artifact_registry_repository_iam_member.pullers["tenant-<id>"]` |
| A10 | `roles/run.invoker` on `swarm-quota-broker` (the account pool) | `module.cloud_run.google_cloud_run_v2_service_iam_member.invokers["swarm-quota-broker:worker-<id>"]` |
| A11 | Firestore `tenants/<id>`, `pools/tenant:<id>`, `pools/provider:<p>:tenant:<id>` | `module.firestore.google_firestore_document.tenant["<id>"]`, `.pool["tenant:<id>"]`, `.pool["provider:<p>:tenant:<id>"]` |
| A12 | *Changed, not deleted:* `swarm-api`'s `TENANT_GROUPS` loses the group (only for `kind = "group"` with `directory_group = true`) | `module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]`, update in place |

Three things about this half that are easy to get wrong:

* **A7 is irreversible and immediate.** The secret module's
  `deletion_protection` defaults to `false` and `terraform/infra/main.tf` does
  not pass the root's `deletion_protection` into it, so the provider keys go
  with their whole version history on the apply, in prod as well as dev. Secret
  Manager's `version_destroy_ttl` delays destroying a *version*; it does not
  delay deleting a *secret*.
* **A11 only exists when `bootstrap_firestore_documents = true`**, which is the
  default and is not overridden in either tfvars file. The lifecycle
  `ignore_changes = [fields]` on those documents stops terraform reverting
  edits; it does not stop terraform deleting them.
* **The GSA-level bindings go with the GSA.** A5, A6, and the Workload Identity
  binding `register-tenant.sh` adds for the older KSA name `swarm-worker`, all
  live on the service account's own IAM policy. Deleting the account deletes
  that policy.

### B. Manual: terraform never held these

| # | Resource | Created by | Why it is manual | Step |
|---|---|---|---|---|
| B1 | GKE namespace `swarm-tenant-<id>` on `swarm-autopilot`, and everything in it: ResourceQuota `swarm-tenant-quota`, LimitRange `swarm-worker-limits`, ServiceAccounts `swarm-agent-worker` and `default` (and `swarm-worker`, with its RoleBinding `swarm-worker-legacy`, where the GSA binds it or an earlier apply left it), Role and RoleBinding `swarm-worker`, `swarm-dispatcher` and `swarm-reaper`, NetworkPolicies `swarm-default-deny`, `swarm-deny-cross-tenant-ingress` and `swarm-allow-worker-egress`, and any Job or Pod the dispatcher created | `scripts/register-tenant.sh` via `kubernetes/apply.sh` | `terraform/` has no kubernetes provider, and the reconciler holds no ClusterRole (the owner decision above) | [9](#9-delete-the-gke-namespace-by-hand) |
| B2 | A second namespace, if the tenant document's `namespace` field names one other than `swarm-tenant-<id>`. Tenants registered before 2026-09-24 may carry the old spelling `swarm-<id>` | an older `register-tenant.sh` | same as B1 | [9](#9-delete-the-gke-namespace-by-hand) |
| B3 | Cloud Run Jobs the dispatcher created itself: `swarm-job-<id>-<profile>-<class>` for a smaller resource class, or any job for a tenant terraform did not know. Labels `managed-by=swarm-scheduler`, `swarm-tenant=<id>` | `CloudRunJobDispatcher.ensure_job` | the reconciler deletes them after 7 days idle (`UNUSED_JOB_TTL_SECONDS`), not straight away | [10](#10-remove-what-the-platform-created-at-runtime) |
| B4 | Account pool: Firestore `accounts/<id>:<label>`; secrets `swarm-account-<id>--<label>` and `...-refresh` (labels `component=swarm-account`, `tenant=<id>`, `managed-by=swarm-secrets`); pending sign-ins in `account_auth`; `<id>` in the `lend_to` list of other tenants' accounts | the quota broker | removing an account deletes its document and **keeps its secret on purpose** (`remove_account` in `quota_broker/main.py`) | [5](#5-take-the-tenant-out-of-the-account-pool) (lends, owned accounts), [6](#6-delete-everything-the-tenant-left) (any account document or sign-in left), [10](#10-remove-what-the-platform-created-at-runtime) (secrets) |
| B5 | Runtime records: `tasks` (and each task's `events` subcollection), `attempts`, `leases`, `workflows`, `quota/<p>:<id>`; ledger entries `credential_publications/<secret name>`; pools terraform does not hold | the API, scheduler, workers and broker | runtime data, never in terraform state | [6](#6-delete-everything-the-tenant-left) |
| B6 | Artifacts and checkpoints under `gs://swarm-artifacts-<project>/tenants/<id>/`, every version, including the `.tenant` marker `register-tenant.sh` writes | workers | the bucket is shared; only the prefix is the tenant's | [6](#6-delete-everything-the-tenant-left) |
| B7 | A provider secret added out of band for a provider that is *not* in the tenant's tfvars `providers` list (labels `component=tenant-credential`, `managed-by=swarm-secrets`) | `scripts/create-secrets.sh` | outside terraform's `for_each` | [10](#10-remove-what-the-platform-created-at-runtime) |

What stays whatever you do, and is worth saying so nobody reports it deleted:

* Cloud Logging entries written by the tenant's workers. The log bucket's
  retention decides.
* Soft-deleted objects. Every object version step 6 deletes is *soft*-deleted,
  and the bucket keeps it for its soft-delete retention: 604800 seconds, 7
  days, read from `gcloud storage buckets describe` on 2026-09-24. Nothing
  deletes a soft-deleted object before that. **They can be restored by anyone
  holding `storage.objects.restore` on the prefix, and `roles/storage.objectUser`
  includes it** (`gcloud iam roles describe roles/storage.objectUser`, the same
  day). That is the role a tenant's worker service account holds on
  `tenants/<id>/`. So a registration of the same id that gets a worker service
  account within 7 days of step 6 could restore the old tenant's objects. The
  script prints how many there are.
* Old versions of every deleted Firestore document. Point-in-time recovery is
  **on** for the `swarm` database: `pointInTimeRecoveryEnablement:
  POINT_IN_TIME_RECOVERY_ENABLED`, `versionRetentionPeriod: 604800s`, read with
  `gcloud firestore databases describe --database=swarm` on 2026-09-24. A
  document step 6 deletes can still be read *as of* a time before the delete,
  for 7 days. Every tenant worker's `swarmTenantWorkerFirestore` role is
  unconditioned and includes `datastore.entities.get` (though not `list`), so
  such a read needs only a known document id. `tenants/<id>`, `quota/<p>:<id>`
  and `accounts/<id>:<label>` have predictable ids; task ids do not. Whether a
  read at a past time needs more than `datastore.entities.get` was **not**
  verified.
* Firestore exports that `scripts/purge-data.sh` wrote earlier, for some other
  purge, to `gs://<bucket>/backups/purge-<timestamp>`. Each holds the whole
  database, every tenant included, until the bucket's age rule expires it after
  `artifact_retention_days`. A tenant's worker cannot read `backups/`. This
  runbook no longer calls `purge-data.sh`, and step 6 takes no backup, because
  a backup would be a copy of exactly what the owner decision says must not
  survive.

---

## The order, and why it is this order

| Step | What | Why it cannot move |
|---|---|---|
| 0 | Decide | steps 6, 7 and 10 cannot be undone, and which path step 7 takes depends on these answers |
| 1 | Point the tools at the right places | everything after this deletes things |
| 2 | Record what exists | the GSA's `uniqueId` is the only handle `undelete` takes, and it is hard to find once the account is gone; and step 11 checks the publication ledger against the secret names recorded here |
| 3 | Stop new work | otherwise step 4 never ends, and step 6 refuses a tenant that is still enabled |
| 4 | Drain | step 6 refuses a tenant that holds any capacity; step 7 deletes the GSA and the jobs running work uses, and the pool documents its leases give capacity back to |
| 5 | Account pool | must follow 4 (no account assigned to a running agent); before 6, which refuses while another tenant still lends to this id, so lenders and borrowers find out while the tenant still exists |
| 6 | Delete everything the tenant left | **before** 7: it runs while `tenants/<id>` still holds the id, so nobody can register the id while its data exists, and the script deletes the tenant document itself only after proving everything else gone. After 3, 4 and 5 for the reasons above; the script checks all three itself |
| 7 | Remove the tenant from terraform | removes `tenants/<id>` and its terraform pools, which is what stops the reconciler reading the namespace, and the provider secrets |
| 8 | Verify terraform's half | |
| 9 | Delete the namespace by hand | **after** 7: the reconciler reads the namespace of every registered tenant on every pass (`_look_namespaced` in `apps/reconciler/reconciler/repair.py`), so a namespace deleted while `tenants/<id>` exists is reported unreadable, as a 403, on every pass |
| 10 | Runtime leftovers | after 7, because nothing can dispatch for the tenant once its document is gone, and because before 7 a provider secret found by label may still be terraform's |
| 11 | Final verification | |

---

## 0. Decide before you start

**Which tenant, and is it one the platform itself needs?** Two tenants in
`dev.tfvars` are platform-internal: `u-sw-c90291` is the tenant the in-VPC
verification job (`swarm-verify`) resolves to, so offboarding it breaks the
release's smoke step, and `smoke` is the mock-runner tenant the smoke tests use.
Do not offboard either without the owner.

**Was it in terraform?** List the tenants terraform declares, and look for the
id in the list (no placeholder to forget to edit, so an empty answer cannot be
a typo):

```bash
sed -n '/^tenants = {/,/^}/p' terraform/environments/dev/dev.tfvars | grep -E '^  [a-z0-9-]+ = \{'
# on 2026-09-24:  eng = {   smoke = {   u-sw-c90291 = {   u-bogdan = {
```

Terraform-declared tenants follow steps 0 to 11. A tenant made by
`scripts/register-tenant.sh` or created self-service by the API on first sign-in
is not in tfvars; for those, replace step 7 with
[the manual section](#if-the-tenant-was-never-in-terraform).

**The tenant's data is deleted. That is decided, not asked.** Owner decision,
2026-09-24: everything the tenant left goes at offboarding, in step 6. This
section used to offer *delete now* or *let it expire*. It no longer does, for
this reason:

**Anything kept is readable by the next tenant with the same id.** A tenant id
is derived from its principal (`swarm_common.identity`), GCS access is granted
by an IAM condition on `tenants/<id>/`, and the API's principal-collision check
compares against `tenants/<id>`, which offboarding deletes. A later
registration that derives the same id, whether the same group coming back or a
different principal that slugs the same way, would get the old prefix, its old
objects (the noncurrent versions too) and every account still lent to that id.
Letting data expire did not close that: live objects expired after
`artifact_retention_days` (14 in dev, 180 in prod), noncurrent versions 30 days
after that, and Firestore records never.

What deleting does **not** close are two 7-day windows described under
[the inventory](#the-inventory-everything-a-tenant-has): soft-deleted objects
can still be restored, and deleted Firestore documents can still be read as of
an earlier time. Nothing in this runbook shortens either.

**Offboarding a tenant is not revoking people.** Who may pass IAP is
`frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`, and today it
is `domain:saga.xyz`. After this runbook, a member of an offboarded group who
still signs in falls back to a personal `u-<user>` tenant, which the API creates
on first sight with no GSA, no secrets and no namespace (their work parks or
fails to dispatch with a missing identity). A personal tenant whose person keeps
access **comes back**: `Store.ensure_tenant` recreates `tenants/u-<user>` and its
pool on their next submission. If people must lose access too, that is a
separate change to `frontend_iap_members`, `allowed_domains` and, in prod,
`api_invokers`.

**There is no approval gate on dev.** Measured 2026-09-24 with
`gh api repos/{owner}/{repo}/environments`: `dev`, `dev-plan` and `build-pr`
exist and none has a protection rule, and there is no `prod` environment. So
merging the tfvars change to `main` *is* the apply: `release.yml`'s
`terraform apply (dev)` job plans and applies in one go with nobody asked. The
pre-merge plan in [step 7](#7-remove-the-tenant-from-terraform) is therefore the
only chance to read what will be deleted. `terraform.yml`'s header describes an
environment approval; for dev, that approval does not exist.

**Prod is not deployed.** The only swarm cluster in the project is
`swarm-autopilot`, labelled `swarm-env=dev`. This runbook uses dev's names. Both
tfvars files leave `name_prefix` at its default `swarm`, so prod would reuse
every name here; check them before running this against a prod that exists.

**Verify.** Before step 1, the PR description you will open in step 7 already
says, in writing: the tenant id, and that it is neither `u-sw-c90291` nor
`smoke` (or that the owner approved it); whether the id is in the tfvars
listing above (terraform path) or not (the manual section); and whether
anyone must also lose access. If any of the three is unanswered, do not start.
The data is not one of the questions: it is deleted (above).

---

## 1. Point the tools at the right places

Put the tenant id between the quotes on the third line, and keep the quotes:

```bash
cd "$(git rev-parse --show-toplevel)"
export ENVIRONMENT=dev PROJECT_ID=saga-agents-staging REGION=us-central1
TENANT='<id>'
unset GSA OUT ARTIFACT_BUCKET
case "$TENANT" in
  ''|*[^[:lower:][:digit:]-]*|????????????*)
    echo "STOP: '$TENANT' is not a tenant id. TENANT is now unset, so no later block will run."
    unset TENANT ;;
  *)
    export TENANT
    export ARTIFACT_BUCKET="swarm-artifacts-${PROJECT_ID}"
    export GSA="swarm-agent-worker-${TENANT}@${PROJECT_ID}.iam.gserviceaccount.com"
    export OUT="$PWD/build/offboard-${TENANT}"
    echo "tenant ${TENANT}" ;;
esac
```

**The check unsets instead of warning.** A check that only prints leaves the
next command free to run with an empty `TENANT`. `scripts/offboard-tenant.sh`
refuses an empty or malformed id itself, but not every command does:
`scripts/purge-data.sh`, which this runbook used to call, given
`--tenant ""` has no tenant filter at all: it would delete every
tenant's records, every tenant's provider keys (measured 2026-09-24: six
secrets across `eng` and `u-bogdan` carry `component=tenant-credential`) and
everything under `tenants/` in the bucket. So a failed check here removes
`TENANT`, `GSA`, `OUT` and `ARTIFACT_BUCKET`, and every later block that
deletes or writes refuses to start without them. The rule is the one
`terraform/modules/tenancy/variables.tf` enforces: lowercase letters, digits and
`-`, at most 11 characters. The unedited `'<id>'` fails it. The pattern is
spelled `[^[:lower:][:digit:]-]` on purpose: `[!...]` is a history expansion in
an interactive zsh, so the whole line fails with `event not found`, and in
macOS's bash 3.2 `[a-z]` also matches `E` (both measured 2026-09-24).

**Why every destructive block below is one command.** Measured 2026-09-24 in
bash 3.2.57 and zsh, pasting into an interactive shell with `TENANT` unset: a
guard on a line of its own, `: "${TENANT:?}"`, prints its error, **and the next
pasted line runs anyway**. A guard stops only the command it is part of. So
every block that deletes or writes is a single command: a `( ... )` subshell
whose first line is the guard, a `bash -s -- "${TENANT:?...}"` script, or one
command with the guard inside its own arguments. Paste each block whole. The
refusal reads `TENANT: run step 1 in this shell`, and it is the runbook
working.

**A new terminal has none of this.** The runbook spans a drain of up to two
hours and a merge and release. If you come back in a new shell or tab, run all
of step 1 again (this block, the `KUBECONFIG` export, and the `KUBECTL` and `k`
lines) before anything else. Setting `TENANT` by hand is not the same thing:
`GSA` and `OUT` stay unset, and the blocks that use them refuse.

The blocks below that talk to Firestore source `scripts/lib/common.sh`, which
sources `.env` first **and lets it win** over these exports. If you keep a
`.env`, check it agrees:
`grep -E '^(PROJECT_ID|ENVIRONMENT|FIRESTORE_DATABASE|ARTIFACT_BUCKET)=' .env`.

**The cluster**, from terraform: `module "gke_autopilot"` in
`terraform/infra/main.tf` names it `"${var.name_prefix}-autopilot"`, which is
`swarm-autopilot`, in `var.region` (`us-central1`). Measured 2026-09-24:

```bash
gcloud container clusters list --project "$PROJECT_ID" --filter='name~^swarm-' \
  --format='table(name,location,status,resourceLabels)'
# swarm-autopilot  us-central1  RUNNING  {..., 'managed-by': 'swarm-terraform', 'swarm-env': 'dev', ...}
```

**The kubeconfig and the context.** Always the isolated one:

```bash
make kubectl                                        # scripts/configure-kubectl.sh
export KUBECONFIG="$PWD/build/kubeconfig-${ENVIRONMENT}.yaml"
```

`scripts/configure-kubectl.sh` writes a kubeconfig holding only the swarm
cluster, refuses to fetch credentials for anything on the deny-list, and
renames the generated context `gke_saga-agents-staging_us-central1_swarm-autopilot`
to `swarm-dev`. **The context to use is `swarm-dev`**, and every kubectl command
below names it explicitly rather than trusting `current-context`.

**The kubectl binary**, resolved the way the scripts resolve it. kubectl 1.22
(EKS) and 1.25 (Docker Desktop) win `$PATH` on the reference workstation, and an
old client silently drops fields it does not understand:

```bash
KUBECTL="$(bash -c 'source scripts/lib/common.sh && kubectl_bin')"
"$KUBECTL" version --client
k() { "$KUBECTL" --context "swarm-${ENVIRONMENT}" "$@"; }
```

**Verify.** Every variable the later guards check, and the context:

```bash
echo "TENANT=${TENANT:-UNSET} GSA=${GSA:-UNSET} OUT=${OUT:-UNSET} KUBECTL=${KUBECTL:-UNSET} KUBECONFIG=${KUBECONFIG:-UNSET}"
"$KUBECTL" config view -o jsonpath="{.contexts[?(@.name==\"swarm-${ENVIRONMENT}\")].context.cluster}"; echo
```

Expect no `UNSET` on the first line, and exactly
`gke_saga-agents-staging_us-central1_swarm-autopilot` on the second. The segment
after the last `_` must be `swarm-autopilot`. Anything else, including an empty
line: stop, and run `make kubectl` again.

Stricter, if you will also type `kubectl` by hand: `eval "$(make kubectl-guard)"`
puts `bin/kubectl` first on `$PATH`. It resolves the binary through the same
`kubectl_bin` and refuses any context that is not `swarm-<env>` or the
gcloud-generated name for `swarm-autopilot`, read-only commands included.

---

## 2. Record what exists

Saved under `build/` (git-ignored) so the offboarding has a record, and so step
11 has something to compare against. Step 6's script writes its inventory and
proof to the same directory.

```bash
( : "${TENANT:?run step 1 in this shell}" "${GSA:?run step 1 in this shell}" "${OUT:?run step 1 in this shell}"
  mkdir -p "$OUT"

  gcloud iam service-accounts describe "$GSA" --project "$PROJECT_ID" --format=json > "$OUT/gsa.json"
  jq -r '.uniqueId' "$OUT/gsa.json"            # keep this; undelete takes it, not the email
  gcloud iam service-accounts get-iam-policy "$GSA" --project "$PROJECT_ID" --format=json > "$OUT/gsa-policy.json"

  gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
    --filter="bindings.members:swarm-agent-worker-${TENANT}@" \
    --format='table(bindings.role,bindings.condition.title)' > "$OUT/project-iam.txt"
  gcloud storage buckets get-iam-policy "gs://${ARTIFACT_BUCKET}" --format=json \
    | jq --arg m "serviceAccount:${GSA}" '[.bindings[]? | select(.members | index($m)) | {role, condition: .condition.title}]' \
    > "$OUT/bucket-iam.json"

  gcloud secrets list --project "$PROJECT_ID" --filter="labels.tenant=${TENANT}" \
    --format='table(name.basename(),labels.component,labels.managed-by)' > "$OUT/secrets.txt"
  gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
    --filter="metadata.labels.swarm-tenant=${TENANT}" \
    --format='table(metadata.name,metadata.labels.managed-by)' > "$OUT/jobs.txt"
  gcloud storage du --summarize "gs://${ARTIFACT_BUCKET}/tenants/${TENANT}/" > "$OUT/artifacts-size.txt"

  k get namespace "swarm-tenant-${TENANT}" --show-labels > "$OUT/namespace.txt"
  k get all,serviceaccounts,roles,rolebindings,networkpolicies,resourcequotas,limitranges \
    -n "swarm-tenant-${TENANT}" -o name >> "$OUT/namespace.txt"
)
```

The block is a subshell without `set -e` on purpose: a tenant with no namespace
or no artifacts makes one command fail, and the rest must still be recorded.

The label filters are exact (`labels.tenant=eng` does not match `eng-x`), and
the IAM filter ends in `@` for the same reason. A `du` that says no URLs matched
means the prefix is empty; a `get namespace` NotFound means it was never
created, which is common for tenants that never ran a `browser` task.

Firestore, read-only (reads nothing secret; `account_auth` holds a PKCE
verifier, so only its count is printed):

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" > "${OUT:?run step 1 in this shell}/firestore.txt" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"
eq() { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"EQUAL",value:{stringValue:$v}}}'; }
echo "== tenants/$t"
fs_get "tenants/$t" | jq "${FS_JQ} if .fields then doc else \"absent\" end"
echo "== pools"
fs_list_docs pools | jq -c --arg t "$t" 'select(.id == ("tenant:" + $t) or (.id | endswith(":tenant:" + $t))) | {id, active, hard_limit, enabled}'
echo "== quota"
fs_query quota "$(eq tenant_id "$t")" | jq -c "${FS_JQ} doc | {id, provider, state}"
echo "== accounts owned"
fs_query accounts "$(eq owner_tenant "$t")" | jq -c "${FS_JQ} doc | {id, state, assigned, lend_to, secret}"
echo "== accounts lent to this tenant"
fs_query accounts "$(jq -nc --arg v "$t" '{fieldFilter:{field:{fieldPath:"lend_to"},op:"ARRAY_CONTAINS",value:{stringValue:$v}}}')" \
  | jq -c "${FS_JQ} doc | {id, owner_tenant, lend_to}"
auth="$(fs_query account_auth "$(eq owner_tenant "$t")")" || die "could not read account_auth; that is a failed read, not none"
echo "== pending account sign-ins: $(printf '%s' "$auth" | grep -c . || true)"
SH
cat "$OUT/firestore.txt"
```

Read the `namespace` field of `tenants/<id>`. If it is anything other than
`swarm-tenant-<id>`, that namespace is B2 and step 9 deletes it too.

**Verify.** The record is complete and names the tenant you meant:

```bash
( : "${OUT:?run step 1 in this shell}"
  ls -l "$OUT"
  jq -r '"uniqueId " + (.uniqueId // "MISSING")' "$OUT/gsa.json"
  grep -A3 '^== tenants/' "$OUT/firestore.txt"
)
```

Expect nine files (`gsa.json`, `gsa-policy.json`, `project-iam.txt`,
`bucket-iam.json`, `secrets.txt`, `jobs.txt`, `artifacts-size.txt`,
`namespace.txt`, `firestore.txt`), a numeric `uniqueId`, and under
`== tenants/<id>` a document (a `{` line and its first fields). A tenant
created self-service by the API has no GSA, so for it `gsa.json` is empty and
there is no `uniqueId` line; that is correct. A tenant document that reads
`"absent"` means a wrong id, or a tenant already offboarded: stop and re-check
step 0.

---

## 3. Stop new work

Disable the tenant. This is the admin route; it needs an admin identity at the
front door (see `scripts/api.sh`'s header for `SWARM_IMPERSONATE_SA`):

```bash
scripts/api.sh PUT "/admin/tenants/${TENANT:?run step 1 in this shell}/limits" '{"enabled": false}'
```

If the API is not reachable from where you are, the same write straight to
Firestore is equivalent: `Store.set_tenant_limits` does nothing more for
`enabled` than update that one field. The existence check comes first because a
Firestore PATCH *creates* a missing document, and a mistyped id would leave
behind a tenant record with no principal, which the API's collision check
treats as matching anyone:

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
fs_get "tenants/$1" | jq -e '.fields' >/dev/null || die "tenants/$1 does not exist; check the id"
fs_patch "tenants/$1" "enabled" '{"enabled":{"booleanValue":false}}'
ok "tenants/$1 disabled"
SH
```

What that does: every submission for the tenant answers 403 (`SubmissionService.tenant_for`),
and the scheduler parks anything it would have admitted as `MANUAL_PAUSE`
("tenant is missing or disabled"). A parked task costs nothing (CONTRACT.md
invariant 1). Running work is untouched.

Then close the tenant's slot pool as well, so nothing is admitted even if the
flag is flipped back by mistake:

```bash
scripts/pause-swarm.sh --tenant "${TENANT:?run step 1 in this shell}" --keep-scheduler
```

**`--keep-scheduler` is not optional.** Without it `pause-swarm.sh` also pauses
`swarm-scheduler-tick`, the one-minute Cloud Scheduler backstop that keeps
admission moving for *every* tenant when a wake message is missed. Do not add `--drain` either: its count is platform-wide. And
`pause-swarm.sh` overwrites `build/pause-state-<env>.json`, the record
`resume-swarm.sh` undoes from; if one exists from an earlier pause, copy it
aside first.

**Verify.**

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
fs_get "tenants/$1"      | jq "${FS_JQ} doc | {enabled}"
fs_get "pools/tenant:$1" | jq "${FS_JQ} doc | {enabled, active}"
SH
```

Expect `"enabled": false` on both. `false // true` is `true` in jq, which is why
these print the field rather than defaulting it.

---

## 4. Drain

Nothing may hold capacity for the tenant before step 6, whose script refuses
otherwise, or before step 7. Capacity is held from `LEASED`, not `RUNNING`
(invariant 3), so this counts all four capacity-holding states and the leases
themselves:

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"
eq()   { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"EQUAL",value:{stringValue:$v}}}'; }
both() { jq -nc --argjson a "$1" --argjson b "$2" '{compositeFilter:{op:"AND",filters:[$a,$b]}}'; }
visited=0
for state in "${CONCURRENCY_STATES[@]}"; do
  n="$(fs_count_where tasks "$(both "$(eq tenant_id "$t")" "$(eq state "$state")")")" \
    || { err "could not count ${state}; that is a failed read, not zero"; exit 1; }
  printf 'tasks %-11s %s\n' "$state" "$n"
  visited=$((visited + 1))
done
live="$(fs_count_where leases "$(both "$(eq tenant_id "$t")" "$(fs_null_filter released_at IS_NULL)")")" \
  || { err "could not count leases; that is a failed read, not zero"; exit 1; }
printf 'unreleased leases %s\n(%s capacity states checked)\n' "$live" "$visited"
SH
```

Expect four zeros, `unreleased leases 0`, and `(4 capacity states checked)`. The
last line is there because an empty loop prints nothing and looks like success.

Until it is zero, one of three things ends each running task:

* **Wait.** Each profile has a hard timeout: `mock` 600 s, `generic` 3600 s,
  `browser` 5400 s, `claude-code` and `codex` 7200 s.
* **The tenant cancels.** A member can still cancel their own tasks after the
  tenant is disabled (reads and cancels go through `scope_for`, which does not
  refuse a disabled tenant): the UI, `scripts/swarm.py cancel <task>`, or
  `POST /v1/tasks/<id>/cancel`.
* **You cancel.** The API will not let an admin cancel another tenant's task.
  The equivalent write is the flag `Store.request_cancel` sets on a task that
  holds capacity. The worker sees it on its next control read, stops the
  runner, checkpoints and finishes the task as `CANCELLED`; if the worker is
  already gone, the reconciler finishes it. The write skips the audit event the
  API would append, so say in the PR which tasks you cancelled. One task per
  run, replacing `<task_id>` inside its quotes (left as it is, the script finds
  no such task and refuses):

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" '<task_id>' <<'SH'
set -euo pipefail
source scripts/lib/common.sh
fs_get "tasks/$2" | jq -e --arg t "$1" "${FS_JQ} doc | .tenant_id == \$t" >/dev/null \
  || die "task $2 is not tenant $1's"
fs_patch "tasks/$2" "cancel_requested,updated_at" \
  "$(jq -nc --arg at "$(iso_now)" '{cancel_requested:{booleanValue:true},updated_at:{timestampValue:$at}}')"
ok "tasks/$2: cancel requested"
SH
```

(The heredoc blocks in this runbook start at column 0 on purpose: `SH` only
ends the heredoc when nothing precedes it on its line.)

Pending tasks (`SUBMITTED`, `QUEUED`, `READY`, `PARKED`) do not block anything
and cost nothing; step 6 deletes them with the rest of the records.

**Verify the backends agree**, because the reconciler releases a lease only when
it can see the execution is gone:

```bash
( : "${TENANT:?run step 1 in this shell}"
  gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
    --filter="metadata.labels.swarm-tenant=${TENANT}" --format='value(metadata.name)' \
  | while read -r job; do
      echo "== $job"
      gcloud run jobs executions list --job "$job" --project "$PROJECT_ID" --region "$REGION" --limit 5
    done
  k get jobs,pods -n "swarm-tenant-${TENANT}"
)
```

Read-only, and guarded anyway: with `TENANT` empty, `k get` asks about a
namespace called `swarm-tenant-`, which does not exist, and answers
`No resources found`, which is exactly the answer this check is looking for.

Expect no execution still running, and `No resources found` on GKE. Then
`pools/tenant:<id>` should read `active: 0` (step 3's verify block).

---

## 5. Take the tenant out of the account pool

Skip this if step 2 listed no accounts owned by, or lent to, the tenant.

**Accounts the tenant owns.** Anyone the tenant lends them to loses them now;
tell those tenants first (step 2 printed `lend_to`). The API route for removing
an account is owner-only and refuses a disabled tenant, so after step 3 the
operator path is the broker's own operation: `AccountStore.remove` deletes the
document and nothing else.

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"
eq() { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"EQUAL",value:{stringValue:$v}}}'; }
rows="$(fs_query accounts "$(eq owner_tenant "$t")" | jq -r "${FS_JQ} doc | [.id, (.assigned // 0)] | @tsv")" \
  || die "could not list accounts; that is a failed read, not an empty pool"
n=0; skipped=0
while IFS=$'\t' read -r id assigned; do
  [[ -n "$id" ]] || continue
  case "$id" in "$t":*) ;; *) die "refusing $id: not an account of $t" ;; esac
  if [[ "$assigned" != "0" ]]; then
    warn "skipping accounts/$id: assigned=$assigned, so an agent is still running on it"
    skipped=$((skipped + 1)); continue
  fi
  fs_delete "accounts/$id"; ok "removed accounts/$id"; n=$((n + 1))
done <<<"$rows"
info "$n account document(s) removed, $skipped skipped"
SH
```

An account with `assigned > 0` is skipped on purpose: something is still
running on it, which means step 4 is not finished. Finish step 4 and run this
again until nothing is skipped.

Their secrets (`swarm-account-<id>--<label>` and `-refresh`) are deleted in
step 10, after the tenant's GSA is gone.

**Accounts other tenants lend to this one.** Take `<id>` out of each `lend_to`.
Preferably the owning tenant does it (Settings in the UI, or
`PUT /v1/accounts/<account_id>/lending` with the list minus `<id>`). The reason
it matters is the id-reuse hazard in step 0: a lend names a tenant *id*, so it
would apply to whoever registers under that id next. The operator equivalent:

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"
docs="$(fs_query accounts "$(jq -nc --arg v "$t" '{fieldFilter:{field:{fieldPath:"lend_to"},op:"ARRAY_CONTAINS",value:{stringValue:$v}}}')")" \
  || die "could not list lent accounts; that is a failed read, not none"
n=0
while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  id="$(jq -r "${FS_JQ} doc | .id" <<<"$row")"
  keep="$(jq -c --arg t "$t" "${FS_JQ} doc | [(.lend_to // [])[] | select(. != \$t)]" <<<"$row")"
  fs_patch "accounts/$id" "lend_to" \
    "$(jq -nc --argjson l "$keep" 'if ($l|length) == 0 then {lend_to:{arrayValue:{}}} else {lend_to:{arrayValue:{values:[$l[]|{stringValue:.}]}}} end')"
  ok "accounts/$id no longer lends to $t"; n=$((n + 1))
done <<<"$docs"
info "$n lending list(s) edited"
SH
```

Pending sign-ins in `account_auth` expire after 15 minutes, and step 6 deletes
any that are left.

**Verify.** Run this even if you skipped the step; it is what shows the skip was
right:

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"
owned="$(fs_count_where accounts "$(jq -nc --arg v "$t" '{fieldFilter:{field:{fieldPath:"owner_tenant"},op:"EQUAL",value:{stringValue:$v}}}')")" \
  || die "could not count owned accounts; that is a failed read, not zero"
lent="$(fs_count_where accounts "$(jq -nc --arg v "$t" '{fieldFilter:{field:{fieldPath:"lend_to"},op:"ARRAY_CONTAINS",value:{stringValue:$v}}}')")" \
  || die "could not count lent accounts; that is a failed read, not zero"
printf 'accounts owned by %s: %s\naccounts lending to %s: %s\n' "$t" "$owned" "$t" "$lent"
SH
```

Expect `0` on both lines. A non-zero owned count is an account that was
skipped (still `assigned`) or added since; a non-zero lending count is a lender
whose edit failed or who re-added the id. Step 6 refuses both: an assigned
account holds capacity, and once `tenants/<id>` is gone a lend to `<id>` still
names it.

---

## 6. Delete everything the tenant left

This is the step the second owner decision adds. `scripts/offboard-tenant.sh`
deletes, for one tenant:

* every object version under `gs://swarm-artifacts-<project>/tenants/<id>/`:
  artifacts, logs, checkpoints and the `.tenant` marker (B6). Every version,
  because the bucket is versioned and a noncurrent version is as readable
  through the tenant's IAM grant as a live one;
* its Firestore records (B5): every event carrying its `tenant_id`, under
  any task, including a task that is already gone; then each task's own
  `events`, then the `tasks` themselves, each task only once a recount finds
  no event left under it; `attempts`, `leases`, `workflows` and `quota`; the
  `accounts` and `account_auth` documents it owns; its `credential_publications`
  entries, listed from the ledger itself (below); and its pools;
* `tenants/<id>` itself, **last**, and only after a second, independent count
  of everything above has come back zero.

Why events are found two ways. Firestore does not delete a document's
subcollections with it: a task document deleted while an event is still under
it leaves that event where no listing of the tenant's *tasks* can reach it
again. That can happen during this step. Every event writer (the scheduler's
cascade cancel, the API, the worker and the reconciler) appends events as a
blind write outside any transaction, so an event can land between the
script's delete of a task's events and its delete of the task. So the script
keeps a task that still has an event under it, and the proof then fails on
it. It also lists and counts events by their own `tenant_id` with a
collection-group query, which finds an event whether or not its task still
exists. That query has to order by `at`, newest first. Measured read-only on
2026-09-24: without that order the database refuses it
(`FAILED_PRECONDITION`, "requires a COLLECTION_GROUP_ASC index"), because the
only index that serves it is terraform's `events-tenant-at`
(`terraform/modules/firestore/indexes.tf`). Deleting that index breaks this
step; it does not make it report zero.

How the ledger is attributed. `credential_publications/<secret name>`
carries no tenant field, so an entry is this tenant's when its id has one of
this tenant's secret-name shapes (`swarm-tenant-<id>-…`, `swarm-account-<id>--…`).
A name shape is a prefix, and prefixes collide, so two more rules apply. If a
secret of that name, or its `-refresh` twin, still has a `tenant` label, the
label decides. If not, the entry is left alone whenever a longer registered
tenant id also matches it (`eng-x`'s entry, while `tenants/eng-x` exists). This
finds an entry whose secret is already gone, which the earlier
label-first listing did not.

It leaves two things alone. Provider secrets belong to step 7 (terraform's) and
step 10 (the rest). And any tenant or pool document carrying
`managed_by=swarm-terraform` is left for step 7: for a tenant declared in
tfvars, that is its `tenants/<id>` and its `tenant:<id>` and
`provider:<p>:tenant:<id>` pools. `terraform/modules/firestore` created those
with `enabled = true` and holds them in state, so deleting them here would let
any release on `main` recreate them, **enabled**, before your tfvars PR
merges. They hold limits and counters, not the tenant's data, and while they
exist the id stays held. The script names each one `left for terraform`.

It refuses, and deletes nothing, when any of the earlier steps is not done. It
checks before it prints the inventory and again after you type the
confirmation, because the prompt can wait for an hour:

| Refusal | Step that fixes it |
|---|---|
| `tenants/<id>` does not exist: not a registered tenant | 0 (the id) |
| the tenant is still enabled | 3 |
| a task in `LEASED`, `DISPATCHED`, `STARTING` or `RUNNING`, an unreleased lease, a pool with `active > 0`, or an account with an agent assigned | 4 (and 5 for the account) |
| another tenant's account still lends to this id | 5 |

**Run it before the step-7 merge.** After the merge, terraform has deleted
`tenants/<id>` and the script refuses the id as unregistered. That refusal is
deliberate: a typo must find nothing to delete, not a prefix.

Dry run first. It is the default, and it prints the capacity table and the
inventory and deletes nothing:

```bash
scripts/offboard-tenant.sh --tenant "${TENANT:?run step 1 in this shell}"
```

Read the inventory: the number of object versions and bytes under the prefix,
and a count per collection. Every number is this tenant's alone. Each listing
is filtered on the tenant's own field, and each document's field and name are
checked again before anything is deleted, so a neighbour whose id starts with
this one (`eng-x` for `eng`) is not counted and not touched.

Then the real run. It asks you to type the tenant id, and it ignores
`SWARM_ASSUME_YES`:

```bash
scripts/offboard-tenant.sh --tenant "${TENANT:?run step 1 in this shell}" --apply
```

For prod, add `--allow-prod`. It then also asks for the project id.

The script deletes the objects first, then the records in batches of 200 with
a running count, then prints `== Proof: what is left of tenant <id> ==`. That
table is a fresh count of every row, taken by a different call from the one
that listed it: a server-side aggregation per collection, a fresh listing of
the ledger, a new `gcloud storage ls --all-versions` for the prefix. Events
have two rows. `task events` counts under every task any listing in this run
saw. `events by tenant_id` counts every event carrying the tenant's id under
any task, including one whose task is gone. **Any non-zero row prints
`VERIFICATION FAILED`, exits non-zero and does not delete `tenants/<id>`**,
so the id stays held.

Then run the same command again. The second run lists everything afresh from
the records themselves. A leftover event is found by its `tenant_id`, and a
task that was kept is found by the task listing. The rerun deletes them and
proves the result. It never works from a parent document the first run
deleted. What a rerun cannot reach is an event under a task that is already
gone *and* that does not carry this tenant's id. The reconciler writes
`tenant_id` as `""` when it does not know it. The script never deletes a task
that still has an event under it, which leaves only the moment between a
task's recount and its delete for that to happen; a rerun cannot recover it
afterwards. If the same row fails twice, read it before running a third time.
Something is still writing for this tenant, and step 3 or 4 did not hold.

The inventory and the proof are also written to
`build/offboard-<id>/offboard-tenant-<timestamp>.txt`, next to step 2's
record. Put that file in the step-7 PR.

Its last lines report the soft-deleted object versions, and the 7-day window
in which they can be restored (see [the inventory](#the-inventory-everything-a-tenant-has)).

**Verify.** The proof ends `(12 checks)`, and every row reads `0` or
`left for terraform`. The line after the table reads `tenants/<id>  absent`
for a tenant terraform never held, or `left for terraform` for one it does.
The exit code is 0.

---

## 7. Remove the tenant from terraform

**Edit** `terraform/environments/<env>/<env>.tfvars`: delete the tenant's
`<id> = { ... }` block, and the comment above it. If the tenant's principal also
appears in `api_invokers`, `admin_groups`, `admin_users` or a `secret_admins`
list, that is the access question from step 0; change it in the same PR only if
that was decided.

**Plan it before merging.** On dev a merge applies at once (step 0), so read the
plan on the branch. From a checkout of the branch, rebased on the `main` that was
last released:

```bash
make tf-plan                                    # scripts/plan.sh; read-only, pins the deployed image digests
TF="$(bash -c 'source scripts/lib/common.sh && terraform_bin')"
"$TF" -chdir=terraform/infra show -json "$PWD/build/${ENVIRONMENT}.tfplan" > "build/${ENVIRONMENT}.plan.json"
scripts/lib/plan-guard.sh --plan "build/${ENVIRONMENT}.plan.json" --mode apply
jq -r --arg t "${TENANT:?run step 1 in this shell}" '
  .resource_changes[]
  | select(.change.actions != ["no-op"] and .change.actions != ["read"])
  | [(.change.actions | join("+")), .address,
     (if (.address | contains($t)) then "" else "   <-- does not name the tenant" end)]
  | @tsv' "build/${ENVIRONMENT}.plan.json"
```

Do not run `make tf-apply`; the release applies. The plan must show:

* one `delete` per row A1 to A11, each address carrying the tenant id;
* for a group tenant with `directory_group = true`, one `update` of
  `module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]` (A12). That
  is the only line allowed to carry the `<-- does not name the tenant` marker;
* **nothing** to `create`, nothing replaced (`delete+create` or
  `create+delete`), and no other `update`.

Anything else means the branch or the state differs from what you think. Stop.

The marker is a substring test, so it has two limits. It is guarded, because
with `TENANT` empty `contains("")` is true of every address, nothing would be
marked, and a plan full of unrelated changes would read as clean. And it cannot
tell `eng` from `eng-x`: an address naming another tenant whose id starts with
this one carries no marker. So read the key of every `delete` too; it must be
this tenant's and no other's.

The plan guard passes a tenant removal: every type deleted is either labelled
`managed-by=swarm-terraform` (secrets, jobs), listed in
`scripts/lib/unlabelable-types.json` (`google_service_account`,
`google_firestore_document`), or an IAM edge, which the guard matches by shape.

**Open the PR**, with the step 0 decisions and the plan output in its body. Its
CI runs fmt, validate, tflint, checkov and `terraform test`; it does **not**
plan, because the workload identity pool only mints tokens for `refs/heads/main`
(see `terraform.yml`'s `plan-not-run` job).

**Merge.** `release.yml` runs on the push: `terraform apply (dev)` plans again,
runs the same plan guard, and applies. Read that job's log and check that its
deletes are the ones you read above.

**Verify.** The apply ran, and removed what your plan said it would:

```bash
gh run list --workflow release.yml --branch main --limit 3
gh run view <run id> --log | grep -E 'Plan: |Apply complete!|Error:'
```

Expect the `terraform apply (dev)` job to have succeeded, and
`Apply complete! Resources: 0 added, <n> changed, <d> destroyed.` where `<d>` is
the number of `delete` lines your plan printed. `<n>` may be larger than your
plan's one-or-none: the release pins the images it has just built
(`scripts/lib/image-refs.sh`), so every service and job whose digest moved is
updated in place in the same apply. Those updates are the release's. A
*destroyed* count that differs from yours is not, and means the state moved
between your plan and the merge. An `Error:` line means a partial apply, with
some of A1 to A11 gone and some not: plan again from this step and read what is
left before going on. Step 8 then checks the result resource by resource.

---

## 8. Verify terraform's half

Every check in this step is read-only and guarded anyway. With `TENANT` empty,
a filter such as `bindings.members:swarm-agent-worker-@` matches no member at
all, so each check would print the empty answer it is looking for without
having looked at the tenant.

```bash
( : "${GSA:?run step 1 in this shell}"
  gcloud iam service-accounts describe "$GSA" --project "$PROJECT_ID" 2>&1 | head -2
)
```

Expect `NOT_FOUND`. A deleted service account can be restored for 30 days with
`gcloud iam service-accounts undelete <uniqueId>`, using the id step 2 saved.
That brings back the account, not the grants terraform removed before deleting
it; putting the tenant back in tfvars is what restores those.

```bash
( : "${TENANT:?run step 1 in this shell}" "${ARTIFACT_BUCKET:?run step 1 in this shell}"
  gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
    --filter="bindings.members:swarm-agent-worker-${TENANT}@" \
    --format='table(bindings.role,bindings.members,bindings.condition.title)'
  gcloud storage buckets get-iam-policy "gs://${ARTIFACT_BUCKET}" --format=json \
    | jq -r --arg t "swarm-agent-worker-${TENANT}@" '.bindings[]? | select(any(.members[]; contains($t))) | .role'
  gcloud artifacts repositories get-iam-policy swarm-images --project "$PROJECT_ID" --location "$REGION" --format=json \
    | jq -r --arg t "swarm-agent-worker-${TENANT}@" '.bindings[]? | select(any(.members[]; contains($t))) | .role'
  gcloud run services get-iam-policy swarm-quota-broker --project "$PROJECT_ID" --region "$REGION" --format=json \
    | jq -r --arg t "swarm-agent-worker-${TENANT}@" '.bindings[]? | select(any(.members[]; contains($t))) | .role'
)
```

Expect all four empty. A member printed as
`deleted:serviceAccount:swarm-agent-worker-<id>@...?uid=<digits>` is a binding
terraform did not own. The usual one is a conditional Firestore binding an old
`register-tenant.sh` added (condition title `swarm_db_only`). It grants nothing,
and a new account with the same email would not inherit it, but remove it so the
policy says what is true. `--all` removes the role's bindings for that member
whatever their condition:

```bash
gcloud projects remove-iam-policy-binding "$PROJECT_ID" --role <role as printed> \
  --member 'deleted:serviceAccount:swarm-agent-worker-<id>@saga-agents-staging.iam.gserviceaccount.com?uid=<digits>' \
  --all
```

```bash
( : "${TENANT:?run step 1 in this shell}"
  gcloud secrets list --project "$PROJECT_ID" \
    --filter="labels.tenant=${TENANT} AND labels.managed-by=swarm-terraform" --format='value(name)'
  gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
    --filter="metadata.labels.swarm-tenant=${TENANT} AND metadata.labels.managed-by=swarm-terraform" \
    --format='value(metadata.name)'
)
```

Expect both empty.

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
fs_get "tenants/$1" | jq "${FS_JQ} if .fields then doc else \"tenants/$1 absent\" end"
fs_list_docs pools | jq -c --arg t "$1" 'select(.id == ("tenant:" + $t) or (.id | endswith(":tenant:" + $t))) | .id'
SH
```

Expect `tenants/<id> absent` and no pool ids. **If the tenant document is back**,
something signed in as that tenant after the apply and the API recreated it
(`Store.ensure_tenant`): a person with a personal tenant, or a group member
during the few seconds before `swarm-api`'s new revision stopped listing the
group. Return to the access question in step 0 before going on.

---

## 9. Delete the GKE namespace by hand

This is the step the owner decision leaves to a person. The reconciler will not
do it: a Namespace is cluster-scoped, and the platform's identities hold only
namespaced Roles, by policy. Do it after step 8 has shown `tenants/<id>` is
gone, for the reason in [the order table](#the-order-and-why-it-is-this-order).
Done the other way round, every reconciler pass logs the namespace as
unreadable; and if it was the only tenant namespace on the cluster, every
namespace the reconciler wants is unreadable, it treats GKE as blind, and the
`reconciler-cannot-see-a-backend` alert fires after three passes.

Use the `k` function from step 1: the binary from `kubectl_bin`, the isolated
kubeconfig, and the explicit context `swarm-dev`.

```bash
NS="swarm-tenant-${TENANT:?run step 1 in this shell}"
k get namespace "$NS" -o jsonpath='{.metadata.labels.managed-by} {.metadata.labels.swarm-tenant}{"\n"}'
```

Expect `swarm-terraform <id>`. These two labels are the ownership rule
`is_namespace_gc_eligible` in `apps/reconciler/reconciler/backends.py` defines
for exactly this case: a recognised `managed-by` marker **and** a
`swarm-tenant` label, which nothing but this platform sets. `swarm-terraform`
is correct even though no terraform state holds the namespace; see
`kubernetes/README.md`, conflict 6. If either label is missing or the tenant
label names someone else: stop.

```bash
k get all,serviceaccounts,roles,rolebindings,networkpolicies,resourcequotas,limitranges -n "$NS"
```

Expect only the objects in inventory row B1, and no Job or Pod. A Job with a
running pod means step 4 is not done; go back rather than delete an agent
mid-run.

The delete is one guarded block that repeats all three checks itself: the
context resolves to `swarm-autopilot`, the namespace carries exactly
`managed-by=swarm-terraform` and `swarm-tenant=<id>`, and no pod in it is still
live. Reading them above is for you; the block does not trust that you did.

```bash
( set -eu
  : "${TENANT:?run step 1 in this shell}" "${KUBECTL:?run step 1 in this shell}" "${NS:?set NS first}"
  cluster="$("$KUBECTL" config view -o jsonpath="{.contexts[?(@.name==\"swarm-${ENVIRONMENT}\")].context.cluster}")"
  [ "${cluster##*_}" = swarm-autopilot ] || { echo "STOP: context swarm-${ENVIRONMENT} is '${cluster}', not swarm-autopilot"; exit 1; }
  owner="$(k get namespace "$NS" -o jsonpath='{.metadata.labels.managed-by} {.metadata.labels.swarm-tenant}')"
  [ "$owner" = "swarm-terraform ${TENANT}" ] || { echo "STOP: $NS is labelled '${owner}', not 'swarm-terraform ${TENANT}'"; exit 1; }
  live="$(k get pods -n "$NS" --field-selector='status.phase!=Succeeded,status.phase!=Failed' -o name)"
  [ -z "$live" ] || { echo "STOP: live pods in $NS, so step 4 is not done:"; echo "$live"; exit 1; }
  k delete namespace "$NS" --wait=false
  k wait --for=delete "namespace/$NS" --timeout=10m
)
```

**Verify.**

```bash
k get namespace "${NS:?set NS first}"
```

Expect `Error from server (NotFound)`. One namespace, by name. Never delete by
label selector or with `--all`: a selector is evaluated across the whole
cluster.

**B2: the tenant document named a different namespace** (step 2). Set `NS` to
that name and run the label check, the listing, the delete block and the verify
again. The delete block's label check applies to it unchanged: a namespace
whose `swarm-tenant` label is not `<id>`, or whose `managed-by` is not
`swarm-terraform`, is not deleted. If an older namespace carries a different
platform marker (one of `MANAGED_VALUES` in the same `backends.py`), the block
refuses it; stop and take it to the owner rather than loosening the check.

The Workload Identity bindings that pointed pods in this namespace at the GSA
(A6, and `register-tenant.sh`'s `swarm-worker` one) went with the GSA in step 7.
There is nothing to remove on the GCP side.

---

## 10. Remove what the platform created at runtime

Every block in this step deletes something irreversible, and each is one
guarded command (step 1 says why). Paste each one whole.

**Jobs the dispatcher created (B3).** The reconciler would delete these after 7
idle days; there is no reason to wait. Both labels are checked again per job,
exactly, as the reconciler checks ownership before its own delete: the project
is shared, and a name prefix (`swarm-job-eng-`) also starts tenant `eng-x`'s
jobs.

```bash
( set -eu
  : "${TENANT:?run step 1 in this shell}" "${OUT:?run step 1 in this shell}"
  gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
    --filter="metadata.labels.swarm-tenant=${TENANT} AND metadata.labels.managed-by=swarm-scheduler" \
    --format='value(metadata.name)' > "$OUT/dispatcher-jobs.txt"
  n=0
  while read -r job; do
    [ -n "$job" ] || continue
    labels="$(gcloud run jobs describe "$job" --project "$PROJECT_ID" --region "$REGION" --format=json \
      | jq -r '(.metadata.labels["managed-by"] // "") + " " + (.metadata.labels["swarm-tenant"] // "")')"
    [ "$labels" = "swarm-scheduler ${TENANT}" ] || { echo "skipping $job: labels are '${labels}'"; continue; }
    gcloud run jobs delete "$job" --project "$PROJECT_ID" --region "$REGION" --quiet
    n=$((n + 1))
  done < "$OUT/dispatcher-jobs.txt"
  echo "$n dispatcher job(s) deleted of $(grep -c . "$OUT/dispatcher-jobs.txt" || true) listed"
)
```

**Account-pool secrets (B4).** Irreversible. Listed by the exact `tenant`
label, then checked by name:

```bash
( set -eu
  : "${TENANT:?run step 1 in this shell}" "${OUT:?run step 1 in this shell}"
  gcloud secrets list --project "$PROJECT_ID" \
    --filter="labels.component=swarm-account AND labels.tenant=${TENANT}" \
    --format='value(name.basename())' > "$OUT/account-secrets.txt"
  cat "$OUT/account-secrets.txt"
  n=0
  while read -r s; do
    [ -n "$s" ] || continue
    case "$s" in "swarm-account-${TENANT}--"*) ;; *) echo "skipping $s"; continue ;; esac
    gcloud secrets delete "$s" --project "$PROJECT_ID" --quiet
    n=$((n + 1))
  done < "$OUT/account-secrets.txt"
  echo "$n account secret(s) deleted of $(grep -c . "$OUT/account-secrets.txt" || true) listed"
)
```

**Provider secrets terraform did not hold (B7).** The tenant's records and
objects went in step 6. What is left here is secrets. After step 7, every
secret still labelled `component=tenant-credential,tenant=<id>` is one that
`scripts/create-secrets.sh` added out of band, for a provider that is not in
the tenant's tfvars. Deleting one is irreversible. The block lists them by the
exact labels. It stops if any of them is still terraform's, because that means
step 7 has not applied, and the next apply would then recreate the secret
empty. It checks each name as well:

```bash
( set -eu
  : "${TENANT:?run step 1 in this shell}" "${OUT:?run step 1 in this shell}"
  gcloud secrets list --project "$PROJECT_ID" \
    --filter="labels.component=tenant-credential AND labels.tenant=${TENANT}" \
    --format='value(name.basename(),labels.managed-by)' > "$OUT/provider-secrets-left.txt"
  cat "$OUT/provider-secrets-left.txt"
  if grep -q 'swarm-terraform' "$OUT/provider-secrets-left.txt"; then
    echo "STOP: a secret above is still terraform's, so step 7 has not applied"; exit 1
  fi
  n=0
  while read -r s _; do
    [ -n "$s" ] || continue
    case "$s" in "swarm-tenant-${TENANT}-"*) ;; *) echo "skipping $s"; continue ;; esac
    gcloud secrets delete "$s" --project "$PROJECT_ID" --quiet
    n=$((n + 1))
  done < "$OUT/provider-secrets-left.txt"
  echo "$n provider secret(s) deleted of $(grep -c . "$OUT/provider-secrets-left.txt" || true) listed"
)
```

This used to be `scripts/purge-data.sh --secrets --artifacts`, which also
deleted the records and the live objects. Step 6 does both now, for every
object version rather than only the live ones. And `purge-data.sh` first
exports the whole database into the bucket, which would be a copy of the data
the owner decision says must not survive.

**Verify.** Nothing this step deletes is left:

```bash
( : "${TENANT:?run step 1 in this shell}"
  gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
    --filter="metadata.labels.swarm-tenant=${TENANT}" --format='value(metadata.name)'
  gcloud secrets list --project "$PROJECT_ID" --filter="labels.tenant=${TENANT}" --format='value(name)'
)
```

Expect no job and no secret. Step 6's proof already showed the prefix and the
records empty. Step 11 checks them once more, with the pools, the account pool
and the publication ledger.

---

## 11. Final verification

Everything below should print nothing, or an explicit absence. It is all
read-only and all guarded: with `TENANT` empty, most of these filters match
nothing and would report a clean tenant without having looked for one.

```bash
( : "${TENANT:?run step 1 in this shell}" "${GSA:?run step 1 in this shell}" "${ARTIFACT_BUCKET:?run step 1 in this shell}"
  gcloud iam service-accounts list --project "$PROJECT_ID" --filter="email=${GSA}" --format='value(email)'
  gcloud secrets list --project "$PROJECT_ID" --filter="labels.tenant=${TENANT}" --format='value(name)'
  gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
    --filter="metadata.labels.swarm-tenant=${TENANT}" --format='value(metadata.name)'
  gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
    --filter="bindings.members:swarm-agent-worker-${TENANT}@" --format='value(bindings.role)'
  gcloud storage ls --all-versions "gs://${ARTIFACT_BUCKET}/tenants/${TENANT}/" 2>&1 | head -3   # matched no objects
  k get namespace "swarm-tenant-${TENANT}" 2>&1                                                   # NotFound
)
```

Then every Firestore record the tenant had, including the ones that live
outside its own documents: other tenants' `lend_to` lists (step 5), pending
sign-ins in `account_auth`, and the publication ledger in
`credential_publications` (both deleted in step 6). The ledger is keyed by
secret name and a name prefix is ambiguous (`swarm-tenant-eng-` also starts
every secret of tenant `eng-x`), so it is checked against the exact names step
2 recorded from their labels.

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" "${OUT:?run step 1 in this shell}/secrets.txt" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"; recorded="$2"
eq()  { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"EQUAL",value:{stringValue:$v}}}'; }
has() { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"ARRAY_CONTAINS",value:{stringValue:$v}}}'; }
checks=0
row() { printf '%-24s %s\n' "$1" "$2"; checks=$((checks + 1)); }
fs_get "tenants/$t" | jq -r "if .fields then \"tenants/$t PRESENT\" else \"tenants/$t absent\" end"
for c in tasks attempts leases workflows quota; do
  n="$(fs_count_where "$c" "$(eq tenant_id "$t")")" || die "could not count $c; that is a failed read, not zero"
  row "$c" "$n"
done
# Events live under tasks/<task>/events, so they are counted across every task
# at once. The order by `at` is required: without it the database refuses the
# query (step 6 says why), and a refusal is not a zero.
q="$(jq -nc --argjson w "$(eq tenant_id "$t")" '{structuredAggregationQuery:{structuredQuery:{
      from:[{collectionId:"events",allDescendants:true}], where:$w,
      orderBy:[{field:{fieldPath:"at"},direction:"DESCENDING"}]},
    aggregations:[{alias:"n",count:{}}]}}')"
n="$(fs_request POST "$(fs_base):runAggregationQuery" "$q" \
      | jq -r '[.[]?|.result?.aggregateFields?.n?.integerValue//empty]|first // "0"')" \
  || die "could not count events by tenant_id; a failed read, not zero"
row "events" "$n"
n="$(fs_count_where accounts "$(eq owner_tenant "$t")")" || die "could not count owned accounts; a failed read, not zero"
row "accounts owned" "$n"
n="$(fs_count_where accounts "$(has lend_to "$t")")" || die "could not count lend_to; a failed read, not zero"
row "accounts lending to it" "$n"
n="$(fs_count_where account_auth "$(eq owner_tenant "$t")")" || die "could not count account_auth; a failed read, not zero"
row "pending sign-ins" "$n"
ids="$(fs_list_docs pools | jq -r --arg t "$t" 'select(.id == ("tenant:" + $t) or (.id | endswith(":tenant:" + $t))) | .id')" \
  || die "could not list pools; a failed read, not none"
row "pools" "$(printf '%s' "$ids" | grep -c . || true)"
names=0; present=0
while read -r secret _; do
  case "$secret" in swarm-tenant-*|swarm-account-*) ;; *) continue ;; esac
  case "$secret" in *-refresh) continue ;; esac
  names=$((names + 1))
  doc="$(fs_get "credential_publications/$secret")" || die "could not read credential_publications/$secret"
  if jq -e '.fields' <<<"$doc" >/dev/null; then echo "  still present: credential_publications/$secret"; present=$((present + 1)); fi
done < "$recorded"
row "publication ledger" "$present (of $names recorded secret names)"
printf '(%s checks)\n' "$checks"
SH
```

Expect `tenants/<id> absent`; `0` for every collection and for events; `0` for
accounts owned, accounts lending to it, pending sign-ins and pools;
`publication ledger 0 (of <k> recorded secret names)`, where `<k>` is the
number of names in `"$OUT/secrets.txt"` that do not end in `-refresh`; and
`(11 checks)`, which is there because a loop that ran over nothing prints
nothing and looks like success.

**A `still present` ledger line for a subscription tenant is expected.** A
subscription tenant is one with a `swarm-tenant-<id>-<p>-refresh` secret. Its
ledger entry can come back after step 6, and step 6 did not fail. The broker's
credential sweep runs every five minutes. It refreshes *every* secret that has
a refresh credential and a `tenant` label, whether or not the tenant is still
registered (`subscription_tenants` in
`apps/quota-broker/quota_broker/secretstore.py`). It then records the token it
published (`_remember_published` in `quota_broker/credentials.py`). So between
step 6 and step 7, while terraform still holds the refresh secret, a broker
process that starts fresh re-creates the entry step 6 deleted. A broker process
that had already cached the entry's digest does not re-create it. Instead it
adds one identical version to the base secret on each sweep, because the
ledger no longer has the entry and its cache stops it writing one. That stops
when the token rotates, the process restarts, or step 7 deletes the secret.
Both behaviours are read from the code on 2026-09-24, not measured. Once step 7
(or step 10, for a secret terraform did not hold) has deleted the refresh
secret, nothing writes the entry again. Delete what is left then. The command
checks each name against the secrets step 2 recorded by label:

```bash
bash -s -- "${TENANT:?run step 1 in this shell}" "${OUT:?run step 1 in this shell}/secrets.txt" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"; recorded="$2"
n=0
while read -r secret _; do
  case "$secret" in "swarm-tenant-${t}-"*|"swarm-account-${t}--"*) ;; *) continue ;; esac
  case "$secret" in *-refresh) continue ;; esac
  doc="$(fs_get "credential_publications/$secret")" || die "could not read credential_publications/$secret"
  if jq -e '.fields' <<<"$doc" >/dev/null; then
    fs_delete "credential_publications/$secret"
    echo "deleted credential_publications/$secret"; n=$((n + 1))
  fi
done < "$recorded"
echo "$n ledger entr(ies) deleted"
SH
```

Then run the check above again: `publication ledger 0`.

Last, the reconciler must have stopped looking for the namespace. Every pass
logs each namespace it wanted and could not read (`_look_namespaced` in
`apps/reconciler/reconciler/repair.py`); a few minutes after step 9 there must
be no such line for this tenant:

```bash
gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"swarm-reconciler\" AND jsonPayload.message:\"namespace unreadable\" AND jsonPayload.namespace=\"swarm-tenant-${TENANT:?run step 1 in this shell}\"" \
  --project "$PROJECT_ID" --freshness=15m --limit 5 \
  --format='value(timestamp,jsonPayload.namespace,jsonPayload.error)'
```

Expect nothing timestamped after step 9. A line means `tenants/<id>` or a live
attempt still names the namespace: go back to step 8 or step 4.
[Troubleshooting](../troubleshooting.md#a-task-is-stuck-in-leased-or-dispatched)
explains how the reconciler reads GKE one namespace at a time.

Put the contents of `build/offboard-<id>/` and the decisions from step 0 in the
PR from step 7 or a follow-up comment on it. That is the record.

---

## If the tenant was never in terraform

A tenant made by `scripts/register-tenant.sh`, or created self-service by the API
on first sign-in, has no terraform state, so step 7 deletes nothing. Do steps 0
to 6 as written, then these in place of step 7, then steps 8 to 11.

On this path step 6 has already deleted `tenants/<id>` and its pools: nothing
holds them in state, so they carry no `managed_by=swarm-terraform` and the
script deletes them itself, the tenant document last, after its proof.

A self-service tenant created by the API has only `tenants/<id>` and
`pools/tenant:<id>`: no GSA, no secrets, no namespace (the API writes
references to them and creates none). Most of this section then finds nothing,
which is the right answer.

Order matters here too: **remove bindings before deleting the account.** Once
the GSA is deleted, a binding that names it can only be removed by its
`deleted:serviceAccount:...?uid=<digits>` form.

```bash
( : "${GSA:?run step 1 in this shell}" "${ARTIFACT_BUCKET:?run step 1 in this shell}"
  # Project bindings (A2, A3): the roles "$OUT/project-iam.txt" lists. `--all`
  # removes every binding of that role for this member, conditioned or not, which
  # matters because an older register-tenant.sh attached a condition to the
  # Firestore role that a plain remove would not match.
  for role in "projects/${PROJECT_ID}/roles/swarmTenantWorkerFirestore" \
              roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent; do
    if gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
         --member "serviceAccount:${GSA}" --role "$role" --all --quiet >/dev/null; then
      echo "removed $role"
    else
      echo "NOT removed: $role (read the error above; 'not found' means it was never bound)"
    fi
  done

  # Bucket bindings (A4). register-tenant.sh binds the object role with the
  # condition titled tenant_prefix_only; --all matches it without restating it.
  for role in roles/storage.objectUser "projects/${PROJECT_ID}/roles/swarmBucketMetadataReader"; do
    if gcloud storage buckets remove-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
         --member "serviceAccount:${GSA}" --role "$role" --all >/dev/null; then
      echo "removed $role"
    else
      echo "NOT removed: $role (read the error above)"
    fi
  done
)
```

No `set -e` here on purpose: a role that was never bound fails its remove, and
the others must still be tried.

**Verify.** Re-run step 8's project and bucket checks: both must print nothing
for the member.

**Secrets and jobs.** A tenant terraform never held has no A7 or A8. Its
provider secrets came from `scripts/create-secrets.sh`
(`component=tenant-credential`, `managed-by=swarm-secrets`, including the
`-refresh` twins), which makes them B7, and step 10's provider-secret block
deletes them. Its jobs, if any, are the dispatcher's (B3), and step 10 deletes
those too. What to check here is that the record agrees:

```bash
( : "${OUT:?run step 1 in this shell}"
  grep -c 'swarm-terraform' "$OUT/secrets.txt" "$OUT/jobs.txt"
)
```

Expect `0` for both files. Any `swarm-terraform` line means terraform does hold
this tenant, and this is the wrong section: go back to step 7.

Then the account:

```bash
gcloud iam service-accounts delete "${GSA:?run step 1 in this shell}" --project "$PROJECT_ID"
```

That takes the account's own IAM policy with it: any actAs grant and the
Workload Identity bindings `register-tenant.sh` added for `swarm-worker` and
`swarm-agent-worker`. There are no A9 or A10 bindings to remove: only terraform
makes those.

The documents terraform would have removed (A11) need nothing here: step 6
deleted them.

**Verify.** Step 8, in full: this section stands in for step 7, and step 8
checks it the same way, down to `tenants/<id> absent` and no pool ids.

---

## What this runbook does not cover

* **Revoking people's access.** See step 0: `frontend_iap_members` in
  `terraform/bootstrap`, `allowed_domains`, and `api_invokers`.
* **Bringing the tenant back.** Re-add it to tfvars (or re-run
  `scripts/register-tenant.sh`), then `kubernetes/apply.sh --tenant <id> --confirm`
  for the namespace. Secret versions are gone for good; the tenant registers new
  keys with `scripts/create-secrets.sh`. Nothing under `tenants/<id>/` or in
  Firestore survives step 6, so a returning tenant starts empty, exactly as a
  different principal with the same id would. The exceptions are the two 7-day
  windows, soft-deleted objects and point-in-time Firestore reads
  ([the inventory](#the-inventory-everything-a-tenant-has) says why).
* **Several tenants at once.** There is no bulk path. Run it once per tenant;
  step 7 can remove several tenants in one PR if the plan lists exactly their
  resources.
