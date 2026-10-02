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
  value = concat(
    compact([
      google_cloud_scheduler_job.safety_tick.name,
      google_cloud_scheduler_job.reconciler.name,
      try(google_cloud_scheduler_job.quota_refresh[0].name, ""),
    ]),
    [for t in sort(tolist(var.rollup_tenant_ids)) : google_cloud_scheduler_job.workflow_rollup[t].name],
  )
}

output "rollup_sweeper_email" {
  description = "The identity the workflow-rollup jobs present. The root sets swarm-api's ROLLUP_SWEEPER_USERS to it, which is what lets it call POST /v1/admin/workflows/rollup and nothing else."
  value       = local.rollup_sweeper_email
}

output "safety_tick_schedule" {
  description = "Exposed so a test can assert the safety tick really is every minute."
  value       = google_cloud_scheduler_job.safety_tick.schedule
}
