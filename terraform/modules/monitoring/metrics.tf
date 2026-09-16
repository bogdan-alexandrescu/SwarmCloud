# Log-based metrics.
#
# Every filter here matches a log line some component in this repository
# actually writes today. That sounds like a low bar and it is not: a log-based
# metric whose filter matches nothing is not an error anywhere -- not at apply,
# not in the console, not in an alert policy built on it. It is simply a number
# that stays at zero, an alert that never fires, and a dashboard tile that looks
# calm. There is no way to tell that apart from a healthy platform by looking.
#
# So the vocabulary is pinned to the emitter, not to an aspiration:
#
#   agent_worker.control.ControlPlane.emit() writes one JSON line per
#   control-plane event -- `{"message": "event", "event_type": "<value>", ...}`
#   -- where `event_type` is a swarm_common.states.EventType VALUE. That is the
#   source of every metric in local.event_metrics.
#
#   agent_worker.metrics.LoggingMetricsExporter writes one JSON line per
#   finished attempt -- `{"message": "attempt resource usage",
#   "peak_rss_bytes": N, ...}`. That is the source of the peak-RSS distribution.
#
# Label paths follow the same rule. agent_worker.logs.StructuredLogger puts the
# attempt's bound identity under a nested `labels` object and keeps per-call
# fields at the top level, and Cloud Logging gives `labels` no special treatment
# inside a JSON payload, so the bound ones are `jsonPayload.labels.tenant_id`
# while a per-call one is `jsonPayload.detail.provider`. Extracting the wrong
# one yields a metric that counts correctly and groups by nothing.
#
# NOT METRICS HERE, deliberately, and this is the honest part of the file:
# `lease_acquired` and admission denials are decided by the SCHEDULER, which
# logs through the standard library with no handler configured, so those lines
# are not JSON and at INFO are not emitted at all. Neither can be measured from
# logs as the code stands. `starting` is used as the throughput signal instead
# -- the worker emits it exactly once per attempt, which is the same thing being
# counted one step later in the same causal chain. Restoring a true admission
# metric needs the scheduler to emit structured JSON; until it does, a metric
# for it would be decoration.

locals {
  # EventType value -> what the metric is for. Every key is a value the WORKER
  # emits; a scheduler-only event does not belong in this map.
  event_metrics = {
    # EventType.STARTING. One per attempt that actually began running, which is
    # the platform's throughput signal on the worker side of the lease.
    "starting" = {
      description = "An attempt started on a worker. One per dispatched attempt: the throughput signal."
      labels = {
        tenant_id      = "EXTRACT(jsonPayload.labels.tenant_id)"
        runner_profile = "EXTRACT(jsonPayload.labels.runner_profile)"
      }
    }
    "lease_released" = {
      description = "Capacity returned to every pool the lease held."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
      }
    }
    "generation_fenced" = {
      description = "A worker found its fencing generation stale and exited without running the agent (CONTRACT.md invariant 5). A nonzero rate means two attempts raced."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
      }
    }
    "quota_exhausted" = {
      description = "A provider refused work; the task parks rather than burning compute on a wait."
      labels = {
        # agent_worker.quota puts the provider in the event detail, not in the
        # logger's bound labels.
        provider  = "EXTRACT(jsonPayload.detail.provider)"
        tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
      }
    }
    "dead_lettered" = {
      description = "A task exhausted its attempts. Every one of these needs a human."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
      }
    }
    "checkpoint_completed" = {
      description = "Mandatory periodic checkpoint landed (invariant 8). A drop to zero means interruptions have become unsurvivable."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
      }
    }
    "parked" = {
      description = "A task became durably ineligible, labelled with the ParkReason. PARKED costs nothing, so a large number here is healthy, not alarming."
      labels = {
        tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
        # ControlPlane.park() puts the ParkReason value in the event detail.
        park_reason = "EXTRACT(jsonPayload.detail.reason)"
      }
    }
  }

  # The shared half of every event filter. Workers run on Cloud Run Jobs and on
  # GKE, and the same line is written either way.
  event_log_sources = "resource.type=(\"cloud_run_job\" OR \"k8s_container\" OR \"cloud_run_revision\")"
}

resource "google_logging_metric" "events" {
  for_each = local.event_metrics

  project = var.project_id
  name    = "${var.name_prefix}/${replace(each.key, "_", "-")}"

  description = each.value.description

  filter = join(" AND ", [
    local.event_log_sources,
    # The literal message ControlPlane.emit() logs, plus the EventType value it
    # carries. Matching on both is what keeps an unrelated line that happens to
    # have an `event_type` field out of the count.
    "jsonPayload.message=\"event\"",
    "jsonPayload.event_type=\"${each.key}\"",
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

# Peak RSS is reported by the worker on every attempt. The sizing note in
# swarm_common.profiles says the resource classes should be corrected from
# production rather than from arithmetic; this is the measurement that does it.
#
# agent_worker.metrics.LoggingMetricsExporter spreads its label dict at the TOP
# level of the record rather than into the logger's bound `labels` object, so
# these extractors are deliberately unnested while the event ones above are not.
resource "google_logging_metric" "peak_rss" {
  project = var.project_id
  name    = "${var.name_prefix}/attempt-peak-rss-bytes"

  description = "Peak resident set size of a completed attempt, for correcting the resource-class sizing from production."

  filter = join(" AND ", [
    "resource.type=(\"cloud_run_job\" OR \"k8s_container\")",
    "jsonPayload.message=\"attempt resource usage\"",
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

# The attempt that finished at 97% of its limit looks like a success and is the
# one that gets OOM-killed next week when the model gets chattier. Because
# requests == limits platform-wide (invariant 7) there is no headroom to absorb
# that, so the near miss has to be countable before it becomes a kill.
resource "google_logging_metric" "oom_near_miss" {
  project = var.project_id
  name    = "${var.name_prefix}/attempt-oom-near-miss"

  description = "An attempt came within a hair of its memory limit. requests == limits, so there is no headroom to absorb the next increase."

  filter = join(" AND ", [
    "resource.type=(\"cloud_run_job\" OR \"k8s_container\")",
    "jsonPayload.message=\"attempt resource usage\"",
    "jsonPayload.oom_near_miss=true",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "runner_profile"
      value_type = "STRING"
    }

    labels {
      key        = "resource_class"
      value_type = "STRING"
    }
  }

  label_extractors = {
    runner_profile = "EXTRACT(jsonPayload.runner_profile)"
    resource_class = "EXTRACT(jsonPayload.resource_class)"
  }
}
