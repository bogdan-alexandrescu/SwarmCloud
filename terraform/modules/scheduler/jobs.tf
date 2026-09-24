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
