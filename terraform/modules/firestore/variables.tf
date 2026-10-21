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

  # Gated on `bootstrap_documents` rather than on a second switch of its own, so
  # the module's DEFAULTS are usable: `pools = {}` with bootstrapping off is a
  # legitimate configuration, while declaring pools and leaving `global` out is
  # not -- evaluate_capacity() treats a missing pool document as UNLIMITED, so
  # the first scheduler pass would admit with no ceiling at all.
  validation {
    condition     = !var.bootstrap_documents || length(var.pools) == 0 || contains(keys(var.pools), "global")
    error_message = "the `global` pool must be among the bootstrapped pools: a missing pool document is treated as unlimited by evaluate_capacity()."
  }
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

variable "bench_database_id" {
  description = <<-EOT
    Name of the disposable admission-contention bench database
    (scripts/bench-contention.sh). Empty, the default, creates none. Must match
    the harness's own pattern, swarm-bench or swarm-bench-<suffix>, so nothing
    created here is a name the harness would refuse, or a live one.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.bench_database_id == "" || can(regex("^swarm-bench(-[a-z0-9]+)*$", var.bench_database_id))
    error_message = "bench_database_id must be empty, swarm-bench, or swarm-bench-<suffix>."
  }
}

variable "daily_backup" {
  description = <<-EOT
    Give the platform database Firestore's managed daily backup (backups.tf).
    On by default (#1818): a backup outlives a deleted database, its
    point-in-time versions do not. The bench database never gets one.
  EOT
  type        = bool
  default     = true
}

variable "backup_retention" {
  description = <<-EOT
    How long each daily backup is kept, in seconds with an `s` suffix.
    604800s (7 days) is the owner's choice on #1818: the same window as
    point-in-time recovery, so an offboarded tenant's documents survive in a
    backup no longer than they already survive as point-in-time versions.
    Firestore's maximum is 8467200s (14 weeks).
  EOT
  type        = string
  default     = "604800s"

  validation {
    condition     = can(regex("^[1-9][0-9]*s$", var.backup_retention)) && try(tonumber(trimsuffix(var.backup_retention, "s")) <= 8467200, false)
    error_message = "backup_retention must be a whole number of seconds with an `s` suffix, at most 8467200s (14 weeks, Firestore's maximum)."
  }
}
