locals {
  labels = merge(
    {
      "managed-by" = "swarm-terraform"
      "swarm-root" = "bootstrap"
    },
    var.extra_labels,
  )

  state_bucket_name = var.state_bucket_name != "" ? var.state_bucket_name : "${var.name_prefix}-tfstate-${var.project_id}"

  log_bucket_name = "${var.name_prefix}-tfstate-logs-${var.project_id}"
}

module "project_services" {
  source = "../modules/project_services"

  count = var.manage_project_services ? 1 : 0

  project_id = var.project_id
  services   = var.prerequisite_services
}
