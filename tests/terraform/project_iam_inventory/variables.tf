variable "covered" {
  description = <<-EOT
    The project-level grants the caller's runs read, each written as
    "<directory under terraform/> <resource type>.<resource name>", e.g.
    "infra google_project_iam_member.verify_reads_run".
  EOT
  type        = list(string)
}
