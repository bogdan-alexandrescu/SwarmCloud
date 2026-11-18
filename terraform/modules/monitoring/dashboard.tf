# Dashboards.
#
# Built with jsonencode rather than a heredoc so a malformed dashboard fails at
# `terraform validate` instead of at apply, and so the tile filters can be
# composed from the same strings the alert policies use.

locals {
  user_metric = "logging.googleapis.com/user"

  dashboard_tiles = [
    {
      title    = "Attempts started"
      note     = "Throughput, counted where the platform can actually see it: one line per attempt the worker began."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.events["starting"].name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.runner_profile"]
    },
    {
      title    = "Parked work by reason"
      note     = "Not errors. PARKED costs nothing, so a tall bar is the platform declining to burn compute on a wait, split by ParkReason."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.events["parked"].name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.park_reason"]
    },
    {
      title    = "Parked work by tenant"
      note     = "The same signal grouped the other way: one tenant parking while the rest run is a credential or quota problem, not a platform one."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.events["parked"].name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.tenant_id"]
    },
    {
      title    = "Provider quota exhaustion"
      note     = "Per provider and tenant: tenants bring their own keys, so one tenant's 429 must not look like an outage."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.events["quota_exhausted"].name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.provider"]
    },
    {
      title    = "Checkpoints completed"
      note     = "Cloud Run ephemeral disk disables live migration, so this line is what makes interruption survivable. Zero is an incident."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.events["checkpoint_completed"].name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = []
    },
    {
      title    = "Fencing generation rejections"
      note     = "Should be flat zero. Anything else means two attempts raced for one task."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.events["generation_fenced"].name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = []
    },
    {
      title    = "Cloud Run Job executions"
      note     = "The primary execution backend. Split by result so failure is visible against volume."
      filter   = "metric.type=\"run.googleapis.com/job/completed_execution_count\" resource.type=\"cloud_run_job\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.result"]
    },
    {
      title    = "Control-plane request rate"
      note     = "Per service and response class."
      filter   = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\""
      aligner  = "ALIGN_RATE"
      reducer  = "REDUCE_SUM"
      group_by = ["resource.label.service_name", "metric.label.response_code_class"]
    },
    {
      title    = "Wake backlog"
      note     = "Undelivered messages on the scheduler wake subscription."
      filter   = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" resource.label.subscription_id=\"${var.wake_subscription}\""
      aligner  = "ALIGN_MAX"
      reducer  = "REDUCE_MAX"
      group_by = []
    },
    {
      title    = "Attempt peak RSS"
      note     = "Feeds the sizing correction: the resource classes are meant to be tuned from production, not arithmetic."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.peak_rss.name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_PERCENTILE_99"
      group_by = ["metric.label.runner_profile"]
    },
    {
      title    = "OOM near misses"
      note     = "Attempts that finished within a hair of their memory limit. requests == limits, so there is no headroom to absorb the next increase."
      filter   = "metric.type=\"${local.user_metric}/${google_logging_metric.oom_near_miss.name}\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.resource_class"]
    },
  ]

  dashboard = {
    displayName      = "Swarm ${var.environment} -- control plane"
    dashboardFilters = []
    mosaicLayout = {
      columns = 12
      tiles = [
        for idx, tile in local.dashboard_tiles : {
          xPos   = (idx % 2) * 6
          yPos   = floor(idx / 2) * 4
          width  = 6
          height = 4
          widget = {
            title = tile.title
            xyChart = {
              chartOptions = { mode = "COLOR" }
              dataSets = [{
                plotType       = "LINE"
                targetAxis     = "Y1"
                legendTemplate = "$${metric.labels}"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = tile.filter
                    aggregation = {
                      alignmentPeriod    = "300s"
                      perSeriesAligner   = tile.aligner
                      crossSeriesReducer = tile.reducer
                      groupByFields      = tile.group_by
                    }
                  }
                }
              }]
              yAxis = {
                label = tile.note
                scale = "LINEAR"
              }
            }
          }
        }
      ]
    }
  }
}

resource "google_monitoring_dashboard" "control_plane" {
  project = var.project_id

  dashboard_json = jsonencode(local.dashboard)
}
