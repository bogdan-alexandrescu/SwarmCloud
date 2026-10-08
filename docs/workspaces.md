# Personal workspaces: created at onboarding by a locked-down provisioner

**Status: PROPOSED 2026-10-08. This is a design only (#847). None of it is
built.** The owner asked on 2026-10-08 for each person's personal space to be
"onboarded at the time of onboarding a new user in swarmcloud", with "a way to
create this via the UI and potentially via the plugin". They also asked that it
"should block starting any task or workflow before this exists". Their decisions
in the issue's comments are the starting point here and are not reopened:

* **A separate, locked-down provisioner** creates the workspace. It starts when
  the person clicks in the console's setup checklist or runs `/sc:setup`.
  swarm-api only records and authorises the request, and gains **no** power to
  create identities or change IAM. The provisioner runs the same steps as
  `scripts/register-tenant.sh`, and the console shows its progress.
* **Before the workspace is ready**, the person may sign in, connect GitHub,
  choose repositories and look around. Every task or workflow submission is
  refused with `403 WORKSPACE_NOT_READY` and a link to the setup step. This
  applies on the console, the API and the plugin.

This document sits beside [onboarding.md](onboarding.md), which covers GitHub
access (#780). That flow gives a person a forge credential. This one gives them
somewhere to run. Frozen-contract requests go in
[contract-change-requests.md](contract-change-requests.md). This design needs
none (§1.4).

What it settles, one line each:

* **A workspace is a record, `workspaces/{tenant_id}`**, in four states:
  `requested`, `provisioning`, `ready` and `failed`. The document id is the
  person's derived tenant id, so there is one per person and a repeated request
  does nothing new (§1).
* **The provisioner is a Cloud Run Job, `swarm-workspace-provisioner`**, with its
  own account. swarm-api may only *run* that job and cannot pass it arguments
  (§2).
* **The shared project cannot bound it.** IAM cannot narrow service-account
  administration to a name prefix. So the recommended design puts personal
  worker identities in **a dedicated project**, where the provisioner's
  account-admin power reaches nothing else (§2.4, decision WD1).
* **Terraform keeps groups and service tenants. The provisioner owns human
  personal tenants.** `u-bogdan` is grandfathered as Terraform-owned (§3).
* **One gate, in the two submission methods every create path goes through**
  (§5).

The names below are placeholders where a real one is not needed. `alice@saga.xyz`
is a new person, and their tenant is `u-alice`. `<project>` is the platform's
project and `<number>` is its project number. `<idproject>` is the proposed
identity project (§2.4).

---

## 0. Today, re-read 2026-10-08 against `main` (4cbb7de)

* **A tenant document appears on first sight, with nothing behind it.**
  `apps/swarm-api/swarm_api/service.py::SubmissionService.tenant_for` calls
  `apps/swarm-api/swarm_api/store.py::Store.ensure_tenant`. That writes
  `tenants/u-<slug>` and `pools/tenant:u-<slug>` for any allowed-domain caller.
  Its own docstring says it does **not** create the service account, secrets,
  bucket conditions or namespace. The work of such a tenant "fails to dispatch
  with a missing-identity error". `tenant_for` is also what
  `apps/swarm-api/swarm_api/routes/tenants.py::get_me` calls, so even a page
  load writes the document. `u-admin` was created that way on 2026-09-21.
* **Every infrastructure boundary is out of band.** It comes either from the
  `tenants` map in `terraform/environments/dev/dev.tfvars` (the tenancy,
  secret_manager, firestore and cloud_run_jobs modules) or from
  `scripts/register-tenant.sh`. The script's §6 writes the same tenant document
  and pool.
* **Release namespaces are Terraform's only.** The release's "apply each tenant
  namespace" step in `.github/workflows/release.yml` iterates
  `terraform output tenant_namespaces`. It is skipped altogether while
  `vars.APPLY_TENANT_NAMESPACES` is not `true`, which is the case today. On
  2026-10-07, `u-bogdan` had no namespace until one was made by hand (#847).
* **The script and Terraform disagree on one grant.** `terraform/modules/tenancy/main.tf`
  gives the scheduler, the reconciler and the deployer `roles/iam.serviceAccountUser`
  on each worker account (`act_as`). `scripts/register-tenant.sh` only *tolerates*
  that binding in its squat check (§2b). It never makes it. A tenant registered
  by the script alone therefore cannot be dispatched to Cloud Run: the dispatcher
  creates jobs on demand (`apps/scheduler/scheduler/dispatch.py::CloudRunJobDispatcher.ensure_job`)
  and needs `actAs` on the job's account. This was found by reading, not by a
  run. The provisioner must make this grant (§4, step P6), and the script should
  too. This is reported as an out-of-territory finding on the PR.
* **A personal tenant runs on nothing until someone gives it a credential.** It
  needs its own provider key, or a pool account owned by or lent to it
  (`apps/quota-broker/quota_broker/accounts.py::accounts_serving`; quota-management.md
  §4). Otherwise its claude-code tasks park `CREDENTIAL_MISSING`. A ready
  workspace does not change that (§8, WD6).

---

## 1. The request record

### 1.1 `workspaces/{tenant_id}`

This is a new collection. It is not a field on `Tenant`, because `Tenant` is in
the frozen contract (`apps/common/swarm_common/models.py`). The document id is
`apps/common/swarm_common/identity.py::tenant_id_for_user` of the requester's
verified email. That is the same function the API resolves a caller with, so
the record is keyed by the only id that person can ever resolve to.

| field | type | written by | meaning |
|---|---|---|---|
| `tenant_id` | string | swarm-api | `u-alice`; equals the document id |
| `principal` | string | swarm-api | `alice@saga.xyz`, lower-cased, from the verified token, never from the body |
| `owner` | string | swarm-api / backfill | `provisioner` for a requested workspace; `terraform` for a tenant in `var.tenants` (§3.3). The provisioner refuses to act on anything else |
| `state` | string | swarm-api, provisioner | `requested` → `provisioning` → `ready`, or → `failed` (§1.2) |
| `request_id` | string | swarm-api | a fresh UUID for each accepted request or retry; the provisioner logs it on every step |
| `requested_at`, `requested_via` | timestamp, string | swarm-api | `console`, `plugin` or `api` |
| `attempt` | int | provisioner | incremented on each claim, so the console can say "attempt 2" |
| `claim` | map | provisioner | `{execution, claimed_at, expires_at}`: a lease on the record. It is held by one execution at a time, and a claim past `expires_at` (15 min) may be taken over |
| `steps` | map | provisioner | `step id → {state: todo/done/failed/skipped, at, code}`, using the ids in §4 |
| `failure` | map or null | provisioner | `{step, code, retryable, at}`. The copy is served from the code (§4.3), never stored free-form, so no command output reaches Firestore |
| `limits` | map | provisioner | the `max_active` and `capacity_units` written to the tenant (§8) |
| `identity` | map | provisioner | `{service_account, namespace, gcs_prefix}` as created. It is for display only. Dispatch reads `tenants/{id}` as it does today |
| `ready_at` | timestamp | provisioner | |
| `spec_keys_version` | string | provisioner | the step-spec key set last applied to the namespace's ConfigMap (§2.6) |
| `managed_by` | string | both | `swarm-workspace-provisioner` |

### 1.2 States

| state | entered when | the person sees | submissions |
|---|---|---|---|
| (no record) | never requested | checklist step "Create your workspace", button enabled | refused, `WORKSPACE_NOT_READY` |
| `requested` | swarm-api accepted a request | "Queued", with a spinner | refused |
| `provisioning` | the provisioner claimed it | each step of §4 with a tick or spinner | refused |
| `ready` | every step is `done` (or `skipped` by rule) | the step is ticked, and the "first task" prompt appears | admitted |
| `failed` | a step failed past its retries | the step's code and copy (§4.3), plus "Try again" when `retryable` | refused |

`ready` is **evidence-derived on the provisioner's side**: it is written only
after the final verification step (P11) has read back every object. swarm-api
never writes `ready`, and nothing a client sends can set it. The same rule
applies in `apps/swarm-api/swarm_api/onboarding.py::derive`: a step's state
comes from evidence, never from a flag a client sets.

### 1.3 Who may request, and idempotency

`POST /v1/workspace` (§6.3) accepts a request only when **all** of these hold.
Otherwise the response is 403 with a code:

1. The caller has a verified Google identity in an allowed domain (`saga.xyz`).
   This is the same `assert_allowed_domain` every route uses.
2. The caller is a **human**: the address does not match the frozen
   service-account pattern in `apps/common/swarm_common/identity.py`. A service
   account's tenant is Terraform's (`u-sw-c90291` for `swarm-verify`).
   Otherwise the code is `WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS`.
3. The caller is not a secret-admin principal. This is the refusal `tenant_for`
   already makes, for the reason given there. Otherwise the code is
   `WORKSPACE_PRINCIPAL_FORBIDDEN`.
4. The target is **always** `tenant_id_for_user(caller.email)`, never a body
   field and never `X-Swarm-Tenant`. Nobody can request a workspace for anyone
   else.
5. If `tenants/{id}` already exists, its `principal` must equal the caller.
   This is the collision check of `Store.ensure_tenant` /
   `Store.assert_tenant_scope`, unchanged. Otherwise the code is
   `WORKSPACE_ID_TAKEN`, and the message names the support route rather than the
   other principal.

The write is **one Firestore transaction** that reads the record and then:

* **No record:** creates it in `requested` with `owner = "provisioner"`, and
  answers 202.
* **`requested`, `provisioning` or `ready`:** writes nothing and answers 200
  with the record. Clicking twice, or clicking in the console and then in the
  plugin, is the same request.
* **`failed` and `retryable`:** moves it to `requested`, sets a new
  `request_id`, keeps `steps` (completed steps are re-verified, not redone), and
  answers 202. A retry is refused for 60 seconds after the last failure
  (`WORKSPACE_RETRY_TOO_SOON`), so a held-down button is one retry.
* **`failed`, not retryable:** answers 409 with the failure copy, which names
  the operator action.
* **`owner = "terraform"`:** answers 200 with the record (it is `ready`). The
  provisioner never touches it.

After a 202, swarm-api triggers the provisioner (§2.2). Failing to trigger is
not a failed request: the record stays `requested`, and the sweep picks it up
within its period.

### 1.4 Frozen contract

None is needed. `workspaces/` is a new collection read only by swarm-api, the
provisioner and the release step. `Tenant` gains no field, and the scheduler's
admission does not read the workspace (the gate is at submission; §5.4 says why
that is enough). The derived account id stays
`apps/common/swarm_common/identity.py::worker_service_account_id`. Only the
*project* half of the email changes under WD1(a), and that half is
configuration, not contract.

---

## 2. The provisioner

### 2.1 Where it runs

| | **(A) Cloud Run Job, run by swarm-api and a sweep** | (B) GitHub Actions workflow, dispatched by swarm-api | (C) Cloud Workflows execution |
|---|---|---|---|
| how it starts | swarm-api calls `run.jobs.run` on `swarm-workspace-provisioner` with **no overrides**, and a Cloud Scheduler job runs it every 5 minutes as a backstop. Pub/Sub cannot start a Job directly (that needs Eventarc → Workflows → Job), so a topic would add two hops and no isolation | swarm-api holds a GitHub token with `actions: write` and calls `workflow_dispatch` | swarm-api holds `workflows.executions.create` on one workflow |
| what swarm-api gains | `run.invoker` on **one job**. It can make the job run, not say what it does: the job reads its work from `workspaces/` | a credential that can start **any** workflow in the repository, the release included | invoker on one workflow, and it may pass arguments |
| identity | its own account (§2.3) | the WIF deployer, or a new WIF principal. Either way a GitHub-side identity holds GCP IAM admin | its own account |
| can it run `register-tenant.sh` and `kubernetes/render.py` unchanged? | yes: the image carries bash, gcloud, the pinned kubectl and the project's Python | yes | **no**: Workflows calls HTTP APIs, so the namespace render would be rewritten in YAML, a second definition of a tenant namespace (CLAUDE.md, "every rule that got restated in a second place here has since drifted") |
| logs | Cloud Logging, project-private | **public**: the repository is public, so every run's log would publish a new person's email and tenant id | Cloud Logging |
| failure modes | the GKE control plane must be reachable from the job (§2.5) | a GitHub outage blocks onboarding; a queued runner delays it by minutes | as (A), plus a second implementation |
| progress | per-step writes to `workspaces/` | the same, from a runner | native step state, but in Workflows rather than where the console reads |

**Recommendation: (A).** It is the only option in which the privileged
identity lives in GCP, logs stay private, the namespace keeps one definition,
and swarm-api's new power is to *start* a fixed program. Decision WD2 asks the
owner.

### 2.2 How a run proceeds

1. **Claim.** In one transaction, take the oldest `workspaces/` record whose
   `state == "requested"`, or whose `state == "provisioning"` with
   `claim.expires_at` in the past. Set `provisioning`, `claim`, and `attempt + 1`.
   At most **one** workspace is handled per execution, and the job's
   `parallelism` is 1. A run finding nothing exits 0 in about 2 seconds.
2. **Steps.** Run §4 in order. Each step reads before it writes and records
   `done` or `failed` on the record immediately. A crashed execution is
   resumed by the next claim from the first step that is not `done`.
3. **Finish.** P11 reads back every object. Only then does the run set `ready`
   and `ready_at`, and clear `claim`.
4. **Then loop** to step 1 until there is nothing to claim or the execution has
   run 20 minutes. The job's `task_timeout` is 30 minutes, and a claim lasts 15
   minutes and is renewed between steps.

Triggers: swarm-api, after each 202, and the sweep
`swarm-workspace-provisioner-sweep` every 5 minutes. The sweep's description
names `managed-by=swarm-terraform`, as the other Cloud Scheduler jobs do. It
also runs on a "refresh" mode once per release (§2.6).

### 2.3 Its service account and exact permissions

**Account:** `swarm-workspace-provisioner@<project>.iam.gserviceaccount.com`. It
is created by Terraform and holds **no key**. Only the job runs as it, and only
the deployer may `actAs` it (granted per account, as for every platform
account in `terraform/bootstrap/deployer_service_accounts.tf`).

Each grant is listed with what bounds it. **Bold** marks a grant that no
condition can narrow, with the reason.

**In `<idproject>` (WD1(a), recommended):**

| role | on | permissions | bound by |
|---|---|---|---|
| custom `swarmWorkspaceIdentityAdmin` | `<idproject>` | `iam.serviceAccounts.create`, `.get`, `.list`, `.getIamPolicy`, `.setIamPolicy`, `iam.serviceAccountKeys.list` | **the project.** Nothing else lives in `<idproject>`. The role has no `delete`, no `keys.create`, no `getAccessToken`, `signBlob` or `actAs`, and no `disable` |

**In `<project>`:**

| role | on | permissions | bound by |
|---|---|---|---|
| custom `swarmWorkspaceSecretBinder` | project, **conditioned** | `secretmanager.secrets.get`, `.getIamPolicy`, `.setIamPolicy` | `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-u-")`. This is the full-name form IAM evaluates, as `terraform/bootstrap/forge_user_slots.tf` already writes it. It reaches no group tenant's secret and none of the other team's |
| custom `swarmForgeSlotCreator` (exists) | project | `secretmanager.secrets.create` | **the project.** Creation is checked against the project, so no name condition applies. This is the same reasoning and the same role swarm-api already holds (forge_user_slots.tf). It can create an empty secret and nothing more |
| custom `swarmWorkspaceBucketIam` (`storage.buckets.get`, `storage.buckets.getIamPolicy`, `storage.buckets.setIamPolicy`) | **the artifact bucket only** (a bucket-level grant, not project) | read and set that one bucket's policy. Not `roles/storage.legacyBucketReader`: it carries `storage.objects.list`, which would list every tenant's object names | the bucket, plus, if W0 shows GCS honours it, `api.getAttribute("iam.googleapis.com/modifiedGrantsByRole", []).hasOnly(["roles/storage.objectViewer", "roles/storage.objectUser", "projects/<project>/roles/swarmBucketMetadataReader"])`. Without that attribute, the provisioner could grant any role on the artifact bucket (§2.4 residual R2) |
| `roles/resourcemanager.projectIamAdmin` | project, **conditioned** | get and set the project policy | `hasOnly([...])` over exactly the worker's three project roles: the worker Firestore custom role, `roles/logging.logWriter` and `roles/monitoring.metricWriter`. This is the pattern `terraform/bootstrap/deployer_conditions.tf` uses for the deployer, with its 10-element and no-OR rules. Unnecessary if W0 confirms the principal-set alternative (§2.4) |
| custom `swarmWorkspaceProvisionerFirestore` | project | `datastore.entities.get`, `.list`, `.create`, `.update`, `datastore.databases.getMetadata` | **the project.** Firestore ignores conditions on the data plane (`scripts/register-tenant.sh` §3 says why, measured 2026-09-16). It has no `delete`. Every tenant worker already has the same reach |
| `roles/container.clusterViewer` | project, **conditioned** | `container.clusters.get` (credentials and network) | `resource.name.startsWith("projects/<project>/locations/<region>/clusters/<swarm cluster>")`, the expression `terraform/modules/iam/bindings.tf` builds for the platform's own container grants. It has no IAM on the other team's cluster |
| `roles/iam.serviceAccountViewer` | **the two accounts** `swarm-scheduler` and `swarm-reconciler`, granted per account | `iam.serviceAccounts.get` | the two accounts. `kubernetes/apply.sh` resolves their `uniqueId` for the dispatcher RoleBinding. A project grant would let it describe every account in the shared project |
| `roles/iam.serviceAccountUser` | **its own account only** | `actAs` | needed only so the Cloud Run Job can run as it. It is held by the deployer, not by the provisioner |

**In the cluster (Kubernetes RBAC):** the user is the provisioner's GSA email.
The files are new: `rbac/provisioner-rbac.yaml` and
`policies/workspace-provisioner-scope.yaml` under kubernetes/.

| object | grants | why it is not enough on its own |
|---|---|---|
| ClusterRole `swarm-workspace-provisioner` + ClusterRoleBinding | `namespaces`: `get`, `create`, `patch`. `serviceaccounts`, `configmaps`, `resourcequotas`, `limitranges`, `networking.k8s.io/networkpolicies`, `rbac.authorization.k8s.io/roles`, `rolebindings`: `get`, `create`, `patch`. `roles`: `escalate`, and `bind` with `resourceNames: [swarm-worker, swarm-dispatcher, swarm-reaper]`. **No `delete` and no `list` of secrets.** It cannot read or create a `Secret`, `Pod` or `Job` | RBAC cannot restrict `create` by name, so a ClusterRole that creates namespaces creates *any* namespace, and the binding reaches every namespace |
| Role `swarm-workspace-provisioner-dns` in `kube-system` | `get` on `services` `kube-dns` and `daemonsets` `node-local-dns` (`resourceNames`) | these are the two reads `kubernetes/cluster-network.sh` makes |
| **ValidatingAdmissionPolicy `swarm-workspace-provisioner-scope`** + binding | for any request whose `request.userInfo.username` is the provisioner: a `Namespace` must be named `swarm-tenant-u-*`, and a namespaced object must be in a `swarm-tenant-u-*` namespace. A `Role` must be one of the three names, with the rules `kubernetes/render.py` renders (compared as a literal in the policy, which a parity test holds to the render). A `RoleBinding` may only reference those Roles, with subjects in its own namespace or the two control-plane identities | this is what turns "any namespace" into "`swarm-tenant-u-*` only". `kubernetes/policies/pod-security.yaml` is the precedent for ValidatingAdmissionPolicy in this cluster |

**What it can never touch on the shared deny-list** (`scripts/lib/common.sh`,
`SHARED_DENY_LIST`):

* **The other team's cluster:** its only `container.*` grant is conditioned to
  the swarm cluster's resource name. Its kubeconfig is built by
  `get-credentials` for that one cluster, inside the job, and `kubernetes/apply.sh`'s
  three cluster refusals run unchanged on every apply.
* **Their VPC, buckets and accounts:** it holds no `compute.*`. Its storage
  grant is on the artifact bucket's own policy. Its account-admin grant is in
  `<idproject>`, and in `<project>` it may only *view* two named platform
  accounts. Its secret grants are conditioned to `swarm-tenant-u-`.
* **Their secrets:** its only unconditioned secret permission is `secrets.create`,
  which can make an empty secret. The secret binder is prefix-conditioned.
* **The provisioner sources `scripts/lib/common.sh`**, as every script does, and
  runs `scripts/register-tenant.sh`, which refuses a cluster on the deny-list
  before applying anything.

### 2.4 The finding that shapes this: the shared project cannot bound account administration

The issue's constraint is "IAM conditions/prefixes that bound it to
`swarm-agent-worker-u-*` accounts". **That cannot be written in `<project>`.** A
worker account needs two kinds of binding on *its own* IAM policy:

* `roles/iam.workloadIdentityUser` for `[swarm-tenant-u-alice/swarm-agent-worker]`
  and `[.../swarm-worker]`;
* `roles/iam.serviceAccountUser` for the scheduler and the reconciler.

Writing them requires `iam.serviceAccounts.setIamPolicy` on the account. "IAM
resources don't provide the resource name" to a condition. This repository
established that in #334 and recorded it in
`terraform/bootstrap/deployer_service_accounts.tf` and in the
`terraform/bootstrap/variables.tf` validation that refuses project-level
`serviceAccountAdmin` for CI. Granting it per account is how the deployer copes,
but a per-account grant needs an owner-applied bootstrap run for each new
account. That is the operator step this issue removes.

So the provisioner can hold `setIamPolicy` on worker accounts only in one of
three ways (decision WD1):

* **(a) A dedicated identity project, `<idproject>` (for example
  `swarm-workspaces-dev`)**, holding only personal worker accounts. There the
  provisioner may administer every account, because every account there *is* a
  personal worker. Terraform makes three one-time grants there:
  * `roles/iam.serviceAccountUser` at `<idproject>` for `swarm-scheduler` and
    `swarm-reconciler`. They may then act as any personal worker, as they may
    for every tenant today.
  * `roles/iam.serviceAccountTokenCreator` at `<idproject>` for `<project>`'s
    Cloud Run service agent (cross-project job identity).
  * `roles/iam.serviceAccountUser` for the deployer.

  The provisioner then writes only the Workload Identity bindings. Cross-project
  use needs `iam.disableCrossProjectServiceAccountUsage` unenforced on
  `<idproject>`. That holds with no organisation (deploying-without-an-organisation.md),
  but W0 verifies it.
* **(b) The same project, with project-level `serviceAccountAdmin`.** The
  provisioner could then set the IAM of every account in the shared project,
  the other team's included, and grant itself token creation on any of them.
  **This breaks rule 2 and the #334 decision. It is listed only to be refused.**
* **(c) The same project, with the account IAM left to a release.** The
  provisioner does everything else and opens a `dev.tfvars` pull request for
  the account. Each new person then waits for the owner's bootstrap apply and
  dev-iam approval (§3.2). That is not operator-free.

**Residual risk under (a), stated so it is not mistaken for zero:**

* **R1.** A compromised provisioner can grant itself token creation on any
  personal worker in `<idproject>`, so it can act as any personal workspace. It
  cannot reach a group tenant's account, the control plane's accounts or the
  other team's accounts.
* **R2.** Bucket and project policy grants, `hasOnly` or not, constrain the
  *roles*, not the *members*. A compromised provisioner could grant the
  artifact bucket's object roles, or the worker Firestore role, to a principal
  of its choosing. That means reading other tenants' artifacts or Firestore
  documents. Two mitigations are proposed:
  * **Detective: an audit sweep.** A Cloud Logging metric and alert on every
    `SetIamPolicy` by the provisioner whose added member is not
    `serviceAccount:swarm-agent-worker-u-*@<idproject>.iam.gserviceaccount.com`.
    The provisioner's own P11 also re-reads the bucket and project policy.
  * **Preventive, to verify in W0:** grant the worker's three project roles and
    the bucket-metadata role once, by Terraform, to the principal set of
    `<idproject>`'s service accounts. If IAM's principal identifiers support
    that set in allow policies, the provisioner needs **no**
    `projectIamAdmin` at all. It would still need the bucket policy for the two
    *prefix-conditioned* object grants, which are per tenant by nature. If the
    set is not supported, the `hasOnly` grant above stands, with the alert.

### 2.5 Network and runtime

* **Image:** `swarm-workspace-provisioner`, built by `application.yml` like the
  others. It contains bash, gcloud with `gke-gcloud-auth-plugin`, the kubectl
  version `kubectl_bin` pins, the project's Python synced from the lockfile, and
  the repository's `scripts/` and `kubernetes/` at the release's commit. A
  provisioner always runs the namespace definition of the release that
  deployed it.
* **The GKE control plane must be reachable from the job.** The job uses Direct
  VPC egress into the platform subnet. Either the cluster's private endpoint or
  the master authorised networks must admit that subnet's range. W0 checks
  which, against `master_authorized_cidrs` in dev.tfvars.
* **Resources:** 1 vCPU, 1 GiB, `requests == limits` (invariant 7). It is not a
  worker, but the rule is the platform's. No Spot (invariant 6).

### 2.6 Keeping provisioned namespaces current

The release's namespace step refreshes the `swarm-spec-verify-keys` ConfigMap
in **Terraform's** namespaces only. Provisioned namespaces need the same
refresh, or a rotated step-spec key never reaches them. Workers there would
then refuse signed tasks as `CANNOT_START`. So:

* The release step gains a final action: execute
  `swarm-workspace-provisioner` with the env override
  `PROVISIONER_MODE=refresh` and `SPEC_VERIFY_KEYS_JSON` set to the same public
  ConfigMap JSON it already renders. The deployer holds
  `run.jobs.runWithOverrides` on that job alone. These are public keys, so
  nothing secret travels in the environment.
* In `refresh` mode the job re-applies the namespace with `kubernetes/apply.sh`
  for every `ready` workspace with `owner = "provisioner"`. It stamps
  `spec_keys_version` on each, and it prints the count of namespaces it
  visited and the count it expected (CLAUDE.md, "Empty output is not success").
* The step runs whether or not `APPLY_TENANT_NAMESPACES` is on. Personal
  namespaces were created by the provisioner, so refreshing them is not the
  "first apply from CI" that switch guards.

---

## 3. Terraform and the provisioner: who owns what

### 3.1 Today

Every tenant Terraform knows is a key of `var.tenants`. The tenancy module
creates the account and its IAM (additive `_iam_member` resources, never an
authoritative `_iam_policy` or a project or bucket `_iam_binding`; checked
2026-10-08). The secret_manager module creates per-provider secrets with
**authoritative** per-secret accessor bindings. The firestore module writes the
tenant and pool documents once (`terraform/modules/firestore/bootstrap.tf`,
`ignore_changes = [fields]`). The cloud_run_jobs module creates per-(tenant,
profile) jobs from `local.jobs`.

A tenant that is **not** in `var.tenants` is invisible to every plan.
Terraform neither creates it nor destroys it. Because every project and bucket
grant is additive, a runtime binding beside Terraform's is never reverted. That
is the property the split below relies on.

### 3.2 The options

| | **(T1) Split by kind: Terraform owns groups and service tenants, the provisioner owns human personal tenants** | (T2) The provisioner opens a `dev.tfvars` pull request | (T3) Terraform reads `workspaces/` |
|---|---|---|---|
| new privilege | the provisioner's (§2.3) | none: the release deployer already does it | none |
| time to ready | about 2 to 5 minutes | a PR, the owner's bootstrap apply from main for the new account (#334 ordering, `scripts/register-tenant.sh` §2b), dev-iam approval, a release: hours to days | a release |
| operator-free | yes | **no**: the owner approves each person | no |
| privacy | emails stay in Firestore | **each person's email is published** in a public repository's tfvars | as T1 |
| fight risk | none, if a human `u-*` tenant can never also be in `var.tenants` (validation below) | none | Terraform would own runtime records, and a removed record would be destroyed |
| teardown | by label and by record (§7) | `terraform` | `terraform` |

**Recommendation: T1**, with two guards so the split cannot be crossed by
accident:

1. **A validation in `terraform/infra/variables.tf`** refuses a new
   `kind = "user"` tenant whose principal is a human (not
   `*.iam.gserviceaccount.com`), unless it is in an explicit
   `grandfathered_personal_tenants` list, which holds `u-bogdan` (§3.3).
2. **The provisioner refuses** any workspace with `owner != "provisioner"`. It
   also refuses a tenant id that is a key of the Terraform-rendered list
   `TERRAFORM_TENANTS`, passed in the job's environment. This is the
   provisioner's side of the same line.

Runtime resources carry `managed-by=swarm-workspace-provisioner`. Secrets and
the namespace carry it as a label. A service account cannot carry labels, so
the account carries it in its description. The `swarm-tenant=<tenant>` label
or description goes with it. `make destroy`'s guard already aborts on what
lacks `managed-by=swarm-terraform`. The destroy runbook gains a provisioner
teardown before it (§7).

**What Terraform still manages for personal tenants:** the shared things they
use. That means the global, backend, runner and provider pools, the custom
roles, the artifact bucket and the cluster. The provisioner's own account, job,
sweep and grants come from W2. A personal tenant's `tenant:<id>` pool and
document are the provisioner's. Pool documents are already treated as
create-once by Terraform, so the provisioner writes them as `register-tenant.sh`
§6 does.

**One consequence to accept:** `aged_prefixes` in
`terraform/modules/storage/main.tf` lists Terraform tenants only. Its own
comment says personal tenants' task objects are not moved to Nearline. That
stays true. It is a storage-class cost difference, and deletion still applies.

### 3.3 Migrating what exists

| tenant | today | after |
|---|---|---|
| `u-bogdan` | in `dev.tfvars` (providers `anthropic`, 80/80). Account, secrets and jobs in Terraform state. Namespace made by hand on 2026-10-07 | **Grandfathered.** It stays in `dev.tfvars` and in `grandfathered_personal_tenants`. A one-time backfill writes `workspaces/u-bogdan` with `owner = "terraform"`, `state = "ready"`. The gate admits it, and the provisioner never touches it |
| `u-sw-c90291` (`swarm-verify`) | in `dev.tfvars`, a service account's tenant | unchanged. The gate exempts service accounts (§5.2) |
| `u-*` documents created by `ensure_tenant` on first sight (for example `u-admin`) | a tenant document and pool with no infrastructure behind them | the backfill writes no workspace record for them. Each such person sees "Create your workspace". The provisioner's P1 adopts the existing document, because its principal matches (`scripts/register-tenant.sh` §0 rules) |

Moving `u-bogdan` to the provisioner later is possible but not proposed. It
means `terraform state rm` of its account, IAM members, secrets and their
bindings, documents and jobs, run by the owner. A `removed` block cannot name a
`for_each` instance. Its account would also stay in `<project>`, where the
provisioner (rightly) has no power. Grandfathering costs nothing.

**`ensure_tenant` stops creating personal tenants.** Once the gate is on (W6),
`tenant_for` no longer writes `tenants/u-*` on first sight for a human caller.
It reads, and a create path then meets the gate. The provisioner's P10 becomes
the only writer of a new personal tenant document. Group tenants are unchanged.

---

## 4. From `register-tenant.sh` to provisioner steps

### 4.1 One definition, not two

The provisioner **runs `scripts/register-tenant.sh`**. It does not reimplement
it. The script already holds every guard this platform has learned:

* the derived-id assertion;
* the never-re-point check;
* the squat inspection of an existing account;
* the conditioned bucket grants and their version-3 policy handling;
* the deny-list refusals in `kubernetes/apply.sh`;
* the tenant document and pool written with `min(max_active, capacity_units)`.

A second implementation is how those would drift (decision WD3 weighs a Python
port). The script gains these flags, each also usable by an operator:

* `--progress-file <path>`: one JSON line per step (`{"step", "state", "code"}`)
  that the provisioner copies to the record. No command output goes in it, so
  nothing redactable is stored.
* `--identity-project <id>`: the account is created in `<idproject>`.
* `--grant-act-as`: the scheduler and reconciler `serviceAccountUser` grant that
  is missing today (§0). Under WD1(a) it is skipped, because the grant is
  project-wide in `<idproject>`.
* `--forge-slot <suffix>`: create the person's GitHub user slot and twin, and
  bind the worker to the slot (P9).
* `--runtime-owned`: skip §2b's deployer-grant check, which only matters for a
  Terraform tenant, and write `managed_by = swarm-workspace-provisioner`.

### 4.2 The steps

Every step reads first. "Done" means the object exists *as specified*, not
merely that a call returned 200. Retries are per step: 3 attempts with backoff
1, 4 and 16 seconds, on `429`, `5xx`, `UNAVAILABLE`, `DEADLINE_EXCEEDED` and
IAM's `409 concurrent policy change`. Anything else fails the step at once.

| id | script section | provisioner action | idempotent because | failure code |
|---|---|---|---|---|
| P1 | 0 | the derived id equals the record's id; an existing `tenants/{id}` has this principal and kind `user` | read only | `WORKSPACE_ID_TAKEN` (not retryable) |
| P2 | id length | `u-<slug>` fits the 11-character budget (`_MAX_TENANT_ID`). The frozen module guarantees it, so a failure here means drift | read only | `WORKSPACE_ID_INVALID` (not retryable) |
| P3 | 2 | create `swarm-agent-worker-u-alice` in `<idproject>`, with the description `managed-by=swarm-workspace-provisioner; swarm-tenant=u-alice` | `describe` first | `IDENTITY_CREATE_FAILED` |
| P4 | 2b | if the account already existed: its policy holds only the platform's bindings and it has no user-managed key. Otherwise refuse | read only | `IDENTITY_NOT_OURS` (not retryable; it names an operator) |
| P5 | 3 (Firestore, telemetry) | project bindings: worker Firestore role, `logWriter`, `metricWriter` (or nothing, if W0's principal set holds) | the script's own present-check per (role, member, condition) | `GRANT_FAILED` |
| P6 | 3 (GCS) | bucket bindings: `objectViewer` on `tenants/u-alice/` plus list prefix, `objectUser` on `tenants/u-alice/` except `verdicts/`, and bucket-metadata reader | `bucket_binding_present` compares role, member **and** expression | `GRANT_FAILED` |
| P7 | 5 (WI) | `workloadIdentityUser` on the account for `swarm-tenant-u-alice/swarm-agent-worker` and `/swarm-worker`, and act-as unless WD1(a) | an additive member; the policy is re-read | `GRANT_FAILED` |
| P8 | 5 | `kubernetes/apply.sh --tenant u-alice --gsa <email> --context <swarm> --cluster <swarm> --confirm`, plus `--spec-verify-keys` from the current key set | `kubectl apply` is declarative. A server dry-run precedes it | `NAMESPACE_APPLY_FAILED`. If the API server did not answer: `CLUSTER_UNREACHABLE` |
| P9 | 4 | create the empty GitHub user slot `swarm-tenant-u-alice-git-u-<16 hex>` and its `-refresh` twin, labelled `managed-by=swarm-api` (swarm-api writes their versions, as in onboarding.md §3.4). Bind the worker as accessor on the base slot only. No provider secret is created (§8) | `describe` first. The binding is checked before it is added | `SECRET_SETUP_FAILED` |
| P10 | 6 | write `tenants/u-alice` (with `managed_by`) and `pools/tenant:u-alice` with `hard_limit = min(max_active, capacity_units)`. `active` is set only at creation and never afterwards | `fs_patch` with the script's mask. A pool that exists keeps its `active` | `CONTROL_PLANE_WRITE_FAILED` |
| P11 | — | **verify**: re-read the account, its policy, the bucket and project bindings, every object the namespace render produces (`kubernetes/render.py::TENANT_FILES`), the slot binding, and the two documents. Then set `ready` | read only | `VERIFY_FAILED`, naming the object |

P9's slot name is deterministic: it is the provider suffix of the person's
email (`swarm-tenant-<tenant>-git-u-<16 hex>`, forge_user_slots.tf). So the
slot can exist, already bound, before the person connects GitHub. swarm-api's
later create must treat `ALREADY_EXISTS` on its own label as success. Phase W4
checks that against `apps/swarm-api/swarm_api/gittokens.py`. Under this
design, the per-tenant bootstrap bindings in `terraform/bootstrap/forge_user_slots.tf`
that give swarm-api version-add, version-manage and refresh-read on a tenant's
slots are made **once** for the `swarm-tenant-u-` prefix (still conditioned,
and still swarm-api only). They are not made per personal tenant. swarm-api is
one principal across every tenant, so one prefix binding grants it nothing a
per-tenant list does not.

The order matches the script's, and that order matters:

* The identity and its grants come before the namespace. `kubernetes/apply.sh`
  reads the account's IAM to decide whether to render the legacy KSA.
* The namespace comes before the tenant document. No dispatch can target a
  tenant whose document does not exist, so a half-made workspace is never
  dispatched into.

### 4.3 Failure copy, word for word

The API serves this copy with the code, as `apps/swarm-api/swarm_api/onboarding.py`
does for §2.3 of onboarding.md. `{step}`, `{attempt}` and `{request_id}` are
filled in. Nothing from a command's output is shown.

| code | retryable | copy |
|---|---|---|
| `IDENTITY_CREATE_FAILED`, `GRANT_FAILED`, `SECRET_SETUP_FAILED`, `CONTROL_PLANE_WRITE_FAILED` | yes | “Setting up your workspace stopped at {step} (attempt {attempt}). Nothing half-made can run: your work stays refused until every step is done. Press Try again; if it stops at the same step twice, an operator can see why under request {request_id}.” |
| `CLUSTER_UNREACHABLE` | yes | “Your workspace is made except its Kubernetes namespace, because the cluster did not answer. This is usually brief. It is retried on its own every 5 minutes, or press Try again.” |
| `NAMESPACE_APPLY_FAILED` | yes | “Your workspace's Kubernetes namespace could not be applied in full, so it is not isolated yet and nothing will run in it. Press Try again; an operator can see the reason under request {request_id}.” |
| `VERIFY_FAILED` | yes | “Every step reported success, but the final check could not find {object}. Nothing runs until it can. Press Try again.” |
| `WORKSPACE_ID_TAKEN` | no | “The workspace name that belongs to your account is already registered to a different identity. Nothing was changed. Ask an operator to look at request {request_id}; this needs a person, not a retry.” |
| `IDENTITY_NOT_OURS` | no | “An identity with your workspace's name already exists and carries access SwarmCloud never grants, so it was not adopted. Nothing was changed. An operator must look at request {request_id} before this can continue.” |
| `WORKSPACE_ID_INVALID` | no | “Your account's workspace name could not be formed. This is a platform fault, not yours: an operator must look at request {request_id}.” |

A retryable step is retried by the sweep up to 3 attempts on the same request,
because it re-claims a `provisioning` record whose claim has expired. After
that the record is `failed` and waits for the person's Try again. An operator
sees every failed request in the admin view, with the step, the code and the
`request_id`. The provisioner's log has the full, redacted reason under that
`request_id`.

---

## 5. The submission gate

### 5.1 Where

There are two methods: `apps/swarm-api/swarm_api/service.py::SubmissionService.submit_tasks`
and `apps/swarm-api/swarm_api/service.py::SubmissionService.submit_workflow`.
Every path that creates work reaches one of them:

* the task routes (single and batch);
* the workflow route;
* the issue run's planner task and plan approval in `routes/runs.py`;
* the repository indexer;
* the issue-CI and merge-wake workflows.

The check runs **first**, before validation, signing or any write, through
`SubmissionService.workspace_gate(ctx, tenant)`, which is new. It runs after
`tenant_for` resolves the tenant. Child tasks (`routes/children.py`) are
submitted by a running worker of a tenant that is, by construction, ready, so
they are not gated. A child of a non-ready tenant cannot exist.

The gate admits a submission when **any** of these holds:

1. the resolved tenant is a **group** tenant (`kind == "group"`). Groups are
   Terraform's;
2. the caller is a **service account** (the frozen pattern), or a listed
   continuation or rollup identity (`member_scope`, `is_rollup_sweeper`). Their
   tenants are Terraform's, and their callers cannot click a button;
3. `workspaces/{tenant_id}` exists with `state == "ready"`.

Otherwise it refuses.

The background submitters (issue CI, merge wake, the indexer) submit as an
`owner` context derived from a run that was itself admitted. They pass rule 3
because that run's tenant is ready. If a workspace were ever un-readied
(deprovisioning, §7), they would be refused like a person, which is correct.

### 5.2 The response

```
HTTP/1.1 403
{"code": "WORKSPACE_NOT_READY",
 "message": "Your SwarmCloud workspace is not ready yet, so no task or workflow can start. Finish setup: create your workspace.",
 "detail": {"tenant_id": "u-alice", "state": "provisioning", "setup_url": "https://<console>/setup#workspace",
            "setup_command": "/sc:setup"}}
```

`WorkspaceNotReady(Forbidden)` is a new class in `apps/swarm-api/swarm_api/errors.py`
with the upper-case code the owner wrote. The onboarding codes are upper-case
too, though `ApiError`'s other codes are lower-case. `state` is `none` when
there is no record. The `message` changes with the state: "is being created" for
`requested`/`provisioning`, and "could not be created: {copy}" for `failed`.

### 5.3 Workflows are refused whole

`submit_workflow` checks before compiling any step, so a refused workflow
creates **no** workflow document, no task and no event. The gate is per
submission, not per step, so a workflow's steps can never be split between
admitted and refused. The plan-approve path refuses the approval and leaves
the run awaiting approval, so approving again once the workspace is ready
works.

### 5.4 Why the gate is at submission and not also at admission

A task can only exist if it was submitted, and the gate refuses submission
until `ready`. So, absent a bypass, no `QUEUED` task belongs to a non-ready
workspace, and admission has nothing to check. Invariants 1 to 3 are untouched:
a refused submission creates nothing and holds nothing. Deprovisioning (§7) is
the one way a ready tenant could stop being ready with tasks queued, and it
must drain or cancel them first. That is the follow-up's problem, and the
reason it is a follow-up.

The gate is introduced behind `WORKSPACE_GATE` (`off` → `on`). It reads
`workspaces/` only when on, so W1 can merge before the provisioner exists.

---

## 6. The console, the plugin and the API

### 6.1 The console's setup checklist

The checklist (`apps/swarm-ui/src/GitHubConnect.tsx`, from onboarding.md §2)
gains **`workspace`** as its second step, after `signed_in`. A person can do
everything else in parallel. The step's state comes from `GET /v1/onboarding`,
which reads the workspace record (`apps/swarm-api/swarm_api/onboarding.py`
gains the step).

```
Set up SwarmCloud                                              2 of 6 done
 ✓ Signed in as alice@saga.xyz
 ○ Create your workspace
   Your own isolated space to run agents in: an identity, a storage area and
   a Kubernetes namespace that only your work uses. It takes 2–5 minutes.
   Until it exists you can look around and connect GitHub, but nothing runs.
   [ Create my workspace ]
 ○ Connect GitHub
 ○ Enable your organisations
 ○ Choose repositories
 ○ Verify access
```

While provisioning (the console polls `GET /v1/workspace` every 3 seconds while
the page is open):

```
 ◐ Creating your workspace — u-alice                       attempt 1, 1m 12s
   ✓ Identity           swarm-agent-worker-u-alice
   ✓ Access grants      storage, database, logs
   ◐ Namespace          swarm-tenant-u-alice
   ○ Control plane
   ○ Final check
   You can carry on with the steps below meanwhile.
```

Failed:

```
 ✕ Creating your workspace stopped at Namespace (attempt 3)
   Your workspace is made except its Kubernetes namespace, because the
   cluster did not answer. This is usually brief. It is retried on its own
   every 5 minutes, or press Try again.
   [ Try again ]   request 6f1c…
```

The Submit screens (`apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/SubmitWorkflow.tsx`)
render a `WORKSPACE_NOT_READY` refusal as a banner with the API's `message` and
a "Finish setup" link to `setup_url`. They do not disable the form ahead of
time: the server is the authority, and a stale client must not guess.

### 6.2 The plugin

`/sc:setup` (`plugin/commands/setup.md`) and `sc setup`
(`apps/swarm-mcp/swarm_mcp/sc.py::run_setup`) gain the step at the same place.
There is a new bridge tool, `swarm_setup_workspace`, in
`apps/swarm-mcp/swarm_mcp/server.py`. It posts the request and then reports
the record. It does not wait inside the tool. `/sc:setup` re-reads status as
the existing steps do.

```
$ /sc:setup
SwarmCloud setup — alice@saga.xyz
  ✓ signed in
  ○ workspace          not created
  ○ github             not connected
  …
Create your workspace now? It makes an identity, a storage area and a
Kubernetes namespace that only your work uses (2–5 minutes). [Y/n] y
  ◐ workspace          requested (u-alice) — carrying on with GitHub meanwhile
  ○ github             not connected
  → open https://github.com/login/oauth/authorize?… to connect GitHub as yourself
…
  ✓ workspace          ready (u-alice, 3m 05s)
  ✓ github             connected as alice-gh
Ready. Try: /sc:run "…" in a granted repository.
```

A plugin submission before `ready` prints the API's message and the command:

```
$ /sc:run "fix the flaky test"
✕ 403 WORKSPACE_NOT_READY: Your SwarmCloud workspace is being created, so no
  task or workflow can start yet. Finish setup with /sc:setup (state: provisioning).
```

### 6.3 Routes (additive)

| route | who | does |
|---|---|---|
| `GET /v1/workspace` | any signed-in human | the caller's record (always their own `u-` id), or `{state: "none"}` |
| `POST /v1/workspace` | the same | the request of §1.3. 202 means accepted, 200 means already in progress or ready, 403 or 409 as listed there |
| `GET /v1/admin/workspaces` | admin | every record not `ready`, with step, code and `request_id` |
| `GET /v1/onboarding` | (exists) | gains the `workspace` step |

None takes or returns a credential, an image, a command or a resource spec
(invariant 10). Limits are the provisioner's defaults (§8). A person cannot
choose them, and an admin changes them afterwards with the existing tenant
limits route.

---

## 7. Deprovisioning (a follow-up, not built now)

When a person leaves, these steps run in order. The order exists so that
nothing is left that runs, and nothing is deleted while it still runs:

1. Set the tenant's `enabled = false`. `tenant_for` then refuses new work.
2. Cancel or drain its work, following `docs/runbooks/tenant-offboarding.md`.
3. Remove the bucket and project bindings.
4. Delete the namespace.
5. Disable, then later delete, the account.
6. Disable the slot versions.
7. Delete the slot secrets after the retention the owner sets.
8. Archive the artifacts under `tenants/u-<id>/`, by the bucket lifecycle.
9. Mark the record `deprovisioned`.

Deprovisioning needs `delete` permissions the provisioner deliberately lacks
(§2.3). So it is **a separate identity or an owner-run script**, decided then.
The provisioner never gains a destructive permission. This design only
reserves the state name `deprovisioned`, and the label and description that
let teardown find every runtime-created object.

---

## 8. Cost and limits for a new personal workspace (proposed; the owner decides, WD5)

| setting | proposed default | today's comparison | why |
|---|---|---|---|
| `max_active` | **8** | `default_tenant_max_active = 20` (`apps/common/swarm_common/config.py`); `u-bogdan` 80; the verify tenant 4 | a person's interactive work. 8 people at the ceiling hold 64 of claude-code's `runner:claude-code` 80 |
| `capacity_units` | **8** | default 40; the verify tenant 8 | the pool is `min(max_active, capacity_units)` = **8 units** |
| `providers` (`credentials`) | **none** | `u-bogdan`: `anthropic` | The workspace runs on the person's own pool account (Accounts page) or one lent to them. No provider secret is created, so there is no key nobody stores. Until either exists, claude-code tasks park `CREDENTIAL_MISSING` (WD6) |
| namespace ResourceQuota | **`--quota-pods 16 --quota-cpu 64 --quota-memory 128Gi --quota-jobs 64 --quota-ephemeral 160Gi`** | `kubernetes/render.py` defaults: pods 100, cpu 400 | with `requests == limits`, this is 8 claude-code pods at 4 vCPU, plus headroom for jobs finishing within their TTL. A per-tenant quota above its pool would be dead weight |
| `monthly_budget_usd` | none | none: no per-tenant budgets are built or planned (owner, 2026-10-01) | — |

**What a workspace costs while idle: nothing.** That follows from invariant 1.
A ready workspace with no `LEASED` work holds no capacity. The account, the
namespace, the bindings and the empty secrets are free or
fractions-of-a-cent-a-month objects (secret versions are billed per active
version, and the slots have none until GitHub is connected).

**What a busy one costs:** at most 8 claude-code pods. At 4 vCPU each on
Autopilot that is **32 vCPU while all 8 run**, and it is bounded by the
pool, not by the namespace. The provisioner itself costs about 1 vCPU-minute
per workspace, plus an empty run of a few seconds every 5 minutes. That is 8,640 runs and
roughly 5 vCPU-hours a month, under a dollar.

**Shared-pool interaction:** `var.pool_limits.providers.anthropic` must be at
least *(Terraform tenants holding the provider) × `provider_tenant`*
(`terraform/infra/variables.tf`). Runtime tenants are not counted, and with
`providers = []` they hold no `provider:anthropic:tenant:*` pool. Their
claude-code work is bounded by the account pool and by the shared
`provider:anthropic` pool, which is the backstop. A new person cannot push the
platform past its shared ceilings, because every reservation is all-or-nothing
across every pool (invariant 2).

---

## 9. Open decisions for the owner

### WD1. Where do personal worker identities live?

* **(a) A dedicated identity project `<idproject>`**, which the owner creates.
  The provisioner administers accounts there only. This is the only option that
  is operator-free *and* keeps account-admin power away from the shared
  project.
* **(b) The shared project, with project-level `serviceAccountAdmin`.** This
  breaks rule 2 and #334. It is not recommended.
* **(c) The shared project, with each account's IAM done by a release**
  (§2.4(c)). There is no new admin power, but every new person waits for the
  owner's bootstrap apply.

**Recommendation:** (a). It is the only option that satisfies both "no
operator" and "never touch the other team's resources".

### WD2. What runs the provisioner?

* **(a) A Cloud Run Job**, run by swarm-api (`run.invoker` on that job only)
  and by a 5-minute sweep.
* **(b) A GitHub Actions workflow** dispatched by swarm-api. Its logs are
  public, and swarm-api would hold a token that can start any workflow.
* **(c) Cloud Workflows.** The namespace render would have to be re-implemented.

**Recommendation:** (a) (§2.1).

### WD3. Does the provisioner run `register-tenant.sh`, or a port of it?

* **(a) Run the script** with five new flags (§4.1). There is one definition of
  a tenant, and its guards are already tested.
* **(b) Port it to a Python package.** It would be easier to unit-test per
  step, but it is a second definition beside the script operators still run.
* **(c) Port it and delete the script's personal-tenant path.** There is one
  definition again, but `--user` registration by an operator goes away.

**Recommendation:** (a). This repository's experience is that a restated rule
drifts.

### WD4. Who owns which tenants?

* **(a) T1:** Terraform owns groups and service tenants, the provisioner owns
  human personal tenants, and `u-bogdan` is grandfathered.
* **(b) T1, and migrate `u-bogdan`** to the provisioner with an owner-run
  `terraform state rm` (§3.3).
* **(c) T2:** every workspace becomes a `dev.tfvars` pull request.

**Recommendation:** (a). (b) moves a working tenant for no gain, and (c)
publishes emails and needs the owner for each person.

### WD5. A new workspace's limits?

* **(a) 8 / 8, quota 16 pods / 64 vCPU** (§8).
* **(b) Today's API default, 20 / 40** (pool 20, quota 80 vCPU).
* **(c) 4 / 8, like the verify tenant.**

**Recommendation:** (a). It is enough for one person's parallel work, and 10
people at the ceiling still fit under claude-code's 80.

### WD6. What does a ready workspace run on?

* **(a) Nothing until the person adds or borrows an account.** The checklist
  gains an "Add a Claude account" hint after `workspace`.
* **(b) The provisioner lends a platform pool account by default.** It works at
  once, but every new person shares the platform's subscriptions.
* **(c) The provisioner creates an empty `anthropic` secret** and the person
  stores a key with `create-secrets.sh`, which needs an operator.

**Recommendation:** (a). The gate is about isolation, and lending is a cost
decision the owner should make per person.

### WD7. Must a member of a group tenant also have a personal workspace before submitting?

* **(a) No.** The gate is on the tenant the submission resolves to, and a group
  tenant is Terraform's and always ready.
* **(b) Yes**, for everyone, before anything else.

**Recommendation:** (a). It refuses nothing that can run safely today.

### WD8. When does the gate turn on?

* **(a) Once W4 has provisioned one workspace end to end in dev** and the
  backfill has run.
* **(b) Now (with W1)**, accepting that new people cannot run until the
  provisioner ships.

**Recommendation:** (a). Turning it on before anything can satisfy it would
refuse every new person with no way forward.

### WD9. Bucket and project grants: role-bounded, or member-bounded?

* **(a) `hasOnly` role conditions plus an alert** on any member outside
  `swarm-agent-worker-u-*` (§2.4 R2).
* **(b) A principal-set grant**, made once by Terraform, so the provisioner
  needs no project IAM at all. This holds only if W0 confirms IAM supports it.
* **(c) A Google group of personal workers**, with the provisioner as the
  group's manager. This needs a Workspace admin and Cloud Identity API access,
  which this project has found unreliable (`searchTransitiveGroups` 403s).

**Recommendation:** (b) if W0 confirms it, else (a).

---

## 10. Build plan

Within a phase no file is in two lanes, and a lane depends only on earlier
phases. New files are named without their root.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| W0 | 0 | **verification, no code**: principal-set allow grants (WD9b); `modifiedGrantsByRole` on bucket `setIamPolicy`; cross-project Cloud Run job identity and Workload Identity to `<idproject>`; the job's network path to the control plane; `ALREADY_EXISTS` handling of a pre-made slot. Each result is dated in this document | `docs/workspaces.md` | owner's answers to WD1–WD9 |
| W1 | 1 | the record, `GET`/`POST /v1/workspace`, the admin list, the `workspace` onboarding step, `WorkspaceNotReady`, and the gate behind `WORKSPACE_GATE=off`. `tenant_for` stops creating `u-*` once the gate is on | new `swarm_api/workspaces.py`, new `swarm_api/routes/workspaces.py`, `apps/swarm-api/swarm_api/service.py`, `apps/swarm-api/swarm_api/errors.py`, `apps/swarm-api/swarm_api/onboarding.py`, `apps/swarm-api/swarm_api/main.py`, `apps/swarm-api/swarm_api/settings.py` | W0 |
| W2 | 1 | **dev-iam**: `<idproject>` wiring; the provisioner's account, custom roles (bootstrapped as `docs/runbooks/custom-roles-to-bootstrap.md` says) and conditioned grants; the job, the sweep, swarm-api's `run.invoker` on the job; the deployer's `runWithOverrides` on it; the `swarm-tenant-u-` prefix bindings for swarm-api's slots; the Firestore index on `workspaces` (`state`, `requested_at`); the validation of §3.2; the `terraform test` assertions | new `terraform/modules/workspace_provisioner/`, new `workspace_provisioner.tf` in terraform/bootstrap, `terraform/bootstrap/forge_user_slots.tf`, `terraform/infra/main.tf`, `terraform/infra/variables.tf`, `terraform/environments/dev/dev.tfvars`, `terraform/modules/firestore/`, new `workspace_provisioner.tftest.hcl` in tests/terraform | W0 |
| W3 | 1 | **dev-iam (cluster)**: the provisioner's ClusterRole, binding and kube-system Role, and the scope ValidatingAdmissionPolicy, applied with `apply.sh --policies`, plus a parity test that the policy's Role literals equal the render | new `rbac/provisioner-rbac.yaml` and `policies/workspace-provisioner-scope.yaml` in kubernetes/, `kubernetes/render.py` (`POLICY_FILES`) | W0 |
| W4 | 2 | the provisioner: claim loop, step runner, progress, refresh mode, image; `register-tenant.sh` flags (§4.1), including the act-as grant missing today | new `apps/workspace-provisioner/`, new `images/workspace-provisioner/Dockerfile`, `scripts/register-tenant.sh`, `.github/workflows/application.yml` | W1, W2, W3 |
| W5 | 2 | the console step, the progress view and the Submit banner; `sc setup`, `swarm_setup_workspace`, `/sc:setup`. Issue forms follow if a tab changes | `apps/swarm-ui/src/GitHubConnect.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/SubmitWorkflow.tsx`, `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/server.py`, `plugin/commands/setup.md` | W1 |
| W6 | 3 | the release's provisioned-namespace refresh (§2.6); smoke covers a fresh workspace in dev; the backfill (`u-bogdan` as `owner: terraform`); `WORKSPACE_GATE=on` (WD8) | `.github/workflows/release.yml`, `scripts/smoke-test.sh`, new `backfill-workspaces.sh` in scripts/ | W4, W5 |
| W7 | 4 | docs: multi-tenancy, offboarding runbook (runtime teardown by label), onboarding | `docs/multi-tenancy.md`, `docs/runbooks/tenant-offboarding.md`, `docs/onboarding.md` | W6 |
| — | later | deprovisioning (§7), with its own identity | — | the owner's decision then |

**W2 and W3 need the owner's dev-iam approval.** W2 also needs an owner-run
bootstrap apply for the custom roles and the creation of `<idproject>`. W3 is
an owner-run `kubernetes/apply.sh --policies --confirm`. W2, W3 and W4 get the
one review (credentials, tenant isolation, IAM). W1 gets it too, for the gate.

---

## 11. Invariants, each with how it holds

1. **Demand only from `LEASED`…`RUNNING`.** Provisioning creates no task and no
   lease. A ready, idle workspace holds nothing, and the provisioner is not a
   worker.
2. **All-or-nothing reservation.** Unchanged. The provisioner writes a pool
   document once, with `active = 0`, and never changes `active`.
3. **Concurrency from `LEASED`.** Unchanged.
4. **Workers never sleep through a wait.** The provisioner is not a worker. It
   does not wait inside an execution for a workspace (one claim, bounded steps,
   resumable), and no tool call in the plugin waits either.
5. **Fencing.** Unchanged. The claim lease on a record is the provisioner's own
   fencing: a stale execution's write is refused by the transaction that
   compares `claim.execution`.
6. **No Spot.** The job declares none.
7. **`requests == limits`.** Both the job and the personal ResourceQuota follow
   it (§2.5, §8).
8. **Checkpointing.** Unchanged for workers.
9. **Isolation.** This is the point of the design. Every workspace gets its own
   account, its own prefix-conditioned grants, its own namespace with
   default-deny networking, and its own slot. These are the same objects
   `register-tenant.sh` makes, made by the same code.
10. **Profiles by name.** No new route accepts an image, a command, a resource
    spec or a backend parameter. The workspace routes take no body fields that
    shape infrastructure.
