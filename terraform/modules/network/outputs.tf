output "network_id" {
  value = google_compute_network.this.id
}

output "network_name" {
  value = google_compute_network.this.name
}

output "network_self_link" {
  value = google_compute_network.this.self_link
}

output "subnetwork_id" {
  value = google_compute_subnetwork.this.id
}

output "subnetwork_name" {
  value = google_compute_subnetwork.this.name
}

output "subnetwork_self_link" {
  value = google_compute_subnetwork.this.self_link
}

output "pods_range_name" {
  value = var.pods_range_name
}

output "services_range_name" {
  value = var.services_range_name
}

output "nat_addresses" {
  description = "Stable egress addresses to hand to a model provider's allow-list."
  value       = google_compute_address.nat[*].address
}

output "internal_ranges" {
  value = local.internal_ranges
}
