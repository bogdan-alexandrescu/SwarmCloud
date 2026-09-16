variable "project_id" {
  description = "Target project. Shared with other teams, so nothing here may disable an API another team relies on."
  type        = string
}

variable "services" {
  description = "Service APIs to hold enabled. Already-enabled services are adopted, not re-created."
  type        = list(string)
}

variable "disable_on_destroy" {
  description = <<-EOT
    Must stay false in a shared project. `terraform destroy` disabling
    container.googleapis.com would take down the other team's live
    `agents-staging` cluster, which is the exact blast radius this platform is
    required never to have.
  EOT
  type        = bool
  default     = false

  validation {
    condition     = var.disable_on_destroy == false
    error_message = "disable_on_destroy must be false: saga-agents-staging is shared and disabling an API would break other teams' workloads."
  }
}

variable "disable_dependent_services" {
  description = "Never cascade API disablement in a shared project."
  type        = bool
  default     = false

  validation {
    condition     = var.disable_dependent_services == false
    error_message = "disable_dependent_services must be false in a shared project."
  }
}
