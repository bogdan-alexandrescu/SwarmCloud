output "enabled_services" {
  description = "The set of services this module holds enabled."
  value       = sort([for s in google_project_service.this : s.service])
}

output "ids" {
  description = "Resource ids, for depends_on wiring by consumers."
  value       = [for s in google_project_service.this : s.id]
}
