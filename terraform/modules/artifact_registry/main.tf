# Container images for every runner profile.
#
# Regional, in the same region as the workloads: pulling a multi-gigabyte agent
# image across regions is paid for on every single cold start.

locals {
  role_suffix = var.custom_role_suffix == "" ? "" : "_${var.custom_role_suffix}"
}

resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"

  description = "Swarm runner images (managed-by=swarm-terraform)"

  docker_config {
    immutable_tags = var.immutable_tags
  }

  vulnerability_scanning_config {
    enablement_config = "INHERITED"
  }

  kms_key_name = var.kms_key_name == "" ? null : var.kms_key_name

  cleanup_policy_dry_run = var.cleanup_dry_run

  # Untagged layers accumulate on every build and nothing ever pulls them.
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = var.untagged_ttl
    }
  }

  # Evaluated before DELETE policies, so a rollback target is always present.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"

    most_recent_versions {
      keep_count = var.keep_recent_versions
    }
  }

  labels = var.labels
}

# Keyed by a STABLE label rather than by the member string. Members are service
# account emails that are unknown until apply, and a for_each whose KEYS are
# unknown fails the very first plan -- terraform cannot name instances it cannot
# compute. Keys static, unknown values only in the value.
#
# `readers` is the CONTROL PLANE. It gets the predefined reader role, which
# includes the *.list surface, because the platform's own components are the
# things that legitimately look at what is in the repository. Tenant workers get
# `pullers` below instead -- see the variable for why enumeration is the line.
resource "google_artifact_registry_repository_iam_member" "readers" {
  for_each = var.readers

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}

# Pull without enumerate.
#
# Created only when something actually needs it: a project-level custom role is
# a project-wide name, and a swarm with no tenants should not plant one.
resource "google_project_iam_custom_role" "image_puller" {
  count = length(var.pullers) > 0 ? 1 : 0

  project = var.project_id
  role_id = "swarmImagePuller${local.role_suffix}"
  title   = "Swarm Image Puller"

  description = "Pull an image by a name already held. Cannot enumerate the repository, cannot push."
  stage       = "GA"

  permissions = var.puller_permissions
}

resource "google_artifact_registry_repository_iam_member" "pullers" {
  for_each = var.pullers

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name

  # Referenced through the resource rather than rebuilt as a string, so the role
  # is created before the binding that names it. `role_id` is configuration, not
  # a computed attribute, so the value is still known at plan time; the index is
  # safe because instances of this resource exist only when `pullers` is
  # non-empty, which is exactly when the role's count is 1.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.image_puller[0].role_id}"
  member = each.value
}

resource "google_artifact_registry_repository_iam_member" "writers" {
  for_each = var.writers

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.writer"
  member     = each.value
}
