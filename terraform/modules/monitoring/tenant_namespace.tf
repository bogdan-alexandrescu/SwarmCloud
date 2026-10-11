# ---------------------------------------------------------------------------
# A tenant document records a namespace that is not the canonical one
# (owner decision 2026-10-11; part of #847)
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. `tenants/u-bogdan` carried `namespace: swarm-u-bogdan`, the
# spelling used before 2026-09-23, while its real namespace is
# `swarm-tenant-u-bogdan`. The dispatcher preferred the stored value
# unchecked, so every GKE task for that tenant was created in a namespace that
# does not exist, and Kubernetes reported it as `jobs.batch is forbidden`
# (docs/gke-dispatch-403.md). Nothing said the document was wrong.
#
# THE EMITTERS. `scheduler.dispatch.verified_namespace` and its copy
# `reconciler.backends.verified_namespace` compare the stored value with the
# derived one on every naming call and log ONE entry per tenant per process
# per hour when they differ, with the fields at the TOP LEVEL of the JSON line
# (swarm_common.logging_setup's `extra=` for the scheduler, StructuredLogger's
# keyword fields for the reconciler):
#
#   {"severity": "ERROR", "event": "tenant_namespace_mismatch",
#    "tenant_id": "...", "stored": "...", "derived": "...", "used": "..."}
#
#   * ERROR: the stored value is outside `swarm-tenant-`. It was IGNORED and
#     the derived name used, so dispatch works -- but the document is wrong,
#     and anything else that reads it (a script, the UI) is misled.
#   * WARNING: inside the prefix but not the derived name (a `-canary`
#     rename `kubernetes/render.py --namespace` allows). KEPT. Counted, not
#     alerted on: it is a permitted configuration, and paging on it hourly
#     would teach people to ignore this policy.
#
# tests/unit/control_plane/test_reconciler_gke_namespaced.py asserts the event
# name both services log is in this metric's filter, and
# tests/terraform/tenant_namespace_alert.tftest.hcl asserts the metric and the
# alert exist.
locals {
  scheduler_services = coalescelist(
    [for s in var.service_names : s if endswith(s, "-scheduler")],
    ["${var.name_prefix}-scheduler"],
  )

  # Logging query syntax, not Monitoring's: `field=(a OR b)`, not one_of().
  # Both services, by name and region: the project is shared.
  tenant_namespace_log_source = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=(${join(" OR ", [for s in concat(local.scheduler_services, local.reconciler_services) : "\"${s}\""])})",
    "resource.labels.location=\"${var.region}\"",
  ])

  tenant_namespace_mismatch_doc = <<-DOC
    A tenant document's `namespace` is outside `swarm-tenant-`. The scheduler
    and the reconciler IGNORED it and used the derived name
    (`swarm-tenant-<tenant_id>`), so GKE dispatch is not affected -- but the
    document is wrong, and this is the value that sent u-bogdan's tasks to a
    namespace that did not exist on 2026-10-11 (docs/gke-dispatch-403.md).

    The log entry (`jsonPayload.event="tenant_namespace_mismatch"`) names
    `tenant_id`, `stored` and `derived`. List every tenant that disagrees with
    `scripts/tenant-namespace-audit.sh`, then repair the document as
    docs/runbooks/gke-dispatch-redispatch.md describes.
  DOC
}

resource "google_logging_metric" "tenant_namespace_mismatch" {
  project = var.project_id
  name    = "${var.name_prefix}/tenant-namespace-mismatch"

  description = "The scheduler or the reconciler found a tenant document whose stored namespace is not the derived one (logged once per tenant per process per hour). ERROR: outside swarm-tenant-, ignored. WARNING: inside it, kept."

  filter = join(" AND ", [
    local.tenant_namespace_log_source,
    "jsonPayload.event=\"tenant_namespace_mismatch\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    # Bounded by the number of registered tenants, and zero on a healthy
    # platform.
    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }

    # ERROR or WARNING: whether the stored value was ignored or kept.
    labels {
      key        = "severity"
      value_type = "STRING"
    }
  }

  label_extractors = {
    tenant_id = "EXTRACT(jsonPayload.tenant_id)"
    severity  = "EXTRACT(severity)"
  }
}

resource "google_monitoring_alert_policy" "tenant_namespace_mismatch" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-tenant-namespace-mismatch"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "A tenant document records a namespace outside swarm-tenant-"

    condition_threshold {
      # Monitoring requires a resource.type restriction on a log-based
      # metric's condition (run 36655830725, alerts.tf spec_signature_invalid).
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.tenant_namespace_mismatch.name}\"",
        "metric.label.severity = \"ERROR\"",
      ])

      # One is enough: each entry is a tenant document that is wrong today.
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant_id"]
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "${trimspace(local.tenant_namespace_mismatch_doc)}${local.alert_docs_suffix}"
  }

  # The entry repeats hourly while the document is wrong, so a policy left
  # open a day past the last one has been repaired.
  alert_strategy {
    auto_close = "86400s"
  }

  # Merged, not just inherited, as every policy here: the marker `make destroy`
  # keys on, even if a caller passes labels without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}
