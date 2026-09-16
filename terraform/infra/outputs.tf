output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "environment" {
  value = var.environment
}

output "labels" {
  description = "The label set every resource carries. destroy.sh matches on managed-by."
  value       = local.labels
}

# --- network ---------------------------------------------------------------

output "network_name" {
  value = module.network.network_name
}

output "subnetwork_name" {
  value = module.network.subnetwork_name
}

output "nat_egress_addresses" {
  description = "Give these to a model provider that allow-lists source addresses."
  value       = module.network.nat_addresses
}

# --- data ------------------------------------------------------------------

output "firestore_database" {
  value = module.firestore.database_name
}

output "firestore_indexes" {
  value = module.firestore.index_names
}

output "artifact_bucket" {
  value = module.storage.artifact_bucket_name
}

output "image_base" {
  value = module.artifact_registry.image_base
}

# --- identity --------------------------------------------------------------

output "service_accounts" {
  value = module.iam.service_account_emails
}

output "tenant_service_accounts" {
  value = module.tenancy.worker_service_accounts
}

output "tenant_namespaces" {
  value = module.tenancy.namespaces
}

output "tenant_secret_ids" {
  value = module.secret_manager.secrets_by_tenant
}

# --- compute ---------------------------------------------------------------

output "service_urls" {
  value = module.cloud_run.service_urls
}

output "job_names" {
  value = module.cloud_run_jobs.job_names
}

output "gke_cluster_name" {
  value = var.enable_gke_autopilot ? module.gke_autopilot[0].cluster_name : ""
}

# --- scheduling ------------------------------------------------------------

output "wake_topic" {
  value = module.scheduler.wake_topic
}

output "scheduler_jobs" {
  value = module.scheduler.scheduler_job_names
}

# --- capacity --------------------------------------------------------------

output "pool_names" {
  description = "Every slot pool materialised in Firestore."
  value       = sort(keys(local.pools))
}

output "pool_limits" {
  value = { for name, p in local.pools : name => p.hard_limit }
}

# The three outputs below are the restatement of swarm_common.profiles that
# locals.tf has to carry because terraform cannot import Python. They are
# exported so a test can compare them against the real file:
# tests/terraform/catalogue_mirror parses apps/common/swarm_common/profiles.py
# off disk and tests/terraform/catalogue.tftest.hcl asserts these against it.
# Without that comparison the mirror is a copy nothing checks, and the two drift
# together the first time a profile is resized.
output "resource_classes" {
  description = "Mirror of swarm_common.profiles.RESOURCE_CLASSES, compared field by field against the parsed Python source in tests/terraform/catalogue.tftest.hcl."
  value       = local.resource_classes
}

output "runner_backends" {
  description = "runner profile -> backend, mirroring resolve_backend()."
  value       = { for name, p in local.runner_profiles : name => p.backend }
}

output "runner_profiles" {
  description = "Mirror of swarm_common.profiles.RUNNER_PROFILES: image, class, backend, provider, timeout and the secret env-var names. Compared against the parsed Python source in tests."
  value = {
    for name, p in local.runner_profiles : name => {
      image            = p.image
      resource_class   = p.resource_class
      backend          = p.backend
      provider         = p.provider
      timeout_seconds  = p.timeout_seconds
      secret_env_names = sort(keys(p.secret_env))
    }
  }
}

output "workspace_size_gib" {
  value = module.cloud_run_jobs.workspace_size_gib
}

output "dashboard_id" {
  value = module.monitoring.dashboard_id
}
