output "platform" {
  description = "account id -> {display_name, description} for the component accounts terraform/bootstrap creates and modules/iam grants roles to."
  value       = local.platform
}

output "tick_id" {
  description = "The account Cloud Scheduler and Pub/Sub push present (modules/iam)."
  value       = local.tick_id
}

output "verify_id" {
  description = "The account the verification job runs as (terraform/infra/verify.tf)."
  value       = local.verify_id
}

output "rollup_sweeper_id" {
  description = "The account the per-tenant workflow-rollup jobs present to swarm-api (modules/scheduler)."
  value       = local.rollup_sweeper_id
}

output "schedule_tick_id" {
  description = "The account the schedule tick presents to swarm-api (modules/scheduler, docs/schedules.md SD10)."
  value       = local.schedule_tick_id
}

output "discovery_id" {
  description = "The account the swarm-discovery service runs as (terraform/infra/discovery.tf); created by terraform/bootstrap."
  value       = local.discovery_id
}

output "doctor_id" {
  description = "The account the scheduled swarm-doctor job runs as (terraform/infra/doctor.tf); created by terraform/bootstrap."
  value       = local.doctor_id
}

output "bootstrap_platform" {
  description = "Every platform account id terraform/bootstrap creates (platform_service_accounts.tf), sorted."
  value       = local.bootstrap_platform
}

output "tenant_worker_prefix" {
  description = "A tenant's worker account id is this plus the tenant key (modules/tenancy)."
  value       = local.tenant_worker_prefix
}

output "worker_ids" {
  description = "tenant key -> worker account id, for var.tenant_ids."
  value       = local.worker_ids
}

output "infra_managed" {
  description = "Every account id terraform/infra deploys as or grants on for var.tenant_ids (bootstrap's platform accounts and the workers), sorted."
  value       = local.infra_managed

  # IAM's rule for an account id: 6 to 30 characters, lowercase letters,
  # digits and hyphens, starting with a letter. A tenant key long enough to
  # break it fails here, at plan, not at the release's create.
  precondition {
    condition     = alltrue([for id in local.infra_managed : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", id))])
    error_message = "a service account id must be 6-30 lowercase letters, digits and hyphens, starting with a letter; check the tenant keys against the worker prefix."
  }

  # Two names for one account would make one tenant's worker another
  # tenant's, or a platform account.
  precondition {
    condition     = length(distinct(local.infra_managed)) == length(local.infra_managed)
    error_message = "two accounts terraform/infra manages derive the same id; a tenant key collides with another account's name."
  }
}
