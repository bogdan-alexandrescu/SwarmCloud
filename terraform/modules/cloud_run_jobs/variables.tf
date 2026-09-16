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

variable "network_tags" {
  description = <<-EOT
    Network tags stamped on every worker instance's Direct VPC egress interface.

    A Cloud Run execution is not a VM, but a tagged Direct VPC egress interface
    IS matched by ordinary VPC firewall rules -- which is the only way to keep
    one tenant's worker from reaching another's over the shared subnet. The
    matching deny rule lives in modules/network/firewall.tf; an empty list here
    removes the workers from its scope, so it is refused.
  EOT
  type        = list(string)
  default     = ["swarm-worker"]

  validation {
    condition     = length(var.network_tags) > 0
    error_message = "worker instances must carry at least one network tag, or no firewall rule can confine them."
  }

  validation {
    condition = alltrue([
      for t in var.network_tags : can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", t))
    ])
    error_message = "a network tag is 1-63 lowercase alphanumerics or dashes, starting with a letter."
  }
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

variable "workspace_memory_fraction" {
  description = <<-EOT
    Share of a container's memory limit the workspace volume may occupy.

    Cloud Run only exposes memory-medium ephemeral volumes, so the workspace and
    the agent process spend the same budget. At 1.0 a full workspace OOM-kills
    the container -- a SIGKILL, so no checkpoint and no park. Below 1.0 the
    write fails instead, which the worker can handle.
  EOT
  type        = number
  default     = 0.5

  validation {
    condition     = var.workspace_memory_fraction > 0 && var.workspace_memory_fraction < 1
    error_message = "workspace_memory_fraction must be between 0 and 1, exclusive: at 1.0 a full workspace OOM-kills the agent instead of failing a write."
  }
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
