# A stored tenant namespace that is not the canonical one has a metric and an
# alert (owner decision 2026-10-11; part of #847).
#
# THE EMITTERS: scheduler.dispatch.verified_namespace and
# reconciler.backends.verified_namespace log, once per tenant per process per
# hour, a line with `event = "tenant_namespace_mismatch"` and `tenant_id` at the
# TOP LEVEL of the JSON payload, at ERROR when the stored value was ignored and
# at WARNING when it was kept (tests/unit/control_plane/
# test_reconciler_gke_namespaced.py holds both lines to those names).

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

run "the_metric_counts_the_scheduler_and_reconciler_mismatch_line" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.event=\"tenant_namespace_mismatch\"",
        "resource.type=\"cloud_run_revision\"",
        "\"swarm-scheduler\"",
        "\"swarm-reconciler\"",
      ] : strcontains(google_logging_metric.tenant_namespace_mismatch.filter, f)
    ])
    error_message = "the metric must match the tenant_namespace_mismatch line from the scheduler and the reconciler"
  }

  assert {
    condition     = !strcontains(google_logging_metric.tenant_namespace_mismatch.filter, "\"swarm-api\"") && !strcontains(google_logging_metric.tenant_namespace_mismatch.filter, "jsonPayload.labels.")
    error_message = "only the scheduler and the reconciler write the line, and its fields are top level, not under `labels`"
  }

  assert {
    condition = (
      google_logging_metric.tenant_namespace_mismatch.label_extractors["tenant_id"] == "EXTRACT(jsonPayload.tenant_id)"
      && google_logging_metric.tenant_namespace_mismatch.label_extractors["severity"] == "EXTRACT(severity)"
    )
    error_message = "tenant_id is a top-level field of the line; severity says whether the stored value was ignored or kept"
  }

  assert {
    condition     = contains(output.log_metric_names, google_logging_metric.tenant_namespace_mismatch.name) && output.log_metric_filters[google_logging_metric.tenant_namespace_mismatch.name] == google_logging_metric.tenant_namespace_mismatch.filter
    error_message = "the metric must be listed with the module's other log metrics"
  }
}

run "one_ignored_namespace_is_enough_to_alert" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      length(google_monitoring_alert_policy.tenant_namespace_mismatch) == 1
      && google_monitoring_alert_policy.tenant_namespace_mismatch[0].severity == "ERROR"
      && google_monitoring_alert_policy.tenant_namespace_mismatch[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.tenant_namespace_mismatch[0].conditions[0].condition_threshold[0].threshold_value == 0
    )
    error_message = "one ERROR tenant_namespace_mismatch entry fires the policy"
  }

  assert {
    condition = alltrue([
      for f in [
        "resource.type = \"cloud_run_revision\"",
        "logging.googleapis.com/user/swarm/tenant-namespace-mismatch",
        "metric.label.severity = \"ERROR\"",
      ] : strcontains(google_monitoring_alert_policy.tenant_namespace_mismatch[0].conditions[0].condition_threshold[0].filter, f)
    ])
    error_message = "the condition reads the metric's ERROR series only, with the resource.type restriction Monitoring requires: a kept in-prefix namespace is not paged on"
  }

  assert {
    condition     = google_monitoring_alert_policy.tenant_namespace_mismatch[0].user_labels["managed-by"] == "swarm-terraform"
    error_message = "the policy carries managed-by=swarm-terraform, which make destroy keys on"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.tenant_namespace_mismatch[0].documentation[0].content, "scripts/tenant-namespace-audit.sh") && contains(output.alert_policy_names, "swarm-dev-tenant-namespace-mismatch")
    error_message = "the alert names the audit script, and is listed with the module's other policies"
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
    condition     = length(google_monitoring_alert_policy.tenant_namespace_mismatch) == 0
    error_message = "create_alerts = false creates no tenant-namespace policy, as for every other"
  }
}
