variable "tenant_ids" {
  description = "Tenant keys whose worker account ids to derive. Empty when a caller needs only the platform accounts."
  type        = list(string)
  default     = []
}
