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
  value = local.custom_roles.job_dispatcher
}

output "job_reaper_role_id" {
  value = local.custom_roles.job_reaper
}

output "granted_roles" {
  description = "component -> sorted project roles, conditioned ones included, so a test can assert that swarm-api holds no compute permission."
  value = {
    for account, roles in local.plain_roles :
    account => sort(concat(roles, contains(keys(local.gke_role_grants), account) ? [local.gke_role_grants[account]] : []))
  }
}

output "gke_role_condition" {
  description = "The CEL expression pinning the container.* grants to the swarm cluster. Empty when the scoping is off or GKE is disabled."
  value       = length(local.gke_condition) > 0 ? local.gke_condition[0].expression : ""
}

output "tick_service_account" {
  value = google_service_account.tick.email
}

output "tick_member" {
  value = "serviceAccount:${google_service_account.tick.email}"
}
