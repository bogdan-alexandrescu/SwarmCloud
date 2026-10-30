# ---------------------------------------------------------------------------
# swarm-doctor: the hourly, read-only doctor (DAYONE-TF-2; D27, plan §5).
# ---------------------------------------------------------------------------
#
# A Cloud Run job, run every hour by Cloud Scheduler, that re-runs the
# install's checks against the live deployment and posts what it finds to
# swarm-api (Admin > Setup > Health). It CANNOT WRITE: it runs as swarm-doctor
# (terraform/bootstrap/platform_service_accounts.tf), whose project roles are
# read-only (terraform/bootstrap/doctor_roles.tf; tests/unit/scripts/
# test_terraform_day_one_edge.py reads each role's permissions). Its only
# reach beyond reading is run.invoker on swarm-api, to post its findings, and
# on its own job, so its schedule can start it.
#
# The swarm-verify image (no new image), with egress through the VPC as the
# verify job has it (lesson 35): its probes reach the cluster's private
# endpoint and swarm-api's internal ingress.
#
# OFF BY DEFAULT (enable_scheduled_doctor): nothing runs hourly before
# DAYONE-DOCTOR-1 ships swarm_install.scheduled_doctor in the swarm-verify
# image; the install engine turns it on for a release that carries it.

locals {
  doctor_on = var.enable_scheduled_doctor

  doctor_job_name = "swarm-doctor"
  doctor_email    = "${module.service_account_ids.doctor_id}@${var.project_id}.iam.gserviceaccount.com"
  doctor_member   = "serviceAccount:${local.doctor_email}"
}

# A plan against a project whose bootstrap has not made the account fails
# here, by name.
data "google_service_account" "doctor" {
  count = local.doctor_on ? 1 : 0

  project    = var.project_id
  account_id = module.service_account_ids.doctor_id
}

resource "google_cloud_run_v2_job" "doctor" {
  count = local.doctor_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = local.doctor_job_name

  labels = merge(local.labels, { component = "doctor" })

  deletion_protection = var.deletion_protection

  template {
    template {
      service_account = "${data.google_service_account.doctor[0].account_id}@${var.project_id}.iam.gserviceaccount.com"
      # The next hour's run is the retry; a doctor that retries inside the hour
      # posts the same finding twice.
      max_retries = 0
      timeout     = "900s"

      vpc_access {
        egress = "ALL_TRAFFIC"

        network_interfaces {
          network    = module.network.network_id
          subnetwork = module.network.subnetwork_id
        }
      }

      containers {
        image = local.image["swarm-verify"]
        # The image's ENTRYPOINT is `bash -lc`; this is the one command it runs.
        args = ["python -m swarm_install.scheduled_doctor"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "API_URL"
          value = module.cloud_run.service_urls["swarm-api"]
        }
        env {
          name  = "API_AUDIENCE"
          value = module.cloud_run.service_urls["swarm-api"]
        }
        env {
          name  = "PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "REGION"
          value = var.region
        }
        env {
          name  = "ENVIRONMENT"
          value = var.environment
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }

  depends_on = [module.project_services]
}

# Posting findings: run.invoker on swarm-api, on the service, nothing else.
resource "google_cloud_run_v2_service_iam_member" "doctor_invokes_api" {
  count = local.doctor_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = "swarm-api"
  role     = "roles/run.invoker"
  member   = local.doctor_member

  depends_on = [module.cloud_run]
}

# The schedule starts the job as the doctor itself: run.invoker on THIS job
# (run.jobs.run, no overrides), so no other identity gains a job grant.
resource "google_cloud_run_v2_job_iam_member" "doctor_scheduler_runs_job" {
  count = local.doctor_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.doctor[0].name
  role     = "roles/run.invoker"
  member   = local.doctor_member
}

resource "google_cloud_scheduler_job" "doctor_hourly" {
  count = local.doctor_on ? 1 : 0

  project     = var.project_id
  region      = var.region
  name        = "${var.name_prefix}-doctor-hourly"
  description = "managed-by=swarm-terraform; runs the read-only swarm-doctor job every hour (D27)"
  schedule    = "0 * * * *"
  time_zone   = "Etc/UTC"

  attempt_deadline = "60s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.doctor[0].name}:run"

    oauth_token {
      service_account_email = local.doctor_email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.doctor_scheduler_runs_job]
}

# The finding count, from the job's structured log (one line per finding,
# event "doctor_finding"), and an alert on any finding in the hour. Emailed to
# the deployment's alert channels (stage 13), so a broken grant is reported
# within the hour rather than at the next person's failed task.
resource "google_logging_metric" "doctor_findings" {
  count = local.doctor_on ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}/doctor-findings"
  filter  = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${local.doctor_job_name}\" AND jsonPayload.event=\"doctor_finding\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "doctor_findings" {
  count = local.doctor_on && var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-doctor-findings"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "the hourly doctor reported a finding"

    condition_threshold {
      filter          = "metric.type = \"logging.googleapis.com/user/${google_logging_metric.doctor_findings[0].name}\" AND resource.type = \"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = module.monitoring.notification_channels

  documentation {
    content   = "swarm-doctor reported at least one finding in the last hour. Admin > Setup > Health in the console lists them, each with the `swarm install --repair <check>` that fixes it."
    mime_type = "text/markdown"
  }

  user_labels = local.labels
}
