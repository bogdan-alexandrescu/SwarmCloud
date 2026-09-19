output "ip_address" {
  description = "The address the A record must point at. DNS is not managed here."
  value       = google_compute_global_address.this.address
}

output "url" {
  value = "https://${var.hostname}"
}

output "backend_service_name" {
  description = "Needed to grant or check IAP access outside terraform."
  value       = google_compute_backend_service.this.name
}

output "dns_record_required" {
  description = <<-EOT
    The one manual step. Until this record exists and resolves, the managed
    certificate stays in PROVISIONING and the host serves a TLS error -- which
    is indistinguishable from a broken deployment unless you know to expect it.
  EOT
  value       = "A  ${var.hostname}  ->  ${google_compute_global_address.this.address}"
}

output "certificate_name" {
  description = "Check readiness with: gcloud compute ssl-certificates describe <name> --global --format='value(managed.status)'"
  value       = google_compute_managed_ssl_certificate.this.name
}

output "iap_audiences" {
  description = <<-EOT
    The audiences IAP mints assertions for, one per backend service, in the
    shape swarm-api must pin:

        /projects/<PROJECT NUMBER>/global/backendServices/<BACKEND SERVICE ID>

    Unpinned, google-auth skips the `aud` check and an assertion minted by IAP
    for ANY backend in ANY project would authenticate -- and an IAP assertion is
    issued to anyone who can reach any IAP-protected resource.
  EOT
  value = compact([
    "/projects/${var.project_number}/global/backendServices/${google_compute_backend_service.this.generated_id}",
    local.ui_enabled ? "/projects/${var.project_number}/global/backendServices/${google_compute_backend_service.ui[0].generated_id}" : "",
  ])
}

