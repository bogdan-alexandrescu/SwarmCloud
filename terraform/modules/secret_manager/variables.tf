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
