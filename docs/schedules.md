# Schedules: typed recurring jobs per repository and tenant, with gates and approvals

**Status: DESIGN, 2026-10-08 (lane SCHED0, part of #892). None of it is
built.** The owner answered the eleven open questions (SD1-SD11) the same
day. Each answer is recorded in §10 as **Owner decision 2026-10-08** and is
applied in every section it changes; where this document used to say
"recommended", it now says what was decided. The owner asked on 2026-10-08:

> "we need a way to view and fully manage crons like this one above for self
> improvement or auto planning and executing and merging open github issues
> and more. This should be a section in the UI as well and maybe shown in new
> runs or maybe below runs in the main menu. Or come up with your preferred
> approach to manage these recurring jobs, the types of jobs we should support
> and the required gates and human approvals if needed. Make it so that its
> also supported per repo and fully multi tenant."

The "cron above" is the operator's session cron (job `bdb47d30`, 2026-10-08),
which sweeps open issues from one person's Claude session. It dies with that
session, expires after seven days, and nobody else can see it. This document
designs what replaces it. The screens are drawn, in two or three variants each,
in [web-ui/mockups/schedules.html](web-ui/mockups/schedules.html), and the
owner's picks are in [web-ui/mockups/PICKS.md](web-ui/mockups/PICKS.md). The
owner's decisions are §10.

What it settles, one line each:

* **A schedule is a tenant's Firestore document,** `schedules/{schedule_id}`.
  It names a **type by name** from a fixed catalogue, a repository scope, a cron
  expression with a timezone, the type's parameters, a gate, a budget and an
  owner. It is not a frozen-contract change (§1).
* **One platform tick fires every due schedule.** One Cloud Scheduler job runs
  every minute for all tenants, personal `u-*` tenants included, as its own
  service account, `swarm-schedule-tick` (SD10). Each due slot
  is claimed **exactly once** by creating a firing document in a Firestore
  transaction. There is no GCP job per schedule (§2).
* **A firing creates ordinary work:** an issue run, a task or a workflow,
  submitted as the schedule's owner in the schedule's tenant, marked
  `metadata.schedule`, and admitted like anything else. A few types make only
  GitHub API calls and no agent work; they run inside swarm-api, use no
  capacity, and are recorded the same way (§2.7, SD8).
* **Twelve types**, from `issue-sweep` to `custom-prompt`. Each has
  parameters, a default gate, an output and a blast radius. The first four
  built are `issue-sweep`, `issue-plan-only`, `repo-index-refresh` and
  `observer` (SD2). The issue sweep that lane SWEEP (PR 898, default off) is
  building is the **first implementation of `issue-sweep`**, and §8.1 says
  how it migrates (§3, §8).
* **A gate is a risk tier plus an approval mode.** Each type sets a floor that a
  tenant can make stricter but never looser. Some **hard stops** apply whatever
  the mode: IAM and Terraform bootstrap changes, frozen-contract edits,
  `.github/workflows/`, security-class issues, budget exhaustion and repeated
  failure (§4). A hold lives on the issue run itself, so no approve path, the
  tick's auto-approval included, can pass it (§4.5). A schedule may be
  switched from approval-required to fully automatic merge in the console, as
  an audited edit, and the hard stops stay in force when it is (SD3, §4.2).
* **One approvals inbox** for plan, run, merge and proposal approvals, across
  schedules and issue runs. A member may also approve by commenting
  `/swarmcloud approve <digest>` on GitHub, in the first build (SD9, §4.9). Each approval is an approval of a **digest**, as
  issue runs approve plans (D3). A pending approval is a document and holds no
  capacity, and it expires (§4.4-§4.7).
* **Fully per tenant (§5).** A tenant sees and edits only its own schedules,
  and every firing uses that tenant's identity, secrets, prefix and ceilings.
  Admins get a cross-tenant view that can pause but cannot edit or approve.
* **A new spine section, Automate** (Overview, Work, Automate, Capacity,
  Admin), holding Schedules and the Approvals inbox, and an admin view under
  Admin (§6, SD1). Also a REST API, MCP tools and `sc schedules`
  (§7).

`eng` is an example tenant and `example-org/example-api` an example repository.
`u-alice` is a personal workspace, as in [workspaces.md](workspaces.md).
Dollar figures given as defaults are the owner's caps (SD4), set for
schedules only; they are not measurements, and no run's cost has been
compared with them.

---

## 0. Today, re-read 2026-10-08 against `main` (cbba982)

* **Recurring work runs from one person's session.** Job `bdb47d30` is a
  `CronCreate` job in the operator's Claude session. It is session-only, it
  expires after seven days, and nobody else can see, pause or audit it
  (#892).
* **Platform ticks are one Cloud Scheduler job per purpose, mostly one per
  tenant.** `terraform/modules/scheduler/jobs.tf` declares `safety_tick`,
  `reconciler`, `quota_refresh` and `forge_refresh` once each, and
  `workflow_rollup`, `issue_run_advance`, `repo_index_poll` and `merge_wake`
  once per tenant in `var.rollup_tenant_ids`. The per-tenant ones call a
  `/v1/admin/...?tenant_id=` route as the `swarm-rollup-sweeper` OIDC identity,
  which swarm-api admits to those routes only
  (`apps/swarm-api/swarm_api/auth.py::ROLLUP_SWEEPER_ROUTES`).
  **A personal `u-*` tenant is in none of the per-tenant ticks** because it is
  not in `var.tenants`. Its issue runs move only when someone reads them
  ([issue-runs.md](issue-runs.md#the-cloud-scheduler-tick-and-why-every-read-also-advances)).
* **The repository index already has a per-repository recurring job.**
  Each registration carries `index.interval_hours` (default 24),
  `index.on_change` (`poll`), `index.min_change_interval_minutes` (default 30),
  `index.full_every_days` and `index.paused`. The 5-minute `repo_index_poll`
  tick reads each head with an ETag and queues an index run when the head has
  moved or the interval has passed
  ([repo-index.md §3.3](repo-index.md#33-triggers-an-interval-and-a-change-on-the-default-branch)).
* **The issue run has the gate shapes this design reuses.**
  - `plan_approval: auto | required`.
  - `PLANNED` is a document and nothing else, so waiting holds no capacity.
  - Approval is of a plan **digest**, and a mismatch answers 409 `plan_changed`.
  - `fix_rounds` is 1-5, default 3.
  - `auto_merge` merges through one merge-only continuation, once CI is green
    and the review said MERGE (`apps/swarm-api/swarm_api/issueci.py::_merge`).
  - Any member of the run's tenant may approve.

  See `apps/swarm-api/swarm_api/issueruns.py` and [issue-runs.md](issue-runs.md).
* **Work submitted on someone's behalf is submitted as a stored member.**
  `apps/swarm-api/swarm_api/routes/runs.py::run_owner_auth` and
  `apps/swarm-api/swarm_api/routes/admin.py::registration_owner_auth` build the
  submitter from the stored creator, never from the tick's identity. They ask
  the directory again on every submission whether that person is still a
  member.
* **There are no dollar budgets for work that is not scheduled** (owner
  decision 2026-10-01,
  [cost-control.md §2](cost-control.md#2-ceilings-you-actually-own)). That
  decision still holds for tasks, workflows and issue runs started by hand.
  The owner set per-type dollar caps for **schedules only** on 2026-10-08
  (SD4, §4.3).
  - `monthly_budget_usd` is refused with a 422.
  - `PARKED(BUDGET_EXHAUSTED)` is never written.
  - Per-attempt `cost_usd` is recorded **when the attempt ends**, and only when
    the runner reported one.

  Concurrency ceilings are the only spend control applied before work runs.
* **Admins live in Firestore** (`admin_roles/`, `admin_audit/`, PR 860,
  `apps/swarm-api/swarm_api/admins.py`). `PLATFORM_OWNER` is the owner, and
  admin rights are platform-wide. **There is no tenant-admin role**: inside a
  tenant every member is equal.
* **A new refusal ships report-only** (PR 873,
  `apps/swarm-api/swarm_api/refusals.py`): it is logged under its own code and
  turned on later with `REFUSAL_<CODE>=on`.
* **The forge credential cannot push `.github/workflows/`.** Workflows write is
  not among the App's permissions ([onboarding.md D8](onboarding.md#d8-what-may-swarmcloud-do-as-the-user-beyond-clone-push-and-pull-request),
  option (a)), and the worker's pushes of workflow files were refused on
  2026-10-08.
* **Security-class issues need the owner's OK for their plans** (owner decision,
  2026-10-08).
* **Lane SWEEP (PR 898, branch `swarm/task_3804051834a74239a9e8`) is in
  flight and not on `main`** at cbba982 (`git grep -i issue_sweep` finds
  nothing in `apps/` or `terraform/`). It is the first implementation of the
  `issue-sweep` type (SD2, §8.1). Its brief:
  - a Cloud Scheduler `issue_sweep` job, shipped **off**;
  - the planner decides readiness, with a `NOT_READY` verdict;
  - at most **8 live runs**;
  - a **territory guard**, so two runs do not edit the same files.

  §8.1 is written against that brief and against PR 898's description, as
  read on 2026-10-08: its route `POST /v1/admin/issues/sweep`, its module
  `swarm_api/issuesweep.py`, its tenant-document settings and its
  `issue_sweep` scheduler job. The build lane re-reads SWEEP's merged code
  before it starts. **SWEEP has since merged** (`ce68220`, #898). Lane S0
  read its names from `main` on 2026-10-08 and lists where they differ from
  §8.1 in §0.1, check 3.
* **The console's Work section has ten tabs:** Agents, Workflows, Runs,
  Timeline, Repositories, Setup, Access and the three Submit screens
  (`apps/swarm-ui/src/App.tsx` `SECTIONS`).
  `tests/unit/scripts/test_issue_forms.py` binds the issue forms' "Where" list
  to that array.
* **No cron library is a dependency** of any app.

### 0.1 Lane S0's results, 2026-10-08, against `main` (`ce68220`)

Lane S0 (§9) ran in a SwarmCloud container. The container had no GitHub
credential, no `gh` and no `gcloud`. So **every result below is read from the
repository, not measured live**, except check 3, which is a reading of the
code itself. Each result names the command the operator runs to measure it
live, and a control showing that the reading could have come out the other
way. The clone holds one commit, `ce68220` (#898, SWEEP), so nothing older
could be compared.

**Check 1. `actions: read` and `checks: read` (§3.7, §3.9).** From the
repository, not measured live.

* **The GitHub App** (onboarding's App, whose user tokens are the tenants'
  `app_user` connections) holds **`checks: read` and no `actions` permission
  at all**. `docs/runbooks/github-app.md` step 3 is the record GitHub was
  configured from. It lists `Checks: Read-only` (line 135), lists
  `Workflows: No access`, and says "everything not listed: No access". It does
  not list Actions. `docs/onboarding.md` §3.4 (step 1) and D8 (a) give the same
  set. A user-to-server token can never hold more than the App's own
  permissions, so a tenant connected through onboarding has no `actions: read`.
* **Tenant tokens** (`swarm-tenant-<t>-git`) are whatever each tenant
  stored. The repository records what they **must** hold, not what they do
  hold: `docs/multi-tenancy.md` lists `Checks: Read` (line 311) and
  `Actions: Read` (line 313) for the issue run's CI loop. A classic token's
  `repo` scope carries both. swarm-api already probes `read_checks` per token
  and repository (`gittokens.CAPABILITIES`, `read_read_checks`). **It has no
  `actions: read` probe.** `forgewrite.job_log_tail` turns a missing
  `actions: read` into `None`, so nothing records its absence.
* **Control.** The same grep of the runbook's permission bullets
  (`grep -n -E '^\* (Actions|Checks|Workflows):' docs/runbooks/github-app.md`)
  finds two lines, Checks and Workflows. So the pattern matches a permission
  that is listed, and finds no Actions line because there is none.
  `docs/multi-tenancy.md` lines 311 and 313 match `Actions: Read`, so the
  word is spelled the way the grep looks for it.
* **Consequence.** `ci-flake-hunter` (§3.7) can find a flake from check
  runs alone (`checks: read`, held). Its `workflow_run` reads and
  `release-health` (§3.9, which needs `actions: read`) are
  `available: false` for every onboarding-connected tenant until the App gains
  `Actions: Read`. Adding it is an org-admin approval, which is the owner's,
  and every installation must then accept the new permission.
* **To measure live.** For the App: open GitHub › Settings › Developer
  settings › GitHub Apps › SwarmCloud › Permissions & events, and read the
  Actions and Checks rows, or read `.permissions` of an installation
  (`GET /app/installations/{id}` with the App's JWT). For a tenant token,
  without printing it: `GH_TOKEN="$(gcloud secrets versions access latest
  --secret=swarm-tenant-<t>-git --project=saga-agents-staging)" gh api
  'repos/<owner>/<repo>/actions/runs?per_page=1' --jq .total_count`. A
  number means `actions: read` is held. `HTTP 403` or `HTTP 404` means it is
  not. The same call to
  `repos/<owner>/<repo>/commits/<default branch>/check-runs?per_page=1`
  answers for `checks: read`. Control: the same calls on a repository the token
  cannot see answer 404.

**Check 2. Update-branch when the base brings `.github/workflows/` changes
(§3.6).** From the repository, not measured live. **No dated measurement of
it exists in the repository.**

* `docs/merge-step.md` line 328 states it as a rule: Workflows: write is
  needed "when the pull request, or the base merged in by `update-branch`,
  changes `.github/workflows/`: GitHub refuses any token without it". No PR,
  log or test is cited for it. The refusal measured on 2026-10-08 (§0) was of
  the worker's **push** of workflow files, which is a different call.
* The onboarding App holds `Workflows: No access` (check 1). So if the rule
  holds, `pr-shepherd`'s update-branch is refused for exactly the pull requests
  whose base changed a workflow.
* **What the code would do with the refusal.** The merge step's
  `_update_branch` (`apps/agent-worker/agent_worker/merge.py`) reads 401, 403
  and 404 as `token_lacks_rights`. It reads **any 422 that does not name a
  conflict as `head_moved`**. If GitHub answers this refusal with a 422, the
  step would misreport it, and `pr-shepherd` must not reuse that
  classification as is. S10's `pr-shepherd` lane matches on the measured
  answer.
* **To measure live** (in a scratch repository with the App installed, not in
  `saga-agents-staging`): open pull request A, then push to its base a commit
  that changes `.github/workflows/x.yml`. Mint an installation token
  (Workflows: none) and call
  `PUT /repos/<o>/<r>/pulls/<A>/update-branch` with `expected_head_sha`.
  Record the status and `message`. **Control:** pull request B, behind a
  base commit that changes only a non-workflow file, called the same way,
  should answer 202. If both answer 202, the rule in merge-step.md is wrong.

**Check 3. SWEEP's merged names (§8.1, PR 898).** Read from `main` at
`ce68220`, which is #898 squash-merged, on 2026-10-08. Not branch
`swarm/task_3804051834a74239a9e8`: SWEEP had already merged. These names are
in the code, so they are measured here:

| §8.1 says | `main` has |
|---|---|
| module `swarm_api/issuesweep.py` | the same |
| `POST /v1/admin/issues/sweep?tenant_id=`, in `ROLLUP_SWEEPER_ROUTES` | the same (`auth.py`, `routes/admin.py::sweep_issues`) |
| `SWEEP_ENABLED`, `var.enable_issue_sweep` | the same (`settings.py` `sweep_enabled`, `terraform/infra/locals.tf`) |
| the tenant document's `issue_sweep.enabled` | `issue_sweep` (`TENANT_FIELD`), a `SweepConfig` of `enabled` (default off), `max_live_runs` (default 8, 1-50), `exclude_issues`, `exclude_labels` and **`submit_as`**, read and written by `GET`/`PUT /v1/admin/tenants/{tenant_id}/issue-sweep` |
| runs `plan_approval: auto`, `auto_merge: true`, `fix_rounds: 2` | the same (`SWEEP_PLAN_APPROVAL`, `SWEEP_AUTO_MERGE`, `SWEEP_FIX_ROUNDS`), with `created_by: issue-sweep` (`SWEEP_CREATOR`) |
| skip labels | `SKIP_LABELS = {epic, blocked, security}`, a constant, plus the tenant's `exclude_labels` |
| the `NOT_READY` verdict and run state | `RunState.NOT_READY` in `issueruns.py`, a terminal state |
| territory guard `routes/runs.py` `territory_conflict` | the same, with `_territory_hold` writing the hold `territory_overlap: <run id>`, and `issueruns.territory_overlap` |
| job `google_cloud_scheduler_job.issue_sweep` | the same: one per tenant in `var.rollup_tenant_ids`, named `<prefix>-issue-sweep-<t>`, at `var.issue_sweep_schedule` (default `7,37 * * * *`), as the rollup sweeper's OIDC identity |

Where the code differs from §8.1, S6 and S9 follow the code (§8.1's rule):

* **The submitter is the tenant's `issue_sweep.submit_as`, not a
  registration's creator.** A tenant whose `submit_as` is unset or is not a
  current member is skipped with that reason (`SweepSubmitterNotMember`,
  code `submit_as_not_member`). S6 maps `submit_as` onto the schedule's
  `owner` (§2.7). The comment above the route in
  `apps/swarm-api/swarm_api/auth.py` still says "each as its registration's
  creator". That comment is stale, and it is an out-of-territory finding for
  the wave epic.
* **The job cannot be paused on its own.** Its `paused` is the module-wide
  `var.paused`, shared by every scheduler job. §8.1 step 3 ("`paused = true`
  in Terraform") therefore needs a per-job variable, or SWEEP's off switches
  instead. S9 names which.
* The cap, the exclusion lists and `submit_as` live on the tenant document,
  not in constants. They become §3.1's parameters as §8.1 step 1 says.

Control: `git grep -c registration_owner_auth -- apps/swarm-api/swarm_api/issuesweep.py`
finds nothing, and the same count for `submit_as` finds 18. So the grep
separates the name §8.1 implied from the one SWEEP uses.

**Check 4. The composite index for the tick query (§2.1, S4).** From the
repository, not measured live.

* The query is `schedules where state == "enabled" and next_run_at <= now
  order by next_run_at`. It has an equality on one field and a range and order
  on another, so Firestore serves it only from a composite index. The index
  is **collection `schedules`, `query_scope = "COLLECTION"`, fields
  `state` ASCENDING then `next_run_at` ASCENDING**. That is the shape S4
  lists.
* It is the same shape as the existing `tasks-state-next-eligible` (`state`,
  `next_eligible_at`) in `terraform/modules/firestore/indexes.tf`, which is
  declared as one entry of `local.indexes`. S4 adds `schedules` there the
  same way.
* `schedules` is a top-level collection (§1.1), not a per-tenant
  subcollection, so `COLLECTION` scope covers every tenant, `u-*` included.
  `COLLECTION_GROUP` would be needed only if that changed.
* Today **no `schedules` index is declared**: `grep -c '"schedules"'
  terraform/modules/firestore/indexes.tf` is 0. Control: the same count for
  `"tasks"` is 14.
* **To measure live** (read-only):
  `gcloud firestore indexes composite list --project=saga-agents-staging --database=swarm --format='table(name,queryScope,fields)' | grep -i schedules`.
  It should print nothing until S4's release. After it, one `COLLECTION`
  index with `state` and `next_run_at` should appear. Control: the same command
  with `grep -i tasks` lists the task indexes, so an empty answer is not a
  wrong database. A query that runs without its index fails with
  `FAILED_PRECONDITION` and the console link to create it. That is the other
  live test, in a dev tick.

**Check 5. The bootstrap file for `swarm-schedule-tick` (SD10, S13).** From
the repository, not measured live.

* **The file is `terraform/bootstrap/deployer_service_accounts.tf`.** It
  grants the release deployer `roles/iam.serviceAccountAdmin` on each account
  in `module.service_account_ids.infra_managed`, minus the CI accounts. It
  restates no account. The list is spelled once, in
  `terraform/modules/service_account_ids/main.tf` (`infra_managed`: the
  `platform` map, `tick_id`, `verify_id`, `rollup_sweeper_id` and the tenant
  workers), and both roots read it.
* So S13 adds `swarm-schedule-tick` as a new id in
  `modules/service_account_ids/main.tf` (and its `outputs.tf`), the way
  `rollup_sweeper_id` was added, and **does not edit the bootstrap file**. The
  owner then applies `terraform/bootstrap` from `main`. The plan's
  `deployer_admin_accounts` output gains exactly
  `swarm-schedule-tick` and its summary is "1 to add, 0 to change,
  0 to destroy" (docs/ci.md, "The deployer's service-account grants").
  `terraform/infra/deployer.tf` `deployer_acts_as` is where the
  `serviceAccountUser` (actAs) grant goes, beside `swarm-rollup-sweeper`.
* **The ordering is S13's to plan.** A grant needs its account to exist
  first. The rollup sweeper's account is created by the release
  (`modules/scheduler/jobs.tf` `google_service_account.rollup_sweeper`,
  without `create_ignore_already_exists`). docs/ci.md's "A new account exists
  before the release that adds it" covers only tenant workers. Its account
  table also omits `swarm-rollup-sweeper`, which is an out-of-territory
  finding.
* Control: `git grep -n rollup_sweeper_id -- terraform/bootstrap` finds
  nothing, and the same id is found in `modules/service_account_ids`. So
  bootstrap really does take its list from the module rather than restating
  it.
* **To measure live:** the owner runs
  `terraform -chdir=terraform/bootstrap plan -var infra_tenants_tfvars=../environments/dev/dev.tfvars`
  from `main` after S13 merges, and reads `deployer_admin_accounts`.

**Check 6. Where onboarding stores a member's verified GitHub login (SD9,
S12).** From the repository, not measured live.

* **`forge_connections/{connection_id}`**, field **`forge_login`**, beside
  **`forge_user_id`** (GitHub's numeric user id), `tenant_id`, `user` (the
  member's email) and `state`. `forgeapp.py` writes them in `_store` from
  `_GitHub.user`, the `GET /user` that it makes with the access token
  that the member's own authorisation code just bought. So the login is one
  GitHub returned for that member's authorisation, as §4.9 requires. The
  `connection_id` is `conn_` and 16 hex digits of tenant, email and forge
  (`connection_id_for`).
* **What S12 must not use instead.** `git_tokens.forge_login`
  (`gittokens.py`) is the login of whatever token was stored, a PAT included,
  so it is not a member's verified login. `issue_runs.forge_login`
  (`issuesync.py`) is a comment's author, so it is not one either.
* **Two cautions for S12.** First, a revoked connection keeps its
  `forge_login` with `state: revoked` (`forgeapp.py` writes only the state,
  `refresh_lease` and `failure`). The mapping must require
  `state == "active"`. Second, a GitHub login can be renamed and reused, but a
  user id cannot. Matching a comment's `user.id` against `forge_user_id`, and
  falling back to the case-insensitive login only where the id is null, is
  stricter than §4.9's login match. S12 asks if it wants to change §4.9's
  rule. No new field is needed, so S12's "stop and ask" condition does not
  fire.
* Control: `git grep -n '"forge_login"' -- apps/swarm-api/swarm_api/forgeapp.py`
  finds 4 lines, two of them writes in `_store`. The same grep over
  `onboarding.py` finds only a read of a token record. So the writer is
  `forgeapp.py` and not the onboarding checklist.
* **To measure live** (read-only, in a dev session with Firestore read):
  read one active connection document in database `swarm`, collection
  `forge_connections`, and confirm that `forge_login` and `forge_user_id`
  are set. Print only those two fields and `state`, never `token_id` or
  `secret_name`.

---

## 1. The model

### 1.1 `schedules/{schedule_id}`

The collection is new, kept by its own module (`swarm_api/schedules.py`) and
read and written there only. It does not go through `store.py` or `codec.py`,
for the reason `issue_runs` gives: its shape is not the frozen contract's and
must not leak into it.

| field | type | written by | meaning |
|---|---|---|---|
| `schedule_id` | string | swarm-api | `sch_` and 12 hex digits from `secrets.token_hex(6)`. Random, so an id says nothing about its tenant. It equals the document id |
| `tenant_id` | string | swarm-api | The owner tenant, resolved from the caller exactly as a task's is. **Never a body field.** Every read compares it with the caller's tenant and answers a mismatch with the same 404 as a missing schedule, as `issue_runs` does |
| `name` | string | caller | 1-80 characters, unique within the tenant. It is what the console lists |
| `type` | string | caller | A name from the catalogue (§3), e.g. `issue-sweep`. **By name, never an image, command or profile** (invariant 10). An unknown or unavailable type is a 422 that names the available ones |
| `scope` | map | caller | `{mode: "repos" \| "all", repo_ids: [...]}`. `repos` lists 1-25 registrations of this tenant. `all` means every registration the tenant has **at firing time**. Platform-scope types (§3.13) take `{mode: "platform"}` and are admin-only |
| `cron` | string | caller | Five fields (§2.3), validated, with a per-type minimum interval |
| `timezone` | string | caller | An IANA name (`Europe/London`), default `UTC`. Validated against `zoneinfo` |
| `params` | map | caller | The type's parameters (§3), validated by that type's own Pydantic model. **Extra keys are refused by name**, as `PlanSpec` refuses them |
| `gate` | map | caller, within the type's floor | `{plan, run, merge, approvers, approval_ttl_hours}` (§4.2). It is stored resolved, with defaults filled in, so a later default change does not loosen an existing schedule. `merge: auto` is set only through the audited switch of §4.2 |
| `budget` | map | caller, within platform caps | `{per_run_usd, per_day_usd, max_concurrent}` (§4.3, SD4) |
| `policy` | map | caller | `{overlap: skip \| queue_one, catch_up: skip \| run_once, jitter: bool, dry_run: bool}`. Defaults come from the type (§2.4-§2.6); `catch_up` is overridable per schedule (SD6) |
| `state` | string | swarm-api | `enabled`, `paused`, `auto_paused` or `disabled` (§1.3) |
| `pause` | map or null | swarm-api | `{by, at, reason, code}`. `code` is set for an auto-pause (§4.3) |
| `owner` | string | swarm-api | The member whose identity firings submit as (§2.7). It starts as the creator and changes only by an explicit "take ownership", which is audited |
| `created_by`, `created_at`, `updated_by`, `updated_at` | string, timestamp | swarm-api | Taken from the verified token, never from the body |
| `next_run_at` | timestamp | swarm-api | The next slot in UTC, **after jitter**. It is null while not `enabled`. The tick's query reads it (§2.1) |
| `next_slot` | timestamp | swarm-api | The same slot before jitter. It is the firing's idempotency key (§2.2) |
| `last_firing` | map or null | swarm-api | `{firing_id, slot, outcome, work_ref, ended_at}`, a copy for the list screen |
| `consecutive_failures` | int | swarm-api | Reset by a success, and drives the auto-pause (§4.3) |
| `spend` | map | swarm-api | `{day, reported_usd, unreported_attempts, reserved_usd}` for the current day in `timezone` (§4.3) |
| `revision` | int | swarm-api | Incremented by every edit. An edit carries the revision it read, and a stale one is a 409 `schedule_changed`, the `plan_changed` pattern |

**Limits that bound one tenant (§5.6):** at most 25 schedules per tenant
(`SCHEDULES_PER_TENANT`, an admin may raise it per tenant), and each type has a
minimum interval (§3).

### 1.2 `schedule_firings/{schedule_id}:{slot}`

There is one document per due slot. `{slot}` is the slot's Unix minute,
**before jitter**. Its **creation is the claim**: the tick creates it inside the
transaction that advances `next_run_at` (§2.2), and a create that finds the
document already there is the duplicate that never happens twice.

| field | meaning |
|---|---|
| `schedule_id`, `tenant_id`, `type`, `slot`, `fired_at` | identity and when the tick claimed it |
| `trigger` | `cron`, `catch_up`, `run_now` or `queued`. A `run_now` firing is keyed `run_now:{uuid}` rather than by slot |
| `state` | §1.4 |
| `params_digest` | sha256 of the resolved `{type, scope (resolved repo list), params, gate, budget}` the firing ran with. A run approval approves this digest (§4.5) |
| `work` | `[{kind: issue_run \| task \| workflow \| api_action, id, repo_id}]`, what it created. A sweep creates several |
| `skip` | `{code, detail}` when nothing was created: `OVERLAP`, `BUDGET_EXHAUSTED`, `MISSED_SLOT`, `OWNER_NOT_MEMBER`, `REPOSITORY_NOT_GRANTED`, `WORKSPACE_NOT_READY`, `NO_CLAUDE_ACCOUNT`, `SCHEDULES_DISABLED` or `DRY_RUN` |
| `dry_run` | what a dry run would have created, as data (§2.8) |
| `approval_id` | set while a run approval is pending |
| `outcome` | `succeeded`, `failed`, `cancelled`, `refused`, `skipped`, `expired`, `rejected` or `partial`, derived from its work (§2.9) |
| `cost` | `{reported_usd, unreported_attempts}`, summed over the work's attempts (§4.3) |
| `history` | each move, with time and actor |

Firings are kept for 90 days, then deleted by a Firestore TTL policy on
`expire_at`. The schedule's audit (§4.8) keeps the decisions for longer.

### 1.3 Schedule states

| state | entered when | the tick | the person sees |
|---|---|---|---|
| `enabled` | created (unless created paused), resumed | fires due slots | the next run in words, e.g. "Mon 09:00 London" |
| `paused` | a member or admin paused it | reads nothing | "Paused by {who} {when}: {reason}" and Resume |
| `auto_paused` | a hard stop of §4.3 | reads nothing | the code's copy, e.g. "Paused after 3 failed runs in a row", and Resume, which is audited |
| `disabled` | an admin disabled it (§5.3), or its type was withdrawn from the catalogue | reads nothing | "Disabled by an admin". Only an admin may re-enable it |

Deleting a schedule is a hard delete of the document. It needs a typed
confirmation in the console. Its firings stay until their TTL, and its audit
stays. Work that a schedule already created is **not** cancelled by a pause or
a delete: it is ordinary work, cancelled the ordinary way. The console offers
"Pause and cancel live runs" as a separate, typed action.

### 1.4 Firing states

```
claimed ─► awaiting_approval ─► (approved) ─► created ─► done (outcome)
   │              │                               ▲
   │              ├─► expired / rejected          │
   │              ▼                               │
   ├─► skipped (code)                             │
   ├─► queued (overlap: queue_one) ───────────────┘
   └─► refused (a submission refusal: WORKSPACE_NOT_READY, …)
```

`awaiting_approval` and `queued` are Firestore documents and nothing else. No
task, lease or pending pod exists for them (invariant 1). The work is created
only at `created`.

### 1.5 Frozen contract

**None is needed.** The pieces are:

* `schedules/`, `schedule_firings/`, `approvals/` and `schedule_audit/` are new
  swarm-api collections.
* The link from work to its firing is a new **swarm-api reserved metadata
  key**, `schedule`, added to
  `apps/swarm-api/swarm_api/validation.py::RESERVED_METADATA_KEYS`. A caller
  cannot forge it. It sits on the task, the workflow and the issue run, so
  every surface can say "made by schedule *nightly sweep*".
* `submitted_by` stays the verified owner's email, as the frozen `Task`
  defines it.

  The owner asked for `created_by: schedule:<id>`. It is served by the API
  from `metadata.schedule`, as a derived `created_by` field on reads, and not
  stored in `submitted_by`. Two reasons. `submitted_by` is "verified email from
  the ID token" in `apps/common/swarm_common/models.py`. And repository grants,
  commit identity and membership checks all read it as a person.
* Every type compiles to work the platform already runs:
  - an issue run;
  - a `claude-code` or `indexer` task;
  - a workflow of `claude-code` steps;
  - a swarm-api API action.

  None needs a new `TaskState`, `ParkReason` or profile.

One **optional** request is written in §12. It is a declared input that would
let `observer` and `cost-report` hand an agent a data file rather than putting
the data in the prompt. The design works without it (§3.4).

---

## 2. Execution

### 2.1 One tick, not one GCP job per schedule

`google_cloud_scheduler_job.schedule_tick` runs every minute and calls
`POST /v1/admin/schedules/tick` with **no tenant parameter**, as its own
service account, **`swarm-schedule-tick`** (**Owner decision 2026-10-08,
SD10**: a dedicated account, not the rollup sweeper). swarm-api admits that
identity to this one route, through a new `SCHEDULE_TICK_ROUTES` set beside
`ROLLUP_SWEEPER_ROUTES` and a new `SCHEDULE_TICK_USERS` setting (rendered
like `ROLLUP_SWEEPER_USERS`). Creating the account is an owner bootstrap
apply, and the release that grants it `run.invoker` on swarm-api waits at
`dev-iam` (§9, "Owner applies").

Why one tick:

* **A per-schedule GCP job would make every edit an infrastructure change.**
  It would mean Cloud Scheduler admin rights for swarm-api, or a Terraform
  apply per edit. It would raise the question of which identity each job
  carries. And it would put the list of tenants' schedules in GCP, outside the
  tenant check every read makes. With one tick, a schedule is data, and the
  tenant check is the one `issue_runs` already makes.
* **A per-tenant tick (the `issue_run_advance` pattern) misses `u-*`
  tenants,** which the owner wants included. A single query over
  `schedules where state == enabled and next_run_at <= now order by
  next_run_at` covers every tenant, personal ones included, with one composite
  index (§9, S4).
* **The identity is its own, so the sweeper's is not widened.** The rollup
  sweeper is admitted to per-tenant routes only
  (`test_rollup_sweeper_is_narrow.py` holds that set). This route reads due
  schedules across tenants, which is a different reach, so it gets a different
  account that is admitted to this one route and to nothing else. It still
  **submits nothing as itself**: every firing is submitted as the schedule's
  owner in the schedule's tenant (§2.7). The cost is one owner bootstrap apply
  and one `dev-iam` release, which the owner accepted in SD10.

The job's `retry_count` is 0. The next minute's tick is the retry, and every
claim is a transaction, so an overlapping retry repeats reads and never a
firing.

### 2.2 Exactly once per slot

For each due schedule, one Firestore transaction does the following:

1. **Re-read the schedule.** If it is not `enabled`, or `next_run_at` is in the
   future, stop. Another tick won.
2. **Compute the slot.** The slot is `next_slot`, the stored un-jittered time.
   Then compute the newest slot that is `<= now` (§2.4, catch-up).
3. **Create `schedule_firings/{id}:{slot}`.** Firestore's `create` fails if the
   document exists, and the transaction aborts with nothing written. A retried
   tick, a second tick and an overlapping run-now all meet the same document.
4. **Advance** `next_slot` and `next_run_at` to the first slot after `now`, and
   write `last_firing` as claimed.

Creating the work happens **after** the transaction, from the firing document
(§2.7). If that step fails (a 5xx, or a timeout of the route), the firing stays
`claimed`. The next tick finds claimed firings older than 2 minutes and
finishes them. Before creating anything, a finisher looks for work already
carrying `metadata.schedule.firing_id` equal to this firing's id (tasks,
workflows and issue runs, §2.7), and adopts what it finds. So a step that
created the work and then failed to record it cannot create it twice. The
submission paths have no idempotency key of their own today (child tasks have
`child_request_id`; top-level tasks and workflows have none), which is why the
lookup is by the firing's own mark.

### 2.3 Cron expressions, timezone and the clock

* **Five fields:** minute, hour, day of month, month and day of week. Allowed:
  lists, ranges, steps, `JAN`-`DEC` and `MON`-`SUN`, and the aliases
  `@hourly`, `@daily`, `@weekly` and `@monthly`.

  Refused: seconds, `@reboot`, and Quartz's `L`, `W`, `#` and `?`. The parser
  is **swarm-api's own**, about 150 lines over `zoneinfo`, not a dependency.
  The grammar is small. One implementation serves the tick, the validation,
  the console's "next five firings" and the words. A second parser in
  TypeScript would drift from it, so the console asks the API
  (`POST /v1/schedules:preview`, §7.1).
* **Day of month and day of week combine as Vixie cron does:** when both are
  restricted, either one matching fires. The words say "or", so
  `0 9 1 * MON` reads "09:00 on the 1st, or on Mondays".
* **Each type has a minimum interval** (§3). It is validated by computing the
  smallest gap between the next 50 firings, so `*/5 * * * *` is refused for a
  type whose minimum is 15 minutes, whatever the spelling.
* **Daylight saving time.** A local time that does not exist (spring forward)
  fires once, at the first instant after the gap. A local time that happens
  twice (fall back) fires once, at its first occurrence. The slot is the UTC
  instant, so the key cannot collide.
* **The clock.** The tick compares `next_run_at` with swarm-api's clock. That
  is Cloud Run's, synchronised by Google. **A late tick does not move a slot:**
  the slot comes from the stored cron and `next_slot`, and the tick time only
  decides whether a slot is due. A tick a minute late fires the 09:00 slot at
  09:01 with `slot = 09:00`. Lateness is recorded as `fired_at - slot`, and
  the admin view shows its 95th percentile.

### 2.4 Catch-up after an outage

A slot is **missed** when `now - slot` exceeds its grace. The grace is the
smaller of 15 minutes and half the schedule's interval. Missed slots are never
all run.

| policy | after an outage that missed several slots |
|---|---|
| `skip` | Each missed slot gets a `skipped / MISSED_SLOT` firing, so the history shows the gap. Then the schedule waits for its next slot |
| `run_once` | The same records, except that the **newest** missed slot fires once, with `trigger: catch_up` |

**Owner decision 2026-10-08 (SD6): the default is per type, and a schedule
may override it** (`policy.catch_up`, §1.1). `run_once` for reports
(`observer`, `cost-report`, `release-health`, `docs-drift`) and
`repo-index-refresh`, and `skip` for everything that acts on repositories. Sweeping issues twice after
an outage would be harmless, because the sweep is idempotent per issue. But a
person reading "it ran at 14:07" after a 09:00 schedule should be told why, so
an acting type does not catch up unless asked to.

Writing a skip record per missed slot is bounded. A schedule paused or broken
for a month writes at most 50 records. Beyond that it writes one
`MISSED_SLOT` record whose detail gives the count.

### 2.5 Overlap

A firing is **live** while any of its work is in a non-terminal state.
For an issue run, `PLANNED` counts as live: the plan is waiting for a person,
and a second sweep would plan the same issues again.

| policy | a slot comes due while the previous firing is live |
|---|---|
| `skip` (default) | `skipped / OVERLAP`, naming the live firing |
| `queue_one` | one `queued` firing. It is created as the live one ends, by the tick that sees it end. A second overlap while one is queued is `skipped / OVERLAP`. **Never more than one queued** |

`max_concurrent` (§4.3) is a separate bound. It counts work items, such as the
sweep's live issue runs, not firings.

### 2.6 Jitter

When `policy.jitter` is on (the default), `next_run_at = next_slot +
offset`. The offset is `sha256(schedule_id) mod J` seconds, and `J` is the
smaller of 300 seconds and 10% of the interval.

* It is **deterministic**: a schedule fires at the same offset every time, and
  a person sees it ("09:03, spread to avoid a stampede").
* It **spreads** the 25 schedules a tenant writes as `0 9 * * 1-5` over five
  minutes rather than one tick.
* The firing key stays the un-jittered `slot`, so jitter cannot create or
  merge firings.

### 2.7 What a firing creates, and as whom

The firing creates work through the **same service methods a person's request
uses**:

* `apps/swarm-api/swarm_api/service.py::SubmissionService.submit_tasks`;
* `SubmissionService.submit_workflow`;
* the issue-run creation that `POST /v1/runs` makes.

So every validation, refusal, grant check, signature and admission rule applies
unchanged.

**As whom.** `schedule_owner_auth(ctx, schedule)` is a third copy of the
pattern in `run_owner_auth` and `registration_owner_auth`. The S2 lane folds
the three into one helper in its own new file, and does not change the other
two:

* the principal is the stored `owner`, in the stored `tenant_id`;
* `is_admin`, `member_scope` and `tenant_member` are an ordinary member's empty
  values;
* membership is asked of the directory **at every firing**
  (`Authenticator.is_tenant_member`).

An owner who has left the tenant makes the firing `skipped /
OWNER_NOT_MEMBER`, and the schedule moves to `auto_paused` with that code. Any
current member may "take ownership", which is audited. A directory lookup that
fails leaves the firing `claimed` for the next tick.

**Marked.** The work carries `metadata.schedule = {schedule_id, firing_id,
slot, type}`. An issue run carries the same map on its own document. Reads
derive `created_by: "schedule:<schedule_id>"` from it.

**Admitted like anything else.** A task the firing creates is `QUEUED` or
`READY` and costs nothing until admission takes a lease (invariants 1-3). A
firing never reserves capacity, never waits for it, and does not know whether
the tenant's pool is full. A full pool is the ordinary queue.

**Types with no agent (Owner decision 2026-10-08, SD8).** `cost-report`,
`release-health` and `pr-shepherd`'s API actions run **inside swarm-api** as
a firing whose `work` entry has `kind: api_action`. They create no task,
use no capacity and need no worker; the firing record is their history. A
deterministic API call does not need a model, and a task that made the same
call would give an agent a write it does not need.

**Personal workspaces.** The submission gates of
[workspaces.md §5](workspaces.md#5-the-submission-gate) apply as they do to a
person: `WORKSPACE_NOT_READY` or `NO_CLAUDE_ACCOUNT` makes the firing
`refused` with that code. Three refusals in a row auto-pause the schedule
(§4.3).

### 2.8 Dry run

With `policy.dry_run`, or `POST …:run` with `{"dry_run": true}`, the firing
computes what it would create and stores it in `dry_run`, then ends `skipped /
DRY_RUN`. Nothing is submitted and nothing is written to GitHub.

* For `issue-sweep`, the output is the candidate issues and why each was taken
  or passed over.
* For `pr-shepherd`, it is the label, update-branch and comment actions.
* For the agent types, it is the compiled prompt's size and the resolved
  repository list. **The agent itself never runs dry.** A dry run that spends
  tokens is not dry.

### 2.9 Outcome, and advancing what a firing made

A firing's outcome is **derived** from its work, the way a workflow's state is
derived from its steps:

* all succeeded: `succeeded`;
* any failed: `failed`, or `partial` when the type does several independent
  things (a sweep that started five runs, of which one failed);
* a run that ended `NOT_READY` (§3.1) counts as succeeded, because "this issue
  is not ready" is an answer.

The tick advances live firings: up to one page, oldest first, after it has
fired due slots. For an issue run it calls
`apps/swarm-api/swarm_api/routes/runs.py::advance_run`. **That closes, for
scheduled runs, the gap of §0:** an `auto` issue run in a `u-*` tenant moves
even when nobody reads it, because the schedule tick reaches every tenant.

### 2.10 Tick bounds and fairness

* It reads at most 200 due schedules per tick, ordered by `next_run_at`.
* It fires at most **5 per tenant per tick**, round-robin by tenant. The rest
  stay due for the next minute. So one tenant's 25 schedules due at 09:00
  cannot delay another tenant's one schedule by more than a tick.
* It stops starting new work at 240 seconds, `repoindex.POLL_BUDGET_SECONDS`'s
  value and reason. It reports `{fired, skipped, advanced, truncated}`, and
  writes the same report, with the tick's start time and each firing's
  lateness (`fired_at − slot`), to `schedule_ticks/{unix_minute}`. Those
  documents expire after 7 days by a TTL policy on `expire_at`, and the admin
  view's tick health (§5.3) reads them.
* A schedule whose firing raised an error is counted and the rest of the page
  goes on, as the issue-run tick does.

### 2.11 Kill switches

| switch | who | effect |
|---|---|---|
| Pause a schedule | any member of its tenant, or an admin | no new firings. Live work continues. `POST /v1/schedules/{id}:pause`, or `/v1/admin/schedules/{id}:pause` (§7.1) |
| Pause and cancel live runs | any member, typed confirmation (the schedule's name) | the above, plus an ordinary cancel of the firing's live work. `POST /v1/schedules/{id}:pause` with `{cancel_live: true, confirm}` (§7.1) |
| Pause all of a tenant's schedules | any member, typed (the tenant id); an admin from §5.3, typed the same | every `enabled` schedule of the tenant becomes `paused` with one reason, audited per schedule. `POST /v1/schedules:pause-all`, or `/v1/admin/tenants/{tenant_id}/schedules:pause` (§7.1) |
| `SCHEDULES_ENABLED=false` | the owner, through `api_settings` | the tick returns `{disabled: true}` immediately. Each due slot is recorded as `skipped / SCHEDULES_DISABLED` when the switch is turned back on, under the catch-up policy |
| Cloud Scheduler `paused` | Terraform (`var.paused`) | the tick is not called at all. The same catch-up applies on resume |

---

## 3. The job-type catalogue

A caller picks a type **by name**. The catalogue is code
(`swarm_api/scheduletypes.py`), in the shape of the frozen `RUNNER_PROFILES`
but not part of it. Each entry declares:

* name and description;
* executor (`issue_runs`, `task`, `workflow` or `api`);
* the Pydantic parameter model;
* the minimum interval;
* the default and floor gates;
* the default budget and catch-up;
* the risk tier (§4.1) and whether it pushes (§5.4);
* who may create it (`member`, `admin` or `owner`);
* `available` and a `disabled_reason`, as profiles have.

No type accepts an image, a command, a profile, a resource class or a backend
parameter. A type that runs an agent names its profile in code. Invariant 10
holds for schedules exactly as for tasks.

| # | type | executor | risk | pushes | default gate | floor | creates | min interval | phase |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `issue-sweep` | issue runs | R3 with `merge: auto`, else R2 | yes | plan auto · **approve merge** | plan auto · merge auto, reached only through the audited switch (§4.2, SD3); in a `platform: true` repository only a platform admin may make the switch (Owner decision 2026-10-08) | members | 15 min | 1 |
| 2 | `issue-plan-only` | issue runs | R1 | no, until approved | **approve plan** | **approve plan** | members | 15 min | 1 |
| 3 | `repo-index-refresh` | `indexer` tasks | R0 | no | auto | auto | members | 1 h | 1 |
| 4 | `observer` | one `claude-code` task | R0 | no | auto | auto | members (tenant), owner (platform) | 1 h | 1 |
| 5 | `epic-triage` | `claude-code` task, then API | R1 | no (comments) | **approve plan** (the ticks) | **approve plan** | members | 6 h | 2 |
| 6 | `pr-shepherd` | API, then continuations | R1 for API actions, R2 for conflict fixes | yes (fixes) | API auto · **approve run** for fixes | auto, fixes included (an admin only) | members | 30 min | 2 |
| 7 | `ci-flake-hunter` | API, then `claude-code` tasks | R1 | no (issues) | auto | auto | members | 6 h | 2 |
| 8 | `dependency-cve-refresh` | workflow | R2 | yes | **approve merge** | **approve merge** | members | 24 h | 2 |
| 9 | `release-health` | API | R1 | no (an issue) | auto | auto | members; owner for the platform's own repository | 15 min | 2 |
| 10 | `docs-drift` | `claude-code` task | R1 report, R2 with `fix: true` | with `fix: true` | auto report · **approve merge** | auto report · **approve merge** with `fix` | members | 24 h | 2 |
| 11 | `cost-report` | API | R0 | no | auto | auto | members (tenant), admin (platform) | 24 h | 2 |
| 12 | `custom-prompt` | workflow from a saved spec | R3 | yes | **approve run + approve merge**; never auto | **approve run + approve merge** | members, with a one-time admin approval of the spec | 6 h | 3 |

The **floor** column is the lowest gate the type allows. Between the floor and
the default, only a platform admin may set the gate (§4.2), with one
exception: the merge switch of SD3, which in a repository that is not
`platform: true` any member may use under the two-person rule of §4.6, and in a
`platform: true` repository only a platform admin may (Owner decision 2026-10-08). A floor of "auto"
still leaves every hard stop of §4.4 in force.

**Platform-wide (admin) types** run with `scope: {mode: "platform"}`: the
platform variants of `observer` and `cost-report`, and `release-health` on the
platform's own repository. They are created by an admin (§3.13). **Owner-only**
are the `observer` platform variant, which reads every tenant's aggregates, and
any schedule whose resolved gate allows an **unattended merge** (`merge:
auto`) in the platform's own repository. Both put the platform's own code or
every tenant's data within one approval's reach.

### 3.1 `issue-sweep`

* **Does.** It reads the open issues of each repository in scope, with the
  tenant's forge token (`forge.read_open_work`).
  - It drops issues that match `labels_exclude`, issues with a live issue run,
    and issues answered `NOT_READY` within `cooldown_hours` whose body and
    labels have not changed since.
  - It creates an issue run for each of up to `max_new_per_firing` remaining
    issues, oldest first, subject to `max_live_runs`.
  - The planner decides readiness. Lane SWEEP's `NOT_READY` verdict ends the
    run with no workflow and a comment that says what is missing.
  - The **territory guard** holds an approved plan whose `files` overlap a live
    run's plan until that run ends. It is lane SWEEP's guard, kept as written.
    [lane-queue.md §4](lane-queue.md) designs the general lock this guard
    would later become.
* **Parameters.**

  | parameter | default | range |
  |---|---|---|
  | `labels_include` | `[]` (any) | — |
  | `labels_exclude` | `["security", "needs-owner", "wontfix", "question"]` | — |
  | `max_live_runs` | 8 | 1-8 |
  | `max_new_per_firing` | 3 | 1-8 |
  | `cooldown_hours` | 72 | 1-720 |
  | `plan_approval` | `auto` | `auto`, `required` |
  | `merge` | `approve` | `off`, `approve`, `auto` (`auto` only through the audited switch, §4.2) |
  | `fix_rounds` | 3 | 1-5 |
  | `territory_guard` | true | **not settable to false** by a member |

  The cap of 8 is lane SWEEP's, and it is the upper bound here: one firing can
  hold at most 8 tenant slots of work at once.
* **Produces.** Issue runs, each linked from the firing. Pull requests, opened
  by those runs. Merges, when the merge gate allows them.
* **Blast radius.** Code changes on branches. Merges into the default branch
  only through the run's existing merge-only continuation, at the reviewed and
  green sha, after the merge gate. A security-class issue is never swept
  (§4.4). Bounded by `max_live_runs`, the tenant's ceilings and the budget.

### 3.2 `issue-plan-only`

* **Does.** The same selection as `issue-sweep`, with `plan_approval:
  required` forced. Plans arrive in the inbox, and approving one is the
  issue run's existing approve. Nothing executes until a person approves.
* **Parameters.** The selection parameters of §3.1, `max_pending_plans`
  (default 5, range 1-20: a new plan is not made while that many are waiting),
  and `fix_rounds`.
* **Default gate.** Approve plan. It is the floor, so it cannot be lowered.
* **Produces.** `PLANNED` issue runs and plan comments on the issues.
* **Blast radius.** One planner task per issue, and plan comments on GitHub.
  No code changes without a person.

### 3.3 `repo-index-refresh`

* **Does.** For each repository in scope, it queues an index run through
  `RepoIndex`'s existing queueing, with that path's in-flight rule: a run in
  flight is not duplicated.
* **Parameters.** `kind` (`incremental` or `full`, default `full`) and
  `only_if_behind` (default false).
* **Alongside the built-in index.** A repository may have a
  `repo-index-refresh` schedule **even while it re-indexes on change**
  (owner's note on SD7, 2026-10-08). The schedule is for timed full
  refreshes; the change trigger keeps running, and the in-flight rule below
  stops the two from duplicating a run.
* **Default gate.** Auto. Floor: auto.
* **Produces.** `indexer` tasks. Their output is the index under the tenant's
  prefix, `repos/<repo_id>/`.
* **Blast radius.** It reads repositories and writes only the tenant's own
  index objects. §8.3 decides how it relates to the existing `index.*`
  settings.

### 3.4 `observer`

* **Does.** It is a read-only self-improvement report over the last
  `window_hours` of the tenant's work. swarm-api computes a **digest** from
  data it already holds. A `claude-code` task with **no repository** gets the
  digest as data, between delimiter lines carrying the firing id: the issue
  run's open-work pattern, bounded to 48 KiB (`MAX_PLANNER_PROMPT_BYTES` is 64
  KiB). The task writes `report.md` and `proposals.json`. The digest holds:
  - cost per run and per profile, with spend coverage;
  - the median and 95th-percentile start latency (`LEASED` to `RUNNING`);
  - the share of pull requests red on their first CI run;
  - fix steps that ran and changed nothing;
  - pull-request titles that state no fact, or carry a closing keyword the
    review did not confirm;
  - parks by reason;
  - refusals by code.
* **Parameters.**

  | parameter | default | range |
  |---|---|---|
  | `window_hours` | 24 | 1-168 |
  | `focus` | all | a subset of `cost`, `latency`, `ci`, `idle_fixes`, `titles`, `parks`, `refusals` |
  | `file_issues` | `false` | — |

* **Default gate.** Auto. Each proposal lands in the inbox as a **proposal**
  (§4.5). Turning one into a GitHub issue is a click by a person, unless
  `file_issues` is true, in which case they are filed as one epic per the
  CLAUDE.md "Issues" rules: one comment per finding.
* **Produces.** `report.md` (an artifact, shown on the firing), proposals, and
  optionally an epic.
* **Blast radius.** None on code. It reads the tenant's own records.
* **Platform variant (owner-only).** It reads aggregates across tenants: counts,
  durations, costs and codes, **never a prompt, an output or a title from
  another tenant**. It runs in the tenant the owner names when creating it
  (§5.3).

### 3.5 `epic-triage`

* **Does.** For each open issue labelled `epic` in scope, a `claude-code` task
  reads the epic's comments and the repository's default branch. It writes
  `ticks.json`: for each unticked box, the evidence (a merged PR or a commit,
  with the file and the call site) or "still open". swarm-api validates each
  proposed tick. **One without a PR or commit reference is refused**, because
  CLAUDE.md says to tick only with evidence. The rest become one **plan
  approval**: the list of ticks with their evidence. When approved, swarm-api,
  not the agent, edits the comments with the tenant's token.
* **Parameters.** `epic_label` (`epic`) and `max_epics` (5, range 1-20).
* **Default gate.** Approve plan. Floor: approve plan.
* **Produces.** Tick proposals, and after approval, edited comments with the
  evidence appended.
* **Blast radius.** Checkbox edits in the epics' comments. Reversible. It never
  closes an epic: an epic closes on its comments, by a person.

### 3.6 `pr-shepherd`

* **Does.** For each open, non-draft pull request in scope with no activity for
  `stale_hours`:
  - It applies the `labels` policy, e.g. adds `stale` or removes `ready` when
    CI is red at the head.
  - It calls GitHub's update-branch when the pull request is behind and
    mergeable.
  - For a **conflict**: if the pull request was opened by a SwarmCloud issue
    run, it submits a conflict-fix continuation of the run's integrator. That
    is the CI fix round's mechanism, the only way the worker can push to an
    existing pull request's branch today. Otherwise it posts one comment
    naming the conflicting files.
* **Parameters.** `stale_hours` (48), `labels` (a small map of conditions to
  add/remove), `update_branch` (true), `fix_conflicts` (false), and
  `max_prs_per_firing` (10).
* **Default gate.** Auto for the API actions. **Approve run** for conflict
  fixes, which push code.
* **Produces.** Labels, merged-base updates, comments and continuation tasks.
* **Blast radius.** A pull request's labels and branch. Update-branch makes a
  merge commit on the PR's branch, and GitHub may refuse it when the merge
  brings in `.github/workflows/` changes the credential cannot push (S0
  verifies this). A refusal is recorded and never retried in the same firing.

### 3.7 `ci-flake-hunter`

* **Does.** It reads the default branch's check runs over `window_days`. A
  **flake** is a check that failed and then passed at the **same sha**. For the
  top `max_flakes` by count, it files or updates one issue per flaky test
  (title as a fact, label `bug`), optionally with a `claude-code` diagnosis
  task whose `diagnosis.md` is attached.
* **Parameters.** `window_days` (7), `min_occurrences` (2), `max_flakes` (3)
  and `diagnose` (false).
* **Default gate.** Auto. Issues are reversible and say who filed them.
* **Produces.** Issues, and diagnosis artifacts.
* **Blast radius.** Issues only. Reading check runs needs `checks: read`, and
  reading workflow runs needs `actions: read`. S0 verifies which the App
  holds; if neither, the type stays `available: false`.

### 3.8 `dependency-cve-refresh`

* **Does.** It reads the latest vulnerability scan for the repository: the
  image scan of `security.yml`, read as a check-run summary. If fixable
  packages are listed, it submits a workflow whose one step bumps the base
  images' package pins (the libtiff fix of 2026-10-08 is the model) and opens
  a pull request through the ordinary review and integrate shape.
* **Parameters.** `paths` (default `["images/"]`), `severity_at_least`
  (`high`) and `max_packages` (10).
* **Default gate.** Approve merge. Floor: approve merge.
* **Produces.** One pull request per firing at most.
* **Blast radius.** Image definitions. The release builds and deploys them, so
  a bad bump reaches the platform at the next release, which is why the merge
  gate cannot be lowered.

### 3.9 `release-health`

* **Does.** It reads the latest runs of a named workflow (default
  `release.yml`) on the default branch. On a **failure** it opens one issue or
  updates the open one: title as a fact, the failing job and step, a log
  excerpt passed through `redact`, label `bug`. On the first success after a
  failure it comments "green again at {sha}". **It never closes the issue**;
  a person does.
* **Parameters.** `workflow` (`release.yml`), `branch` (the default branch) and
  `issue_label` (`bug`).
* **Default gate.** Auto.
* **Produces.** One issue per failure streak.
* **Blast radius.** One issue. Needs `actions: read` (S0).

### 3.10 `docs-drift`

* **Does.** A `claude-code` task checks, in the repository's `docs/`:
  - line citations against the files they name;
  - `path::symbol` citations against the code;
  - stated defaults against the values in code.

  It writes `drift.md`. With `fix: true` it also edits the docs (only paths
  under `paths`) and opens a pull request.
* **Parameters.** `paths` (`["docs/"]`) and `fix` (false).
* **Default gate.** Auto for the report. Approve merge with `fix`.
* **Produces.** A report, and optionally a docs-only pull request.
* **Blast radius.** Documentation. A diff outside `paths` is a hard stop at the
  merge gate (§4.4).

### 3.11 `cost-report`

* **Does.** It sums `cost_usd` over the window's attempts by schedule,
  repository, profile and person, **with coverage**: rows without a reported
  cost are counted and the total is marked a lower bound, the attempts list's
  rule. It stores the report on the firing and, optionally, posts it as a
  comment on a named issue.
* **Parameters.** `window` (`day` or `week`) and `post_to_issue` (optional
  `owner/repo#N`).
* **Default gate.** Auto.
* **Produces.** A stored report.
* **Blast radius.** None, or one comment.
* **Platform variant (admin).** It covers all tenants, and is visible to
  admins only.

### 3.12 `custom-prompt`

* **Does.** It runs a saved workflow spec, signed as every workflow spec is.
  The spec is stored on the schedule **with its digest**.
* **Parameters.** `spec` (a workflow spec, validated by the same
  `WorkflowCreate` model as `POST /v1/workflows`, so any profile it names is
  named, never an image) and `max_steps` (5).
* **Gate.**
  - **Approve run** and **approve merge** are the floor, and members cannot
    lower them.
  - **Activation:** the first time a spec digest is enabled, a platform admin
    approves the digest in the inbox, and editing the spec needs that approval
    again. A spec is a standing instruction to an agent with push rights, which
    the catalogue's other types are not.
  - `merge: auto` is never allowed.
* **Produces.** Whatever the spec's workflow produces.
* **Blast radius.** Bounded by the spec's repositories, the gate and the
  budget.

### 3.13 Platform-scope types

A platform-scope schedule is created by an admin and **owned by a tenant the
admin names**. That tenant's identity, ceilings and spend are the ones used.
The platform does not run work outside a tenant. The owner names a tenant for
platform automation when the first one is created (SD7).

What such a schedule reads across tenants is **aggregates built by swarm-api**
(counts, durations, costs, codes), never a tenant's prompts, outputs, titles or
repository contents. So no tenant's data enters another tenant's worker
(invariant 9).

---

## 4. Gates and approvals

### 4.1 Risk tiers

| tier | what the work may change | examples |
|---|---|---|
| R0 | SwarmCloud's own records and the tenant's own objects | `repo-index-refresh`, `observer`, `cost-report` |
| R1 | GitHub issues, comments and labels. No code | `issue-plan-only`, `epic-triage`, `release-health`, `ci-flake-hunter` |
| R2 | Code on branches, through pull requests. No merge | `pr-shepherd` fixes, `dependency-cve-refresh`, `docs-drift` with `fix` |
| R3 | A merge into the default branch, or a standing free-form instruction | `issue-sweep` with `merge: auto`, `custom-prompt` |

A schedule's tier is computed from its type and its resolved gate. The console
shows it as a chip, and the admin view sorts by it.

### 4.2 Approval modes

A gate has three independent points, plus who may approve:

| point | values | meaning |
|---|---|---|
| `run` | `auto`, `approve` | `approve`: each firing waits in the inbox, showing its resolved parameters and repositories, before it creates anything |
| `plan` | `auto`, `approve` | for types that plan (issue runs, epic ticks): each plan waits for approval of its digest. This is the issue run's `plan_approval: required` |
| `merge` | `off`, `approve`, `auto` | `approve`: a green, reviewed pull request waits in the inbox before the merge-only continuation is submitted. `auto` is the issue run's `auto_merge` |
| `approvers` | `members`, `owner_only`, or a list of members | §4.6 |

The owner's five modes map onto it like this:

| mode | `run` | `plan` | `merge` |
|---|---|---|---|
| auto | `auto` | `auto` | `auto` or `off` |
| approve the plan | `auto` | `approve` | any |
| approve each run | `approve` | any | any |
| approve before merge | any | any | `approve` |
| owner-only | any | any | any, with `approvers: owner_only` |

**Floors.**
- Each type declares a floor (§3, the `floor` column). A member may set any gate **at or above**
  the floor.
- Lowering a gate below the type's **default** needs a platform admin, and is
  audited. **The one exception is the merge switch below.**
- Below the floor is impossible.
- Raising a gate is always allowed and takes effect at the next firing. A
  firing already awaiting approval keeps the gate it was claimed with.

**The switch from approval-required to fully automatic (Owner decision
2026-10-08, SD3).** A schedule whose type's floor allows `merge: auto`
(today `issue-sweep`) shows a control on its gate card, in the console, to
switch it between "asks before merge" and "fully automatic". Fully automatic
is the issue run's `auto_merge`: the merge-only continuation is submitted
once CI is green and the review's verdict is MERGE at the `green_sha`.

* It is an **audited edit**: a `schedule_audit` entry `gate_merge_auto` (or
  `gate_merge_approve` for switching back) carrying the before and after of
  `gate`, the person, and the revision.
* It needs **the same approver rule as SD5** (§4.6). The member who switches
  it on must not be the last editor of the schedule's gate, scope or spec when
  the tenant has more than one member, so one person cannot set a gate and
  then switch it open alone. In a one-person `u-*` tenant the person switches
  it after a typed confirmation, which is audited. **In a `platform: true`
  repository only platform admins may switch to `merge: auto`** (**Owner
  decision 2026-10-08**: "only admins in the platform repos"): the admin
  roles of PR 860 (`admin_roles/`, `PLATFORM_OWNER` included). Elsewhere the
  SD5 rule above applies.
* **Switching back to approval is always allowed**, by any member, at once,
  and takes effect at the next merge decision. A run already awaiting merge
  approval keeps waiting.
* It does not touch the hard stops. **These cannot be switched off by it or
  by anything else:** `.github/workflows/` (§4.4 refuses it at plan), IAM and
  Terraform bootstrap, the frozen contract, security-class plans, budget
  exhaustion and run-over-budget, and auto-pause. A run held for any of them
  does not merge until a `merge` approval from the hold's approvers, whatever
  the switch says (§4.5).

### 4.3 Budgets, and what they can and cannot promise

`budget = {per_run_usd, per_day_usd, max_concurrent}`.

**Owner decision 2026-10-08 (SD4): per-type dollar caps, for schedules
only.** These are the platform caps and the defaults (§1.1):

| type | per run | per day | concurrent |
|---|---|---|---|
| `issue-sweep` | $15 | $120 | 8 |
| `issue-plan-only` | $3 | $30 | 5 |
| `observer` | $5 | $10 | 1 |
| `repo-index-refresh` | $5 | $40 | 4 |

The other types' figures are set in their lanes (§9, S10). No measurement
backs these numbers; they are the owner's caps. **An unknown cost counts at
the per-run cap**, never as zero.

* **`max_concurrent` is enforced before work exists.** It is the number of
  live work items the schedule may have: a sweep's issue runs, a shepherd's
  continuations. A firing creates no more than the remainder.
* **`per_day_usd` is a firing gate, not an admission control.** A firing is
  skipped with `BUDGET_EXHAUSTED` when `reported_today + reserved >
  per_day_usd`.
  - `reserved` is `per_run_usd` for each live work item, and for each attempt
    whose cost was not reported. **An unknown cost counts as the cap, not as
    zero.**
  - The day is the schedule's timezone's.
  - Nothing parks: `PARKED(BUDGET_EXHAUSTED)` stays unwritten, as
    cost-control.md requires.
* **`per_run_usd` is a tripwire, not a ceiling.** `cost_usd` is written when
  an attempt ends, so nothing can stop a run at its cap mid-flight.
  - The real bound on one run is its time: the profile's `timeout_seconds` and
    the issue run's `fix_rounds`.
  - A run whose recorded cost exceeds `per_run_usd` ends its firing `failed`
    with `RUN_OVER_BUDGET`, and **pauses the schedule at once**.

  The worst case for a day is therefore `per_day_usd + max_concurrent ×
  (actual run cost − per_run_usd)`, and the console's budget card says so in
  words.

How this sits with the owner's decision of 2026-10-01 ("no per-tenant dollar
budgets"): **that decision still holds for everything that is not scheduled**
(tasks, workflows and issue runs started by hand). Nothing in admission reads
these figures, no tenant-wide budget is created, and the scheduler is
unchanged. The figures bound what a **schedule** may start, which the owner
confirmed in SD4. S9 records the carve-out in cost-control.md.

### 4.4 Hard stops, whatever the mode

None of these can be switched off: not by the merge switch (§4.2), not by an
admin, not by a gate set to `auto`.

| stop | detected | what happens |
|---|---|---|
| **IAM, Terraform bootstrap, `terraform/**/iam*.tf`, any `google_*_iam_*` change** | at plan (the plan's `files`, and a scan of step prompts for the paths) **and** at merge (the pull request's changed files, read through the forge API before the merge continuation is submitted, `issueci._merge`'s point) | **Held** (§4.5, "The hold lives on the issue run"), and the approver must satisfy BOTH the hold's rule and the schedule's approvers (`owner_only` or a named list): a hold only ever makes approval stricter and never replaces them. In a `platform: true` repository the hold is `NEEDS_OWNER` with `approvers: owner_only`. In any other repository it is `NEEDS_SECOND_MEMBER` with `approvers: second_member` (§4.6, SD11). The plan's files are advisory, which is why the merge check exists |
| **The frozen contract**, `apps/common/swarm_common/**`, in a repository whose registration marks it `platform: true` | the same two points | `NEEDS_OWNER` with `approvers: owner_only`, and the inbox item links [contract-change-requests.md](contract-change-requests.md). `platform: true` exists only in a platform repository, so this hold never reaches another tenant |
| **`.github/workflows/**`** | at plan | **Refused, not held.** The credential cannot push those files (§0), so the step would fail after spending. When the planner's plan names such a file, the plan is not stored: the run moves `PLANNING` → `FAILED`, a transition `RUN_TRANSITIONS` already has, with `error` carrying the code `WORKFLOWS_PATH` and the matched files. No workflow is submitted, no re-plan is attempted, and no state is added. The issue gets a comment saying the change must be made by a person or split out of the issue, and the sweep's cooldown (§3.1) applies to it as to `NOT_READY`. A person's plan edit (`PLANNED` → `PLANNED`) that adds such a path is refused with the existing 422 `invalid_plan`, naming the path |
| **A security-class issue**: label `security`, or the form's severity S0 | at selection and at plan | Excluded from `issue-sweep` by default. If a member removes `security` from `labels_exclude`, or a run is created on such an issue by any path, its plan is **always** held and its merge is never `auto` (owner decision 2026-10-08). The hold is `NEEDS_OWNER`, `approvers: owner_only`, in a `platform: true` repository, and `NEEDS_SECOND_MEMBER` elsewhere (SD11) |
| **Protected paths** a tenant declares on a registration (`hard_stop_paths`, a field lane S11 adds, default `[".github/workflows/**", "terraform/bootstrap/**", "**/iam*.tf", "CODEOWNERS"]`) | both points | `NEEDS_OWNER` in a `platform: true` repository. In another repository, `NEEDS_SECOND_MEMBER`, with the matched path named |
| **Budget exhausted** | at firing | `skipped / BUDGET_EXHAUSTED`. The schedule stays `enabled` and fires again the next day |
| **Run over budget** | at the work's end | the firing is `failed / RUN_OVER_BUDGET`. The schedule becomes `auto_paused` |
| **N consecutive failures** (`failed` or `refused`; default N = 3, range 1-10) | at outcome | `auto_paused / CONSECUTIVE_FAILURES`, naming the last N firings |
| **Owner left the tenant** | at firing | `auto_paused / OWNER_NOT_MEMBER` |
| **Repository no longer registered or granted** | at firing | that repository is skipped, `REPOSITORY_NOT_GRANTED`. If the scope resolves to none, the firing is `refused` and counts as a failure |

**Two registration fields carry these stops, and neither exists today.**
`platform: true` marks a repository as the platform's own. Only a platform
admin may set or clear it, because clearing it would loosen the owner's holds.
`hard_stop_paths` is the list above. A member may add patterns but cannot
remove the four defaults, which are its floor. Lane S11 (§9) adds both to the
registration model and its routes; nothing reads them before S5.

Every hard stop is **its own code**. Each new refusal among them ships
report-only behind a `refusals.SWITCHES` entry (PR 873), except the
`.github/workflows/` refusal and the security-class owner gate. Those two
record what the platform already cannot do and what the owner already decided,
so a report-only phase would only let through work that cannot succeed. The
owner confirmed this exception in SD3.

### 4.5 Approvals: one record, one inbox

`approvals/{approval_id}` is a new collection, kept by `swarm_api/approvals.py`.

| field | meaning |
|---|---|
| `tenant_id` | as everywhere: a mismatch is a 404 |
| `kind` | `run` (a firing waiting to create work), `plan` (an issue-run plan or an epic-tick list), `merge` (a green PR waiting), `proposal` (an observer or flake proposal to file as an issue), `spec` (a `custom-prompt` digest, admins only), `hold` (any §4.4 hold: `NEEDS_OWNER` or `NEEDS_SECOND_MEMBER`, with its `approvers`) |
| `subject` | `{schedule_id?, firing_id?, run_id?, pr?}` |
| `digest` | what is approved. For `run`, the firing's `params_digest`. For `plan`, the plan digest (D3). For `merge`, `{head_sha, verdict}`, so a push after the request makes the approval stale (409 `merge_changed`). For `spec`, the spec digest |
| `summary` | what the inbox shows, built by swarm-api from the subject, masked |
| `approvers` | resolved at creation (§4.6) |
| `state` | `pending`, `approved`, `rejected`, `expired` or `superseded` |
| `requested_at`, `expires_at`, `decided_by`, `decided_at`, `reason` | `reason` is required for a rejection |

**Issue runs join the inbox without moving.** A `PLANNED` issue run's existing
approve, edit and reject stay where they are, in `issueruns.py` and
`routes/runs.py`. The inbox **projects** them: the list route reads `PLANNED`
runs (the existing index `issue_runs where tenant_id == T and state ==
PLANNED`) beside `approvals/`, and an approve from the inbox calls the run's
existing approve with the digest shown. So a plan approved in Work › Runs, from
`sc plan approve` or from the inbox is one approval, and there is one source of
truth for it.

**The hold lives on the issue run, so no approve path can step round it.** A
projection alone would leave three ways past an owner's hold:
`routes/runs.py::approve_plan`, which any member may call (and which
`swarm_plan_approve` and `sc plan approve` reach), and the tick's auto-approval
of `plan_approval: auto` runs in `routes/runs.py::_advance_state`. So S5:

* adds one field to the `IssueRun` document in `issueruns.py`, `hold: {code,
  approvers, matched, set_at}` or null. `code` is a §4.4 code such as
  `NEEDS_OWNER`; `approvers` is `owner_only` or `second_member`; `matched` names
  the paths or the label that set it;
* sets it where the plan is stored (`_from_planner`, `PLANNING` → `PLANNED`)
  and re-checks it on every plan edit. **An edit can add a hold but never
  clears one**: only the hold's approver approving, or anyone rejecting, ends
  it. Otherwise a member could edit the IAM file out of the plan and approve
  it, and the plan's files are advisory;
* checks it **inside `_approve`**, the one function every approval goes
  through. An approval by someone the hold does not admit answers 403
  `hold_approver_required`, naming the code. An auto-approval (`by:
  AUTO_APPROVER`) never satisfies a hold, so `_advance_state` leaves a held
  `auto` run `PLANNED`, holding nothing (invariant 1), and it appears in the
  inbox as a `kind: hold` item;
* checks it at the merge point, `issueci._merge`. A held run's `auto_merge`
  does not merge; its merge waits for a `merge` approval from the hold's
  approvers.

This applies to every issue run, scheduled or not, because a run created by
hand would otherwise be the way round a schedule's hold. The security-class
hold is enforced from its first day; the IAM and protected-path holds follow
the report-only rule below §4.4.

Approving is one transaction:

1. Check the state and the digest.
2. Write the decision and a `schedule_audit` entry.
3. Move the subject: create the firing's work, submit the merge continuation,
   or apply the epic ticks.

Two approvers racing produce one transition, and the loser gets 409
`already_decided`.

### 4.6 Who may approve

* **Default: any member of the schedule's tenant** (**Owner decision
  2026-10-08, SD5**). This is the issue runs' rule, and the only role a
  tenant has (§0: there is no tenant-admin role).
* **`approvers: owner_only`**: `PLATFORM_OWNER` only. It is forced for the
  `NEEDS_OWNER` holds of §4.4, which arise only in `platform: true`
  repositories. Those live in the tenant the owner names for platform
  automation (SD7), of which the owner is a member, so the owner approves them
  as a member of that tenant and never across tenants.
* **`approvers: second_member`**: any member of the run's tenant **other than**
  the person who created the run, last edited its plan, or last changed the
  schedule's gate, scope or spec. It is forced for the §4.4 holds in every
  repository that is not `platform: true`: IAM and bootstrap paths, protected
  paths and security-class plans. In a one-person tenant (a `u-*` workspace)
  there is no second member, so that person approves their own after a typed
  confirmation that names the matched paths, and the audit records it.
  **Owner decision 2026-10-08 (SD11).**
* **A named list.** Each name must be a member at creation **and** at approval,
  because membership is asked of the directory.
* **A merge-tier approval needs a second person** (SD5). The member who last
  changed the schedule's gate, spec or scope may not approve its R3 runs or
  merges, or switch its merge to automatic (§4.2), when the tenant has more
  than one member. In a personal `u-*` tenant there is one person, so the
  rule cannot apply, and the console says so on the gate card.
* **Platform admins cannot approve another tenant's work** by being admins
  (§5.3), and that includes `PLATFORM_OWNER`. An admin who is also a member
  approves as a member. So an `owner_only` item can only exist where the owner
  is a member, and a hold in `eng` or `u-alice` is never one that nobody there
  can decide. **SD11 chose this over an audited cross-tenant exception:** the
  owner has no way to read or decide another tenant's hold.

### 4.7 Expiry

`approval_ttl_hours` defaults to 72 (range 1-336). An expired approval:

* For `run`, the firing ends `expired`, and the next slot fires normally.
* For `plan`, the issue run is **rejected with reason "expired"**. It moves to
  the existing `REJECTED` state rather than gaining a state, and the issue's
  cooldown starts.
* For `merge`, the pull request stays open, green and unmerged, and the run
  stays `DONE`. Nothing merges after expiry without a new approval, which the
  console's "Request merge again" creates.

**A pending approval holds nothing**, before and after expiry. No task, lease
or slot exists for it, and a plan's planner task has already ended (invariant
1). Expiry is a tidy-up of the inbox, not a capacity release. Expired items
leave the inbox and remain in the schedule's history.

### 4.8 The audit

`schedule_audit/{id}` is **append-only**, like `admin_audit`. The module that
writes it has no update or delete path. An entry is written in the same
transaction as the change it records:

* create, edit (with the before and after of `gate`, `budget`, `scope`,
  `cron`, `params` and `state`), pause, resume, auto-pause, disable, delete,
  take-ownership and run-now;
* every approval decision;
* every hard stop.

Fields: `{schedule_id, tenant_id, action, by, at, detail}`. `by` is an email,
`schedule-tick` or `admin:<email>`. It is private (Firestore): the console
shows it to the tenant's members, and the admin view to admins.

### 4.9 Notifications

| event | console | plugin | GitHub | admins |
|---|---|---|---|---|
| an approval is waiting | the Automate section's count badge, Overview's "Waiting on you" card | `sc` prints "2 approvals waiting" on its next command; `swarm_approvals` | the issue's plan comment, for plans, and the pull request, for merges, each showing `/swarmcloud approve <digest>` (§4.9) | — |
| auto-pause | a banner on the schedule and the list | the same line | — | a log-based metric, `schedule_auto_paused`, on the existing alert channel (`alert_emails`), counted per tenant |
| a hard stop needing the owner | an inbox item marked owner | the same | — | the same alert, `schedule_needs_owner` |

The plugin's approval prompt is AskUserQuestion-shaped. When `swarm_approvals`
returns pending items, the operator's Claude session asks the person with the
item's summary and the choices Approve, Reject or Later. Approve calls
`swarm_schedule_approve` with the digest shown. **A GitHub comment
(`/swarmcloud approve <digest>`) approves, in the first build** (**Owner
decision 2026-10-08, SD9**: it is not phase 2). It is lane S12 (§9). The rules
below are **Owner decision 2026-10-08 (accepted as designed)**.

* **Where.** On the issue, for a plan; on the pull request, for a merge. The
  plan comment and the merge request show the exact command with the item's
  current digest.
* **The poll reads comments.** For each pending approval whose subject has a
  GitHub thread, the schedule tick's advance step reads the comments newer
  than the last one seen (a cursor stored on the approval) with the tenant's
  forge token. It reads nothing for a subject with no pending approval, so
  the cost is bounded by the inbox. A comment is only ever a request: it is
  turned into the same `:approve` transaction as the console's (§4.5), so the
  digest check, the hold check and the audit are the same.
* **The GitHub login is mapped to a member, and the mapping is verified.**
  The login must be the one a current member connected through onboarding's
  GitHub connection, a login GitHub itself returned for that member's
  authorisation, never a name written in a comment. S0 verifies where the
  connection stores it. A comment is accepted only when:
  - its author is a `User`, not a bot, and the login matches that member
    case-insensitively;
  - that member is **still a member** of the item's tenant, asked of the
    directory now;
  - the comment has not been edited (`updated_at` equals `created_at`), so an
    approval cannot be rewritten after it was read;
  - the digest in the comment equals the item's **current** digest. A stale
    digest approves nothing and the poll replies once with the current one;
  - the member satisfies §4.6 for this item: the hold's approvers, and the
    second-person rule. A member who is the last editor cannot approve a
    merge-tier item by comment any more than in the console.
* **What a comment cannot do.** A comment cannot satisfy a typed confirmation,
  so a one-person tenant's own hold (§4.6) is approved in the console, and a
  comment on it gets a reply saying so. A comment from a login with no
  verified member gets one reply saying it approved nothing. `reject` stays in
  the console, the plugin and the API.
* **The audit** records `via: github_comment`, the comment id, the login and
  the member it resolved to.

---

## 5. Multi-tenancy and isolation

### 5.1 A tenant sees only its own

Every route resolves the caller's tenant, honouring `X-Swarm-Tenant` as a
selector that never grants
([multi-tenancy.md](multi-tenancy.md#the-tenant-switcher-x-swarm-tenant-selects-it-never-grants)).
It compares the stored `tenant_id` and answers a mismatch with the 404 a
missing id gets. That covers `schedules/`, `schedule_firings/`, `approvals/`
and `schedule_audit/`. A schedule id, a firing id and an approval id are
therefore never an oracle for another tenant.

### 5.2 A firing runs as its tenant

The work a firing creates is ordinary work in `schedule.tenant_id`, submitted
as the stored owner (§2.7). So it runs with that tenant's worker identity,
secrets, GCS prefix `tenants/<id>/`, namespace and `tenant:<id>` pools, with no
schedule-specific path (invariant 9). The forge token is the one the tenant's
work already resolves (git-tokens.md's order). A type that calls GitHub from
swarm-api uses the same `SecretManagerForgeTokens` read as the issue preview,
for that tenant's secret only.

### 5.3 The admin view

`GET /v1/admin/schedules` (`is_admin`) lists every tenant's schedules. Each row
shows:

* the tenant, type, tier, cron in words, next run and last outcome;
* spend today with its coverage;
* pending approvals (a count only);
* state.

Admins may **pause**, **disable** or **re-enable** any schedule, and pause all
of a tenant's (`POST /v1/admin/tenants/{tenant_id}/schedules:pause`, typed with
the tenant id, §2.11 and §7.1), each audited as `admin:<email>`. An admin does
not cancel a tenant's live runs from here: that is the tenant's
`:pause` with `cancel_live`, or an admin's ordinary cancel of the work itself. They may **not** edit another
tenant's parameters, gate, scope or spec, and may **not** approve its
approvals. Those are the tenant's decisions, and an admin's console session is
not that tenant's identity. They also create platform-scope schedules (§3.13).

### 5.4 Repository scope

* A schedule's `repo_ids` must each be a registration **of the schedule's
  tenant** (`repositories/{repo_id}.tenant_id`). It is checked at create, at
  edit and at every firing, and `all` resolves at firing time.
* For a type that **pushes** (§3, "pushes"), the owner's grant on each
  repository must be `write` (`swarm_api/access.py`, `MODES`) when
  `REPOSITORY_GRANTS_ENFORCED` is on. That is the same check a submission
  makes, made early so the form can say so. A grant revoked later skips that
  repository at firing (`REPOSITORY_NOT_GRANTED`).
* A repository registered by two tenants is two registrations. Each tenant's
  schedules see only their own, as the index does (repo-index.md §2.4).

### 5.5 Personal workspaces

A `u-*` tenant is a tenant: it gets its own schedules, scoped to its own
registrations, firing as its one member.

* It is in the tick because the tick queries across tenants (§2.1).
* The issue runs its schedules create are advanced by the schedule tick
  (§2.9), so an `auto` sweep in `u-alice` moves while Alice is away. Her
  unscheduled runs still advance only on read: that gap is the per-tenant
  tick's, and is not widened or closed here.
* Before the workspace is ready, firings are `refused / WORKSPACE_NOT_READY`.
  Creating a schedule is allowed, so a person can set one up during
  onboarding, but it is created **paused**, with the reason shown.

### 5.6 What a runaway schedule can and cannot do to another tenant

| it CAN | it CANNOT |
|---|---|
| Fill its own tenant's `tenant:<id>` pool, up to `max_active` and `capacity_units`. The tenant's other work then queues behind it | Exceed its tenant's ceilings. Admission checks every pool in one transaction (invariants 2-3) |
| Take `global` and `resource:<class>` slots up to its tenant ceiling, so other tenants' work queues longer when the platform is full. **This is the existing ceiling arithmetic**: the sum of tenant ceilings above `global` is the oversubscription an admin chose | Hold capacity while waiting. Approvals, queued firings and `PLANNED` runs are documents (invariant 1) |
| Use a shared pool Claude account's concurrency where its tenant borrows one. Per-key `provider:<p>:tenant:<id>` AIMD keeps its 429s to its own target | Exhaust another tenant's provider key: keys are per tenant (invariant 9) |
| Spend its own forge token's rate limit | Delay another tenant's firings by more than one tick: at most 5 firings per tenant per tick, round-robin (§2.10) |
| Fire as often as its type's minimum interval allows, across at most 25 schedules | Run without bound. `max_concurrent`, `per_day_usd`, the 8-run cap of the sweep and the consecutive-failure pause bound it, and an admin can pause it (§5.3) |

What an admin uses when one tenant's schedules crowd the platform is what they
use today: the tenant's ceiling (`PUT /admin/tenants/<t>/limits`), plus pause
in §5.3.

---

## 6. The console

### 6.1 Placement (SD1)

**Owner decision 2026-10-08: a new spine section, Automate** (mock-ups §2,
variant **P3**). The spine becomes **Overview · Work · Automate · Capacity ·
Admin**. This is not the Work › Schedules tab this design first
recommended.

* **Automate holds two pages: Schedules and Approvals.** Approvals is a page of
  its own at `/approvals`, not a second pane of Schedules, with a count badge on
  the section and on the page. Its items cover issue runs as well as
  schedules. It is also on Overview, as a "Waiting on you" card that appears
  only when something is waiting.
* Schedules list the **built-in rows** read-only (SD7, §8.3), so all recurring
  work is in one place.
* navigation.html kept the spine to four sections on purpose, and
  repositories.html rejected a section for one page. Automate is a section
  because it has two pages and a different question from Work's ("what runs
  by itself, and what is waiting on me", against "what is running").

The alternatives, drawn and not chosen:

* **P1, Work › Schedules, after Runs,** with Approvals as its second pane. It
  was the recommendation, because "below runs in the main menu" is what the
  owner described.
* **P2, inside Runs**, as a segmented control: "Issue runs | Schedules |
  Approvals". Runs today means issue runs, and a schedule is not a run.

The admin cross-tenant view is **Admin › Schedules** (`admin: true`), beside
Tenants.

Adding a section changes `SECTIONS` in `apps/swarm-ui/src/App.tsx`. So the
build lane (S7) also updates the issue forms' "Where" list, which
`tests/unit/scripts/test_issue_forms.py` holds to `SECTIONS`, and the nav
tests (`nav.links.test.tsx`, `test_nav_headings_agree.py`).
`SECTIONS` today is Overview, Work, Capacity and Admin, and `App.tsx`'s own
header comment argues for few sections, so S7 amends that comment with the
reason Automate exists. The new entry's `id` must be a **literal**, not a
constant: `test_nav_headings_agree.py` reads the array out of the TypeScript
with a regex, and every tab needs a `SectionBody` case. S7 reads how both
tests parse sections before it edits them, and records any difference
here.

**Read by S7, 2026-10-11.** Neither parser needed a change. Both read
`SECTIONS` with the same regex (`test_issue_forms.py` imports
`test_nav_headings_agree.py`'s), and the literal `id: 'automate'` entry parses
like the other four. Two things follow from how they read it. A one-tab
section draws no tab strip, so the forms name it by the section alone ("Web UI
· Automate (/automate/schedules)"); when Approvals becomes the second tab both
forms' options become "Automate › Schedules" and "Automate › Approvals", and
the forms test fails until they do. And a tab is added only with its
`SectionBody` case and its screen, because the nav test fails a tab whose
route renders "No such pane": so Approvals joins `SECTIONS` in the lane that
builds `Approvals.tsx`, not before. `docs/web-ui/redesign.md` §2 also carries
each section's question verbatim (`apps/swarm-ui/tests/sections.test.ts`), so
Automate's row was added there.

### 6.2 Screens

Each screen is drawn in 2-3 variants in the mock-ups, with a recommendation.

| screen | variants | recommended | why |
|---|---|---|---|
| Schedules list (Automate › Schedules) | A table · B cards grouped by repository · C split list and detail | **A** | It holds the eight columns the owner listed without wrapping at 1280px, and drops to two lines a row on a phone. B shows repository grouping, which the "Repository" filter on A also gives |
| Schedule detail | A tabs (History, Budget, Gate, Settings, Audit) · B one page, timeline first | **A** | Run history is the first tab and the reason to open the page. Audit and Settings are long and rarely read |
| Create and edit | A one page from a type template · B a four-step wizard · C a drawer over the list | **A** | Every field has a default from the type, so most schedules are a type, a repository and a time. One page with the cron editor and the next five firings beside it shows the whole decision at once |
| Approvals inbox (Automate › Approvals) | A one list, each item expanding in place · B split list and detail | **B** on desktop, A on a phone | A plan or a merge needs its diff or plan read before approval, which a split view gives room for |
| Admin cross-tenant | A one table with a tenant column · B per-tenant groups with rollups | **A** | Sorting by tier, spend or failures across tenants is the admin's question |

The docs/web-ui rules apply:

* theme tokens and the sky-blue brand (palette A);
* the 12px floor, no all-caps;
* phone widths: each screen has a 390px frame;
* **an unknown is a dash with its reason, never 0.** "Spend today" is
  `$3.10 · 2 attempts unreported` with a partial mark (`Mark kind="partial"`),
  not `$3.10`. A schedule that has never fired shows the `Absent` empty state,
  not "0 runs";
* no invented data: every figure on a frame is labelled example data and
  mapped to its field (mock-ups §10).

### 6.3 Cron in words, and the next five

The form offers presets (every hour, weekdays at 09:00, nightly, weekly on
Monday) and a raw field. Below the field it shows the expression in words and
the **next five firings in the schedule's timezone and in the viewer's**. All
of that comes from `POST /v1/schedules:preview {cron, timezone, type}`, which
answers `{words, next: [...], min_gap_minutes, refusal?}` from the tick's own
parser (§2.3), so the form and the tick cannot disagree.

---

## 7. The plugin and the API

### 7.1 Routes

All of these are tenant-scoped as in §5.1. The new refusals ship report-only
(PR 873) except as §4.4 says.

| route | does |
|---|---|
| `GET /v1/schedule-types` | the catalogue: name, description, parameter schema, default and floor gate, minimum interval, availability. Each tenant sees the types it may create |
| `GET /v1/schedules` | the tenant's schedules with `last_firing`, `next_run_at`, words, spend today with coverage, and pending approval count |
| `POST /v1/schedules` | create: `{name, type, scope, cron, timezone, params?, gate?, budget?, policy?, state?}`. Takes an optional `client_request_id`; a repeat within 24 h returns the schedule the first one created |
| `GET /v1/schedules/{id}` | one schedule, with its last 50 firings |
| `PATCH /v1/schedules/{id}` | edit, carrying `revision`. A gate below the default needs `is_admin`. `gate.merge: auto` is **refused here** with 422 `use_merge_switch`; the switch has its own route, next row |
| `POST /v1/schedules/{id}:merge-mode` | the SD3 switch (§4.2): `{mode: "approve" \| "auto", revision, confirm?}`. `auto` is allowed to a member who is not the last editor of the gate, scope or spec (`confirm` carries the typed confirmation in a one-person tenant), allowed only to a platform admin (the admin roles of PR 860, `PLATFORM_OWNER` included) in a `platform: true` repository (Owner decision 2026-10-08), and only where the type's floor permits it. `approve` is allowed to any member. Both write a `schedule_audit` entry in the same transaction |
| `DELETE /v1/schedules/{id}` | delete (the console's typed confirmation is client-side; the route takes `confirm: <name>`) |
| `POST /v1/schedules/{id}:pause` · `:resume` · `:run` | `:pause` takes `{reason?, cancel_live?, confirm?}`; `:run` takes `{dry_run?}` and makes a `run_now` firing, under the gate's `run` point |
| `POST /v1/schedules/{id}:pause` with `{cancel_live: true, confirm}` | §2.11's "pause and cancel live runs". Any member of the schedule's tenant. `confirm` is the schedule's `name`, typed exactly (surrounding spaces trimmed, as `DELETE` does); missing or different is 422 `confirmation_required` and nothing moves. The schedule is paused as a plain `:pause` pauses it (a `disabled` one is left `disabled`, since it already makes no firings), then every work item of the schedule's `created` firings in this tenant is cancelled through the platform's own cancel: a task through `POST /v1/tasks/{id}/cancel`'s path, a workflow through `POST /v1/workflows/{id}/cancel`'s, an issue run through its live planner task or workflow. Work already ended is reported, not an error. The firing then ends `cancelled` on the tick's ordinary advance (§2.9). Audit: `pause` (when the state moved) and then one `cancel_live` entry, `by` the caller, `detail: {firings, items: [{kind, id, firing_id, result}]}`. Another tenant's id is the 404 a missing one gets (§5.1) |
| `POST /v1/schedules:pause-all` | §2.11's "pause all of a tenant's schedules". Any member of the caller's tenant, which is the only tenant it reaches. `{confirm, reason?}`: `confirm` is the caller's tenant id, typed exactly; missing or different is 422 `confirmation_required` and nothing moves. Every `enabled` schedule of the tenant becomes `paused` with the one reason, each in its own transaction with its own audit entry `pause_all` (`by` the caller's email, `detail: {from, to, revision, reason}`). `paused`, `auto_paused` and `disabled` ones are left as they are. Answers `{tenant_id, paused: [ids], unchanged: [ids]}` |
| `POST /v1/admin/tenants/{tenant_id}/schedules:pause` | §5.3: the same for any tenant, `is_admin` only. `{confirm, reason?}`: `confirm` is the `tenant_id` of the path, typed. An unknown tenant is a 404. Each entry is `admin_pause_all`, `by: admin:<email>`. Live runs are not cancelled (§5.3) |
| `POST /v1/schedules/{id}:take-ownership` | the caller becomes the owner. Audited |
| `GET /v1/schedules/{id}/firings` · `/{firing_id}` | the history. A firing shows its work links, cost and dry-run output |
| `GET /v1/schedules/{id}/audit` | the audit, newest first |
| `POST /v1/schedules:preview` | §6.3 |
| `GET /v1/approvals` | the inbox: `approvals/` plus projected `PLANNED` runs, filterable by kind |
| `GET /v1/approvals/{id}` | one item, with what its kind needs to decide it: for `merge`, the run's `green_sha`, the review verdict and requirements, and the pull request's changed files checked against the protected paths |
| `POST /v1/approvals/{id}:approve` · `:reject` | `{digest}` / `{reason}`. A projected run's id is `run:<run_id>` and calls its existing approve |
| `POST /v1/admin/schedules/tick` | the tick (§2). Admits only the `swarm-schedule-tick` identity (SD10) |
| `GET /v1/admin/schedules` | §5.3, with tick health read from `schedule_ticks/` (§2.10) |
| `POST /v1/admin/schedules/{id}:pause` · `:disable` · `:enable` | §5.3 |

### 7.2 MCP tools

| tool | wraps |
|---|---|
| `swarm_schedule_types` | `GET /v1/schedule-types` |
| `swarm_schedules` | the list, or one schedule with `id` |
| `swarm_schedule_create` | create. The tool's description lists the available types by name and says that no image or command is accepted |
| `swarm_schedule_update` | `PATCH`, with revision |
| `swarm_schedule_pause` · `swarm_schedule_resume` | the two verbs |
| `swarm_schedule_run_now` | `:run`, with `dry_run` |
| `swarm_approvals` | the inbox |
| `swarm_schedule_approve` · `swarm_schedule_reject` | approve with the digest the tool returned, or reject with a reason. For a projected issue-run plan this is the same call as `swarm_plan_approve` |

### 7.3 `sc schedules`

```
sc schedules                         list, like the console's table
sc schedules show <name|id>          detail and the last 10 firings
sc schedules new <type> --repo owner/repo --cron "0 9 * * 1-5" [--tz Europe/London] [--param k=v ...] [--dry-run]
sc schedules pause|resume <name|id> [--reason ...]
sc schedules run <name|id> [--dry-run]
sc schedules preview "<cron>" [--tz ...]
sc approvals                         the inbox; then approve|reject <id>
```

`plugin/commands/sc.md` documents them. **The operator's session cron becomes
unnecessary:** the session no longer runs a loop. It reads `sc approvals` when
the person asks, or when `sc` prints the waiting count.

---

## 8. Migration

### 8.1 Lane SWEEP's `issue_sweep` becomes the `issue-sweep` type

**Lane SWEEP (PR 898, default off) is the first implementation of
`issue-sweep`** (SD2). It ships first, as briefed and as PR 898's description
(read 2026-10-08) says it is built:

* a per-tenant Cloud Scheduler job, `issue_sweep`, **off by default** twice
  over: `SWEEP_ENABLED` on swarm-api (`var.enable_issue_sweep`) and the
  tenant document's `issue_sweep.enabled`;
* the sweep logic in `swarm_api/issuesweep.py`, behind
  `POST /v1/admin/issues/sweep?tenant_id=`, admitted to the rollup sweeper
  through `ROLLUP_SWEEPER_ROUTES`;
* the `NOT_READY` verdict and run state, the cap of 8 and the territory guard
  (`routes/runs.py` `territory_conflict`, which applies to every run and
  stays where it is);
* runs started `plan_approval: auto`, `auto_merge: true`, `fix_rounds: 2`.

The migration keeps SWEEP's logic and moves its trigger and configuration:

1. **S6 makes SWEEP's module the type's executor.** Its constants (cap, skip
   labels, `fix_rounds`, the tenant document's `max_live_runs` and exclusion
   lists) become the parameters of §3.1, with SWEEP's values as the defaults.
   Its readiness verdict and territory guard stay in its module, unchanged in
   behaviour. Its tests keep passing, which is the lane's acceptance. The
   route and the tenant document's settings are kept until step 4.
2. **S9 creates one `issue-sweep` schedule** per tenant that had SWEEP enabled,
   with SWEEP's cadence and scope, written by an operator through the API and
   owned by a named member. **It starts with the §3 default gate: plan
   automatic, merge asks first (`merge: approve`)**, because the sweep that
   merges unattended is now the audited switch of SD3 (§4.2), not the default.
   A tenant whose sweep auto-merged under SWEEP therefore gets a **stricter**
   gate on migration, and a person who wants the old behaviour back uses the
   switch, under the two-person rule. Its first firing runs **dry** (§2.8), and
   its candidate list is compared with SWEEP's last real sweep.
3. **The SWEEP job is paused** (`paused = true` in Terraform) in the same
   release that enables the schedule. Two triggers of one sweep never run: the
   per-issue live-run check already dedupes, but a paused job makes it
   obvious.
4. **After 7 days of the schedule firing without an auto-pause**, the next
   S9 change deletes `google_cloud_scheduler_job.issue_sweep`, its route's
   entry in `ROLLUP_SWEEPER_ROUTES`, `SWEEP_ENABLED` and the tenant document's
   `issue_sweep` settings, and points SWEEP's docs here.

If SWEEP's merged names differ from the above, S6 follows SWEEP's code and
records the difference in this document. That is the rule for when another
lane's layout contradicts this one.

### 8.2 The operator's session cron

Job `bdb47d30` is deleted by the operator (`CronDelete`) **once the first
`issue-sweep` schedule has fired for real** and its firing shows created issue
runs. It is a session object, not code, so its deletion is a step in S9's
acceptance, evidenced by the firing id. It must not be deleted earlier,
because until then it is the only sweep.

### 8.3 The repository index stays where it is (SD7)

**Owner decision 2026-10-08: keep `index.*` on the registration and the
5-minute poll as they are, and show them as built-in rows in Automate.**

* The index's main trigger is **a change on the default branch**, found by an
  ETag poll, with in-flight dedupe and a minimum change interval. A cron
  expression cannot say "when the head moves".
* Folding it in would make the schedule tick read every repository's head every
  five minutes, a second poll beside the first.
* `index.interval_hours` is a backstop, not a schedule anyone plans around.

So:

* Automate › Schedules shows each registration's index cadence as a **read-only
  "built-in" row** ("Index · example-org/example-api · on change, at most every
  30 min, and every 24 h"), linking to the repository's Settings. A person sees
  all recurring work in one place.
* **`repo-index-refresh` may be scheduled for a repository that also
  re-indexes on change** (the owner's note), for timed full refreshes, e.g.
  Sunday 03:00, across several repositories. The two triggers share the
  index's in-flight rule, so a refresh that finds a run in flight is not
  duplicated.

Folding the settings into schedules (the rejected option b) would have meant
migrating every registration's settings and moving the change trigger into the
tick. The owner chose not to.

### 8.4 Other existing ticks

`workflow_rollup`, `issue_run_advance`, `merge_wake`, `forge_refresh`,
`quota_refresh`, `reconciler` and `safety_tick` are **platform machinery**.
They are not jobs a tenant manages, and they stay in Terraform. The admin view
lists them read-only, as "Platform ticks", from a static list in the S7 lane,
so an admin sees every recurring thing in one place.

---

## 9. Build plan

Within a phase no file is in two lanes, and a lane depends only on earlier
phases. New files are named without their root.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| S0 | 0 | **Verification, no code.** Each result is dated in §0 of this document. It checks: which of `actions: read` and `checks: read` the GitHub App and tenant tokens hold (§3.7, §3.9); whether update-branch is refused when the base brings `.github/workflows/` changes (§3.6); SWEEP's merged names (§8.1, PR 898); the composite index shape for the tick query; the bootstrap file that lists the service accounts the deployer may act as, for the new `swarm-schedule-tick` account (SD10); and where onboarding's GitHub connection stores a member's verified login (SD9) | `docs/schedules.md` | SWEEP merged |
| S1 | 1 | The model: the `schedules/` document and its validation, the cron parser with words and preview, gate resolution with floors, budget arithmetic, and the type catalogue with all twelve entries and `available` from the executor modules' presence (so later type lanes add a file and edit nothing shared) | new `swarm_api/schedules.py`, new `swarm_api/cronexpr.py`, new `swarm_api/scheduletypes.py`, new `tests/unit/control_plane/test_schedules_model.py`, new `test_cronexpr.py` | S0 |
| S2 | 2 | The tick and firings: §2.1-§2.11, `schedule_owner_auth`, `metadata.schedule` reserved, the tick route admitted to the `swarm-schedule-tick` identity only (SD10), auto-pause and its codes and switches | new `swarm_api/schedulefire.py`, new `swarm_api/routes/schedule_tick.py`, `apps/swarm-api/swarm_api/auth.py` (a new `SCHEDULE_TICK_ROUTES` and `SCHEDULE_TICK_USERS`; `ROLLUP_SWEEPER_ROUTES` is not widened), `apps/swarm-api/swarm_api/validation.py` (`RESERVED_METADATA_KEYS`), `apps/swarm-api/swarm_api/refusals.py`, `apps/swarm-api/swarm_api/main.py`, new `tests/unit/control_plane/test_schedule_tick.py` | S1 |
| S13 | 1 | **The tick's identity (SD10).** The service account `swarm-schedule-tick`, its `run.invoker` on swarm-api (the pattern of `rollup_sweeper_invokes_api`), its id in the deployer's list of accounts, `SCHEDULE_TICK_USERS` rendered into swarm-api's environment, and `terraform test` assertions that the account has no other grant. **An IAM change: the owner applies the bootstrap and approves `dev-iam`** | `terraform/modules/service_account_ids/main.tf`, `terraform/modules/service_account_ids/outputs.tf`, `terraform/modules/scheduler/main.tf`, `terraform/modules/scheduler/outputs.tf`, `terraform/infra/main.tf`, `terraform/infra/locals.tf`, `terraform/infra/deployer.tf`, the bootstrap file S0 names, new `tests/terraform/schedule_tick_identity.tftest.hcl` | S0 |
| S4 | 2 | Terraform: `google_cloud_scheduler_job.schedule_tick` (every minute, `retry_count = 0`, the OIDC of `swarm-schedule-tick`), the composite indexes (`schedules`: `state`, `next_run_at`; `schedule_firings`: `schedule_id`, `slot` desc; `approvals`: `tenant_id`, `state`, `requested_at`), the TTL policies on `schedule_firings.expire_at` and `schedule_ticks.expire_at`, the `schedule_auto_paused` and `schedule_needs_owner` log metrics and alerts, and `terraform test` assertions. **No IAM change of its own**: the account is S13's, so this is an ordinary release once S13 has been applied | `terraform/modules/scheduler/jobs.tf`, `terraform/modules/scheduler/variables.tf`, `terraform/modules/firestore/indexes.tf`, `terraform/modules/monitoring/`, new `tests/terraform/schedule_tick.tftest.hcl` | S13 applied |
| S11 | 2 | The registration fields the hard stops read (§4.4): `platform` (bool, default false, set and cleared by a platform admin only, audited in `admin_audit`) and `hard_stop_paths` (the four defaults are a floor a member cannot remove), on create, patch and read, and a card for them on the repository's Settings tab | `apps/swarm-api/swarm_api/repositories.py` (`RepositoryCreate`, `RepositoryPatch`, the stored record), `apps/swarm-api/swarm_api/routes/repositories.py`, `apps/swarm-ui/src/RepositoriesDetail.tsx`, `apps/swarm-ui/src/api.ts`, new `tests/unit/control_plane/test_repository_hard_stops.py`, a vitest under `apps/swarm-ui/src/__tests__/` | S0 |
| S3 | 3 | The tenant routes of §7.1 except approvals and the merge switch, and the admin list and actions | new `swarm_api/routes/schedules.py`, `apps/swarm-api/swarm_api/main.py`, new `tests/unit/control_plane/test_schedule_routes.py` | S2 |
| S5 | 4 | Approvals: `approvals/`, the inbox with projected `PLANNED` runs, the run, merge and proposal gates, the hard stops of §4.4 at plan and at merge, the issue run's `hold` and its check in `_approve` and `issueci._merge` (§4.5), expiry, `schedule_audit/`, the issue run's `metadata.schedule` with its lookup by firing id, the SD3 merge switch (`POST /v1/schedules/{id}:merge-mode`, its approver rule, with platform admins only in `platform: true` repositories, and its audit entry), and mounting the approvals router. It follows S3 only because both mount a router in `main.py` | new `swarm_api/approvals.py`, new `swarm_api/routes/approvals.py`, new `swarm_api/schedaudit.py`, `apps/swarm-api/swarm_api/issueruns.py`, `apps/swarm-api/swarm_api/issueci.py`, `apps/swarm-api/swarm_api/routes/runs.py`, `apps/swarm-api/swarm_api/refusals.py` (the holds' report-only switches), `apps/swarm-api/swarm_api/main.py`, new `tests/unit/control_plane/test_approvals.py`, new `tests/unit/control_plane/test_issue_run_holds.py` | S3, S11 |
| S6 | 5 | The first types (SD2): `issue-sweep` (adopting SWEEP's module from PR 898), `issue-plan-only`, `repo-index-refresh`, `observer` | new `swarm_api/schedtypes/` (`issue_sweep.py`, `issue_plan_only.py`, `repo_index_refresh.py`, `observer.py`), SWEEP's module, new `tests/unit/control_plane/test_schedtypes_*.py` | S3, S5, SWEEP merged |
| S7 | 5 | The console (SD1): the new **Automate** section in `SECTIONS` with Schedules (list, detail, create/edit, the SD3 merge switch on the gate card, the built-in index rows) and Approvals as its two pages, the section's badge, the Overview card, Admin › Schedules, and `App.tsx`'s header comment; the issue forms' "Where" list | new `Schedules.tsx`, `ScheduleDetail.tsx`, `ScheduleEdit.tsx`, `Approvals.tsx` and `AdminSchedules.tsx` in apps/swarm-ui/src, `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/Overview.tsx`, `.github/ISSUE_TEMPLATE/`, tests under `apps/swarm-ui/src/__tests__/` (`nav.links.test.tsx` among them), `tests/unit/control_plane/test_nav_headings_agree.py`, and `tests/unit/scripts/test_issue_forms.py` only if its section parser needs the new entry (S7 says so in its PR) | S3, S5 |
| S12 | 5 | **GitHub-comment approvals (SD9), in the first build.** The poll that reads `/swarmcloud approve <digest>` comments on pending approvals' threads, the GitHub-login-to-member mapping and its verification (§4.9), the command shown in the plan comment and the merge request, and the audit's `via: github_comment` | new `swarm_api/ghapprove.py`, `apps/swarm-api/swarm_api/schedulefire.py` (one call from the advance step), `apps/swarm-api/swarm_api/issueruns.py` and `apps/swarm-api/swarm_api/issueci.py` (the comment text only), new `tests/unit/control_plane/test_github_comment_approvals.py`. If the verified login needs a field onboarding does not store, the lane stops and asks | S5, S11 |
| S8 | 5 | The plugin: the MCP tools of §7.2, `sc schedules` and `sc approvals`, and the docs | `apps/swarm-mcp/swarm_mcp/server.py`, `apps/swarm-mcp/swarm_mcp/sc.py`, new `swarm_mcp/schedules.py`, `plugin/commands/sc.md`, `plugin/README.md`, new `tests/unit/mcp/test_schedules_tools.py` | S3, S5 |
| S9 | 6 | Migration (§8.1-§8.2): the schedules created, SWEEP's job paused and later deleted (with its route, `SWEEP_ENABLED` and tenant settings), the session cron deleted; docs: issue-runs.md, operations.md, cost-control.md (§4.3's relation to 2026-10-01), multi-tenancy.md (§5.6) | `terraform/modules/scheduler/jobs.tf`, `apps/swarm-api/swarm_api/auth.py`, `docs/issue-runs.md`, `docs/operations.md`, `docs/cost-control.md`, `docs/multi-tenancy.md` | S6, S7, S8, S12 |
| S10a-h | 7+ | One lane per remaining type: `epic-triage`, `pr-shepherd`, `ci-flake-hunter`, `dependency-cve-refresh`, `release-health`, `docs-drift`, `cost-report`, `custom-prompt` (with its admin spec approval). Each adds **one file** in `swarm_api/schedtypes/` and its test. They can run in parallel because the catalogue reads availability from the file's presence | one new file each | S6; S0 for the GitHub permissions |

**Acceptance per lane, measurable:**

* **S1.** The parser's table test covers every field form, the DST gap and
  repeat in `Europe/London` and `America/New_York`, and the Vixie OR. Every
  catalogue entry validates its own defaults. A gate below its floor is
  refused.
* **S2.** Two concurrent ticks over one due schedule create **one** firing,
  tested against the Firestore emulator in the integration job. A retried
  create of a **task or a workflow** returns the first work (the issue run's
  lookup by firing id is S5's, and S5 tests it). A schedule paused between the query and the
  transaction fires nothing. Catch-up `skip` and `run_once` after a simulated
  3-hour gap write the expected records. The per-tenant cap of 5 holds.
* **S13.** `terraform test` asserts the account `swarm-schedule-tick` exists
  with `managed-by=swarm-terraform`, holds `run.invoker` on swarm-api and
  **no other binding**, and appears in the deployer's list. A swarm-api test
  shows the tick route answers 403 to the rollup sweeper's identity and to an
  admin, and 200 to `SCHEDULE_TICK_USERS`; and that `ROLLUP_SWEEPER_ROUTES` is
  unchanged.
* **S4.** `terraform test` asserts one `schedule_tick` job, `retry_count = 0`,
  the schedule-tick account's email, `managed-by=swarm-terraform` in its description, and
  the three indexes.
* **S11.** A member's `PATCH` carrying `platform` answers 403, and an
  admin's is recorded in `admin_audit`. A `hard_stop_paths` that drops one of
  the four defaults answers 422 naming it. Both fields round-trip through
  create, patch and read, and a registration stored before them reads as
  `platform: false` with the defaults.
* **S3.** Another tenant's schedule id answers 404 on every route. An unknown
  type answers 422 naming the available ones. A `params` extra key is refused
  by name.
* **S5.** Approving a stale digest answers 409. Two approvals of one item make
  one transition. A merge approval after a new push answers 409
  `merge_changed`. An expired plan ends `REJECTED` with reason "expired" and
  no task exists. The approvals route answers on a running app (`create_app()`
  mounts it). For the hard stops:
  - a planner plan naming `.github/workflows/` ends the run `FAILED` with
    `WORKFLOWS_PATH`, no workflow is submitted, and the issue gets the comment.
    A plan edit adding such a path answers 422;
  - a plan touching `terraform/bootstrap/` in a `platform: true` repository is
    held `NEEDS_OWNER`. A member's `POST /v1/runs/{id}/plan:approve` on it
    answers 403 `hold_approver_required`, and so does `swarm_plan_approve`;
    `PLATFORM_OWNER`'s approve succeeds;
  - the same plan in a repository that is not `platform: true` is held
    `NEEDS_SECOND_MEMBER`: its creator's approve answers 403 and another
    member's succeeds;
  - a `plan_approval: auto` run with a security-class issue or an IAM plan is
    **not** auto-approved by the tick: after `advance_run` it is still
    `PLANNED` with its `hold` set and nothing submitted;
  - an edit that removes the held path leaves the hold in place;
  - a held run with `auto_merge` does not merge until a `merge` approval from
    the hold's approvers.
* **S6.** SWEEP's tests pass unchanged against the executor. A dry-run sweep
  lists candidates and creates nothing. `observer` produces `report.md` from a
  fixture digest, offline with the mock runner.
* **S5, the switch.** A member who last edited the gate cannot switch the
  merge to `auto` (403) when the tenant has two members, and another member
  can; a one-person tenant needs the typed `confirm`; a `platform: true`
  repository answers 403 to a member and succeeds for `PLATFORM_OWNER`;
  switching back succeeds for anyone; each writes its audit entry. A run
  held by any §4.4 stop does not merge with the switch on, and `PATCH` with
  `gate.merge: auto` answers 422 `use_merge_switch`.
* **S12.** A comment from a verified, current member with the current digest
  approves, once, with `via: github_comment` in the audit. A stale digest,
  an edited comment, a bot, an unmapped login, a login whose member has left
  the tenant, and the last editor on a merge-tier item each approve nothing.
  A comment on a one-person tenant's own hold gets the console reply. Two
  comments racing with the console's approve make one transition.
* **S7.** The vitest for each screen covers its empty state (`Absent`), a
  partial spend (`Mark kind="partial"`) and a 390px layout. The issue forms
  test passes with the new section, and `nav.links.test.tsx` and
  `test_nav_headings_agree.py` accept the literal `automate` entry.
* **S8.** The MCP tool list test includes the new tools, and
  `swarm_schedule_create` refuses `image`.
* **S9.** On dev:
  - the issue sweep fires as a schedule and is visible in the console;
  - `repo-index-refresh` or `observer` fires from a second schedule;
  - a member of another tenant gets 404 for both;
  - a schedule with `run: approve` holds its firing until a member approves it
    in the inbox;
  - the migrated schedule is `merge: approve` and the merge waits in the
    inbox, and switching it to `auto` by a second member merges the next
    green, reviewed pull request, with the audit entry in the PR;
  - one plan is approved by a GitHub comment from a verified member;
  - job `bdb47d30` is deleted, with the firing id in the PR.

  These are #892's own acceptance criteria.

**Owner applies.** Under SD10 (**Owner decision 2026-10-08**: a dedicated
account), lane S13 needs two things from the owner, in this order:

1. **The bootstrap apply** that adds the `swarm-schedule-tick` account to what
   the deployer may create and act as (the file S0 names). It is the owner's
   to apply.
2. **A `dev-iam` approval** on the release whose IAM plan creates the account
   and its `run.invoker` binding on swarm-api. Post the held IAM rows before
   asking.

S4's job waits for both, because the job names the account. Indexes, metrics
and alerts are ordinary release resources. If S0 finds that `actions: read` is
missing, adding it to the GitHub App is an org-admin approval, which is the
owner's, and only S10's `ci-flake-hunter` and `release-health` wait for it.
Nothing else in this plan needs the owner to apply anything.

**One review** (credentials, tenant isolation, IAM) for S2 (the tick
submitting on behalf of members), S13 (the new account's IAM), S11 (who may
mark a repository `platform`), S5 (approvals, hard stops and the merge
switch), S12 (a GitHub login becoming an approval) and S10's
`custom-prompt`.

---

## 10. Decisions for the owner

The owner answered all eleven on **2026-10-08**. Each decision below is marked
**Owner decision 2026-10-08**, lists the options as they were asked (the
chosen one in bold, so the reasons the others lost stay on record), and names
the sections it changed. Nothing in this document may contradict a decision
here; where it does, the decision wins and the text is a defect.

### SD1. Where do schedules live in the console?

**Owner decision 2026-10-08: (c), a new spine section, Automate.** The spine
is Overview · Work · Automate · Capacity · Admin, and Automate holds Schedules
and the Approvals inbox. This is **not** the recommended option (a).

* (a) Work › Schedules, after Runs, with the approvals inbox as its second
  pane (P1). This was the recommendation.
* (b) Inside Runs, as a segmented control (P2).
* **(c) A new spine section, Automate (P3).**

Changed: the summary, §6.1 and the §6.2 table, §9's S7 lane and acceptance
(`SECTIONS` gains a section, so `test_issue_forms.py`'s "Where" list follows),
the mock-ups (P3 marked as the pick) and
[PICKS.md](web-ui/mockups/PICKS.md).

### SD2. Which types are built first?

**Owner decision 2026-10-08: (a).** `issue-sweep`, `issue-plan-only`,
`repo-index-refresh` and `observer`.

* **(a) `issue-sweep`, `issue-plan-only`, `repo-index-refresh`, `observer`.**
* (b) `issue-sweep`, `observer`, `cost-report`, `release-health`.
* (c) `issue-sweep`, `issue-plan-only`, `epic-triage`, `pr-shepherd`.

Lane SWEEP (PR 898, default off) is the first `issue-sweep` implementation,
and §8.1 says how it migrates. Changed: the summary, §0, §8.1, S6.

### SD3. The default gate per type

**Owner decision 2026-10-08: (a), plus a per-schedule switch to fully
automatic.**

**Follow-up, owner 2026-10-08:** in a repository registered `platform: true`, only
platform admins (the PR 860 admin roles, `PLATFORM_OWNER` included) may make the
switch; elsewhere the §4.6 rule applies (any member but the last editor).
Switching back to approval is always allowed.

* **(a) As in the §3 table:** the sweep plans automatically and **asks before
  merge**; plan-only, epic-triage and custom-prompt ask before plan or run;
  index, observer, flake, release-health and cost-report are auto; the
  `.github/workflows/` refusal and the security owner gate are enforced from
  day one, not report-only.
* (b) Stricter for the first two weeks.
* (c) Looser: the sweep merges automatically.

**The addition.** The console has a control, per schedule, to switch it from
approval-required to **fully automatic**: auto-merge on a green, reviewed
MERGE verdict. The switch is an audited edit and needs the same approver rule
as SD5. The hard stops (workflow files, IAM and bootstrap, the frozen
contract, security-class plans, budget, auto-pause) **cannot be switched off**.
So (c) is available, schedule by schedule, but never as a default.

Changed: §3 (floor column, §3.1 `merge`), §4.2 (the switch), §4.4 (what
cannot be switched off), §4.5, §7.1 (`:merge-mode`), §8.1 (the migrated
schedule starts at `merge: approve`), S5, S7 and their acceptance.

### SD4. Budget defaults

**Owner decision 2026-10-08: (a), per-type dollar caps for schedules only.**

* **(a) Per type, enforced as §4.3 says:**

  | type | per run | per day | concurrent |
  |---|---|---|---|
  | `issue-sweep` | $15 | $120 | 8 |
  | `issue-plan-only` | $3 | $30 | 5 |
  | `observer` | $5 | $10 | 1 |
  | `repo-index-refresh` | $5 | $40 | 4 |

  An unknown cost counts at the per-run cap.
* (b) No dollar figures at all, only `max_concurrent`.
* (c) One per-tenant daily schedules budget.

**The owner's 2026-10-01 "no dollar budgets" decision still holds for
non-scheduled work** (tasks, workflows and issue runs started by hand). These
figures bound what a schedule may start and nothing else. Changed: §0, §4.3,
S9 (cost-control.md records the carve-out). No measurement backs the figures.

### SD5. Who may approve?

**Owner decision 2026-10-08: (a), any member of the tenant, with a second
person for the merge tier.**

* **(a) Any member of the tenant.** A merge-tier approval needs someone other
  than the last editor of the gate, scope or spec when the tenant has more
  than one member.
* (b) Only the schedule's owner, plus the owner holds.
* (c) Only platform admins.
* (d) Members for R0-R2, admins for R3.

Changed: §4.6, and §4.2's switch, which uses the same rule.

### SD6. Catch-up after an outage

**Owner decision 2026-10-08: (c), per type, overridable per schedule.**

* (a) Always skip missed slots.
* (b) Always run the newest missed slot once.
* **(c) Per type:** `run_once` for reports and the index, `skip` for types
  that act on repositories. **Overridable per schedule** (`policy.catch_up`).

Changed: §1.1 (`policy`), §2.4.

### SD7. The repository index and platform-scope schedules

**Owner decision 2026-10-08: (a), with a note.**

* **(a) Keep `index.*` and the poll; show them as built-in rows in
  Automate.** Platform-scope schedules run in a tenant the owner names.
* (b) Fold the index's interval into a `repo-index-refresh` schedule per
  registration, and move the change trigger into the tick.

**The owner's note:** also allow a `repo-index-refresh` schedule for timed
full refreshes **even when the repository re-indexes on change.** Changed:
§3.3, §8.3.

### SD8. Types with no agent

**Owner decision 2026-10-08: (a).** `cost-report`, `release-health` and
`pr-shepherd`'s API actions run in swarm-api as firings with
`work.kind: api_action`, and use no capacity.

* **(a) Run in swarm-api.**
* (b) Every firing is a task.

Changed: §2.7. (§1.2 and §3 already named `api_action` and the `api` executor.)

### SD9. GitHub-comment approvals

**Owner decision 2026-10-08: (b), in the first build, not phase 2.**

* (a) Phase 2.
* **(b) In the first build.** `/swarmcloud approve <digest>`, including the
  GitHub-login-to-member mapping and its verification, and the poll that
  reads the comments.

Changed: the summary, §4.9 (the design), §9: new lane S12, and its
acceptance and review.

### SD10. The tick's identity

**Owner decision 2026-10-08: (b), a dedicated `swarm-schedule-tick` service
account.** This is not the recommended option (a).

* (a) Reuse `swarm-rollup-sweeper` and add one route. No owner apply.
* **(b) A dedicated `swarm-schedule-tick` account:** an owner bootstrap apply
  and a `dev-iam` release.

Changed: §2.1, §7.1, §9: new lane S13, S2 and S4 changed, and "Owner applies"
rewritten. The rollup sweeper's route set is not widened.

### SD11. Whose OK does a hold need outside the platform's own repositories?

**Owner decision 2026-10-08: (a).** Holds need the owner in the platform's
repositories (registration `platform: true`) and a second member of the tenant
elsewhere. A one-person `u-*` tenant approves its own hold after a typed,
audited confirmation.

* **(a) Owner-only in `platform: true` repositories; elsewhere a second
  member** (`approvers: second_member`, §4.6). A one-person `u-*` tenant
  approves its own after a typed confirmation, which is audited.
* (b) Owner-only everywhere, with one audited exception to §5.3.
* (c) Owner-only everywhere, with no exception.

Why (a) kept §5's isolation without exception: no admin, the owner included,
reads or decides another tenant's hold. Changed: §4.4 (the rows no longer wait
on the owner's answer), §4.6. S5 builds it.

---

## 11. Invariants, each with how it holds

1. **Demand only from `LEASED`…`RUNNING`.** A schedule, a firing, a queued
   firing, a pending approval and a `PLANNED` run are Firestore documents.
   A firing's work is created `QUEUED` or `READY` and takes capacity only at
   admission. Nothing in this design creates a pod, a lease or a slot. The
   tick is a request to swarm-api, not a worker.
2. **All-or-nothing reservation.** Unchanged: firings submit through the
   ordinary paths, and admission is untouched.
3. **Concurrency from `LEASED`.** Unchanged. `max_concurrent` counts the
   schedule's live work items from creation, which is stricter than `LEASED`
   and is a firing bound, not a pool.
4. **Workers never sleep through a wait.** No worker waits for an approval:
   the approval is before the work exists (run), between a planner task that
   ended and a workflow not yet created (plan), or after a run that is `DONE`
   (merge). The tick never waits inside a request beyond its 240 s budget.
5. **Fencing.** Unchanged for attempts. The firing's own fence is the creation
   of `schedule_firings/{id}:{slot}` in a transaction (§2.2), and every
   approval moves its subject in the transaction that checks its digest.
6. **No Spot.** Nothing here declares any.
7. **`requests == limits`.** Unchanged. Types name profiles whose classes are
   fixed.
8. **Checkpointing.** Unchanged for the work a firing creates. A firing has
   nothing to checkpoint: its state is its document.
9. **Isolation.** Every collection is tenant-scoped, with the 404 rule. Every
   firing submits as a current member, in its tenant, with its tenant's
   credentials and prefix. Platform-scope types read aggregates only and run
   in a named tenant. An admin can pause but cannot edit or approve another
   tenant's schedules.
10. **Profiles by name.** A caller picks a schedule **type** by name, and a
    type names its profile in code. No route here accepts an image, a
    command, a resource spec or a backend parameter. `custom-prompt`'s spec is
    the ordinary `WorkflowCreate`, which already refuses them.

---

## 12. Contract change requests

**None is required.** One is optional. It is written here as a request, for
the owner to decide whether to file it in
[contract-change-requests.md](contract-change-requests.md). This lane's
territory does not include that file.

### Request S-A (optional). `profiles.py`: a declared `context` input on `claude-code`, a swarm-api-composed data file staged into the workspace

**What is true today.** `claude-code` declares one input, `issue`, which the
worker stages as `issue.md` (contract request 28). Data that swarm-api
composes, like the issue run's open work, goes into the prompt between
delimiter lines, inside `MAX_PLANNER_PROMPT_BYTES` (64 KiB), because the
claude-code runner passes the prompt as one argv string and Linux refuses a
single argument over 128 KiB.

**Why.** `observer`'s digest over 168 hours, and `cost-report` if it ever runs
an agent, can exceed 48 KiB for a busy tenant. Then the digest is truncated,
and the report says so.

**The requested change.** `_CLI_AGENT_INPUTS` gains `context`: an object
reference, `{uri, sha256, bytes}`, to an object under the task's own tenant
prefix, written by swarm-api. The worker stages it as `context.json` after
checking the digest. The bound is 1 MiB. A caller may not set it. Like
`input_from`, it is a swarm-api reserved key.

**What it would break.** Nothing existing: the input is optional. Workers
built before it refuse it, so the rollout order is worker, then API.

**If it is declined.** `observer` keeps the 48 KiB prompt digest, truncated
with a stated count. That is enough for the 24-hour default.

**Invariants.** 9: the object is under the task's tenant prefix, read by that
tenant's worker. 10: it is data, not an image, a command or a resource spec.
Others are untouched.
