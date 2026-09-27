variable "parity_test_file" {
  description = <<-EOT
    The .tftest.hcl file, in tests/terraform, whose runs hold each project-level
    grant to deployer_grantable_project_roles, e.g. "deployer_iam.tftest.hcl".
    Its assertions are read as text; what they pass to contains() is what
    counts as read.
  EOT
  type        = string
}
