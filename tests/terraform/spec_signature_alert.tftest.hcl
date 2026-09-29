# Contract request 34 (#342), decision 9: every spec_signature_invalid is an
# attack or a bug, so it pages.
#
# THE OWN-SPEC HALF ONLY. A worker refusing its own step's spec logs, at
# ERROR, `spec signature invalid: refusing to run this task` with a top-level
# `end_cause` of `spec_signature_invalid` (#353, agent_worker.lifecycle
# `_verify_spec`; StructuredLogger keeps per-call fields at the top of the
# JSON payload). The upstream half -- MERGE_REFUSED / VERDICT_REFUSED with
# `result_summary.spec_check.reason` starting `upstream:` -- waits for
# contract request 33's build, and this metric must not swallow it by
# matching those end causes early: they have other, unrelated causes.

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
    error_message = "the own-spec metric must not match CR 33's end causes; their upstream half is keyed on the reason prefix, when CR 33 is built"
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

  assert {
    condition = (
      google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.spec_signature_invalid[0].conditions[0].condition_threshold[0].threshold_value == 0
    )
    error_message = "a single refusal must fire the alert: every occurrence is an attack or a bug (decision 9)"
  }
}
