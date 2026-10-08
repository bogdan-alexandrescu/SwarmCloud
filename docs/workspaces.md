# Personal workspaces: approved by an admin, applied by a guarded CI run

**Status: DESIGN, revised 2026-10-08 with the owner's decisions (part of
#847). None of it is built.** The owner asked on 2026-10-08 for each person's
personal space to be "onboarded at the time of onboarding a new user in
swarmcloud", with "a way to create this via the UI and potentially via the
plugin". They also asked that it "should block starting any task or workflow
before this exists".

The first version of this document proposed a locked-down runtime
provisioner. The owner's decisions of 2026-10-08, posted on #847 and on this
design's pull request, replaced it. **This document describes the chosen
design throughout**; §9 records every decision with its options, for history.

* **Identities stay in the shared project (WD1 (c)).** There is **no**
  privileged runtime provisioner holding account-admin power. A person's
  identity and its IAM are made by a Terraform apply, as every other tenant's
  are.
* **Approval is one click by any admin**, in a new console screen,
  **Admin → People**. That click is the only human step. SwarmCloud then
  triggers a CI run that plans the bootstrap and infra layers for that one
  person and applies them with a **dedicated identity**, behind a **plan
  guard** that allows only *creations* of *that person's own* resources.
  Anything else stops the run and asks the owner.
* **The list of approved people is private.** The repository is public, so
  the list lives in Firestore and CI reads it at plan time. A pull request, a
  plan, a summary or a log names a person only by an **opaque workspace id**,
  `w-` and six random hex digits (`w-3f9a2c`). No email, name or name-derived
  tenant id appears in anything public.
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
* **The apply is a GitHub Actions workflow, `workspace-apply.yml`**,
  dispatched by swarm-api with the workspace id as its only input. It runs as
  a new identity, `swarm-workspace-deployer`, that only that workflow on `main`
  can impersonate (§2).
* **The plan guard is the bound.** IAM cannot narrow service-account
  administration to a name prefix in the shared project (§2.4), so the
  dedicated identity's power is bounded by what it is allowed to *apply*: a
  plan of creations under one workspace's addresses, with every IAM member
  checked (§2.5).
* **Terraform owns every tenant.** Groups and service tenants come from
  `dev.tfvars`, and people come from the private list. `u-bogdan` moves into
  the private list with a plan that changes nothing (§3, WD4).
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
  run. Personal workspaces no longer go through the script (§4.1), so this
  design does not depend on it, but the script still has the gap for group
  tenants. It is reported as an out-of-territory finding on the PR.
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
(§2.6). Nothing copies it into the repository, and CI reads it at plan time.

| field | type | written by | meaning |
|---|---|---|---|
| `tenant_id` | string | swarm-api | `u-alice`; equals the document id. **Private**: it is derived from the email, so it is a name |
| `workspace_id` | string | swarm-api | `w-3f9a2c`: `w-` and 6 hex digits from `secrets.token_hex(3)`, **random, not derived from the email**, so nobody can recover it by hashing a guessed address. It is unique (§1.3). This is the only id that appears in anything public |
| `principal` | string | swarm-api | `alice@saga.xyz`, lower-cased, from the verified token, never from the body |
| `state` | string | swarm-api, the workflow | §1.2 |
| `request_id` | string | swarm-api | a fresh UUID for each accepted request or retry; the workflow logs it, privately |
| `requested_at`, `requested_via` | timestamp, string | swarm-api | `console`, `plugin` or `api` |
| `decision` | map or null | swarm-api | `{by, at, verdict: approved/denied, reason}`. `reason` is required for a denial and is shown to the person. `by` is an admin's email; it never leaves Firestore |
| `limits` | map | swarm-api | `{max_active, capacity_units, quota_pods, quota_cpu}`. Defaults from §8; an admin raises them in People (§6.4) |
| `run` | map or null | swarm-api, the workflow | `{run_id, attempt, dispatched_at, mode}` of the current CI run. The console links to the run, whose public page shows only the workspace id |
| `steps` | map | the workflow | `step id → {state: todo/running/done/failed/held, at, code}`, using the ids in §4.2 |
| `failure` | map or null | the workflow | `{step, code, retryable, at}`. The copy is served from the code (§4.3), never stored free-form, so no command output reaches Firestore |
| `ready_at` | timestamp | the workflow | written by the verify step only |
| `migrated` | bool | the migration (§3.3) | `true` for `u-bogdan`, whose resources predate the record |

### 1.2 States

| state | entered when | the person sees | submissions to their own tenant |
|---|---|---|---|
| (no record) | never requested | checklist step "Request your workspace", button enabled | refused, `WORKSPACE_NOT_READY` |
| `requested` | swarm-api accepted a request | "Waiting for an admin to approve" | refused |
| `denied` | an admin denied it | "Not approved: {reason}", and "Request again" | refused |
| `approved` | an admin approved it; the dispatch is being made | "Approved. Setting up…" | refused |
| `applying` | the workflow claimed the record | each step of §4.2 with a tick or spinner | refused |
| `needs_owner` | the plan guard refused the plan (§2.5) | "Approved. A change needs the platform owner's review before it can finish." | refused |
| `failed` | a step failed past its retries | the step's code and copy (§4.3) | refused |
| `ready` | the verify step read back every object | the step is ticked; the Claude account step comes next | admitted once a Claude account exists (§5) |

`ready` is **evidence-derived on the workflow's side**: it is written only
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
  (`WORKSPACE_REQUEST_TOO_SOON`).
* **`failed`:** answers 409 with the failure copy. A retry is an admin's
  action (§6.4), because it starts a CI run with a privileged identity.

**Approve and deny** are `POST /v1/admin/workspaces/{workspace_id}/approve`
and `/deny` (§6.3). Both need `is_admin` (§6.5). Approve is accepted only from
`requested`; deny from `requested` or `failed`, and it needs a non-empty
reason of at most 500 characters. Each writes `decision` and an
`admin_audit` entry in the same transaction. An admin may approve their own
request: the plan guard bounds what any approval can create, so a self-approval
creates nothing a different admin's approval would not, and the audit shows it.

After an approval, swarm-api dispatches the workflow (§2.1). A failed dispatch
is not a failed approval: the record stays `approved` and the dispatch sweep
retries it (§2.2).

### 1.4 Frozen contract

None is needed. `workspaces/`, `workspace_ids/`, `people/`, `admin_roles/` and
`admin_audit/` are new collections read only by swarm-api and the workflow.
`Tenant` gains no field, and the scheduler's admission does not read the
workspace (the gate is at submission; §5.4 says why that is enough). The
derived ids stay `identity.py::tenant_id_for_user` and
`identity.py::worker_service_account_id`.

---

## 2. The guarded apply

### 2.1 What triggers it

The owner chose CI as the thing that applies a person's workspace. What starts
that CI run is still open (WD2). The options:

| | **(A) swarm-api dispatches `workspace-apply.yml` with a single-purpose GitHub App** | (B) Pub/Sub → Cloud Build trigger | (C) A scheduled GitHub workflow that polls Firestore |
|---|---|---|---|
| how it starts | after an approval, swarm-api mints an installation token from a new App, `swarmcloud-workspaces`, and calls `workflow_dispatch` on `workspace-apply.yml` at `main` with one input, `workspace_id` | swarm-api publishes the workspace id to a topic; a Cloud Build trigger subscribed to it runs the same steps | a `schedule:` workflow every 5 minutes reads `workspaces/` for `approved` records |
| what swarm-api gains | a private key whose token has `actions: write` on this one repository. GitHub cannot narrow that to one workflow, so it can also dispatch, re-run or cancel other workflows (§2.4 R3) | `pubsub.topics.publish` on one topic | nothing |
| identity that applies | `swarm-workspace-deployer`, through the existing WIF pool, pinned to this workflow file at `refs/heads/main` (§2.3) | the trigger's service account | as (A) |
| where it is gated and reviewed | GitHub environments, the same mechanism as `dev-iam`, so the owner's fallback (§2.5) is the reviewer prompt he already uses | Cloud Build has approvals, but they live in the console, a second place to review | as (A) |
| logs | **public**, so every command's output is redirected to a private object and the public log carries only the workspace id (§2.6) | private (Cloud Logging) | public, as (A), plus an empty run every 5 minutes |
| latency | seconds to queue, then the run (5 to 15 minutes) | similar | up to 5 minutes before the run, often more: GitHub delays scheduled runs under load |
| owner's one-time step | create the App, install it on this repository, store its key with `scripts/create-secrets.sh --stdin` | connect the repository to Cloud Build | none |

`swarmcloud-merge`, the App that merges `ready` pull requests today, is not an
option: [merge-step.md](merge-step.md) §5 retires it once the merge step has
merged its proving set, and giving it a second job would keep it alive for a
reason that step does not know about.

**Recommendation: (A).** It keeps the apply in the CI the owner named, with
the review fallback in the environment he already approves `dev-iam` in. Its
costs are a public log, handled by §2.6, and a token that can start other
workflows, handled by the workflow refusing anything not approved in Firestore
(§2.2, step A1) and by the release's own approval for prod.

### 2.2 How a run proceeds

`workspace-apply.yml` takes one input, `workspace_id`, validated against
`^w-[0-9a-f]{6}$` before anything else runs. It has `concurrency: group:
workspace-apply, cancel-in-progress: false`, so runs are serial, and it shares
the Terraform state lock with the release (`-lock-timeout=10m`).

| id | step | public log shows |
|---|---|---|
| A1 | **claim.** Read `workspaces` where `workspace_id == input`. Refuse unless the record is `approved` (or `needs_owner` on the owner's path) and `decision.verdict == approved` by an email that is an admin in `admin_roles/` now. In one transaction set `applying` and `run`. Then `::add-mask::` the tenant id, the email, the worker account and the namespace | `claimed w-3f9a2c` |
| A2 | **read the private list** (§2.6) into `$RUNNER_TEMP`, mode 0600 | `personal workspaces read: 4` |
| A3 | **plan the infra layer** with `-target='module.personal_workspace["w-3f9a2c"]'`, `-out`, all output to the private log | `infra plan: written` |
| A4 | **guard the infra plan** (§2.5) | the verdict and, on a refusal, each refused row as (address, action, rule id) |
| A5 | **apply the infra plan**: the account, its IAM, the project and bucket members, the forge slot, the tenant and pool documents, the jobs | `infra apply: 23 created` |
| A6 | **plan and guard the bootstrap layer**: one resource, the release deployer's `serviceAccountAdmin` on the new account (§3.2), then apply it | `bootstrap: 1 created` |
| A7 | **apply the namespace** with `kubernetes/apply.sh` and the current step-spec keys | `namespace: applied` |
| A8 | **write the record's limits** to the tenant and pool documents, as `register-tenant.sh` §6 does | `limits: written` |
| A9 | **verify**: re-read the account, its policy, the bucket and project members, every object `kubernetes/render.py::TENANT_FILES` produces, the slot binding and both documents. Only then set `ready` and `ready_at` | `verified: 41 of 41 objects` |

The infra layer comes **before** the bootstrap layer, which is the reverse of
today's operator order (§0). That works because the dedicated identity may set
IAM on a new account itself (§2.3); the bootstrap grant exists only so that
later releases, which run as the release deployer, can manage the account's
IAM when a module changes.

**Each step records itself** on the record (`steps.<id>`) when it starts and
ends, which is what the console's progress view reads (§6.1, §6.4).

**A refused guard** (A4, A6) does not fail the run. It holds the plan, sets
`needs_owner`, and hands over to the job `owner-review`, which names the
`dev-iam` environment (§2.5).

**Retries.** A step whose command fails on `429`, `5xx`, `UNAVAILABLE`,
`DEADLINE_EXCEEDED` or IAM's `409 concurrent policy change` is retried 3 times
with backoff 4, 16 and 64 seconds. Any other failure sets `failed` with a code
(§4.3). A re-run of a failed workspace plans afresh, and Terraform plans only
what is still missing, so a partial apply resumes rather than repeats.

**The dispatch sweep.** An `approved` record whose dispatch failed, or whose
run never claimed it within 10 minutes, is dispatched again by
`POST /v1/admin/workspaces/sweep`, which the existing `swarm-tick` scheduler
identity calls every 5 minutes, beside its other admin sweeps. The route
dispatches at most one run per record per 10 minutes, and records each
attempt.

### 2.3 The dedicated identity and its permissions

**Account:** `swarm-workspace-deployer@<project>.iam.gserviceaccount.com`. It is
created by the owner's bootstrap apply (`terraform/bootstrap/`), holds **no
key**, and is impersonable through the existing WIF pool only by
`principalSet://…/attribute.job_workflow_ref/<repo>/.github/workflows/workspace-apply.yml@refs/heads/main`,
the same per-file pattern `terraform/bootstrap/wif.tf` already uses. The
release deployer cannot act as it, and it cannot act as the release deployer.

| role | on | why | bound by |
|---|---|---|---|
| custom `swarmWorkspaceAccountAdmin` (`iam.serviceAccounts.create`, `.get`, `.list`, `.getIamPolicy`, `.setIamPolicy`) | **the project** | create a person's worker account and write its Workload Identity and act-as bindings, before any per-account grant can exist | **nothing in IAM** (§2.4). It has no `delete`, `disable`, `keys.create`, `getAccessToken`, `signBlob` or `actAs`. Its use is bounded by the plan guard and watched by the alert (§2.4) |
| `roles/resourcemanager.projectIamAdmin` | project, **conditioned** | add the worker's three project roles | `hasOnly([...])` over exactly the worker Firestore custom role, `roles/logging.logWriter` and `roles/monitoring.metricWriter`, the pattern `terraform/bootstrap/deployer_conditions.tf` uses. Unnecessary if W0 confirms the principal-set grant (WD9 (b)) |
| custom `swarmWorkspaceBucketIam` (`storage.buckets.get`, `.getIamPolicy`, `.setIamPolicy`) | **the artifact bucket only** | the worker's two prefix-conditioned object grants and the bucket-metadata role | the bucket. Not `roles/storage.legacyBucketReader`, which carries `storage.objects.list` |
| custom `swarmWorkspaceSecretBinder` (`secretmanager.secrets.get`, `.getIamPolicy`, `.setIamPolicy`) | project, **conditioned** | bind the worker to its forge slot | `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-u-")`, the full-name form `terraform/bootstrap/forge_user_slots.tf` writes |
| custom `swarmForgeSlotCreator` (exists) | project | create the empty slot and its twin | the project; creation is checked there. The same role swarm-api holds |
| custom `swarmWorkspaceFirestore` (`datastore.entities.get`, `.list`, `.create`, `.update`, `datastore.databases.getMetadata`) | project | read the private list, write progress, the tenant and pool documents | **the project**; Firestore ignores conditions on the data plane (`scripts/register-tenant.sh` §3). No `delete` |
| `roles/run.developer` | project, **conditioned** to job names starting `projects/<project>/locations/<region>/jobs/swarm-worker-u-` | the tenant's Cloud Run jobs | the name prefix |
| `roles/iam.serviceAccountUser` | **on each worker account it creates**, granted by its own A5 apply through the tenancy module's act-as members | Cloud Run requires `actAs` on a job's identity | the account, written by a guarded plan |
| `roles/container.clusterViewer` | project, **conditioned** to the swarm cluster's resource name | `get-credentials` for the namespace step | the expression `terraform/modules/iam/bindings.tf` builds; nothing on the other team's cluster |
| `roles/storage.objectUser` | the state bucket, **conditioned** to `infra/dev/`, `bootstrap/` and `workspace-runs/` | the two layers' state and lock, and the private run logs (§2.6) | the prefixes |

**In the cluster**, the user is the identity's email. The files are new:
`rbac/workspace-deployer-rbac.yaml` and `policies/workspace-deployer-scope.yaml`
under kubernetes/.

| object | grants | why it is not enough on its own |
|---|---|---|
| ClusterRole `swarm-workspace-deployer` + ClusterRoleBinding | `namespaces`: `get`, `create`, `patch`. `serviceaccounts`, `configmaps`, `resourcequotas`, `limitranges`, `networking.k8s.io/networkpolicies`, `rbac.authorization.k8s.io/roles`, `rolebindings`: `get`, `create`, `patch`. `roles`: `escalate`, and `bind` with `resourceNames: [swarm-worker, swarm-dispatcher, swarm-reaper]`. **No `delete`, and nothing on `Secret`, `Pod` or `Job`** | RBAC cannot restrict `create` by name, so a ClusterRole that creates namespaces creates *any* namespace |
| Role `swarm-workspace-deployer-dns` in `kube-system` | `get` on `services` `kube-dns` and `daemonsets` `node-local-dns` (`resourceNames`) | the two reads `kubernetes/cluster-network.sh` makes |
| **ValidatingAdmissionPolicy `swarm-workspace-deployer-scope`** + binding | for any request by this user: a `Namespace` must be named `swarm-tenant-u-*`, and a namespaced object must be in a `swarm-tenant-u-*` namespace. A `Role` must be one of the three names, with the rules `kubernetes/render.py` renders (a parity test holds the literal to the render). A `RoleBinding` may only reference those Roles | this turns "any namespace" into "`swarm-tenant-u-*` only". `kubernetes/policies/pod-security.yaml` is the precedent |

**What it can never touch on the shared deny-list** (`scripts/lib/common.sh`,
`SHARED_DENY_LIST`): it holds no `compute.*`; its only `container.*` grant is
conditioned to the swarm cluster, and `kubernetes/apply.sh`'s three cluster
refusals run on every apply; its storage grants are on the artifact bucket's
policy and on state prefixes; its secret binder is prefix-conditioned. Its one
unbounded grant, `swarmWorkspaceAccountAdmin`, *could* reach the other team's
accounts if misused, which is why the guard refuses any plan whose IAM touches
an account other than the new worker (§2.5, rule G5), and why the alert below
exists.

### 2.4 The finding that shapes this, and why the identity fits #334

A worker account needs two kinds of binding on *its own* IAM policy:
`roles/iam.workloadIdentityUser` for `[swarm-tenant-u-alice/swarm-agent-worker]`
and `[.../swarm-worker]`, and `roles/iam.serviceAccountUser` for the scheduler,
the reconciler and the deployer. Writing them requires
`iam.serviceAccounts.setIamPolicy` on the account. "IAM resources don't provide
the resource name" to a condition. This repository established that in #334
and recorded it in `terraform/bootstrap/deployer_service_accounts.tf` and in
the `terraform/bootstrap/variables.tf` validation that refuses project-level
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
* **(c) The shared project, with each account's IAM done by a release.**
  **Chosen.** The first version said this needed the owner for each person.
  The owner's approval flow removes that: the power sits with a dedicated CI
  identity, and the per-person approval is an admin's click.

**Why the dedicated identity fits #334.** #334 refused project-level account
admin to the release deployer because that identity applies *any* plan that
reaches `main`. Three things differ here:

1. **It applies only guard-approved plans.** Its one workflow refuses to apply
   a plan the guard has not passed, and the guard passes only creations under
   one workspace's addresses, with every IAM member and target checked (§2.5).
   A plan that touches an existing account's policy, another tenant, or a role
   outside the list stops and goes to the owner.
2. **Nothing else can use it.** WIF admits only `workspace-apply.yml` at
   `refs/heads/main`. The release, the CI fixer, a pull request and the
   release deployer cannot impersonate it.
3. **It is a separate identity.** The release deployer keeps its per-account
   grants, and its validations stay. #334's rule "CI never holds project-level
   `serviceAccountAdmin`" now reads "the release deployer never does; one
   guarded workflow does", and `terraform/bootstrap/variables.tf` gains a
   validation that the new identity's roles are exactly the list in §2.3.

**Residual risk, stated so it is not mistaken for zero:**

* **R1. The guard is code, not IAM.** A change to `workspace-apply.yml`,
  `scripts/lib/plan-guard.sh` or `scripts/lib/workspace-plan.jq` that merges to
  `main` changes what the identity will apply. Mitigation: `.github/CODEOWNERS`
  names the owner on those three files, and branch protection requires the code
  owner's review for them. The guard's self-test runs in CI on every change.
* **R2. A stolen token of this identity** (from a compromised runner during a
  run) could set IAM on any account in the shared project for the token's life.
  Mitigation: **detective**, a Cloud Logging metric and alert on every
  `SetIamPolicy` by `swarm-workspace-deployer` whose target is not
  `swarm-agent-worker-u-*`, or whose added member is not that account's worker
  or the platform's three control-plane accounts; and the run is short and
  serial, so a token outside a run is itself an anomaly.
* **R3. The dispatch App's token** can start, re-run or cancel any workflow in
  the repository. A forged dispatch of `workspace-apply.yml` does nothing,
  because A1 refuses a record no admin approved. A dispatch of the release to
  `dev` deploys `main`, which a merge does anyway; to `prod` it waits for the
  release's own approval.
* **R4. Bucket and project members.** The identity's `hasOnly` grants constrain
  *roles*, not *members*. The guard checks members (G5); the alert covers the
  rest. WD9 (b) would remove the project half.

### 2.5 The plan guard for a person's plan

A new mode, `scripts/lib/plan-guard.sh --mode workspace --workspace w-3f9a2c
--expect <private json>`, with its rules stated once in a new
`scripts/lib/workspace-plan.jq`, as `iam-plan.jq` states the IAM rule. It reads
`terraform show -json` of the saved plan. `--expect` is a file in
`$RUNNER_TEMP` holding the record's tenant id, worker account email and
bucket, which the rules compare against. **It prints only addresses, actions
and rule ids, never an attribute value.**

The plan is allowed only when **every** rule holds. Data-source reads and
`no-op` changes are ignored; everything else is a change.

| rule | the plan must… | refused example |
|---|---|---|
| G0 | satisfy the existing apply rules: no change to anything on `SHARED_DENY_LIST`, and everything created carries `managed-by=swarm-terraform` (or is of an unlabelable type) | a resource without the label |
| G1 | address every change under `module.personal_workspace["w-3f9a2c"].` (infra layer), or be exactly `google_service_account_iam_member.deployer_personal_sa_admin["w-3f9a2c"]` (bootstrap layer) | `module.tenancy.google_service_account.worker["eng"]` |
| G2 | have `actions == ["create"]` on every change: no update, delete, replace, forget or import, and no `previous_address` (no moves) | an in-place update of the person's own account |
| G3 | use only these types: `google_service_account`, `google_service_account_iam_member`, `google_project_iam_member`, `google_storage_bucket_iam_member`, `google_secret_manager_secret`, `google_secret_manager_secret_iam_member`, `google_firestore_document`, `google_cloud_run_v2_job`. Never an `_iam_policy` or `_iam_binding` (authoritative) | `google_project_iam_binding` |
| G4 | create exactly one `google_service_account`, whose `account_id` is `worker_service_account_id(tenant)` from `--expect` | a second account, or one named for another tenant |
| G5 | for each IAM resource: the target is this worker account (account IAM), this project (project IAM), the artifact bucket (bucket IAM), or a secret named `swarm-tenant-<tenant>-…` (secret IAM); the member is this worker, its two Workload Identity principals, or the scheduler, reconciler or release deployer for `serviceAccountUser` on this worker; and the role is on the list for that target type: project {worker Firestore role, `logWriter`, `metricWriter`}, bucket {`objectViewer`, `objectUser`, `swarmBucketMetadataReader`} with a condition that names `tenants/<tenant>/`, account {`workloadIdentityUser`, `serviceAccountUser`, and `serviceAccountAdmin` for the release deployer in the bootstrap layer}, secret {`secretAccessor`} | `objectViewer` on the bucket for any other member, or a condition naming another prefix |
| G6 | create at most the module's known count (40), and at least one resource unless the record's verify already passes | a `for_each` that expanded into every workspace |

**A refusal is not a failure.** The run writes the saved plan to
`gs://<state bucket>/workspace-runs/w-3f9a2c/<run_id>/plan.tfplan` with its
sha256, sets `needs_owner`, writes the refused rows (address, action, rule id)
to the run's summary, and the job `owner-review` waits in the **`dev-iam`**
environment. The owner reads the rows there, and the full private plan in the
state bucket. Approving applies **that saved plan, checked by sha256**, exactly
as the release's `infrastructure-iam` job does; rejecting leaves the record
`needs_owner` until he or an admin denies or retries it.

**How `dev-iam` is bypassed, and only when.** A dev release's IAM plan waits in
`dev-iam` because the release deployer applies whatever is on `main`. A
guard-approved person's plan does not: its job names the environment
`workspace-apply`, which has **no** reviewer, and is restricted to the `main`
branch in GitHub's settings. The bypass therefore holds only for a plan that
passed G0 to G6 for one workspace. Every other plan this workflow could make
goes to `dev-iam`, and the release's own IAM rule is unchanged.

### 2.6 Privacy: how CI reads the list without printing it

The repository is public, so its Actions logs and summaries are public. The
rule is that **a person appears in public only as `w-…`**.

1. **The list is read, not committed.** `scripts/workspace-list.sh` (new) runs
   a Firestore `runQuery` on `workspaces` for every record in `approved`,
   `applying`, `needs_owner` or `ready`, and writes
   `$RUNNER_TEMP/personal.tfvars.json` with mode 0600:

   ```json
   {"personal_workspaces": {"w-3f9a2c": {"tenant_id": "u-alice", "principal": "alice@saga.xyz",
     "max_active": 8, "capacity_units": 8, "quota_pods": 16, "quota_cpu": 64}}}
   ```

   It prints one line, `personal workspaces read: N`, and refuses to write a
   file if the query fails. The file is outside the checkout, passed as
   `-var-file`, and deleted in an `always()` step.
2. **The variable is sensitive.** `terraform/infra/variables.tf` declares
   `personal_workspaces` with `sensitive = true` and a validation that every key
   matches `^w-[0-9a-f]{6}$`. The module is instantiated with
   `for_each = nonsensitive(toset(keys(var.personal_workspaces)))`: the keys are
   opaque by validation, so unwrapping them is safe, and every value inside
   stays marked. A plan then shows `account_id = (sensitive value)`, and an
   address shows only `module.personal_workspace["w-3f9a2c"]`.
3. **Inner keys carry no name.** The module calls the existing tenancy,
   secret_manager, firestore and cloud_run_jobs modules with a one-entry map
   keyed by the workspace id and a `tenant_id` attribute, so no inner address
   contains `u-alice`. Those modules read `each.value.tenant_id` where they read
   `each.key` today (defaulting to the key, so group tenants are unchanged).
4. **Command output goes to a private object.** Every `terraform`, `gcloud`
   and `kubectl` invocation in the workflow writes stdout and stderr to
   `gs://<state bucket>/workspace-runs/w-3f9a2c/<run_id>/log.txt` through
   `redact`, never to the job log. The job log shows the step lines of §2.2.
   An error prints `step A5 failed: GRANT_FAILED; see the private log`.
5. **Masks as a backstop.** A1 registers `::add-mask::` for the tenant id, the
   email, the worker account email and the namespace, so a line that escapes
   rule 4 is printed as `***`.
6. **Outputs.** `terraform/infra`'s `tenant_namespaces` output is split:
   group tenants as today, and personal namespaces in a `sensitive` output the
   release's namespace step reads into a file and never echoes.

**The release reads the list too.** Personal workspaces are in
`terraform/infra`'s state, so the release's `terraform apply` job runs A2
before it plans. If it did not, the release would plan their destruction. Two
guards make a failed read a failed plan rather than a destruction: the list
script refuses to write a file on any error, and the module sets
`lifecycle { prevent_destroy = true }` on the worker account and its forge
slot. Destroying a personal workspace is deprovisioning (§7), which is not this
workflow.

---

## 3. Terraform owns every tenant

### 3.1 Today

Every tenant Terraform knows is a key of `var.tenants`. The tenancy module
creates the account and its IAM (additive `_iam_member` resources, never an
authoritative `_iam_policy` or a project or bucket `_iam_binding`; checked
2026-10-08). The secret_manager module creates per-provider secrets with
**authoritative** per-secret accessor bindings. The firestore module writes the
tenant and pool documents once (`terraform/modules/firestore/bootstrap.tf`,
`ignore_changes = [fields]`). The cloud_run_jobs module creates per-(tenant,
profile) jobs from `local.jobs`.

### 3.2 The options, and the one chosen (WD4)

| | (T1) Split by kind: Terraform owns groups, a provisioner owns people | (T2) Each workspace is a `dev.tfvars` pull request | (T3) Terraform reads `workspaces/` | **(T4) Terraform owns both: groups from `dev.tfvars`, people from the private list** |
|---|---|---|---|---|
| new privilege | a runtime provisioner's | none | none | the dedicated CI identity's (§2.3) |
| operator-free | yes | no | no | **yes**: an admin's click |
| privacy | emails stay in Firestore | **emails published** in a public repository | as T1 | **as T1**: the list is read at plan time (§2.6) |
| one definition of a tenant | no: the script beside Terraform | yes | yes | **yes**: the same modules |
| teardown | by label and record | `terraform` | `terraform` | `terraform`, by a deprovisioning run (§7) |

**Chosen: T4** (owner decision WD4, 2026-10-08). T1 was the first version's
recommendation and is kept here for history. T3 was refused because a record
removed from Firestore would be destroyed by the next plan; T4 answers that
with `prevent_destroy` and the list script's fail-closed read (§2.6).

What each layer holds for a person:

* **`terraform/infra`**: a new module, `terraform/modules/personal_workspace/`,
  one instance per workspace id, composing the existing tenancy, secret_manager
  (the forge slot only; no provider secret, §8), firestore and cloud_run_jobs
  modules for one tenant. The shared things a person uses (the global, backend,
  runner and provider pools, the custom roles, the artifact bucket, the
  cluster) stay where they are.
* **`terraform/bootstrap`**: `deployer_service_accounts.tf` gains
  `google_service_account_iam_member.deployer_personal_sa_admin`, keyed by
  workspace id, granting the release deployer `serviceAccountAdmin` on each
  personal worker account. It reads the same private list, through the same
  script and `-var-file`, when the owner applies the bootstrap; and the
  workflow applies only its one instance (A6). The forge slot bindings for
  swarm-api in `forge_user_slots.tf` are made **once** for the
  `swarm-tenant-u-` prefix (conditioned, swarm-api only), not per person.

**Two validations keep the two sources apart:**

1. `terraform/infra/variables.tf` refuses a `var.tenants` entry of
   `kind = "user"` whose principal is a human (not `*.iam.gserviceaccount.com`):
   people live in the private list only, once `u-bogdan` has moved (§3.3).
2. It also refuses a tenant id present in both `var.tenants` and
   `var.personal_workspaces`.

**One consequence to accept:** `aged_prefixes` in
`terraform/modules/storage/main.tf` lists `var.tenants` only. Personal
tenants' task objects are not moved to Nearline. That is a storage-class cost
difference, and deletion still applies. Including them would put tenant ids
into a lifecycle rule's public plan; the rule could be keyed by workspace id
later if the cost matters.

### 3.3 Migrating what exists

| tenant | today | after |
|---|---|---|
| `u-bogdan` | in `dev.tfvars` (providers `anthropic`, 80/80). Account, secrets and jobs in Terraform state. Namespace made by hand on 2026-10-07 | **moved into the private list.** A record `workspaces/u-bogdan` is written with a workspace id, `state = ready`, `migrated = true`, its current limits and its `anthropic` provider. A pull request removes it from `dev.tfvars` and adds `moved` blocks from `module.tenancy…["u-bogdan"]` (and the other modules' instances) to `module.personal_workspace["w-…"]…`, and the same in the bootstrap layer. **The release plan must show only moves: 0 to add, 0 to change, 0 to destroy.** The owner applies the bootstrap's moves from `main` the same way |
| `u-sw-c90291` (`swarm-verify`) | in `dev.tfvars`, a service account's tenant | unchanged. Service tenants stay in `dev.tfvars` |
| `u-*` documents created by `ensure_tenant` on first sight (for example `u-admin`) | a tenant document and pool with no infrastructure behind them | no record is written. Each such person sees "Request your workspace", and the module adopts the existing documents because their principal matches |

The `moved` blocks are public and pair `u-bogdan` with its workspace id. That
reveals nothing new: `u-bogdan` is already in `dev.tfvars` and its history.
`u-bogdan` keeps its `anthropic` provider secret, so the private list's schema
carries an optional `providers` list, empty for every new person (§8).

**`ensure_tenant` stops creating personal tenants.** Once the gate is on
(WD8), `tenant_for` no longer writes `tenants/u-*` on first sight for a human
caller. It reads, and a create path then meets the gate. The module's
firestore instance becomes the only writer of a new personal tenant document.
Group tenants are unchanged.

---

## 4. From `register-tenant.sh` to the run's steps

### 4.1 One definition, not two

The first version ran `scripts/register-tenant.sh` inside a provisioner. Under
T4 a person's resources are **the same Terraform modules** that make a group
tenant, so there is still one definition of a tenant, and it is the one the
release already applies. What the script holds that Terraform does not is
carried over:

| the script's guard | where it lives now |
|---|---|
| the derived-id assertion | G4, and the module's validation that `tenant_id == tenant_id_for_user(principal)` (the private list carries both; `scripts/workspace-list.sh` recomputes and refuses a mismatch) |
| never re-point an account | G2: a person's plan can only create |
| the squat inspection of an existing account | `create_ignore_already_exists` would adopt a squatted account, so A3 first reads it: an account that already exists with any binding the module does not make, or a user-managed key, fails `IDENTITY_NOT_OURS` before the plan |
| the conditioned bucket grants and their version-3 handling | the tenancy module, as for group tenants |
| the deny-list refusals in `kubernetes/apply.sh` | unchanged, A7 runs `apply.sh` |
| `min(max_active, capacity_units)` for the pool | A8, the same arithmetic |

`register-tenant.sh` stays for operators registering a group tenant before its
release. Its `--user` path is no longer how a person is onboarded; its header
says so, and the act-as gap of §0 is filed separately.

### 4.2 The steps

The step ids are those of §2.2. "Done" means the object exists *as specified*,
not merely that a call returned 200.

| id | console label | idempotent because | failure code |
|---|---|---|---|
| A1 | Approved | the claim is one transaction; a second run finds `applying` with a live run and exits | `WORKSPACE_NOT_APPROVED` (not retryable) |
| A2 | — | read only | `PRIVATE_LIST_UNREADABLE` |
| A3 | Planning | a plan writes nothing; the squat check reads | `IDENTITY_NOT_OURS` (not retryable), `PLAN_FAILED` |
| A4 | Safety check | read only | — (a refusal is `needs_owner`, not a failure) |
| A5 | Identity and access | Terraform creates only what the state lacks | `APPLY_FAILED` |
| A6 | Release access | as A5 | `APPLY_FAILED` |
| A7 | Namespace | `kubectl apply` is declarative; a server dry-run precedes it | `NAMESPACE_APPLY_FAILED`, or `CLUSTER_UNREACHABLE` |
| A8 | Limits | a patch with the script's mask; an existing pool keeps its `active` | `CONTROL_PLANE_WRITE_FAILED` |
| A9 | Final check | read only | `VERIFY_FAILED`, naming the object type, never its name |

The order matters: the identity and its grants come before the namespace,
because `kubernetes/apply.sh` reads the account's IAM to decide whether to
render the legacy KSA; and the namespace comes before `ready`, so no
submission is admitted into a half-made workspace.

P9 of the first version made the forge slot. It is now part of A5, through the
module. Its name is deterministic (`swarm-tenant-<tenant>-git-u-<16 hex>`,
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
| `PLAN_FAILED`, `APPLY_FAILED`, `CONTROL_PLANE_WRITE_FAILED`, `PRIVATE_LIST_UNREADABLE` | yes | “Setting up your workspace stopped at {step}. Nothing half-made can run: your work stays refused until every step is done. An admin has been shown this and can retry it; the reference is {request_id}.” |
| `CLUSTER_UNREACHABLE` | yes | “Your workspace is made except its Kubernetes namespace, because the cluster did not answer. This is usually brief. An admin can retry it from People.” |
| `NAMESPACE_APPLY_FAILED` | yes | “Your workspace's Kubernetes namespace could not be applied in full, so it is not isolated yet and nothing will run in it. An admin can retry it; the reference is {request_id}.” |
| `VERIFY_FAILED` | yes | “Every step reported success, but the final check could not find one of your workspace's objects. Nothing runs until it can. An admin can retry it.” |
| `IDENTITY_NOT_OURS` | no | “An identity with your workspace's name already exists and carries access SwarmCloud never grants, so it was not adopted. Nothing was changed. The platform owner must look at request {request_id} before this can continue.” |
| `WORKSPACE_ID_TAKEN` | no | “The workspace name that belongs to your account is already registered to a different identity. Nothing was changed. Ask an admin to look at request {request_id}; this needs a person, not a retry.” |
| `WORKSPACE_NOT_APPROVED` | no | (not shown to the person: a dispatch for a record no admin approved. It is an `admin_audit` entry and an alert.) |

The copy for `needs_owner` is the state's own line in §1.2. A retry is an
admin's **Retry** in People, which re-dispatches the workflow; the person has
no retry button, because each retry is a privileged CI run.

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
ready workspace "runs nothing" until then.

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
   ✓ Planning
   ✓ Safety check
   ◐ Identity and access
   ○ Namespace
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
| `POST /v1/admin/workspaces/{workspace_id}/approve` | admin | §1.3; dispatches the workflow |
| `POST /v1/admin/workspaces/{workspace_id}/deny` | admin | §1.3; body `{reason}` |
| `POST /v1/admin/workspaces/{workspace_id}/retry` | admin | from `failed` or `needs_owner`: re-dispatches |
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
run will create its identity and namespace."), then the row shows the run's
steps live, from the record's `steps`, with a link to the run (whose public
page shows only `w-3f9a2c`). **Deny** opens a text box; the reason is
required and is shown to the person. A `needs_owner` row reads "Waiting for
the platform owner" with the run link; a `failed` row has **Retry**.

**3. Per-person ceiling and Claude accounts.**

* **Ceiling**: a number field, default 8. Saving writes the record's `limits`
  (with `quota_pods = 2 × max_active`, `quota_cpu = 8 × max_active`, the ratio
  of §8), the tenant's `max_active` and pool `hard_limit` through the existing
  tenant-limits path, and dispatches the workflow in mode `limits`, which runs
  only A1, A7 and A8 (the namespace quota and the documents; no Terraform, so
  no guard is involved). Lowering it never cancels running work; the pool
  refuses new leases above the new limit.
* **Lend or reclaim**: lists the person's loan request, if any, and the pool
  accounts an admin may lend: those owned by a group tenant, or by the admin's
  own personal tenant. **Lend** adds the person's tenant to the account's
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
3. Remove the record from the private list, and run a deprovisioning plan
   whose only changes are deletions under `module.personal_workspace["w-…"]`.
4. Delete the namespace.
5. Disable the slot versions; delete the slot secrets after the retention the
   owner sets.
6. Archive the artifacts under `tenants/u-<id>/`, by the bucket lifecycle.
7. Mark the record `deprovisioned`.

Step 3 is exactly what this design's guard refuses (G2: create only), and what
`prevent_destroy` blocks. So deprovisioning is **a separate, owner-approved
run** with its own guard mode (deletions under one workspace id only), decided
then. This design reserves the state name `deprovisioned`.

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
namespace. Each approval costs one CI run of 5 to 15 minutes on a GitHub-hosted
runner, which the public repository does not pay for.

**Shared-pool interaction:** `var.pool_limits.providers.anthropic` must be at
least *(tenants holding the provider) × `provider_tenant`*
(`terraform/infra/variables.tf`). New people hold no provider, so they hold no
`provider:anthropic:tenant:*` pool. Their claude-code work is bounded by the
account pool and by the shared `provider:anthropic` pool. A new person cannot
push the platform past its shared ceilings, because every reservation is
all-or-nothing across every pool (invariant 2).

---

## 9. Decisions

Each decision keeps its options, for history. Those the owner made on
2026-10-08 are marked; the rest are open and carry a recommendation.

### WD1. Where do personal worker identities live?

* (a) A dedicated identity project, administered by a runtime provisioner.
* (b) The shared project, with project-level `serviceAccountAdmin` for the
  release deployer. Breaks rule 2 and #334.
* **(c) The shared project, with each account's IAM done by a Terraform apply**
  (§2.4).

**Decided 2026-10-08 (owner): (c).** Identities stay in `saga-agents-staging`,
and there is **no** privileged runtime provisioner with account-admin power.
Any admin approves a person with one click in Admin → People; that is the only
human step. SwarmCloud then triggers CI, which plans the bootstrap and infra
layers for that person and applies them with a dedicated identity behind a
plan guard that allows only creations of that person's own resources. Anything
else stops and asks the owner (§2).

### WD2. What triggers the guarded apply? (revised for (c); open)

The first version asked what runs a provisioner: (a) a Cloud Run Job, (b) a
GitHub Actions workflow, (c) Cloud Workflows. Under (c) the apply is CI by the
owner's decision, so the question is what starts it:

* **(a) swarm-api dispatches `workspace-apply.yml`** with a new single-purpose
  GitHub App holding `actions: write` on this repository (§2.1 (A)).
* (b) Pub/Sub → a Cloud Build trigger running the same steps, with private
  logs but a second CI and a second place to review.
* (c) A scheduled workflow polling Firestore: no credential in swarm-api, but
  minutes of delay and an empty public run every 5 minutes.
* Reusing `swarmcloud-merge` was considered and refused: merge-step.md §5
  retires it.

**Recommendation:** (a). The owner's one-time step is creating and installing
the App and storing its key.

### WD3. What makes a person's resources? (revised for (c); open)

The first version asked whether a provisioner runs `register-tenant.sh` or a
port of it. Under (c) and WD4:

* **(a) The existing Terraform modules**, instantiated once per workspace id
  through `module.personal_workspace` (§3.2), with the script's guards carried
  over (§4.1).
* (b) `register-tenant.sh`, run by the workflow. It is a second definition
  beside Terraform, and WD4 makes Terraform the owner.
* (c) A new standalone module, written for people. A third definition.

**Recommendation:** (a). It is the definition the release already applies, and
a restated rule drifts.

### WD4. Who owns which tenants?

* (a) T1: Terraform owns groups and service tenants, a provisioner owns people,
  and `u-bogdan` is grandfathered.
* (b) T1, and migrate `u-bogdan` to the provisioner.
* (c) T2: every workspace becomes a `dev.tfvars` pull request.
* **(d) T4: Terraform owns both**, groups and service tenants through
  `dev.tfvars` and personal workspaces through the private list.

**Decided 2026-10-08 (owner): (d).** `u-bogdan` migrates into the private list
with a plan that changes nothing (§3.3).

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

### WD9. Bucket and project grants: role-bounded, or member-bounded? (revised for (c); open)

The guard now checks every member (G5), so this asks what the dedicated
identity *holds*:

* **(a) `hasOnly` role conditions** on its project and bucket grants, plus the
  alert (§2.4 R2, R4).
* **(b) A principal-set grant**, made once by Terraform, of the worker's three
  project roles to every `swarm-agent-worker-u-*` account, so the identity
  needs no project IAM at all. This holds only if W0 confirms IAM supports such
  a set in an allow policy.
* (c) A Google group of personal workers. This needs Cloud Identity API access,
  which this project has found unreliable (`searchTransitiveGroups` 403s).

**Recommendation:** (b) if W0 confirms it, else (a).

### PEOPLE. The user-management screen

**Decided 2026-10-08 (owner): all four parts in v1** (§6.4): everyone who has
signed in; pending requests with Approve / Deny and live progress; per-person
ceiling and lending or reclaiming a Claude account; granting or removing
admin, every change recorded with who and when.

### PRIVACY. Where the list of approved people lives

**Decided 2026-10-08 (owner):** out of the repository, in SwarmCloud's private
store, read by CI at run time; public artefacts name a person only by an
opaque workspace id (§2.6). This **supersedes** the approval comment's
"SwarmCloud opens and merges the config change for the person": there is no
per-person pull request, because the config change is the private record.

---

## 10. Build plan

Within a phase no file is in two lanes, and a lane depends only on earlier
phases. New files are named without their root.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| W0 | 0 | **verification, no code**: principal-set allow grants (WD9 (b)); that a GitHub-hosted runner reaches the GKE control plane under `master_authorized_cidrs`; that `nonsensitive(keys(...))` of a sensitive map keeps the values marked in plan and apply output; that apply's `[id=…]` lines print nothing for a sensitive id; `ALREADY_EXISTS` handling of a pre-made slot. Each result is dated in this document | `docs/workspaces.md` | the open decisions WD2, WD3, WD9 |
| W1 | 1 | the record and `workspace_ids/`, `GET`/`POST /v1/workspace`, the loan request, the `workspace` and `claude_account` onboarding steps, `WorkspaceNotReady` and `NoClaudeAccount`, the gate behind `WORKSPACE_GATE=off`; `people/` writes; `tenant_for` stops creating `u-*` once the gate is on | new `swarm_api/workspaces.py`, new `swarm_api/routes/workspaces.py`, `apps/swarm-api/swarm_api/service.py`, `apps/swarm-api/swarm_api/errors.py`, `apps/swarm-api/swarm_api/onboarding.py`, `apps/swarm-api/swarm_api/main.py` | W0 |
| W2 | 1 | admin roles in Firestore (§6.5): `admin_roles/`, `admin_audit/`, `PLATFORM_OWNER`, the one-time migration of `admin_users`, the grant and remove routes with the owner and last-admin safeguards | new `swarm_api/admins.py`, new `swarm_api/routes/people.py`, `apps/swarm-api/swarm_api/auth.py`, `apps/swarm-api/swarm_api/settings.py` | W0 |
| W3 | 1 | **the plan guard**: `--mode workspace`, `scripts/lib/workspace-plan.jq` with G0–G6, its self-test cases (every rule refusing a fixture built to trip it), and `scripts/workspace-list.sh`; `.github/CODEOWNERS` on the guard and the workflow | `scripts/lib/plan-guard.sh`, new `lib/workspace-plan.jq` and `workspace-list.sh` in scripts/, new `workspace-plan-cases.json` in scripts/lib/, `.github/CODEOWNERS` | W0 |
| W4 | 2 | **Terraform, owner-approved once**: `module.personal_workspace`; the modules reading `each.value.tenant_id`; `var.personal_workspaces` (sensitive, validated); `prevent_destroy`; the sensitive namespace output; the two validations of §3.2; in bootstrap, `swarm-workspace-deployer`, its custom roles and conditioned grants, its WIF binding, the `workspace-apply` state prefixes, `deployer_personal_sa_admin`, the `swarm-tenant-u-` slot bindings and the validation of its role list; the alert of §2.4; `terraform test` assertions | new `terraform/modules/personal_workspace/`, `terraform/modules/tenancy/`, `terraform/modules/secret_manager/`, `terraform/modules/firestore/`, `terraform/modules/cloud_run_jobs/`, `terraform/infra/main.tf`, `terraform/infra/variables.tf`, `terraform/infra/outputs.tf`, new `workspace_deployer.tf` in terraform/bootstrap, `terraform/bootstrap/deployer_service_accounts.tf`, `terraform/bootstrap/forge_user_slots.tf`, `terraform/bootstrap/wif.tf`, `terraform/bootstrap/variables.tf`, new `personal_workspace.tftest.hcl` in tests/terraform | W3 |
| W5 | 2 | **cluster, owner-applied once**: the deployer's ClusterRole, binding and kube-system Role, and the scope ValidatingAdmissionPolicy, plus the parity test that the policy's Role literals equal the render | new `rbac/workspace-deployer-rbac.yaml` and `policies/workspace-deployer-scope.yaml` in kubernetes/, `kubernetes/render.py` (`POLICY_FILES`) | W0 |
| W6 | 2 | People and the approval flow in swarm-api: the admin list, approve, deny, retry, limits, loan, the dispatch through the App and the sweep route | `swarm_api/routes/people.py` (W2's new file, extended), new `swarm_api/dispatch_workspace.py`, `apps/swarm-api/swarm_api/routes/accounts.py` | W1, W2 |
| W7 | 3 | **the workflow**: `workspace-apply.yml` (A1–A9, the `limits` mode, `owner-review` in `dev-iam`), and the release's `terraform apply` job reading the private list (A2) and the namespace step reading the sensitive output | new `.github/workflows/workspace-apply.yml`, `.github/workflows/release.yml`, `.github/workflows/application.yml` (actionlint list) | W3, W4, W5, W6 |
| W8 | 3 | the console: the checklist steps, the progress view, the Submit banners, Admin → People; the plugin: `sc setup`, `swarm_setup_workspace`, `/sc:setup`; the issue forms' "Where" list | `apps/swarm-ui/src/GitHubConnect.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/SubmitWorkflow.tsx`, `apps/swarm-ui/src/App.tsx`, new `People.tsx` in apps/swarm-ui/src, `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/server.py`, `plugin/commands/setup.md`, `.github/ISSUE_TEMPLATE/` | W1, W6 |
| W9 | 4 | **the `u-bogdan` migration**: the record, the `moved` blocks in both layers, its removal from `dev.tfvars`; the release's plan must read 0/0/0 | `terraform/infra/moved.tf`, `terraform/bootstrap/moved.tf`, `terraform/environments/dev/dev.tfvars` | W7 |
| W10 | 5 | the first real approval end to end, then `WORKSPACE_GATE=on` (WD8); docs: multi-tenancy, offboarding runbook, onboarding, ci.md (the `workspace-apply` environment and the App) | `docs/multi-tenancy.md`, `docs/runbooks/tenant-offboarding.md`, `docs/onboarding.md`, `docs/ci.md` | W8, W9 |
| — | later | deprovisioning (§7), with its own guard mode | — | the owner's decision then |

**Which phases need the owner, once each:**

* **W4** is an IAM plan, so its release waits in `dev-iam`, and its bootstrap
  half (the new identity, its roles, its WIF binding) is an owner-run bootstrap
  apply from `main`, as every bootstrap change is. Custom roles follow
  `docs/runbooks/custom-roles-to-bootstrap.md`.
* **W5** is an owner-run `kubernetes/apply.sh --policies --confirm`.
* **W6/W7** need the owner to create the `swarmcloud-workspaces` App (if WD2
  (a)), install it on this repository and store its key with
  `scripts/create-secrets.sh --stdin`, and to create the `workspace-apply`
  environment restricted to `main` with no reviewer.
* **W9** is an owner-run bootstrap apply of the `moved` blocks; the infra half
  is a release whose plan he checks reads 0 to add, 0 to change, 0 to destroy.

After those, **no person's onboarding needs the owner**, unless the guard
refuses a plan. W3, W4, W6 and W7 get the one review (credentials, tenant
isolation, IAM). W1 gets it too, for the gate.

---

## 11. Invariants, each with how it holds

1. **Demand only from `LEASED`…`RUNNING`.** Requesting, approving and applying
   a workspace create no task and no lease. A ready, idle workspace holds
   nothing, and the CI run is not a worker.
2. **All-or-nothing reservation.** Unchanged. A8 writes a pool document's
   limit, never its `active`; a pool that exists keeps its `active`.
3. **Concurrency from `LEASED`.** Unchanged. Lowering a ceiling refuses new
   leases above it and touches nothing running.
4. **Workers never sleep through a wait.** The run is not a worker. swarm-api
   and the plugin never wait inside a request or a tool call for a run; the
   console polls.
5. **Fencing.** Unchanged. The claim in A1 is the run's own fence: a second
   run finds `applying` with a live run and exits, and the saved plan an owner
   approves is checked by sha256.
6. **No Spot.** Nothing here declares any.
7. **`requests == limits`.** The personal ResourceQuota follows it (§8).
8. **Checkpointing.** Unchanged for workers.
9. **Isolation.** This is the point of the design. Every workspace gets its own
   account, its own prefix-conditioned grants, its own namespace with
   default-deny networking, and its own slot, made by the same modules that
   make a group tenant's, and the guard refuses a plan that would give any of
   them to another member (G5).
10. **Profiles by name.** No new route accepts an image, a command, a resource
    spec or a backend parameter. The workspace routes take no body fields that
    shape infrastructure; the one admin input, a ceiling, is a number the
    server turns into a quota by a fixed ratio.
