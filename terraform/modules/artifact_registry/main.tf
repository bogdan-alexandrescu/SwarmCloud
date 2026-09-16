# Container images for every runner profile.
#
# Regional, in the same region as the workloads: pulling a multi-gigabyte agent
# image across regions is paid for on every single cold start.

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

resource "google_artifact_registry_repository_iam_member" "readers" {
  for_each = toset(var.readers)

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}

resource "google_artifact_registry_repository_iam_member" "writers" {
  for_each = toset(var.writers)

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.writer"
  member     = each.value
}
