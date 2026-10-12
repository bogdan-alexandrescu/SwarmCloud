# The build mirrors (OPS-MIRROR, part of #1030; docs/runbooks/build-mirror.md).
#
# On 2026-10-12 three release image builds failed inside Cloud Build on
# fetches from outside Google: twice "Get https://ghcr.io/v2/: ...
# Client.Timeout exceeded" pulling ghcr.io/astral-sh/uv, once "curl: (22) ...
# error: 504" downloading shellcheck from a github.com release. Nothing about
# those images was wrong. So every image build now reads:
#
#   * its BASE IMAGES, and each Dockerfile's `# syntax=` frontend, through
#     the two REMOTE repositories below, by the digest it pinned before
#     (`FROM ${MIRROR_GHCR}/${UV_IMAGE}`, `FROM ${MIRROR_DOCKER_HUB}/library/
#     ${PYTHON_IMAGE}`). A remote repository fetches a digest from upstream on
#     its first pull and serves it from its own cache after that; a pinned
#     digest never changes, so once cached it never needs upstream again.
#     scripts/refresh-build-mirror.sh pulls every pinned reference through
#     ahead of the first build;
#   * its RELEASE FILES (gh, terraform, tofu, tflint, shellcheck, trivy) from
#     the bucket below, each at its upstream URL without `https://`. Nothing
#     fills it but scripts/refresh-build-mirror.sh, which checks each file
#     against the sha256 in images/agent-runtime-base/tool-mirror.sha256 --
#     the one the Dockerfile checks again -- before uploading it. A build
#     whose object is missing fails, naming it; it never falls back.
#
# NO IAM HERE, deliberately. `gcloud builds submit` runs as the project's
# default compute account (docs/ci.md), which holds roles/editor and so
# already reads both repositories and the bucket. The deployer, which
# applies this file, holds roles/artifactregistry.admin and roles/storage.admin
# conditioned on the `swarm-` bucket prefix (terraform/bootstrap/wif.tf,
# deployer_storage_bucket_prefixes); the refresh runs as an operator.
#
# THE NAMES ARE WRITTEN INTO EVERY DOCKERFILE as the MIRROR_* argument
# defaults (us-central1-docker.pkg.dev/saga-agents-staging/swarm-mirror-*),
# because a checked-in cloudbuild.yaml passes no build argument for them. A
# test (tests/unit/scripts/test_build_mirror.py) holds the two equal for the
# default name_prefix and the dev project.

resource "google_artifact_registry_repository" "mirror_ghcr" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.name_prefix}-mirror-ghcr"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"

  description = "Pull-through cache of ghcr.io for image builds (managed-by=swarm-terraform; docs/runbooks/build-mirror.md)"

  remote_repository_config {
    description = "ghcr.io"
    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }
  }

  # CKV_GCP_84: same wiring as modules/artifact_registry -- a CMEK when one is
  # configured, Google-managed encryption otherwise.
  kms_key_name = var.mirror_kms_key_name == "" ? null : var.mirror_kms_key_name

  labels = local.labels

  depends_on = [module.project_services]
}

resource "google_artifact_registry_repository" "mirror_dockerhub" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.name_prefix}-mirror-dockerhub"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"

  description = "Pull-through cache of Docker Hub for image builds (managed-by=swarm-terraform; docs/runbooks/build-mirror.md)"

  remote_repository_config {
    description = "Docker Hub"
    docker_repository {
      public_repository = "DOCKER_HUB"
    }
  }

  # CKV_GCP_84: same wiring as modules/artifact_registry -- a CMEK when one is
  # configured, Google-managed encryption otherwise.
  kms_key_name = var.mirror_kms_key_name == "" ? null : var.mirror_kms_key_name

  labels = local.labels

  depends_on = [module.project_services]
}

# `swarm-build-mirror-<project>`: under the `swarm-` prefix the deployer's
# storage grants are conditioned on, and the name scripts/lib/common.sh
# derives as BUILD_MIRROR_BUCKET.
resource "google_storage_bucket" "build_mirror" {
  project  = var.project_id
  name     = "${var.name_prefix}-build-mirror-${var.project_id}"
  location = upper(var.region)

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # A refresh with --force overwrites; the previous file stays recoverable.
  versioning {
    enabled = true
  }

  logging {
    log_bucket        = module.storage.access_log_bucket_name
    log_object_prefix = "${var.name_prefix}-build-mirror"
  }

  force_destroy = var.bucket_force_destroy

  labels = merge(local.labels, { "swarm-purpose" = "build-mirror" })
}
