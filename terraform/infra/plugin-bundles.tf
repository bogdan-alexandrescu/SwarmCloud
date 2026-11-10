# ---------------------------------------------------------------------------
# Managed plugins: the bundle build's account, its grants, the repository it
# pushes to and the dependency mirror it installs through
# (docs/design/managed-plugins.md §1.4.2, §1.4.3, §15.2; lane 15 of §17.1).
# ---------------------------------------------------------------------------
#
# gstack and security-audit are managed plugins whose versions are BUILT into
# an image version rather than unpacked per attempt (§1.4, the owner's Q13 of
# 6 November 2026). When a platform admin applies an update to one of them,
# swarm-api starts one Cloud Build build in this project, as the account
# below, running the swarm-plugin-bundler image (lane 14) FROM this
# deployment's own copy of the live release image, by digest (the owner,
# 9 November 2026: the build runs in the deployment). Its output is one image
# per runner image a claude-code profile uses, pushed to
# `<name_prefix>-plugin-bundles` and run by digest only.
#
# THE BUILDER MAY DO EXACTLY FOUR THINGS (§15.2), each a grant below:
#
#   * read the pinned source archives under platform/sources/ in the plugins
#     bucket (get only; a list names the bucket, so no condition admits one);
#   * read the deployment's copies of the release images (the bundler image
#     and the base it builds FROM) in the platform repository;
#   * fetch dependencies, only through this deployment's mirrors (the owner,
#     9 November 2026, Q19): gstack's npm packages through the npm remote
#     repository below at its frozen bun.lock, Bun's pinned zip from the build
#     mirror bucket (build_mirror.tf, the same object the release build reads),
#     and the Dockerfile frontend through the Docker Hub mirror;
#   * push to `<name_prefix>-plugin-bundles`, and to no other repository;
#
# plus write its own build log (Cloud Build refuses a build as a user-specified
# account that cannot write one). No Firestore, no Secret Manager, no GitHub,
# no tenants/ prefix, no other project role.
#
# THE SWITCH. Every resource here is planned only with var.builtins_managed
# "on": off, as an existing deployment keeps it until a platform admin turns
# it on (§1.4.5), plans nothing at all, so nothing here costs money or grants
# access before the switch.
#
# WHAT IS NOT HERE, AND WHY (asked for in this lane's pull request).
#
#   * swarm-api's roles/iam.serviceAccountUser on the builder account. Setting
#     an account's IAM policy needs serviceAccountAdmin on that account, which
#     the release deployer holds only on the accounts terraform/bootstrap
#     grants it on (deployer_service_accounts.tf, #334), and this root runs as
#     the deployer in every release and every install pass. So the row is a
#     terraform/bootstrap change the owner applies; until it is applied no
#     bundle build can start, and nothing else here depends on it.
#   * swarm-api's right to start a build (cloudbuild.builds.create and get).
#     roles/cloudbuild.builds.editor is not on the deployer's grantable project
#     roles, and the narrow role it should be is a custom role, which this root
#     may not define (tests/terraform/custom_roles_out_of_ci.tftest.hcl).
#
# THE OTHER TEAM'S RESOURCES. Nothing here names one: one account, two
# repositories, all spelled from name_prefix; the grants are on those two
# repositories, the platform repository, the plugins bucket and the build
# mirror bucket, and the one project-level row is roles/logging.logWriter,
# which is on the deployer's grantable list.

locals {
  builtins_managed_on = var.builtins_managed == "on"

  plugin_bundler_name   = "swarm-plugin-bundler"
  plugin_bundler_email  = "${local.plugin_bundler_name}@${var.project_id}.iam.gserviceaccount.com"
  plugin_bundler_member = "serviceAccount:${local.plugin_bundler_email}"

  plugin_bundles_repository_id = "${var.name_prefix}-plugin-bundles"
  plugin_mirror_npm_id         = "${var.name_prefix}-mirror-npm"

  # The image path every bundle image is under: what the scheduler accepts a
  # bundle record's digest under (§1.4.4) and what swarm-api pushes to. ""
  # with the switch off, so nothing accepts a bundle digest.
  plugin_bundle_repository = local.builtins_managed_on ? "${var.region}-docker.pkg.dev/${var.project_id}/${local.plugin_bundles_repository_id}" : ""

  # The pinned source archives a bundle builds from (§1.4.3): get only.
  plugins_sources_condition = "resource.name.startsWith(\"${local.plugins_objects}/platform/sources/\")"
}

# ---------------------------------------------------------------------------
# The builder account.
# ---------------------------------------------------------------------------

resource "google_service_account" "plugin_bundler" {
  count = local.builtins_managed_on ? 1 : 0

  project      = var.project_id
  account_id   = local.plugin_bundler_name
  display_name = "Swarm plugin bundle builder"
  description  = "managed-by=swarm-terraform; the bundle build of the managed plugins gstack and security-audit (managed-plugins.md section 1.4.2). Reads platform/sources/, the release images and the mirrors; pushes to the plugin-bundles repository only."
}

# ---------------------------------------------------------------------------
# The repositories.
# ---------------------------------------------------------------------------

# Where every bundle image is pushed:
# <region>-docker.pkg.dev/<project>/<name_prefix>-plugin-bundles/<runner image
# name>:<bundle_id>. The tag is written once and never moved (§1.4.3), so the
# repository refuses a moved tag. No cleanup policy: a bundle's images are
# deleted only by the retention sweep of §9.3, which knows which keys an
# unfinished step still names; an age rule would delete one under a queued step.
resource "google_artifact_registry_repository" "plugin_bundles" {
  count = local.builtins_managed_on ? 1 : 0

  project       = var.project_id
  location      = var.region
  repository_id = local.plugin_bundles_repository_id
  format        = "DOCKER"
  description   = "Bundle images of the managed plugins gstack and security-audit, built in this deployment (managed-by=swarm-terraform; managed-plugins.md section 1.4.3)"

  docker_config {
    immutable_tags = true
  }

  # CKV_GCP_84: as build_mirror.tf -- a CMEK when one is configured.
  kms_key_name = var.mirror_kms_key_name == "" ? null : var.mirror_kms_key_name

  labels = merge(local.labels, { "swarm-purpose" = "plugin-bundles" })

  depends_on = [module.project_services]
}

# gstack's npm dependencies (Q19): a pull-through cache of the public npm
# registry, so the build never fetches from it directly. The recipe installs
# only at the archive's bun.lock, frozen, and fails unless the lockfile is
# byte-for-byte the archive's afterwards (images/plugin-bundler/recipes/gstack).
resource "google_artifact_registry_repository" "mirror_npm" {
  count = local.builtins_managed_on ? 1 : 0

  project       = var.project_id
  location      = var.region
  repository_id = local.plugin_mirror_npm_id
  format        = "NPM"
  mode          = "REMOTE_REPOSITORY"

  description = "Pull-through cache of the npm registry for the plugin bundle build (managed-by=swarm-terraform; managed-plugins.md section 1.4.2, Q19)"

  remote_repository_config {
    description = "npm registry"
    npm_repository {
      public_repository = "NPMJS"
    }
  }

  kms_key_name = var.mirror_kms_key_name == "" ? null : var.mirror_kms_key_name

  labels = merge(local.labels, { "swarm-purpose" = "plugin-bundle-mirror" })

  depends_on = [module.project_services]
}

# ---------------------------------------------------------------------------
# The builder's grants. An IAM member takes no labels: a conditioned row's
# description carries managed-by=swarm-terraform, and each row is listed in
# install/manifest.d/plugin-bundles.yaml.
# ---------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "plugin_bundler_sources" {
  count = local.builtins_managed_on ? 1 : 0

  bucket = google_storage_bucket.plugins.name
  role   = "roles/storage.objectViewer"
  member = local.plugin_bundler_member

  condition {
    title       = "swarm-plugin-bundler-sources-read"
    description = "managed-by=swarm-terraform; the bundle build reads the pinned source archives under platform/sources/ only, get and no list (managed-plugins.md section 1.4.2)."
    expression  = local.plugins_sources_condition
  }

  depends_on = [google_service_account.plugin_bundler]
}

# Bun's pinned zip, at its upstream URL without https:// (tool-mirror.sha256),
# the object the recipe checks against its pinned sha256 before it runs.
resource "google_storage_bucket_iam_member" "plugin_bundler_tool_mirror" {
  count = local.builtins_managed_on ? 1 : 0

  bucket = google_storage_bucket.build_mirror.name
  role   = "roles/storage.objectViewer"
  member = local.plugin_bundler_member

  depends_on = [google_service_account.plugin_bundler]
}

# The bundler image and the release image a bundle builds FROM, both this
# deployment's copies (stage 4 copied them by digest after cosign verify).
resource "google_artifact_registry_repository_iam_member" "plugin_bundler_release_images" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = module.artifact_registry.repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.plugin_bundler_member

  depends_on = [google_service_account.plugin_bundler]
}

resource "google_artifact_registry_repository_iam_member" "plugin_bundler_npm" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.mirror_npm[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.plugin_bundler_member

  depends_on = [google_service_account.plugin_bundler]
}

# The bundle Dockerfile's `# syntax=` frontend, by its pinned digest, as every
# release build reads it (build_mirror.tf).
resource "google_artifact_registry_repository_iam_member" "plugin_bundler_dockerhub" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.mirror_dockerhub.repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.plugin_bundler_member

  depends_on = [google_service_account.plugin_bundler]
}

# The one repository it writes: push, never delete (writer holds no delete).
resource "google_artifact_registry_repository_iam_member" "plugin_bundler_push" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.plugin_bundles[0].repository_id
  role       = "roles/artifactregistry.writer"
  member     = local.plugin_bundler_member

  depends_on = [google_service_account.plugin_bundler]
}

# Its own build log, write only: a build as a user-specified account must
# name where its log goes, and swarm-api starts it with CLOUD_LOGGING_ONLY.
# The page never shows the log (§1.4.3: a fixed code and the build id).
resource "google_project_iam_member" "plugin_bundler_logs" {
  count = local.builtins_managed_on ? 1 : 0

  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = local.plugin_bundler_member

  depends_on = [google_service_account.plugin_bundler]
}

# ---------------------------------------------------------------------------
# Who pulls a bundle image: the same two sides as the platform repository
# (main.tf, module.artifact_registry). The control plane may read and list;
# a tenant worker, which runs attacker-controlled code by design, pulls
# without enumerating (swarmImagePuller, defined in terraform/bootstrap).
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository_iam_member" "plugin_bundles_readers" {
  for_each = local.builtins_managed_on ? toset(local.control_plane_services) : toset([])

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.plugin_bundles[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = module.iam.service_account_members[each.key]
}

resource "google_artifact_registry_repository_iam_member" "plugin_bundles_pullers" {
  for_each = local.builtins_managed_on ? var.tenants : {}

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.plugin_bundles[0].repository_id
  role       = module.plugins_role_ids.names.image_puller
  # Spelled from the account id modules/tenancy creates it under, as
  # plugins.tf's tenant read is, so the plan knows the member.
  member = "serviceAccount:${module.service_account_ids.tenant_worker_prefix}${each.key}@${var.project_id}.iam.gserviceaccount.com"

  depends_on = [module.tenancy]
}
