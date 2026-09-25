output "terraform_dir" {
  description = "The terraform/ directory actually read. Empty means no candidate path held terraform/infra, which the caller asserts against."
  value       = local.terraform_dir
}

output "files_read" {
  description = "How many .tf files were scanned, so an empty scan is visible rather than silent."
  value       = length(local.files)
}

output "declared" {
  description = "Every project-policy-writing resource declared in terraform/infra and terraform/modules."
  value       = local.declared
}

output "uncovered" {
  description = "Declared, and read by none of the caller's runs: a grant no parity check holds to the grantable list."
  value       = [for d in local.declared : d if !contains(var.covered, d)]
}

output "stale" {
  description = "Named by the caller and no longer declared: renamed, moved or removed, or the scan stopped matching."
  value       = [for c in var.covered : c if !contains(local.declared, c)]
}
