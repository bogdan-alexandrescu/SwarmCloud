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
  description  = "managed-by=swarm-terraform; OIDC identity of the per-tenant workflow-rollup, issue-run-advance, repo-index-poll and merge-wake jobs, and of the task_finished push to swarm-api. swarm-api admits it to those five /v1/admin routes only. No project roles."
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
