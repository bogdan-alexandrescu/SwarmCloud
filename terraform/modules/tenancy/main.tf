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

  role_suffix = var.custom_role_suffix == "" ? "" : "_${var.custom_role_suffix}"

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
# of `(default)` and every other database in this shared project. It cannot keep
# tenant A's worker out of tenant B's documents, and no condition can.
#
# What IS available at this layer is the shape of the access, so the role is
# built rather than borrowed. roles/datastore.user grants entities.delete and
# entities.list on top of what a worker needs. Removing them removes the two
# capabilities that turn "can touch the shared database" into the worst version
# of itself:
#
#   entities.delete -- a hostile worker could delete another tenant's tasks,
#   leases and attempts, and the pool documents the whole platform admits
#   against. Nothing in the worker path deletes a document: agent_worker.control
#   and swarm_common.admission are get/set/update only.
#
#   entities.list -- queries. Without it, a document can only be fetched by an
#   id already known, and task, attempt and lease ids are `<prefix>_<20 hex>`.
#   So a hostile worker cannot ENUMERATE other tenants' work: no dumping every
#   prompt, repo URL and artifact path in the database. The worker path runs no
#   queries at all; every read is a document ref built from an id the dispatcher
#   handed this attempt (agent_worker.control, agent_worker.secrets.load_tenant).
#
# The residual is real and is written down here rather than implied: a worker
# still holds create/get/update across the database, so it can read a document
# whose id it can guess (`pools/global`, `tenants/<id>`) and write one. Closing
# that needs the worker off direct Firestore access entirely -- reaching state
# through a control-plane service that scopes by caller identity -- or one
# database per tenant, and neither is expressible in IAM.
resource "google_project_iam_custom_role" "worker_firestore" {
  project = var.project_id
  role_id = "swarmTenantWorkerFirestore${local.role_suffix}"
  title   = "Swarm Tenant Worker Firestore"

  description = "Read and write control-plane documents by id. No deletes, no queries."
  stage       = "GA"

  permissions = [
    # The client library resolves the named database before its first call.
    "datastore.databases.get",
    "resourcemanager.projects.get",
    # Document get / set / update, which is the entire worker data path.
    "datastore.entities.get",
    "datastore.entities.create",
    "datastore.entities.update",
  ]
}

resource "google_project_iam_member" "worker_firestore" {
  for_each = var.tenants

  project = var.project_id
  role    = google_project_iam_custom_role.worker_firestore.id
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
# WHOLE bucket, which is exactly the isolation this module exists to keep.
resource "google_project_iam_custom_role" "bucket_metadata_reader" {
  project = var.project_id
  role_id = "swarmBucketMetadataReader${local.role_suffix}"
  title   = "Swarm Bucket Metadata Reader"

  description = "storage.buckets.get only. Enough to mount, not enough to enumerate."
  stage       = "GA"

  permissions = ["storage.buckets.get"]
}

resource "google_storage_bucket_iam_member" "worker_bucket_metadata" {
  for_each = var.tenants

  bucket = var.artifact_bucket
  role   = google_project_iam_custom_role.bucket_metadata_reader.id
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
