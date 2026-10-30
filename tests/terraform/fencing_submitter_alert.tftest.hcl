# FENCE-SUBMITTER (part of #1822). The generation-fencing alert drops the
# SUBMITTERS that fence on purpose, not only tenants.
#
# Measured 2026-10-22 01:11Z over 7 days, `jsonPayload.event_type=
# "generation_fenced"` grouped by `jsonPayload.labels.tenant_id`: eng 347,
# smoke 57. verify-remote's race and concurrency tests fence deliberately, but
# verify-remote submits as eng, so no tenant filter could drop its fences
# without hiding eng's real ones. The worker now writes `submitter_service` on
# the fence's event line (agent_worker.logs.submitter_service: a service
# account's short name, or `person`, never an address), the metric extracts
# it, and the policy excludes `fencing_alert_excluded_submitters`.

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

run "the_fence_metric_extracts_the_submitter_and_the_alert_drops_swarm_verify" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    fencing_alert_excluded_tenants = ["smoke", "u-sw-c90291"]
  }

  # MUTATION: drop `submitter_service` from the generation_fenced labels in
  # metrics.tf and the metric counts every fence but groups by no submitter,
  # so the policy's clause below filters on a label that is always empty.
  assert {
    condition     = google_logging_metric.events["generation_fenced"].label_extractors["submitter_service"] == "EXTRACT(jsonPayload.submitter_service)"
    error_message = "the fence metric must extract the worker's top-level submitter_service field (agent_worker.control `_fence_fields`)"
  }

  # MUTATION: declare the extractor but not the descriptor label and the
  # metric is refused at apply; this pins both halves.
  assert {
    condition = contains(
      [for l in google_logging_metric.events["generation_fenced"].metric_descriptor[0].labels : l.key],
      "submitter_service",
    )
    error_message = "the fence metric's descriptor must declare the submitter_service label it extracts"
  }

  # MUTATION: change the variable's default to [] and swarm-verify's 347
  # deliberate fences a week page again.
  assert {
    condition     = strcontains(google_monitoring_alert_policy.generation_fenced[0].conditions[0].condition_threshold[0].filter, "NOT metric.label.submitter_service = one_of(\"swarm-verify\")")
    error_message = "by default the fencing alert excludes swarm-verify's fences by submitter"
  }

  # MUTATION: replace the tenant clause with the submitter clause and smoke's
  # fences page again; the tenant exclusion stays as it was.
  assert {
    condition     = strcontains(google_monitoring_alert_policy.generation_fenced[0].conditions[0].condition_threshold[0].filter, "NOT metric.label.tenant_id = one_of(\"smoke\", \"u-sw-c90291\")")
    error_message = "the tenant exclusion keeps working beside the submitter one"
  }
}

run "each_listed_submitter_is_excluded" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    fencing_alert_excluded_submitters = ["swarm-verify", "swarm-accept"]
  }

  # MUTATION: write only the first name (`var...[0]`) into the clause and the
  # second submitter's fences still page.
  assert {
    condition     = strcontains(google_monitoring_alert_policy.generation_fenced[0].conditions[0].condition_threshold[0].filter, "NOT metric.label.submitter_service = one_of(\"swarm-verify\", \"swarm-accept\")")
    error_message = "every name in fencing_alert_excluded_submitters is excluded"
  }
}

run "an_empty_submitter_list_adds_no_clause" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    fencing_alert_excluded_tenants    = ["smoke", "u-sw-c90291"]
    fencing_alert_excluded_submitters = []
  }

  # MUTATION: drop the `length(...) == 0 ? [] :` guard and the filter reads
  # `one_of()`, which Monitoring refuses at apply.
  assert {
    condition     = !strcontains(google_monitoring_alert_policy.generation_fenced[0].conditions[0].condition_threshold[0].filter, "submitter_service")
    error_message = "an empty fencing_alert_excluded_submitters adds no clause to the filter"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.generation_fenced[0].conditions[0].condition_threshold[0].filter, "NOT metric.label.tenant_id = one_of(")
    error_message = "emptying the submitter list leaves the tenant exclusion in place"
  }
}

run "a_submitter_with_a_quote_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    fencing_alert_excluded_submitters = ["swarm-verify\") OR (metric.label.tenant_id = \"eng"]
  }

  # MUTATION: remove the variable's validation and the name is written into
  # the filter unescaped, closing the one_of and widening the exclusion.
  expect_failures = [var.fencing_alert_excluded_submitters]
}
