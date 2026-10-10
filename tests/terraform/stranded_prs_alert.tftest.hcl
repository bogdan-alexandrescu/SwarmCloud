# Stranded pull requests have a metric and an alert (part of #295).
#
# THE EMITTER: swarm_api.strandedprs._log_stranded logs, at WARNING, one line
# per stranded pull request per six hours, with `extra={"event": "pr_stranded",
# "tenant_id": ..., "reason": ...}`, which swarm_common.logging_setup writes at
# the TOP LEVEL of the JSON payload (tests/unit/control_plane/
# test_stranded_prs.py holds the line to those names).

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

run "the_metric_counts_swarm_apis_pr_stranded_line" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.event=\"pr_stranded\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=(\"swarm-api\")",
      ] : strcontains(google_logging_metric.pr_stranded.filter, f)
    ])
    error_message = "the metric must match swarm-api's `pr_stranded` line, from the swarm-api Cloud Run service only"
  }

  assert {
    condition     = !strcontains(google_logging_metric.pr_stranded.filter, "swarm-scheduler") && !strcontains(google_logging_metric.pr_stranded.filter, "jsonPayload.labels.")
    error_message = "only swarm-api writes the line, and its fields are top level (CloudLoggingFormatter), not under `labels`"
  }

  assert {
    condition = (
      google_logging_metric.pr_stranded.label_extractors["tenant_id"] == "EXTRACT(jsonPayload.tenant_id)"
      && google_logging_metric.pr_stranded.label_extractors["reason"] == "EXTRACT(jsonPayload.reason)"
    )
    error_message = "tenant_id and reason are top-level fields of the line"
  }

  assert {
    condition     = google_logging_metric.pr_stranded.metric_descriptor[0].metric_kind == "DELTA" && google_logging_metric.pr_stranded.metric_descriptor[0].value_type == "INT64"
    error_message = "a count of stranded pull requests is a DELTA INT64 series"
  }

  assert {
    condition     = contains(output.log_metric_names, google_logging_metric.pr_stranded.name) && output.log_metric_filters[google_logging_metric.pr_stranded.name] == google_logging_metric.pr_stranded.filter
    error_message = "the metric must be listed with the module's other log metrics"
  }
}

run "one_stranded_pull_request_is_enough_to_alert" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      length(google_monitoring_alert_policy.pr_stranded) == 1
      && google_monitoring_alert_policy.pr_stranded[0].severity == "WARNING"
      && google_monitoring_alert_policy.pr_stranded[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.pr_stranded[0].conditions[0].condition_threshold[0].threshold_value == 0
    )
    error_message = "one pr_stranded entry fires the policy, at WARNING: somebody has to look"
  }

  assert {
    condition = (
      strcontains(google_monitoring_alert_policy.pr_stranded[0].conditions[0].condition_threshold[0].filter, "resource.type = \"cloud_run_revision\"")
      && strcontains(google_monitoring_alert_policy.pr_stranded[0].conditions[0].condition_threshold[0].filter, "logging.googleapis.com/user/swarm/pr-stranded")
    )
    error_message = "the condition reads the pr-stranded metric with the resource.type restriction Monitoring requires"
  }

  assert {
    condition     = google_monitoring_alert_policy.pr_stranded[0].user_labels["managed-by"] == "swarm-terraform"
    error_message = "the policy carries managed-by=swarm-terraform, which make destroy keys on"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.pr_stranded[0].documentation[0].content, "GET /v1/stranded-prs") && contains(output.alert_policy_names, "swarm-dev-pr-stranded")
    error_message = "the alert says where the rows are, and is listed with the module's other policies"
  }
}

run "no_alert_when_alerts_are_off" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    create_alerts = false
  }

  assert {
    condition     = length(google_monitoring_alert_policy.pr_stranded) == 0
    error_message = "create_alerts = false creates no stranded-PR policy, as for every other"
  }
}
