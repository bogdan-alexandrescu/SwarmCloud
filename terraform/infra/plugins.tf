# ---------------------------------------------------------------------------
# Managed plugins: the bucket, its IAM, the intake job and its network
# (the job's account is made by terraform/bootstrap/plugin_intake.tf)
# (docs/design/managed-plugins.md §2.2, §3, §9.3 and §15; lane 3 of §17).
# ---------------------------------------------------------------------------
#
# One bucket holds every pinned plugin copy, by digest:
#
#   platform/<sha256>.tar             platform installs, read by every tenant
#   tenants/<tenant>/<sha256>.tar     a workspace's private installs, read by
#                                     that workspace's worker account alone
#   quarantine/<intake_id>            the bytes swarm-api fetched (or the
#                                     console uploaded through a signed URL)
#                                     before anything has unpacked them
#   staging/<intake_id>/...           what the intake job wrote back
#
# THE INTAKE JOB (§2.2 option A, the owner's answer of 2026-11-06). The
# untrusted archive is unpacked only in `swarm-plugin-intake`, a Cloud Run job
# whose account reads quarantine/ and creates under staging/ and holds nothing
# else: no list, no delete, no write to platform/ or tenants/, no Firestore, no
# Secret Manager and no project role at all. It has NO EGRESS: every packet
# leaves through its own network, `<name_prefix>-plugin-intake`, which has no
# router and no Cloud NAT, so nothing it sends reaches the internet; Private
# Google Access on its subnetwork is its one way to Cloud Storage. The network
# is not the swarm network on purpose: there, `allow_internal` would let a job
# the archive takes over reach the cluster and the internal services. Its
# egress firewall admits TCP 443 alone (Google's APIs) and denies the rest.
#
# THE JOB EXISTS ONCE ITS IMAGE DOES. `swarm-plugin-intake` is built by lane 4
# (apps/plugin-intake); it is a gated image (images.json), so the job is planned
# only when the release carries its digest, exactly as github_hooks.tf's
# receiver is. Everything else here is made by every apply, so the bucket and
# the job's account, network and grants are in place before the first image.
#
# WHAT IS NOT HERE, AND WHY. swarm-api's versions.access on the -plg- secrets
# (§6.2) cannot be granted by this root: a role carrying versions.access may
# not be on the CI deployer's grantable list (terraform/bootstrap
# variables.tf, deployer_secret_permissions), so it is a terraform/bootstrap
# change the owner applies, asked for in this lane's pull request. A personal
# workspace's read of platform/ and of its own tenants/<t>/ is made by
# scripts/register-tenant.sh, which makes every other per-workspace grant;
# installs declare no Terraform tenants, so the rows below cover the tenants
# this root lists, and the script's rows are asked for in the same pull
# request.
#
# THE OTHER TEAM'S RESOURCES. Nothing here names one: one new bucket, one
# new network, its subnetwork and two firewalls, all spelled from
# name_prefix or the project id; the grants are on the new bucket and job only,
# and the one project-level row is conditioned on swarm-tenant-*-plg- names.

locals {
  plugins_bucket = var.plugins_bucket_name != "" ? var.plugins_bucket_name : "${var.project_id}-swarm-plugins"

  plugin_intake_name   = "swarm-plugin-intake"
  plugin_intake_email  = "${local.plugin_intake_name}@${var.project_id}.iam.gserviceaccount.com"
  plugin_intake_member = "serviceAccount:${local.plugin_intake_email}"
  plugin_intake_image  = lookup(var.image_refs, "swarm-plugin-intake", "")
  plugin_intake_on     = local.plugin_intake_image != ""

  # The intake job's own subnetwork. Its network is peered with nothing, so the
  # range may repeat one used elsewhere; /26 is Direct VPC egress's smallest.
  plugin_intake_cidr = "10.250.0.0/26"
  plugin_intake_tag  = "swarm-plugin-intake"

  # The caps the owner set on 2026-11-06 (NOT the design's smaller defaults):
  # 100 MiB packed, 500 MiB unpacked, 20,000 files.
  plugin_caps = {
    PLUGIN_MAX_PACKED_BYTES   = tostring(100 * 1024 * 1024)
    PLUGIN_MAX_UNPACKED_BYTES = tostring(500 * 1024 * 1024)
    PLUGIN_MAX_FILES          = "20000"
  }

  # Object-name prefixes, as IAM conditions read them. A list request names the
  # bucket, never an object, so no condition below can be true of a list: every
  # conditioned read is get-only, as §3 asks of the tenant reads.
  plugins_objects = "projects/_/buckets/${local.plugins_bucket}/objects"
  plugins_prefix = {
    platform   = "resource.name.startsWith(\"${local.plugins_objects}/platform/\")"
    quarantine = "resource.name.startsWith(\"${local.plugins_objects}/quarantine/\")"
    staging    = "resource.name.startsWith(\"${local.plugins_objects}/staging/\")"
    tenants    = "resource.name.startsWith(\"${local.plugins_objects}/tenants/\")"
  }
  plugins_tenant_read = {
    for t, _ in var.tenants :
    t => "${local.plugins_prefix.platform} || resource.name.startsWith(\"${local.plugins_objects}/tenants/${t}/\")"
  }
  plugins_gc_condition = "${local.plugins_prefix.platform} || ${local.plugins_prefix.tenants}"

  # A plugin secret by its full name: swarm-tenant-<t>-plg-<16 hex> (§6.2), with
  # a non-empty tenant before the first -plg-. The project NUMBER: IAM names
  # secrets by it, and the id "can't be substituted" (workspace_secrets.tf in
  # terraform/bootstrap says the same of -wsec-).
  plugin_secret_condition = "resource.name.startsWith(\"projects/${data.google_project.this.number}/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-plg-\") != \"\""
}

resource "google_storage_bucket" "plugins" {
  project  = var.project_id
  name     = local.plugins_bucket
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  labels = merge(local.labels, { component = "plugins" })

  # A pinned copy deleted by mistake (or by a wrong GC pass) is recoverable for
  # a week; the reconciler's deletes leave a noncurrent version that ages out.
  versioning {
    enabled = true
  }

  logging {
    log_bucket        = module.storage.access_log_bucket_name
    log_object_prefix = "${var.name_prefix}-plugins"
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 7
    }
    action {
      type = "Delete"
    }
  }

  # Intake leftovers: a quarantine object is deleted by swarm-api once its
  # intake ends, and staging/ is copied out; whatever a crash leaves goes after
  # a day. platform/ and tenants/ are the reconciler's (§9.3), never aged here.
  lifecycle_rule {
    condition {
      age            = 1
      matches_prefix = ["quarantine/", "staging/"]
    }
    action {
      type = "Delete"
    }
  }
}

# ---------------------------------------------------------------------------
# The bucket's grants. An IAM member takes no labels: each condition's
# description carries managed-by=swarm-terraform.
# ---------------------------------------------------------------------------

# swarm-api owns the catalogue: it writes quarantine/ (and signs the console's
# upload URL to it), reads staging/, writes platform/ and tenants/ after
# re-hashing a staged tar (§2.3), and deletes intake leftovers. It is the only
# writer of quarantine/, platform/ and tenants/.
resource "google_storage_bucket_iam_member" "plugins_api_objects" {
  bucket = google_storage_bucket.plugins.name
  role   = "roles/storage.objectUser"
  member = module.iam.service_account_members["swarm-api"]
}

# The intake job reads the one quarantine object it is started on: get, under
# quarantine/ only, and no list.
resource "google_storage_bucket_iam_member" "plugins_intake_read" {
  bucket = google_storage_bucket.plugins.name
  role   = "roles/storage.objectViewer"
  member = local.plugin_intake_member

  condition {
    title       = "swarm-plugin-intake-quarantine-read"
    description = "managed-by=swarm-terraform; the intake job reads objects under quarantine/ only (managed-plugins.md section 2.2)."
    expression  = local.plugins_prefix.quarantine
  }

  depends_on = [data.google_service_account.plugin_intake]
}

# ...and creates its report and the normalised tar under staging/: create
# only, so it can neither overwrite nor delete what is there.
resource "google_storage_bucket_iam_member" "plugins_intake_stage" {
  bucket = google_storage_bucket.plugins.name
  role   = "roles/storage.objectCreator"
  member = local.plugin_intake_member

  condition {
    title       = "swarm-plugin-intake-staging-create"
    description = "managed-by=swarm-terraform; the intake job creates objects under staging/ only, never overwriting (managed-plugins.md section 2.2)."
    expression  = local.plugins_prefix.staging
  }

  depends_on = [data.google_service_account.plugin_intake]
}

# Garbage collection (§9.3): the reconciler's daily sweep lists the pinned
# objects and deletes one no version, candidate or unfinished step names.
# Read and list across the bucket; delete under platform/ and tenants/ only,
# through the delete-only role terraform/bootstrap defines (the one voice.tf
# uses), so it can never write a pinned copy.
# The delete role's full name, a plain string: terraform/bootstrap defines it,
# and this root may not so much as read one (#79).
module "plugins_role_ids" {
  source = "../modules/custom_role_ids"

  project_id = var.project_id
}

resource "google_storage_bucket_iam_member" "plugins_reconciler_list" {
  bucket = google_storage_bucket.plugins.name
  role   = "roles/storage.objectViewer"
  member = module.iam.service_account_members["swarm-reconciler"]
}

resource "google_storage_bucket_iam_member" "plugins_reconciler_gc" {
  bucket = google_storage_bucket.plugins.name
  role   = module.plugins_role_ids.names.history_purge_deleter
  member = module.iam.service_account_members["swarm-reconciler"]

  condition {
    title       = "swarm-plugins-gc"
    description = "managed-by=swarm-terraform; the reconciler deletes unreferenced pinned plugin copies under platform/ and tenants/ only (managed-plugins.md section 9.3)."
    expression  = local.plugins_gc_condition
  }
}

# A tenant's worker reads platform copies and its own workspace's copies, get
# only: a worker of workspace A cannot download B's private plugin even with
# B's digest in hand (§3).
resource "google_storage_bucket_iam_member" "plugins_tenant_read" {
  for_each = var.tenants

  bucket = google_storage_bucket.plugins.name
  role   = "roles/storage.objectViewer"
  # Spelled from the account id modules/tenancy creates it under, so the
  # plan knows the member; the account exists first (depends_on).
  member = "serviceAccount:${module.service_account_ids.tenant_worker_prefix}${each.key}@${var.project_id}.iam.gserviceaccount.com"

  condition {
    title       = "swarm-plugins-read-${each.key}"
    description = "managed-by=swarm-terraform; tenant ${each.key} reads plugin copies under platform/ and tenants/${each.key}/ only, get and no list (managed-plugins.md section 3)."
    expression  = local.plugins_tenant_read[each.key]
  }

  depends_on = [module.tenancy]
}

# ---------------------------------------------------------------------------
# swarm-api's plugin secrets (§6.2): versions added to swarm-tenant-<t>-plg-*
# only. The one row CI may make; the read is terraform/bootstrap's (header).
# ---------------------------------------------------------------------------

resource "google_project_iam_member" "plugins_api_secret_versions" {
  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = module.iam.service_account_members["swarm-api"]

  condition {
    title       = "swarm plugin secrets"
    description = "managed-by=swarm-terraform; swarm-api adds versions to plugin secrets swarm-tenant-<t>-plg-* only (managed-plugins.md section 6.2). No tenant worker holds this."
    expression  = local.plugin_secret_condition
  }
}

# ---------------------------------------------------------------------------
# The intake job's account and network.
# ---------------------------------------------------------------------------

# The account is a platform account, so terraform/bootstrap creates it
# (bootstrap/plugin_intake.tf; plan §8) and this root reads it.
data "google_service_account" "plugin_intake" {
  project    = var.project_id
  account_id = local.plugin_intake_name
}

# Deploying a job that runs as the account needs actAs on it, so the grant is
# planned only with the job (local.plugin_intake_on: once the release carries
# the intake image's digest). The account is bootstrap's and the deployer holds
# no right to read or set its policy, so planning the grant before then fails
# every release on iam.serviceAccounts.getIamPolicy. Before the intake image
# ships, terraform/bootstrap must first give the deployer that right on this
# account; the owner applies that change.
resource "google_service_account_iam_member" "plugin_intake_deployer_acts_as" {
  count = local.plugin_intake_on && local.deployer_member != "" ? 1 : 0

  # Spelled, so the plan knows it; the read orders it after the account exists.
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.plugin_intake_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member

  depends_on = [data.google_service_account.plugin_intake]
}

resource "google_compute_network" "plugin_intake" {
  project                 = var.project_id
  name                    = "${var.name_prefix}-plugin-intake"
  auto_create_subnetworks = false
  description             = "managed-by=swarm-terraform; the plugin intake job's network: no router, no NAT, peered with nothing (managed-plugins.md section 2.2)."
}

resource "google_compute_subnetwork" "plugin_intake" {
  project       = var.project_id
  name          = "${var.name_prefix}-plugin-intake"
  region        = var.region
  network       = google_compute_network.plugin_intake.id
  ip_cidr_range = local.plugin_intake_cidr

  # Cloud Storage through Google's network; no other route leaves it.
  private_ip_google_access = true
  description              = "managed-by=swarm-terraform; the plugin intake job's subnetwork, Private Google Access only."

  # A job an archive takes over shows up here as refused connections.
  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 1
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "plugin_intake_deny_egress" {
  project   = var.project_id
  name      = "${var.name_prefix}-fw-plugin-intake-deny-egress"
  network   = google_compute_network.plugin_intake.name
  direction = "EGRESS"
  priority  = 65534

  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }

  description = "managed-by=swarm-terraform; the plugin intake network sends nothing but HTTPS to Google's APIs."
}

resource "google_compute_firewall" "plugin_intake_allow_google_apis" {
  project   = var.project_id
  name      = "${var.name_prefix}-fw-plugin-intake-allow-apis"
  network   = google_compute_network.plugin_intake.name
  direction = "EGRESS"
  priority  = 1000

  destination_ranges = ["0.0.0.0/0"]
  target_tags        = [local.plugin_intake_tag]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }

  description = "managed-by=swarm-terraform; HTTPS from the intake job, which with no NAT reaches only Google's APIs through Private Google Access."
}

# ---------------------------------------------------------------------------
# The job.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "plugin_intake" {
  count = local.plugin_intake_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = local.plugin_intake_name

  labels = merge(local.labels, { component = "plugins" })

  deletion_protection = var.deletion_protection

  template {
    # swarm-api starts one execution per intake, naming its intake id.
    task_count = 1

    template {
      service_account = local.plugin_intake_email
      # A failed intake is reported, never retried on the same bytes.
      max_retries = 0
      timeout     = "600s"

      vpc_access {
        egress = "ALL_TRAFFIC"

        network_interfaces {
          network    = google_compute_network.plugin_intake.id
          subnetwork = google_compute_subnetwork.plugin_intake.id
          tags       = [local.plugin_intake_tag]
        }
      }

      containers {
        image = local.plugin_intake_image

        # Memory and disk are one (the in-memory filesystem): 500 MiB unpacked
        # beside a 100 MiB archive and the normalised tar.
        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        # Names and numbers only: no secret, no credential, no database.
        env {
          name  = "PLUGINS_BUCKET"
          value = google_storage_bucket.plugins.name
        }

        dynamic "env" {
          for_each = local.plugin_caps
          content {
            name  = env.key
            value = env.value
          }
        }
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
    data.google_service_account.plugin_intake,
    google_service_account_iam_member.plugin_intake_deployer_acts_as,
  ]
}

# swarm-api starts an execution with the intake id as its argument: run with
# overrides on THIS job, nothing else.
resource "google_cloud_run_v2_job_iam_member" "plugin_intake_api_runs" {
  count = local.plugin_intake_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.plugin_intake[0].name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = module.iam.service_account_members["swarm-api"]
}

output "plugins_bucket" {
  description = "The managed plugins' bucket (plugins.tf); swarm install's verify check and swarm doctor read it."
  value       = google_storage_bucket.plugins.name
}
