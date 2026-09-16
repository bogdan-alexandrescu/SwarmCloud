output "cluster_name" {
  value = google_container_cluster.this.name
}

output "cluster_id" {
  value = google_container_cluster.this.id
}

output "endpoint" {
  value     = google_container_cluster.this.endpoint
  sensitive = true
}

output "workload_identity_pool" {
  description = "Used by the tenancy module to bind each tenant KSA to its GSA."
  value       = "${var.project_id}.svc.id.goog"
}

output "location" {
  value = google_container_cluster.this.location
}
