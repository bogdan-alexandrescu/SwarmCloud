# ---------------------------------------------------------------------------
# Managed plugins: the bundle job, its account's grants, the repository it
# pushes to and the dependency mirror it installs through
# (docs/design/managed-plugins.md §1.4.2, §1.4.3, §1.4.7, §15.2).
# ---------------------------------------------------------------------------
#
# gstack and security-audit are managed plugins whose versions are BUILT into
# an image version rather than unpacked per attempt (§1.4, the owner's Q13 of
# 6 November 2026). When a platform admin applies an update to one of them,
# swarm-api runs ONE EXECUTION of the Cloud Run job below,
# `swarm-plugin-bundle`, whose identity is the builder account and whose image
# is the bundler image by digest (the owner, 11 November 2026: one job
# execution under the builder account). The job appends one layer per bundled plugin to
# this deployment's copy of each live release image, with no container daemon
# (images/plugin-bundler/swarm-bundle, `job`), and pushes the result to
# `<name_prefix>-plugin-bundles`, run by digest only.
#
# WHAT A CALLER REACHES. swarm-api holds swarmPluginBundleRunner
# (run.jobs.run, run.jobs.runWithOverrides, run.executions.get; defined in
# terraform/bootstrap/plugin_bundle_runner.tf) on THIS job's IAM policy and
# nowhere else: it can start the job with arguments and read its executions,
# and cannot change its image, command, identity or network, run any other
# job, or act as the builder. Everything the job is configured with is in its
# fixed command; an execution's arguments follow the command's `--` and are
# checked by the entrypoint against closed patterns, which also discards the
# whole environment an override can set (§1.4.2).
#
# THE BUILDER MAY DO EXACTLY FIVE THINGS (§1.4.2, "The builder's grants"):
#
#   1. read the pinned source archives under platform/sources/ in the plugins
#      bucket (get only, by condition; a list names the bucket, so none);
#   2. read the platform repository: the release images it appends to and
#      mounts from (Cloud Run pulls the job's own image as its service agent);
#   3. read the ONE pinned Bun object in the build mirror bucket, by condition
#      on its name (images/agent-runtime-base/bundled-plugins.json);
#   4. read the npm mirror repository, which `bun install --frozen-lockfile`
#      fetches through (Q19);
#   5. write to `<name_prefix>-plugin-bundles` alone: push, no delete,
#      immutable tags.
#
# REMOVED with the job: the Docker Hub mirror read (no Dockerfile
# frontend without a daemon) and roles/logging.logWriter on the project. The
# job writes stdout and stderr, which Cloud Run collects for the job itself;
# the sink below routes those entries to the platform's own log bucket.
#
# THE SWITCH. Every resource here is planned only with var.builtins_managed
# "on": off, as an existing deployment keeps it until a platform admin turns
# it on (§1.4.5), plans nothing at all. The job and the rows that exist only
# for it (swarm-api's binding, the log sink) also wait for the release to
# carry the bundler image's digest, as the intake job waits for its own
# (images.json lists it as a gated image).
#
# WHAT IS NOT HERE, AND WHY. The custom role swarmPluginBundleRunner and the
# release deployer's actAs on the builder (deploying a job that runs as an
# account needs it) are terraform/bootstrap rows the owner applies
# (plugin_bundle_runner.tf): this root may define no custom role
# (tests/terraform/custom_roles_out_of_ci.tftest.hcl) and the deployer holds
# no right to set the builder's IAM policy. swarm-api holds no actAs on the
# builder and no build-service permission anywhere.
#
# THE JOB'S NETWORK (the security review of 11 November 2026). The job
# EXECUTES upstream code: gstack's ./setup, `bun install`, security-audit's
# validator tests. A Cloud Run job with no VPC access has direct internet
# egress, from which that code could send the builder's token (the metadata
# server's, or the one the recipe hands bun) outside the deployment, or fetch
# at build time code that was never in the archive the admin reviewed. So every
# packet the job sends leaves through its own network,
# `<name_prefix>-plugin-bundle`, built as the intake job's is (plugins.tf): no
# router and no Cloud NAT, so nothing it sends reaches the internet; Private
# Google Access on its subnetwork is its one way to Artifact Registry
# (<region>-docker.pkg.dev, <region>-npm.pkg.dev) and Cloud Storage, over the
# default domains, the network's own default route and the metadata server's
# DNS (both made with the network, as the intake network's are). Its egress
# firewall admits TCP 443 from the job's tag alone and denies the rest; flow
# logs on the subnetwork show a refused connection. It is neither the swarm
# network (`allow_internal` there would let the job reach the cluster and the
# internal services) nor the intake network (a build must not reach a job that
# is unpacking an untrusted archive, or the other way round). What it does NOT
# close: Google's APIs answer for every project, so a request to another
# project's resource over HTTPS is not stopped by this network; that is a VPC
# Service Controls perimeter's work, and this deployment has none.
#
# THE OTHER TEAM'S RESOURCES. Nothing here names one: one job, one sink, two
# repositories, one network, its subnetwork and two firewalls, all spelled
# from name_prefix or fixed platform names; the grants are on those
# repositories, the platform repository, the plugins bucket, the build mirror
# bucket and the job itself. No project-level row.

locals {
  builtins_managed_on = var.builtins_managed == "on"

  plugin_bundler_name   = "swarm-plugin-bundler"
  plugin_bundler_email  = "${local.plugin_bundler_name}@${var.project_id}.iam.gserviceaccount.com"
  plugin_bundler_member = "serviceAccount:${local.plugin_bundler_email}"

  plugin_bundles_repository_id = "${var.name_prefix}-plugin-bundles"
  plugin_mirror_npm_id         = "${var.name_prefix}-mirror-npm"

  # The image path every bundle image is under: what the scheduler accepts a
  # bundle record's digest under (§1.4.4) and what the job pushes to (its
  # command's --bundle-repository). "" with the switch off, so nothing
  # accepts a bundle digest.
  plugin_bundle_repository = local.builtins_managed_on ? "${var.region}-docker.pkg.dev/${var.project_id}/${local.plugin_bundles_repository_id}" : ""

  # The pinned source archives a bundle builds from (§1.4.3): get only.
  plugins_sources_condition = "resource.name.startsWith(\"${local.plugins_objects}/platform/sources/\")"

  # THE ONE Bun object the gstack recipe reads (its pinned zip, at its
  # upstream URL without https://, as scripts/refresh-build-mirror.sh uploads
  # it). Read from the release's default pins, the one place it is written, so
  # a Bun bump moves the condition in the same commit as the pin.
  plugin_bundle_bun_object    = jsondecode(file("${path.module}/../../images/agent-runtime-base/bundled-plugins.json")).plugins.gstack.toolchain.bun.object
  plugin_bundle_bun_condition = "resource.name == \"projects/_/buckets/${google_storage_bucket.build_mirror.name}/objects/${local.plugin_bundle_bun_object}\""

  # The job: switch on AND the release carries the bundler image's digest.
  plugin_bundler_image   = lookup(var.image_refs, "swarm-plugin-bundler", "")
  plugin_bundle_job_on   = local.builtins_managed_on && local.plugin_bundler_image != ""
  plugin_bundle_job_name = "swarm-plugin-bundle"

  # The job's own subnetwork. Its network is peered with nothing, so the range
  # could repeat the intake network's; the next /26 (Direct VPC egress's
  # smallest) keeps the two apart in flow logs.
  plugin_bundle_cidr = "10.250.0.64/26"
  plugin_bundle_tag  = "swarm-plugin-bundle"

  # The runner images a bundle is built for, each with the gstack list its
  # tree carries: the browser image lays the browser list over the base list
  # (images/agent-runtime-browser/Dockerfile), every other image the base
  # list. The entrypoint refuses a runner image this list does not name.
  plugin_bundle_runner_images = [
    for name in local.runner_images : "--runner-image=${name}:${name == "agent-runtime-browser" ? "browser" : "base"}"
  ]

  # The job's whole configuration, in its fixed command. An execution's
  # override may replace the arguments (which follow the trailing `--`), the
  # environment, the task count and the timeout; none of them moves this.
  plugin_bundle_command = concat(
    [
      "python3",
      "-I",
      "/opt/swarm-bundler/swarm-bundle",
      "job",
      "--project=${var.project_id}",
      "--region=${var.region}",
      "--plugins-bucket=${google_storage_bucket.plugins.name}",
      "--build-mirror-bucket=${google_storage_bucket.build_mirror.name}",
      "--base-repository=${local.image_base}",
      "--bundle-repository=${local.plugin_bundle_repository}",
      "--npm-registry=https://${var.region}-npm.pkg.dev/${var.project_id}/${local.plugin_mirror_npm_id}/",
    ],
    local.plugin_bundle_runner_images,
    ["--"],
  )
}

# ---------------------------------------------------------------------------
# The builder account.
# ---------------------------------------------------------------------------

# The account is a platform account, so terraform/bootstrap creates it
# (bootstrap/plugin_bundler.tf; plan §8) and this root reads it, only with the
# switch on, as every grant below is.
data "google_service_account" "plugin_bundler" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  account_id = local.plugin_bundler_name
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

# 1. The pinned source archives, get only.
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

  depends_on = [data.google_service_account.plugin_bundler]
}

# 3. The ONE pinned Bun object (its zip, which the recipe checks against its
# pinned sha256 before it runs), no longer the whole bucket.
resource "google_storage_bucket_iam_member" "plugin_bundler_tool_mirror" {
  count = local.builtins_managed_on ? 1 : 0

  bucket = google_storage_bucket.build_mirror.name
  role   = "roles/storage.objectViewer"
  member = local.plugin_bundler_member

  condition {
    title       = "swarm-plugin-bundler-bun-read"
    description = "managed-by=swarm-terraform; the bundle job reads the one pinned Bun zip of the build mirror and nothing else in it (managed-plugins.md section 1.4.2)."
    expression  = local.plugin_bundle_bun_condition
  }

  depends_on = [data.google_service_account.plugin_bundler]
}

# 2. The release images a bundle appends to and mounts its base layers from,
# this deployment's copies (stage 4 copied them by digest after cosign
# verify). Cloud Run pulls the job's own image as its service agent.
resource "google_artifact_registry_repository_iam_member" "plugin_bundler_release_images" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = module.artifact_registry.repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.plugin_bundler_member

  depends_on = [data.google_service_account.plugin_bundler]
}

# 4. gstack's npm dependencies, through the mirror only (Q19).
resource "google_artifact_registry_repository_iam_member" "plugin_bundler_npm" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.mirror_npm[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.plugin_bundler_member

  depends_on = [data.google_service_account.plugin_bundler]
}

# 5. The one repository it writes: push, never delete (writer holds no
# delete), and the repository refuses a moved tag.
resource "google_artifact_registry_repository_iam_member" "plugin_bundler_push" {
  count = local.builtins_managed_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.plugin_bundles[0].repository_id
  role       = "roles/artifactregistry.writer"
  member     = local.plugin_bundler_member

  depends_on = [data.google_service_account.plugin_bundler]
}

# Nothing else: no project role, no Docker Hub mirror, no log writer (the
# header says why each went).

# ---------------------------------------------------------------------------
# The job's network: Google's APIs and nothing else (the header says why).
# Planned with the switch, before the bundler image, so it is in place for the
# job's first deploy. The compute types take no labels
# (scripts/lib/unlabelable-types.json): each description carries
# managed-by=swarm-terraform.
# ---------------------------------------------------------------------------

resource "google_compute_network" "plugin_bundle" {
  count = local.builtins_managed_on ? 1 : 0

  project                 = var.project_id
  name                    = "${var.name_prefix}-plugin-bundle"
  auto_create_subnetworks = false
  description             = "managed-by=swarm-terraform; the plugin bundle job's network: no router, no NAT, peered with nothing (managed-plugins.md section 1.4.2)."

  depends_on = [module.project_services]
}

resource "google_compute_subnetwork" "plugin_bundle" {
  count = local.builtins_managed_on ? 1 : 0

  project       = var.project_id
  name          = "${var.name_prefix}-plugin-bundle"
  region        = var.region
  network       = google_compute_network.plugin_bundle[0].id
  ip_cidr_range = local.plugin_bundle_cidr

  # Artifact Registry and Cloud Storage through Google's network; no other
  # route leaves it.
  private_ip_google_access = true
  description              = "managed-by=swarm-terraform; the plugin bundle job's subnetwork, Private Google Access only."

  # Upstream code that tries to leave shows up here as refused connections.
  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 1
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "plugin_bundle_deny_egress" {
  count = local.builtins_managed_on ? 1 : 0

  project   = var.project_id
  name      = "${var.name_prefix}-fw-plugin-bundle-deny-egress"
  network   = google_compute_network.plugin_bundle[0].name
  direction = "EGRESS"
  priority  = 65534

  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }

  description = "managed-by=swarm-terraform; the plugin bundle network sends nothing but HTTPS to Google's APIs."
}

resource "google_compute_firewall" "plugin_bundle_allow_google_apis" {
  count = local.builtins_managed_on ? 1 : 0

  project   = var.project_id
  name      = "${var.name_prefix}-fw-plugin-bundle-allow-apis"
  network   = google_compute_network.plugin_bundle[0].name
  direction = "EGRESS"
  priority  = 1000

  destination_ranges = ["0.0.0.0/0"]
  target_tags        = [local.plugin_bundle_tag]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }

  description = "managed-by=swarm-terraform; HTTPS from the bundle job, which with no NAT reaches only Google's APIs through Private Google Access."
}

# ---------------------------------------------------------------------------
# The job (§1.4.2, "The job").
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "plugin_bundle" {
  count = local.plugin_bundle_job_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = local.plugin_bundle_job_name

  labels = merge(local.labels, { component = "plugins" })

  deletion_protection = var.deletion_protection

  template {
    # One execution per Apply, one task: the entrypoint refuses a count
    # other than one whatever an override sets.
    task_count  = 1
    parallelism = 1

    template {
      service_account = local.plugin_bundler_email
      # A failed build is reported; a retry is an admin's Apply again.
      max_retries = 0
      # The task's ceiling. The entrypoint keeps its own 3000-second
      # deadline inside it, whatever timeout an execution is given.
      timeout = "3600s"

      # Every packet through the job's own network, which reaches Google's
      # APIs (Artifact Registry, where the mirrors are, and Cloud Storage)
      # through Private Google Access and nothing else: no router, no NAT.
      vpc_access {
        egress = "ALL_TRAFFIC"

        network_interfaces {
          network    = google_compute_network.plugin_bundle[0].id
          subnetwork = google_compute_subnetwork.plugin_bundle[0].id
          tags       = [local.plugin_bundle_tag]
        }
      }

      containers {
        image   = local.plugin_bundler_image
        command = local.plugin_bundle_command

        # Memory and disk are one (the in-memory filesystem): gstack's setup,
        # its dependencies and two staged trees, each packed as a layer.
        resources {
          limits = {
            cpu    = "4"
            memory = "8Gi"
          }
        }

        # No env block: the entrypoint discards every variable an execution
        # carries and runs each recipe with its own fixed environment.
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }

  depends_on = [
    module.project_services,
    data.google_service_account.plugin_bundler,
    google_compute_firewall.plugin_bundle_deny_egress,
    google_compute_firewall.plugin_bundle_allow_google_apis,
  ]
}

# swarm-api runs THIS job with arguments and reads its executions, through
# the custom role terraform/bootstrap defines; on the job's own IAM policy,
# never on the project, because Cloud Run exposes no resource name to IAM
# Conditions and a project grant could not be narrowed to one job.
resource "google_cloud_run_v2_job_iam_member" "plugin_bundle_api_runs" {
  count = local.plugin_bundle_job_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.plugin_bundle[0].name
  role     = module.plugins_role_ids.names.plugin_bundle_runner
  member   = module.iam.service_account_members["swarm-api"]
}

# The executions' logs, upstream output included, copied to the platform's own
# log bucket (log_bucket.tf); they also stay in the project's _Default bucket,
# where project-level log readers see them. The page never shows them (§1.4.3:
# a fixed code and the execution's name). The
# destination is a log bucket of this project, so the sink needs no writer
# grant (log_bucket.tf says why). Planned only with that bucket.
#
# No labels: google_logging_project_sink has none
# (scripts/lib/unlabelable-types.json).
resource "google_logging_project_sink" "plugin_bundle_logs" {
  count = local.plugin_bundle_job_on && local.swarm_worker_logs_on == 1 ? 1 : 0

  project     = var.project_id
  name        = "${var.name_prefix}-plugin-bundle-logs"
  destination = "logging.googleapis.com/${local.swarm_worker_logs_bucket}"
  filter      = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${local.plugin_bundle_job_name}\""
  description = "managed-by=swarm-terraform; the plugin bundle job's executions only, to the platform's log bucket (terraform/infra/plugin-bundles.tf)."

  depends_on = [google_logging_project_bucket_config.swarm_worker_logs]
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
