# The safety-tick-stopped alert, which dev had switched off since 2026-09-19
# waiting for `cloudscheduler.googleapis.com/job/attempt_count` to appear.
#
# Google publishes no Cloud Scheduler metric (checked 2026-09-24 against the
# published metric list and the project's own descriptors), so it never would
# have. The alert now watches a logs-based metric built from Cloud Scheduler's
# own attempt logs, which terraform creates in the same apply. These runs pin
# the two things that made the old version impossible to create and the one
# thing that would make the new one quietly wrong.

mock_provider "google" {}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  labels      = { "managed-by" = "swarm-terraform" }

  service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
  wake_subscription        = "swarm-scheduler-wake-sub"
  dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
  safety_tick_job          = "swarm-scheduler-tick"
}

run "the_tick_alert_watches_a_metric_this_configuration_creates" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  # A metric type Google does not publish cannot be referenced by an alert
  # policy at all: `Error 404: Cannot find metric(s)`, at apply, every apply.
  assert {
    condition     = !strcontains(google_monitoring_alert_policy.safety_tick_absent[0].conditions[0].condition_absent[0].filter, "cloudscheduler.googleapis.com/")
    error_message = "the tick alert watches a Cloud Scheduler metric; Google publishes none, so the policy can never be created"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.safety_tick_absent[0].conditions[0].condition_absent[0].filter, "metric.type = \"logging.googleapis.com/user/${google_logging_metric.safety_tick_attempts.name}\"")
    error_message = "the tick alert must watch the logs-based metric this module creates, so the metric exists before the policy does"
  }

  # A logs-based metric keeps the log entry's monitored resource, so the job
  # filter still works -- and still has to be there, or a second scheduler
  # job's attempts would keep the alert quiet while the tick is dead.
  assert {
    condition = alltrue([
      for f in [
        "resource.type = \"cloud_scheduler_job\"",
        "resource.labels.job_id = \"swarm-scheduler-tick\"",
      ] : strcontains(google_monitoring_alert_policy.safety_tick_absent[0].conditions[0].condition_absent[0].filter, f)
    ])
    error_message = "the absence alert must be pinned to the tick job's own series"
  }
}

run "the_metric_counts_only_delivered_ticks_of_the_tick_job" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  # The shape Cloud Scheduler actually writes, read from this project's logs on
  # 2026-09-24: one AttemptFinished per attempt under
  # cloudscheduler.googleapis.com/executions; a failed one carries `status`
  # (swarm-quota-refresh, 2026-09-20: status "INTERNAL", severity ERROR).
  assert {
    condition = alltrue([
      for f in [
        "logName=\"projects/saga-agents-staging/logs/cloudscheduler.googleapis.com%2Fexecutions\"",
        "resource.type=\"cloud_scheduler_job\"",
        "resource.labels.job_id=\"swarm-scheduler-tick\"",
        "resource.labels.location=\"us-central1\"",
        "jsonPayload.\"@type\"=\"type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished\"",
      ] : strcontains(google_logging_metric.safety_tick_attempts.filter, f)
    ])
    error_message = "the tick metric must count finished attempts of the tick job, in its region, from Cloud Scheduler's execution log"
  }

  # A tick whose publish fails every minute never reaches the scheduler. If
  # failed attempts counted, the alert would stay quiet through exactly the
  # outage it exists for.
  assert {
    condition     = strcontains(google_logging_metric.safety_tick_attempts.filter, "NOT jsonPayload.status:*")
    error_message = "failed attempts (AttemptFinished with a status) must not count as a delivered tick"
  }

  assert {
    condition     = google_logging_metric.safety_tick_attempts.metric_descriptor[0].metric_kind == "DELTA" && google_logging_metric.safety_tick_attempts.metric_descriptor[0].value_type == "INT64"
    error_message = "a counter of attempts is a DELTA INT64 series; the absence alert aligns it with ALIGN_SUM"
  }
}
