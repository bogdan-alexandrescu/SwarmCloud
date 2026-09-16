output "service_account_emails" {
  description = "component -> email."
  value       = local.sa_email
}

output "service_account_members" {
  description = "component -> IAM member string."
  value       = local.sa_member
}

output "api_service_account" {
  value = local.sa_email["swarm-api"]
}

output "scheduler_service_account" {
  value = local.sa_email["swarm-scheduler"]
}

output "quota_broker_service_account" {
  value = local.sa_email["swarm-quota-broker"]
}

output "reconciler_service_account" {
  value = local.sa_email["swarm-reconciler"]
}

output "job_dispatcher_role_id" {
  value = google_project_iam_custom_role.job_dispatcher.id
}

output "job_reaper_role_id" {
  value = google_project_iam_custom_role.job_reaper.id
}

output "granted_roles" {
  description = "component -> sorted roles, so a test can assert that swarm-api holds no compute permission."
  value       = { for account, roles in local.plain_roles : account => sort(roles) }
}

output "tick_service_account" {
  value = google_service_account.tick.email
}

output "tick_member" {
  value = "serviceAccount:${google_service_account.tick.email}"
}
