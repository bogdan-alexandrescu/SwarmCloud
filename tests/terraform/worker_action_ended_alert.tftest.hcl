# The four worker-action end causes -- merge_refused, merge_failed,
# verdict_refused, verdict_failed (contract requests 33 and 35) -- have a
# metric and an alert (#453, the #462 review).
#
# THE EMITTER: agent_worker.lifecycle `Worker._record_worker_action_end` logs,
# at ERROR, `worker action ended` with a top-level `end_cause`, for a refusal
# and for an outage's last attempt alike
# (tests/unit/worker/test_worker_action_starts_no_runner.py holds both).

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

run "the_metric_counts_the_four_end_causes_from_the_workers_line" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.message=\"worker action ended\"",
        "jsonPayload.end_cause=(\"merge_refused\" OR \"merge_failed\" OR \"verdict_refused\" OR \"verdict_failed\")",
        "\"cloud_run_job\"",
        "\"k8s_container\"",
      ] : strcontains(google_logging_metric.worker_action_ended.filter, f)
    ])
    error_message = "the metric must match the worker's `worker action ended` line, keyed on all four end causes, from Cloud Run Jobs and GKE pods alike"
  }

  # The same line carries cannot_start; it is not one of the four.
  assert {
    condition     = !strcontains(google_logging_metric.worker_action_ended.filter, "cannot_start")
    error_message = "the metric must count only the four worker-action end causes"
  }

  assert {
    condition = (
      google_logging_metric.worker_action_ended.label_extractors["end_cause"] == "EXTRACT(jsonPayload.end_cause)"
      && google_logging_metric.worker_action_ended.label_extractors["tenant_id"] == "EXTRACT(jsonPayload.labels.tenant_id)"
    )
    error_message = "end_cause is a top-level field of the line and tenant_id a bound label (StructuredLogger)"
  }

  assert {
    condition     = google_logging_metric.worker_action_ended.metric_descriptor[0].metric_kind == "DELTA" && google_logging_metric.worker_action_ended.metric_descriptor[0].value_type == "INT64"
    error_message = "a count of ends is a DELTA INT64 series"
  }

  assert {
    condition     = contains(output.log_metric_names, google_logging_metric.worker_action_ended.name)
    error_message = "the metric must be listed with the module's other log metrics"
  }
}

run "one_end_is_enough_to_alert_refused_and_failed_apart" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition     = length(google_monitoring_alert_policy.worker_action_ended[0].conditions) == 2 && google_monitoring_alert_policy.worker_action_ended[0].combiner == "OR"
    error_message = "the policy carries a refused condition and a failed condition, either of which fires it"
  }

  assert {
    condition = alltrue([
      for c in google_monitoring_alert_policy.worker_action_ended[0].conditions :
      strcontains(c.condition_threshold[0].filter, "metric.type = \"logging.googleapis.com/user/${google_logging_metric.worker_action_ended.name}\"")
      && strcontains(c.condition_threshold[0].filter, "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")")
      && c.condition_threshold[0].comparison == "COMPARISON_GT"
      && c.condition_threshold[0].threshold_value == 0
    ])
    error_message = "each condition watches this module's metric, carries the resource.type restriction Monitoring requires, and fires on a single end"
  }

  assert {
    condition = (
      strcontains(google_monitoring_alert_policy.worker_action_ended[0].conditions[0].condition_threshold[0].filter, "metric.label.end_cause = one_of(\"merge_refused\", \"verdict_refused\")")
      && strcontains(google_monitoring_alert_policy.worker_action_ended[0].conditions[1].condition_threshold[0].filter, "metric.label.end_cause = one_of(\"merge_failed\", \"verdict_failed\")")
    )
    error_message = "the first condition is the two _refused causes and the second the two _failed causes"
  }

  assert {
    condition     = google_monitoring_alert_policy.worker_action_ended[0].user_labels["managed-by"] == "swarm-terraform"
    error_message = "the policy must carry the marker make destroy keys on"
  }

  assert {
    condition     = contains(output.alert_policy_names, "swarm-dev-worker-action-ended")
    error_message = "the policy must be listed with the module's other alert policies"
  }
}
