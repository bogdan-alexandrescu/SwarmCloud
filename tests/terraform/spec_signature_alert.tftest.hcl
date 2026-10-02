# Contract request 34 (#342), decision 9: every spec_signature_invalid is an
# attack or a bug, so it pages.
#
# TWO HALVES. A worker refusing its own step's spec logs, at ERROR, `spec
# signature invalid: refusing to run this task` with a top-level `end_cause`
# of `spec_signature_invalid` (#353, agent_worker.lifecycle `_verify_spec`;
# StructuredLogger keeps per-call fields at the top of the JSON payload). A
# merge or post-verdict worker refusing an UPSTREAM step's spec (contract
# request 33, #295) ends MERGE_REFUSED or VERDICT_REFUSED with
# `spec_check.reason` starting `upstream:` -- a second metric and a second
# condition on the same policy. The own-spec metric must not swallow it by
# matching those end causes: they have other, unrelated causes.

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

run "the_metric_counts_spec_signature_refusals_from_both_backends" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.end_cause=\"spec_signature_invalid\"",
        "jsonPayload.message=\"spec signature invalid: refusing to run this task\"",
        "\"cloud_run_job\"",
        "\"k8s_container\"",
      ] : strcontains(google_logging_metric.spec_signature_invalid.filter, f)
    ])
    error_message = "the metric must match the worker's refusal line, keyed on its end cause, from Cloud Run Jobs and GKE pods alike"
  }

  assert {
    condition = alltrue([
      for f in ["merge_refused", "verdict_refused", "MERGE_REFUSED", "VERDICT_REFUSED"] :
      !strcontains(google_logging_metric.spec_signature_invalid.filter, f)
    ])
    error_message = "the own-spec metric must not match CR 33's end causes; their upstream half is keyed on the reason prefix, in its own metric"
  }

  assert {
    condition     = google_logging_metric.spec_signature_invalid.metric_descriptor[0].metric_kind == "DELTA" && google_logging_metric.spec_signature_invalid.metric_descriptor[0].value_type == "INT64"
    error_message = "a count of refusals is a DELTA INT64 series"
  }
}

run "one_refusal_is_enough_to_alert" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].filter, "metric.type = \"logging.googleapis.com/user/${google_logging_metric.spec_signature_invalid.name}\"")
    error_message = "the alert must watch the metric this module creates"
  }

  # Not pinned to cloud_run_job: a GKE browser pod's refusal pages too.
  assert {
    condition     = !strcontains(google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].filter, "resource.type = \"cloud_run_job\"")
    error_message = "the alert must not be limited to Cloud Run Jobs; GKE workers verify the same way"
  }

  # Run 36655830725 (main 4bb7564): "Error creating AlertPolicy: googleapi:
  # Error 400: Field alert_policy.conditions[0].condition_threshold.filter had
  # an invalid value ... must specify a restriction on resource.type".
  # Monitoring requires a resource.type restriction on every log-based
  # metric's condition filter; the sibling policies in this file
  # (tasks_dead_lettered, generation_fenced, job_failures) all carry one.
  assert {
    condition     = strcontains(google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].filter, "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")")
    error_message = "the alert's filter must restrict resource.type (Monitoring rejects a log-based metric condition without one) while still covering both worker backends, matching the metric's own filter"
  }

  assert {
    condition = (
      google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].threshold_value == 0
    )
    error_message = "a single refusal must fire the alert: every occurrence is an attack or a bug (decision 9)"
  }
}

run "the_upstream_half_counts_only_upstream_spec_refusals" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  # Keyed on all of: the refusal line, the two worker-action end causes, and
  # the `upstream:` prefix -- so a merge refused for a red check or a stale
  # head (the end causes' other reasons) never pages as an attack.
  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.message=\"upstream spec signature invalid: refusing this worker action\"",
        "jsonPayload.end_cause=(\"merge_refused\" OR \"verdict_refused\")",
        "jsonPayload.spec_check.reason=~\"^upstream:\"",
        "\"cloud_run_job\"",
        "\"k8s_container\"",
      ] : strcontains(google_logging_metric.spec_upstream_invalid.filter, f)
    ])
    error_message = "the upstream metric must match the worker action's refusal line, keyed on merge_refused/verdict_refused AND the upstream: reason prefix"
  }

  assert {
    condition     = !strcontains(google_logging_metric.spec_upstream_invalid.filter, "spec_signature_invalid")
    error_message = "the upstream metric must not count the own-spec refusals the first metric already counts"
  }

  # The task id in upstream:<task id>:<why> is unbounded; a label carrying it
  # would mint a time series per task. Only <why> is extracted.
  assert {
    condition     = google_logging_metric.spec_upstream_invalid.label_extractors["reason"] == "REGEXP_EXTRACT(jsonPayload.spec_check.reason, \"^upstream:[^:]*:(.*)$\")"
    error_message = "the reason label must be CR 34's <why>, not the whole upstream:<task id>:<why> string"
  }

  assert {
    condition     = contains(output.log_metric_names, google_logging_metric.spec_upstream_invalid.name)
    error_message = "the upstream metric must be listed with the module's other log metrics"
  }
}

run "one_upstream_refusal_is_enough_to_alert_on_the_same_policy" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition     = length(google_monitoring_alert_policy.spec_signature_invalid[0].conditions) == 2
    error_message = "the spec-signature policy carries both halves: own spec and upstream spec"
  }

  assert {
    condition = (
      strcontains(google_monitoring_alert_policy.spec_signature_invalid[0].conditions[1].condition_threshold[0].filter, "metric.type = \"logging.googleapis.com/user/${google_logging_metric.spec_upstream_invalid.name}\"")
      && strcontains(google_monitoring_alert_policy.spec_signature_invalid[0].conditions[1].condition_threshold[0].filter, "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")")
    )
    error_message = "the second condition must watch the upstream metric, with the resource.type restriction Monitoring requires"
  }

  assert {
    condition = (
      google_monitoring_alert_policy.spec_signature_invalid[0].combiner == "OR"
      && google_monitoring_alert_policy.spec_signature_invalid[0].conditions[1].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.spec_signature_invalid[0].conditions[1].condition_threshold[0].threshold_value == 0
    )
    error_message = "a single upstream refusal must fire the alert on its own"
  }
}
