# Personal workspaces: approved by an admin, applied by a guarded cloud job

**Status: DESIGN, final pass 2026-10-08 with every owner decision (part of
#847). None of it is built.** The owner asked on 2026-10-08 for each person's
personal space to be "onboarded at the time of onboarding a new user in
swarmcloud", with "a way to create this via the UI and potentially via the
plugin". They also asked that it "should block starting any task or workflow
before this exists".

The first version of this document proposed a locked-down runtime
provisioner. The owner's decisions of 2026-10-08, posted on #847 and on this
design's pull request, replaced it, and his later decisions that day on WD2,
WD3 and WD9 replaced the GitHub Actions and Terraform-module version of the
apply. **This document describes the chosen design throughout**; §9 records
every decision with its options, for history.

* **Identities stay in the shared project (WD1 (c)).** There is **no**
  privileged runtime provisioner holding account-admin power.
* **Approval is one click by any admin**, in a new console screen,
  **Admin → People**. That click is the only human step. swarm-api then
  publishes the workspace id to Pub/Sub, and a **Cloud Build job in the
  project** (WD2) runs `scripts/register-tenant.sh --workspace <w-id>` (WD3) as
  a **dedicated identity**, `swarm-workspace-deployer`, behind a **call guard**
  that wraps every cloud and cluster call the script makes and allows only
  creations of *that workspace's own* resources. Anything else stops the run
  and asks the owner.
* **The list of approved people is private.** The repository is public, so
  the list lives in Firestore, and Terraform never reads it. The job's logs are
  in the project's Cloud Logging, not a public CI log. Anything public names a
  person only by an **opaque workspace id**, `w-` and six random hex digits
  (`w-3f9a2c`).
* **Before the workspace is ready**, the person may sign in, connect GitHub,
  choose repositories and look around. Every task or workflow submission that
  resolves to their personal tenant is refused with `403 WORKSPACE_NOT_READY`
  and a link to the setup step, on the console, the API and the plugin. Team
  (group) submissions are never blocked (WD7).

This document sits beside [onboarding.md](onboarding.md), which covers GitHub
access (#780). That flow gives a person a forge credential. This one gives them
somewhere to run. Frozen-contract requests go in
[contract-change-requests.md](contract-change-requests.md). This design needs
none (§1.4).

What it settles, one line each:

* **A workspace is a record, `workspaces/{tenant_id}`**, carrying the opaque
  `workspace_id`. It moves through `requested`, `approved`, `applying` and
  `ready`, with `denied`, `needs_owner` and `failed` beside them (§1).
* **The apply is a Cloud Build job**, started by a Pub/Sub message holding only
  the workspace id. Its trigger builds only `main`, and it is the only thing
  that can run as `swarm-workspace-deployer` (§2.1, §2.4).
* **The call guard is the bound.** IAM cannot narrow service-account
  administration to a name prefix in the shared project (§2.4), so the
  identity's power is bounded by what the guard lets the script call: creations
  and bindings of one workspace's resources, with every member and condition
  checked (§2.5). The owner accepted the residual risk with three safeguards
  (§2.4).
* **Terraform owns group and service tenants only.** Personal workspaces are
  made by the script and live outside Terraform state; nothing in Terraform may
  plan to change or destroy them, and a `terraform test` holds that (§3).
  `u-bogdan` moves out of state with a plan that destroys nothing (§3.3).
* **The database and bucket-metadata roles are granted once**, to the set of
  all personal worker identities, if W0 confirms IAM supports it (WD9, §2.3).
* **A ready workspace runs nothing until the person adds a Claude account or
  an admin lends one** (§5, WD6).
* **One gate, in the two submission methods every create path goes through**,
  shipped off and turned on after one real end-to-end approval (§5, WD8).

The names below are placeholders where a real one is not needed.
`alice@saga.xyz` is a new person, their tenant is `u-alice`, and their
workspace id is `w-3f9a2c`. `<project>` is the platform's project and
`<number>` is its project number.

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
* **A new account needs the owner twice.** `terraform/bootstrap/deployer_service_accounts.tf`
  grants the release deployer `roles/iam.serviceAccountAdmin` per account,
  because the project-level grant was refused in #334. The account must exist
  before that grant, so a new tenant today is: `register-tenant.sh` creates the
  account, the owner applies the bootstrap from `main`, then the release's IAM
  plan waits in `dev-iam` for the owner's approval
  ([ci.md](ci.md#a-new-account-exists-before-the-release-that-adds-it)).
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
  run. Personal workspaces are made by this script (WD3), so its new
  `--workspace` mode makes the grant, for the scheduler and the reconciler
  (§4.1, A4). The operator's `--group` and `--user` paths keep the gap; it is
  reported as an out-of-territory finding on the PR.
* **Admin is configuration.** `apps/swarm-api/swarm_api/settings.py` reads
  `admin_groups` and `admin_users`. Changing who is an admin needs a deploy,
  and nothing records who changed it or when.
* **A personal tenant runs on nothing until someone gives it a credential.** It
  needs its own provider key, or a pool account owned by or lent to it
  (`apps/quota-broker/quota_broker/accounts.py::accounts_serving`; quota-management.md
  §4). Otherwise its claude-code tasks park `CREDENTIAL_MISSING`. A ready
  workspace does not change that, and WD6 turns it into a refusal at submission
  (§5).

---

## 1. The request record

### 1.1 `workspaces/{tenant_id}`

This is a new collection, private to the platform. It is not a field on
`Tenant`, because `Tenant` is in the frozen contract
(`apps/common/swarm_common/models.py`). The document id is
`apps/common/swarm_common/identity.py::tenant_id_for_user` of the requester's
verified email. That is the same function the API resolves a caller with, so
the record is keyed by the only id that person can ever resolve to.

**This collection is the private list** the owner's privacy decision asks for
(§2.6). Nothing copies it into the repository or into Terraform; the Cloud
Build job reads one record from it by workspace id.

| field | type | written by | meaning |
|---|---|---|---|
| `tenant_id` | string | swarm-api | `u-alice`; equals the document id. **Private**: it is derived from the email, so it is a name |
| `workspace_id` | string | swarm-api | `w-3f9a2c`: `w-` and 6 hex digits from `secrets.token_hex(3)`, **random, not derived from the email**, so nobody can recover it by hashing a guessed address. It is unique (§1.3). This is the only id that appears in anything public |
| `principal` | string | swarm-api | `alice@saga.xyz`, lower-cased, from the verified token, never from the body |
| `state` | string | swarm-api, the job | §1.2 |
| `request_id` | string | swarm-api | a fresh UUID for each accepted request or retry; the job logs it, privately |
| `requested_at`, `requested_via` | timestamp, string | swarm-api | `console`, `plugin` or `api` |
| `decision` | map or null | swarm-api | `{by, at, verdict: approved/denied, reason}`. `reason` is required for a denial and is shown to the person. `by` is an admin's email; it never leaves Firestore |
| `limits` | map | swarm-api | `{max_active, capacity_units, quota_pods, quota_cpu}`. Defaults from §8; an admin raises them in People (§6.4) |
| `run` | map or null | swarm-api, the job | `{build_id, attempt, published_at, mode}` of the current Cloud Build run. The build's page is in the project's console, readable only by those with access to the project's builds and logs (§2.6) |
| `steps` | map | the job | `step id → {state: todo/running/done/failed/held, at, code}`, using the ids in §4.2 |
| `failure` | map or null | the job | `{step, code, retryable, at}`. The copy is served from the code (§4.3), never stored free-form, so no command output reaches Firestore |
| `ready_at` | timestamp | the job | written by the verify step only |
| `migrated` | bool | the migration (§3.3) | `true` for `u-bogdan`, whose resources predate the record |

### 1.2 States

| state | entered when | the person sees | submissions to their own tenant |
|---|---|---|---|
| (no record) | never requested | checklist step "Request your workspace", button enabled | refused, `WORKSPACE_NOT_READY` |
| `requested` | swarm-api accepted a request | "Waiting for an admin to approve" | refused |
| `denied` | an admin denied it | "Not approved: {reason}", and "Request again" | refused |
| `approved` | an admin approved it; the dispatch is being made | "Approved. Setting up…" | refused |
| `applying` | the job claimed the record | each step of §4.2 with a tick or spinner | refused |
| `needs_owner` | the call guard stopped a call (§2.5) | "Approved. A change needs the platform owner's review before it can finish." | refused |
| `failed` | a step failed past its retries | the step's code and copy (§4.3) | refused |
| `ready` | the verify step read back every object | the step is ticked; the Claude account step comes next | admitted once a Claude account exists (§5) |

`ready` is **evidence-derived on the job's side**: it is written only
after the final verification step (A9) has read back every object. swarm-api
never writes `ready`, and nothing a client sends can set it. The same rule
applies in `apps/swarm-api/swarm_api/onboarding.py::derive`: a step's state
comes from evidence, never from a flag a client sets.

### 1.3 Who may request, who may approve, and idempotency

`POST /v1/workspace` (§6.3) accepts a request only when **all** of these hold.
Otherwise the response is 403 with a code:

1. The caller has a verified Google identity in an allowed domain (`saga.xyz`).
   This is the same `assert_allowed_domain` every route uses.
2. The caller is a **human**: the address does not match the frozen
   service-account pattern in `apps/common/swarm_common/identity.py`. A service
   account's tenant is in `dev.tfvars` (`u-sw-c90291` for `swarm-verify`).
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

* **No record:** draws a `workspace_id`, creates `workspace_ids/{w-…}` (a
  one-field document holding the tenant id, whose creation fails if the id is
  taken, so a collision draws again), creates the record in `requested`, and
  answers 202.
* **`requested`, `approved`, `applying`, `needs_owner` or `ready`:** writes
  nothing and answers 200 with the record. Clicking twice, or clicking in the
  console and then in the plugin, is the same request.
* **`denied`:** moves it back to `requested` with a new `request_id`, keeping
  the previous `decision` in a `history` list so the admin sees the earlier
  reason. It answers 202. A new request is refused for 24 hours after a denial
  (`WORKSPACE_REQUEST_TOO_SOON`); an admin may still approve the denied record
  at any time (confirmed by the owner, 2026-10-08).
* **`failed`:** answers 409 with the failure copy. A retry is an admin's
  action (§6.4), because it starts a run of a privileged identity.

**Approve and deny** are `POST /v1/admin/workspaces/{workspace_id}/approve`
and `/deny` (§6.3). Both need `is_admin` (§6.5). Approve is accepted only from
`requested`; deny from `requested` or `failed`, and it needs a non-empty
reason of at most 500 characters. Each writes `decision` and an
`admin_audit` entry in the same transaction. An admin may approve their own
request: the call guard bounds what any approval can create, so a self-approval
creates nothing a different admin's approval would not, and the audit shows it
(confirmed by the owner, 2026-10-08).

After an approval, swarm-api publishes the workspace id to Pub/Sub (§2.1). A
failed publish is not a failed approval: the record stays `approved` and the
dispatch sweep retries it (§2.2).

### 1.4 Frozen contract

None is needed. `workspaces/`, `workspace_ids/`, `people/`, `admin_roles/` and
`admin_audit/` are new collections read only by swarm-api and the job.
`Tenant` gains no field, and the scheduler's admission does not read the
workspace (the gate is at submission; §5.4 says why that is enough). The
derived ids stay `identity.py::tenant_id_for_user` and
`identity.py::worker_service_account_id`.

---

## 2. The guarded apply

### 2.1 What triggers it (decided, WD2)

**Approval publishes to Pub/Sub, and a Cloud Build job in the project does the
work** (owner decision WD2, 2026-10-08). The job's logs therefore stay in the
project's Cloud Logging, not in a public GitHub Actions log.

* **The topic.** `swarm-workspace-apply`, in the platform's project. After an
  approval (or a retry, a limits change, or the sweep) swarm-api publishes one
  message whose data is `{"workspace_id": "w-3f9a2c", "mode": "create"}`
  (`mode` is `create` or `limits`). swarm-api gains `roles/pubsub.publisher` on
  that one topic and nothing else. The message names nobody: the workspace id
  is opaque (§1.1).
* **The trigger.** A Cloud Build trigger of the Pub/Sub kind,
  `swarm-workspace-apply`, subscribed to that topic. It builds **only `main`**:
  its source is this repository at `refs/heads/main`, and its build file,
  `scripts/cloudbuild/workspace-apply.yaml`, is read from `main` too. The
  message's two fields reach the build as the substitutions `_WORKSPACE_ID` and
  `_MODE`, and nothing else from the message is used.
* **The identity.** The trigger runs as `swarm-workspace-deployer` (§2.3), and
  that trigger is the only thing that may (§2.4).
* **Who made it.** The topic, the trigger and the identity are created by the
  owner's bootstrap apply (`terraform/bootstrap/`), once. The repository's
  connection to Cloud Build is the owner's one-time step (§10).

What the job does is one script: `scripts/register-tenant.sh --workspace
w-3f9a2c` (WD3, §4), with every cloud and cluster call it makes passing through
the call guard (§2.5).

The options considered (GitHub Actions dispatched by a single-purpose App, a
scheduled GitHub workflow polling Firestore, and this one) are kept in §9 WD2,
for history.

### 2.2 How a run proceeds

The build file has three build steps, all in one image pinned by digest that
carries bash, gcloud, kubectl with `gke-gcloud-auth-plugin`, jq, curl and
python3 (W0 confirms the stock `google-cloud-cli` image has them all; if not,
the release builds a small one).

1. **Validate.** `_WORKSPACE_ID` must match `^w-[0-9a-f]{6}$` and `_MODE` must
   be `create` or `limits`, before anything else runs. A malformed message ends
   the build and writes nothing.
2. **Install the guard.** The step puts `scripts/lib/guard-bin/` first on
   `PATH` and exports `SWARM_CALL_GUARD`, so every `gcloud`, `kubectl` and
   `curl` the script runs goes through `scripts/lib/workspace-guard.sh` (§2.5).
3. **Run the script**, `scripts/register-tenant.sh --workspace "$_WORKSPACE_ID"
   --mode "$_MODE"`, whose steps are these:

| id | step | the build log shows |
|---|---|---|
| A1 | **claim.** Read `workspaces` where `workspace_id == _WORKSPACE_ID`. Refuse unless the record is `approved` (or `failed` or `needs_owner` with an admin's retry recorded) and `decision.verdict == approved` by an email that is an admin in `admin_roles/` now. In one transaction set `applying` and `run` (§1.1). Write the guard's expectation file (§2.5) from the record, mode 0600, outside the checkout | `claimed w-3f9a2c` |
| A2 | **check the name is free.** If the worker account already exists, read its policy and keys: any binding the mode does not make, or a user-managed key, fails `IDENTITY_NOT_OURS` (the script's existing squat inspection, §2b) | `identity: absent` or `identity: ours` |
| A3 | **the account.** Create `swarm-agent-worker-u-alice` if absent | `identity: created` |
| A4 | **its own IAM.** `roles/iam.workloadIdentityUser` for the namespace's two Kubernetes service accounts, and `roles/iam.serviceAccountUser` (act-as) for the scheduler and the reconciler, the grant the script lacks today (§0) | `account bindings: 4` |
| A5 | **bucket and project grants.** The two `tenants/u-alice/`-conditioned object grants on the artifact bucket and the `.tenant` marker object. The database and bucket-metadata roles are **not** granted here when the one-time principal-set grant exists (WD9, §2.3); on the fallback they are, with the project telemetry roles | `access: granted` |
| A6 | **forge slot.** Create the person's empty forge slot and its twin if absent, and grant the worker `secretAccessor` on them | `forge slot: bound` |
| A7 | **namespace.** `kubernetes/apply.sh` for the tenant, with the record's quota flags (§8) | `namespace: applied` |
| A8 | **control plane.** Write the tenant and pool documents with the record's limits, as the script's §6 does | `limits: written` |
| A9 | **verify.** Re-read the account, its policy, the bucket members, the slot binding, every object `kubernetes/render.py::TENANT_FILES` produces, and both documents. Only then set `ready` and `ready_at` | `verified: 41 of 41 objects` |

`--mode limits` runs only A1, A7 and A8: a ceiling change re-applies the
namespace quota and the documents, and makes no IAM call.

**Each step records itself** on the record (`steps.<id>`) when it starts and
ends, which is what the console's progress view reads (§6.1, §6.4). The build
log's step lines carry only the workspace id; everything else goes to the same
log, privately (§2.6).

**A guard stop** does not fail the run. The guard refuses the call before it
reaches Google or the cluster, the script sets `needs_owner` with the step id,
and the build ends. §2.5 says what the owner does next.

**Retries.** A call that fails on `429`, `5xx`, `UNAVAILABLE`,
`DEADLINE_EXCEEDED` or IAM's `409 concurrent policy change` is retried 3 times
with backoff 4, 16 and 64 seconds. The bucket policy is shared by every
workspace, so two runs at once can meet the 409; the retry absorbs it. Any
other failure sets `failed` with a code (§4.3). Every step reads before it
writes (the script's existing `describe` and `_binding_present` checks), so a
re-run after a partial apply makes only what is missing.

**Two runs for one workspace** cannot both proceed: A1's claim is one
transaction, and a second build finds `applying` with a live build id and
exits. Runs for different workspaces may run at once.

**The dispatch sweep.** An `approved` record whose publish failed, or whose
build never claimed it within 10 minutes, is published again by
`POST /v1/admin/workspaces/sweep`, which the existing `swarm-tick` scheduler
identity calls every 5 minutes, beside its other admin sweeps. The route
publishes at most once per record per 10 minutes, and records each attempt.

### 2.3 The dedicated identity and its permissions

**Account:** `swarm-workspace-deployer@<project>.iam.gserviceaccount.com`. It is
created by the owner's bootstrap apply, holds **no key**, and has no WIF
binding: no GitHub workflow can impersonate it. The release deployer cannot act
as it, and it cannot act as the release deployer.

| role | on | why | bound by |
|---|---|---|---|
| custom `swarmWorkspaceAccountAdmin` (`iam.serviceAccounts.create`, `.get`, `.list`, `.getIamPolicy`, `.setIamPolicy`) | **the project** | create a person's worker account and write its Workload Identity and act-as bindings, before any per-account grant can exist | **nothing in IAM** (§2.4). It has no `delete`, `disable`, `keys.create`, `getAccessToken`, `signBlob` or `actAs`. Its use is bounded by the call guard and watched by the alert (§2.4). **Accepted by the owner, 2026-10-08** |
| custom `swarmWorkspaceProjectReader` (`resourcemanager.projects.getIamPolicy`, `iam.roles.get`) | project | the script's reads of the project policy and of the two custom roles | read only |
| `roles/resourcemanager.projectIamAdmin`, **conditioned** | project | **fallback only** (WD9): add the worker's database and telemetry roles per person | `hasOnly([...])` over exactly the worker Firestore custom role, `roles/logging.logWriter` and `roles/monitoring.metricWriter`, the pattern `terraform/bootstrap/deployer_conditions.tf` uses. **Not granted** when W0 confirms the principal-set grant |
| custom `swarmWorkspaceBucketIam` (`storage.buckets.get`, `.getIamPolicy`, `.setIamPolicy`) | **the artifact bucket only** | the worker's two prefix-conditioned object grants (and, on the fallback, the bucket-metadata role) | the bucket. Not `roles/storage.legacyBucketReader`, which carries `storage.objects.list` |
| `roles/storage.objectCreator`, **conditioned** | the artifact bucket, to `tenants/` | the `.tenant` marker object the script writes | the prefix; no read, no delete |
| custom `swarmWorkspaceSecretBinder` (`secretmanager.secrets.get`, `.getIamPolicy`, `.setIamPolicy`) | project, **conditioned** | bind the worker to its forge slot | `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-u-")`, the full-name form `terraform/bootstrap/forge_user_slots.tf` writes |
| custom `swarmForgeSlotCreator` (exists) | project | create the empty slot and its twin | the project; creation is checked there. The same role swarm-api holds |
| custom `swarmWorkspaceFirestore` (`datastore.entities.get`, `.list`, `.create`, `.update`, `datastore.databases.getMetadata`) | project | read the record and `admin_roles/`, write progress, the tenant and pool documents | **the project**; Firestore ignores conditions on the data plane (`scripts/register-tenant.sh` §3). No `delete` |
| `roles/container.clusterViewer`, **conditioned** | project, to the swarm cluster's resource name | `get-credentials` for the namespace step | the expression `terraform/modules/iam/bindings.tf` builds; nothing on the other team's cluster |
| `roles/logging.logWriter` | project | Cloud Build writes the job's log as its identity | write only |

It holds **no** Cloud Run, Terraform state or Pub/Sub grant. It needs none: the
dispatcher creates a tenant's Cloud Run jobs on demand
(`apps/scheduler/scheduler/dispatch.py::CloudRunJobDispatcher.ensure_job`),
which is exactly why A4's act-as grant matters, and no personal resource is in
Terraform state (§3).

**The one-time principal-set grant (WD9).** Terraform, in the bootstrap layer,
grants the worker Firestore custom role (the database role) and
`swarmBucketMetadataReader` (the bucket-metadata role) **once**, to the set of
all personal worker identities, so the job never edits the project's or the
bucket's role list for a person. The same grant carries the two telemetry
roles, `logWriter` and `metricWriter`, which every worker holds today and which
have the same shape. Only each person's `tenants/<tenant>/` object grants stay
per person. This holds only if W0 confirms that an IAM allow policy accepts a
principal set that names exactly those accounts and not, say, every service
account in the shared project; a project-wide service-account set would hand
the database role to the other team's 12 accounts and is refused. **Fallback**,
if W0 says no: the conditioned `projectIamAdmin` row above, plus the alert on
any member not named `swarm-agent-worker-*` (§2.4 R4).

**In the cluster**, the user is the identity's email **and its numeric
uniqueId**, both bound and both matched, for the reason
`kubernetes/rbac/dispatcher-rbac.yaml` records: GKE presents a service account
holding an access token by its uniqueId. Built in W5 as
`kubernetes/rbac/provisioner-rbac.yaml` and
`kubernetes/policies/workspace-provisioner-scope.yaml`, rendered by
`kubernetes/render.py policies` (`POLICY_FILES`, the policy before the RBAC)
and applied by `kubernetes/apply.sh --policies`, which looks the uniqueIds up.
Until the bootstrap apply has created the account, that lookup fails and the
render binds and matches the email alone, in both files, so the deployer is
never bound without being scoped.

| object | grants | why it is not enough on its own |
|---|---|---|
| ClusterRole `swarm-workspace-deployer` + ClusterRoleBinding | `namespaces`: `get`, `create`, `patch`. `serviceaccounts`, `resourcequotas`, `limitranges`, `networking.k8s.io/networkpolicies`, `rbac.authorization.k8s.io/roles`, `rolebindings`: `get`, `create`, `patch`. `roles`: `escalate`, and `bind` with `resourceNames: [swarm-worker, swarm-dispatcher, swarm-reaper]`. **No `delete`, no `list`, and nothing on `Secret`, `Pod`, `Job` or `ConfigMap`**: exactly the verbs a `--workspace` run's kubectl calls need (the apply's get-then-create-or-patch, `escalate`/`bind` for the tenant Roles, reads by name), held there by a test over a run's calls. The job renders without `--spec-verify-keys`, so a personal namespace holds no `swarm-spec-verify-keys` ConfigMap | RBAC cannot restrict `create` by name, so a ClusterRole that creates namespaces creates *any* namespace |
| Role `swarm-workspace-deployer-dns` in `kube-system` | `get` on `services` `kube-dns` and `daemonsets` `node-local-dns` (`resourceNames`) | the two reads `kubernetes/cluster-network.sh` makes |
| **ValidatingAdmissionPolicy `swarm-workspace-deployer-scope`** + binding, and three per-kind companions (`-namespaces`, `-roles`, `-rolebindings`) | for any request by this user (`matchConditions` on the caller; no namespace selector, which the deployer could escape by writing an unlabelled namespace): only `CREATE` and `UPDATE`, no subresource; a `Namespace` must be named `swarm-tenant-u-*` and carry `app.kubernetes.io/part-of=swarm`, its own `swarm-tenant` label and PSA `restricted`; a namespaced object must be in a `swarm-tenant-u-*` namespace and be a kind and name the tenant render produces. A `Role` must be one of the three names, with the rules `kubernetes/render.py` renders. A `RoleBinding` may only reference those Roles, binding `swarm-dispatcher` only to the scheduler, `swarm-reaper` only to the reconciler and `swarm-worker` only to ServiceAccounts in its namespace. `tests/unit/worker/test_workspace_provisioner_scope.py` holds every literal to the render, and the matched users to the bound subjects | this turns "any namespace" into "`swarm-tenant-u-*` only". `kubernetes/policies/pod-security.yaml` is the precedent |

**What it can never touch on the shared deny-list** (`scripts/lib/common.sh`,
`SHARED_DENY_LIST`): it holds no `compute.*`; its only `container.*` grant is
conditioned to the swarm cluster, and `kubernetes/apply.sh`'s three cluster
refusals run on every apply; its storage grants are on the artifact bucket; its
secret binder is prefix-conditioned. Its one unbounded grant,
`swarmWorkspaceAccountAdmin`, *could* reach the other team's accounts if
misused, which is why the guard refuses any account call that names an account
other than the new worker (§2.5, rule C3), and why the alert below exists.

### 2.4 The finding that shapes this, and the accepted risk

A worker account needs two kinds of binding on *its own* IAM policy:
`roles/iam.workloadIdentityUser` for `[swarm-tenant-u-alice/swarm-agent-worker]`
and `[.../swarm-worker]`, and `roles/iam.serviceAccountUser` for the scheduler
and the reconciler. Writing them requires `iam.serviceAccounts.setIamPolicy` on
the account. "IAM resources don't provide the resource name" to a condition.
This repository established that in #334 and recorded it in
`terraform/bootstrap/deployer_service_accounts.tf` and in the
`terraform/bootstrap/variables.tf` validation that refuses project-level
`serviceAccountAdmin` for CI. A per-account grant needs the account to exist
first, and the grant itself needs an identity that can set IAM on accounts it
was not granted on, which today is the owner.

So something with project-wide account-IAM power has to act for each new
person, or the owner does. The first version of this design looked at three
places to put that power (history, kept from WD1):

* **(a) A dedicated identity project** holding only personal worker accounts,
  administered by a runtime provisioner. Not chosen: the owner did not want a
  privileged runtime component with account-admin power.
* **(b) The release deployer, with project-level `serviceAccountAdmin`.**
  Refused, then and now: every plan from `main` would run with power over every
  account in the shared project, the other team's included. That is exactly
  what #334 removed.
* **(c) The shared project, with a dedicated identity doing each person's
  account IAM.** **Chosen.** The per-person approval is an admin's click.

**The owner accepted, on 2026-10-08,** that `swarm-workspace-deployer` holds
project-wide account-IAM power, which Google cannot narrow, with three
safeguards. With WD2 decided they read:

1. **It is usable only by the `swarm-workspace-apply` Cloud Build trigger,
   which builds only `main`.** Nothing holds `iam.serviceAccountUser` or
   `iam.serviceAccountTokenCreator` on the identity: the bootstrap writes its
   policy with no such member, and it has no WIF binding and no key. A build
   that runs as a user-specified account needs `actAs` on it from whoever
   creates or edits the trigger or submits the build, and the owner's bootstrap
   apply is what creates this trigger. W0 lists every principal holding
   `actAs` at the project level in the shared project, because such a holder
   could submit a build as this identity too; the alert below catches it if one
   does.
2. **`.github/CODEOWNERS` names the owner** on the guard
   (`scripts/lib/workspace-guard.sh`, `scripts/lib/workspace-calls.json`,
   `scripts/lib/guard-bin/`), on `scripts/register-tenant.sh`,
   `scripts/lib/common.sh` and on the build file, and branch protection
   requires the code owner's review for them. That is what the trigger builds,
   so it is what bounds the identity.
3. **A log alert** fires on any change by `swarm-workspace-deployer` to an
   account not named `swarm-agent-worker-*`.

Two more alerts come with the trigger: any build that runs as this identity
from a trigger other than `swarm-workspace-apply` (or with no trigger), and any
change to that trigger, to the topic's IAM, or to the identity's own IAM
policy.

**Why this fits #334.** #334 refused project-level account admin to the release
deployer because that identity applies *any* plan that reaches `main`. Here:

1. **It runs one script under a call guard.** The guard allows only calls that
   create or bind that one workspace's resources, and stops on anything else
   (§2.5).
2. **Nothing else can use it** (safeguard 1).
3. **It is a separate identity.** The release deployer keeps its per-account
   grants, and its validations stay. #334's rule "CI never holds project-level
   `serviceAccountAdmin`" now reads "the release deployer never does; one
   guarded Cloud Build job does", and `terraform/bootstrap/variables.tf` gains
   a validation that the new identity's roles are exactly the list in §2.3.

**Residual risk, stated so it is not mistaken for zero:**

* **R1. The guard is code, not IAM.** A change to the guard, the script or the
  build file that merges to `main` changes what the identity will do.
  Mitigation: safeguard 2, and the guard's self-test runs in CI on every
  change.
* **R2. A stolen token of this identity** (from a compromised build) could set
  IAM on any account in the shared project for the token's life. The guard
  bounds the script, not a token used directly. Mitigation: **detective**,
  safeguard 3 and the two alerts above; and a run lasts minutes, so a call by
  this identity outside a build is itself an anomaly the alert sees.
* **R3. A forged Pub/Sub message** does nothing on its own: A1 refuses a
  record no admin approved, and swarm-api is the topic's only publisher.
* **R4. Bucket members.** The bucket grant bounds *roles* on one bucket, not
  *members*. The guard checks every member (C4); the alert covers a token used
  directly. On the WD9 fallback the project half has the same shape, with the
  alert on any member not named `swarm-agent-worker-*`.
* **R5. The logs are private to the project, and the project is shared**
  (§2.6).

### 2.5 The call guard

The guard is a wrapper around every cloud and cluster call the script makes.
It no longer reads a Terraform plan, because no plan is made (WD3).

**How it is wired.** `scripts/lib/guard-bin/` holds three shims, `gcloud`,
`kubectl` and `curl`. Each passes its argument list to
`scripts/lib/workspace-guard.sh check`, and only on an allow runs the real
binary, by absolute path. `scripts/lib/common.sh`'s `kubectl_bin` and
`prefer_local_bin` return the shim while `SWARM_CALL_GUARD` is set, because they
otherwise resolve a binary explicitly and would step around `PATH`.
`register-tenant.sh --workspace` refuses to start unless `SWARM_CALL_GUARD`
names a readable expectation file and `command -v gcloud`, `kubectl_bin` and
`command -v curl` all resolve into `guard-bin/`. A unit test reads the script,
`common.sh` and `kubernetes/apply.sh` and fails on any call to these three
binaries by an absolute path.

**The expectation file**, written by A1 from the record, holds the workspace
id, the tenant id, the worker account email, the namespace, the bucket, the two
rendered bucket condition expressions, the forge slot names and the Firestore
document paths, all derived by the frozen
`identity.py::worker_service_account_id` and the script's existing naming. The
names are the tenant's, not the workspace id's, because the frozen contract
derives them from the tenant; the guard ties them to the workspace id through
the approved record.

**The rules**, stated once in `scripts/lib/workspace-calls.json` as allowed
call shapes. A call is allowed only if one rule matches it whole:

| rule | allows | refused example |
|---|---|---|
| C0 | nothing by default: a call matching no rule stops the run | `gcloud compute instances list` |
| C1 | reads (`describe`, `get-iam-policy`, `list` filtered to this account, `kubectl get`, Firestore `GET`) of this workspace's resources, and of the fixed shared resources the script reads: the project's policy, the two custom roles, the artifact bucket, the swarm cluster's credentials, `kube-system`'s two DNS objects. Before A1 has written the expectation file, the only calls allowed are A1's own: the `runQuery` on `workspaces` for the given workspace id, reads of `admin_roles/`, and the claim's transaction on that one record | `gcloud iam service-accounts get-iam-policy` on another tenant's account |
| C2 | `gcloud iam service-accounts create <id>` where `<id>` is the expected worker account id, and nothing else on accounts: no `delete`, `disable`, `update`, `keys …` or `set-iam-policy` | creating `swarm-agent-worker-u-bob` |
| C3 | `gcloud iam service-accounts add-iam-policy-binding <the expected worker email>` with role `workloadIdentityUser` and a member that is one of the two expected Kubernetes service accounts in `swarm-tenant-<tenant>`, or role `serviceAccountUser` and a member that is the scheduler or the reconciler | a binding on any other account, or `serviceAccountTokenCreator` |
| C4 | `gcloud storage buckets add-iam-policy-binding gs://<artifact bucket>` with member the expected worker, role `objectViewer` or `objectUser`, and a condition **string-equal** to the expected expression for `tenants/<tenant>/` (these are the fixed shared resource's prefix-conditioned per-person bindings); `gcloud storage cp - gs://<bucket>/tenants/<tenant>/.tenant`. On the WD9 fallback only: the bucket-metadata role for the expected worker | a condition naming another prefix, or any other member |
| C5 | **WD9 fallback only:** `gcloud projects add-iam-policy-binding` with member the expected worker and a role among the worker Firestore role, `logWriter`, `metricWriter`. Under the principal-set grant this rule is off, and any project IAM call stops the run | `roles/editor` |
| C6 | `gcloud secrets create` of the expected forge slot names, and `add-iam-policy-binding` on them with member the expected worker and role `secretAccessor` | a binding on `swarm-tenant-eng-git` |
| C7 | `kubectl apply`, `create` or `patch` whose every object (the guard reads the `-f` file or stdin) is the `Namespace` `swarm-tenant-<tenant>` or is in that namespace, plus the server dry-run that precedes it; the admission policy of §2.3 checks the same server-side | an object in `swarm-system` |
| C8 | `curl` `PATCH` or `commit` to the Firestore REST API on exactly `tenants/<tenant>`, `pools/tenant:<tenant>` and `workspaces/<tenant>`, and the metadata server's token endpoint | a write to another tenant's pool |
| C9 | never: any `remove-iam-policy-binding`, `set-iam-policy`, `delete`, `disable`, `keys create` or Firestore `DELETE`, whatever its target | the script's legacy repair path that removes an unconditioned bucket binding |

**Stopping.** On a refusal the guard prints the rule it fell through to and the
call, through `redact`, to the private build log; exits non-zero before the
real binary runs; and the script sets `needs_owner` with the step id. Nothing
the refused call would have done has happened.

**What the owner does then.** The console shows "waiting for the platform
owner" (§1.2). The owner reads the refused call in the build's log. If it is
right, he runs `scripts/register-tenant.sh --workspace w-3f9a2c` himself, with
his own credentials and the guard in `--report-only` mode (it prints what it
would refuse and lets it run); then an admin's **Retry** runs the job again,
which now finds everything present, passes every rule (a read and a no-op
match C1), and reaches A9. If it is wrong, an admin denies the request. The
deployer identity is never given an unguarded path.

**Tested where the plan guard is tested.** `scripts/lib/workspace-guard-cases.json`
holds one call built to trip each rule and one allowed call per rule, and the
guard's self-test runs in `make test` and the `shell` CI job beside
`plan-guard.sh`'s. A second test feeds every `gcloud`, `kubectl` and `curl` call
the workspace mode's dry run records and asserts each one matches a rule, so
the script and the guard cannot drift apart without CI going red.

### 2.6 Privacy now that the logs are Cloud Logging

The repository is public; the build's logs are not. The rule stays that **a
person appears in anything public only as `w-…`**, and with WD2 and WD3 almost
nothing public is left to protect:

1. **The list is never in the repository and never in Terraform.** It is
   `workspaces/` in Firestore. Terraform does not read it (§3), so there is no
   tfvars file, no sensitive variable and no plan that could carry a name.
2. **The trigger's input is opaque.** The Pub/Sub message carries the
   workspace id and the mode only. A1 reads the rest from Firestore.
3. **The build log is private to the project, and the project is shared.**
   Cloud Build writes it to Cloud Logging (`options.logging:
   CLOUD_LOGGING_ONLY`, no logs bucket). Anyone holding log-viewing on
   `saga-agents-staging` can read it, which may include the other team. So the
   build file routes its own log entries, by a sink on the build's trigger id,
   to a log bucket readable only by the owner and the platform's admins, with
   an exclusion from `_Default`. W0 confirms a Cloud Build log can be routed
   that way; if it cannot, the residual is that the shared project's log
   viewers can read a person's tenant id and email in these logs. Either way
   every command's output still passes through `redact`, so no credential is
   written.
4. **Step lines name the workspace id only** (§2.2), so a screenshot of the
   build's step list or the People pane's run link names nobody.
5. **Firestore holds the failure code, not output.** The record's `failure`
   holds a code, and the copy is served from the code (§4.3).
6. **Outputs.** `terraform/infra`'s `tenant_namespaces` output lists group and
   service tenants only, as today; personal namespaces are not in Terraform at
   all, so the release's namespace step never sees them (§3).

---

## 3. Terraform owns group and service tenants only

### 3.1 Today

Every tenant Terraform knows is a key of `var.tenants`. The tenancy module
creates the account and its IAM (additive `_iam_member` resources, never an
authoritative `_iam_policy` or a project or bucket `_iam_binding`; checked
2026-10-08). The secret_manager module creates per-provider secrets with
**authoritative** per-secret accessor bindings. The firestore module writes the
tenant and pool documents once (`terraform/modules/firestore/bootstrap.tf`,
`ignore_changes = [fields]`). The cloud_run_jobs module creates per-(tenant,
profile) jobs from `local.jobs`.

### 3.2 The split (decided, WD3 with WD4)

**Terraform owns group and service tenants. Personal workspaces are created by
the script's `--workspace` mode and live outside Terraform state** (owner
decision WD3, 2026-10-08, which supersedes the WD4 wording "Terraform owns
personal workspaces through the private list"; §9 keeps that for history).

| | group and service tenants | personal workspaces |
|---|---|---|
| defined in | `terraform/environments/dev/dev.tfvars` (`var.tenants`) | `workspaces/{tenant_id}` in Firestore |
| made by | the release (`terraform/infra`), after the owner's bootstrap grant | the Cloud Build job, `register-tenant.sh --workspace` (§2, §4) |
| account IAM done by | the release deployer, per-account grant | `swarm-workspace-deployer`, under the call guard |
| database and bucket-metadata roles | per tenant, by the tenancy module | once, to the principal set of all personal workers (WD9), or per person on the fallback |
| Cloud Run jobs | `cloud_run_jobs` module | the dispatcher, on demand (`ensure_job`) |
| teardown | `terraform` | a separate, owner-approved run (§7) |

**Terraform must never plan to change or destroy a script-created personal
resource.** Three things make that hold:

1. **No resource for them.** Nothing in `terraform/infra` or
   `terraform/bootstrap` iterates over people. There is no
   `var.personal_workspaces`, no module instance and no data source reading
   Firestore, so a personal resource is never in state and a plan cannot
   address it.
2. **No authoritative resource over anything they are bound into.** A personal
   worker is a member of the artifact bucket's policy, of the project's policy
   (on the WD9 fallback), and of its own forge slots' policies. An
   authoritative `_iam_policy` or `_iam_binding` on any of those would remove
   it at the next release. So Terraform keeps to additive `_iam_member`
   resources there, as it does today (§3.1), and the secret_manager module's
   authoritative bindings stay on group and service tenants' own provider
   secrets only.
3. **A personal id cannot enter `var.tenants`.** `terraform/infra/variables.tf`
   refuses a `var.tenants` entry of `kind = "user"` whose principal is a human
   (not `*.iam.gserviceaccount.com`), once `u-bogdan` has moved out (§3.3).

**`tests/terraform/personal_workspaces_absent.tftest.hcl`** holds those three
to account. Against the dev tfvars and a fixture of personal-shaped ids
(`u-fixture-person`, `w-000000`), it asserts that no planned resource's key,
`account_id`, `secret_id`, namespace or IAM member contains a personal id; that
no `google_storage_bucket_iam_binding`, `google_storage_bucket_iam_policy`,
`google_project_iam_binding` or `google_project_iam_policy` exists in either
layer; that every authoritative secret binding is on a secret named for a key
of `var.tenants`; and that the validation of rule 3 refuses a human user
tenant.

**The one Terraform resource that touches personal workers** is the WD9
principal-set grant: an additive `google_project_iam_member` per role (and a
`google_storage_bucket_iam_member` for the bucket-metadata role) whose member
is the set, in the bootstrap layer. It names no person.

**One consequence to accept:** `aged_prefixes` in
`terraform/modules/storage/main.tf` lists `var.tenants` only. Personal
tenants' task objects are not moved to Nearline. That is a storage-class cost
difference, and deletion still applies. A rule keyed on `tenants/u-` could be
added later if the cost matters.

### 3.3 Migrating what exists

| tenant | today | after |
|---|---|---|
| `u-bogdan` | in `dev.tfvars` (providers `anthropic`, 80/80). Account, secrets and jobs in Terraform state. Namespace made by hand on 2026-10-07 | **moved out of Terraform state, without destroying anything.** A pull request removes it from `dev.tfvars` and adds `removed` blocks with `lifecycle { destroy = false }` for its instances of the tenancy, secret_manager, firestore and cloud_run_jobs modules, and, in the bootstrap layer, for its `deployer_admin` grant. **The release plan must show 0 to add, 0 to change, 0 to destroy, and only forgets.** A record `workspaces/u-bogdan` is written with a workspace id, `state = ready`, `migrated = true`, its current limits and its `anthropic` provider. A `--mode verify` run (A9 only) then reads it back; its Terraform-era bindings for the release deployer are on the allowed list for a record with `migrated = true` only |
| `u-sw-c90291` (`swarm-verify`) | in `dev.tfvars`, a service account's tenant | unchanged. Service tenants stay in `dev.tfvars` |
| `u-*` documents created by `ensure_tenant` on first sight (for example `u-admin`) | a tenant document and pool with no infrastructure behind them | no record is written. Each such person sees "Request your workspace", and A8 adopts the existing documents because their principal matches |

The `removed` blocks are public and name `u-bogdan`, which is already in
`dev.tfvars` and its history; they name no workspace id. `u-bogdan` keeps its
`anthropic` provider secret, now outside Terraform with its accessor binding
left in place; the record's optional `providers` list says so, and is empty
for every new person (§8). The release deployer's forgotten
`serviceAccountAdmin` on `u-bogdan`'s account stays until the owner removes it;
it grants nothing the release still uses.

**`ensure_tenant` stops creating personal tenants.** Once the gate is on
(WD8), `tenant_for` no longer writes `tenants/u-*` on first sight for a human
caller. It reads, and a create path then meets the gate. A8 becomes the only
writer of a new personal tenant document. Group tenants are unchanged.

---

## 4. `register-tenant.sh --workspace`

### 4.1 One definition, run under a guard

The owner chose the script, not Terraform modules, to make a person's
resources (WD3). It is already the operator's definition of a tenant, with its
squat inspection, its version-3 bucket-policy handling and its deny-list
refusals. The new mode changes what it reads and what it may do:

| | today (`--user`, `--group`) | `--workspace w-3f9a2c` |
|---|---|---|
| input | flags | the workspace id only; every other value comes from the approved record. Any of `--user`, `--group`, `--tenant`, `--providers`, `--add-provider` alongside it is refused |
| the derived-id assertion | as today | recomputes `tenant_id_for_user(principal)` and refuses a record whose `tenant_id` differs |
| runs where | an operator's machine | the Cloud Build job only: it refuses to start without the guard installed (§2.5) |
| §1 the group must exist | yes | skipped: a person, not a group |
| §2b the release deployer's grant | checked | skipped: Terraform never manages a personal account, so the release deployer needs no grant on it. The squat inspection stays, with the allowed bindings narrowed to the four the mode makes |
| §3 IAM | the project roles, the bucket grants, the bucket-metadata role | the bucket grants only, under the WD9 principal-set grant; on the fallback, also the project and bucket-metadata roles |
| act-as | **never made** (the gap of §0) | **made**: `serviceAccountUser` for the scheduler and the reconciler, A4 |
| §4 secrets | the provider secrets | no provider secret (§8); the forge slot and its binding, A6 |
| §5 Kubernetes | `kubernetes/apply.sh` | the same, with the record's quota flags, A7 |
| §6 control plane | the tenant and pool documents | the same, and every step's progress on the record |
| repair paths | the script removes a stale unconditioned bucket binding, among others | never: a removal is C9, so it stops the run and goes to the owner |

`--mode limits` runs only A1, A7 and A8. `--mode verify` runs only A9, for the
migration (§3.3) and for an admin's check from People.

The act-as gap stays open for the operator's `--group` and `--user` paths; it
is reported separately, as an out-of-territory finding on this design's pull
request.

### 4.2 The steps

The step ids are those of §2.2. "Done" means the object exists *as specified*,
not merely that a call returned 200.

| id | console label | idempotent because | failure code |
|---|---|---|---|
| A1 | Approved | the claim is one transaction; a second run finds `applying` with a live build and exits | `WORKSPACE_NOT_APPROVED` (not retryable) |
| A2 | Checking the name is free | read only | `IDENTITY_NOT_OURS` (not retryable) |
| A3 | Identity | created only if absent | `APPLY_FAILED` |
| A4 | Identity | each binding is read first and added only if absent | `GRANT_FAILED` |
| A5 | Access | as A4; the bucket policy is read at version 3 | `GRANT_FAILED` |
| A6 | Access | the slot is created only if absent; its binding as A4 | `GRANT_FAILED` |
| A7 | Namespace | `kubectl apply` is declarative; a server dry-run precedes it | `NAMESPACE_APPLY_FAILED`, or `CLUSTER_UNREACHABLE` |
| A8 | Limits | a patch with the script's mask; an existing pool keeps its `active` | `CONTROL_PLANE_WRITE_FAILED` |
| A9 | Final check | read only | `VERIFY_FAILED`, naming the object type, never its name |

A guard stop at any step is `needs_owner`, not a failure code.

The order matters: the identity and its grants come before the namespace,
because `kubernetes/apply.sh` reads the account's IAM to decide whether to
render the legacy KSA; and the namespace comes before `ready`, so no
submission is admitted into a half-made workspace.

The forge slot's name is deterministic (`swarm-tenant-<tenant>-git-u-<16 hex>`,
forge_user_slots.tf), so it can exist, already bound, before the person
connects GitHub; swarm-api's later create must treat `ALREADY_EXISTS` on its
own label as success, which W0 checks against
`apps/swarm-api/swarm_api/gittokens.py`.

### 4.3 Failure copy, word for word

The API serves this copy with the code, as `apps/swarm-api/swarm_api/onboarding.py`
does for §2.3 of onboarding.md. `{step}` and `{request_id}` are filled in.
Nothing from a command's output is shown.

| code | retryable | copy |
|---|---|---|
| `APPLY_FAILED`, `GRANT_FAILED`, `CONTROL_PLANE_WRITE_FAILED` | yes | “Setting up your workspace stopped at {step}. Nothing half-made can run: your work stays refused until every step is done. An admin has been shown this and can retry it; the reference is {request_id}.” |
| `CLUSTER_UNREACHABLE` | yes | “Your workspace is made except its Kubernetes namespace, because the cluster did not answer. This is usually brief. An admin can retry it from People.” |
| `NAMESPACE_APPLY_FAILED` | yes | “Your workspace's Kubernetes namespace could not be applied in full, so it is not isolated yet and nothing will run in it. An admin can retry it; the reference is {request_id}.” |
| `VERIFY_FAILED` | yes | “Every step reported success, but the final check could not find one of your workspace's objects. Nothing runs until it can. An admin can retry it.” |
| `IDENTITY_NOT_OURS` | no | “An identity with your workspace's name already exists and carries access SwarmCloud never grants, so it was not adopted. Nothing was changed. The platform owner must look at request {request_id} before this can continue.” |
| `WORKSPACE_ID_TAKEN` | no | “The workspace name that belongs to your account is already registered to a different identity. Nothing was changed. Ask an admin to look at request {request_id}; this needs a person, not a retry.” |
| `WORKSPACE_NOT_APPROVED` | no | (not shown to the person: a build for a record no admin approved. It is an `admin_audit` entry and an alert.) |

The copy for `needs_owner` is the state's own line in §1.2. A retry is an
admin's **Retry** in People, which publishes the message again; the person has
no retry button, because each retry runs a privileged identity.

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
submitted by a running worker of a tenant that is, by construction, admitted,
so they are not gated.

**The gate applies only to a submission that resolves to a person's own
tenant** (owner decision WD7). It admits a submission when **any** of these
holds:

1. the resolved tenant is a **group** tenant (`kind == "group"`). A member of
   `eng@saga.xyz` submits as `eng` at once, with or without a workspace;
2. the caller is a **service account** (the frozen pattern), or a listed
   continuation or rollup identity (`member_scope`, `is_rollup_sweeper`). Their
   tenants are in `dev.tfvars`, and their callers cannot click a button;
3. the tenant is a personal one, `workspaces/{tenant_id}` has
   `state == "ready"`, **and the tenant has a Claude account** (owner decision
   WD6): an active pool account it owns, a pool account lent to it, or a
   provider key in its `providers` (only `u-bogdan`'s, §3.3).

Otherwise it refuses, with `WORKSPACE_NOT_READY` when the workspace is not
ready, or `NO_CLAUDE_ACCOUNT` when it is ready and has no account. The account
check applies to every runner profile, because the owner's decision is that a
ready workspace "runs nothing" until then (confirmed by the owner,
2026-10-08).

The background submitters (issue CI, merge wake, the indexer) submit as an
`owner` context derived from a run that was itself admitted, so they pass the
same rules.

### 5.2 The responses

```
HTTP/1.1 403
{"code": "WORKSPACE_NOT_READY",
 "message": "Your SwarmCloud workspace is not ready yet, so no task or workflow can start. Finish setup: request your workspace.",
 "detail": {"workspace_id": "w-3f9a2c", "state": "requested", "setup_url": "https://<console>/setup#workspace",
            "setup_command": "/sc:setup"}}
```

```
HTTP/1.1 403
{"code": "NO_CLAUDE_ACCOUNT",
 "message": "No Claude account yet. Add your own, or ask an admin to lend you one, and then try again.",
 "detail": {"setup_url": "https://<console>/setup#claude-account", "setup_command": "/sc:setup"}}
```

`WorkspaceNotReady(Forbidden)` and `NoClaudeAccount(Forbidden)` are new classes
in `apps/swarm-api/swarm_api/errors.py`, with the upper-case codes the owner
wrote. The onboarding codes are upper-case too, though `ApiError`'s other codes
are lower-case. `state` is `none` when there is no record. The `message` changes
with the state: "is waiting for an admin's approval" for `requested`, "was not
approved: {reason}" for `denied`, "is being created" for `approved`/`applying`,
"is waiting for the platform owner's review" for `needs_owner`, and "could not
be created: {copy}" for `failed`.

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
must drain or cancel them first. A reclaimed loan (§6.4) can leave a ready
tenant with queued claude-code work and no account; those tasks park
`CREDENTIAL_MISSING`, as they do today, and hold nothing.

### 5.5 It ships switched off (WD8)

Both checks are behind `WORKSPACE_GATE` (`off` → `on`), read from swarm-api's
settings, and read `workspaces/` only when on. The order the owner decided:

1. Everything ships with the gate **off**.
2. One real person is approved through the console end to end: the workspace
   reaches `ready`, they add or are lent a Claude account, and a task of theirs
   runs.
3. In the same step, the `u-bogdan` record is confirmed `ready` (it is written
   `ready` by the migration, §3.3), every other existing personal tenant with
   resources behind it is given a `ready` record the same way, and
   `WORKSPACE_GATE=on` is set.

---

## 6. The console, the plugin and the API

### 6.1 The console's setup checklist

The checklist (`apps/swarm-ui/src/GitHubConnect.tsx`, from onboarding.md §2)
gains **`workspace`** as its second step, after `signed_in`, and
**`claude_account`** right after it. A person can do everything else in
parallel. Both states come from `GET /v1/onboarding`
(`apps/swarm-api/swarm_api/onboarding.py` gains the steps).

```
Set up SwarmCloud                                              2 of 7 done
 ✓ Signed in as alice@saga.xyz
 ○ Request your workspace
   Your own isolated space to run agents in: an identity, a storage area and
   a Kubernetes namespace that only your work uses. An admin approves it, and
   it is ready a few minutes later. Until then you can look around and connect
   GitHub, but nothing of yours runs. Team work is not affected.
   [ Request my workspace ]
 ○ Add a Claude account (or ask an admin to lend one)
 ○ Connect GitHub
 ○ Enable your organisations
 ○ Choose repositories
 ○ Verify access
```

Waiting, and then setting up (the console polls `GET /v1/workspace` every 5
seconds while the page is open and the record is not `ready`):

```
 ◐ Workspace requested — waiting for an admin to approve           2h ago

 ◐ Setting up your workspace — w-3f9a2c                              4m 10s
   ✓ Approved            by an admin
   ✓ Checking the name is free
   ✓ Identity
   ◐ Access
   ○ Namespace
   ○ Limits
   ○ Final check
   You can carry on with the steps below meanwhile.
```

Denied:

```
 ✕ Your workspace request was not approved
   “Please use the eng team space for the migration work.”
   [ Request again ]   (available tomorrow)
```

Ready, with the Claude account step next:

```
 ✓ Workspace ready (w-3f9a2c)
 ○ Add a Claude account (or ask an admin to lend one)
   Your workspace runs nothing until it has a Claude account to run on.
   [ Add key ]   [ Request a loan ]
```

**[Add key]** opens the Accounts page's existing add flow
(`POST /v1/accounts/authorize` and `/exchange`, owner = the caller's tenant).
**[Request a loan]** posts `POST /v1/workspace/loan-request`, which records a
request admins see in People (§6.4), and the step then reads "Loan requested".
The step is ticked when the tenant has an account, by the same rule as §5.1
(3).

The Submit screens (`apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/SubmitWorkflow.tsx`)
render a `WORKSPACE_NOT_READY` or `NO_CLAUDE_ACCOUNT` refusal as a banner with
the API's `message` and a "Finish setup" link to `setup_url`. They do not
disable the form ahead of time: the server is the authority, and a stale client
must not guess.

### 6.2 The plugin

`/sc:setup` (`plugin/commands/setup.md`) and `sc setup`
(`apps/swarm-mcp/swarm_mcp/sc.py::run_setup`) gain both steps at the same
place. There is a new bridge tool, `swarm_setup_workspace`, in
`apps/swarm-mcp/swarm_mcp/server.py`. It posts the request (or the loan
request) and then reports the record. It does not wait inside the tool.
`/sc:setup` re-reads status as the existing steps do.

```
$ /sc:setup
SwarmCloud setup — alice@saga.xyz
  ✓ signed in
  ○ workspace          not requested
  ○ claude account     none
  ○ github             not connected
  …
Request your workspace now? An admin approves it; it is ready a few minutes
after that. [Y/n] y
  ◐ workspace          requested (w-3f9a2c) — waiting for an admin
  ○ claude account     none — add one in the console, or ask for a loan? [loan/skip] loan
  ◐ claude account     loan requested
  → open https://github.com/login/oauth/authorize?… to connect GitHub as yourself
…
```

A plugin submission before then prints the API's message and the command:

```
$ /sc:run "fix the flaky test"
✕ 403 WORKSPACE_NOT_READY: Your SwarmCloud workspace is waiting for an admin's
  approval, so no task or workflow can start yet. Finish setup with /sc:setup.
```

### 6.3 Routes (additive)

| route | who | does |
|---|---|---|
| `GET /v1/workspace` | any signed-in human | the caller's record (always their own), or `{state: "none"}`. Never another person's |
| `POST /v1/workspace` | the same | the request of §1.3 |
| `POST /v1/workspace/loan-request` | the same | records a loan request (`loan_requests/{tenant_id}`), idempotent |
| `GET /v1/admin/people` | admin | §6.4's list |
| `POST /v1/admin/workspaces/{workspace_id}/approve` | admin | §1.3; publishes the workspace id (§2.1) |
| `POST /v1/admin/workspaces/{workspace_id}/deny` | admin | §1.3; body `{reason}` |
| `POST /v1/admin/workspaces/{workspace_id}/retry` | admin | from `failed` or `needs_owner`: publishes again |
| `PUT /v1/admin/workspaces/{workspace_id}/limits` | admin | §6.4's ceiling; body `{max_active}` only |
| `PUT /v1/admin/people/{workspace_id}/loan` | admin | lend or reclaim an account (§6.4) |
| `PUT /v1/admin/admins/{email}` and `DELETE` | admin | grant or remove admin (§6.5) |
| `POST /v1/admin/workspaces/sweep` | `swarm-tick` | §2.2's dispatch sweep |
| `GET /v1/onboarding` | (exists) | gains the `workspace` and `claude_account` steps |

Admin routes address a person by **workspace id**, so a URL in a browser
history, a proxy log or a screenshot of the address bar names nobody. None
takes or returns a credential, an image, a command or a resource spec
(invariant 10).

### 6.4 Admin → People

A new pane, **People**, in the Admin section (`apps/swarm-ui/src/App.tsx`
`SECTIONS`), so the issue forms' "Where" list follows it
(`tests/unit/scripts/test_issue_forms.py`). Admins only. v1 has four parts, as
the owner decided.

**1. Everyone who has signed in.** One row per person, from a new collection
`people/{tenant_id}` that swarm-api writes on an authenticated request at most
once per 10 minutes per person (`principal`, `first_seen`, `last_seen`,
`teams` resolved at last sight).

```
People                                                   12 people · 2 pending
 Person            Teams        GitHub      Workspace         Claude account     Last active
 alice@saga.xyz    eng          connected   ◐ requested       none               3 min ago     [Approve] [Deny]
 bogdan@saga.xyz   eng, admin   connected   ✓ ready w-91c0de  own (2)            now
 carol@saga.xyz    —            —           ✓ ready w-7a12f4  lent by eng        2 days ago
 dan@saga.xyz      eng          connected   ✕ failed (Namespace)  none           1 h ago      [Retry]
```

* **Teams**: the group tenants the person resolves to, checked per registered
  group (the Cloud Identity constraint: `searchTransitiveGroups` 403s here).
* **GitHub**: from the onboarding state (#780).
* **Workspace**: the record's state, with the workspace id.
* **Claude account**: `own (n)`, `lent by <tenant>`, `provider key` or `none`,
  by the rule in §5.1 (3).
* **Last activity**: the later of `last_seen` and the last submission.

**2. Pending requests.** `requested` records sort first. **Approve** asks for
one confirmation ("Create a workspace for alice@saga.xyz with 8 agents? A CI
job will create its identity and namespace."), then the row shows the run's
steps live, from the record's `steps`, with a link to the build for those who
can read the project's builds (the owner). **Deny** opens a text box; the reason is
required and is shown to the person. A `needs_owner` row reads "Waiting for
the platform owner" with the step the guard stopped at; a `failed` row has **Retry**.

**3. Per-person ceiling and Claude accounts.**

* **Ceiling**: a number field, default 8. Saving writes the record's `limits`
  (with `quota_pods = 2 × max_active`, `quota_cpu = 8 × max_active`, the ratio
  of §8), the tenant's `max_active` and pool `hard_limit` through the existing
  tenant-limits path, and publishes the workspace id in mode `limits`, which
  runs only A1, A7 and A8 (the namespace quota and the documents; no IAM call),
  under the same guard. Lowering it never cancels running work; the pool
  refuses new leases above the new limit.
* **Lend or reclaim**: lists the person's loan request, if any, and the pool
  accounts an admin may lend: those owned by a group tenant, or by the admin's
  own personal tenant (confirmed by the owner, 2026-10-08). **Lend** adds the person's tenant to the account's
  `lend_to`; **Reclaim** removes it. Running work on a reclaimed account
  finishes; new claude-code work parks `CREDENTIAL_MISSING` and, for
  submission, the gate's account check applies (§5.4). A person's *own*
  account is never lendable or reclaimable here; its owner keeps the existing
  `PUT /v1/accounts/{id}/lending`.

**4. Admins.** §6.5.

Every action in People writes an `admin_audit` entry in the same transaction:
`{action, target_workspace_id or target_email, by, at, detail}`, and the pane
shows the last 50 under the table. The audit is private (Firestore); nothing
of it goes to a public log.

### 6.5 Admin roles move to Firestore

Today `is_admin` is `admin_groups` membership or `admin_users` (configuration).
After this design:

* **`admin_roles/{email}`** documents `{role: "owner" | "admin", granted_by,
  granted_at}` are the source of admin rights. `is_admin` is true for an email
  with a document, or in `admin_groups`.
* **The owner** is `PLATFORM_OWNER`, one email in swarm-api's configuration,
  seeded as the one `role: owner` document at first start. Configuration, not
  the UI, is what makes someone the owner, so no admin can make themselves one.
* **`admin_users` is migrated once** into `role: admin` documents with
  `granted_by: "config-migration"`, and then holds only the owner, as a break
  glass that works if Firestore's admin documents are lost.
* **Grant** (`PUT /v1/admin/admins/{email}`) by any admin, for an allowed-domain
  human email.
* **Remove** (`DELETE`) by any admin, with two safeguards checked in the same
  transaction:
  * **the owner cannot be demoted by others**: the owner's document can be
    changed only by the owner, and the owner's rights come from configuration
    anyway;
  * **at least one admin always**: a removal that would leave no `admin` or
    `owner` document is refused (`LAST_ADMIN`).
* Every grant and removal writes `admin_audit` with who and when, shown in
  People.

`admin_pool_users` and the rollup identities are unchanged: they are narrower
capabilities, not admin.

---

## 7. Deprovisioning (a follow-up, not built now)

When a person leaves, these steps run in order. The order exists so that
nothing is left that runs, and nothing is deleted while it still runs:

1. Set the tenant's `enabled = false`. `tenant_for` then refuses new work.
2. Cancel or drain its work, following `docs/runbooks/tenant-offboarding.md`.
3. Delete the person's worker account, its bindings on the artifact bucket and
   its forge slot bindings, by a deprovisioning run of the script.
4. Delete the namespace.
5. Disable the slot versions; delete the slot secrets after the retention the
   owner sets.
6. Archive the artifacts under `tenants/u-<id>/`, by the bucket lifecycle.
7. Mark the record `deprovisioned`.

Step 3 is exactly what this design's guard refuses (C9: no removal, no
deletion). So deprovisioning is **a separate, owner-approved run** with its own
guard mode (deletions of one workspace id's resources only), decided then.
Terraform plays no part in it, because no personal resource is in its state
(§3.2). This design reserves the state name `deprovisioned`.

---

## 8. Cost and limits for a new personal workspace (decided, WD5)

| setting | default | today's comparison | why |
|---|---|---|---|
| `max_active` | **8** | `default_tenant_max_active = 20` (`apps/common/swarm_common/config.py`); `u-bogdan` 80; the verify tenant 4 | a person's interactive work. 8 people at the ceiling hold 64 of claude-code's `runner:claude-code` 80 |
| `capacity_units` | **8** | default 40; the verify tenant 8 | the pool is `min(max_active, capacity_units)` = **8 units** |
| `providers` (`credentials`) | **none** | `u-bogdan`: `anthropic` | the workspace runs on the person's own pool account or one lent to them (WD6). No provider secret is created, so there is no key nobody stores |
| namespace ResourceQuota | **16 pods, 64 vCPU** (`--quota-pods 16 --quota-cpu 64 --quota-memory 128Gi --quota-jobs 64 --quota-ephemeral 160Gi`) | `kubernetes/render.py` defaults: pods 100, cpu 400 | with `requests == limits`, this is 8 claude-code pods at 4 vCPU, plus headroom for jobs finishing within their TTL. It scales with the ceiling (2 pods and 8 vCPU per agent) |
| raisable | **per person, by an admin**, in People (§6.4) | the tenant-limits admin route | the owner's decision |
| `monthly_budget_usd` | none | none: no per-tenant budgets are built or planned (owner, 2026-10-01) | — |

**What a workspace costs while idle: nothing.** That follows from invariant 1.
A ready workspace with no `LEASED` work holds no capacity. The account, the
namespace, the bindings and the empty secrets are free or
fractions-of-a-cent-a-month objects.

**What a busy one costs:** at most 8 claude-code pods. At 4 vCPU each on
Autopilot that is **32 vCPU while all 8 run**, bounded by the pool, not by the
namespace. Each approval costs one Cloud Build run of a few minutes, billed
per build-minute on the default pool (or on a private pool, if W0 finds the
default pool cannot reach the cluster, which has its own price).

**Shared-pool interaction:** `var.pool_limits.providers.anthropic` must be at
least *(tenants holding the provider) × `provider_tenant`*
(`terraform/infra/variables.tf`). New people hold no provider, so they hold no
`provider:anthropic:tenant:*` pool. Their claude-code work is bounded by the
account pool and by the shared `provider:anthropic` pool. A new person cannot
push the platform past its shared ceilings, because every reservation is
all-or-nothing across every pool (invariant 2).

---

## 9. Decisions

Each decision keeps its options, for history. **Every decision is now made:**
the owner decided them all on 2026-10-08, the last (WD2, WD3, WD9 and the
confirmed defaults) after 05:00 UTC. Superseded options and recommendations
appear only here.

### WD1. Where do personal worker identities live?

* (a) A dedicated identity project, administered by a runtime provisioner.
* (b) The shared project, with project-level `serviceAccountAdmin` for the
  release deployer. Breaks rule 2 and #334.
* **(c) The shared project, with each account's IAM done by a dedicated,
  guarded identity** (§2.4).

**Decided 2026-10-08 (owner): (c).** Identities stay in `saga-agents-staging`,
and there is **no** privileged runtime provisioner with account-admin power.
Any admin approves a person with one click in Admin → People; that is the only
human step. SwarmCloud then triggers a guarded apply with a dedicated identity
that allows only creations of that person's own resources. Anything else stops
and asks the owner (§2). The first wording, "triggers CI, which plans the
bootstrap and infra layers", was refined by WD2 (a Cloud Build job) and WD3
(the script, not Terraform).

### WD2. What triggers the guarded apply?

The first version asked what runs a provisioner: (a) a Cloud Run Job, (b) a
GitHub Actions workflow, (c) Cloud Workflows. Under WD1 (c) the question became
what starts the apply:

* (a) swarm-api dispatches a GitHub Actions workflow, `workspace-apply.yml`,
  with a new single-purpose GitHub App holding `actions: write` on this
  repository. Its logs would be public, so every command's output had to be
  redirected and masked, and the App's token could start other workflows. This
  was the earlier recommendation.
* **(b) Pub/Sub → a Cloud Build trigger in the project**, with private logs.
* (c) A scheduled GitHub workflow polling Firestore: no credential in
  swarm-api, but minutes of delay and an empty public run every 5 minutes.
* Reusing `swarmcloud-merge` was considered and refused: merge-step.md §5
  retires it.

**Decided 2026-10-08 (owner): (b).** Approval publishes to Pub/Sub, and a Cloud
Build job in the project does the work, so its logs stay private in Cloud
Logging (§2.1, §2.6). The accepted safeguard "usable only by
`workspace-apply.yml` on `main`" becomes "`swarm-workspace-deployer` is usable
only by that Cloud Build trigger, which builds only `main`". CODEOWNERS on the
guard and the alert on a change to any account not named
`swarm-agent-worker-*` stay as they were (§2.4).

### WD3. What makes a person's resources?

The first version asked whether a provisioner runs `register-tenant.sh` or a
port of it. Under WD1 (c):

* (a) The existing Terraform modules, instantiated once per workspace id
  through a `module.personal_workspace`, guarded by a plan guard over
  `terraform show -json`. This was the earlier recommendation.
* **(b) `scripts/register-tenant.sh`, run by the job**, in a new `--workspace`
  mode.
* (c) A new standalone module, written for people. A third definition.

**Decided 2026-10-08 (owner): (b).** Consequences, each reflected above:

1. The guard is a wrapper around each cloud and cluster call the script makes,
   allowing only creations and bindings of that workspace id's resources (and
   the per-person prefix-conditioned bindings on the fixed shared resources)
   and stopping on anything else. It no longer reads a Terraform plan (§2.5).
2. Personal workspaces are created outside Terraform state. Terraform owns
   group and service tenants only, and nothing in Terraform may plan to change
   or destroy a script-created personal resource: there are no resources for
   them, Terraform keeps to additive bindings where they are members, and a
   `terraform test` asserts personal ids never appear in a plan (§3.2).
3. The script's act-as gap (§0) is closed in this mode (A4).

### WD4. Who owns which tenants?

* (a) T1: Terraform owns groups and service tenants, a provisioner owns people,
  and `u-bogdan` is grandfathered.
* (b) T1, and migrate `u-bogdan` to the provisioner.
* (c) T2: every workspace becomes a `dev.tfvars` pull request.
* (d) T4: Terraform owns both, groups and service tenants through
  `dev.tfvars` and personal workspaces through the private list.

**Decided 2026-10-08 (owner): (d), then superseded the same day by WD3 (b).**
Terraform owns group and service tenants only; personal workspaces are the
script's, outside state, with the job in place of the provisioner (§3.2).
`u-bogdan` still leaves `dev.tfvars`, now by `removed` blocks that destroy
nothing (§3.3). What (d) got right survives: there is no per-person pull
request, and the list stays private.

### WD5. A new workspace's limits?

* **(a) 8 / 8, quota 16 pods / 64 vCPU** (§8).
* (b) Today's API default, 20 / 40 (pool 20, quota 80 vCPU).
* (c) 4 / 8, like the verify tenant.

**Decided 2026-10-08 (owner): (a)**, pool 8 and capacity_units 8, namespace
16 pods / 64 vCPU, raisable per person by an admin in People.

### WD6. What does a ready workspace run on?

* **(a) Nothing until the person adds or borrows an account.**
* (b) A platform pool account lent by default.
* (c) An empty `anthropic` secret the person fills through an operator.

**Decided 2026-10-08 (owner): (a).** The checklist gains "Add a Claude account
(or ask an admin to lend one)" after the workspace, with [Add key] and
[Request a loan]. A submission before then is refused, `NO_CLAUDE_ACCOUNT`,
"No Claude account yet" (§5).

### WD7. Must a member of a group tenant also have a personal workspace before submitting?

* **(a) No.** The gate is on the tenant the submission resolves to.
* (b) Yes, for everyone.

**Decided 2026-10-08 (owner): (a).** The gate applies only to personal
submissions; team (group) submissions are never blocked.

### WD8. When does the gate turn on?

* (a) Once one workspace has been made end to end and the backfill has run.
* (b) Now, accepting that new people cannot run until the apply ships.

**Decided 2026-10-08 (owner): (a), made precise.** The gate ships **off**. It
is turned on only after one real person has been approved through the console
end to end (workspace ready and a task run), with the existing personal
tenants (`u-bogdan`) marked ready in the same step (§5.5).

### WD9. Bucket and project grants: role-bounded, or member-bounded?

The guard checks every member (C4, C5), so this asks what the dedicated
identity *holds*:

* (a) `hasOnly` role conditions on its project and bucket grants, plus the
  alert (§2.4 R4).
* **(b) A principal-set grant**, made once by Terraform, of the database and
  bucket-metadata roles to the set of all personal worker identities, so the
  job edits no shared permission list per person. This holds only if W0
  confirms IAM supports such a set in an allow policy.
* (c) A Google group of personal workers. This needs Cloud Identity API access,
  which this project has found unreliable (`searchTransitiveGroups` 403s).

**Decided 2026-10-08 (owner): (b), with (a) as the fallback.** Terraform makes
the database and bucket-metadata roles a one-time grant to that principal set
(the two telemetry roles ride the same grant, §2.3). Only each person's
`tenants/<tenant>/` bucket condition stays per person. If W0 shows the
principal set is not supported, or that the only available set is wider than
the personal workers, the fallback is per-person edits limited by `hasOnly`
role conditions, plus the alert on any member not named
`swarm-agent-worker-*`.

### Defaults the owner confirmed

**Decided 2026-10-08 (owner)**, each as this document already proposed:

1. **An admin may approve their own request**, and the audit records it
   (§1.3).
2. **A 24-hour wait before re-requesting after a denial.** Admins may still
   approve at any time (§1.3).
3. **Admins lend only accounts owned by a group tenant or by themselves**
   (§6.4).
4. **`NO_CLAUDE_ACCOUNT` applies to every runner profile** (§5.1).

### PEOPLE. The user-management screen

**Decided 2026-10-08 (owner): all four parts in v1** (§6.4): everyone who has
signed in; pending requests with Approve / Deny and live progress; per-person
ceiling and lending or reclaiming a Claude account; granting or removing
admin, every change recorded with who and when.

### PRIVACY. Where the list of approved people lives

**Decided 2026-10-08 (owner):** out of the repository, in SwarmCloud's private
store, read by the Cloud Build job at run time (WD2) and never by Terraform
(WD3); public artefacts name a person only by an opaque workspace id (§2.6). This **supersedes** the approval comment's
"SwarmCloud opens and merges the config change for the person": there is no
per-person pull request, because the config change is the private record.

---

## 10. Build plan

Within a phase no file is in two lanes, and a lane depends only on earlier
phases. New files are named without their root.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| W0 | 0 | **verification, no code**: whether an IAM allow policy accepts a principal set naming exactly the personal worker accounts (WD9; if not, the fallback); every principal holding `actAs` or token-creator at the project level in the shared project (§2.4 safeguard 1); that updating or running the trigger needs `actAs` on its account; that a Cloud Build default-pool build reaches the GKE control plane under `master_authorized_cidrs` (if not, a private pool, which is an owner decision on cost); that the build's log can be routed to a restricted log bucket and excluded from `_Default` (§2.6); that the stock `google-cloud-cli` image carries every tool the script needs; `ALREADY_EXISTS` handling of a pre-made slot. Each result is dated in this document | `docs/workspaces.md` | — |
| W1 | 1 | the record and `workspace_ids/`, `GET`/`POST /v1/workspace`, the loan request, the `workspace` and `claude_account` onboarding steps, `WorkspaceNotReady` and `NoClaudeAccount`, the gate behind `WORKSPACE_GATE=off`; `people/` writes; `tenant_for` stops creating `u-*` once the gate is on | new `swarm_api/workspaces.py`, new `swarm_api/routes/workspaces.py`, `apps/swarm-api/swarm_api/service.py`, `apps/swarm-api/swarm_api/errors.py`, `apps/swarm-api/swarm_api/onboarding.py`, `apps/swarm-api/swarm_api/main.py` | W0 |
| W2 | 1 | admin roles in Firestore (§6.5): `admin_roles/`, `admin_audit/`, `PLATFORM_OWNER`, the one-time migration of `admin_users`, the grant and remove routes with the owner and last-admin safeguards | new `swarm_api/admins.py`, new `swarm_api/routes/people.py`, `apps/swarm-api/swarm_api/auth.py`, `apps/swarm-api/swarm_api/settings.py` | W0 |
| W3 | 1 | **the call guard**: `scripts/lib/workspace-guard.sh` with C0–C9 in `scripts/lib/workspace-calls.json`, the three shims, `--report-only`, its self-test cases (every rule refusing a call built to trip it), the no-absolute-path test, and the guard-aware `kubectl_bin` and `prefer_local_bin`; `.github/CODEOWNERS` on the guard, the script, `common.sh` and the build file | new `lib/workspace-guard.sh`, `lib/workspace-calls.json`, `lib/workspace-guard-cases.json` and `lib/guard-bin/` in scripts/, `scripts/lib/common.sh`, `.github/CODEOWNERS`, new `tests/unit/scripts/test_workspace_guard.py` | W0 |
| W4 | 2 | **Terraform, owner-approved once**: in bootstrap, `swarm-workspace-deployer` (no key, no WIF, no `actAs` member), its custom roles and conditioned grants, the `swarm-workspace-apply` topic with swarm-api as its only publisher, the Pub/Sub Cloud Build trigger building `main` only, the restricted log bucket and sink, the `swarm-tenant-u-` slot bindings for swarm-api, **the WD9 principal-set grant** (or, on the fallback, the conditioned `projectIamAdmin`), and the validation of the identity's role list; the alerts of §2.4; in infra, the validation refusing a human user tenant; `terraform test` assertions, including `personal_workspaces_absent` (§3.2) | new `workspace_deployer.tf` in terraform/bootstrap, `terraform/bootstrap/forge_user_slots.tf`, `terraform/bootstrap/variables.tf`, `terraform/modules/monitoring/`, `terraform/infra/variables.tf`, new `personal_workspaces_absent.tftest.hcl` and `workspace_deployer.tftest.hcl` in tests/terraform | W0 |
| W5 | 2 | **cluster, owner-applied once**: the deployer's ClusterRole, binding and kube-system Role, and the scope ValidatingAdmissionPolicy, plus the parity test that the policy's Role literals equal the render | new `rbac/provisioner-rbac.yaml` and `policies/workspace-provisioner-scope.yaml` in kubernetes/, `kubernetes/render.py` (`POLICY_FILES`), `kubernetes/apply.sh` (`--policies` looks up the uniqueIds) | W0 |
| W6 | 2 | **`register-tenant.sh --workspace`** (§4.1): the record read, the derived-id check, `--mode create`, `limits` and `verify`, A1–A9 with progress writes, **the act-as grant**, the narrowed squat inspection, the forge slot, the refusal to start without the guard; and **the build file** `scripts/cloudbuild/workspace-apply.yaml` (validate, install the guard, run) | `scripts/register-tenant.sh`, new `cloudbuild/workspace-apply.yaml` in scripts/, `tests/unit/scripts/` (new `test_register_tenant_workspace.py`) | W3 |
| W7 | 2 | People and the approval flow in swarm-api: the admin list, approve, deny, retry, limits, loan, the Pub/Sub publish and the sweep route | `swarm_api/routes/people.py` (W2's new file, extended), new `swarm_api/publish_workspace.py`, `apps/swarm-api/swarm_api/routes/accounts.py` | W1, W2 |
| W8 | 3 | the console: the checklist steps, the progress view, the Submit banners, Admin → People; the plugin: `sc setup`, `swarm_setup_workspace`, `/sc:setup`; the issue forms' "Where" list | `apps/swarm-ui/src/GitHubConnect.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/SubmitWorkflow.tsx`, `apps/swarm-ui/src/App.tsx`, new `People.tsx` in apps/swarm-ui/src, `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/server.py`, `plugin/commands/setup.md`, `.github/ISSUE_TEMPLATE/` | W1, W7 |
| W9 | 4 | **the `u-bogdan` migration**: the `removed` blocks in both layers, its removal from `dev.tfvars`, the record, and a `--mode verify` run; the release's plan must read 0 to add, 0 to change, 0 to destroy | `terraform/infra/removed.tf`, `terraform/bootstrap/removed.tf`, `terraform/environments/dev/dev.tfvars` | W4, W6 |
| W10 | 5 | the first real approval end to end, then `WORKSPACE_GATE=on` (WD8); docs: multi-tenancy, offboarding runbook, onboarding, ci.md (the Cloud Build job and where its logs are), register-tenant.sh's header | `docs/multi-tenancy.md`, `docs/runbooks/tenant-offboarding.md`, `docs/onboarding.md`, `docs/ci.md` | W5, W8, W9 |
| — | later | deprovisioning (§7), with its own guard mode | — | the owner's decision then |

**The owner's one-time steps:**

* **Connect this repository to Cloud Build** (a second-generation repository
  connection, read only), so the trigger can build `main`. This installs
  Google's Cloud Build App on the repository; it holds no write access.
* **W4** is a bootstrap change, so it is an owner-run bootstrap apply from
  `main`, as every bootstrap change is: the identity, its roles, the topic, the
  trigger, the log bucket and the WD9 grant. Its infra half is a release whose
  IAM plan waits in `dev-iam`. Custom roles follow
  `docs/runbooks/custom-roles-to-bootstrap.md`.
* **W5** is an owner-run `kubernetes/apply.sh --policies --confirm`.
* **W9** is an owner-run bootstrap apply of the `removed` block; the infra half
  is a release whose plan he checks reads 0 to add, 0 to change, 0 to destroy.
* **If W0 needs a Cloud Build private pool** to reach the cluster, creating it
  (and its cost) is his decision then.

After those, **no person's onboarding needs the owner**, unless the guard stops
a run. W3, W4, W6 and W7 get the one review (credentials, tenant isolation,
IAM). W1 gets it too, for the gate.

---

## 11. Invariants, each with how it holds

1. **Demand only from `LEASED`…`RUNNING`.** Requesting, approving and applying
   a workspace create no task and no lease. A ready, idle workspace holds
   nothing, and the Cloud Build run is not a worker.
2. **All-or-nothing reservation.** Unchanged. A8 writes a pool document's
   limit, never its `active`; a pool that exists keeps its `active`.
3. **Concurrency from `LEASED`.** Unchanged. Lowering a ceiling refuses new
   leases above it and touches nothing running.
4. **Workers never sleep through a wait.** The run is not a worker. swarm-api
   and the plugin never wait inside a request or a tool call for a run; the
   console polls.
5. **Fencing.** Unchanged. The claim in A1 is the run's own fence: a second
   run finds `applying` with a live build and exits, and a guard stop leaves
   nothing half-applied that a later run would trust: every step reads before
   it writes, and A9 re-reads everything before `ready`.
6. **No Spot.** Nothing here declares any.
7. **`requests == limits`.** The personal ResourceQuota follows it (§8).
8. **Checkpointing.** Unchanged for workers.
9. **Isolation.** This is the point of the design. Every workspace gets its own
   account, its own prefix-conditioned grants, its own namespace with
   default-deny networking, and its own slot, made by the same script that
   registers a group tenant, and the guard refuses any call that would give
   any of them to another member, or touch another tenant's resources (C2–C8).
10. **Profiles by name.** No new route accepts an image, a command, a resource
    spec or a backend parameter. The workspace routes take no body fields that
    shape infrastructure; the one admin input, a ceiling, is a number the
    server turns into a quota by a fixed ratio.
