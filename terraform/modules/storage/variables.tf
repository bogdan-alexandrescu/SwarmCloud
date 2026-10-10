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
  description = <<-EOT
    Days after its customTime that a LIVE artifact or log object is deleted,
    for every tenant, mapped or created at runtime. Nothing under
    tenants/<t>/repos/ carries a customTime, so this clock never deletes it.
    The worker stamps customTime at upload on everything except checkpoints,
    so this window never applies to a checkpoint: those are removed by
    reference (apps/reconciler/reconciler/checkpoints.py), however long a
    PARKED task waits. See the lifecycle rule in main.tf.
  EOT
  type        = number
  default     = 90

  validation {
    condition     = var.artifact_retention_days >= 7
    error_message = "artifacts must survive at least a week so a failed run can still be investigated."
  }
}

variable "clone_bundle_retention_days" {
  description = <<-EOT
    Days after upload that a clone bundle (*.swarm-clone.bundle) or a branch
    head pointer (*.swarm-clone.head) is deleted, for every tenant, mapped or
    created at runtime. See the lifecycle rule in main.tf.
  EOT
  type        = number
  # 7: a workflow's downstream steps pin to the upstream's base within the
  # same run, so a bundle is read within hours; a week also covers re-runs
  # and resumes. A miss costs only today's clone from GitHub, so longer buys
  # little and shorter risks a resumed workflow paying the clone again.
  default = 7

  validation {
    condition     = var.clone_bundle_retention_days >= 1
    error_message = "clone bundles must survive at least a day: GCS reads age = 0 as delete on the next pass, before a workflow's next step can clone from one."
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
  description = "Checkpoints are written constantly and read almost never; cold storage after two weeks. Applies to tenants/<t>/tasks/ and tenants/<t>/verdicts/ only, never tenants/<t>/repos/ (see aged_prefixes in main.tf)."
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
  description = "Tenant ids. They publish the per-tenant object prefixes and list the prefixes the Nearline rule cold-stores (tasks/ and verdicts/, never repos/); a tenant missing here, such as a runtime personal tenant, is not cold-stored but still expires on the bucket-wide customTime Delete."
  type        = list(string)
  default     = []
}

variable "labels" {
  type = map(string)
}
