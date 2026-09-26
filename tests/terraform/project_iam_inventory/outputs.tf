output "terraform_dir" {
  description = "The terraform/ directory actually read. Empty means no candidate path held terraform/infra, which the caller asserts against."
  value       = local.terraform_dir
}

output "files_read" {
  description = "How many .tf files were scanned, so an empty scan is visible rather than silent."
  value       = length(local.files)
}

output "test_file_read" {
  description = "Whether the caller's .tftest.hcl was found and read. False means parity_reads is empty because nothing was read, not because nothing is read."
  value       = local.test_source != ""
}

output "declared" {
  description = "Every project-policy-writing resource declared in terraform/infra and terraform/modules."
  value       = local.declared
}

output "parity_reads" {
  description = "Every such resource an assert in the caller's test file passes, by its role, to contains(<...>deployer_grantable_project_roles, ...), in a run that plans the directory declaring it."
  value       = local.parity_reads
}

output "uncovered" {
  description = "Declared, and read by no parity assertion: a grant nothing holds to the grantable list."
  value       = [for d in local.declared : d if !contains(local.parity_reads, d)]
}

output "stale" {
  description = "Read by a parity assertion and not found by the declaration scan: the scan stopped matching, or the assertion reads a grant from a directory this module does not scan."
  value       = [for p in local.parity_reads : p if !contains(local.declared, p)]
}

output "module_sources" {
  description = "Every module block's source in the scanned files, as \"<dir under terraform/> <source>\"."
  value       = local.module_sources
}

output "unscanned_module_sources" {
  description = "Module sources whose files this scan does not read: a registry or git module, or a path out of terraform/infra and terraform/modules. A grant declared there would be checked by nothing."
  value       = local.unscanned_module_sources
}
