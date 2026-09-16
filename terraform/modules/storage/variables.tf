variable "project_id" {
  type = string
}

variable "location" {
  type    = string
  default = "US-CENTRAL1"
}

variable "name_prefix" {
  type    = string
  default = "swarm"

  validation {
    condition     = startswith(var.name_prefix, "swarm")
    error_message = "name_prefix must start with 'swarm'."
  }
}

variable "bucket_suffix" {
  description = "Appended to make the globally unique bucket name. The project id is the obvious choice."
  type        = string
}

variable "artifact_retention_days" {
  type    = number
  default = 90

  validation {
    condition     = var.artifact_retention_days >= 7
    error_message = "artifacts must survive at least a week so a failed run can still be investigated."
  }
}

variable "log_retention_days" {
  type    = number
  default = 400
}

variable "noncurrent_version_retention_days" {
  type    = number
  default = 30
}

variable "keep_noncurrent_versions" {
  type    = number
  default = 3
}

variable "nearline_after_days" {
  description = "Checkpoints are written constantly and read almost never; cold storage after two weeks."
  type        = number
  default     = 14
}

variable "kms_key_name" {
  description = "Optional CMEK. Empty uses Google-managed keys."
  type        = string
  default     = ""
}

variable "force_destroy" {
  description = "Must stay false outside a throwaway environment: true lets `terraform destroy` delete every checkpoint in the bucket."
  type        = bool
  default     = false
}

variable "soft_delete_retention_seconds" {
  description = "Soft delete window. 0 disables it."
  type        = number
  default     = 604800
}

variable "tenants" {
  description = "Tenant ids, used only to publish the per-tenant object prefixes."
  type        = list(string)
  default     = []
}

variable "labels" {
  type = map(string)
}
