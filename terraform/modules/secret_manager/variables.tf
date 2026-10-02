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
    # provider -> the COMPLETE list of identities that may read that
    # provider's secret, in place of `accessor`. Required for every provider
    # in var.action_providers, whose secret the worker account must never read.
    accessor_overrides = optional(map(list(string)), {})
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
        for m in concat([v.accessor], v.admin_members, flatten(values(v.accessor_overrides))) :
        m != "allUsers" && m != "allAuthenticatedUsers"
      ])
    ])
    error_message = "allUsers/allAuthenticatedUsers may never appear on a provider-key secret."
  }

  validation {
    condition = alltrue([
      for t, v in var.tenant_secrets : alltrue([
        for p, members in v.accessor_overrides :
        length(members) > 0 && alltrue([for m in members : startswith(m, "serviceAccount:")])
      ])
    ])
    error_message = "an accessor override names at least one reader, and every reader is a service account."
  }

  # Without an override an App key's secret would fall back to the worker
  # account -- the one identity every agent of the tenant can mint a token for.
  validation {
    condition = alltrue([
      for t, v in var.tenant_secrets : alltrue([
        for p in v.providers : !contains(var.action_providers, p) || contains(keys(v.accessor_overrides), p)
      ])
    ])
    error_message = "git-merge and git-review need an accessor override naming the merge or post-verdict account: their secret must never fall back to the tenant's worker account."
  }

  validation {
    condition = alltrue([
      for t, v in var.tenant_secrets : alltrue([
        for p, members in v.accessor_overrides : !contains(var.action_providers, p) || !contains(members, v.accessor)
      ])
    ])
    error_message = "the tenant's worker account may never read a git-merge or git-review secret: any agent of the tenant can mint its token."
  }
}

variable "action_providers" {
  description = <<-EOT
    Providers whose secret is a GitHub App key that only a #295 worker-action
    account may read: `git-merge` (the merge account) and `git-review` (the
    post-verdict account). A tenant listing one must name its readers in
    `accessor_overrides`, and must not name its worker account there; neither
    is ever refreshed (contract request 35, MAJOR 1, and its git-merge twin).
  EOT
  type        = list(string)
  default     = ["git-merge", "git-review"]
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
