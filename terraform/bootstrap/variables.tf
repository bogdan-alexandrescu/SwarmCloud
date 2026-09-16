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

    Fully qualified refs only, and no wildcards. GitHub's `assertion.ref` is a
    full ref name, so the provider condition compares it for equality -- a
    pattern like `refs/heads/*` would simply never match anything and the pool
    would be unusable rather than wide. The validations below refuse both that
    and an empty list, which used to render the condition as
    `assertion.repository == "..." && ()`: malformed CEL that fails at apply.
  EOT
  type        = list(string)
  default     = ["refs/heads/main"]

  validation {
    condition     = length(var.github_allowed_refs) > 0
    error_message = "at least one ref must be allowed: an empty list renders a malformed attribute condition, and the ref pin is the whole boundary in front of the deployer's roles."
  }

  validation {
    condition = alltrue([
      for r in var.github_allowed_refs : can(regex("^refs/(heads|tags)/[A-Za-z0-9._/-]+$", r))
    ])
    error_message = "each ref must be a fully qualified refs/heads/<name> or refs/tags/<name>."
  }

  validation {
    condition = alltrue([
      for r in var.github_allowed_refs : !strcontains(r, "*")
    ])
    error_message = "wildcards are not supported: assertion.ref is compared for equality, so a pattern matches nothing at all."
  }

  validation {
    condition = alltrue([
      for r in var.github_allowed_refs : !startswith(r, "refs/pull/")
    ])
    error_message = "refs/pull/* is a pull request ref, including one from a fork; allowing it hands a deploy token to anyone who can open a PR."
  }
}

variable "deployer_roles" {
  description = <<-EOT
    Predefined roles granted to the GitHub deployer service account. Wide by
    necessity -- it manages the whole platform -- but explicitly enumerated
    rather than roles/owner, so what CI can do is reviewable in a diff.

    Secret Manager is deliberately absent. roles/secretmanager.admin includes
    secretmanager.versions.access, which would let anything running on an
    allowed ref print every tenant's Anthropic and OpenAI key in cleartext --
    while the humans who rotate those keys get only secretVersionAdder and
    cannot read one back (modules/secret_manager/main.tf). Terraform needs to
    CREATE secrets and set their IAM policy, not to read their payloads, so it
    gets the custom swarmSecretProvisioner role in wif.tf instead. The
    validation below stops the predefined role being put back by hand.
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
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ]

  validation {
    condition     = !contains(var.deployer_roles, "roles/owner") && !contains(var.deployer_roles, "roles/editor")
    error_message = "roles/owner and roles/editor are never granted to CI; enumerate the roles instead so the grant is reviewable."
  }

  # Every role here confers secretmanager.versions.access, directly or by
  # inheritance. A CI identity that can read a provider key back can exfiltrate
  # every tenant's key in one workflow run, which is a strictly larger authority
  # than anything else on this list and is not needed to manage the platform.
  validation {
    condition = length([
      for r in var.deployer_roles : r
      if contains([
        "roles/secretmanager.admin",
        "roles/secretmanager.secretAccessor",
        "roles/secretmanager.secretVersionManager",
      ], r)
    ]) == 0
    error_message = "no role granting secretmanager.versions.access may be given to CI: terraform creates secrets and sets their IAM policy, it never reads a payload. The custom swarmSecretProvisioner role covers what it does need."
  }
}

variable "deployer_secret_permissions" {
  description = <<-EOT
    The custom secret-provisioning role CI actually gets. Everything needed to
    create a tenant's secret, set who may read it, and expire an old version --
    and nothing that can read a payload back.

    `secretmanager.versions.access` is the permission being withheld, and a
    validation refuses it explicitly so it cannot be reintroduced by appending
    to this list.
  EOT
  type        = list(string)
  default = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.get",
    "secretmanager.secrets.list",
    "secretmanager.secrets.update",
    "secretmanager.secrets.delete",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
    # Version metadata and lifecycle. `add` is what a rotation needs; none of
    # these returns the payload.
    "secretmanager.versions.add",
    "secretmanager.versions.get",
    "secretmanager.versions.list",
    "secretmanager.versions.enable",
    "secretmanager.versions.disable",
    "secretmanager.versions.destroy",
    "secretmanager.locations.get",
    "secretmanager.locations.list",
  ]

  validation {
    condition     = !contains(var.deployer_secret_permissions, "secretmanager.versions.access")
    error_message = "secretmanager.versions.access is the one permission this role exists to withhold: it is what turns a deploy identity into a reader of every tenant's provider key."
  }

  validation {
    condition = alltrue([
      for p in var.deployer_secret_permissions : startswith(p, "secretmanager.")
    ])
    error_message = "this role covers Secret Manager only; anything else belongs in deployer_roles where it is visible as a named role."
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
