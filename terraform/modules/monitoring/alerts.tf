# Alert policies.
#
# Every policy here fires on something a human would actually act on. Note what
# is deliberately NOT alerted: a large PARKED or QUEUED backlog. Those states
# cost nothing (CONTRACT.md invariant 1), so paging on them would train the team
# to ignore the pager on exactly the days the platform is working as designed.

locals {
  notification_channels = concat(
    [for c in google_monitoring_notification_channel.email : c.id],
    var.extra_notification_channels,
  )

  alert_docs_suffix = "\n\nEnvironment: ${var.environment}. Managed by swarm-terraform; edit terraform/modules/monitoring rather than the console."

  # The project is shared. Naming the four swarm services explicitly, and pinning
  # the region, keeps these policies off another team's Cloud Run services --
  # a prefix match would catch anything they happen to call swarm-something.
  swarm_services_filter = "resource.labels.service_name = one_of(${join(", ", [for s in var.service_names : "\"${s}\""])})"
  region_filter         = "resource.labels.location = \"${var.region}\""
}

resource "google_monitoring_notification_channel" "email" {
  for_each = toset(var.alert_emails)

  project      = var.project_id
  display_name = "swarm-${var.environment}-${replace(each.value, "@", "-at-")}"
  type         = "email"

  labels = {
    email_address = each.value
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# The one-minute safety tick stopped.
#
# This is the single most important policy in the file. Pub/Sub is the fast wake
# path; the tick is what bounds staleness when Pub/Sub fails. If the tick dies
# quietly, the platform looks healthy right up until someone notices their tasks
# have been READY for an hour.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "safety_tick_absent" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-safety-tick-stopped"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "No scheduler tick attempt in 10 minutes"

    condition_absent {
      filter = join(" AND ", [
        "resource.type = \"cloud_scheduler_job\"",
        "resource.labels.job_id = \"${var.safety_tick_job}\"",
        "metric.type = \"cloudscheduler.googleapis.com/job/attempt_count\"",
      ])

      duration = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "The one-minute safety tick has not run for ten minutes. Pub/Sub may still be waking the scheduler, so the queue is not necessarily stalled -- but the bound on staleness is gone. Check the Cloud Scheduler job and the swarm-scheduler service logs.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Control-plane 5xx.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "service_errors" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-control-plane-5xx"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "5xx rate above threshold on a swarm Cloud Run service"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"run.googleapis.com/request_count\"",
        "metric.labels.response_code_class = \"5xx\"",
        local.swarm_services_filter,
        local.region_filter,
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.error_rate_threshold
      duration        = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "A swarm control-plane service is returning 5xx. Check the revision logs; a Firestore contention storm and a bad deploy look the same from here, so compare against the admission-denied metric before rolling back.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Execution failures.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "job_failures" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-job-executions-failing"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Failed Cloud Run Job executions"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "metric.type = \"run.googleapis.com/job/completed_execution_count\"",
        "metric.labels.result = \"failed\"",
        "resource.labels.job_name = starts_with(\"${var.name_prefix}-\")",
        local.region_filter,
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.job_failure_threshold
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.job_name"]
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "Agent executions are failing faster than the retry policy absorbs. Look at the attempt records in Firestore for exit codes and at the peak-RSS metric for OOM near misses before assuming the runner image is at fault.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Fencing races. Should be flat zero.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "generation_fenced" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-generation-fencing"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Workers exiting on a stale fencing generation"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.events["generation_fenced"].name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "Fencing is doing its job, but it should rarely have to. A sustained rate means two attempts for the same task are being dispatched -- look for a reconciler reclaiming leases that were still alive, or a dispatch timeout set below real cold-start time.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Dead letters: tasks and wake messages.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "dead_letter_backlog" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-wake-messages-dead-lettered"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "Undelivered messages on the wake dead-letter subscription"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_subscription\"",
        "resource.labels.subscription_id = \"${var.dead_letter_subscription}\"",
        "metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "Wake messages are being dead-lettered, so the scheduler push endpoint is rejecting them. The one-minute safety tick is carrying the platform in the meantime; latency is degraded, throughput is not.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

resource "google_monitoring_alert_policy" "wake_backlog" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-scheduler-not-draining"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "Wake subscription backlog sustained"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_subscription\"",
        "resource.labels.subscription_id = \"${var.wake_subscription}\"",
        "metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.undelivered_wake_threshold
      duration        = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "Wake messages are arriving faster than the scheduler acknowledges them. Check whether the drain loop is hitting its bound every pass -- that is the expected shape under genuine saturation and is not itself a fault.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Checkpointing stopped.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "tasks_dead_lettered" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-tasks-dead-lettered"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Tasks exhausted every attempt"

    condition_threshold {
      # A dead-letter can be written by the worker (Cloud Run Jobs or a GKE pod)
      # or by the scheduler (a Cloud Run service), so the restriction names all
      # three. Monitoring REQUIRES a resource.type restriction, and naming only
      # one would silently drop the other two sources.
      filter = join(" AND ", [
        "resource.type = one_of(\"cloud_run_job\", \"cloud_run_revision\", \"k8s_container\")",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.events["dead_lettered"].name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "600s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.labels.tenant_id"]
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "Tasks have exhausted their attempts and been dead-lettered. These never retry on their own; somebody has to look at them.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}
