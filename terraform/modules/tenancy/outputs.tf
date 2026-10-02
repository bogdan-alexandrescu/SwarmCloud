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

output "action_profiles" {
  description = "runner profile -> {suffix, provider, sole_accessor, also_reads}: the #295 profiles that run as their own per-tenant account (modules/service_account_ids)."
  value       = local.action_profiles
}

output "action_service_accounts" {
  description = "\"<tenant>:<runner profile>\" -> the #295 account that profile's Job runs as."
  value       = { for k, sa in google_service_account.action : k => sa.email }
}

output "action_members" {
  description = "\"<tenant>:<runner profile>\" -> IAM member string of the #295 account."
  value       = { for k, sa in google_service_account.action : k => "serviceAccount:${sa.email}" }
}

output "action_providers" {
  description = "The providers whose secret a #295 account reads alone: never the worker's, and never refreshed."
  value       = sort(distinct([for p, a in local.action_profiles : a.provider if a.sole_accessor]))
}

output "forge" {
  description = "tenant_id -> its forge record (host, owner, repo, review App ids), or null. Rendered into the merge and post-verdict Jobs."
  value       = { for t, v in var.tenants : t => v.forge }
}

output "secret_readers" {
  description = "tenant_id -> provider -> its secret's readers by name (\"worker\" or a \"<tenant>:<profile>\" key), for every provider whose readers are not the worker alone."
  value       = local.secret_readers
}

# accessor_overrides is what keeps the worker account off the App keys: see
# local.secret_readers in main.tf.
output "secret_inputs" {
  description = "Ready-shaped input for the secret_manager module."
  value = {
    for t, v in var.tenants : t => {
      providers = v.providers
      accessor  = "serviceAccount:${google_service_account.worker[t].email}"
      accessor_overrides = {
        for p, readers in local.secret_readers[t] : p => [
          for r in readers :
          r == "worker" ? "serviceAccount:${google_service_account.worker[t].email}" : "serviceAccount:${google_service_account.action[r].email}"
        ]
      }
    }
    if length(v.providers) > 0
  }
}
