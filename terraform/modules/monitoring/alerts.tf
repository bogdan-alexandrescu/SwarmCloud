# Alert policies.
#
# Every policy here fires on something a human would actually act on. Note what
# is deliberately NOT alerted: a large PARKED or QUEUED backlog. Those states
# cost nothing (CONTRACT.md invariant 1), so paging on them would train the team
# to ignore the pager on exactly the days the platform is working as designed.

locals {
  # One email channel per alert address, keyed by a hash of it, never the
  # address: a resource's address reaches the repository's public Actions logs
  # and summaries, and alert_emails comes from the private tfvars.
  email_channels = { for e in toset(var.alert_emails) : substr(sha256(e), 0, 12) => e }

  # The policies name the hashed channels only. The address-keyed channels the
  # first alert release made were kept for one release while every policy moved
  # to these (Cloud Monitoring refuses to delete a channel a policy still names);
  # the 2026-11-15 release moved all 21, and this one deletes the old ones.
  notification_channels = concat(
    [for k, c in google_monitoring_notification_channel.email : c.id if contains(keys(local.email_channels), k)],
    var.extra_notification_channels,
  )

  # Every policy takes its channels from its own severity (archived issue 1817, the owner's
  # option (b) of 2026-10-21): CRITICAL and ERROR page the urgent channels as
  # well as the ordinary ones, WARNING only records to the ordinary ones. A
  # severity missing from this map is a plan error, not a policy that quietly
  # reaches nobody. tests/terraform/observability.tftest.hcl reads each
  # policy's `severity` and holds its channels to this map.
  channels_by_severity = {
    CRITICAL = concat(local.notification_channels, var.urgent_notification_channels)
    ERROR    = concat(local.notification_channels, var.urgent_notification_channels)
    WARNING  = local.notification_channels
  }

  alert_docs_suffix = "\n\nEnvironment: ${var.environment}. Managed by swarm-terraform; edit terraform/modules/monitoring rather than the console."

  # The project is shared. Naming the four swarm services explicitly, and pinning
  # the region, keeps these policies off another team's Cloud Run services --
  # a prefix match would catch anything they happen to call swarm-something.
  swarm_services_filter = "resource.labels.service_name = one_of(${join(", ", [for s in var.service_names : "\"${s}\""])})"
  region_filter         = "resource.labels.location = \"${var.region}\""
}

resource "google_monitoring_notification_channel" "email" {
  # Keyed by the hash (locals above). The address stays in the channel's
  # attributes, which no public log prints.
  for_each = local.email_channels

  project      = var.project_id
  display_name = "swarm-${var.environment}-${replace(each.value, "@", "-at-")}"
  type         = "email"

  labels = {
    email_address = each.value
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# The one-minute safety tick stopped.
#
# This is the single most important policy in the file. Pub/Sub is the fast wake
# path; the tick is what bounds staleness when Pub/Sub fails. If the tick dies
# quietly, the platform looks healthy right up until someone notices their tasks
# have been READY for an hour.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "safety_tick_absent" {
  # Also gated on its own flag, which is now only a switch. It used to be
  # WAITING for `cloudscheduler.googleapis.com/job/attempt_count` to exist; that
  # metric is not one Google publishes, so the wait could not end. The metric
  # below is a logs-based one this module creates in the same apply, and the
  # reference to it orders the two. See metrics.tf for the measurement.
  count = var.create_alerts && var.enable_safety_tick_alert ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-safety-tick-stopped"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "No scheduler tick delivered in 10 minutes"

    condition_absent {
      filter = join(" AND ", [
        "resource.type = \"cloud_scheduler_job\"",
        "resource.labels.job_id = \"${var.safety_tick_job}\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.safety_tick_attempts.name}\"",
      ])

      duration = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels_by_severity["CRITICAL"]

  documentation {
    mime_type = "text/markdown"
    content   = "The one-minute safety tick has not been delivered for ten minutes. Pub/Sub may still be waking the scheduler, so the queue is not necessarily stalled -- but the bound on staleness is gone. Check the Cloud Scheduler job (a failed attempt logs AttemptFinished at ERROR with a status) and the swarm-scheduler service logs. If the swarm was paused with scripts/pause-swarm.sh and its scheduler step, the tick is paused too and this is expected.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Control-plane 5xx.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "service_errors" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-control-plane-5xx"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "5xx rate above threshold on a swarm Cloud Run service"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"run.googleapis.com/request_count\"",
        "metric.labels.response_code_class = \"5xx\"",
        local.swarm_services_filter,
        local.region_filter,
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.error_rate_threshold
      duration        = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = "A swarm control-plane service is returning 5xx. Check the revision logs; a Firestore contention storm and a bad deploy look the same from here, so compare against the admission-denied metric before rolling back.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels

  # THE GUARD (archived issue 1817). Until 2026-10-21 every one of the 21 policies had an
  # empty channel list, because alert_emails was [] and nothing said so: the
  # 10-20 GKE dispatch outage fired and reached nobody. With
  # require_alert_channels true, alerts on and no channel anywhere is a failed
  # plan. It lives on this policy because it exists whenever any policy does
  # (create_alerts), so create_alerts = false is never refused. Only one of
  # the lists needs to be set: urgent alone still reaches a person for
  # CRITICAL and ERROR. require_alert_channels is false until the operator's
  # change that commits the console-made channel ids sets it, so this cannot
  # turn the release plan red before then.
  lifecycle {
    precondition {
      condition = !(
        var.require_alert_channels &&
        length(local.notification_channels) == 0 &&
        length(var.urgent_notification_channels) == 0
      )
      error_message = "require_alert_channels is true but no alert policy has a notification channel: set extra_notification_channels and/or urgent_notification_channels to the ids of channels made in the console (docs/operations.md, \"Alert notification channels\"). #1817: 21 policies reached nobody."
    }
  }
}

# ---------------------------------------------------------------------------
# Execution failures.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "job_failures" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-job-executions-failing"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Failed Cloud Run Job executions"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "metric.type = \"run.googleapis.com/job/completed_execution_count\"",
        "metric.labels.result = \"failed\"",
        "resource.labels.job_name = starts_with(\"${var.name_prefix}-\")",
        local.region_filter,
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.job_failure_threshold
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.job_name"]
      }
    }
  }

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = "Agent executions are failing faster than the retry policy absorbs. Look at the attempt records in Firestore for exit codes and at the peak-RSS metric for OOM near misses before assuming the runner image is at fault.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Fencing races. Should be flat zero.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "generation_fenced" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-generation-fencing"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Workers exiting on a stale fencing generation"

    condition_threshold {
      # The verification and acceptance tenants fence at startup by design:
      # 215 of the 270 fences in the 7 days archived issue 1817 measured were
      # verify-remote's own. Filtered on the metric's tenant_id label
      # (metrics.tf, EXTRACT(jsonPayload.labels.tenant_id)) so a real race in
      # any other tenant still fires.
      #
      # And on its submitter_service label (FENCE-SUBMITTER, part of archived issue 1822):
      # verify-remote submits as eng, so its fences carry eng's tenant id
      # (eng 347, smoke 57 over the 7 days measured 2026-10-22), and only the
      # submitter tells them from eng's real races. A fence whose line names
      # no submitter has an empty label and is still counted.
      filter = join(" AND ", concat(
        [
          "resource.type = \"cloud_run_job\"",
          "metric.type = \"logging.googleapis.com/user/${google_logging_metric.events["generation_fenced"].name}\"",
        ],
        length(var.fencing_alert_excluded_tenants) == 0 ? [] : [
          "NOT metric.label.tenant_id = one_of(${join(", ", [for t in var.fencing_alert_excluded_tenants : "\"${t}\""])})",
        ],
        length(var.fencing_alert_excluded_submitters) == 0 ? [] : [
          "NOT metric.label.submitter_service = one_of(${join(", ", [for s in var.fencing_alert_excluded_submitters : "\"${s}\""])})",
        ],
      ))

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = "Fencing is doing its job, but it should rarely have to. A sustained rate means two attempts for the same task are being dispatched -- look for a reconciler reclaiming leases that were still alive, or a dispatch timeout set below real cold-start time.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Dead letters: tasks and wake messages.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "dead_letter_backlog" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-wake-messages-dead-lettered"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "Undelivered messages on the wake dead-letter subscription"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_subscription\"",
        "resource.labels.subscription_id = \"${var.dead_letter_subscription}\"",
        "metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = "Wake messages are being dead-lettered, so the scheduler push endpoint is rejecting them. The one-minute safety tick is carrying the platform in the meantime; latency is degraded, throughput is not.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

resource "google_monitoring_alert_policy" "wake_backlog" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-scheduler-not-draining"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "Wake subscription backlog sustained"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_subscription\"",
        "resource.labels.subscription_id = \"${var.wake_subscription}\"",
        "metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.undelivered_wake_threshold
      duration        = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = "Wake messages are arriving faster than the scheduler acknowledges them. Check whether the drain loop is hitting its bound every pass -- that is the expected shape under genuine saturation and is not itself a fault.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# Checkpointing stopped.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "tasks_dead_lettered" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-tasks-dead-lettered"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Tasks exhausted every attempt"

    condition_threshold {
      # A dead-letter can be written by the worker (Cloud Run Jobs or a GKE pod)
      # or by the scheduler (a Cloud Run service), so the restriction names all
      # three. Monitoring REQUIRES a resource.type restriction, and naming only
      # one would silently drop the other two sources.
      filter = join(" AND ", [
        "resource.type = one_of(\"cloud_run_job\", \"cloud_run_revision\", \"k8s_container\")",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.events["dead_lettered"].name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "600s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.labels.tenant_id"]
      }
    }
  }

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = "Tasks have exhausted their attempts and been dead-lettered. These never retry on their own; somebody has to look at them.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# A BACKEND THAT CANNOT DISPATCH AT ALL
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. On 2026-09-23 a thirty-step workflow failed and the split was
# exactly along backend lines: 22 Cloud Run steps succeeded, all 6 GKE steps
# failed. Every `browser` task the platform had ever accepted -- seven, over two
# days -- had failed the same way, and nothing said so. `make smoke` was green
# throughout because it only ever submitted a `mock` task, which is Cloud Run.
#
# The counters to catch it already existed. `swarm_scheduler_dispatch_failures_total`
# is labelled by backend and was incrementing the whole time; nothing watched
# it. A metric nobody alerts on is a metric nobody has.
#
# THRESHOLD IS ZERO, DELIBERATELY. A dispatch failure is not a task failure: the
# lease is returned and the work is retried, so a handful are survivable and
# invisible to a user. What is not survivable is a backend that refuses EVERY
# dispatch, and the only honest threshold between those two is "more than none,
# sustained". 900s of continuous failure on one backend is not a blip.
#
# GROUPED BY BACKEND, which is the whole point: an aggregate across backends
# stays comfortably low while one of them is totally dead, because the healthy
# one dominates the sum. That aggregation is precisely how this hid.
resource "google_monitoring_alert_policy" "dispatch_failing_by_backend" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-dispatch-failing-on-a-backend"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "A dispatch backend is refusing work"

    condition_threshold {
      # A logs-based metric, not the Prometheus counter: the counter has no
      # descriptor until a failure is exported, so an alert on it cannot be
      # created before the failure it exists for. metrics.tf, dispatch_failures.
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.dispatch_failures.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "900s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        # The label that makes a dead backend visible instead of averaged away.
        group_by_fields = ["metric.labels.backend"]
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = <<-DOC
      One dispatch backend is failing every attempt while the others may be fine.

      Check which: the alert groups by `backend`, so the firing series names it.

      * `GKE_AUTOPILOT` — the `browser` and `claude-code` profiles use it
        (`claude-code` since contract request 53, 2026-10-08). Confirm the tenant namespace exists (`swarm-tenant-<id>`, and note
        the scheduler and `kubernetes/render.py` must agree on that spelling),
        that `kubernetes/rbac/dispatcher-rbac.yaml` is applied in it, and that
        the scheduler holds `container.jobs.create`. Kubernetes authorises
        before it resolves, so a MISSING NAMESPACE reports as
        `jobs.batch is forbidden` — a 403 about permissions, never a 404. Do
        not spend the investigation on IAM before checking the namespace exists.
      * `CLOUD_RUN_JOB` — check `run.jobs.create` on the scheduler and the
        Artifact Registry image tag the profile resolves to.

      `dispatch ok` and `dispatch failed` are both logged per attempt with the
      backend on the line.${local.alert_docs_suffix}
    DOC
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = var.labels
}

# ---------------------------------------------------------------------------
# THE RECONCILER CANNOT SEE A BACKEND, OR IS HOLDING A LEASE IT CANNOT PROVE DEAD
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. From 2026-09-16 the reconciler's GKE listing failed on
# essentially every pass -- a cluster-scope `list jobs` its namespaced Role
# could never authorise -- and nothing paged. On 2026-09-24 that blindness held
# five dead leases of workflow wf_ebb3ab2d65664707a559 for hours: 10 units on
# every one of seven pools, `resource:browser` at its hard limit, the workflow
# stuck RUNNING and a user's cancel ignored. The reconciler said so on every
# pass, in two log lines, and each pass still reported `findings=0` -- the same
# number a healthy pass reports. `/reconcile` answers 200 either way, so the
# 5xx policy above could not see it either; turning a blind pass into a 5xx
# was considered and not done, because it would make Cloud Scheduler retry a
# pass that cannot succeed and page on the symptom rather than the cause.
#
# So the two lines themselves are counted, and this policy watches the counts:
#
#   * "backend unavailable; skipping its findings" -- a whole backend was
#     unreadable. More than ten such passes in fifteen minutes (the tick is
#     */1, so fifteen passes) is not a blip. Grouped by backend, so one blind backend is not
#     averaged away by a healthy one.
#   * "not repairing: ..." -- one lease detected as dead and held back because
#     the backend that would prove it could not be read. The SAME lease refused
#     on every pass for more than thirty minutes is a stranded slot, however
#     healthy the backend looks overall -- which is exactly the per-namespace
#     case the first condition cannot see.
#
# THE MESSAGES ARE THE EMITTER'S, VERBATIM. reconciler/repair.py spells them
# once, as BACKEND_UNAVAILABLE and NOT_REPAIRING, and
# tests/unit/control_plane/test_reconciler_gke_namespaced.py parses this file
# and asserts each string is in the `filter` of the metric below that counts it
# (a comment quoting it, like this one, does not count): a log-based metric
# whose filter matches nothing is a number that stays at zero and an alert that
# never fires.
locals {
  reconciler_services = coalescelist(
    [for s in var.service_names : s if endswith(s, "-reconciler")],
    ["${var.name_prefix}-reconciler"],
  )

  # Logging query syntax, not Monitoring's: `field=(a OR b)`, not one_of().
  reconciler_log_filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=(${join(" OR ", [for s in local.reconciler_services : "\"${s}\""])})",
    "resource.labels.location=\"${var.region}\"",
    # reconciler.logs.StructuredLogger stamps every line with this component.
    "jsonPayload.component=\"reconciler\"",
  ])
}

resource "google_logging_metric" "reconciler_backend_unavailable" {
  project = var.project_id
  name    = "${var.name_prefix}/reconciler-backend-unavailable"

  description = "A reconciliation pass could not list a backend at all and made no conclusions about anything on it."

  filter = join(" AND ", [
    local.reconciler_log_filter,
    "jsonPayload.message=\"backend unavailable; skipping its findings\"",
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

  # A per-call field, so top level: StructuredLogger spreads keyword fields
  # into the record rather than under a nested `labels` object.
  label_extractors = {
    backend = "EXTRACT(jsonPayload.backend)"
  }

  # Readable by the alert policy only some minutes after it is created: the
  # policy waits on this create-time sleep (log_metric_settle_seconds, metrics.tf).
  # Runs on create or replace only. on_failure = continue: a failed sleep must
  # not taint the metric, which the next apply would then replace.
  provisioner "local-exec" {
    command    = "sleep ${local.log_metric_settle_seconds}"
    on_failure = continue
  }
}

resource "google_logging_metric" "reconciler_not_repairing" {
  project = var.project_id
  name    = "${var.name_prefix}/reconciler-not-repairing"

  description = "The reconciler detected a dead lease and held it back because the backend or namespace that would prove it was unreadable."

  filter = join(" AND ", [
    local.reconciler_log_filter,
    "jsonPayload.message=\"not repairing: the backend that would hold this execution was unreadable\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    # One series per held lease. Cardinality is bounded by the number of
    # leases held back at once, which is zero on a healthy platform.
    labels {
      key        = "lease_id"
      value_type = "STRING"
    }

    labels {
      key        = "backend"
      value_type = "STRING"
    }
  }

  label_extractors = {
    lease_id = "EXTRACT(jsonPayload.lease_id)"
    backend  = "EXTRACT(jsonPayload.backend)"
  }

  # Readable by the alert policy only some minutes after it is created: the
  # policy waits on this create-time sleep (log_metric_settle_seconds, metrics.tf).
  # Runs on create or replace only. on_failure = continue: a failed sleep must
  # not taint the metric, which the next apply would then replace.
  provisioner "local-exec" {
    command    = "sleep ${local.log_metric_settle_seconds}"
    on_failure = continue
  }
}

resource "google_monitoring_alert_policy" "reconciler_blind" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-reconciler-cannot-see-a-backend"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "A backend was unreadable on 3 or more reconciliation passes in 15 minutes"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.reconciler_backend_unavailable.name}\"",
      ])

      # More than 10 in a trailing 15-minute window: most of the fifteen
      # passes of a */1 tick. It was more than 2, all three passes of the */5
      # tick, and the window is kept so the alert still means "blind for most
      # of a quarter of an hour", not "blind for three minutes". Not all 15:
      # a pass that overruns its tick answers 409 and logs nothing, and one
      # skipped pass must not hide a backend that stayed blind.
      comparison      = "COMPARISON_GT"
      threshold_value = 10
      duration        = "0s"

      aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.labels.backend"]
      }
    }
  }

  conditions {
    display_name = "The same lease held back as unprovable for more than 30 minutes"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.reconciler_not_repairing.name}\"",
      ])

      # Refused at least once in every trailing 10-minute window (two passes,
      # so one late tick does not reset it), continuously for 30 minutes.
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "1800s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.labels.lease_id"]
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = <<-DOC
      The reconciler is holding leases it believes are dead because it cannot
      read the backend that would prove it. Those leases keep their capacity on
      every pool they hold, so admission for that tenant, profile or resource
      class stalls behind them -- and a cancel on those tasks cannot complete.

      Read the latest pass: `findings_suppressed`, `suppressed[]` (kind, lease,
      task, backend, namespace) and `unreadable_namespaces` in
      `reconciler_passes`, or the `reconciliation pass complete` log line.

      * `GKE_AUTOPILOT` — the reconciler reads tenant namespaces one at a time
        under the namespaced `swarm-reaper` Role. A 403 there means the
        namespace does not exist OR its `swarm-reaper` RoleBinding does not
        name the reconciler by BOTH email and numeric uniqueId; Kubernetes
        authorises before it resolves, so the two look identical. Do not add a
        ClusterRole.
      * `CLOUD_RUN_JOB` — check the reconciler's `run.executions.list`.

      Runbook: docs/troubleshooting.md, "A task is stuck in LEASED or
      DISPATCHED".${local.alert_docs_suffix}
    DOC
  }

  alert_strategy {
    auto_close = "3600s"
  }

  # Merged, not just inherited: this policy must carry the marker `make
  # destroy` keys on even if a caller passes labels without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# A step spec did not verify (contract request 34, decision 9).
#
# ONE IS ENOUGH. A refusal is either an agent rewriting a parked step's
# document -- the attack archived issue 342 is about -- or a platform bug that fails real
# work; neither is noise, and neither is fixed by waiting. Not pinned to
# cloud_run_job: a GKE browser pod verifies the same way and its refusal pages
# too (the metric's filter already names both workers' resource types).
#
# TWO CONDITIONS, ONE POLICY. The second is the upstream half: a merge or
# post-verdict worker refusing an upstream step's spec (MERGE_REFUSED /
# VERDICT_REFUSED with an `upstream:` reason; metrics.tf,
# spec_upstream_invalid). It is the same attack one step later -- an agent
# rewrote a step the chain depends on -- so it pages the same people.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "spec_signature_invalid" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-spec-signature-invalid"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "A worker refused a task whose step spec did not verify"

    condition_threshold {
      # Run 36655830725 (main 4bb7564): "Error creating AlertPolicy: googleapi:
      # Error 400: ... must specify a restriction on resource.type". Monitoring
      # requires one on every log-based metric's condition; naming only one
      # type would silently drop the other worker (the tasks_dead_lettered
      # policy above explains the same requirement). Both types here match the
      # metric's own filter (metrics.tf), so a version signed by either
      # backend still pages.
      filter = join(" AND ", [
        "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.spec_signature_invalid.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant_id", "metric.label.job_name", "metric.label.reason"]
      }
    }
  }

  conditions {
    display_name = "A merge or post-verdict worker refused an upstream step's spec"

    condition_threshold {
      # The same resource.type restriction as the condition above, for the
      # same reason (run 36655830725).
      filter = join(" AND ", [
        "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.spec_upstream_invalid.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant_id", "metric.label.job_name", "metric.label.end_cause", "metric.label.reason"]
      }
    }
  }

  notification_channels = local.channels_by_severity["CRITICAL"]

  documentation {
    mime_type = "text/markdown"
    content   = "A worker refused to run a task because its step spec did not verify (end cause `spec_signature_invalid`). Every occurrence is an agent rewriting a parked step's task document or a platform bug. Read the worker's ERROR line (`spec signature invalid: refusing to run this task`): `spec_check.reason` says which check failed and `spec_check.task_id` which task. `unsigned` after the legacy window, or `foreign_key_version` right after a key rotation, is a rollout problem (docs/runbooks/spec-signing-rollout.md); `signature_mismatch` or `not_canonical` on a task nobody resubmitted is the attack -- look at who wrote the task document after its submission. The second condition is a merge or post-verdict worker refusing an UPSTREAM step's spec (end cause `merge_refused` or `verdict_refused`, `spec_check.reason` = `upstream:<task id>:<why>`): the same reading applies to the named upstream task, and nothing reached the forge.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  # Merged, not just inherited, like the policy above: this one must carry the
  # marker `make destroy` keys on even if a caller passes labels without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# A merge or post-verdict worker action ended unmerged or unposted (the four
# end causes; metrics.tf, worker_action_ended).
#
# TWO CONDITIONS, ONE POLICY, split the way the end causes are: _refused means
# a condition for merging or posting was not met and nothing changed on the
# forge; _failed means the action was allowed and the forge did not do it.
# They are read differently (a stale head or a red check against a forge
# outage), so each condition says which it is. Either way the workflow's last
# step did not land and is never retried on its own, so ONE is enough, as for
# a dead-lettered task -- and WARNING, as for a dead-lettered task: somebody
# has to look. The one refusal that may be an attack, `spec_unverified` (an
# upstream step's spec did not verify), is named in the documentation below.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "worker_action_ended" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-worker-action-ended"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "A merge or post-verdict was refused (merge_refused, verdict_refused)"

    condition_threshold {
      # Monitoring requires a resource.type restriction on a log-based
      # metric's condition (run 36655830725); both worker types, matching the
      # metric's own filter.
      filter = join(" AND ", [
        "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.worker_action_ended.name}\"",
        "metric.label.end_cause = one_of(\"merge_refused\", \"verdict_refused\")",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant_id", "metric.label.end_cause"]
      }
    }
  }

  conditions {
    display_name = "A merge or post-verdict failed at the forge (merge_failed, verdict_failed)"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = one_of(\"cloud_run_job\", \"k8s_container\")",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.worker_action_ended.name}\"",
        "metric.label.end_cause = one_of(\"merge_failed\", \"verdict_failed\")",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant_id", "metric.label.end_cause"]
      }
    }
  }

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = "A merge or post-verdict step ended without merging its pull request or posting its review. Read the worker's ERROR line `worker action ended`: `end_cause` says which action and whether it was refused (`merge_refused`, `verdict_refused`: a condition was not met and nothing changed on the forge) or failed (`merge_failed`, `verdict_failed`: the action was allowed and the forge did not do it), and `code` says why. The task's `result_summary.merge.refusal` or `result_summary.verdict.refusal` carries the same code and message. These are never retried on their own. A `code` of `spec_unverified` means an upstream step's spec did not verify: read it as the spec-signature alert says (an agent rewrote a step the chain depends on, or a platform bug) and look at the named upstream task. See docs/merge-step.md section 6.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  # Merged, not just inherited, like the spec-signature policy: this one must
  # carry the marker `make destroy` keys on even if a caller passes labels
  # without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# A SCHEDULE NEEDS A PERSON (docs/schedules.md §4.9, lane S4)
# ---------------------------------------------------------------------------
#
# One policy, two conditions, on the existing alert channel (alert_emails), as
# §4.9 says: "the same alert". Either fires it. Both are one event being
# enough: an auto-paused schedule fires nothing until a member resumes it, and
# a hold that needs the owner waits for the owner and nobody else. Neither
# holds capacity while it waits (invariant 1), so the cost of the wait is
# lateness, which is what the email is for.
resource "google_monitoring_alert_policy" "schedule_needs_attention" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-schedule-needs-attention"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "A schedule was auto-paused (schedule_auto_paused)"

    condition_threshold {
      # Monitoring requires a resource.type restriction on a log-based
      # metric's condition; swarm-api is a Cloud Run service.
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.schedule_auto_paused.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.code"]
      }
    }
  }

  conditions {
    display_name = "A scheduled run needs the owner (schedule_needs_owner)"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.schedule_needs_owner.name}\"",
      ])

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

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = "A schedule needs a person. **Auto-paused:** the schedule tick paused a schedule and it fires nothing until a member resumes it; the series names the pause code, and swarm-api's WARNING line `schedule <id> auto_paused: <code>` names the schedule. Admin › Schedules shows it with its tenant and reason. Resume it once the cause is fixed; its catch-up policy decides what the missed slots do. **Needs the owner:** a scheduled run met a hard stop only the owner may lift; the series names the tenant and the inbox item is marked owner. Neither holds capacity while it waits. See docs/schedules.md sections 1.3, 4.4 and 4.9.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  # Merged, not just inherited, as worker_action_ended's: the marker `make
  # destroy` keys on must be there even if a caller passes labels without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# An approved personal workspace is waiting (metrics.tf, workspace_stuck).
#
# ONE IS ENOUGH: a person who was told "approved" is waiting on a build that
# is not coming, and on 2026-10-09 that wait lasted 19 hours unseen. The sweep
# logs each stuck record once an hour, so a 30-minute window catches every
# one, and a record that stays stuck re-notifies hourly rather than going
# quiet after the first page. WARNING, like a dead-lettered task: nothing is
# broken for anyone else, but a person is blocked until someone looks.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "workspace_stuck" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-workspace-stuck"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "An approved personal workspace has waited with nothing building it"

    condition_threshold {
      # Monitoring requires a resource.type restriction on a log-based
      # metric's condition, matching the metric's own filter.
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.workspace_stuck.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "1800s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.reason"]
      }
    }
  }

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = "An approved personal workspace is waiting and nothing is building it. Read swarm-api's `workspace_stuck` log entries (jsonPayload.event): `workspace_id` is the record (open it in Admin -> People), `minutes_waiting` how long. `reason` says why: `publishing_off` means WORKSPACE_APPLY_PUBLISH is off (terraform var.workspace_apply_publish), so no approval is ever sent to the workspace job -- turn it on in the same release as the owner's bootstrap apply with enable_workspace_deployer (docs/workspaces.md section 10); `never_dispatched` means no publish was attempted for 15 minutes; `dispatched_unclaimed` means a publish is more than 30 minutes old and no build claimed it -- check the swarm-workspace-apply topic and its Cloud Build trigger. Retry from People once the cause is fixed. See docs/workspaces.md section 2.2.${local.alert_docs_suffix}"
  }

  alert_strategy {
    auto_close = "86400s"
  }

  # Merged, not just inherited, like the worker-action policy: this one must
  # carry the marker `make destroy` keys on even if a caller passes labels
  # without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ===========================================================================
# The four operational monitors: the front door's uptime, the reconciler's
# pass duration, Cloud NAT dropping packets, and a browser secret granted
# outside swarm-api's slot. Each reads a signal something already writes;
# the emitter is cited above each.
# ===========================================================================

locals {
  # The gateway terraform/modules/network creates: google_compute_router_nat
  # "this", named "${var.name_prefix}-nat".
  nat_gateway_name = coalesce(var.nat_gateway_name, "${var.name_prefix}-nat")

  # `[.]` rather than `\.`, as workspace_alerts.tf: inside a quoted Logging
  # string a backslash is the query language's escape before the regex's.
  browser_project_re = replace(var.project_id, ".", "[.]")
  swarm_api_email    = "swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # The three secret shapes swarm_api.browser_slots accepts, by the tail of
  # resourceName (projects/<number>/secrets/<name>): a workflow's carry
  # (-browser-wf-), a park's browser (-browser-park-) and a named login
  # (-browser-login-). Any tenant, not only u-*: swarm-api refuses group
  # tenants, so an entry on a group tenant's browser secret is someone else.
  browser_secret_re = "/secrets/swarm-tenant-[a-z0-9-]+-browser-(wf|park|login)-[^/]+$"

  # What a slot grant is: the tenant's personal worker
  # (swarm_api.browser_slots.worker_member, swarm-agent-worker-<tenant>, a
  # personal tenant's id starting u-) on SLOT_ROLES, nothing else.
  browser_member_re = "^serviceAccount:swarm-agent-worker-u-[a-z0-9-]+@${local.browser_project_re}[.]iam[.]gserviceaccount[.]com$"
  browser_roles_re  = "^roles/secretmanager[.](secretAccessor|secretVersionAdder)$"
}

# ---------------------------------------------------------------------------
# The front door: the API's health route through the load balancer.
#
# THE SIGNAL: Cloud Monitoring's own uptime check on
# https://<front_door_hostname>/healthz -- the swarm-api route
# (swarm_api/routes/health.py) the front door's URL map sends to the API's
# backend (terraform/modules/frontend, the API path rule).
#
# THE HEALTHY ANSWER IS 302, NOT 200. That backend has IAP on, and IAP
# answers every unauthenticated request with a redirect to sign-in before
# swarm-api sees it. An uptime check follows no redirect, and its service-
# agent token cannot pass IAP (IAP here admits only an allowlisted OAuth
# client's audience). So the check proves the hostname resolves, the
# certificate is valid, the forwarding rule and URL map route /healthz, and
# IAP is gating it. It cannot see swarm-api itself: service_errors and the
# safety tick watch that. A 200 here would mean IAP is OFF on the API, which
# is why 2xx is NOT accepted (docs/web-ui/01-reachability.md: an IAP 302 must
# never be read as the API's 200). The owner's choice: questions.json of the
# pull request that added this.
# ---------------------------------------------------------------------------
resource "google_monitoring_uptime_check_config" "front_door" {
  count = var.create_alerts && var.front_door_hostname != "" ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-front-door-healthz"
  period       = var.front_door_check_period
  timeout      = var.front_door_check_timeout

  http_check {
    path           = "/healthz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_value = 302
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.front_door_hostname
    }
  }

  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

resource "google_monitoring_alert_policy" "front_door_down" {
  count = var.create_alerts && var.front_door_hostname != "" ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-front-door-down"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "The front door's /healthz failed from more than ${var.front_door_failing_regions} region(s) for ${var.front_door_down_seconds}s"

    condition_threshold {
      # A built-in metric, written by the check itself: no log-based metric,
      # so nothing to wait for.
      filter = join(" AND ", [
        "metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\"",
        "resource.type = \"uptime_url\"",
        "metric.labels.check_id = \"${google_monitoring_uptime_check_config.front_door[0].uptime_check_id}\"",
      ])

      # Count the regions whose latest check failed (Cloud Monitoring's own
      # uptime-alert shape): ALIGN_NEXT_OLDER keeps each region's last
      # result, REDUCE_COUNT_FALSE counts the failures.
      comparison      = "COMPARISON_GT"
      threshold_value = var.front_door_failing_regions
      duration        = "${var.front_door_down_seconds}s"

      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.project_id", "resource.label.host"]
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = <<-DOC
      The front door stopped giving IAP's sign-in redirect for `/healthz` from
      more than one checker region. People cannot reach the console or the
      API through it; workers, the scheduler and the reconciler do not use it
      and keep running.

      Read the uptime check's last result (Monitoring -> Uptime checks): the
      status code says which layer failed.

      * TLS or certificate error -- the managed certificate expired or no
        longer matches the hostname's A record (`terraform/modules/frontend`,
        output `ip_address`).
      * DNS failure -- the A record at the registrar is gone or points
        elsewhere.
      * 404 -- the URL map no longer routes `/healthz` to the API backend.
      * 200 -- IAP is OFF on the API's backend. Treat as an access-control
        incident: the API is reachable without sign-in.
      * 5xx -- the load balancer cannot reach IAP or the backend service.

      The check cannot see swarm-api itself behind IAP; `control-plane-5xx`
      watches that.${local.alert_docs_suffix}
    DOC
  }

  alert_strategy {
    auto_close = "1800s"
  }

  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# The reconciler's passes are slower than its tick.
#
# THE EMITTER: reconciler/repair.py, Reconciler.run_once, ends every pass with
#     self._log.info("reconciliation pass complete", **report.as_dict() ...)
# through reconciler.logs.StructuredLogger, which writes the keyword fields at
# the TOP LEVEL of jsonPayload with component="reconciler". PassReport.as_dict
# carries `duration_seconds`: (finished_at - started_at) in float seconds,
# rounded to the millisecond. scripts/bench-reconcile.sh reads the same line.
# tests/unit/control_plane/test_reconciler_gke_namespaced.py runs a real pass
# and holds that line to this metric's filter and extractor.
#
# WHY IT MATTERS: the tick is */1 and a pass holds a lock; a pass still running
# when the next tick arrives makes that tick answer 409 already_running
# (reconciler/service.py) and log nothing. Passes slower than a minute are
# passes skipped: dead leases held longer, capacity released later.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "reconciler_pass_duration" {
  project = var.project_id
  name    = "${var.name_prefix}/reconciler-pass-duration"

  description = "Seconds each reconciliation pass took (the `duration_seconds` of the reconciler's `reconciliation pass complete` line)."

  filter = join(" AND ", [
    local.reconciler_log_filter,
    "jsonPayload.message=\"reconciliation pass complete\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }

  value_extractor = "EXTRACT(jsonPayload.duration_seconds)"

  # Seconds. Fine where passes live (p50 3.0s, p90 8.7s, measured 2026-09-24/25),
  # a bound at 60 so the default threshold is exact, and up to 900s, the
  # reconciler service's request timeout (terraform/infra, swarm-reconciler),
  # past which no pass completes to log.
  bucket_options {
    explicit_buckets {
      bounds = [1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900]
    }
  }

  # Readable by the alert policy only some minutes after it is created: the
  # policy waits on this create-time sleep (log_metric_settle_seconds, metrics.tf).
  # Runs on create or replace only. on_failure = continue: a failed sleep must
  # not taint the metric, which the next apply would then replace.
  provisioner "local-exec" {
    command    = "sleep ${local.log_metric_settle_seconds}"
    on_failure = continue
  }
}

resource "google_monitoring_alert_policy" "reconciler_pass_slow" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-reconciler-pass-slow"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "p99 reconciliation pass above ${var.reconciler_pass_slow_seconds}s for ${var.reconciler_pass_slow_duration_seconds}s"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.reconciler_pass_duration.name}\"",
      ])

      # Five passes per 300s window at the */1 tick, so the p99 is the
      # window's slowest pass; the max across revisions, so a second
      # instance's fast passes do not hide a slow one.
      comparison      = "COMPARISON_GT"
      threshold_value = var.reconciler_pass_slow_seconds
      duration        = "${var.reconciler_pass_slow_duration_seconds}s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  notification_channels = local.channels_by_severity["WARNING"]

  documentation {
    mime_type = "text/markdown"
    content   = <<-DOC
      Reconciliation passes are taking longer than the one-minute tick, so
      ticks are being skipped (a tick that finds a pass running answers 409
      and does nothing). Dead leases are released and stranded resources
      collected later than they should be.

      Read the recent `reconciliation pass complete` lines (or the
      `reconciler_passes` collection): `duration_seconds` against
      `tasks_examined`, `leases_examined`, `executions_examined`, `sweep`
      (`full` passes read more) and `errors`. A backend listing that has grown,
      or a slow Firestore read, shows there. `scripts/bench-reconcile.sh`
      measures pass time on demand.${local.alert_docs_suffix}
    DOC
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# Cloud NAT is dropping the platform's outbound packets.
#
# THE EMITTER: Cloud NAT itself. Every NAT gateway writes
# router.googleapis.com/nat/dropped_sent_packets_count (resource nat_gateway,
# label `reason`) -- a built-in metric, so no log-based metric and no wait.
# The gateway is terraform/modules/network's google_compute_router_nat "this",
# `<name_prefix>-nat`, which also logs each drop (log_config ERRORS_ONLY, the
# nat_flows log the 2026-10-09 incident read). With endpoint-independent
# mapping off, every drop is OUT_OF_RESOURCES: a worker's new connection to a
# model provider or a forge that never opened.
#
# The filter names the gateway and the region: the project is shared, and
# another team's NAT drops are not ours to page on.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "nat_drops" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-nat-dropping-packets"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "${local.nat_gateway_name} dropped packets in every ${var.nat_drop_window_seconds}s window for ${var.nat_drop_duration_seconds}s"

    condition_threshold {
      filter = join(" AND ", [
        "metric.type = \"router.googleapis.com/nat/dropped_sent_packets_count\"",
        "resource.type = \"nat_gateway\"",
        "resource.labels.gateway_name = \"${local.nat_gateway_name}\"",
        "resource.labels.region = \"${var.region}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = var.nat_drop_threshold
      duration        = "${var.nat_drop_duration_seconds}s"

      aggregations {
        alignment_period     = "${var.nat_drop_window_seconds}s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.labels.reason"]
      }
    }
  }

  notification_channels = local.channels_by_severity["ERROR"]

  documentation {
    mime_type = "text/markdown"
    content   = <<-DOC
      Cloud NAT has been dropping the platform's outbound packets for fifteen
      minutes. Workers' new connections to model providers and forges fail
      or stall; attempts time out and retry.

      `reason` OUT_OF_RESOURCES (the only one possible with endpoint-
      independent mapping off) means a VM or pod ran out of NAT ports: read
      the drops with

          gcloud logging read 'resource.type="nat_gateway" AND jsonPayload.allocation_status="DROPPED"'

      and see whether they come from one source. Raise
      `nat_max_ports_per_vm` or `nat_static_ip_count` (terraform/infra) if
      the platform's own concurrency grew; look for a runaway connection loop
      in one attempt if not. docs/incidents/2026-10-09-egress-open-delay.md
      has the earlier reading.${local.alert_docs_suffix}
    DOC
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}

# ---------------------------------------------------------------------------
# A browser secret was granted outside swarm-api's slot.
#
# THE EMITTER: Secret Manager's Admin Activity audit log, which cannot be
# turned off. swarm_api.browser_slots (BrowserSlots._bind_only) grants a
# signed-in browser session's secret by set_iam_policy on that one secret,
# making it exactly the tenant's personal worker on secretAccessor and
# secretVersionAdder; every such call writes a SetIamPolicy entry with the
# caller in authenticationInfo and the change in serviceData.policyDelta. The
# app logs no line of its own on a grant, so this is the signal.
#
# THE ONE EXPECTED GRANT DOES NOT PAGE: swarm-api, a personal worker, the two
# slot roles. Anything else on a browser secret -- another principal writing
# its policy, a member who is not a personal worker, a third role -- is a
# person's signed-in session readable by someone it was not made for
# (terraform/bootstrap/browser_logins.tf: "IAM IS THE WHOLE BOUNDARY").
# Each `!~` on the repeated bindingDeltas is true when ANY delta fails it, as
# workspace_alerts.tf relies on. One consequence: swarm-api's own rewrite
# REMOVING an intruder also pages, which is the right thing to look at.
#
# A filter cannot compare the member's tenant with the secret's; swarm-api
# checks that (the secret's swarm-tenant label) before it binds.
# ---------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "browser_secret_grant" {
  count = var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-browser-secret-grant-outside-slot"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "A browser-session secret's IAM was written other than by swarm-api's slot grant"

    condition_matched_log {
      filter = join(" AND ", [
        "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\"",
        "protoPayload.serviceName=\"secretmanager.googleapis.com\"",
        "protoPayload.methodName=~\"(?i)setiampolicy$\"",
        "protoPayload.resourceName=~\"${local.browser_secret_re}\"",
        "(${join(" OR ", [
          "NOT protoPayload.authenticationInfo.principalEmail=\"${local.swarm_api_email}\"",
          "protoPayload.serviceData.policyDelta.bindingDeltas.member!~\"${local.browser_member_re}\"",
          "protoPayload.serviceData.policyDelta.bindingDeltas.role!~\"${local.browser_roles_re}\"",
        ])})",
      ])
    }
  }

  notification_channels = local.channels_by_severity["CRITICAL"]

  documentation {
    mime_type = "text/markdown"
    content   = <<-DOC
      A signed-in browser session's secret (`swarm-tenant-<tenant>-browser-wf-*`,
      `-browser-park-*` or `-browser-login-*`) had its IAM policy written by
      someone other than swarm-api, or given a member that is not a personal
      worker (`swarm-agent-worker-u-*`), or a role other than
      `secretAccessor` / `secretVersionAdder`. That secret holds a person's
      logged-in session; whoever the binding names can act as them on the
      site it is for.

      1. Read the entry (it is linked from the incident): the principal,
         the secret, and `serviceData.policyDelta.bindingDeltas`.
      2. Remove the binding, or delete the secret: the person signs in again.
      3. If the principal was not swarm-api, find how it came to hold
         `secretmanager.secrets.setIamPolicy` on the secret. If it was
         swarm-api, its slot check let the call through: read swarm-api's
         log for `browser-session/slot` around the entry's time.${local.alert_docs_suffix}
    DOC
  }

  # A log-match policy must say how often it may notify; it opens one incident
  # per matching entry otherwise.
  alert_strategy {
    notification_rate_limit {
      period = "${var.browser_secret_grant_notify_seconds}s"
    }
    auto_close = "1800s"
  }

  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}
