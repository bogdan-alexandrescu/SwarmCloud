output "secret_ids" {
  value = sort(keys(google_secret_manager_secret.this))
}

output "secrets_by_tenant" {
  description = "tenant_id -> { provider -> secret id }, for wiring a job's secret_env."
  value = {
    for tenant_id, cfg in var.tenant_secrets : tenant_id => {
      for provider in cfg.providers : provider => "swarm-tenant-${tenant_id}-${provider}"
    }
  }
}

output "secret_names" {
  description = "Fully qualified secret names."
  value       = { for k, s in google_secret_manager_secret.this : k => s.name }
}
