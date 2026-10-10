output "dashboard_id" {
  value = google_monitoring_dashboard.control_plane.id
}

output "log_metric_names" {
  value = sort(concat(
    [for m in google_logging_metric.events : m.name],
    [google_logging_metric.peak_rss.name, google_logging_metric.oom_near_miss.name, google_logging_metric.spec_signature_invalid.name, google_logging_metric.spec_upstream_invalid.name, google_logging_metric.worker_action_ended.name, google_logging_metric.schedule_auto_paused.name, google_logging_metric.schedule_needs_owner.name, google_logging_metric.workspace_stuck.name, google_logging_metric.pr_stranded.name],
  ))
}

output "log_metric_filters" {
  description = "metric name -> its log filter, so a test can assert each one matches a line some component actually writes."
  value = merge(
    { for k, m in google_logging_metric.events : m.name => m.filter },
    {
      (google_logging_metric.peak_rss.name)               = google_logging_metric.peak_rss.filter
      (google_logging_metric.oom_near_miss.name)          = google_logging_metric.oom_near_miss.filter
      (google_logging_metric.spec_signature_invalid.name) = google_logging_metric.spec_signature_invalid.filter
      (google_logging_metric.spec_upstream_invalid.name)  = google_logging_metric.spec_upstream_invalid.filter
      (google_logging_metric.worker_action_ended.name)    = google_logging_metric.worker_action_ended.filter
      (google_logging_metric.pr_stranded.name)            = google_logging_metric.pr_stranded.filter
      (google_logging_metric.schedule_auto_paused.name)   = google_logging_metric.schedule_auto_paused.filter
      (google_logging_metric.schedule_needs_owner.name)   = google_logging_metric.schedule_needs_owner.filter
      (google_logging_metric.workspace_stuck.name)        = google_logging_metric.workspace_stuck.filter
    },
  )
}

output "notification_channels" {
  value = local.notification_channels
}

# Built with a splat rather than [0], so a policy gated OFF contributes nothing
# instead of failing the whole output.
#
# It used to index [0] unconditionally under `var.create_alerts`, which assumed
# every policy shares that one flag. `safety_tick_absent` has its own gate (it
# was switched off for days waiting on a Cloud Scheduler metric that Google does
# not publish), and the moment it was switched off this output became:
#
#     Error: Invalid index
#     google_monitoring_alert_policy.safety_tick_absent is empty tuple
#
# Neither `terraform validate` nor the mock-provider tests catch that: a count
# only resolves against real variables, so the failure arrives at apply, in a
# deploy, on an output nobody was thinking about.
output "alert_policy_names" {
  value = concat(
    google_monitoring_alert_policy.safety_tick_absent[*].display_name,
    google_monitoring_alert_policy.service_errors[*].display_name,
    google_monitoring_alert_policy.job_failures[*].display_name,
    google_monitoring_alert_policy.generation_fenced[*].display_name,
    google_monitoring_alert_policy.dead_letter_backlog[*].display_name,
    google_monitoring_alert_policy.wake_backlog[*].display_name,
    google_monitoring_alert_policy.tasks_dead_lettered[*].display_name,
    google_monitoring_alert_policy.spec_signature_invalid[*].display_name,
    google_monitoring_alert_policy.worker_action_ended[*].display_name,
    google_monitoring_alert_policy.pr_stranded[*].display_name,
    google_monitoring_alert_policy.schedule_needs_attention[*].display_name,
    google_monitoring_alert_policy.workspace_stuck[*].display_name,
  )
}
