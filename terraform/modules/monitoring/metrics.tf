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
# `lease_acquired` and admission denials are decided by the SCHEDULER, and the
# scheduler emits no per-event vocabulary. Its lines ARE JSON now --
# swarm_common.logging_setup.configure_logging() installs a formatter that
# writes severity/message/logger, and scheduler/main.py calls it at startup --
# but they are printf-style messages with the payload interpolated into the
# text (`log.info("drain finished %s", report.to_dict())`, loop.py), so there is
# no `event_type` field to filter on and no label to group by. `starting` is
# used as the throughput signal instead -- the worker emits it exactly once per
# attempt, which is the same thing being counted one step later in the same
# causal chain. Restoring a true admission metric needs the scheduler to emit
# the same `{"message": "event", "event_type": ...}` shape ControlPlane.emit()
# does; until it does, a metric for it would be decoration.

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

# ---------------------------------------------------------------------------
# The safety tick, counted from Cloud Scheduler's own logs.
#
# THE METRIC THE ALERT USED TO WATCH DOES NOT EXIST, AND NEVER WILL.
# `cloudscheduler.googleapis.com/job/attempt_count` was the alert's metric
# type, and enable_safety_tick_alert was switched off in dev on 2026-09-19 to
# wait for it to "appear". Checked 2026-09-24:
#
#   * Google's published metric list for every cloud* service
#     (docs.cloud.google.com/monitoring/api/metrics_gcp_c) has sections for
#     cloudbuild, clouddeploy, cloudfunctions, cloudkms, cloudsql, cloudtasks
#     and cloudtrace -- and no Cloud Scheduler metric of any name;
#   * the project's metric descriptors filtered to
#     starts_with("cloudscheduler.googleapis.com") number ZERO, five days after
#     the tick job began attempting every minute, against 49 for
#     run.googleapis.com with the same query.
#
# There was nothing to wait for. The alert could never be created.
#
# WHAT DOES EXIST is a log line per attempt. Cloud Scheduler writes
# `cloudscheduler.googleapis.com/executions` entries on the monitored resource
# `cloud_scheduler_job` (labels project_id, location, job_id), and this job's
# entries were read the same day:
#
#   AttemptStarted   INFO, then
#   AttemptFinished  INFO, jsonPayload keys {@type, jobName, pubsubTopic,
#                    targetType: PUB_SUB} -- one pair per minute
#
# and a FAILED attempt, from swarm-quota-refresh on 2026-09-20, is
#
#   AttemptFinished  ERROR, with jsonPayload.status = "INTERNAL" and debugInfo
#
# So this counts finished attempts that carry no `status`: a tick that fired
# and was delivered. A tick whose publish fails every minute has not reached
# the scheduler, which is the thing the alert is for, so it must not count.
#
# A logs-based metric's series keep the log entry's monitored resource, so the
# alert can still filter on resource.type = cloud_scheduler_job and the job id.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "safety_tick_attempts" {
  project = var.project_id
  name    = "${var.name_prefix}/scheduler-tick-delivered"

  description = "A Cloud Scheduler attempt of the one-minute safety tick finished without an error status. Its absence for ten minutes is the safety-tick-stopped alert."

  filter = join(" AND ", [
    "logName=\"projects/${var.project_id}/logs/cloudscheduler.googleapis.com%2Fexecutions\"",
    "resource.type=\"cloud_scheduler_job\"",
    "resource.labels.job_id=\"${var.safety_tick_job}\"",
    "resource.labels.location=\"${var.region}\"",
    "jsonPayload.\"@type\"=\"type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished\"",
    "NOT jsonPayload.status:*",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# Dispatch failures, per backend, counted from the scheduler's own log line.
#
# The alert that watches this (alerts.tf, dispatch_failing_by_backend) used to
# read the Prometheus counter swarm_scheduler_dispatch_failures_total. Release
# 36023801562 could not create it:
#
#     Error 404: Cannot find metric(s) that match type =
#     "prometheus.googleapis.com/swarm_scheduler_dispatch_failures_total/counter"
#
# A labelled Prometheus counter has no descriptor until a sample is exported,
# and this one is exported only after a dispatch FAILS -- so the alert for the
# failure could not exist until after the failure it is for. A logs-based metric
# exists the moment terraform creates it.
#
# The line is apps/scheduler/scheduler/loop.py's
#     "dispatch failed task=%s attempt=%s backend=%s code=%s: %s"
# measured in Cloud Logging as jsonPayload.message on cloud_run_revision (e.g.
# 2026-09-24T03:33:08Z, backend=GKE_AUTOPILOT code=gke_create_job_failed). Only
# the scheduler writes a message beginning "dispatch failed ".
resource "google_logging_metric" "dispatch_failures" {
  project = var.project_id
  name    = "${var.name_prefix}/dispatch-failures"

  description = "One per failed dispatch attempt, labelled by backend, from the scheduler's 'dispatch failed' line."

  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "jsonPayload.message=~\"^dispatch failed \"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "backend"
      value_type = "STRING"
    }
  }

  label_extractors = {
    backend = "REGEXP_EXTRACT(jsonPayload.message, \"backend=(\\\\S+)\")"
  }
}

# ---------------------------------------------------------------------------
# A worker refused its own step's spec (contract request 34, #342; decision 9).
#
# THE EMITTER: #353's `agent_worker.lifecycle._verify_spec` logs, at ERROR,
# `{"message": "spec signature invalid: refusing to run this task",
#   "end_cause": "spec_signature_invalid", "spec_check": {"reason": ...,
#   "task_id": ..., "key_version": ..., "digest": ...}, "labels": {...}}`
# -- per-call fields at the top of the payload, bound identity under
# `labels` (StructuredLogger, as for every metric above). Keyed on BOTH the
# message and the end cause, so neither an unrelated line that happens to carry
# the end cause nor a reworded one counts. No spec content is ever in the line.
#
# THE OWN-SPEC HALF. Contract request 33's worker actions refusing an UPSTREAM
# step's spec are counted by `spec_upstream_invalid` below, keyed on the reason
# prefix: MERGE_REFUSED and VERDICT_REFUSED have other, unrelated causes, so
# this metric must not match them.
#
# WHO CAN WRITE THE LINE (#346 box 51, #354 security review). The agent runs
# beside the worker as the same uid. Its own stdout is a pipe into a file, but
# tini -- PID 1, whose stdout and stderr ARE the container's log -- is
# dumpable, so the agent can open /proc/1/fd/1 and write a JSON line carrying
# this message, this end cause and any `labels.tenant_id` it likes. No
# jsonPayload field is the worker's alone. What no process in the container
# can set is the entry's monitored resource: Cloud Run and GKE fill it in. So
# both refusal metrics (this and spec_upstream_invalid below):
#
#   * count only a WORKER container's line (local.spec_refusal_source): a
#     Cloud Run Job named `<name_prefix>-job-*` (scheduler.dispatch
#     `job_id_for`, terraform/infra/locals.tf), or the container named
#     `worker` in this platform's own cluster. Before, any Cloud Run Job or
#     any pod in the shared project -- the other team's agents-staging
#     cluster included -- could page CRITICAL with one line;
#   * name the tenant from that resource, never the payload: `tenant_id` from
#     the GKE namespace (`swarm-tenant-<tenant>`, dispatch `namespace_for`),
#     `job_name` from the Cloud Run Job (`<name_prefix>-job-<tenant>-<profile>`,
#     one Job per tenant and profile). An agent can no longer page in another
#     tenant's name.
#
# WHAT THIS DOES NOT CLOSE: an agent can still write the line into its OWN
# worker container's log, and that pages -- under its own tenant's job or
# namespace, which is the attribution the responder starts from. Only a write
# path the agent cannot reach (a non-dumpable PID 1, or the worker logging
# through a channel the agent holds no credential for) separates the two, and
# that is the worker's image, not this filter.
# ---------------------------------------------------------------------------
locals {
  spec_refusal_cluster = coalesce(var.gke_cluster_name, "${var.name_prefix}-autopilot")

  spec_refusal_source = join(" OR ", [
    "(resource.type=\"cloud_run_job\" AND resource.labels.job_name=~\"^${var.name_prefix}-job-\")",
    "(resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${local.spec_refusal_cluster}\" AND resource.labels.container_name=\"worker\")",
  ])

  # The resource's labels, not the payload's. On Cloud Run tenant_id reads
  # empty and job_name says which tenant; on GKE the reverse.
  spec_refusal_identity_extractors = {
    tenant_id = "REGEXP_EXTRACT(resource.labels.namespace_name, \"^${var.name_prefix}-tenant-(.+)$\")"
    job_name  = "EXTRACT(resource.labels.job_name)"
  }

  # Declared after tenant_id, never instead of it: a log-based metric's
  # existing label is kept, and only added to.
  spec_refusal_job_label = {
    key         = "job_name"
    description = "The worker's Cloud Run Job, <name_prefix>-job-<tenant>-<profile>; empty for a GKE worker, whose tenant is tenant_id."
  }
}

resource "google_logging_metric" "spec_signature_invalid" {
  project = var.project_id
  name    = "${var.name_prefix}/spec-signature-invalid"

  description = "A worker refused to run a task whose step spec did not verify (end cause spec_signature_invalid). Every occurrence is an attack on a parked step or a platform bug."

  filter = join(" AND ", [
    "(${local.spec_refusal_source})",
    "jsonPayload.message=\"spec signature invalid: refusing to run this task\"",
    "jsonPayload.end_cause=\"spec_signature_invalid\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }

    labels {
      key         = local.spec_refusal_job_label.key
      value_type  = "STRING"
      description = local.spec_refusal_job_label.description
    }

    # unsigned, unknown_format, foreign_key_version, not_canonical,
    # signature_mismatch, environment_mismatch -- a short, fixed vocabulary
    # (agent_worker.specverify, #353), so a bounded label.
    labels {
      key        = "reason"
      value_type = "STRING"
    }
  }

  label_extractors = merge(local.spec_refusal_identity_extractors, {
    reason = "EXTRACT(jsonPayload.spec_check.reason)"
  })
}

# ---------------------------------------------------------------------------
# A worker action refused an UPSTREAM step's spec (contract requests 33 and
# 34; merge-step.md §6 row 42, §6a row 5).
#
# `merge` verifies the signed specs of the union it depends on -- author,
# review, post-verdict, fix, proof -- and `post-verdict` those of its own
# upstream, before either reads a credential. A failure ends the task
# MERGE_REFUSED or VERDICT_REFUSED, and the detail goes in CR 34's own field,
# `spec_check.reason = "upstream:<task id>:<why>"`, where <why> is CR 34's
# vocabulary verbatim (`unsigned`, `signature_mismatch`, ...).
#
# THE EMITTER this metric expects (Track B, merge-step.md §10 item 7): the
# worker logs, at ERROR,
# `{"message": "upstream spec signature invalid: refusing this worker action",
#   "end_cause": "merge_refused" | "verdict_refused",
#   "spec_check": {"reason": "upstream:<task id>:<why>", ...}, "labels": {...}}`
# -- the same StructuredLogger shape as the own-spec line above. Keyed on the
# message, the two end causes AND the `upstream:` prefix, so neither the
# end causes' other refusals (a red check, a stale head) nor a reworded line
# counts. Nothing emits it until that worker is built; a metric with no
# writer reads zero, which is what a disabled merge step should read.
#
# `why` is extracted, not the whole reason: the task id in the middle is
# unbounded, and a label carrying it would mint a time series per task.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "spec_upstream_invalid" {
  project = var.project_id
  name    = "${var.name_prefix}/spec-upstream-invalid"

  description = "A merge or post-verdict worker refused because an upstream step's spec did not verify (end cause merge_refused or verdict_refused, spec_check.reason upstream:...). Every occurrence is an attack on the chain or a platform bug."

  filter = join(" AND ", [
    "(${local.spec_refusal_source})",
    "jsonPayload.message=\"upstream spec signature invalid: refusing this worker action\"",
    "jsonPayload.end_cause=(\"merge_refused\" OR \"verdict_refused\")",
    "jsonPayload.spec_check.reason=~\"^upstream:\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }

    labels {
      key         = local.spec_refusal_job_label.key
      value_type  = "STRING"
      description = local.spec_refusal_job_label.description
    }

    # merge_refused or verdict_refused: which worker action refused.
    labels {
      key        = "end_cause"
      value_type = "STRING"
    }

    # CR 34's reason vocabulary, the <why> of upstream:<task id>:<why>.
    labels {
      key        = "reason"
      value_type = "STRING"
    }
  }

  label_extractors = merge(local.spec_refusal_identity_extractors, {
    end_cause = "EXTRACT(jsonPayload.end_cause)"
    reason    = "REGEXP_EXTRACT(jsonPayload.spec_check.reason, \"^upstream:[^:]*:(.*)$\")"
  })
}

# ---------------------------------------------------------------------------
# A merge or post-verdict worker action ended without doing its job (contract
# requests 33 and 35; merge-step.md §6, §6a): the four end causes
# merge_refused, merge_failed, verdict_refused and verdict_failed.
#
# THE EMITTER: agent_worker.lifecycle `Worker._record_worker_action_end` logs,
# at ERROR, `{"message": "worker action ended", "action": "merge" |
# "post_verdict", "code": <refusal code>, "end_cause": <one of the four>,
# "labels": {...}}` -- per-call fields at the top of the payload, bound
# identity under `labels` (StructuredLogger, as for every metric above). It
# writes that line for a refusal, and for the last attempt of a forge outage
# that spent its retries (`fail_retryably` returning FAILED): that second path
# is how most _failed ends arrive, and a metric reading only the first would
# miss them. tests/unit/worker/test_worker_action_starts_no_runner.py holds
# the line in both paths.
#
# Keyed on the message AND the four end causes, so a cannot_start (the same
# line, another end cause) and a reworded line do not count. An upstream
# step's spec failing to verify is one of the _refused ends here (code
# `spec_unverified`). `spec_upstream_invalid` above is meant to page on that
# case alone, but it keys on a message no worker writes yet (read 2026-10-08:
# no `upstream spec signature invalid` line in apps/), so until it does, this
# metric is the only count of it.
#
# `code` is not a label: it carries the forge's own error codes as well as
# the worker's vocabulary, so it is read from the log line, not grouped by.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "worker_action_ended" {
  project = var.project_id
  name    = "${var.name_prefix}/worker-action-ended"

  description = "A merge or post-verdict worker action ended the task merge_refused, merge_failed, verdict_refused or verdict_failed: the pull request was not merged or the review was not posted."

  filter = join(" AND ", [
    "resource.type=(\"cloud_run_job\" OR \"k8s_container\")",
    "jsonPayload.message=\"worker action ended\"",
    "jsonPayload.end_cause=(\"merge_refused\" OR \"merge_failed\" OR \"verdict_refused\" OR \"verdict_failed\")",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }

    # One of the four: which action, and refused (nothing changed on the
    # forge) or failed (allowed, and the forge did not do it).
    labels {
      key        = "end_cause"
      value_type = "STRING"
    }
  }

  label_extractors = {
    tenant_id = "EXTRACT(jsonPayload.labels.tenant_id)"
    end_cause = "EXTRACT(jsonPayload.end_cause)"
  }
}

# ---------------------------------------------------------------------------
# An approved personal workspace is waiting and nothing is building it
# (docs/workspaces.md §2.2, #847).
#
# WHY THIS EXISTS. On 2026-10-09 the owner's own workspace request was
# approved and then sat at `approved` for ~19 hours with no dispatch, no
# failure and nothing to say so: publishing was off, and no job called the
# dispatch sweep. The setup page showed it as in progress the whole time.
#
# THE EMITTER: swarm-api's dispatch sweep (POST /v1/admin/workspaces/sweep,
# called every 10 minutes by modules/scheduler `workspace_sweep`) logs ONE
# structured entry per stuck record per hour, `jsonPayload.event =
# "workspace_stuck"`, with workspace_id, reason, approved_at and
# minutes_waiting. A record is stuck when it is `approved` and publishing is
# off (reason publishing_off), or it has had no dispatch attempt for more than
# 15 minutes (never_dispatched), or its last dispatch is more than 30 minutes
# old and no build has claimed it (dispatched_unclaimed). The sweep runs that
# check even with publishing off, so the line exists in exactly the state of
# 2026-10-09.
#
# Only swarm-api, by service name and region: the project is shared. `reason`
# is a label (three values); workspace_id is not -- it is read from the line,
# and it is opaque by design, so the line names nobody.
# ---------------------------------------------------------------------------
locals {
  api_services = coalescelist(
    [for s in var.service_names : s if endswith(s, "-api")],
    ["${var.name_prefix}-api"],
  )
}

resource "google_logging_metric" "workspace_stuck" {
  project = var.project_id
  name    = "${var.name_prefix}/workspace-stuck"

  description = "An approved personal workspace is waiting with nothing building it: swarm-api's dispatch sweep logged workspace_stuck (publishing_off, never_dispatched or dispatched_unclaimed)."

  # Logging query syntax, not Monitoring's: `field=(a OR b)`, not one_of().
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=(${join(" OR ", [for s in local.api_services : "\"${s}\""])})",
    "resource.labels.location=\"${var.region}\"",
    "jsonPayload.event=\"workspace_stuck\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key        = "reason"
      value_type = "STRING"
    }
  }

  label_extractors = {
    reason = "EXTRACT(jsonPayload.reason)"
  }
}
