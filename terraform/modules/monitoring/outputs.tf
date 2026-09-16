output "dashboard_id" {
  value = google_monitoring_dashboard.control_plane.id
}

output "log_metric_names" {
  value = sort(concat(
    [for m in google_logging_metric.events : m.name],
    [google_logging_metric.peak_rss.name, google_logging_metric.oom_near_miss.name],
  ))
}

output "log_metric_filters" {
  description = "metric name -> its log filter, so a test can assert each one matches a line some component actually writes."
  value = merge(
    { for k, m in google_logging_metric.events : m.name => m.filter },
    {
      (google_logging_metric.peak_rss.name)      = google_logging_metric.peak_rss.filter
      (google_logging_metric.oom_near_miss.name) = google_logging_metric.oom_near_miss.filter
    },
  )
}

output "notification_channels" {
  value = local.notification_channels
}

output "alert_policy_names" {
  value = var.create_alerts ? [
    google_monitoring_alert_policy.safety_tick_absent[0].display_name,
    google_monitoring_alert_policy.service_errors[0].display_name,
    google_monitoring_alert_policy.job_failures[0].display_name,
    google_monitoring_alert_policy.generation_fenced[0].display_name,
    google_monitoring_alert_policy.dead_letter_backlog[0].display_name,
    google_monitoring_alert_policy.wake_backlog[0].display_name,
    google_monitoring_alert_policy.tasks_dead_lettered[0].display_name,
  ] : []
}
