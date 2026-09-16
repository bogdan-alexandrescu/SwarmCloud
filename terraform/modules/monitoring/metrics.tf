# Log-based metrics.
#
# These read the `swarm_event` field of the structured logs the control plane
# emits. The values are not invented here: they are exactly the
# swarm_common.states.EventType members, so a metric cannot silently stop
# matching because someone renamed an event -- renaming one means editing the
# frozen contract, which is a conversation rather than a commit.
#
# Cross-track requirement: every component must log a JSON payload carrying
# `swarm_event`, and where applicable `tenant_id`, `runner_profile`, `provider`
# and `blocked_reason`.

locals {
  event_metrics = {
    "lease_acquired" = {
      description = "A task was admitted and now holds capacity. This is the platform's throughput signal."
      labels = {
        tenant_id      = "EXTRACT(jsonPayload.tenant_id)"
        runner_profile = "EXTRACT(jsonPayload.runner_profile)"
      }
    }
    "lease_released" = {
      description = "Capacity returned to every pool the lease held."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.tenant_id)"
      }
    }
    "generation_fenced" = {
      description = "A worker found its fencing generation stale and exited without running the agent (CONTRACT.md invariant 5). A nonzero rate means two attempts raced."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.tenant_id)"
      }
    }
    "quota_exhausted" = {
      description = "A provider refused work; the task parks rather than burning compute on a wait."
      labels = {
        provider  = "EXTRACT(jsonPayload.provider)"
        tenant_id = "EXTRACT(jsonPayload.tenant_id)"
      }
    }
    "dead_lettered" = {
      description = "A task exhausted its attempts. Every one of these needs a human."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.tenant_id)"
      }
    }
    "checkpoint_completed" = {
      description = "Mandatory periodic checkpoint landed (invariant 8). A drop to zero means interruptions have become unsurvivable."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.tenant_id)"
      }
    }
    "parked" = {
      description = "A task became durably ineligible. PARKED costs nothing, so a large number here is healthy, not alarming."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.tenant_id)"
      }
    }
  }
}

resource "google_logging_metric" "events" {
  for_each = local.event_metrics

  project = var.project_id
  name    = "${var.name_prefix}/${replace(each.key, "_", "-")}"

  description = each.value.description

  filter = join(" AND ", [
    "resource.type=(\"cloud_run_revision\" OR \"cloud_run_job\")",
    "jsonPayload.swarm_event=\"${each.key}\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    dynamic "labels" {
      for_each = each.value.labels
      content {
        key        = labels.key
        value_type = "STRING"
      }
    }
  }

  label_extractors = each.value.labels
}

# Admission denial is not an error -- it is the platform refusing to
# oversubscribe. It is measured with the blocking reason attached so "the
# platform is busy" can be told apart from "you personally are at your limit",
# which is exactly the distinction swarm_common.states.BlockedReason exists for.
resource "google_logging_metric" "admission_denied" {
  project = var.project_id
  name    = "${var.name_prefix}/admission-denied"

  description = "READY task not admitted on a scheduler pass, labelled with the BlockedReason."

  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "jsonPayload.swarm_event=\"admission_denied\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "blocked_reason"
      value_type = "STRING"
    }

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }
  }

  label_extractors = {
    blocked_reason = "EXTRACT(jsonPayload.blocked_reason)"
    tenant_id      = "EXTRACT(jsonPayload.tenant_id)"
  }
}

# Peak RSS is reported by the worker on every attempt. The sizing note in
# swarm_common.profiles says the resource classes should be corrected from
# production rather than from arithmetic; this is the measurement that does it.
resource "google_logging_metric" "peak_rss" {
  project = var.project_id
  name    = "${var.name_prefix}/attempt-peak-rss-bytes"

  description = "Peak resident set size of a completed attempt, for correcting the resource-class sizing from production."

  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "jsonPayload.swarm_event=\"attempt_completed\"",
    "jsonPayload.peak_rss_bytes>0",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "By"

    labels {
      key        = "runner_profile"
      value_type = "STRING"
    }

    labels {
      key        = "resource_class"
      value_type = "STRING"
    }
  }

  value_extractor = "EXTRACT(jsonPayload.peak_rss_bytes)"

  label_extractors = {
    runner_profile = "EXTRACT(jsonPayload.runner_profile)"
    resource_class = "EXTRACT(jsonPayload.resource_class)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 32
      growth_factor      = 1.4
      scale              = 67108864
    }
  }
}
