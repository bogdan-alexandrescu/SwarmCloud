# Runbook: offboard a tenant

**When to use this.** A tenant (a Google group, or a personal `u-<user>`
tenant) is leaving the platform and everything provisioned for it has to go:
its identity, its keys, its jobs, its namespace, its records and its artifacts.

**How long.** About an hour of commands, plus however long the tenant's running
work takes to finish (up to two hours for a `claude-code` or `codex` task).

**What it changes.** Resources named for one tenant id, in the swarm's own
project resources and on the swarm's own Autopilot cluster. Nothing shared by
other tenants is deleted; three shared things are *edited* (the artifact
bucket's IAM policy, the image repository's IAM policy and `swarm-quota-broker`'s
invoker list each lose this tenant's member).

**The decision this runbook exists for.** Owner decision, 2026-09-24: the
reconciler does **not** garbage-collect GKE tenant namespaces, and it is not
getting a ClusterRole so that it could. PR #32 (commit `8a533b0`) removed the
reconciler's cluster-scope list and delete calls; `GkeBackend.list_job_resources`
in `apps/reconciler/reconciler/backends.py` now returns nothing, by design. A
Namespace is a cluster-scoped object, so collecting one needs cluster-scope
rights, and the policy that the platform's service accounts hold only namespaced
Roles is kept. So deleting `swarm-tenant-<id>` is a person's job, and
[step 8](#8-delete-the-gke-namespace-by-hand) is where it happens. Nothing else
in this repository will ever do it.

> **The deny-list.** `saga-agents-staging` is a shared project. It holds
> another team's live GKE cluster `agents-staging`, their VPC, their buckets
> and twelve of their service accounts. Every name this runbook deletes starts
> with `swarm` and ends in the tenant id. **If a command is about to touch a
> name that does not, stop.** The list of protected names lives once, in
> `SHARED_DENY_LIST` in `scripts/lib/common.sh`, and is not copied here
> (a second copy is how the two drift). Print the live list with:
>
> ```bash
> bash -c 'source scripts/lib/common.sh && printf "%s\n" "${SHARED_DENY_LIST[@]}"'
> ```
>
> The project id itself contains the string `agents-staging`, so "the command
> mentions agents-staging" is not the test. The test for a kube context is the
> segment after its last `_`: ours is `swarm-autopilot`, theirs is
> `agents-staging` (`gke_saga-agents-staging_us-central1-a_agents-staging`,
> which is the *current* context in a default kubeconfig on the reference
> workstation).

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
| B1 | GKE namespace `swarm-tenant-<id>` on `swarm-autopilot`, and everything in it: ResourceQuota `swarm-tenant-quota`, LimitRange `swarm-worker-limits`, ServiceAccounts `swarm-worker`, `swarm-agent-worker` and `default`, Role and RoleBinding `swarm-worker`, `swarm-dispatcher` and `swarm-reaper`, NetworkPolicies `swarm-default-deny`, `swarm-deny-cross-tenant-ingress` and `swarm-allow-worker-egress`, and any Job or Pod the dispatcher created | `scripts/register-tenant.sh` via `kubernetes/apply.sh` | `terraform/` has no kubernetes provider, and the reconciler holds no ClusterRole (the owner decision above) | [8](#8-delete-the-gke-namespace-by-hand) |
| B2 | A second namespace, if the tenant document's `namespace` field names one other than `swarm-tenant-<id>`. Tenants registered before 2026-09-24 may carry the old spelling `swarm-<id>` | an older `register-tenant.sh` | same as B1 | [8](#8-delete-the-gke-namespace-by-hand) |
| B3 | Cloud Run Jobs the dispatcher created itself: `swarm-job-<id>-<profile>-<class>` for a smaller resource class, or any job for a tenant terraform did not know. Labels `managed-by=swarm-scheduler`, `swarm-tenant=<id>` | `CloudRunJobDispatcher.ensure_job` | the reconciler deletes them after 7 days idle (`UNUSED_JOB_TTL_SECONDS`), not straight away | [9](#9-remove-what-the-platform-created-at-runtime) |
| B4 | Account pool: Firestore `accounts/<id>:<label>`; secrets `swarm-account-<id>--<label>` and `...-refresh` (labels `component=swarm-account`, `tenant=<id>`, `managed-by=swarm-secrets`); pending sign-ins in `account_auth`; `<id>` in the `lend_to` list of other tenants' accounts | the quota broker | removing an account deletes its document and **keeps its secret on purpose** (`remove_account` in `quota_broker/main.py`) | [5](#5-take-the-tenant-out-of-the-account-pool), [9](#9-remove-what-the-platform-created-at-runtime) |
| B5 | Runtime records: `tasks` (and each task's `events` subcollection), `attempts`, `leases`, `workflows`, `quota/<p>:<id>`; ledger entries `credential_publications/<secret name>` | the API, scheduler, workers and broker | runtime data, never in terraform state | [9](#9-remove-what-the-platform-created-at-runtime) |
| B6 | Artifacts and checkpoints under `gs://swarm-artifacts-<project>/tenants/<id>/`, including the `.tenant` marker `register-tenant.sh` writes | workers | the bucket is shared; only the prefix is the tenant's | [9](#9-remove-what-the-platform-created-at-runtime) |
| B7 | A provider secret added out of band for a provider that is *not* in the tenant's tfvars `providers` list (labels `component=tenant-credential`, `managed-by=swarm-secrets`) | `scripts/create-secrets.sh` | outside terraform's `for_each` | [9](#9-remove-what-the-platform-created-at-runtime) |

What stays whatever you do, and is worth saying so nobody reports it deleted:
Cloud Logging entries written by the tenant's workers (the log bucket's
retention decides), the Firestore export `scripts/purge-data.sh` writes to
`gs://<bucket>/backups/purge-<timestamp>` before it deletes (the whole
database, every tenant; the bucket's age rule expires it after
`artifact_retention_days`), and soft-deleted objects, which the bucket keeps for
7 days.

---

## The order, and why it is this order

| Step | What | Why it cannot move |
|---|---|---|
| 0 | Decide | steps 6 and 9 cannot be undone, and what they delete depends on these answers |
| 1 | Point the tools at the right places | everything after this deletes things |
| 2 | Record what exists | the GSA's `uniqueId` is the only handle `undelete` takes, and it is hard to find once the account is gone |
| 3 | Stop new work | otherwise step 4 never ends |
| 4 | Drain | step 6 deletes the GSA and the jobs running work uses, and the pool documents its leases give capacity back to |
| 5 | Account pool | must follow 4 (no account assigned to a running agent); do it before 6 so lenders and borrowers find out while the tenant still exists |
| 6 | Remove the tenant from terraform | removes `tenants/<id>`, which is what stops the reconciler reading the namespace |
| 7 | Verify terraform's half | |
| 8 | Delete the namespace by hand | **after** 6: the reconciler reads the namespace of every registered tenant on every pass (`_look_namespaced` in `apps/reconciler/reconciler/repair.py`), so a namespace deleted while `tenants/<id>` exists is reported unreadable, as a 403, on every pass |
| 9 | Runtime leftovers | after 4, because deleting an unreleased lease loses the capacity it holds; after 6, because nothing can dispatch for the tenant once its document is gone |
| 10 | Final verification | |

---

## 0. Decide before you start

**Which tenant, and is it one the platform itself needs?** Two tenants in
`dev.tfvars` are platform-internal: `u-sw-c90291` is the tenant the in-VPC
verification job (`swarm-verify`) resolves to, so offboarding it breaks the
release's smoke step, and `smoke` is the mock-runner tenant the smoke tests use.
Do not offboard either without the owner.

**Was it in terraform?** `grep -n "^  ${TENANT} = {" terraform/environments/<env>/<env>.tfvars`.
Terraform-declared tenants follow steps 0 to 10. A tenant made by
`scripts/register-tenant.sh` or created self-service by the API on first sign-in
is not in tfvars; for those, replace step 6 with
[the manual section](#if-the-tenant-was-never-in-terraform).

**Keep the tenant's data, or delete it now?** Choose per tenant, and write the
choice down in the PR that removes it:

* *Delete now* (step 9 with `--artifacts`): records, artifacts and checkpoints
  go today. There is no undo beyond the purge script's Firestore export and
  GCS soft delete (7 days).
* *Let it expire*: the bucket's lifecycle deletes live objects after
  `artifact_retention_days` (14 in dev, 180 in prod) and noncurrent versions 30
  days after that; Firestore records stay until purged.

  **Whatever you keep stays readable by the next tenant with the same id.** A
  tenant id is derived from its principal (`swarm_common.identity`), GCS access
  is granted by an IAM condition on `tenants/<id>/`, and step 6 deletes
  `tenants/<id>`, which is the record that the API's principal-collision check
  compares against. A later registration that derives the same id, whether the
  same group coming back or a different principal that slugs the same way,
  gets the old prefix, its old objects and every account still lent to that id.
  If the id may ever be reused, delete.

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
pre-merge plan in [step 6](#6-remove-the-tenant-from-terraform) is therefore the
only chance to read what will be deleted. `terraform.yml`'s header describes an
environment approval; for dev, that approval does not exist.

**Prod is not deployed.** The only swarm cluster in the project is
`swarm-autopilot`, labelled `swarm-env=dev`. This runbook uses dev's names. Both
tfvars files leave `name_prefix` at its default `swarm`, so prod would reuse
every name here; check them before running this against a prod that exists.

---

## 1. Point the tools at the right places

```bash
export ENVIRONMENT=dev PROJECT_ID=saga-agents-staging REGION=us-central1
export TENANT=<id>
export ARTIFACT_BUCKET="swarm-artifacts-${PROJECT_ID}"
export GSA="swarm-agent-worker-${TENANT}@${PROJECT_ID}.iam.gserviceaccount.com"
case "$TENANT" in ''|*[!a-z0-9-]*) echo "not a tenant id: '$TENANT'";; *) echo "tenant $TENANT";; esac
```

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

Now prove the context points at our cluster:

```bash
"$KUBECTL" config view -o jsonpath="{.contexts[?(@.name==\"swarm-${ENVIRONMENT}\")].context.cluster}"; echo
```

Expect exactly `gke_saga-agents-staging_us-central1_swarm-autopilot`. The segment
after the last `_` must be `swarm-autopilot`. Anything else, including an empty
line: stop, and run `make kubectl` again.

Stricter, if you will also type `kubectl` by hand: `eval "$(make kubectl-guard)"`
puts `bin/kubectl` first on `$PATH`. It resolves the binary through the same
`kubectl_bin` and refuses any context that is not `swarm-<env>` or the
gcloud-generated name for `swarm-autopilot`, read-only commands included.

---

## 2. Record what exists

Saved under `build/` (git-ignored) so the offboarding has a record, and so step
10 has something to compare against.

```bash
OUT="build/offboard-${TENANT}"; mkdir -p "$OUT"

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
```

The label filters are exact (`labels.tenant=eng` does not match `eng-x`), and
the IAM filter ends in `@` for the same reason. A `du` that says no URLs matched
means the prefix is empty; a `get namespace` NotFound means it was never
created, which is common for tenants that never ran a `browser` task.

Firestore, read-only (reads nothing secret; `account_auth` holds a PKCE
verifier, so only its count is printed):

```bash
bash -s -- "$TENANT" > "$OUT/firestore.txt" <<'SH'
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
`swarm-tenant-<id>`, that namespace is B2 and step 8 deletes it too.

---

## 3. Stop new work

Disable the tenant. This is the admin route; it needs an admin identity at the
front door (see `scripts/api.sh`'s header for `SWARM_IMPERSONATE_SA`):

```bash
scripts/api.sh PUT "/admin/tenants/${TENANT}/limits" '{"enabled": false}'
```

If the API is not reachable from where you are, the same write straight to
Firestore is equivalent: `Store.set_tenant_limits` does nothing more for
`enabled` than update that one field. The existence check comes first because a
Firestore PATCH *creates* a missing document, and a mistyped id would leave
behind a tenant record with no principal, which the API's collision check
treats as matching anyone:

```bash
bash -s -- "$TENANT" <<'SH'
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
scripts/pause-swarm.sh --tenant "$TENANT" --keep-scheduler
```

**`--keep-scheduler` is not optional.** Without it `pause-swarm.sh` also pauses
`swarm-scheduler-tick`, the one-minute Cloud Scheduler backstop that keeps
admission moving for *every* tenant when a wake message is missed. Do not add `--drain` either: its count is platform-wide. And
`pause-swarm.sh` overwrites `build/pause-state-<env>.json`, the record
`resume-swarm.sh` undoes from; if one exists from an earlier pause, copy it
aside first.

**Verify.**

```bash
bash -s -- "$TENANT" <<'SH'
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

Nothing may hold capacity for the tenant before step 6. Capacity is held from
`LEASED`, not `RUNNING` (invariant 3), so this counts all four
capacity-holding states and the leases themselves:

```bash
bash -s -- "$TENANT" <<'SH'
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
  run, replacing `<task_id>`:

```bash
bash -s -- "$TENANT" <task_id> <<'SH'
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
and cost nothing; step 9 deletes them with the rest of the records, or they stay
parked if you are keeping the history.

**Verify the backends agree**, because the reconciler releases a lease only when
it can see the execution is gone:

```bash
gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
  --filter="metadata.labels.swarm-tenant=${TENANT}" --format='value(metadata.name)' \
| while read -r job; do
    echo "== $job"
    gcloud run jobs executions list --job "$job" --project "$PROJECT_ID" --region "$REGION" --limit 5
  done
k get jobs,pods -n "swarm-tenant-${TENANT}"
```

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
bash -s -- "$TENANT" <<'SH'
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
step 9, after the tenant's GSA is gone.

**Accounts other tenants lend to this one.** Take `<id>` out of each `lend_to`.
Preferably the owning tenant does it (Settings in the UI, or
`PUT /v1/accounts/<account_id>/lending` with the list minus `<id>`). The reason
it matters is the id-reuse hazard in step 0: a lend names a tenant *id*, so it
would apply to whoever registers under that id next. The operator equivalent:

```bash
bash -s -- "$TENANT" <<'SH'
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

Pending sign-ins in `account_auth` expire after 15 minutes and are cleared in
step 9.

---

## 6. Remove the tenant from terraform

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
jq -r --arg t "$TENANT" '
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

The plan guard passes a tenant removal: every type deleted is either labelled
`managed-by=swarm-terraform` (secrets, jobs), listed in
`scripts/lib/unlabelable-types.json` (`google_service_account`,
`google_firestore_document`), or an IAM edge, which the guard matches by shape.

**Open the PR**, with the step 0 decisions and the plan output in its body. Its
CI runs fmt, validate, tflint, checkov and `terraform test`; it does **not**
plan, because the workload identity pool only mints tokens for `refs/heads/main`
(see `terraform.yml`'s `plan-not-run` job).

**Merge.** `release.yml` runs on the push: `terraform apply (dev)` plans again,
runs the same plan guard, and applies. Read that job's log and check its plan is
the one you read above.

---

## 7. Verify terraform's half

```bash
gcloud iam service-accounts describe "$GSA" --project "$PROJECT_ID" 2>&1 | head -2
```

Expect `NOT_FOUND`. A deleted service account can be restored for 30 days with
`gcloud iam service-accounts undelete <uniqueId>`, using the id step 2 saved.
That brings back the account, not the grants terraform removed before deleting
it; putting the tenant back in tfvars is what restores those.

```bash
gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
  --filter="bindings.members:swarm-agent-worker-${TENANT}@" \
  --format='table(bindings.role,bindings.members,bindings.condition.title)'
gcloud storage buckets get-iam-policy "gs://${ARTIFACT_BUCKET}" --format=json \
  | jq -r --arg t "swarm-agent-worker-${TENANT}@" '.bindings[]? | select(any(.members[]; contains($t))) | .role'
gcloud artifacts repositories get-iam-policy swarm-images --project "$PROJECT_ID" --location "$REGION" --format=json \
  | jq -r --arg t "swarm-agent-worker-${TENANT}@" '.bindings[]? | select(any(.members[]; contains($t))) | .role'
gcloud run services get-iam-policy swarm-quota-broker --project "$PROJECT_ID" --region "$REGION" --format=json \
  | jq -r --arg t "swarm-agent-worker-${TENANT}@" '.bindings[]? | select(any(.members[]; contains($t))) | .role'
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
gcloud secrets list --project "$PROJECT_ID" \
  --filter="labels.tenant=${TENANT} AND labels.managed-by=swarm-terraform" --format='value(name)'
gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
  --filter="metadata.labels.swarm-tenant=${TENANT} AND metadata.labels.managed-by=swarm-terraform" \
  --format='value(metadata.name)'
```

Expect both empty.

```bash
bash -s -- "$TENANT" <<'SH'
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

## 8. Delete the GKE namespace by hand

This is the step the owner decision leaves to a person. The reconciler will not
do it: a Namespace is cluster-scoped, and the platform's identities hold only
namespaced Roles, by policy. Do it after step 7 has shown `tenants/<id>` is
gone, for the reason in [the order table](#the-order-and-why-it-is-this-order).
Done the other way round, every reconciler pass logs the namespace as
unreadable; and if it was the only tenant namespace on the cluster, every
namespace the reconciler wants is unreadable, it treats GKE as blind, and the
`reconciler-cannot-see-a-backend` alert fires after three passes.

Use the `k` function from step 1: the binary from `kubectl_bin`, the isolated
kubeconfig, and the explicit context `swarm-dev`.

```bash
NS="swarm-tenant-${TENANT}"
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

```bash
k delete namespace "$NS" --wait=false
k wait --for=delete "namespace/$NS" --timeout=10m
k get namespace "$NS"
```

Expect `Error from server (NotFound)` from the last command. One namespace, by
name. Never delete by label selector or with `--all`: a selector is evaluated
across the whole cluster.

**B2: the tenant document named a different namespace** (step 2). Run the same
three commands with `NS` set to that name, label check included.

The Workload Identity bindings that pointed pods in this namespace at the GSA
(A6, and `register-tenant.sh`'s `swarm-worker` one) went with the GSA in step 6.
There is nothing to remove on the GCP side.

---

## 9. Remove what the platform created at runtime

**Jobs the dispatcher created (B3).** The reconciler would delete these after 7
idle days; there is no reason to wait. The label is checked again per job, as
the reconciler's own delete does, because the project is shared:

```bash
gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
  --filter="metadata.labels.swarm-tenant=${TENANT} AND metadata.labels.managed-by=swarm-scheduler" \
  --format='value(metadata.name)' > "$OUT/dispatcher-jobs.txt"
n=0
while read -r job; do
  [ -n "$job" ] || continue
  owner="$(gcloud run jobs describe "$job" --project "$PROJECT_ID" --region "$REGION" --format='value(metadata.labels.managed-by)')"
  [ "$owner" = "swarm-scheduler" ] || { echo "skipping $job: managed-by=$owner"; continue; }
  gcloud run jobs delete "$job" --project "$PROJECT_ID" --region "$REGION" --quiet
  n=$((n + 1))
done < "$OUT/dispatcher-jobs.txt"
echo "$n dispatcher job(s) deleted of $(grep -c . "$OUT/dispatcher-jobs.txt" || true) listed"
```

**Account-pool secrets (B4).** Irreversible. Listed by label, checked by name:

```bash
gcloud secrets list --project "$PROJECT_ID" \
  --filter="labels.component=swarm-account AND labels.tenant=${TENANT}" \
  --format='value(name.basename())' > "$OUT/account-secrets.txt"
cat "$OUT/account-secrets.txt"
n=0
while read -r s; do
  case "$s" in "swarm-account-${TENANT}--"*) ;; *) echo "skipping $s"; continue ;; esac
  gcloud secrets delete "$s" --project "$PROJECT_ID" --quiet
  n=$((n + 1))
done < "$OUT/account-secrets.txt"
echo "$n account secret(s) deleted"
```

**Records, leftover provider secrets and artifacts (B5 to B7).** One script,
which exports the whole database to the bucket first, asks for a typed
confirmation, and ignores `SWARM_ASSUME_YES`. Dry run first:

```bash
scripts/purge-data.sh --tenant "$TENANT" \
  --collections tasks,attempts,leases,workflows,quota --secrets --artifacts --dry-run
```

Name the collections. `--all` adds `pools` and `tenants`, and with `--tenant`
it would match `tenants/<id>` (already gone) but never a pool, because pool
documents carry no `tenant_id`. Leave out `--artifacts` if step 0 chose to let
them expire. `--secrets` deletes every secret labelled
`component=tenant-credential,tenant=<id>`, which after step 6 is only B7; run
before step 6, it would delete terraform's secrets and the next apply would
recreate them empty.

Then the real run (add `--allow-prod` for prod). Two things about its output
that look wrong and are not: the counts it prints under "Counting" and repeats
at the confirmation are **whole-collection** counts for every tenant, because
the count runs before the tenant filter does; what it deletes is filtered by
`tenant_id`. And it selects at most 1000 documents per collection per run, so
**repeat the dry run until every collection says `nothing matched`.**

`purge-data.sh --artifacts` removes the *live* objects. The bucket is versioned,
so every checkpoint it removed is still there as a noncurrent version, and the
lifecycle rule keeps those for 30 days. If step 0 chose delete, remove them too:

```bash
gcloud storage rm --recursive --all-versions "gs://${ARTIFACT_BUCKET}/tenants/${TENANT}/"
```

Soft delete still keeps them for 7 days (the storage module's
`soft_delete_retention_seconds`), restorable by anyone holding
`storage.objects.restore` on the bucket.

**The small Firestore leftovers.** Pools terraform did not own (a tenant created
by `register-tenant.sh` or the API, or a per-provider pool an admin created with
`PUT /v1/admin/limits/provider/<p>/tenant/<id>`), pending sign-ins, and the
publication ledger for this tenant's secrets. The ledger is keyed by secret
name, and a name prefix is ambiguous (`swarm-tenant-eng-` is also the start of
tenant `eng-x`'s secrets), so it is matched against the exact names step 2
recorded from their labels:

```bash
bash -s -- "$TENANT" "$OUT/secrets.txt" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"; recorded="$2"
eq() { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"EQUAL",value:{stringValue:$v}}}'; }
n=0
pools="$(fs_list_docs pools | jq -c --arg t "$t" 'select(.id == ("tenant:" + $t) or (.id | endswith(":tenant:" + $t)))')" \
  || die "could not list pools"
while IFS= read -r p; do
  [[ -n "$p" ]] || continue
  [[ "$(jq -r '.active // 0' <<<"$p")" == "0" ]] || die "$(jq -r .id <<<"$p") still has active capacity; step 4 is not done"
  fs_delete "pools/$(jq -r .id <<<"$p")"; n=$((n + 1))
done <<<"$pools"
names="$(fs_query account_auth "$(eq owner_tenant "$t")" | jq -r '.name | split("/") | last')" \
  || die "could not list pending sign-ins"
while IFS= read -r id; do [[ -n "$id" ]] || continue; fs_delete "account_auth/$id"; n=$((n + 1)); done <<<"$names"
while read -r secret _; do
  case "$secret" in swarm-tenant-*|swarm-account-*) ;; *) continue ;; esac
  case "$secret" in *-refresh) continue ;; esac
  doc="$(fs_get "credential_publications/$secret")" || die "could not read credential_publications/$secret"
  jq -e '.fields' <<<"$doc" >/dev/null || continue
  fs_delete "credential_publications/$secret"; n=$((n + 1))
done < "$recorded"
info "$n leftover document(s) deleted"
SH
```

---

## 10. Final verification

Everything below should print nothing, or an explicit absence:

```bash
gcloud iam service-accounts list --project "$PROJECT_ID" --filter="email=${GSA}" --format='value(email)'
gcloud secrets list --project "$PROJECT_ID" --filter="labels.tenant=${TENANT}" --format='value(name)'
gcloud run jobs list --project "$PROJECT_ID" --region "$REGION" \
  --filter="metadata.labels.swarm-tenant=${TENANT}" --format='value(metadata.name)'
gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
  --filter="bindings.members:swarm-agent-worker-${TENANT}@" --format='value(bindings.role)'
gcloud storage ls "gs://${ARTIFACT_BUCKET}/tenants/${TENANT}/" 2>&1 | head -3   # only if step 0 chose delete
k get namespace "swarm-tenant-${TENANT}" 2>&1                                    # NotFound
```

```bash
bash -s -- "$TENANT" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
t="$1"
eq() { jq -nc --arg f "$1" --arg v "$2" '{fieldFilter:{field:{fieldPath:$f},op:"EQUAL",value:{stringValue:$v}}}'; }
fs_get "tenants/$t" | jq -r "if .fields then \"tenants/$t PRESENT\" else \"tenants/$t absent\" end"
visited=0
for c in tasks attempts leases workflows quota; do
  n="$(fs_count_where "$c" "$(eq tenant_id "$t")")" || die "could not count $c; that is a failed read, not zero"
  printf '%-10s %s\n' "$c" "$n"; visited=$((visited + 1))
done
n="$(fs_count_where accounts "$(eq owner_tenant "$t")")" || die "could not count accounts"
printf '%-10s %s\n(%s collections checked)\n' accounts "$n" "$((visited + 1))"
SH
```

Expect `tenants/<id> absent`, zeros for every collection you purged, and
`(6 collections checked)`.

Last, the reconciler must have stopped looking for the namespace. Every pass
logs each namespace it wanted and could not read (`_look_namespaced` in
`apps/reconciler/reconciler/repair.py`); a few minutes after step 8 there must
be no such line for this tenant:

```bash
gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"swarm-reconciler\" AND jsonPayload.message:\"namespace unreadable\" AND jsonPayload.namespace=\"swarm-tenant-${TENANT}\"" \
  --project "$PROJECT_ID" --freshness=15m --limit 5 \
  --format='value(timestamp,jsonPayload.namespace,jsonPayload.error)'
```

Expect nothing timestamped after step 8. A line means `tenants/<id>` or a live
attempt still names the namespace: go back to step 7 or step 4.
[Troubleshooting](../troubleshooting.md#a-task-is-stuck-in-leased-or-dispatched)
explains how the reconciler reads GKE one namespace at a time.

Put the contents of `build/offboard-<id>/` and the decisions from step 0 in the
PR from step 6 or a follow-up comment on it. That is the record.

---

## If the tenant was never in terraform

A tenant made by `scripts/register-tenant.sh`, or created self-service by the API
on first sign-in, has no terraform state, so step 6 deletes nothing. Do steps 0
to 5 as written, then these in place of step 6, then steps 7 to 10.

A self-service tenant created by the API has only `tenants/<id>` and
`pools/tenant:<id>`: no GSA, no secrets, no namespace (the API writes
references to them and creates none). Most of this section then finds nothing,
which is the right answer.

Order matters here too: **remove bindings before deleting the account.** Once
the GSA is deleted, a binding that names it can only be removed by its
`deleted:serviceAccount:...?uid=<digits>` form.

```bash
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
```

Then re-run step 7's project and bucket checks: both must print nothing for
the member.

Secrets (A7) and jobs (A8): `gcloud secrets delete` for each
`swarm-tenant-<id>-<p>` and `-refresh` in `"$OUT/secrets.txt"`, and
`gcloud run jobs delete` for each job in `"$OUT/jobs.txt"`, checking the label
as step 9 does. Then the account:

```bash
gcloud iam service-accounts delete "$GSA" --project "$PROJECT_ID"
```

That takes the account's own IAM policy with it: any actAs grant and the
Workload Identity bindings `register-tenant.sh` added for `swarm-worker` and
`swarm-agent-worker`. There are no A9 or A10 bindings to remove: only terraform
makes those.

Finally the documents terraform would have removed (A11). Only after step 4
showed `active: 0`:

```bash
bash -s -- "$TENANT" <<'SH'
set -euo pipefail
source scripts/lib/common.sh
fs_delete "tenants/$1" && ok "tenants/$1 deleted"
SH
```

The pools go in step 9's leftovers block, which checks `active` before each
delete.

---

## What this runbook does not cover

* **Revoking people's access.** See step 0: `frontend_iap_members` in
  `terraform/bootstrap`, `allowed_domains`, and `api_invokers`.
* **Bringing the tenant back.** Re-add it to tfvars (or re-run
  `scripts/register-tenant.sh`), then `kubernetes/apply.sh --tenant <id> --confirm`
  for the namespace. Secret versions are gone for good; the tenant registers new
  keys with `scripts/create-secrets.sh`. Anything kept under `tenants/<id>/` is
  readable by it again, which is the point for a returning tenant and the hazard
  for a different one.
* **Several tenants at once.** There is no bulk path. Run it once per tenant;
  step 6 can remove several tenants in one PR if the plan lists exactly their
  resources.
