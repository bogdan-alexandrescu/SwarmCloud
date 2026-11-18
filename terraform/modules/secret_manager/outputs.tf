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

output "github_app_secret_ids" {
  description = "The GitHub App's platform secret ids (docs/runbooks/github-app.md). Empty while github_app_secrets_enabled is false. Names only; never a value."
  value       = sort([for k, s in google_secret_manager_secret.github_app : s.secret_id])
}
