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

output "ca_certificate" {
  description = <<-EOT
    The cluster CA, base64-encoded, for a client running OUTSIDE the cluster.

    The reconciler runs on Cloud Run, not in the cluster, so
    kubernetes.config.load_incluster_config() has nothing to read and fails with
    "Service host/port is not set." It builds a client from GKE_ENDPOINT plus
    this certificate instead. Without both, the GKE backend is unreadable on
    every pass and the reconciler refuses to act on anything it might hold.
  EOT
  value       = google_container_cluster.this.master_auth[0].cluster_ca_certificate
  sensitive   = true
}
