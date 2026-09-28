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

variable "pullers" {
  description = <<-EOT
    Identities that may PULL an image by name and nothing else, keyed by a
    stable label.

    Tenant workers belong here rather than in `readers`, and the split is the
    point. Artifact Registry IAM stops at the REPOSITORY: there is no per-image
    binding and no condition attribute that can name one, so every identity with
    any grant on this repository can fetch every image in it. What is still
    available at this layer is the shape of the access, so the role is built
    rather than borrowed.

    roles/artifactregistry.reader carries the whole *.list surface --
    packages.list, versions.list, tags.list, dockerimages.list,
    repositories.list. That is what turns "can pull" into "can ENUMERATE": one
    tenant's worker, running attacker-controlled code by design, could list
    every image name in the repository, and the first customer- or
    tenant-specific runner image published here would be discoverable by every
    other tenant. Dropping the list permissions means an image can only be
    fetched under a name the caller already holds -- the same construction as
    the tenant Firestore role in modules/tenancy (no entities.list) and the
    bucket metadata role beside it (no objects.list).

    The residual is written down rather than implied: a puller that GUESSES an
    image name can still pull it. Closing that needs a separate repository per
    trust boundary, which IAM cannot express inside one.
  EOT
  type        = map(string)
  default     = {}
}

variable "puller_permissions" {
  description = <<-EOT
    The pull-only role's permission set: roles/artifactregistry.reader minus
    every enumeration permission, and minus everything that writes.

    Verified against `gcloud iam list-testable-permissions` for this project on
    2026-09-16 -- an invalid permission name fails at apply, not at plan, so
    this list is checked against the API rather than remembered.
  EOT
  type        = list(string)
  default = [
    # Resolve the repository and the region.
    "artifactregistry.repositories.get",
    "artifactregistry.locations.get",
    # Pull: the manifest, the version behind the tag, and the layers.
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.dockerimages.get",
    "artifactregistry.packages.get",
    "artifactregistry.tags.get",
    "artifactregistry.versions.get",
    "artifactregistry.files.get",
    "artifactregistry.files.download",
  ]

  validation {
    condition     = length(var.puller_permissions) > 0
    error_message = "the pull-only role needs at least the download permissions, or every worker fails to start on an image pull."
  }

  validation {
    condition = alltrue([
      for p in var.puller_permissions : startswith(p, "artifactregistry.")
    ])
    error_message = "this role covers Artifact Registry only; anything else belongs in a named role where it is visible."
  }

  # `.list`, `listEffectiveTags` and `listTagBindings` are all enumeration, and
  # enumeration is the single property this role exists to withhold.
  validation {
    condition = alltrue([
      for p in var.puller_permissions : !strcontains(lower(p), "list")
    ])
    error_message = "no enumeration permission may be added: the point of this role is that a tenant worker cannot discover an image name it was not given."
  }

  validation {
    condition = alltrue([
      for p in var.puller_permissions :
      !can(regex("(create|update|delete|upload|export|setIamPolicy|createOnPush)", p))
    ])
    error_message = "the pull role is read-only: no runtime identity may push, delete or export an image."
  }
}

variable "custom_role_suffix" {
  description = "Disambiguates the custom role id when a previous one is still inside its 7-day soft-delete window."
  type        = string
  default     = ""
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
