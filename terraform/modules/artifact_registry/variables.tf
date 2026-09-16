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
  description = "IAM members granted artifactregistry.reader. Every runtime SA that pulls an image belongs here."
  type        = list(string)
  default     = []
}

variable "writers" {
  description = "IAM members granted artifactregistry.writer. CI only -- no runtime identity may push."
  type        = list(string)
  default     = []
}

variable "kms_key_name" {
  type    = string
  default = ""
}

variable "labels" {
  type = map(string)
}
