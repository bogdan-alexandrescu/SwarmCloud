variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "network" {
  type = string
}

variable "subnetwork" {
  type = string
}

variable "resource_classes" {
  description = <<-EOT
    Mirror of swarm_common.profiles.RESOURCE_CLASSES. The Python catalogue is
    authoritative; this map exists because terraform cannot import it, and
    tests/terraform asserts the two agree.
  EOT
  type = map(object({
    cpu        = number
    memory_gib = number
    disk_gib   = number
  }))

  validation {
    condition     = alltrue([for k, v in var.resource_classes : contains([1, 2, 4, 8], v.cpu)])
    error_message = "Cloud Run Jobs accepts only 1, 2, 4 or 8 vCPU."
  }

  validation {
    condition     = alltrue([for k, v in var.resource_classes : v.memory_gib <= 32])
    error_message = "32 GiB is the Cloud Run Jobs ceiling; anything larger must run on GKE Autopilot."
  }

  validation {
    condition     = alltrue([for k, v in var.resource_classes : v.cpu < 4 || v.memory_gib >= 2])
    error_message = "Cloud Run requires at least 2 GiB of memory to allocate 4 or more vCPU."
  }
}

variable "jobs" {
  description = <<-EOT
    One Cloud Run Job per (tenant, runner profile).

    This shape is forced by Cloud Run: the service account is set on the JOB
    resource and cannot be overridden per execution, so a single shared job
    would run every tenant's work under one identity and collapse the tenant
    boundary. The dispatcher creates the same shape at runtime for tenants that
    are not declared in tfvars; the reconciler garbage-collects the ones it
    made and must skip anything carrying `managed-by=swarm-terraform`.
  EOT
  type = map(object({
    tenant_id             = string
    runner_profile        = string
    resource_class        = string
    service_account_email = string
    image                 = string
    command               = optional(list(string), [])
    args                  = optional(list(string), [])
    env                   = optional(map(string), {})
    # env var name -> secret id. Values live in Secret Manager and are read at
    # start; nothing is ever baked into an image or a tfvars file.
    secret_env      = optional(map(string), {})
    timeout_seconds = optional(number, 3600)
  }))

  validation {
    condition     = alltrue([for k, v in var.jobs : startswith(k, "swarm-")])
    error_message = "every Cloud Run Job name must start with 'swarm-'."
  }

  validation {
    condition     = alltrue([for k, v in var.jobs : length(k) <= 63])
    error_message = "Cloud Run Job names are limited to 63 characters."
  }

  validation {
    condition     = alltrue([for k, v in var.jobs : v.timeout_seconds > 0 && v.timeout_seconds <= 86400])
    error_message = "job timeout must be positive and at most 24h."
  }
}

variable "artifact_bucket" {
  description = "Artifact and checkpoint bucket, mounted read-write via Cloud Storage FUSE."
  type        = string
}

variable "mount_artifact_bucket" {
  description = <<-EOT
    Mandatory checkpointing (CONTRACT.md invariant 8) writes to GCS. Mounting
    the bucket means a checkpoint is a file copy rather than an SDK upload the
    worker has to get right while it is being terminated.
  EOT
  type        = bool
  default     = true
}

variable "workspace_mount_path" {
  type    = string
  default = "/workspace"
}

variable "artifact_mount_path" {
  type    = string
  default = "/gcs"
}

variable "deletion_protection" {
  type    = bool
  default = false
}

variable "labels" {
  type = map(string)
}
