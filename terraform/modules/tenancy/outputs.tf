output "worker_service_accounts" {
  description = "tenant_id -> service account email."
  value       = { for t, sa in google_service_account.worker : t => sa.email }
}

output "worker_members" {
  description = "tenant_id -> IAM member string, for the secret accessor bindings."
  value       = { for t, sa in google_service_account.worker : t => "serviceAccount:${sa.email}" }
}

output "namespaces" {
  description = "tenant_id -> Kubernetes namespace on the Autopilot cluster."
  value       = local.namespace
}

output "gcs_prefixes" {
  description = "tenant_id -> object prefix. Matches WorkerConfig.gcs_prefix."
  value       = local.gcs_prefix
}

output "tenant_ids" {
  value = sort(local.tenant_ids)
}

output "tenant_providers" {
  description = "tenant_id -> providers it holds credentials for."
  value       = { for t, v in var.tenants : t => v.providers }
}

output "secret_inputs" {
  description = "Ready-shaped input for the secret_manager module."
  value = {
    for t, v in var.tenants : t => {
      providers = v.providers
      accessor  = "serviceAccount:${google_service_account.worker[t].email}"
    }
    if length(v.providers) > 0
  }
}
