# Project-level role grants.
#
# Read this file as the answer to "what could this component do if it were
# compromised?". Two properties are deliberate:
#
#   * Every Firestore grant is conditioned to the `swarm` database. The project
#     is shared, and an unconditioned roles/datastore.user is read-write over
#     every other team's Firestore data in the project.
#   * swarm-api holds no compute permission at all. It accepts a runner profile
#     by NAME and writes a document. Everything that creates infrastructure is
#     downstream of the scheduler (invariant 10).

locals {
  sa_email = { for k, sa in google_service_account.platform : k => sa.email }

  sa_member = { for k, sa in google_service_account.platform : k => "serviceAccount:${sa.email}" }

  firestore_condition = var.scope_firestore_to_database ? [{
    title       = "swarm-database-only"
    description = "Restricts this grant to the swarm database; (default) belongs to other teams."
    expression  = "resource.type == \"datastore.googleapis.com/Database\" && resource.name == \"projects/${var.project_id}/databases/${var.firestore_database}\""
  }] : []

  # Observability roles every component needs and none of which grant data access.
  telemetry_roles = [
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ]

  # account key -> list of unconditioned project roles.
  plain_roles = merge(
    {
      for account in keys(local.platform_accounts) :
      account => local.telemetry_roles
    },
    {
      "swarm-api" = concat(local.telemetry_roles, [
        # Required to send `x-goog-user-project` on Cloud Identity calls. Without
        # it group membership checks fail 403 SERVICE_DISABLED naming gcloud's
        # shared client project, which reads like a permissions problem and is
        # not (CONTRACT.md, verified operational constraint).
        "roles/serviceusage.serviceUsageConsumer",
      ])
      "swarm-scheduler" = concat(local.telemetry_roles, [
        google_project_iam_custom_role.job_dispatcher.id,
      ], var.gke_enabled ? [google_project_iam_custom_role.gke_dispatcher[0].id] : [])
      "swarm-quota-broker" = concat(local.telemetry_roles, [
        # Reads its own metrics to drive the adaptive target. No write access to
        # anything outside Firestore.
        "roles/monitoring.viewer",
      ])
      "swarm-reconciler" = concat(local.telemetry_roles, [
        google_project_iam_custom_role.job_reaper.id,
        "roles/monitoring.viewer",
      ], var.gke_enabled ? [google_project_iam_custom_role.gke_reaper[0].id] : [])
    }
  )

  plain_bindings = {
    for pair in flatten([
      for account, roles in local.plain_roles : [
        for role in roles : {
          key     = "${account}:${role}"
          account = account
          role    = role
        }
      ]
    ]) : pair.key => pair
  }

  # Every platform component reads and writes control-plane state.
  firestore_accounts = keys(local.platform_accounts)
}

resource "google_project_iam_member" "plain" {
  for_each = local.plain_bindings

  project = var.project_id
  role    = each.value.role
  member  = local.sa_member[each.value.account]
}

resource "google_project_iam_member" "firestore" {
  for_each = toset(local.firestore_accounts)

  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.sa_member[each.value]

  dynamic "condition" {
    for_each = local.firestore_condition
    content {
      title       = condition.value.title
      description = condition.value.description
      expression  = condition.value.expression
    }
  }
}

# --------------------------------------------------------------------------
# Artifact bucket access for the control plane.
# --------------------------------------------------------------------------

# The API serves result artifacts back to callers, so it reads objects. It
# cannot write or delete them: the worker produces artifacts, the API only
# hands them over.
resource "google_storage_bucket_iam_member" "api_reader" {
  bucket = var.artifact_bucket
  role   = "roles/storage.objectViewer"
  member = local.sa_member["swarm-api"]
}

# The reconciler cleans up artifacts belonging to tasks that were abandoned
# before any lifecycle rule would reach them.
resource "google_storage_bucket_iam_member" "reconciler_admin" {
  bucket = var.artifact_bucket
  role   = "roles/storage.objectAdmin"
  member = local.sa_member["swarm-reconciler"]
}
