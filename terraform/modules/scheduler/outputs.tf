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
    [for t in sort(tolist(var.rollup_tenant_ids)) : google_cloud_scheduler_job.issue_run_advance[t].name],
    [for t in sort(tolist(var.rollup_tenant_ids)) : google_cloud_scheduler_job.repo_index_poll[t].name],
  )
}

output "rollup_sweeper_email" {
  description = "The identity the workflow-rollup, issue-run-advance and repo-index-poll jobs present. The root sets swarm-api's ROLLUP_SWEEPER_USERS to it, which is what lets it call POST /v1/admin/workflows/rollup, POST /v1/admin/runs/advance and POST /v1/admin/repositories/poll and nothing else."
  value       = local.rollup_sweeper_email
}

output "safety_tick_schedule" {
  description = "Exposed so a test can assert the safety tick really is every minute."
  value       = google_cloud_scheduler_job.safety_tick.schedule
}

output "issue_run_advance_schedule" {
  description = "Exposed so a test can assert how often issue runs advance without a reader (#454)."
  value       = var.issue_run_advance_schedule
}

output "repo_index_poll_schedule" {
  description = "Exposed so a test can assert how often registered repositories are polled (docs/repo-index.md §3.3)."
  value       = var.repo_index_poll_schedule
}

output "execution_cancel_topic" {
  description = "Where swarm-api publishes a cancelled task's attempt for the reconciler to stop (#627)."
  value       = google_pubsub_topic.execution_cancel.name
}
