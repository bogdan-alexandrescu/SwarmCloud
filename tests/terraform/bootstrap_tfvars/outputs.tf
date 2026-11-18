output "path" {
  description = "The terraform.tfvars actually read. Empty means no candidate path held it, which the caller asserts against."
  value       = local.path
}

output "assignments" {
  description = "How many top-level `deployer_scoped_roles = [...]` assignments were found. Anything but 1 means the list below is not the file's."
  value       = length(local.assignments)
}

output "deployer_scoped_roles" {
  description = "The roles terraform/bootstrap/terraform.tfvars scopes, as committed."
  value       = local.deployer_scoped_roles
}
