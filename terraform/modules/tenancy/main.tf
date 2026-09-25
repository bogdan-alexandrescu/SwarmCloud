# Per-tenant identity and isolation.
#
# CONTRACT.md invariant 9 in one module: own GSA, own secrets, own GCS prefix,
# own namespace. Everything a tenant can reach is reachable only by that
# tenant's service account.
#
# What a tenant worker CANNOT do is the more important list, and it is enforced
# by absence -- there is no run.*, container.*, compute.* or iam.* grant for a
# worker identity anywhere in this repository. A worker that is talked into
# running attacker-controlled code still cannot create infrastructure, because
# the identity it runs under has never been able to (invariant 10).

locals {
  owner_marker = "managed-by=${lookup(var.labels, "managed-by", "swarm-terraform")}"

  tenant_ids = keys(var.tenants)

  sa_account_id = { for t, _ in var.tenants : t => "swarm-agent-worker-${t}" }

  namespace = { for t, _ in var.tenants : t => "${var.namespace_prefix}${t}" }

  gcs_prefix = { for t, _ in var.tenants : t => "tenants/${t}/" }

  # (tenant, dispatcher) pairs for the actAs grant.
  #
  # Keyed by the dispatcher's LABEL, never by its member string: the member is a
  # service account email that is unknown until apply, and a for_each whose keys
  # are unknown cannot be planned -- the first plan on a fresh project fails
  # outright. Keys stay static; only the value is unknown.
  act_as_grants = {
    for pair in flatten([
      for t in local.tenant_ids : [
        for label, m in var.dispatcher_members : {
          key    = "${t}:${label}"
          tenant = t
          member = m
        }
      ]
    ]) : pair.key => pair
  }

  telemetry_roles = [
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ]

  telemetry_grants = {
    for pair in flatten([
      for t in local.tenant_ids : [
        for role in local.telemetry_roles : {
          key    = "${t}:${role}"
          tenant = t
          role   = role
        }
      ]
    ]) : pair.key => pair
  }

  # Workload Identity bindings only make sense once the cluster exists.
  wi_tenants = var.workload_identity_pool == "" ? {} : var.tenants
}

resource "google_service_account" "worker" {
  for_each = var.tenants

  project      = var.project_id
  account_id   = local.sa_account_id[each.key]
  display_name = "Swarm agent worker (${each.key})"
  description  = "${local.owner_marker}; tenant ${each.key} (${each.value.principal}). No infrastructure-creation permissions."
}

# --------------------------------------------------------------------------
# Control-plane state.
#
# Read this block together with its limits, because they are load-bearing.
#
# Firestore IAM has NO collection- or document-level granularity: the smallest
# resource a binding or a condition can name is the database. Every swarm
# identity therefore shares one authorization scope over the `swarm` database,
# and a worker running attacker-controlled code -- which is the normal case, not
# the exceptional one -- is inside that scope. The condition below keeps it out
# of `(default)` and every other database in this shared project.
#
# CORRECTION, verified live 2026-09-16: that is FALSE. Firestore does not
# evaluate IAM Conditions on the data plane -- only for administrative
# operations -- so the condition neither restricted anything nor was silently
# ignored: it DENIED document access outright, and every control-plane service
# came up with /readyz reporting "firestore unavailable: PermissionDenied".
# The condition now defaults off (see modules/iam/variables.tf), which means a
# swarm identity can reach any Firestore database in this project. Today `swarm`
# is the only one. The real boundary is a separate project.
#
# It also cannot keep tenant A's worker out of tenant B's documents, and no
# condition can -- which is why the SHAPE of the grant below carries the weight.
#
# What IS available at this layer is the shape of the access, so the role is
# built rather than borrowed: swarmTenantWorkerFirestore, which drops
# entities.delete and entities.list from what roles/datastore.user grants.
# terraform/bootstrap/platform_roles.tf defines it, with the reasoning for each
# of its five permissions, since #79 (owner decision 2026-09-25): CI no longer
# holds roles/iam.roleAdmin, so no role can be defined in a module CI applies.
#
# The residual is real and is written down here rather than implied: a worker
# still holds create/get/update across the database, so it can read a document
# whose id it can guess (`pools/global`, `tenants/<id>`) and write one. Closing
# that needs the worker off direct Firestore access entirely -- reaching state
# through a control-plane service that scopes by caller identity -- or one
# database per tenant, and neither is expressible in IAM.
#
# The role names below are plain strings from ../custom_role_ids, the one
# spelling this module and terraform/bootstrap share. A string, not a resource
# reference, so it is known at plan and nothing here reads a role: CI holds no
# iam.roles.* permission.
module "custom_role_ids" {
  source = "../custom_role_ids"

  project_id = var.project_id
}

resource "google_project_iam_member" "worker_firestore" {
  for_each = var.tenants

  project = var.project_id
  role    = module.custom_role_ids.names.worker_firestore
  member  = "serviceAccount:${google_service_account.worker[each.key].email}"

  dynamic "condition" {
    for_each = var.scope_firestore_to_database ? [1] : []
    content {
      title       = "swarm-database-only"
      description = "Restricts this grant to the swarm database."
      expression  = "resource.type == \"datastore.googleapis.com/Database\" && resource.name == \"projects/${var.project_id}/databases/${var.firestore_database}\""
    }
  }
}

resource "google_project_iam_member" "worker_telemetry" {
  for_each = local.telemetry_grants

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.worker[each.value.tenant].email}"
}

# --------------------------------------------------------------------------
# Object storage: one prefix per tenant, enforced by IAM rather than by habit.
# --------------------------------------------------------------------------

# Cloud Storage FUSE and the client libraries both need the bucket's own
# metadata, and a prefix condition can never match the bucket resource name.
# Granting legacyBucketReader instead would hand over objects.list across the
# WHOLE bucket, which is exactly the isolation this module exists to keep. So
# the grant is swarmBucketMetadataReader, storage.buckets.get alone, defined in
# terraform/bootstrap/platform_roles.tf (#79).
resource "google_storage_bucket_iam_member" "worker_bucket_metadata" {
  for_each = var.tenants

  bucket = var.artifact_bucket
  role   = module.custom_role_ids.names.bucket_metadata_reader
  member = "serviceAccount:${google_service_account.worker[each.key].email}"
}

resource "google_storage_bucket_iam_member" "worker_objects" {
  for_each = var.tenants

  bucket = var.artifact_bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.worker[each.key].email}"

  condition {
    title       = "swarm-tenant-prefix-${each.key}"
    description = "Objects under tenants/${each.key}/ only, including prefix-scoped listing."

    # Two clauses, both required. The first covers get/create/delete on an
    # object path. The second covers LIST: a list request has no object name, so
    # the only way to scope it is the objectListPrefix attribute -- without it
    # the worker could enumerate every tenant's object names.
    expression = join(" || ", [
      "resource.name.startsWith(\"projects/_/buckets/${var.artifact_bucket}/objects/tenants/${each.key}/\")",
      "api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"tenants/${each.key}/\")",
    ])
  }
}

# --------------------------------------------------------------------------
# Dispatch: actAs, scoped to the individual service account.
# --------------------------------------------------------------------------

resource "google_service_account_iam_member" "act_as" {
  for_each = local.act_as_grants

  service_account_id = google_service_account.worker[each.value.tenant].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value.member
}

# --------------------------------------------------------------------------
# Workload Identity: the tenant's KSA on Autopilot assumes the tenant's GSA.
# --------------------------------------------------------------------------

resource "google_service_account_iam_member" "workload_identity" {
  for_each = local.wi_tenants

  service_account_id = google_service_account.worker[each.key].name
  role               = "roles/iam.workloadIdentityUser"

  # Namespace is part of the principal, so a Pod in another tenant's namespace
  # cannot impersonate this GSA even if it guesses the KSA name.
  member = "serviceAccount:${var.workload_identity_pool}[${local.namespace[each.key]}/${each.value.ksa_name}]"
}
