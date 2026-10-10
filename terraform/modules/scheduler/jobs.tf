# Cloud Scheduler: the ticks.

resource "google_cloud_scheduler_job" "safety_tick" {
  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-scheduler-tick"

  description = "managed-by=swarm-terraform; one-minute safety tick for the scheduler drain loop"
  schedule    = var.safety_tick_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # Has no effect here: Cloud Scheduler ignores attempt_deadline for a
  # pubsub_target, and a publish returns in milliseconds. It equals the default
  # one-minute interval rather than being shorter, and lowering it would change
  # nothing. tests/terraform/scheduler.tftest.hcl pins the value.
  attempt_deadline = "60s"

  pubsub_target {
    topic_name = google_pubsub_topic.wake.id
    data       = base64encode(jsonencode({ source = "cloud-scheduler", reason = "safety-tick" }))

    attributes = {
      source = "safety-tick"
    }
  }

  retry_config {
    retry_count          = 1
    min_backoff_duration = "5s"
    max_backoff_duration = "20s"
    max_doublings        = 1
  }
}

resource "google_cloud_scheduler_job" "reconciler" {
  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-reconciler-tick"

  description = "managed-by=swarm-terraform; reclaims expired leases and garbage-collects unused Job resources"
  schedule    = var.reconciler_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  attempt_deadline = "600s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.reconciler_endpoint, "/")}${var.reconciler_path}"
    body        = base64encode(jsonencode({ source = "cloud-scheduler" }))

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = var.tick_service_account
      audience              = var.reconciler_endpoint
    }
  }

  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
    max_doublings        = 2
  }
}

# Gated by an explicit boolean, never by `endpoint == ""`. The endpoint is a
# Cloud Run service URI, which is unknown until apply, and a `count` that
# depends on an unknown value cannot be planned at all -- terraform refuses
# rather than guessing how many instances to create.
resource "google_cloud_scheduler_job" "quota_refresh" {
  count = var.enable_quota_refresh ? 1 : 0

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-quota-refresh"

  description = "managed-by=swarm-terraform; recomputes provider quota state and adaptive pool targets"
  schedule    = var.quota_refresh_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.quota_broker_endpoint, "/")}${var.quota_broker_path}"
    body        = base64encode(jsonencode({ source = "cloud-scheduler" }))

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = var.tick_service_account
      audience              = coalesce(var.quota_broker_audience, var.quota_broker_endpoint)
    }
  }

  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }
}

# --- The workflow-rollup sweep (D17) ----------------------------------------
#
# POST /v1/admin/workflows/rollup (apps/swarm-api/swarm_api/routes/admin.py)
# converges the STORED Workflow.state of workflows nobody reads. Every read
# derives the state and writes it back, so a workflow somebody looks at is
# already right; this is for the rest, so "list my failed workflows" stays
# answerable for workflows nobody has opened (docs/workflows.md, "Workflow
# state"). Until this job nothing called the route on a schedule, and such a
# workflow kept a stale stored state for ever.
#
# ONE JOB PER REGISTERED TENANT, because the route takes exactly one tenant_id
# and deliberately will not derive it from the caller. Keyed on the tenant ids
# the root already knows at plan (the keys of var.tenants), never on anything
# computed. A personal `u-` tenant the API creates at runtime is not in that
# map and is not swept; its workflows still converge on every read.
#
# ITS OWN IDENTITY, NOT THE TICK. swarm-api lets this one address call this one
# route and nothing else (swarm_api.auth.ROLLUP_SWEEPER_ROUTES, via
# ROLLUP_SWEEPER_USERS); it holds no project role, and its one grant is
# run.invoker on swarm-api (terraform/infra main.tf, rollup_sweeper_invokes_api),
# without which Cloud Run's edge refuses it wherever api_invokers is not allUsers.
# The account id is spelled in modules/service_account_ids so terraform/bootstrap
# grants the release deployer serviceAccountAdmin on it, and terraform/infra's
# deployer.tf gives the deployer actAs on it, which creating a job that mints
# its OIDC token requires.

module "service_account_ids" {
  source = "../service_account_ids"
}

# NO create_ignore_already_exists, for the reason modules/iam gives for
# swarm-tick (#334): a release that meets a 409 should fail rather than adopt
# an account somebody else made under this name, with their keys and policy.
resource "google_service_account" "rollup_sweeper" {
  project      = var.project_id
  account_id   = module.service_account_ids.rollup_sweeper_id
  display_name = "Swarm Workflow Rollup Sweeper"
  description  = "managed-by=swarm-terraform; OIDC identity of the workflow-rollup, issue-run-advance, issue-sweep, stranded-pr-sweep, repo-index-poll, merge-wake, forge-refresh, workspace-sweep jobs and task_finished push; admitted to those nine /v1/admin routes only. No roles."
}

locals {
  # Built from the account id rather than read from `.email`, which a mock
  # provider (tests/terraform) leaves unknown at plan; the reference keeps the
  # job ordered after the account.
  rollup_sweeper_email = "${google_service_account.rollup_sweeper.account_id}@${var.project_id}.iam.gserviceaccount.com"
}

resource "google_cloud_scheduler_job" "workflow_rollup" {
  for_each = var.rollup_tenant_ids

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-workflow-rollup-${each.key}"

  description = "managed-by=swarm-terraform; converges the stored Workflow.state of tenant ${each.key}'s workflows nobody reads"
  schedule    = var.workflow_rollup_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # A sweep reads at most one page of live workflows and their step tasks; it
  # is bounded by the API's page size, not by the tenant's history.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/workflows/rollup?tenant_id=${urlencode(each.key)}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # A missed sweep costs nothing a reader can see -- every read derives -- so
  # one retry is enough; the next schedule is the real retry.
  retry_config {
    retry_count          = 1
    min_backoff_duration = "30s"
    max_backoff_duration = "120s"
  }
}

# --- The issue-run tick (#454) ------------------------------------------------
#
# POST /v1/admin/runs/advance (apps/swarm-api/swarm_api/routes/admin.py) moves
# one tenant's live issue runs as far as their tasks say: PLANNING -> PLANNED
# when the planner has written plan.json, an `auto` run's approval and its
# workflow, RUNNING -> DONE/FAILED/CANCELLED from that workflow, and the
# write-back to the GitHub issue after each. Every run READ does the same, but
# owner decision on #454 is "Advancing runs: swarm-api, on a Cloud Scheduler
# tick": without one, a run nobody watches -- and every `auto` run, whose point
# is that nobody has to -- never moved and its issue was never told.
#
# A PLANNED run waiting for a person is not read by the tick at all
# (swarm_api.issueruns.IssueRuns.tickable), and the tick creates nothing for
# it: a waiting plan holds no capacity (invariant 1).
#
# SAME TENANTS AND SAME IDENTITY as the workflow rollup above, for the same
# reasons: the route takes exactly one tenant_id, and swarm-api admits the
# rollup-sweeper account to it by name (swarm_api.auth.ROLLUP_SWEEPER_ROUTES)
# and to nothing wider. Its one grant, run.invoker on swarm-api, is already the
# rollup's (terraform/infra main.tf, rollup_sweeper_invokes_api). An auto
# approval the tick makes is submitted as the run's creator in the run's
# tenant (routes/runs.py run_owner_auth), never as this account.

resource "google_cloud_scheduler_job" "issue_run_advance" {
  for_each = var.rollup_tenant_ids

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-issue-run-advance-${each.key}"

  description = "managed-by=swarm-terraform; advances tenant ${each.key}'s issue runs (#454) nobody is reading"
  schedule    = var.issue_run_advance_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # One tick reads at most one page of a tenant's movable runs, oldest first,
  # and each run costs a task or workflow read plus, when it moved, one
  # GitHub write-back. Bounded by the API's page size.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/runs/advance?tenant_id=${urlencode(each.key)}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry: the schedule is every minute, so the next tick IS the retry,
  # and a retried tick overlapping the next one only doubles the reads (each
  # move is a transaction, so it can never double a transition).
  retry_config {
    retry_count = 0
  }
}

# --- The issue sweeper (owner decisions 2026-10-08) ---------------------------
#
# POST /v1/admin/issues/sweep (apps/swarm-api/swarm_api/routes/admin.py,
# swarm_api.issuesweep.sweep_tenant) lists one tenant's registered
# repositories' open issues with that tenant's token and starts an issue run
# -- plan_approval auto, auto_merge, two fix rounds -- for each candidate,
# oldest-updated first, until the tenant has its cap of live runs (default 8).
# Whether an issue is ready is the planner's call (NOT_READY); the sweep only
# skips what it can decide from the listing (docs/issue-runs.md "Sweeper").
#
# EVERY 30 MINUTES, OFF THE HOUR AND THE HALF HOUR (var.issue_sweep_schedule):
# a planner runs for minutes and a run for hours, so a fresh candidate waiting
# up to half an hour costs nothing anyone sees, while each sweep reads every
# registered repository's open issues and pull requests. :07 and :37 keep it
# clear of whatever else an operator schedules on :00 and :30.
#
# OFF UNTIL TWO SWITCHES SAY ON, neither of them this job: swarm-api's
# SWEEP_ENABLED (terraform/infra var.enable_issue_sweep) and the tenant's own
# `issue_sweep.enabled`. Until then each call answers which switch is off and
# starts nothing, so the job can exist from the start.
#
# SAME TENANTS AND SAME IDENTITY as the jobs above: the route takes exactly one
# tenant_id, and swarm-api admits the rollup-sweeper account to it by name
# (swarm_api.auth.ROLLUP_SWEEPER_ROUTES) -- and, like the repository poll,
# admits nobody else. Its one grant, run.invoker on swarm-api, is already the
# rollup's (terraform/infra main.tf, rollup_sweeper_invokes_api), so this job
# adds no IAM member. A run it starts is submitted as the registration's
# creator in that tenant (routes/admin.py registration_owner_auth), never as
# this account.

resource "google_cloud_scheduler_job" "issue_sweep" {
  for_each = var.rollup_tenant_ids

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-issue-sweep-${each.key}"

  description = "managed-by=swarm-terraform; issue_sweep: starts issue runs for tenant ${each.key}'s ready-looking open issues, up to its live-run cap (docs/issue-runs.md Sweeper)"
  schedule    = var.issue_sweep_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # The route stops starting work at 240 s (issuesweep.SWEEP_BUDGET_SECONDS)
  # and reports what it did not reach, so it answers inside this deadline.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/issues/sweep?tenant_id=${urlencode(each.key)}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry: a retry would read every repository again, and the next sweep
  # is half an hour away. A live run is never started twice for one issue --
  # the sweep skips an issue with a live run -- so an overlap only re-reads.
  retry_config {
    retry_count = 0
  }
}

# --- The stranded-PR sweep (part of #295) -------------------------------------
#
# POST /v1/admin/stranded-prs/sweep (apps/swarm-api/swarm_api/routes/
# strandedprs.py, swarm_api.strandedprs.sweep_tenant) lists one tenant's
# registered repositories' open pull requests with that tenant's token, keeps
# the ones that tenant's tasks opened, classifies every one open over two hours
# that nothing is going to merge (held, conflict, checks_red, ci_never_ran,
# behind, merge_failed, no_merge_step), stores the rows GET /v1/stranded-prs
# serves, and logs one `pr_stranded` entry per pull request per six hours --
# which the monitoring module's pr-stranded metric counts and alerts on. On
# 2026-10-09, 40 SwarmCloud pull requests were left open and nobody was told.
#
# REDRIVE FALSE, ALWAYS. The body says so explicitly, and swarm-api refuses
# `redrive: true` from this identity anyway: submitting merges is an admin's
# call (`POST ... {"redrive": true}`), so a leaked scheduler token reads and
# logs, and submits nothing.
#
# EVERY 30 MINUTES, AT :19 AND :49 (var.stranded_pr_sweep_schedule): clear of
# :00 and :30, where schedules bunch, and of the issue sweep's :07 and :37, so
# the two reads of every registered repository with one tenant token do not
# land in the same minute. A pull request is stranded only after two hours, so
# half an hour more before anyone is told costs nothing anyone sees.
#
# SAME TENANTS AND SAME IDENTITY as the jobs above: the route takes exactly one
# tenant_id, and swarm-api admits the rollup-sweeper account to it by name
# (swarm_api.auth.ROLLUP_SWEEPER_ROUTES). NOT the platform tick (swarm-tick):
# swarm-api admits only ROLLUP_SWEEPER_USERS to the sweeper routes, so a job
# presenting swarm-tick would be refused 403 on every call. Its one grant,
# run.invoker on swarm-api, is already the rollup's (terraform/infra main.tf,
# rollup_sweeper_invokes_api), so this job adds no IAM member.

resource "google_cloud_scheduler_job" "stranded_pr_sweep" {
  for_each = var.rollup_tenant_ids

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-stranded-pr-sweep-${each.key}"

  description = "managed-by=swarm-terraform; stranded_pr_sweep: finds tenant ${each.key}'s SwarmCloud pull requests nothing is going to merge and logs pr_stranded (redrive false)"
  schedule    = var.stranded_pr_sweep_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # The route stops reading GitHub at 240 s (strandedprs.SWEEP_BUDGET_SECONDS)
  # and reports `truncated`, so it answers inside this deadline.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/stranded-prs/sweep?tenant_id=${urlencode(each.key)}"
    body        = base64encode(jsonencode({ redrive = false }))
    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry: a retry would read every repository again, and the next sweep
  # is half an hour away. The log dedupe is per pull request, so an overlap
  # does not page twice inside six hours.
  retry_config {
    retry_count = 0
  }
}

# --- The repository index poll (docs/repo-index.md §3.3, lane RI4) ------------
#
# POST /v1/admin/repositories/poll (apps/swarm-api/swarm_api/routes/admin.py,
# swarm_api.repoindex.RepoIndex.poll) reads, for each of one tenant's
# registered repositories, the default branch's head with the last response's
# ETag -- GitHub answers an unchanged branch 304, which costs no rate limit --
# and queues an index run where the head moved and the minimum change interval
# has passed, or where `interval_hours` has passed since the last one. A run in
# flight is never duplicated: a newer head is recorded as pending and indexed
# once the running one ends. Without this job a repository is indexed only
# when a person presses "Index now".
#
# An index run is an ordinary task, queued by profile name and admitted like
# any other (invariants 1-3, 10): the job creates no capacity and holds none.
#
# SAME TENANTS AND SAME IDENTITY as the two jobs above: the route takes exactly
# one tenant_id, and swarm-api admits the rollup-sweeper account to it by name
# (swarm_api.auth.ROLLUP_SWEEPER_ROUTES) -- and, unlike the rollup, admits
# nobody else, admins included (§6.1). Its one grant, run.invoker on
# swarm-api, is already the rollup's (terraform/infra main.tf,
# rollup_sweeper_invokes_api), so this job adds no IAM member. A run it queues
# is submitted as the registration's creator in that tenant
# (routes/admin.py registration_owner_auth), never as this account.

resource "google_cloud_scheduler_job" "repo_index_poll" {
  for_each = var.rollup_tenant_ids

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-repo-index-poll-${each.key}"

  description = "managed-by=swarm-terraform; repo_index_poll: reads tenant ${each.key}'s registered repositories' heads and queues their index runs (docs/repo-index.md §3.3)"
  schedule    = var.repo_index_poll_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # One forge GET per registration, each bounded by the client's timeout; the
  # route stops starting reads at 240 s (repoindex.POLL_BUDGET_SECONDS) and
  # reports what it did not reach, so it answers inside this deadline.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/repositories/poll?tenant_id=${urlencode(each.key)}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry: the next tick IS the retry, five minutes later. Every queueing
  # is a claim in a Firestore transaction, so an overlapping retry could never
  # queue a second run of one registration; it would only repeat the reads.
  retry_config {
    retry_count = 0
  }
}

# --- The merge step's wake (docs/merge-step.md "Revised 2026-10-06" §1, MS2) --
#
# POST /v1/admin/merges/wake (apps/swarm-api/swarm_api/routes/admin.py,
# swarm_api.mergewake.wake_tenant) reads, for each of one tenant's merge steps
# PARKED on CI_PENDING, the pull request and its checks at the head the step
# parked at, with that tenant's own -git token, and marks the ones whose checks
# have settled (`metadata.merge_wait.wake_requested_at`). The scheduler's
# `_promote_ci_waits` returns a marked park to READY; only admission takes
# capacity (invariants 1-3). The platform has no webhook receiver, so this
# re-read IS the signal that CI has finished; without it a parked merge waits
# for its fallback instant (15 minutes) instead of about a minute.
#
# SAME TENANTS AND SAME IDENTITY as the jobs above: the route takes exactly one
# tenant_id, and swarm-api admits the rollup-sweeper account to it by name
# (swarm_api.auth.ROLLUP_SWEEPER_ROUTES) and to nothing wider. Its one grant,
# run.invoker on swarm-api, is already the rollup's (terraform/infra main.tf,
# rollup_sweeper_invokes_api), so this job adds no IAM member. The token it
# reads is swarm-api's existing per-secret accessor grant on the tenant's -git
# secret (terraform/modules/secret_manager), the same the issue-run CI loop
# uses.

resource "google_cloud_scheduler_job" "merge_wake" {
  for_each = var.rollup_tenant_ids

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-merge-wake-${each.key}"

  description = "managed-by=swarm-terraform; merge_wake: marks tenant ${each.key}'s CI-waiting merge steps whose checks have settled (docs/merge-step.md, 2026-10-06)"
  # Every minute: a parked merge waits on this read between its CI settling
  # and its wake. A tick with no CI_PENDING park costs one Firestore query and
  # reads no token; each park is read at most once per CI_READ_SECONDS (30 s).
  schedule  = "* * * * *"
  time_zone = var.time_zone
  paused    = var.paused

  # One page of parks, a few GETs each, each bounded by the writer's timeout.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/merges/wake?tenant_id=${urlencode(each.key)}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry: the next tick IS the retry, a minute later. Each mark is a
  # guarded transaction, so an overlapping retry could only repeat the reads.
  retry_config {
    retry_count = 0
  }
}

# --- The GitHub user-token refresher (docs/onboarding.md §3.4 item 6, D2) ----
#
# POST /v1/admin/forge/refresh on swarm-api refreshes every connected user's
# GitHub user access token that is near expiry, with the App's client secret
# and the user's refresh token (its `-refresh` twin slot), and publishes the
# new access token as a version of the user's slot, so the worker's read at
# runtime always finds one at least two hours from expiry (§3.3 step 3).
# Owner decision D2 (2026-10-07): swarm-api does this on a Cloud Scheduler
# sweep, behind an interface a dedicated broker can take over later -- which
# would change this job's endpoint and identity, and nothing else here.
#
# ONE JOB, NOT ONE PER TENANT. The sweep walks the forge_connections due for a
# refresh across tenants and takes each one's `refresh_lease`, so one
# refresher at a time spends a refresh token that works once. A per-tenant
# job would only multiply the walk.
#
# SAME IDENTITY as the per-tenant jobs above: the rollup-sweeper account,
# whose one grant is run.invoker on swarm-api (terraform/infra main.tf,
# rollup_sweeper_invokes_api), so this job adds no IAM member. swarm-api must
# admit it to this one route (swarm_api.auth.ROLLUP_SWEEPER_ROUTES), which the
# route's own lane (OB3) does.
#
# OFF UNTIL enable_forge_refresh, because the route ships with OB3: a job
# calling a route swarm-api does not serve answers 404 every 15 minutes and
# refreshes nothing. Turning it on is one tfvars line once OB3 is deployed
# (docs/runbooks/github-app.md, step 7). A bool, never `api_endpoint != ""`:
# the endpoint is unknown until apply, and a count cannot depend on it.

resource "google_cloud_scheduler_job" "forge_refresh" {
  count = var.enable_forge_refresh ? 1 : 0

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-forge-refresh"

  description = "managed-by=swarm-terraform; refreshes GitHub user access tokens before they expire"
  schedule    = var.forge_refresh_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # A user access token lives 8 hours and the sweep refreshes it with at least
  # two left, so a sweep that runs out of time leaves the rest to the next
  # tick, 15 minutes on. Each refresh is one GitHub POST and two Secret
  # Manager writes.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}${var.forge_refresh_path}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry. A refresh token works once and is rotated by its use: a retry
  # racing the attempt it retries could spend the new token before the first
  # attempt had persisted it. The refresh_lease guards against that inside
  # swarm-api; not retrying keeps the scheduler from testing it every time.
  # The next tick IS the retry.
  retry_config {
    retry_count = 0
  }
}

# --- The schedule tick (docs/schedules.md §2.1, lane S4) ----------------------
#
# POST /v1/admin/schedules/tick (apps/swarm-api/swarm_api/routes/schedule_tick.py,
# lane S2) claims every due slot of every enabled schedule, as one firing per
# slot created inside the transaction that advances the schedule (§2.2), and
# then creates the firing's work as the schedule's owner in the schedule's
# tenant (§2.7). A schedule is data; this one job is the only GCP resource
# schedules ever need, so editing a schedule is never an infrastructure change.
#
# ONE JOB, NOT ONE PER TENANT, and NO tenant_id: the route reads
# `schedules where state == enabled and next_run_at <= now` across every tenant,
# personal `u-` ones included, which a job keyed on var.tenants would miss
# (§2.1). The query's composite index is modules/firestore's
# "schedules-state-next-run".
#
# ITS OWN IDENTITY, lane S13's account (owner decision SD10, 2026-10-08), made
# in main.tf beside this file: swarm-api admits it to this one route
# (auth.SCHEDULE_TICK_ROUTES through SCHEDULE_TICK_USERS) and its one grant is
# run.invoker on swarm-api (terraform/infra main.tf, schedule_tick_invokes_api).
# The OIDC token below is minted BY the account; it is not a grant TO it, so
# this job adds no IAM member, and tests/terraform/schedule_tick_identity.tftest.hcl
# still finds exactly one grant. The deployer's actAs on the account, which
# creating a job that mints its token needs, is S13's (terraform/infra
# deployer.tf).
#
# NO RETRY (§2.1). The next minute's tick is the retry, and every claim is a
# transaction keyed on the slot, so an overlapping retry repeats reads and never
# a firing; a Cloud Scheduler retry would only add a second caller to that race.
#
# Stopped two ways (§2.11): var.paused stops calling the route at all, and
# swarm-api's SCHEDULES_ENABLED=false makes the route answer {disabled: true}.
# Either way the catch-up policy decides what the missed slots do on resume.
resource "google_cloud_scheduler_job" "schedule_tick" {
  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-schedule-tick"

  description = "managed-by=swarm-terraform; fires every tenant's due schedules, one firing per slot (docs/schedules.md)"
  schedule    = var.schedule_tick_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # The route stops STARTING work at 240 s (schedulefire.TICK_BUDGET_SECONDS,
  # the repository poll's budget and reason); the deadline leaves it room to
  # answer, as repo_index_poll's does.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/schedules/tick"

    # Not coalesce(), as the per-tenant jobs use: those are made per
    # rollup_tenant_ids, which is empty wherever api_endpoint is, while this
    # job is unconditional, and coalesce() of two empty strings is an error.
    # The root always passes swarm-api's URL (terraform/infra main.tf).
    oidc_token {
      service_account_email = local.schedule_tick_email
      audience              = var.api_audience != "" ? var.api_audience : trimsuffix(var.api_endpoint, "/")
    }
  }

  retry_config {
    retry_count = 0
  }
}

# --- The personal-workspace dispatch sweep (docs/workspaces.md §2.2, #847) ---
#
# POST /v1/admin/workspaces/sweep on swarm-api publishes again the workspace id
# of an `approved` record whose publish failed or whose build never claimed
# it, and is the DETECTOR for a record that waits: it logs one
# `workspace_stuck` entry per stuck record per hour (reason publishing_off,
# never_dispatched or dispatched_unclaimed), which modules/monitoring counts
# and pages on. On 2026-10-09 an approved request sat 19 hours with nothing
# calling this route and nothing saying so; this job is the caller.
#
# ALWAYS ON, unlike forge_refresh: with WORKSPACE_APPLY_PUBLISH off the sweep
# publishes nothing, but it still finds and logs every approved record, so the
# wait is paged rather than silent. The count is enable_workspace_sweep, a
# bool for #748's reason (enable_task_finished_push): api_endpoint is unknown
# at plan in the root, so it cannot gate a count, and a module planned on its
# own with no endpoint must not build a job that calls nowhere. The root sets
# it to a literal true, not to a tfvars variable, so no environment can switch
# the detector off with the thing it detects.
#
# ONE JOB, NOT ONE PER TENANT: the route takes no tenant_id. A personal
# workspace is no tenant of var.tenants (docs/workspaces.md §3.2), and the
# sweep walks every approved record by its opaque id.
#
# SAME IDENTITY as forge_refresh and the per-tenant jobs above: the
# rollup-sweeper account. swarm-api admits that address -- ROLLUP_SWEEPER_USERS
# -- to this route (swarm_api.auth.ROLLUP_SWEEPER_ROUTES), and its run.invoker
# on swarm-api (terraform/infra main.tf, rollup_sweeper_invokes_api) is the
# edge grant, so this job adds no IAM member. NOT swarm-tick, which
# docs/workspaces.md §2.2 named: swarm-api admits swarm-tick to no admin
# route, and it holds no run.invoker on swarm-api, so every call would be
# refused.
resource "google_cloud_scheduler_job" "workspace_sweep" {
  count = var.enable_workspace_sweep ? 1 : 0

  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-workspace-sweep"

  description = "managed-by=swarm-terraform; re-dispatches approved personal workspaces nobody built and logs each one waiting as workspace_stuck"
  schedule    = var.workspace_sweep_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # One page of approved records and at most one publish each; bounded by
  # the API's page size, like the rollup sweep.
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.api_endpoint, "/")}${var.workspace_sweep_path}"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  # No retry: the next tick, ten minutes on, IS the retry. A retry racing the
  # attempt it retries could publish one record twice; the route's own
  # once-per-10-minutes rule absorbs that, but not retrying keeps it from being
  # tested every time.
  retry_config {
    retry_count = 0
  }

  lifecycle {
    precondition {
      condition     = var.api_endpoint != ""
      error_message = "enable_workspace_sweep needs api_endpoint: the job POSTs swarm-api's dispatch sweep."
    }
  }
}
