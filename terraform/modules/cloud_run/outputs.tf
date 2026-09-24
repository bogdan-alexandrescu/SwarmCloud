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

# Read off the resource rather than echoed from var.services, so a test of the
# root asserts what the service will actually run: terraform/infra deploys by
# digest, and tests/terraform/image_refs.tftest.hcl holds it to that.
output "images" {
  value = { for k, s in google_cloud_run_v2_service.this : k => s.template[0].containers[0].image }
}
