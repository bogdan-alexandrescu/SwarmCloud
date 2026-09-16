output "artifact_bucket_name" {
  value = google_storage_bucket.artifacts.name
}

output "artifact_bucket_url" {
  value = google_storage_bucket.artifacts.url
}

output "access_log_bucket_name" {
  value = google_storage_bucket.access_logs.name
}

output "tenant_prefixes" {
  description = "Matches agent_worker.config.WorkerConfig.gcs_prefix, which builds tenants/<tenant>/tasks/<task>/attempts/<attempt>."
  value       = { for t in var.tenants : t => "tenants/${t}/" }
}
