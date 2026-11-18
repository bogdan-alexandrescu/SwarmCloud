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

    The role itself, swarmImagePuller, is defined in terraform/bootstrap
    (platform_roles.tf, `image_puller_permissions`) since #79; this module only
    binds it.
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
