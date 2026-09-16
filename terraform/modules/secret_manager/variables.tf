variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "tenant_secrets" {
  description = <<-EOT
    Per-tenant provider credentials.

    Keyed by tenant id. `secret_id` for each provider is
    `swarm-tenant-<tenant_id>-<provider>`, which is exactly what
    swarm_common.models.Tenant.secret_name() returns -- the app looks the name
    up rather than being told it, so the two must not drift.
  EOT
  type = map(object({
    providers     = list(string)
    accessor      = string # serviceAccount:swarm-agent-worker-<tenant>@...
    admin_members = optional(list(string), [])
  }))

  validation {
    condition = alltrue([
      for t, v in var.tenant_secrets : startswith(v.accessor, "serviceAccount:")
    ])
    error_message = "a tenant's secret accessor must be a service account; users do not read provider keys directly."
  }

  validation {
    condition = alltrue([
      for t, v in var.tenant_secrets : alltrue([
        for m in concat([v.accessor], v.admin_members) :
        m != "allUsers" && m != "allAuthenticatedUsers"
      ])
    ])
    error_message = "allUsers/allAuthenticatedUsers may never appear on a provider-key secret."
  }
}

variable "version_destroy_ttl" {
  description = "Delayed destruction window. A rotation that turns out wrong can be undone inside it."
  type        = string
  default     = "86400s"
}

variable "kms_key_name" {
  description = "Optional CMEK for secret payloads."
  type        = string
  default     = ""
}

variable "deletion_protection" {
  type    = bool
  default = false
}

variable "labels" {
  type = map(string)
}

variable "enable_subscription_refresh" {
  description = <<-EOT
    Whether to create the `-refresh` siblings at all.

    Separate from `refresher_member` for a plan-time reason, not a stylistic
    one: the member is a service account email produced by another module, so
    its VALUE is unknown until apply, and `var.refresher_member != ""` is
    therefore also unknown. A for_each key set derived from it cannot be
    planned. The bool is known statically, so the set of secrets is known
    statically, and only the members inside each binding are resolved late.
  EOT
  type        = bool
  default     = false
}

variable "refresher_member" {
  description = <<-EOT
    The quota broker, which is the platform's SINGLE writer of subscription
    credentials. Empty disables the whole mechanism.

    Setting it creates a `<secret_id>-refresh` sibling for every tenant/provider
    pair, holding the long-lived half of a Claude subscription credential. The
    broker exchanges it for a short-lived access token and publishes that to the
    base secret, which is the only half a worker ever mounts.

    The sibling is deliberately NOT readable by the tenant's worker: a refresh
    token is a standing grant on the tenant's Claude account, while an access
    token expires. Giving a job the refresh token would let a prompt-injected
    agent walk out with durable account access.

    One writer is not a simplification. Refresh tokens ROTATE, so two refreshers
    racing would each invalidate the other's token and lock the tenant out.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.refresher_member == "" || startswith(var.refresher_member, "serviceAccount:")
    error_message = "the credential refresher must be a service account."
  }

  validation {
    condition     = !var.enable_subscription_refresh || var.refresher_member != ""
    error_message = "enable_subscription_refresh needs a refresher_member; otherwise the refresh secrets would be created with nobody able to read them."
  }
}
