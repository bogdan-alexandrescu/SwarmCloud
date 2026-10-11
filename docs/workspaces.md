# Personal workspaces: approved by an admin, applied by a guarded cloud job

**Status: DESIGN, final pass 2026-10-08 with every owner decision (part of
#847), amended 2026-10-10: WD2 is re-decided, and the apply runs as a Cloud
Run job, `swarm-workspace-apply`, not a Cloud Build trigger (§9 WD2).** Lanes
W1 to W9 have since been built against the Cloud Build shape; §10 says which
of their pieces the re-decision rewrites, and what is removed once the job
ships. One choice inside the re-decision, how an approval starts the job, was
**decided by the owner on 2026-10-10: (ii), Pub/Sub through Eventarc and Workflows** (§2.1).

The owner asked on 2026-10-08 for each person's personal space to be
"onboarded at the time of onboarding a new user in swarmcloud", with "a way to create this via the UI and potentially via the
plugin". They also asked that it "should block starting any task or workflow
before this exists".

The first version of this document proposed a locked-down runtime
provisioner. The owner's decisions of 2026-10-08, posted on #847 and on this
design's pull request, replaced it, and his later decisions that day on WD2,
WD3 and WD9 replaced the GitHub Actions and Terraform-module version of the
apply. His re-decision of WD2 on 2026-10-10 (recorded on #847) replaced the
Cloud Build trigger with a Cloud Run job. **This document describes the
chosen design throughout**; §9 records every decision with its options, for
history.

* **Identities stay in the shared project (WD1 (c)).** There is **no**
  privileged runtime provisioner holding account-admin power.
* **Approval is one click by any admin**, in a new console screen,
  **Admin → People**. That click is the only human step. swarm-api then
  starts a **Cloud Run job in the project**, `swarm-workspace-apply` (WD2,
  re-decided 2026-10-10), which runs `scripts/register-tenant.sh --workspace <w-id>` (WD3) as
  a **dedicated identity**, `swarm-workspace-deployer`, behind a **call guard**
  that wraps every cloud and cluster call the script makes and allows only
  creations of *that workspace's own* resources. Anything else stops the run
  and asks the owner.
* **The list of approved people is private.** The repository is public, so
  the list lives in Firestore, and Terraform never reads it. The job's logs are
  in the project's Cloud Logging, routed to a restricted bucket, not a public
  CI log. Anything public names a
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
* **The apply is a Cloud Run job**, `swarm-workspace-apply`, running the
  `images/workspace-apply` image the release builds from `main`, pinned by
  digest by the owner's bootstrap apply. An execution is started with the
  workspace id and the mode and nothing else that the job will use, and the
  job is the only thing that runs as `swarm-workspace-deployer` (§2.1, §2.4).
  An approval starts it through Pub/Sub, Eventarc and Workflows: swarm-api only
  publishes, as built (**decided 2026-10-10, owner: option (ii)**, §2.1).
  Installing it is Terraform alone: no forge connection, no grant to Google's
  Cloud Build agent, no private pool (§10).
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

### W0b, read 2026-10-10: the documentation half

W0b (§10) checks the 2026-10-10 re-decision. This lane could not run `gcloud`
or read the live project, so it answered what Google's public documentation
and this repository can answer. Each item below names the page it read and
that page's "Last updated" date. Every live proof is the operator's and is
marked **pending**. Items (4) to (6) need the job's first execution after W4b,
so they are not started.

**(1) Does Cloud Run check `actAs` on every `jobs.update` and `jobs.create`?
Documentation says yes for create and update, with no exception for an update
that leaves the account unchanged. Live proof pending, operator.**

* [Configure service identity for jobs](https://docs.cloud.google.com/run/docs/configuring/jobs/service-accounts)
  (last updated 2026-10-07, read 2026-10-10): "Certain operations, like
  creating or updating a job, require the deployer account to have
  permissions on the service identity resource". It also says
  `roles/iam.serviceAccountUser` "contains the `iam.serviceAccounts.actAs`
  permission, which is required to attach a service account on the job".
* The page names *updating* without a qualifier. It does **not** say in so
  many words that an update leaving the account unchanged is checked. No
  Google page read on 2026-10-10 says so either way, and so does not
  contradict it. That gap is why the live proof is still required before W4b
  is applied: an update from an account holding `run.jobs.update` but no
  `actAs` on the job's account must be refused with
  `iam.serviceAccounts.actAs` named. For example, a label change on a
  throwaway job whose account the probe identity cannot act as.
* Cloud Run is not among the services with IAM's legacy "attach without
  `actAs`" behaviour. [Requiring permission to attach service accounts to resources](https://docs.cloud.google.com/iam/docs/service-accounts-actas)
  (last updated 2026-10-07) lists App Engine, Managed Service for Apache
  Airflow, Cloud Data Fusion, Dataflow, Managed Service for Apache Spark and
  Dataform, and does not mention Cloud Run. [Roles for service account authentication](https://docs.cloud.google.com/iam/docs/service-account-permissions)
  (last updated 2026-10-07) is the page whose note "some Google Cloud services
  did not always require" `actAs` points at that list.
* So the documentation does not return the design to the owner. §2.3's
  "change what it runs" row stands, pending the operator's refusal.

**(2) What `runWithOverrides` can override, and whether `CLOUD_RUN_*` or `LD_*`
names are accepted.**

* [Method: projects.locations.jobs.run](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run)
  (last updated 2025-07-09) allows exactly these fields:
  * `overrides.containerOverrides[]`, each carrying:
    * `name`, which picks the container;
    * `args`, which "Will replace existing args";
    * `clearArgs`;
    * `env`, which "Will be merged with existing env".
  * `overrides.taskCount`.
  * `overrides.timeout`.

  The authorization line names `run.jobs.runWithOverrides` "on the specified
  resource overrides". There is **no** field for the image, the command, the
  service account, the network or the volumes.
* [Execute jobs](https://docs.cloud.google.com/run/docs/execute/jobs#override-job-configuration)
  (last updated 2026-10-07) agrees: "You can override the arguments,
  environment variables, number of tasks, and task timeout".
* The same page adds a fifth override: running a regular job as a **delayed**
  one, deferred by up to 12 hours. That is
  [Delay execution of a job](https://docs.cloud.google.com/run/docs/delayed-jobs),
  **Preview**, a `gcloud beta` flag, and absent from the 2025-07-09 REST
  reference. It affects availability only: a deferred start is what the sweep
  already sees as `dispatched_unclaimed` after 10 minutes and dispatches
  again. If the deferred execution starts later, A1 refuses it, because the
  record is no longer `approved` (§2.2).
* **An `env` override may carry a secret reference, not just a value.**
  [`EnvVar`](https://docs.cloud.google.com/run/docs/reference/rest/v2/Container#EnvVar)
  (last updated 2026-08-25) has `value` or `valueSource.secretKeyRef`, which
  Cloud Run resolves as the job's identity. The deployer holds no
  `secretmanager.versions.access` (§2.3), so such an override fails at the
  start, and step 0 discards the variable either way (§2.2).
* **`CLOUD_RUN_*`: documented as reserved in a job's configuration; not
  documented for overrides.**
  [Configure environment variables for jobs](https://docs.cloud.google.com/run/docs/configuring/jobs/environment-variables)
  (last updated 2026-10-07): "The environment variables defined in the
  container runtime contract are reserved and cannot be set".
  [The runtime contract](https://docs.cloud.google.com/run/docs/container-contract)
  (last updated 2026-10-07) lists `CLOUD_RUN_JOB`, `CLOUD_RUN_EXECUTION`,
  `CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_TASK_ATTEMPT` and `CLOUD_RUN_TASK_COUNT`
  for jobs.
  * Neither page says whether a `jobs.run` override is refused the same way.
    Live proof pending, operator: an override setting `CLOUD_RUN_TASK_COUNT`
    on a throwaway job.
  * Step 0 does not depend on the answer. It takes `CLOUD_RUN_EXECUTION` only
    through `WS_BUILD_RE`, and a forged `CLOUD_RUN_TASK_COUNT=1` beside a real
    `taskCount` above one still meets A1's one-transaction claim.
* **`LD_*`: no documented restriction.** None of the pages above restricts an
  override's name beyond `EnvVar`'s "Must not exceed 32768 characters", and
  none names `LD_*` at all. Read from the documentation, an override may set
  `LD_PRELOAD` and `LD_LIBRARY_PATH`. §2.4 R6 stands as written. Live proof
  pending, operator.
* **A `jobs.run` is a Data Access entry, not Admin Activity, and is not
  logged by default.**
  * [Cloud Run audit logging](https://docs.cloud.google.com/run/docs/audit-logging)
    (last updated 2026-10-07) lists `google.cloud.run.v2.Jobs.RunJob` (and
    v1's) as "Audit log type: Data access", with permissions
    "`run.jobs.run` - DATA_WRITE" and "`run.jobs.runWithOverrides` -
    DATA_WRITE". `UpdateJob`, `ReplaceJob` and `SetIamPolicy` are Admin
    Activity.
  * [Enable Data Access audit logs](https://docs.cloud.google.com/logging/docs/audit/configure-data-access)
    (last updated 2026-10-08): "Data Access audit logs are disabled by default
    for all services but some BigQuery services". They "are stored in the
    `_Default` bucket unless you've routed them elsewhere". Only Admin
    Activity, System Event and Access Transparency go to `_Required`
    ([Routing overview](https://docs.cloud.google.com/logging/docs/routing/overview),
    last updated 2026-10-08).
  * **This corrects two places that said Admin Activity:** §2.4's alert on a
    `jobs.run` from an unexpected caller, and §2.6 item 4. Until Cloud Run's
    `DATA_WRITE` audit logs are enabled in the project, no entry records who
    started an execution, so the alert would never fire.
  * Enabling them is a project-wide audit config per service, so it covers
    the other team's Cloud Run too. That is W4b's to build and the owner's to
    accept; this lane's `questions.json` asks.
  * Whether the entry carries the request's overrides is not stated: pending,
    the first execution after the config exists.

**(3) Who the repository's Terraform grants, at project level in the shared
project, `actAs`, token-creator, `run.jobs.run`, `runWithOverrides`,
`run.jobs.update` or `setIamPolicy`.** Read from `main` at `b6ede3a`. The live
list is the operator's (pending). A live listing also includes what Terraform
does not grant: the project's owners and editors, the default compute
account's Editor if it still has it, and anything granted by hand.

| principal | role | carries | file and resource | condition |
|---|---|---|---|---|
| release deployer `swarm-tf-deployer` | `roles/run.admin` | `run.jobs.run`, `runWithOverrides`, `run.jobs.update`, `run.jobs.setIamPolicy`; no `actAs` | `terraform/bootstrap/wif.tf` `google_project_iam_member.deployer_roles["roles/run.admin"]`, from the `deployer_roles` default in `terraform/bootstrap/variables.tf` (line 293) | none: "UNSCOPABLE: Cloud Run is not in IAM's resource-attribute list" (same file) |
| `swarm-scheduler` | custom `swarmJobDispatcher` | `run.jobs.run`, `runWithOverrides`, `run.jobs.update` (and `create`); no `setIamPolicy`, no `actAs` | role in `terraform/bootstrap/platform_roles.tf` (`job_dispatcher`); grant `module.iam.google_project_iam_member.plain["swarm-scheduler:…/swarmJobDispatcher"]`, `terraform/modules/iam/bindings.tf`, called from `terraform/infra/main.tf` `module "iam"` | none |
| acceptance `swarm-accept` | custom `swarmAcceptanceRunner` | `run.jobs.run`, `runWithOverrides`; no update, no `setIamPolicy` | `terraform/bootstrap/acceptance.tf` `google_project_iam_custom_role.acceptance_runner`, granted by `google_project_iam_member.acceptance_runs_jobs` | none, removed on purpose (#965) |
| release deployer | `roles/resourcemanager.projectIamAdmin` | `resourcemanager.projects.setIamPolicy`, so it can grant `swarmJobDispatcher` (run, overrides, update) to any member, itself included; it holds none of the six directly | `terraform/bootstrap/deployer_conditions.tf` `google_project_iam_member.deployer_project_iam_admin` (two chunks), list `deployer_grantable_project_roles` | `hasOnly` over the roles terraform/infra grants. That list carries no `actAs`, token-creator or `run.admin` |
| `swarm-workspace-deployer` (**not created today**: `enable_workspace_deployer` defaults false) | custom `swarmWorkspaceAccountAdmin` | `iam.serviceAccounts.setIamPolicy` project-wide, so it could grant `actAs` or token-creator on any account, its own included. The guard's rule C3 and the alert of §2.4 bound this, not IAM | `terraform/bootstrap/workspace_deployer.tf` `google_project_iam_member.workspace_deployer["swarmWorkspaceAccountAdmin"]` | none (§2.4, accepted 2026-10-08) |

* **Nothing in this Terraform grants these roles at project level:**
  `roles/owner`, `roles/editor`, `roles/iam.serviceAccountUser`,
  `roles/iam.serviceAccountTokenCreator`, `roles/run.developer`,
  `roles/run.invoker` or the `run.jobsExecutor*` roles. The repository has no
  `google_project_iam_binding` or `google_project_iam_policy` at all. None of
  its custom roles lists `iam.serviceAccounts.actAs`, `getAccessToken`,
  `signBlob`, `signJwt`, `implicitDelegation` or `run.jobs.setIamPolicy`.
* **Every `actAs` and token-creator grant in the repository is per account,
  not project-level:**
  * the release deployer on the default compute account (`wif.tf`
    `deployer_acts_as_cloudbuild`) and on each platform account
    (`terraform/infra/deployer.tf` `deployer_acts_as`);
  * the scheduler, the reconciler and the release deployer on each tenant
    worker (`terraform/modules/tenancy/main.tf` `act_as`);
  * swarm-api on itself (`modules/iam/bindings.tf` `api_signs_as_itself`);
  * the release deployer's per-account `serviceAccountAdmin`
    (`terraform/bootstrap/deployer_service_accounts.tf` `deployer_admin`),
    which lets it grant itself `actAs` on those accounts. They do not include
    `swarm-workspace-deployer`, whose policy the bootstrap writes
    authoritatively empty.
* How the role contents were established: the predefined roles' permissions
  (`run.admin`'s, and the absence of `actAs` from it) come from Google's role
  reference, not this repository. The custom roles were read from their
  definitions.

**(4) to (6): pending, they need the job's first execution after W4b.**

**(7) Option (ii): `roles/workflows.invoker`, and the Pub/Sub agent's
token-creator.**

* **`roles/workflows.invoker` cannot be granted narrower than the project.**
  * [Workflows roles and permissions](https://docs.cloud.google.com/workflows/docs/access-control)
    (last updated 2026-10-07) gives "Lowest-level resources where you can
    grant this role: Project" for the invoker and for every other Workflows
    role.
  * No condition can narrow it either: `workflows.googleapis.com`, like
    `run.googleapis.com`, is absent from the services in
    [Resource attributes for IAM Conditions](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes)
    (last updated 2026-10-07).
  * The Terraform provider has no per-workflow IAM resource; it is the open
    feature request
    [hashicorp/terraform-provider-google#13125](https://github.com/hashicorp/terraform-provider-google/issues/13125).
  * [Roles and permissions for Workflows targets](https://docs.cloud.google.com/eventarc/standard/docs/workflows/roles-permissions)
    (last updated 2026-10-07) grants `roles/workflows.invoker` "on the
    project". For a direct Pub/Sub event it does not require
    `roles/eventarc.eventReceiver`.
  * **What this means:** `swarm-workspace-dispatch` can start *any* workflow
    in the shared project, the other team's included, though each workflow
    then runs as its own account. §2.1 already says "at the project". It has
    no key and no WIF binding, and only Eventarc uses it. W4b's alert on a
    change to the dispatch account's IAM is what watches it.
* **The Pub/Sub token-creator grant is needed only if the Pub/Sub service
  agent was enabled on or before 8 April 2021.** The same Eventarc page says:
  "If you enabled the Cloud Pub/Sub service agent on or before April 8, 2021,
  to support authenticated Pub/Sub push requests, grant the Service Account
  Token Creator role … Otherwise, this role is granted by default."
  * The cut-off is the agent's enabling, not the project's creation. A
    project created after 8 April 2021 cannot have enabled it before, so for
    such a project the answer is no.
  * This project's creation date is not in the repository. Pending, operator:
    `gcloud projects describe <project> --format='value(createTime)'`.
  * One indirect signal in the repository: three Pub/Sub push subscriptions
    already present OIDC tokens (the wake push and `task_finished` in
    `terraform/modules/scheduler/main.tf`, and the execution-cancel push in
    `execution_cancel.tf` beside it). No Terraform grant gives the agent
    token-creator. If those pushes deliver, the agent can already mint them.

---

### W0b, read 2026-10-10: the operator's live half

Read by the operator with `gcloud` against `saga-agents-staging`.

* **(3) Who holds the powers that reach the job (read ~18:45Z,
  `gcloud projects get-iam-policy`).** At the project level:
  `swarmJobDispatcher` (and the conditioned `swarmGkeDispatcher`) is held by
  `swarm-scheduler`; `swarmJobReaper` (and the conditioned `swarmGkeReaper`)
  by `swarm-reconciler`; `swarmAcceptanceRunner` by `swarm-accept`;
  `roles/run.admin` by `swarm-tf-deployer` and one owner; `roles/owner` by the
  two human owners, and `roles/iam.serviceAccountUser` by one of them. **One
  finding:** the default compute account, `<number>-compute@developer`, holds
  `roles/editor` on the whole project. Editor carries
  `iam.serviceAccounts.actAs` and Cloud Run job update and run, so a workload
  running as the default compute account bypasses the job-level policy of
  §2.3. It is Google's legacy default grant, and in this shared project the
  other team's workloads may use it, so it is recorded as a residual risk and
  as #1020, to be removed together with that team, never unilaterally.
* **(1) The refused-update proof: shown, 2026-10-10 ~19:05Z.** With a
  temporary `roles/iam.serviceAccountTokenCreator` for the operator on
  `swarm-scheduler` only (the owner's approval; removed right after and read
  back empty), a no-op label update of `swarm-verify` run as `swarm-scheduler`
  (which holds `run.jobs.update` through `swarmJobDispatcher` but no `actAs`
  on `swarm-verify`) was refused: `Permission 'iam.serviceaccounts.actAs'
  denied on service account swarm-verify@...`. The update did not change the
  account, so Cloud Run checks `actAs` on every update, not only on one that
  changes the account. It was not the operator's own identity that was
  refused: the operator holds `actAs` project-wide. Nothing changed (no
  probe label). The first attempt, without the grant, had been refused at
  impersonation and proved nothing. **W4b's gate (1) is met.**

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
Run job reads one record from it by workspace id.

| field | type | written by | meaning |
|---|---|---|---|
| `tenant_id` | string | swarm-api | `u-alice`; equals the document id. **Private**: it is derived from the email, so it is a name |
| `workspace_id` | string | swarm-api | `w-3f9a2c`: `w-` and 6 hex digits from `secrets.token_hex(3)`, **random, not derived from the email**, so nobody can recover it by hashing a guessed address. It is unique (§1.3). This is the only id that appears in anything public |
| `principal` | string | swarm-api | `alice@saga.xyz`, lower-cased, from the verified token, never from the body |
| `state` | string | swarm-api, the job | §1.2 |
| `request_id` | string | swarm-api | a fresh UUID for each accepted request or retry; the job logs it, privately |
| `requested_at`, `requested_via` | timestamp, string | swarm-api | `console`, `plugin` or `api` |
| `decision` | map or null | swarm-api | `{by, at, verdict: approved/denied, reason, auto}`. `reason` is required for a denial and is shown to the person. `auto` is true when an admin's own request was approved automatically (§1.3), with reason `requester is an admin`. `by` is an admin's email; it never leaves Firestore |
| `held` | string or absent | swarm-api | `migrating` on a `requested` record an admin asked for whose tenant predates the workspace job (§1.3, §3.3): not approved automatically, never published |
| `limits` | map | swarm-api | `{max_active, capacity_units, quota_pods, quota_cpu}`. Defaults from §8; an admin raises them in People (§6.4) |
| `run` | map or null | swarm-api, the job | `{build_id, attempt, published_at, mode}` of the current run. Since the 2026-10-10 re-decision `build_id` holds the Cloud Run **execution** name (`swarm-workspace-apply-x7k2p`), which the job reads from `CLOUD_RUN_EXECUTION`; the field keeps its name because W6 and W7 already write it (`register-tenant.sh`, `swarm_api/people.py`), and `register-tenant.sh`'s `WS_BUILD_RE` already admits an execution name. The execution's page is in the project's console, and its log is readable only by the restricted bucket's readers (§2.6) |
| `steps` | map | the job | `step id → {state: todo/running/done/failed/held, at, code}`, using the ids in §4.2 |
| `failure` | map or null | the job | `{step, code, retryable, at}`. The copy is served from the code (§4.3), never stored free-form, so no command output reaches Firestore |
| `ready_at` | timestamp | the job | written by the verify step only |
| `migrated` | bool | the migration (§3.3) | `true` for `u-bogdan`, whose resources predate the record |

### 1.2 States

| state | entered when | the person sees | submissions to their own tenant |
|---|---|---|---|
| (no record) | never requested | checklist step "Request your workspace", button enabled | refused, `WORKSPACE_NOT_READY` |
| `requested` | swarm-api accepted a non-admin's request (an admin's goes straight to `approved`, §1.3) | "Waiting for an admin to approve", or, when `held`, that it is being migrated | refused |
| `denied` | an admin denied it | "Not approved: {reason}", and "Request again" | refused |
| `approved` | an admin approved it, or it was an admin's own request; the dispatch is being made | "Approved. Setting up…" | refused |
| `applying` | the job claimed the record | each step of §4.2 with a tick or spinner | refused |
| `needs_owner` | the call guard stopped a call (§2.5) | "Approved. A change needs the platform owner's review before it can finish." | refused |
| `failed` | a step failed past its retries | the step's code and copy (§4.3) | refused |
| `ready` | the verify step read back every object | the step is ticked; the Claude account step comes next | admitted once a Claude account exists (§5) |

`ready` is **evidence-derived on the job's side**: it is written only
after the final verification step (A9) has read back every object. swarm-api
never writes `ready`, and nothing a client sends can set it. The one exception
is §3.3's migration of a tenant whose resources Terraform already made:
`Workspaces.migrate`, which no route calls, run by an operator through
`scripts/workspace-migrate-record.sh`, and followed by a `--mode verify` run
that reads every object back. The same rule
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
and `/deny` (§6.3). Both need `is_admin` (§6.5). Approve is accepted from
`requested`, or from `denied` at any time; deny from `requested` or `failed`,
and it needs a non-empty reason of at most 500 characters. Each writes
`decision` and an `admin_audit` entry in the same transaction.

**An admin's own request is approved automatically** (owner decision,
2026-10-09, replacing 2026-10-08's "an admin may approve their own request",
which left an admin to click Approve on themselves). When the caller of
`POST /v1/workspace` is an admin (§6.5), the request's own transaction also
performs the approval: the same code the approve route runs
(`People._approve_in`), not a copy of it. The record goes straight to
`approved` with `decision = {by: <the admin>, auto: true, reason: "requester is
an admin"}`, one `admin_audit` entry `approve_own_workspace_auto` is written,
and the same publish follows (§2.1). Nothing else is relaxed: checks 1-5 above
run first, the 24-hour wait after a denial still applies, a `failed` record is
still a 409, and the limits are §8's defaults, as for anyone. This holds
because the call guard (§2.5) bounds what any approval can create, so an
automatic approval creates nothing an admin's click would not. An admin's
`requested` record that predates the decision is approved the next time they
ask. A non-admin's request is unchanged: it waits in `requested` for an admin.
An admin whose role lookup fails is not an admin for this purpose (`is_admin`
is false), so their request waits too.

**The one exception is a tenant that predates the workspace job** (§3.3):
`u-bogdan`, whose identity Terraform made. A request for it is never approved
automatically and never published, because the apply's squat check (A2) would
fail `IDENTITY_NOT_OURS` on that identity. It is recognised by what the code
can already read: the tenant document carries Terraform's
`managed_by = "swarm-terraform"` (`ensure_tenant` and A8 never write it), or
the record carries `migrated = true`. Such a request stays `requested` with
`held = "migrating"`, and the person is told the workspace is being migrated,
not that it waits for an admin. The People pane offers no Approve on a held
record. A manual approval of it through the API is refused
`WORKSPACE_MIGRATING`, a new refusal that ships report-only like every other
(`REFUSAL_WORKSPACE_MIGRATING`, [api-refusals.md](api-refusals.md)). Lane W9's
migration writes its record instead.

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

### 2.1 What triggers it (decided, WD2; re-decided 2026-10-10)

**An approval starts one execution of a Cloud Run job in the project, and the
job does the work** (owner re-decision of WD2, 2026-10-10, recorded on #847;
it supersedes the Cloud Build trigger of 2026-10-08, §9 WD2). The job's logs
stay in the project's Cloud Logging, routed to a restricted bucket (§2.6), not
in a public GitHub Actions log.

* **The job.** `swarm-workspace-apply`, a Cloud Run job (`google_cloud_run_v2_job`)
  in the platform's region, in the platform's project. It runs **the image the
  release already builds from `main`**, `images/workspace-apply`, named **by
  digest** in the bootstrap variable that pins it today
  (`workspace_apply_builder_image`, renamed `workspace_apply_image` by W4b), and
  that image now carries the code: the guard, `register-tenant.sh`,
  `kubernetes/` and what they import (§2.2). One task, no retries
  (`max_retries = 0`: a retry is an admin's action, §1.3), a task timeout of
  1800 seconds, as the build had.
* **The identity.** The job runs as `swarm-workspace-deployer` (§2.3), and that
  job is the only thing that may (§2.4).
* **Its network.** Direct VPC egress into the swarm subnet, `ALL_TRAFFIC`, as
  the worker jobs do (`terraform/modules/cloud_run_jobs`), with its own network
  tag, `swarm-workspace-apply`. That reaches the GKE control plane from inside
  the VPC: through the private endpoint where `gke_enable_private_endpoint` is
  true (prod), and through Cloud NAT to the public endpoint where it is false
  and `gke_master_authorized_cidrs` admits it (dev, which is open). This is what
  replaces W0's private-pool question (§10).
* **What reaches it from a caller.** The workspace id and the mode, as the
  execution's two container arguments, and nothing else the job will use. The
  job's entrypoint re-validates both (`^w-[0-9a-f]{6}$`; `create` or `limits`)
  before anything runs, and discards every environment variable a caller could
  override (§2.2 step 0, §2.4). The record's `request_id` no longer rides in the
  call: A1 reads it from the record and logs it, privately.
* **Who made it.** The job, its IAM policy, the identity, the log bucket and
  whatever dispatches it are created by the owner's bootstrap apply
  (`terraform/bootstrap/`), once, **by Terraform alone**: no repository
  connection, no forge, no OAuth, no grant to a Google service agent (§10).

What the job does is one script: `scripts/register-tenant.sh --workspace
w-3f9a2c` (WD3, §4), with every cloud and cluster call it makes passing through
the call guard (§2.5).

#### How an approval starts the job: decided 2026-10-10 (owner), option (ii)

**Decided: (ii).** swarm-api keeps only the publish to one topic; an Eventarc
trigger hands the message to a Workflows definition that validates the id and
mode and runs the job with those two arguments, as `swarm-workspace-dispatch`.
The comparison below is kept as the record of why. Both options
start the same job with the same two arguments; they differ in who holds the
power to start it, and so in what a compromised swarm-api could do. The
question and the recommendation are in this lane's `questions.json`.

**(i) swarm-api runs the job directly.** On an approval, a retry, a ceiling
change or the sweep, swarm-api calls `jobs.run` on
`projects/<project>/locations/<region>/jobs/swarm-workspace-apply` with
`overrides.containerOverrides[0].args = ["w-3f9a2c", "create"]`. It holds a
custom role of `run.jobs.run` and `run.jobs.runWithOverrides` (and
`run.executions.get`, to read the execution it started) **as a binding on that
job's own IAM policy**, never at the project: Cloud Run exposes no
`resource.name` to IAM Conditions (#965, and `terraform/bootstrap/acceptance.tf`
records the same finding), so a project-level grant could not be narrowed to one
job. The topic, its publisher binding and the message go away; W7's
`publish_workspace.py` becomes a caller of the Cloud Run Admin API.

**(ii) Keep the publish; Eventarc and Workflows run the job.** swarm-api keeps
exactly what it holds today, `roles/pubsub.publisher` on the one topic
`swarm-workspace-apply`, and publishes the same message (§2.2's sweep and
switch unchanged, and W7's code unchanged). An Eventarc trigger on that topic
delivers it to a Workflows definition, `swarm-workspace-apply`, which decodes
the message, checks `workspace_id` against `^w-[0-9a-f]{6}$` and `mode` against
`create|limits`, and calls `googleapis.run.v2.projects.locations.jobs.run` with
those two values as the arguments and nothing else, with `skip_polling` so the
workflow ends once the execution has started. The workflow runs as a new
account, `swarm-workspace-dispatch` (no key, no WIF, its own IAM policy written
authoritatively empty), which holds the same run role **on the job only**; the
Eventarc trigger runs as the same account, holding `roles/workflows.invoker`
(at the project: W0b (7) found on 2026-10-10 that Workflows accepts no narrower
grant, §0).

| | (i) swarm-api runs the job | (ii) Pub/Sub → Eventarc → Workflows |
|---|---|---|
| **what a compromised swarm-api can do** | start executions with **any arguments and any environment variables**, any task count and any timeout: `runWithOverrides` cannot be narrowed to the arguments. The job's entrypoint must therefore neutralise every override (§2.2 step 0) for swarm-api, the most exposed component, not to hold a lever on the identity with project-wide account-IAM power | publish a message. The workflow forwards a valid id and mode and nothing else, so swarm-api cannot reach the environment, the task count or the timeout. This is today's §2.4 R3, unchanged |
| **install steps** | one IAM binding on the job; no new API | the Workflows and Eventarc APIs (`terraform/infra/main.tf`'s service list), one workflow, one Eventarc trigger, one account and its two grants. All Terraform; where the Pub/Sub service agent was enabled on or before 8 April 2021, it also needs `iam.serviceAccountTokenCreator`, a Terraform grant (W0b (7), §0) |
| **swarm-api code** | rewritten publish path (W7b), sweep calls `jobs.run`, a synchronous failure lands on the record | none |
| **latency to the execution starting** | about a second | a few seconds more (Pub/Sub push, Eventarc, a Workflows execution). Both are noise beside the job's own start and its minutes of work |
| **cost** | nothing beyond the job | a few Workflows steps and one HTTP call per approval: inside Workflows' monthly free tier at any rate this platform will approve people. Pub/Sub's share is fractions of a cent |
| **failure visibility** | swarm-api sees a refused `jobs.run` at once | a workflow failure is seen by the sweep's `dispatched_unclaimed` after 10 minutes, as a lost build is today (§2.2) |

**What both share.** A1 refuses a record no admin approved, but swarm-api
writes `workspaces/` and `admin_roles/`, so in either option a compromised
swarm-api can get one forged approval applied; the guard bounds that to one
workspace's own resources, as today. **And in either option**
`swarm-scheduler` (`swarmJobDispatcher`), `swarm-accept`
(`swarmAcceptanceRunner`) and the release deployer (`roles/run.admin`) already
hold `run.jobs.runWithOverrides` **project-wide**, unscopably
(`terraform/bootstrap/platform_roles.tf`, `acceptance.tf`, `variables.tf`), so
the entrypoint's override scrub (§2.2 step 0) is load-bearing in both. (ii)
keeps the internet-facing service off that list; it does not empty the list.
None of those holders can change what the job runs: that needs `actAs` on the
deployer as well (§2.4 safeguard 1).

**Recommended: (ii).** The point of the job is to hold the one power Google
cannot narrow; (ii) keeps the service that faces every user's token at
"publish one opaque id", the shape §2.4 R3 already argues is harmless, and it
changes no swarm-api code. Its extra parts are all Terraform and cost nothing
measurable. (i) is simpler, and acceptable only because the entrypoint scrub
exists anyway for the scheduler, the acceptance account and the release
deployer; it would add the most exposed caller to that list.

Until the owner chooses, §2.2 to §2.6 describe what holds under **both**, and
name the option where they differ.

The options considered for the 2026-10-08 decision (GitHub Actions dispatched
by a single-purpose App, a scheduled GitHub workflow polling Firestore, and a
Cloud Build trigger) are kept in §9 WD2, for history.

### 2.2 How a run proceeds

**The job's image carries the code** (re-decision of 2026-10-10). A Cloud
Build run was handed a checkout of `main`; a Cloud Run execution is handed
nothing but its image. So `images/workspace-apply`, which already carries bash,
gcloud, kubectl with `gke-gcloud-auth-plugin`, jq, curl, python3 and uv (owner
decision 2026-10-08: not the stock `google-cloud-cli` image, which lacks jq and
whose bundled Python carries 7 HIGH CVEs), now also copies in, read-only and
root-owned under `/opt/swarm`, exactly the files today's build reads from its
checkout: `scripts/register-tenant.sh`, `scripts/lib/` (the guard, its rules,
its cases, its shims, `common.sh`), `kubernetes/`, and the two Python packages
those import (`apps/common/swarm_common`, and `quota_broker` for
`kubernetes/render.py`). The release builds and scans it from `main` with every
other image (`scripts/build-images.sh` `ALL_TARGETS`, already); the job runs it
**by digest**, and only the owner's bootstrap apply moves that digest (§2.4 R1).

The build file's three steps become the job's **entrypoint**, preceded by one
step a build never needed. The job's `command` is fixed in the job spec, which
no caller can override (Cloud Run's run overrides carry only arguments,
environment variables, task count and timeout, plus a Preview "delayed"
execution mode; W0b (2), §0). Steps 1 to 3 are `scripts/workspace-apply.sh`, which replaces
`scripts/cloudbuild/workspace-apply.yaml` line for line, and step 0 is
`images/workspace-apply/entry.py` (W6b, W4b; §10):

0. **Scrub** (new). The command is `python3 -I /opt/swarm/entry.py`: isolated
   mode, so no `PYTHON*` variable is read. It keeps **no** environment variable
   it was given. It reads the project and region from the metadata server, not
   from the environment; takes `CLOUD_RUN_EXECUTION` only after checking it
   against `register-tenant.sh`'s `WS_BUILD_RE`, as the run's `build_id`, and
   uses it for nothing but the record and the log; refuses unless
   `CLOUD_RUN_TASK_COUNT` is `1` (A1's claim would stop a second task anyway);
   and then `execve`s step 1 with a **fixed** environment written in that file
   (`PATH`, `HOME`, `PROJECT_ID`, `SWARM_ENV_FILE`, `SWARM_CALL_GUARD`,
   `SWARM_CALL_GUARD_ENFORCE=1`, `SWARM_KUBECTL`, `BUILD_ID`, `NO_COLOR`). This
   is what keeps a caller holding `runWithOverrides` (§2.1: the scheduler, the
   acceptance account and the release deployer in either option, swarm-api in
   option (i)) from reaching the run through `PATH`, an exported bash function,
   `BASH_ENV`, `SWARM_CALL_GUARD_ENFORCE` or `CLOUDSDK_*`. Its own deadline is
   1800 seconds, whatever timeout the execution was given.
1. **Validate.** Exactly two arguments: the workspace id must match
   `^w-[0-9a-f]{6}$` and the mode must be `create` or `limits`, before anything
   else runs. Anything else (no arguments, three, a malformed id) ends the
   execution and writes nothing. Under option (ii) the workflow has already
   checked both; neither check alone is load-bearing.
2. **Install the guard.** As the build's step 2 did: the guard's self-test
   against `scripts/lib/workspace-guard-cases.json` on this image, then
   `workspace-guard.sh init --workspace-id`, which admits A1's reads only; then
   `/opt/swarm/scripts/lib/guard-bin/` first on `PATH`, and a check that
   `gcloud`, `kubectl` and `curl` each resolve into it, so every call the script
   makes goes through `scripts/lib/workspace-guard.sh` (§2.5).
3. **Run the script**, `scripts/register-tenant.sh --workspace <w-id> --mode
   <mode>`, whose steps are these. Its exit 3, "stopped for the platform owner",
   ends the execution as a success, as it ended the build (§2.5):

| id | step | the job's log shows |
|---|---|---|
| A1 | **claim.** Read `workspaces` where `workspace_id` equals the validated argument. Refuse unless the record is `approved` (or `failed` or `needs_owner` with an admin's retry recorded) and `decision.verdict == approved` by an email that is an admin in `admin_roles/` now. In one transaction set `applying` and `run` (§1.1). Write the guard's expectation file (§2.5) from the record, mode 0600, outside `/opt/swarm` (in the execution's in-memory filesystem) | `claimed w-3f9a2c` |
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
ends, which is what the console's progress view reads (§6.1, §6.4). The job's
log lines for each step carry only the workspace id; everything else goes to the same
log, privately (§2.6).

**A guard stop** does not fail the run. The guard refuses the call before it
reaches Google or the cluster, the script sets `needs_owner` with the step id,
and the execution ends. §2.5 says what the owner does next.

**Retries.** A call that fails on `429`, `5xx`, `UNAVAILABLE`,
`DEADLINE_EXCEEDED` or IAM's `409 concurrent policy change` is retried 3 times
with backoff 4, 16 and 64 seconds. The bucket policy is shared by every
workspace, so two runs at once can meet the 409; the retry absorbs it. Any
other failure sets `failed` with a code (§4.3). Every step reads before it
writes (the script's existing `describe` and `_binding_present` checks), so a
re-run after a partial apply makes only what is missing.

**Two runs for one workspace** cannot both proceed: A1's claim is one
transaction, and a second execution finds `applying` with a live execution
name in `run.build_id` and exits. Runs for different workspaces may run at once.

**The dispatch sweep.** An `approved` record whose dispatch failed, or whose
execution never claimed it within 10 minutes, is dispatched again by
`POST /v1/admin/workspaces/sweep`, which the Cloud Scheduler job
`swarm-workspace-sweep` calls every 10 minutes as the rollup-sweeper account
(`modules/scheduler` `workspace_sweep`), beside its other admin sweeps. Not
`swarm-tick`, as this section first said: swarm-api admits `swarm-tick` to no
admin route, and the route is on `ROLLUP_SWEEPER_ROUTES`. The route publishes
at most once per record per 10 minutes, and records each attempt. Under option
(ii) "dispatched" is the publish, exactly as built; under option (i) it is
swarm-api's `jobs.run` call.

**Publishing has a switch, and off is not silent.** swarm-api publishes only
when `WORKSPACE_APPLY_PUBLISH` is on: Terraform's `workspace_apply_publish`,
false in `dev.tfvars` until the job and its dispatch exist (under option (i)
the switch keeps its name and gates the `jobs.run` call). Flip it in the same
release as the owner's bootstrap apply with `enable_workspace_deployer`
(§10); before that, every publish would record `publish_failed` (under option
(i), every `jobs.run` call would fail the same way). The sweep job
runs either way, because the sweep is also the detector: it logs one
`workspace_stuck` entry per stuck record per hour (`reason` `publishing_off`,
`never_dispatched` or `dispatched_unclaimed`), and the `workspace-stuck` alert
(`modules/monitoring`) pages on one within 30 minutes. On 2026-10-09 an
approved request waited ~19 hours with neither in place.

### 2.3 The dedicated identity and its permissions

**Account:** `swarm-workspace-deployer@<project>.iam.gserviceaccount.com`. It is
created by the owner's bootstrap apply, holds **no key**, and has no WIF
binding: no GitHub workflow can impersonate it. The release deployer cannot act
as it, and it cannot act as the release deployer. Since the 2026-10-10
re-decision it is the runtime identity of one Cloud Run job,
`swarm-workspace-apply`, and of nothing else (§2.4 safeguard 1).

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
| `roles/logging.logWriter` | project | a Cloud Build build wrote its log as its identity. **A Cloud Run job does not need it for its own output**: Cloud Run collects the container's stdout and stderr itself. W0b confirms that on one execution; then W4b drops the row, and the bootstrap's role-list validation with it | write only |

The image pull grant on the builder image's repository (`swarmImagePuller`,
`workspace_deployer_pull` in `terraform/bootstrap/workspace_deployer.tf`) goes
too: Cloud Run pulls a job's image as the project's Cloud Run service agent,
not as the job's account, for an image in the same project's Artifact
Registry. W0b confirms it on the same execution.

It holds **no** Cloud Run, Terraform state or Pub/Sub grant. It needs none: the
dispatcher creates a tenant's Cloud Run jobs on demand
(`apps/scheduler/scheduler/dispatch.py::CloudRunJobDispatcher.ensure_job`),
which is exactly why A4's act-as grant matters, and no personal resource is in
Terraform state (§3).

**Who may run, update or delete the job** (re-decision of 2026-10-10). The
deployer's power is reachable only through the job, so the job's own IAM is
part of what bounds it. Cloud Run is not in IAM's resource-attribute list (#965,
`terraform/bootstrap/acceptance.tf`), so no grant on Cloud Run can be narrowed
by a condition: a grant is either a binding **on the job** or project-wide.

| action on the job | needs | who holds it |
|---|---|---|
| **change what it runs** (image, command, environment, account, network) | `run.jobs.update` **and** `iam.serviceAccounts.actAs` on `swarm-workspace-deployer` | nobody holds `actAs` on the deployer at the account level: the bootstrap writes that policy authoritatively empty. So only project-level `actAs` holders (the project's owners and editors, and whoever else W0b's dated listing finds) and the owner's bootstrap apply. `swarm-scheduler` holds `run.jobs.update` project-wide (`swarmJobDispatcher`) **but no `actAs` on the deployer**, so it cannot. **W0b must confirm that Cloud Run checks `actAs` on every update of a job whose account is unchanged** (documentation half read 2026-10-10: creating or updating a job needs it, with no exception stated; the live refusal is pending, §0 W0b (1)); if it does not, the scheduler could swap the image, and the re-decision goes back to the owner before W4b ships |
| **create another job, service or build as the deployer** | `actAs` on the deployer | the same project-level holders; the alert on any execution, revision or build as the deployer that is not this job's catches one (§2.4) |
| **start an execution with its arguments** (`run.jobs.run`) | the job's policy, or project-wide | under option (ii) `swarm-workspace-dispatch`, under option (i) swarm-api, by a binding on the job; project-wide, `swarm-scheduler` (`swarmJobDispatcher`), `swarm-accept` (`swarmAcceptanceRunner`), the release deployer (`roles/run.admin`) and the project's owners and editors |
| **start one with overrides** (`run.jobs.runWithOverrides`) | the same | the same list. An override can replace the arguments, set environment variables, the task count and the timeout. It cannot change the image, the command, the account or the network. **Does it let a caller change anything beyond the workspace id? In the API, yes**: that is why step 0 (§2.2) discards every environment variable, refuses a task count other than one, keeps its own deadline, and step 1 refuses any argument list but `[<w-id>, <mode>]`. With those, an override can choose which approved workspace runs and nothing else |
| **set the job's IAM** (`run.jobs.setIamPolicy`) | project-wide `roles/run.admin` | the release deployer. The bootstrap writes the job's policy **authoritatively** (`google_cloud_run_v2_job_iam_policy`), so a member added outside it is removed at the next bootstrap apply, and the alert on any change to the job's IAM pages at once |
| **cancel or delete it** | `run.executions.cancel`, `run.jobs.delete` | `swarm-reconciler` (`swarmJobReaper`) project-wide, and `roles/run.admin`. Availability only: the job carries `managed-by=swarm-terraform`, which `apps/reconciler/reconciler/backends.py::is_gc_eligible` never collects, and re-creating it as the deployer needs `actAs` |

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
| **ValidatingAdmissionPolicy `swarm-workspace-deployer-scope`** + binding, and three per-kind companions (`-namespaces`, `-roles`, `-rolebindings`) | for any request by this user (`matchConditions` on the caller; no namespace selector, which the deployer could escape by writing an unlabelled namespace; the scope policy matches the resources `provisioner-rbac.yaml` grants a write on, listed by name because GKE Autopilot's admission webhook refuses a `("*", "*")` rule, and a test holds the list to the grants): only `CREATE` and `UPDATE`, no subresource; a `Namespace` must be named `swarm-tenant-u-*` and carry `app.kubernetes.io/part-of=swarm`, its own `swarm-tenant` label and PSA `restricted`; a namespaced object must be in a `swarm-tenant-u-*` namespace and be a kind and name the tenant render produces. A `Role` must be one of the three names, with the rules `kubernetes/render.py` renders. A `RoleBinding` may only reference those Roles, binding `swarm-dispatcher` only to the scheduler, `swarm-reaper` only to the reconciler and `swarm-worker` only to ServiceAccounts in its namespace. `tests/unit/worker/test_workspace_provisioner_scope.py` holds every literal to the render, and the matched users to the bound subjects | this turns "any namespace" into "`swarm-tenant-u-*` only". `kubernetes/policies/pod-security.yaml` is the precedent |

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
safeguards. With WD2 re-decided on 2026-10-10 they read:

1. **It is usable only by the `swarm-workspace-apply` Cloud Run job, which runs
   only the image the owner's bootstrap pinned by digest, with a command no
   caller can change.** Nothing holds `iam.serviceAccountUser` or
   `iam.serviceAccountTokenCreator` on the identity: the bootstrap writes its
   policy with no such member, and it has no WIF binding and no key. Making
   anything run as it (a job, a service, a build) or changing what the job runs
   needs `actAs` on it, and the owner's bootstrap apply is what exercises that.
   W0b lists every principal holding `actAs` or token-creator at the project
   level in the shared project, because such a holder could run something as
   this identity too; the alert below catches it if one does. **What changed
   from the Cloud Build shape:** starting the job is not gated by `actAs`.
   Anyone holding `run.jobs.run` on it may start it, and Cloud Run cannot narrow
   that grant by condition, so several platform accounts already may (§2.3,
   "Who may run, update or delete the job"). Starting it is therefore not the
   boundary; **what a start can choose is**: one of the two arguments, each
   re-validated, with every overridable environment variable discarded (§2.2
   steps 0 and 1), and A1 refusing a record no admin approved.
2. **`.github/CODEOWNERS` names the owner** on the guard
   (`scripts/lib/workspace-guard.sh`, `scripts/lib/workspace-calls.json`,
   `scripts/lib/guard-bin/`), on `scripts/register-tenant.sh`,
   `scripts/lib/common.sh`, on the entrypoint (`scripts/workspace-apply.sh`
   and `images/workspace-apply/entry.py`, in place of the build file), on
   `images/workspace-apply/Dockerfile`, and, under option (ii), on the workflow's
   source; branch protection requires the code owner's review for them. That is
   what the image carries, so it is what bounds the identity. A merged change
   reaches the identity only when the owner's bootstrap apply moves the image
   digest, which is a second, deliberate step the Cloud Build shape did not have
   (it built whatever `main` held).
3. **A log alert** fires on any change by `swarm-workspace-deployer` to an
   account not named `swarm-agent-worker-*`.

Two more alerts, re-targeted from the trigger to the job: any execution,
revision or build that runs as this identity and is not an execution of
`swarm-workspace-apply` (Cloud Run's and Cloud Build's audit entries name the
account a resource runs as); and any change to the job, to the job's IAM
policy, to the identity's own IAM policy, and, under option (ii), to the
workflow, the Eventarc trigger, the dispatch account's IAM or the topic's IAM
(under option (i), the topic is gone). New with the job: an alert on any
`jobs.run` of `swarm-workspace-apply` whose caller is not the dispatcher of the
chosen option (`swarm-workspace-dispatch`, or swarm-api), read from Cloud Run's
**Data Access** audit log. W0b (2) found on 2026-10-10 that `RunJob` is a Data
Access (`DATA_WRITE`) entry, not Admin Activity, and Data Access logs are off by
default, so this alert needs Cloud Run's `DATA_WRITE` audit logs enabled in the
project first (§0, W0b (2)). Whether the entry carries the overrides is pending
the first execution.

**Why this fits #334.** #334 refused project-level account admin to the release
deployer because that identity applies *any* plan that reaches `main`. Here:

1. **It runs one script under a call guard.** The guard allows only calls that
   create or bind that one workspace's resources, and stops on anything else
   (§2.5).
2. **Nothing else can run as it, and starting it chooses only which approved
   workspace runs** (safeguard 1).
3. **It is a separate identity.** The release deployer keeps its per-account
   grants, and its validations stay. #334's rule "CI never holds project-level
   `serviceAccountAdmin`" now reads "the release deployer never does; one
   guarded Cloud Run job does", and `terraform/bootstrap/variables.tf` gains
   a validation that the new identity's roles are exactly the list in §2.3.

**Residual risk, stated so it is not mistaken for zero:**

* **R1. The guard is code, not IAM.** A change to the guard, the script or the
  entrypoint changes what the identity will do once it is in the pinned image.
  Mitigation: safeguard 2; the guard's self-test runs in CI on every change and
  again at the start of every execution (§2.2 step 2); and the owner's
  bootstrap apply moves the digest, so a merge alone changes nothing.
* **R2. A stolen token of this identity** (from a compromised execution) could
  set IAM on any account in the shared project for the token's life. The guard
  bounds the script, not a token used directly. Mitigation: **detective**,
  safeguard 3 and the alerts above; and a run lasts minutes, so a call by this
  identity outside an execution is itself an anomaly the alert sees.
* **R3. A forged start** does nothing on its own: A1 refuses a record no admin
  approved. Under option (ii) a forged start is a forged Pub/Sub message, and
  swarm-api is the topic's only granted publisher; under option (i) it is a
  `jobs.run` by any holder of the run permissions (§2.3), and the alert on a
  caller other than swarm-api sees it.
* **R4. Bucket members.** The bucket grant bounds *roles* on one bucket, not
  *members*. The guard checks every member (C4); the alert covers a token used
  directly. On the WD9 fallback the project half has the same shape, with the
  alert on any member not named `swarm-agent-worker-*`.
* **R5. The logs are private to the project, and the project is shared**
  (§2.6).
* **R6. The scrub is code, and runs after the dynamic loader.** Step 0 is
  `python3`, a dynamically linked program, so an overridden `LD_PRELOAD` or
  `LD_LIBRARY_PATH` is read before step 0 can discard it. It can only name
  files already in the image, which a caller cannot add to before the process
  starts, so it can select a library the image ships and not supply one. The
  image is scanned with every other image. W0b (2) read on 2026-10-10 that
  no documentation restricts these names in an override (§0), so they are
  taken as accepted until the operator's live proof says otherwise. If the owner wants this closed rather
  than argued, step 0 becomes a statically linked binary; that is a change to
  W4b's Dockerfile, not to this design.

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
call, through `redact`, to the job's private log; exits non-zero before the
real binary runs; and the script sets `needs_owner` with the step id. Nothing
the refused call would have done has happened.

**What the owner does then.** The console shows "waiting for the platform
owner" (§1.2). The owner reads the refused call in the execution's log (§2.6). If it is
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

The repository is public; the job's logs are not. The rule stays that **a
person appears in anything public only as `w-…`**, and with WD2 and WD3 almost
nothing public is left to protect:

1. **The list is never in the repository and never in Terraform.** It is
   `workspaces/` in Firestore. Terraform does not read it (§3), so there is no
   tfvars file, no sensitive variable and no plan that could carry a name.
2. **The job's input is opaque.** An execution carries the workspace id and
   the mode only, as its two arguments (and, under option (ii), so does the
   Pub/Sub message and the workflow's argument). A1 reads the rest from
   Firestore.
3. **The job's log is private to the project, and the project is shared.**
   Cloud Run writes the container's output to Cloud Logging as
   `resource.type="cloud_run_job"` with `resource.labels.job_name="swarm-workspace-apply"`.
   Anyone holding log-viewing on `saga-agents-staging` can read `_Default`,
   which may include the other team. So **§2.6's restricted bucket and its
   `_Default` exclusion stay, re-targeted at the job's logs**: the bootstrap's
   sink `swarm-workspace-apply` routes entries matching that filter to the log
   bucket `swarm-workspace-apply`, readable only by the project's owners and
   logging admins and the people in `workspace_log_readers`, and the project
   exclusion of the same name keeps them out of `_Default`. This is firmer than
   the build's filter: `job_name` is a documented label of the
   `cloud_run_job` resource, where the build's `build_trigger_id` was an
   unverified one (`terraform/bootstrap/workspace_deployer.tf`, "NOT VERIFIED
   LIVE"). W0b reads one execution's entries back from the restricted bucket
   and confirms `_Default` holds none. Every command's output still passes
   through `redact`, so no credential is written.
4. **What still reaches every log viewer.** The Admin Activity audit trail is
   routed to `_Required` whatever any sink says. For the job it records each
   account the script creates, which names the tenant id (as it did under
   Cloud Build), and every change to the job and its IAM. A `jobs.run` is not
   in it: W0b (2) found on 2026-10-10 that `RunJob` is a Data Access entry,
   off by default and written to `_Default` once enabled. Then it carries the
   caller and, if the request is logged, the overrides: the workspace id and
   the mode. Under option (ii) the workflow logs no call
   arguments (`call_log_level` `LOG_NONE`), and its event is the opaque id.
5. **Log lines name the workspace id only** (§2.2), so a screenshot of the
   execution's page or the People pane's run link names nobody.
6. **Firestore holds the failure code, not output.** The record's `failure`
   holds a code, and the copy is served from the code (§4.3).
7. **Outputs.** `terraform/infra`'s `tenant_namespaces` output lists group and
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
| made by | the release (`terraform/infra`), after the owner's bootstrap grant | the Cloud Run job `swarm-workspace-apply`, `register-tenant.sh --workspace` (§2, §4) |
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
| `u-bogdan` | in `dev.tfvars` (providers `anthropic`, 80/80). Account, secrets and jobs in Terraform state. Namespace made by hand on 2026-10-07 | **moved out of Terraform state, without destroying anything.** A pull request removes it from `dev.tfvars` and adds `removed` blocks with `lifecycle { destroy = false }` for its instances of the tenancy, secret_manager, firestore and cloud_run_jobs modules, and, in the bootstrap layer, for its `deployer_admin` grant. **The release plan must show 0 to add, 0 to change, 0 to destroy, and only forgets.** `scripts/workspace-migrate-record.sh u-bogdan --apply` then makes `workspaces/u-bogdan` `ready` with `migrated = true`, its current limits and its `anthropic` provider, **keeping the workspace id of a request that already exists** (below). A `--mode verify` run (A9 only) then reads it back; its Terraform-era bindings for the release deployer are on the allowed list for a record with `migrated = true` only |
| `u-sw-c90291` (`swarm-verify`) | in `dev.tfvars`, a service account's tenant | unchanged. Service tenants stay in `dev.tfvars` |
| `u-*` documents created by `ensure_tenant` on first sight (for example `u-admin`) | a tenant document and pool with no infrastructure behind them | no record is written. Each such person sees "Request your workspace", and A8 adopts the existing documents because their principal matches |

The `removed` blocks are public and name `u-bogdan`, which is already in
`dev.tfvars` and its history; they name no workspace id. `u-bogdan` keeps its
`anthropic` provider secret, now outside Terraform with its accessor binding
left in place; the record's optional `providers` list says so, and is empty
for every new person (§8). The release deployer's forgotten
`serviceAccountAdmin` on `u-bogdan`'s account stays until the owner removes it;
it grants nothing the release still uses.

#### W9, built 2026-10-09: what the pull request does, and what the owner runs after it

**Built (lane W9 of #847, 2026-10-09). Not yet applied: nothing below has run
against the project.** The pull request must not merge before the owner's go.

* `terraform/environments/dev/dev.tfvars` no longer names `u-bogdan`; a dated
  comment in its place says why. The anthropic ceiling stays 120, above the
  new floor of 80 (eng and smoke).
* `terraform/infra/removed.tf` forgets u-bogdan's **33** instances: its
  account, its Workload Identity, act-as and project grants, its three bucket
  grants, its image-pull and broker-invoke grants, its secret and refresh
  twin with their three authoritative bindings, its tenant document and two
  pool documents, its four Cloud Run jobs, its five scheduler jobs and its
  wake-topic publisher grant. `terraform/bootstrap/removed.tf` forgets the
  **5** grants the bootstrap made for it: the release deployer's
  `serviceAccountAdmin` on its account (`deployer_admin`), and the four
  user-slot grants (`forge_slot_version_adder`, `forge_slot_version_manager`,
  `forge_refresh_reader`, `forge_slot_reader`). Both sets were read from a
  mock-provider plan with and without u-bogdan, on 2026-10-09.
* **The `removed` blocks name no instance.** Terraform 1.16 refuses an
  instance key in `removed` ("Resource instance keys not allowed", checked
  2026-10-09), and a block naming a whole `for_each` resource would forget
  every tenant's instance. So each instance is first `moved` to an address
  nothing declares, and that address is `removed` with `destroy = false`,
  as `terraform/infra/custom_roles_moved_to_bootstrap.tf` does for the
  broker's grant. A plan with u-bogdan back in `dev.tfvars` fails ("Moved
  object still exists"), which is a guard against re-adding it.
* `tests/terraform/u_bogdan_removed.tftest.hcl` holds it: dev.tfvars names no
  u-bogdan and the bootstrap plans no grant for it while it still plans eng's;
  terraform/infra plans no u-bogdan resource while it still plans eng's;
  every instance a tenant shaped like u-bogdan gets is a `moved` source; every
  `moved` is u-bogdan's and lands on a `removed` with `destroy = false`. A
  mock plan has no state, so it cannot show the forget itself. That is step 2.
* `scripts/workspace-migrate-record.sh` writes the record of step 3 through
  `swarm_api.workspaces.Workspaces.migrate`, dry run by default. Added on the
  owner's review of this pull request (2026-10-09), with the in-place rule for
  a request that already exists. `tests/unit/control_plane/test_workspace_migrate.py`
  holds the rule (the id kept in place, the fresh create, the no-op re-run, the
  refusals, a dry run writing nothing) and
  `tests/unit/scripts/test_workspace_migrate_record.py` runs the real script
  against a fake Firestore (dry run, `--apply` through a terminal,
  `SWARM_ASSUME_YES` ignored, the live quota read only through the swarm
  cluster's context).

**Two things the plan will show that are not forgets.** Neither destroys
anything:

* **One in-place change: the artifact bucket's Nearline rule.**
  `terraform/modules/storage` lists each `var.tenants` key's `tasks/` and
  `verdicts/` prefixes in the bucket's Nearline lifecycle rule
  (`aged_prefixes`), so the release plans **1 to change**:
  `module.storage.google_storage_bucket.artifacts`, its `matches_prefix`
  losing `tenants/u-bogdan/tasks/` and `tenants/u-bogdan/verdicts/`. That is
  the cost difference §3.2 already accepts for every personal workspace; the
  Delete rule does not change. If the owner wants a literal 0 to change, the
  rule needs a `tenants/u-` prefix, which is a change to the storage module and
  is not in W9's territory.
* **u-bogdan's four forgotten Cloud Run jobs keep `managed-by=swarm-terraform`.**
  The dispatcher refreshes the image only of jobs labelled
  `managed-by=swarm-scheduler` (`apps/scheduler/scheduler/dispatch.py`,
  `_refresh_image`), so these stay at the image of the last release that
  applied them. Relabelling them `managed-by=swarm-scheduler` hands them to
  the dispatcher. That is an owner decision, and it is not done by this PR.

**The owner's steps after merge, in this order.** No agent runs them: each
needs credentials only the owner holds.

1. **The bootstrap apply of `terraform/bootstrap/removed.tf`**, from `main`,
   as every bootstrap change is (`scripts/bootstrap.sh`). Its plan must read
   **0 to add, 0 to change, 0 to destroy**, and list the 5 grants above under
   "will no longer be managed by Terraform". Anything else is a stop.
2. **The release's infra plan.** The release after merge plans
   `terraform/infra`. For u-bogdan it must read **0 to add, 0 to change, 0 to
   destroy**, and list the 33 instances above as forgotten. The one in-place
   change it may carry is the bucket rule above. Any destroy or replace naming
   `u-bogdan` is a stop: do not approve it in `dev-iam`.
3. **The workspace record: `scripts/workspace-migrate-record.sh u-bogdan`**,
   first without `--apply` (a dry run that prints the record before and
   after, redacted, and writes nothing), then with `--apply`, which asks for
   `u-bogdan` typed at a terminal and ignores `SWARM_ASSUME_YES`. The write is
   `swarm_api.workspaces.Workspaces.migrate`, one Firestore transaction, so
   the record's shape is the one `request()` gives a new record and is stated
   nowhere else. It reads `tenants/u-bogdan` for the principal, the live
   `max_active` and `capacity_units` (80 and 80) and the provider keys
   (`anthropic`) that become `providers`, and the namespace's live
   ResourceQuota (`swarm-tenant-quota`, through the swarm cluster's context
   only) for `quota_pods` and `quota_cpu`; the verify run compares all four.
   What it writes depends on the record it finds:

   | the record | what the migration does |
   |---|---|
   | `requested`, `failed` or `denied` | **completed in place.** Its `workspace_id`, its `workspace_ids/` entry and its `request_id` are kept; `state` becomes `ready`, `migrated` `true`, `providers` and `limits` as read, `failure` null, `steps` empty; an earlier decision moves to `history` |
   | none | created as a request creates one, with a **fresh** `workspace_id` and its `workspace_ids/` entry, `requested_via` `migration`, then the same fields |
   | `ready` and `migrated` | **nothing is written**: a re-run is a no-op and asks nothing |
   | `approved`, `applying`, `needs_owner`, or `ready` not migrated | refused, nothing written: an execution may be making, or made, the same resources |

   Both writes record `decision` `{by: "migration", verdict: "approved",
   reason: "Terraform-era tenant moved by W9", at}` and one `admin_audit`
   entry (`action` `migrate`, `target_workspace_id`, `by` `migration`) in the
   same transaction. `ready_at` stays empty: the verify run writes it. The
   `--apply` run writes only if the record is still in the state the dry run
   read; otherwise it refuses and the transaction writes nothing.

   **A request already exists (owner, 2026-10-09).** The owner requested a
   workspace in the console on 2026-10-09, before this ran, so
   `workspaces/u-bogdan` is `requested`, with a workspace id and its
   `workspace_ids/` entry. The migration completes that record in place and
   keeps its id; writing a fresh id, as this step first said, would have
   orphaned the id the request, its index entry and Admin › People already
   name. The id is not written here (step 5): the script prints it.

   After the record, the same run **creates the person's empty forge slot
   pair** (`swarm-tenant-u-bogdan-git-u-<hex>` and its `-refresh` twin,
   labelled `tenant=u-bogdan`, no value) and grants the worker
   `secretAccessor` on the slot only, through `scripts/lib/forge-slot.sh`, the
   function A6 runs, so a migrated workspace has the shape A9 verifies (owner
   decision 2026-10-10). A re-run with the pair present changes nothing.

   The verify run in step 4 reads this record, so it comes first.
4. **`scripts/register-tenant.sh --workspace <w-id> --mode verify`**, with the id
   step 3 printed (the existing request's), under the
   call guard (§2.5): the expectation file from
   `scripts/lib/workspace-guard.sh init --workspace-id <w-id>`, outside the
   checkout and named by `SWARM_CALL_GUARD`, and `scripts/lib/guard-bin`
   first on `PATH`. This is A1 and A9 only. A1 admits a `migrated` record
   whatever its decision (the migration's names no admin), and the narrowed squat inspection allows the release
   deployer's two Terraform-era bindings on it. A9 re-reads every object,
   rendering a `migrated` tenant with `swarm-agent-worker` only: a
   Terraform-made tenant has no legacy `swarm-worker` objects.
   On a `migrated` record A9 also accepts Terraform's tenant and pool documents where they can legitimately differ (no `namespace`, which `ignore_changes` keeps off a document written before `bootstrap.tf` named it; a limit stored as an integral double or a string of digits; a principal in another case). Both documents must still exist, `service_account` must be the tenant's, and the limits must agree. A failure logs the names of the fields that disagree, never their values (2026-10-10).
5. **Date the result here**: the plan counts of steps 1 and 2, the record's
   workspace id kept private (§1.1), and the verify run's outcome.

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
| runs where | an operator's machine | the Cloud Run job only: it refuses to start without the guard installed (§2.5) |
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
| A1 | Approved | the claim is one transaction; a second run finds `applying` with a live execution and exits | `WORKSPACE_NOT_APPROVED` (not retryable) |
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
| `WORKSPACE_NOT_APPROVED` | no | (not shown to the person: an execution for a record no admin approved. It is an `admin_audit` entry and an alert.) |

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
| `POST /v1/workspace` | the same | the request of §1.3; an admin's own is approved and published in the same call |
| `POST /v1/workspace/loan-request` | the same | records a loan request (`loan_requests/{tenant_id}`), idempotent |
| `GET /v1/admin/people` | admin | §6.4's list |
| `POST /v1/admin/workspaces/{workspace_id}/approve` | admin | §1.3; publishes the workspace id (§2.1) |
| `POST /v1/admin/workspaces/{workspace_id}/deny` | admin | §1.3; body `{reason}` |
| `POST /v1/admin/workspaces/{workspace_id}/retry` | admin | from `failed` or `needs_owner`: publishes again |
| `PUT /v1/admin/workspaces/{workspace_id}/limits` | admin | §6.4's ceiling; body `{max_active}` only |
| `PUT /v1/admin/people/{workspace_id}/loan` | admin | lend or reclaim an account (§6.4) |
| `PUT /v1/admin/admins/{email}` and `DELETE` | admin | grant or remove admin (§6.5) |
| `POST /v1/admin/workspaces/sweep` | the rollup sweeper (`swarm-workspace-sweep`, every 10 minutes) | §2.2's dispatch sweep |
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
steps live, from the record's `steps`, with a link to the job's execution for
those who can read its restricted log bucket (the owner and
`workspace_log_readers`, §2.6). **Deny** opens a text box; the reason is
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
namespace. Each approval costs one Cloud Run job execution of a few minutes,
billed per vCPU-second and GiB-second while it runs (a fraction of a cent at
1 vCPU), and, under option (ii) of §2.1, a few Workflows steps inside its
monthly free tier. No private pool: the job reaches the cluster through direct
VPC egress (§2.1).

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
confirmed defaults) after 05:00 UTC. WD2 was re-decided on 2026-10-10, and one
choice inside it, how an approval starts the job, was decided by the owner the
same day: (ii) (§2.1). Superseded options and recommendations
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
bootstrap and infra layers", was refined by WD2 (a Cloud Build job, then on
2026-10-10 a Cloud Run job) and WD3 (the script, not Terraform).

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
`swarm-agent-worker-*` stay as they were (§2.4). **Superseded on 2026-10-10,
below.** (a), (b) and (c) are kept as they were, for history.

**Re-decided 2026-10-10 (owner, recorded on #847): a Cloud Run job,
`swarm-workspace-apply`.** A person's workspace setup
(`scripts/register-tenant.sh --workspace w-xxxxxx` under the call guard,
`scripts/lib/workspace-guard.sh`) runs as a Cloud Run job, not as a Cloud Build
trigger.

*Why.* SwarmCloud must be generic and easy to install. The Cloud Build path
makes every install do steps Terraform cannot:

* a second-generation repository connection, which is an OAuth authorisation
  and Google's Cloud Build GitHub App on the repository, and works only for the
  forges Cloud Build connects to;
* a Secret Manager grant to Google's Cloud Build service agent, so it can store
  that connection's token. On 2026-10-10 it took two bootstrap applies (PRs
  993 and 1005) and a narrowing the same night, because the first prefix also
  matched the platform's own GitHub App secrets
  (`terraform/bootstrap/cloudbuild_connection.tf`);
* possibly a private pool, so a build can reach a GKE control plane under
  `master_authorized_cidrs` (W0's open question, never verified).

A Cloud Run job:

* runs the image the release already builds from `main`
  (`images/workspace-apply`), pinned by digest;
* runs as `swarm-workspace-deployer`, with its logs private in Cloud Logging:
  §2.6's restricted bucket and `_Default` exclusion stay, re-targeted at the
  job's logs;
* needs no forge connection, so it works for any forge;
* reaches the private GKE endpoint through direct VPC egress, like the worker
  jobs;
* is installed by Terraform alone.

*What it supersedes.* The Cloud Build trigger `swarm-workspace-apply` and its
Pub/Sub subscription; the build file `scripts/cloudbuild/workspace-apply.yaml`
(its three steps become the job's entrypoint, §2.2); the repository connection
`swarm-github` and the two grants to Cloud Build's service agent in
`terraform/bootstrap/cloudbuild_connection.tf`; the safeguard wording "usable
only by that Cloud Build trigger, which builds only `main`", which now reads
"usable only by that Cloud Run job, which runs only the image the owner pinned"
(§2.4 safeguard 1); and W0's private-pool question (§10). The Pub/Sub topic
survives under option (ii) of §2.1 and goes under option (i).

*What it does not change.* The call guard and its rules (§2.5), the script and
its steps A1 to A9 (§2.2), the identity's power and its bounds (§2.3), the
accepted risk and safeguards 2 and 3 (§2.4), the record (§1), the sweep and
its switch (§2.2), and every invariant (§11).

*What it adds to think about.* Starting a Cloud Run job is not gated by
`actAs`, and Cloud Run grants cannot be conditioned, so the scheduler, the
acceptance account and the release deployer can already start this job with
overrides. The job's entrypoint therefore discards every overridable value but
its two validated arguments (§2.2 step 0, §2.3, §2.4 R6).

*Decided 2026-10-10 (owner): (ii)*, the existing publish reaches the job
through Eventarc and Workflows, over (i) swarm-api running the job directly
(§2.1).

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

**Decided 2026-10-08 (owner)**, each as this document already proposed; item 1 was
replaced by the owner on 2026-10-09:

1. **An admin's own request is approved automatically** (decided
   2026-10-09, replacing 2026-10-08's "an admin may approve their own
   request", by hand). The decision says `auto: true` and the audit entry is
   `approve_own_workspace_auto`; a tenant that predates the workspace job is
   the exception (§1.3).
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
store, read by the Cloud Run job at run time (WD2) and never by Terraform
(WD3); public artefacts name a person only by an opaque workspace id (§2.6). This **supersedes** the approval comment's
"SwarmCloud opens and merges the config change for the person": there is no
per-person pull request, because the config change is the private record.

---

## 10. Build plan

Within a phase no file is in two lanes, and a lane depends only on earlier
phases. New files are named without their root.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| W0 | 0 | **verification, no code**: whether an IAM allow policy accepts a principal set naming exactly the personal worker accounts (WD9; if not, the fallback); every principal holding `actAs` or token-creator at the project level in the shared project (§2.4 safeguard 1); that updating or running the trigger needs `actAs` on its account; that a Cloud Build default-pool build reaches the GKE control plane under `master_authorized_cidrs` (if not, a private pool, which is an owner decision on cost); that the build's log can be routed to a restricted log bucket and excluded from `_Default` (§2.6); that the stock `google-cloud-cli` image carries every tool the script needs; `ALREADY_EXISTS` handling of a pre-made slot. Each result is dated in this document. **Re-decided 2026-10-10:** the trigger's `actAs`, the default pool's reach and the build log's routing no longer apply; W0b asks the job's questions | `docs/workspaces.md` | — |
| W1 | 1 | the record and `workspace_ids/`, `GET`/`POST /v1/workspace`, the loan request, the `workspace` and `claude_account` onboarding steps, `WorkspaceNotReady` and `NoClaudeAccount`, the gate behind `WORKSPACE_GATE=off`; `people/` writes; `tenant_for` stops creating `u-*` once the gate is on | new `swarm_api/workspaces.py`, new `swarm_api/routes/workspaces.py`, `apps/swarm-api/swarm_api/service.py`, `apps/swarm-api/swarm_api/errors.py`, `apps/swarm-api/swarm_api/onboarding.py`, `apps/swarm-api/swarm_api/main.py` | W0 |
| W2 | 1 | admin roles in Firestore (§6.5): `admin_roles/`, `admin_audit/`, `PLATFORM_OWNER`, the one-time migration of `admin_users`, the grant and remove routes with the owner and last-admin safeguards | new `swarm_api/admins.py`, new `swarm_api/routes/people.py`, `apps/swarm-api/swarm_api/auth.py`, `apps/swarm-api/swarm_api/settings.py` | W0 |
| W3 | 1 | **the call guard**: `scripts/lib/workspace-guard.sh` with C0–C9 in `scripts/lib/workspace-calls.json`, the three shims, `--report-only`, its self-test cases (every rule refusing a call built to trip it), the no-absolute-path test, and the guard-aware `kubectl_bin` and `prefer_local_bin`; `.github/CODEOWNERS` on the guard, the script, `common.sh` and the build file (**re-decided 2026-10-10:** the build file's entry moves to the entrypoint and the image, in W6b) | new `lib/workspace-guard.sh`, `lib/workspace-calls.json`, `lib/workspace-guard-cases.json` and `lib/guard-bin/` in scripts/, `scripts/lib/common.sh`, `.github/CODEOWNERS`, new `tests/unit/scripts/test_workspace_guard.py` | W0 |
| W4 | 2 | **Terraform, owner-approved once**: in bootstrap, `swarm-workspace-deployer` (no key, no WIF, no `actAs` member), its custom roles and conditioned grants, the `swarm-workspace-apply` topic with swarm-api as its only publisher, the Pub/Sub Cloud Build trigger building `main` only, the restricted log bucket and sink, the `swarm-tenant-u-` slot bindings for swarm-api, **the WD9 principal-set grant** (or, on the fallback, the conditioned `projectIamAdmin`), and the validation of the identity's role list; the alerts of §2.4; in infra, the validation refusing a human user tenant; `terraform test` assertions, including `personal_workspaces_absent` (§3.2). **Re-decided 2026-10-10:** the Cloud Build trigger, its repository variable, the image-pull grant and the trigger-keyed log filter are replaced by W4b; the identity, its roles and grants, the log bucket, the WD9 fallback grant and the alerts' account rule stay | new `workspace_deployer.tf` in terraform/bootstrap, `terraform/bootstrap/forge_user_slots.tf`, `terraform/bootstrap/variables.tf`, `terraform/modules/monitoring/`, `terraform/infra/variables.tf`, new `personal_workspaces_absent.tftest.hcl` and `workspace_deployer.tftest.hcl` in tests/terraform | W0 |
| W5 | 2 | **cluster, owner-applied once**: the deployer's ClusterRole, binding and kube-system Role, and the scope ValidatingAdmissionPolicy, plus the parity test that the policy's Role literals equal the render | new `rbac/provisioner-rbac.yaml` and `policies/workspace-provisioner-scope.yaml` in kubernetes/, `kubernetes/render.py` (`POLICY_FILES`), `kubernetes/apply.sh` (`--policies` looks up the uniqueIds) | W0 |
| W6 | 2 | **`register-tenant.sh --workspace`** (§4.1): the record read, the derived-id check, `--mode create`, `limits` and `verify`, A1–A9 with progress writes, **the act-as grant**, the narrowed squat inspection, the forge slot, the refusal to start without the guard; and **the build file** `scripts/cloudbuild/workspace-apply.yaml` (validate, install the guard, run). **Re-decided 2026-10-10:** the script stays; the build file is replaced by the job's entrypoint in W6b | `scripts/register-tenant.sh`, new `cloudbuild/workspace-apply.yaml` in scripts/, `tests/unit/scripts/` (new `test_register_tenant_workspace.py`) | W3 |
| W7 | 2 | People and the approval flow in swarm-api: the admin list, approve, deny, retry, limits, loan, the Pub/Sub publish and the sweep route | `swarm_api/routes/people.py` (W2's new file, extended), new `swarm_api/publish_workspace.py`, `apps/swarm-api/swarm_api/routes/accounts.py` | W1, W2 |
| W8 | 3 | the console: the checklist steps, the progress view, the Submit banners, Admin → People; the plugin: `sc setup`, `swarm_setup_workspace`, `/sc:setup`; the issue forms' "Where" list | `apps/swarm-ui/src/GitHubConnect.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/SubmitWorkflow.tsx`, `apps/swarm-ui/src/App.tsx`, new `People.tsx` in apps/swarm-ui/src, `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/server.py`, `plugin/commands/setup.md`, `.github/ISSUE_TEMPLATE/` | W1, W7 |
| W9 | 4 | **the `u-bogdan` migration**: the `removed` blocks in both layers, its removal from `dev.tfvars`, the record, and a `--mode verify` run; the release's plan must read 0 to add, 0 to change, 0 to destroy | `terraform/infra/removed.tf`, `terraform/bootstrap/removed.tf`, `terraform/environments/dev/dev.tfvars`, `scripts/workspace-migrate-record.sh` | W4, W6 |
| W10 | 8 (was 5) | the first real approval end to end, **through the job**, then `WORKSPACE_GATE=on` (WD8); docs: multi-tenancy, offboarding runbook, onboarding, ci.md (the Cloud Run job `swarm-workspace-apply`, how it is started, and where its logs are: the restricted bucket of §2.6), register-tenant.sh's header | `docs/multi-tenancy.md`, `docs/runbooks/tenant-offboarding.md`, `docs/onboarding.md`, `docs/ci.md` | W5, W8, W9, W4b, W6b, W7b |
| W0b | 6 | **verification for the 2026-10-10 re-decision, no code**, each result dated in this document: **(1), before W4b is applied:** that Cloud Run checks `iam.serviceAccounts.actAs` on the job's account on every `jobs.update` and `jobs.create`, including one that leaves the account unchanged, read from Google's documentation and shown by a refused update from an account holding `run.jobs.update` but no `actAs` (§2.3; if it does not, stop and ask the owner); (2) that `runWithOverrides` carries only arguments, environment variables, task count and timeout, and whether an override may set `CLOUD_RUN_*` or `LD_*` names (§2.2 step 0, §2.4 R6); (3) every principal holding, at the project level in the shared project, `actAs` or token-creator, and `run.jobs.run`, `runWithOverrides`, `update` or `setIamPolicy` (§2.3); and on the first execution after W4b: (4) that its output reaches Cloud Logging without `roles/logging.logWriter`, and its image is pulled without `swarmImagePuller`; (5) that its entries carry `resource.labels.job_name`, land in the restricted bucket and not in `_Default`; (6) that direct VPC egress reaches the control plane: the public endpoint through Cloud NAT in dev, the private endpoint from the swarm subnet where it is private; (7) **option (ii) only:** whether `roles/workflows.invoker` can be granted narrower than the project, and whether this project needs the Pub/Sub service agent's token-creator grant for Eventarc | `docs/workspaces.md` | the owner's choice in §2.1 for (7) |
| W6b | 6 | **the job's image and entrypoint**: `images/workspace-apply/Dockerfile` copies in, read-only under `/opt/swarm`, `scripts/register-tenant.sh`, `scripts/lib/`, `kubernetes/`, `apps/common/swarm_common` and `quota_broker`, and sets no `ENTRYPOINT` the job relies on (the job's `command` is W4b's); new `entry.py` (§2.2 step 0: discard the environment, metadata-server project and region, `CLOUD_RUN_EXECUTION` checked against `WS_BUILD_RE`, task count one, own 1800-second deadline, `execve` with the fixed environment); new `scripts/workspace-apply.sh` (steps 1 to 3, the build file's lines, with the two-argument check and exit 3 as success), which replaces `scripts/cloudbuild/workspace-apply.yaml`, deleted; `register-tenant.sh`'s header and `BUILD_ID` wording; CODEOWNERS on `entry.py`, `scripts/workspace-apply.sh` and the Dockerfile in place of the build file; the PR-check expectation that the guard and the script now rebuild `workspace-apply`; a unit test that every environment variable handed to `entry.py` is gone in the child, that any argument list but `[<w-id>, <mode>]` is refused, and that a task count other than one is refused | `images/workspace-apply/Dockerfile`, new `images/workspace-apply/entry.py`, new `scripts/workspace-apply.sh`, `scripts/cloudbuild/workspace-apply.yaml` (deleted), `scripts/register-tenant.sh` (comments and the `BUILD_ID` message only), `.github/CODEOWNERS`, `tests/unit/scripts/test_build_images_pr_check.py`, `tests/unit/scripts/test_register_tenant_workspace.py`, new `tests/unit/scripts/test_workspace_apply_entry.py` | W6 (built) |
| W4b | 7 | **Terraform, owner-approved once**: in `workspace_deployer.tf`, `google_cloud_run_v2_job.workspace_apply` in place of `google_cloudbuild_trigger.workspace_apply` (the image by digest, `command` `python3 -I /opt/swarm/entry.py`, one task, `max_retries = 0`, timeout 1800s, direct VPC egress `ALL_TRAFFIC` into the swarm subnet with the worker network tag, so `modules/network`'s worker-ingress deny covers it, `managed-by=swarm-terraform`) and its authoritative `google_cloud_run_v2_job_iam_policy` naming the chosen dispatcher only; the sink and exclusion filter re-targeted at `resource.type="cloud_run_job" AND resource.labels.job_name="swarm-workspace-apply"`; `workspace_deployer_pull` removed, and `roles/logging.logWriter` if W0b (4) says so; `workspace_apply_builder_image` renamed `workspace_apply_image`, `workspace_apply_repository` removed, the swarm subnet named; the alerts of §2.4 re-targeted, plus the alert on a `jobs.run` from any other caller. **Option (ii):** new `workspace_dispatch.tf` (the account `swarm-workspace-dispatch` with its policy written empty, the workflow, the Eventarc trigger on the existing topic, the run role on the job, the invoker grant) and the workflow's source, and the Workflows and Eventarc APIs in infra's service list. **Option (i):** the topic and its publisher binding removed, and swarm-api's run role bound on the job. `terraform test` for each | `terraform/bootstrap/workspace_deployer.tf`, `terraform/bootstrap/variables.tf`, `terraform/modules/monitoring/workspace_alerts.tf`, `scripts/lib/unlabelable-types.json` (drop `google_cloudbuild_trigger`), `tests/terraform/workspace_deployer.tftest.hcl`, `tests/terraform/personal_workspaces_absent.tftest.hcl`; option (ii) adds new `terraform/bootstrap/workspace_dispatch.tf`, new `terraform/bootstrap/workflows/workspace-apply.yaml`, `terraform/infra/main.tf` (the service list only) and new `tests/terraform/workspace_dispatch.tftest.hcl` | W0b (1), W6b (its image promoted, for the digest), the owner's choice |
| W7b | 7 | **swarm-api. Option (i):** `publish_workspace.py` becomes a caller of `jobs.run` on the one job with the two arguments (same `publish` contract: returns False, never raises, a failure stays `approved` for the sweep), its settings, and the Cloud Run client dependency. **Option (ii):** the module docstring's "Cloud Build trigger" becomes "Eventarc and Workflows start the Cloud Run job", and nothing else | `apps/swarm-api/swarm_api/publish_workspace.py`; option (i) adds `apps/swarm-api/swarm_api/settings.py`, `apps/swarm-api/pyproject.toml` and the publish tests under `tests/unit/control_plane/` | the owner's choice |
| W11 | 9 | **removal, once the job ships** (the first execution has reached A9 and W0b (4) to (6) are dated): delete `terraform/bootstrap/cloudbuild_connection.tf` (its two custom roles and the two grants to Cloud Build's service agent) and any assertion that names it; the owner then deletes the `swarm-github` connection (`gcloud builds connections delete swarm-github --region=us-central1`), the `swarm-github-github-oauthtoken-*` secret Google's agent made for it, which deleting a connection does not remove, and uninstalls Google's Cloud Build GitHub App from the repository. `cloudbuild.googleapis.com` stays enabled: the release builds every image with Cloud Build | `terraform/bootstrap/cloudbuild_connection.tf` (deleted), `tests/terraform/` (none names it, read 2026-10-10) | W4b applied, W10 |
| — | later | deprovisioning (§7), with its own guard mode | — | the owner's decision then |

**The owner's one-time steps** (shrunk by the 2026-10-10 re-decision):

* **Choose how an approval starts the job**, (i) or (ii) (§2.1). Recommended:
  (ii).
* **W4 and W4b** are bootstrap changes, so they are owner-run bootstrap applies
  from `main`, as every bootstrap change is: the identity, its roles, the job
  with its image digest and its IAM, the dispatch (topic, workflow and Eventarc
  trigger under (ii)), the log bucket and the WD9 grant. Its infra half (the
  service list, under (ii)) is a release whose IAM plan waits in `dev-iam`.
  Custom roles follow `docs/runbooks/custom-roles-to-bootstrap.md`. **Nothing
  outside Terraform**: no repository connection, no OAuth, no grant to a Google
  service agent. **Removed:** "Connect this repository to Cloud Build".
* **W5** is an owner-run `kubernetes/apply.sh --policies --confirm`.
* **W9** is an owner-run bootstrap apply of the `removed` blocks; the infra
  half is a release whose plan he checks reads 0 to add, 0 to change, 0 to
  destroy for u-bogdan; then the record (`scripts/workspace-migrate-record.sh
  u-bogdan --apply`) and a `--mode verify` run. §3.3 lists
  the five steps in order (W9 built 2026-10-09, not yet applied).
* **No private pool.** W0's question, whether a Cloud Build default-pool build
  could reach the control plane under `master_authorized_cidrs`, is replaced by
  W0b (6): the job runs in the swarm VPC through direct VPC egress, as the
  worker jobs do, so it reaches the private endpoint from inside the VPC where
  the endpoint is private, and the public endpoint through Cloud NAT where the
  authorised networks admit it (dev's are open). If a prod cluster ever
  restricts its private endpoint to listed ranges, the swarm subnet's range is
  the entry to add, in `gke_master_authorized_cidrs`, a Terraform value.
* **Moving the image** is a bootstrap apply that changes
  `workspace_apply_image` to the digest the release promoted. It is the step
  that lets a merged change to the guard, the script or the entrypoint reach
  the identity (§2.4 R1).
* **W11, after the job ships:** delete the `swarm-github` connection, its
  OAuth secret, and Google's Cloud Build App from the repository.

**Removal note.** The `swarm-github` Cloud Build connection and the grants in
`terraform/bootstrap/cloudbuild_connection.tf` exist only for the Cloud Build
trigger. They are **deleted once the job ships** (W11): not before, so a
rollback to the trigger stays possible until the first execution has made a
workspace end to end, and not long after, because the condition there admits
Google's agent to set IAM on a secret prefix nothing else needs.

After those, **no person's onboarding needs the owner**, unless the guard stops
a run. W3, W4, W6 and W7 get the one review (credentials, tenant isolation,
IAM). W1 gets it too, for the gate, and so do W4b, W6b and W7b: the job's IAM
and its entrypoint scrub are what decide who can steer the identity.

---

## 11. Invariants, each with how it holds

Re-read on 2026-10-10 against WD2's re-decision: every statement below holds
under the Cloud Run job, under either dispatch option of §2.1.

1. **Demand only from `LEASED`…`RUNNING`.** Requesting, approving and applying
   a workspace create no task and no lease. A ready, idle workspace holds
   nothing, and the job's execution is not a worker: it runs as
   `swarm-workspace-deployer`, not a tenant's worker identity, holds no lease,
   is created by Terraform and never by the dispatcher, and is no tenant's
   capacity.
2. **All-or-nothing reservation.** Unchanged. A8 writes a pool document's
   limit, never its `active`; a pool that exists keeps its `active`.
3. **Concurrency from `LEASED`.** Unchanged. Lowering a ceiling refuses new
   leases above it and touches nothing running.
4. **Workers never sleep through a wait.** The run is not a worker. swarm-api
   and the plugin never wait inside a request or a tool call for a run; the
   console polls. Neither dispatch waits either: under option (i) `jobs.run`
   returns once the execution exists, and under option (ii) the workflow calls
   it with `skip_polling`.
5. **Fencing.** Unchanged. The claim in A1 is the run's own fence: a second
   execution, whoever started it and with whatever task count, finds
   `applying` with a live execution and exits, and a guard stop leaves
   nothing half-applied that a later run would trust: every step reads before
   it writes, and A9 re-reads everything before `ready`.
6. **No Spot.** Nothing here declares any; a Cloud Run job has no Spot
   capacity to declare.
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
    server turns into a quota by a fixed ratio. The job's two arguments are
    the record's opaque id and a mode the route chooses, never request input,
    under either option; the job's image, command and resources are fixed by
    the bootstrap, and its entrypoint discards whatever else an override
    carries (§2.2 step 0).
