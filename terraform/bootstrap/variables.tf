variable "project_id" {
  description = "SHARED project. Nothing in this root may touch another team's resources."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "location" {
  description = "State bucket location. Regional, matching the workloads."
  type        = string
  default     = "US-CENTRAL1"
}

variable "name_prefix" {
  type    = string
  default = "swarm"

  validation {
    condition     = startswith(var.name_prefix, "swarm")
    error_message = "name_prefix must start with 'swarm'."
  }
}

variable "state_bucket_name" {
  description = "Defaults to <prefix>-tfstate-<project_id>, which is globally unique because project ids are."
  type        = string
  default     = ""
}

variable "state_noncurrent_versions_to_keep" {
  description = "How many previous state files survive. State corruption is usually noticed several applies later."
  type        = number
  default     = 20

  validation {
    condition     = var.state_noncurrent_versions_to_keep >= 5
    error_message = "keep at least 5 previous state versions; fewer than that and a corruption found a day later is unrecoverable."
  }
}

variable "state_noncurrent_retention_days" {
  type    = number
  default = 365
}

variable "manage_project_services" {
  type    = bool
  default = true
}

variable "prerequisite_services" {
  description = <<-EOT
    The APIs terraform itself needs before terraform/infra can plan anything.
    The full 18 are enabled by terraform/infra; these are the ones without which
    that root cannot even authenticate or read the project.
  EOT
  type        = list(string)
  default = [
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ]
}

# ---------------------------------------------------------------------------
# GitHub Actions Workload Identity Federation (optional)
# ---------------------------------------------------------------------------

variable "enable_github_wif" {
  description = "Create a Workload Identity pool so GitHub Actions can deploy WITHOUT a downloadable service account key."
  type        = bool
  default     = false
}

variable "github_repository" {
  description = "owner/repo allowed to assume the deployer SA. Required when enable_github_wif is true."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be in owner/repo form."
  }
}

variable "github_allowed_refs" {
  description = <<-EOT
    Git refs permitted to deploy. Default is the default branch only.

    This is the difference between "our CI can deploy" and "anyone who can open
    a pull request against this repo can deploy": a workflow triggered from a
    fork runs with the repository's own OIDC subject unless the ref is pinned.
  EOT
  type        = list(string)
  default     = ["refs/heads/main"]
}

variable "deployer_roles" {
  description = <<-EOT
    Roles granted to the GitHub deployer service account. Wide by necessity --
    it manages the whole platform -- but explicitly enumerated rather than
    roles/owner, so what CI can do is reviewable in a diff.
  EOT
  type        = list(string)
  default = [
    "roles/artifactregistry.admin",
    "roles/cloudscheduler.admin",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/container.admin",
    "roles/datastore.owner",
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/logging.configWriter",
    "roles/monitoring.editor",
    "roles/pubsub.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ]

  validation {
    condition     = !contains(var.deployer_roles, "roles/owner")
    error_message = "roles/owner is never granted to CI; enumerate the roles instead so the grant is reviewable."
  }
}

variable "extra_labels" {
  type    = map(string)
  default = {}

  validation {
    condition     = !contains(keys(var.extra_labels), "managed-by")
    error_message = "managed-by is fixed by this configuration."
  }
}
