output "service_urls" {
  description = "Map of service name to its https URI. Reachable only from inside the VPC or via an internal load balancer."
  value       = { for k, s in google_cloud_run_v2_service.this : k => s.uri }
}

output "service_ids" {
  value = { for k, s in google_cloud_run_v2_service.this : k => s.id }
}

output "service_names" {
  value = sort(keys(google_cloud_run_v2_service.this))
}
