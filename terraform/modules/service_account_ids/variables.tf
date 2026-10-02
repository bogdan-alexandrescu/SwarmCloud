variable "tenant_ids" {
  description = "Tenant keys whose worker account ids to derive. Empty when a caller needs only the platform accounts."
  type        = list(string)
  default     = []
}

variable "tenant_providers" {
  description = <<-EOT
    Tenant key -> the providers it registers. Only `git-merge` and `git-review`
    are read: each brings the tenant's own merge, post-verdict and review
    accounts into `action_ids` and `infra_managed` (#295). Empty for a caller
    that needs only the worker accounts.
  EOT
  type        = map(list(string))
  default     = {}
}
