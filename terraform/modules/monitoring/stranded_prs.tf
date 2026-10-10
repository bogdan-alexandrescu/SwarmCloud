# ---------------------------------------------------------------------------
# Stranded pull requests (part of #295)
# ---------------------------------------------------------------------------
#
# On 2026-10-09, 40 pull requests SwarmCloud opened were left open and nobody
# was told: 30 had no merge step at all, 7 merge steps failed
# behind_too_often, and nothing on the platform looked at an open pull request
# after the run that opened it ended. swarm-api's stranded-PR sweep
# (apps/swarm-api/swarm_api/strandedprs.py, POST /v1/admin/stranded-prs/sweep,
# the scheduler module's stranded_pr_sweep job every 30 minutes) now looks,
# and logs ONE entry per stranded pull request per six hours:
#
#   {"severity": "WARNING", "message": "pr_stranded tenant=... owner/repo#N ...",
#    "event": "pr_stranded", "tenant_id": "...", "repository": "owner/repo",
#    "number": N, "reason": "<held|conflict|checks_red|ci_never_ran|behind|
#    merge_failed|no_merge_step>", "remedy": "<merge_pr|fix_ci|rebase|none>", ...}
#
# THE EMITTER: swarm_common.logging_setup.CloudLoggingFormatter writes every
# `extra=` field at the TOP LEVEL of the JSON line, so the fields are
# jsonPayload.event, jsonPayload.tenant_id and jsonPayload.reason -- not under
# a nested `labels` object, which is the worker's StructuredLogger's shape
# (metrics.tf explains the difference). tests/unit/control_plane/
# test_stranded_prs.py holds the line to these names.
#
# ONE IS ENOUGH, AT WARNING. The sweep already waited two hours before calling
# a pull request stranded, and dedupes for six, so every entry is a pull
# request a person has to look at: the alert is the "anyone being told" the
# entry exists for. The documentation says what each reason asks of them.

locals {
  api_services = coalescelist(
    [for s in var.service_names : s if endswith(s, "-api")],
    ["${var.name_prefix}-api"],
  )

  pr_stranded_doc = <<-DOC
    A pull request SwarmCloud opened has been open over two hours and nothing
    is going to merge it. `GET /v1/stranded-prs` lists every one with its
    reason, detail and remedy; the log entry (`jsonPayload.event="pr_stranded"`)
    names the repository and number.

    * `no_merge_step` / `behind` (remedy `merge_pr`): an admin's
      `POST /v1/admin/stranded-prs/sweep?tenant_id=<t>` with
      `{"redrive": true}` submits a merge for each whose checks are green, one
      per repository per sweep.
    * `merge_failed`: the merge step's refusal is in `detail`; read it before
      submitting another.
    * `checks_red` / `ci_never_ran` (remedy `fix_ci`): a CI fix round or a
      person. Never redriven.
    * `conflict` (remedy `rebase`): resolve onto the base.
    * `held`: someone labelled it `hold`. Reported, never acted on.
  DOC
}

resource "google_logging_metric" "pr_stranded" {
  project = var.project_id
  name    = "${var.name_prefix}/pr-stranded"

  description = "swarm-api's stranded-PR sweep found a SwarmCloud-opened pull request open over two hours that nothing is going to merge (logged once per pull request per six hours)."

  # Logging query syntax, not Monitoring's: `field=(a OR b)`, not one_of().
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=(${join(" OR ", [for s in local.api_services : "\"${s}\""])})",
    "jsonPayload.event=\"pr_stranded\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }

    # The seven reasons of swarm_api.strandedprs.REASONS: a short, fixed
    # vocabulary, so a bounded label.
    labels {
      key        = "reason"
      value_type = "STRING"
    }
  }

  label_extractors = {
    tenant_id = "EXTRACT(jsonPayload.tenant_id)"
    reason    = "EXTRACT(jsonPayload.reason)"
  }
}

resource "google_monitoring_alert_policy" "pr_stranded" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-pr-stranded"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "A SwarmCloud-opened pull request is stranded"

    condition_threshold {
      # Monitoring requires a resource.type restriction on a log-based
      # metric's condition (run 36655830725, alerts.tf spec_signature_invalid);
      # swarm-api is a Cloud Run service, which is what the metric's filter names.
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.pr_stranded.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant_id", "metric.label.reason"]
      }
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "${trimspace(local.pr_stranded_doc)}${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  # Merged, not just inherited, as every policy here: the marker `make destroy`
  # keys on, even if a caller passes labels without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}
