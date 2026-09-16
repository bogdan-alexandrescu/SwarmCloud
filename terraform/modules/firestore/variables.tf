variable "project_id" {
  type = string
}

variable "location" {
  type    = string
  default = "us-central1"
}

variable "database_id" {
  description = <<-EOT
    A NAMED database. `(default)` must stay free: saga-agents-staging is shared,
    and a project gets exactly one default database. Taking it would permanently
    deny it to whichever team asks for it next.
  EOT
  type        = string
  default     = "swarm"

  validation {
    condition     = var.database_id != "(default)"
    error_message = "The (default) database must stay available to other teams in this shared project."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,62}$", var.database_id))
    error_message = "database_id must be lowercase alphanumeric with dashes, 4-63 characters."
  }
}

variable "delete_protection" {
  type    = bool
  default = true
}

variable "deletion_policy" {
  description = "ABANDON leaves the database in place when the terraform resource is removed."
  type        = string
  default     = "ABANDON"

  validation {
    condition     = contains(["ABANDON", "DELETE"], var.deletion_policy)
    error_message = "deletion_policy must be ABANDON or DELETE."
  }
}

variable "point_in_time_recovery" {
  type    = bool
  default = true
}

variable "concurrency_mode" {
  description = <<-EOT
    PESSIMISTIC. Admission reserves every pool in one transaction and relies on
    the loser being aborted and retried (see swarm_common.admission); optimistic
    concurrency would surface that as a client-visible contention failure
    instead.
  EOT
  type        = string
  default     = "PESSIMISTIC"

  validation {
    condition     = contains(["OPTIMISTIC", "PESSIMISTIC"], var.concurrency_mode)
    error_message = "concurrency_mode must be OPTIMISTIC or PESSIMISTIC."
  }
}

variable "event_ttl_field" {
  description = "Field on tasks/{id}/events whose timestamp expires the document. Empty disables the TTL policy."
  type        = string
  default     = "expires_at"
}

variable "bootstrap_documents" {
  description = "Create the pool and tenant documents the scheduler expects to already exist."
  type        = bool
  default     = true
}

variable "pools" {
  description = <<-EOT
    Slot pools to create, keyed by the pool name swarm_common.models.pool_names_for
    produces: global, tenant:<id>, resource:<class>, runner:<profile>,
    backend:<backend>, provider:<p>, provider:<p>:tenant:<id>.
  EOT
  type = map(object({
    hard_limit = number
    enabled    = optional(bool, true)
  }))
  default = {}

  validation {
    condition     = alltrue([for k, v in var.pools : v.hard_limit >= 0])
    error_message = "a pool hard_limit cannot be negative."
  }

  validation {
    condition     = !var.bootstrap_documents_required || contains(keys(var.pools), "global")
    error_message = "the `global` pool must exist: a missing pool document is treated as unlimited by evaluate_capacity()."
  }
}

variable "bootstrap_documents_required" {
  description = "Internal switch so the `global` pool check only fires when pools are being created at all."
  type        = bool
  default     = true
}

variable "tenant_documents" {
  description = "Tenant records mirroring swarm_common.models.Tenant."
  type = map(object({
    kind            = string
    principal       = string
    display_name    = optional(string, "")
    max_active      = optional(number, 20)
    capacity_units  = optional(number, 40)
    credentials     = optional(list(string), [])
    service_account = optional(string, "")
    gcs_prefix      = optional(string, "")
    namespace       = optional(string, "")
  }))
  default = {}
}
