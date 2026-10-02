# Cloud Scheduler: the ticks.

resource "google_cloud_scheduler_job" "safety_tick" {
  project = var.project_id
  region  = var.region
  name    = "${var.name_prefix}-scheduler-tick"

  description = "managed-by=swarm-terraform; one-minute safety tick for the scheduler drain loop"
  schedule    = var.safety_tick_schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # Shorter than the schedule interval: a tick that has not started within a
  # minute is superseded by the next one rather than piling up.
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
  description  = "managed-by=swarm-terraform; OIDC identity of the workflow-rollup Cloud Scheduler jobs. swarm-api admits it to POST /v1/admin/workflows/rollup only. No project roles."
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
