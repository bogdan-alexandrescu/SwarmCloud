output "state_bucket" {
  description = "Pass to terraform/infra: terraform init -backend-config=\"bucket=<this>\"."
  value       = google_storage_bucket.state.name
}

output "state_bucket_url" {
  value = google_storage_bucket.state.url
}

output "state_log_bucket" {
  value = google_storage_bucket.state_logs.name
}

output "enabled_services" {
  value = var.manage_project_services ? module.project_services[0].enabled_services : []
}

output "github_workload_identity_provider" {
  description = "Value for google-github-actions/auth's workload_identity_provider input."
  value       = var.enable_github_wif ? google_iam_workload_identity_pool_provider.github[0].name : ""
}

output "github_deployer_service_account" {
  value = var.enable_github_wif ? google_service_account.deployer[0].email : ""
}

output "github_principals" {
  description = "ref -> the principalSet permitted to assume the deployer SA. One per allowed ref: the binding pins the ref as well as the repository, so the provider's attribute_condition is not the only thing holding the boundary."
  value       = local.github_principals
}
