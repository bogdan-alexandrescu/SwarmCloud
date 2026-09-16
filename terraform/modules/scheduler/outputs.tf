output "wake_topic" {
  value = google_pubsub_topic.wake.name
}

output "wake_topic_id" {
  value = google_pubsub_topic.wake.id
}

output "wake_subscription" {
  value = google_pubsub_subscription.wake.name
}

output "dead_letter_topic" {
  value = google_pubsub_topic.dead_letter.name
}

output "scheduler_job_names" {
  value = compact([
    google_cloud_scheduler_job.safety_tick.name,
    google_cloud_scheduler_job.reconciler.name,
    try(google_cloud_scheduler_job.quota_refresh[0].name, ""),
  ])
}

output "safety_tick_schedule" {
  description = "Exposed so a test can assert the safety tick really is every minute."
  value       = google_cloud_scheduler_job.safety_tick.schedule
}
