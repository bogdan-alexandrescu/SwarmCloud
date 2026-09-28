variable "project_id" {
  type = string
}

variable "firestore_database" {
  type    = string
  default = "swarm"
}

# DEFAULTS OFF. Firestore does not evaluate IAM Conditions on the data plane, so
# this condition does not scope document access -- it DENIES it. Verified live:
# every identity carrying it reported "firestore unavailable: PermissionDenied".
# See terraform/modules/iam/variables.tf and docs/security.md.
variable "scope_firestore_to_database" {
  type    = bool
  default = false
}

variable "artifact_bucket" {
  type = string
}

variable "workload_identity_pool" {
  description = "<project>.svc.id.goog. Empty when the Autopilot cluster is not deployed."
  type        = string
  default     = ""
}

variable "tenants" {
  description = <<-EOT
    Tenants known at provisioning time.

    A tenant is a Google group (`eng@saga.xyz` -> `eng`) or a personal fallback
    (`u-<user>`), exactly as swarm_common.identity derives them. `providers` is
    the set this tenant will register keys for; a runner whose provider is
    absent parks as CREDENTIAL_MISSING rather than failing mid-run.
  EOT
  type = map(object({
    kind         = string # "group" | "user"
    principal    = string # eng@saga.xyz
    display_name = optional(string, "")
    providers    = optional(list(string), [])
    # Nullable, matching terraform/infra: a default here would shadow the
    # environment's pool_limits.default_tenant rather than fall through to it.
    max_active     = optional(number)
    capacity_units = optional(number, 40)
    # Kubernetes service account that assumes this tenant's GSA on Autopilot.
    ksa_name = optional(string, "swarm-agent-worker")
  }))

  validation {
    condition     = alltrue([for t, v in var.tenants : contains(["group", "user"], v.kind)])
    error_message = "tenant kind must be 'group' or 'user'."
  }

  validation {
    condition     = alltrue([for t, v in var.tenants : can(regex("^[a-z0-9-]+$", t))])
    error_message = "tenant ids are lowercase alphanumeric with dashes, matching swarm_common.identity's slugging."
  }

  validation {
    condition     = alltrue([for t, v in var.tenants : length(t) <= 11])
    error_message = "tenant id must be at most 11 characters: the service account id swarm-agent-worker-<tenant> has a 30-character GCP limit."
  }

  validation {
    condition     = alltrue([for t, v in var.tenants : v.kind != "user" || startswith(t, "u-")])
    error_message = "a personal tenant id must be prefixed u- so it cannot collide with a group tenant."
  }
}

variable "dispatcher_members" {
  description = <<-EOT
    Identities allowed to actAs a tenant worker SA, keyed by component name.
    Cloud Run pins the service account on the Job resource, so whoever creates
    that Job needs iam.serviceAccountUser on the SA it names -- granted on the
    SA itself, never project-wide.

    Keyed by component because the members are service account emails that only
    exist after apply; terraform cannot plan a for_each over unknown keys.
  EOT
  type        = map(string)
  default     = {}
}

variable "namespace_prefix" {
  description = "Kubernetes namespace per tenant on the Autopilot cluster."
  type        = string
  default     = "swarm-tenant-"
}

variable "custom_role_suffix" {
  type    = string
  default = ""
}

variable "labels" {
  type = map(string)
}
