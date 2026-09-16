variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "repository_id" {
  description = "Must match swarm_common.config.Settings.artifact_registry, which defaults to swarm-images."
  type        = string
  default     = "swarm-images"

  validation {
    condition     = startswith(var.repository_id, "swarm-")
    error_message = "repository_id must start with 'swarm-'."
  }
}

variable "immutable_tags" {
  description = <<-EOT
    When true a tag can never be repointed. That is what makes an attempt
    reproducible: the image a task ran is still the image that tag names.
  EOT
  type        = bool
  default     = false
}

variable "keep_recent_versions" {
  type    = number
  default = 20
}

variable "untagged_ttl" {
  description = "How long an untagged image survives. Go format, e.g. 604800s."
  type        = string
  default     = "604800s"
}

variable "cleanup_dry_run" {
  description = "True evaluates the cleanup policies and logs, without deleting."
  type        = bool
  default     = false
}

variable "readers" {
  description = <<-EOT
    IAM members granted artifactregistry.reader, keyed by a stable label.

    A map rather than a list because the members are service account emails that
    are only known after apply: `for_each = toset(<unknown>)` cannot be planned
    at all, so the key has to be something the configuration already knows.
  EOT
  type        = map(string)
  default     = {}
}

variable "writers" {
  description = "IAM members granted artifactregistry.writer, keyed by a stable label. CI only -- no runtime identity may push."
  type        = map(string)
  default     = {}
}

variable "kms_key_name" {
  type    = string
  default = ""
}

variable "labels" {
  type = map(string)
}
