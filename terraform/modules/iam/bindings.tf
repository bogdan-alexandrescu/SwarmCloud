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

  # Custom role names, built from the role_id this configuration sets rather than
  # read back from the resource's computed `id`.
  #
  # Both forms are the same string. The difference is WHEN it is known: `.id` is
  # computed and therefore unknown until apply, and a role name that is unknown
  # at plan time lands in the for_each keys below and makes the very first plan
  # fail. Referencing `.role_id` still creates the dependency edge, but the value
  # is already in the configuration, so the key is known.
  custom_roles = {
    job_dispatcher = "projects/${var.project_id}/roles/${google_project_iam_custom_role.job_dispatcher.role_id}"
    job_reaper     = "projects/${var.project_id}/roles/${google_project_iam_custom_role.job_reaper.role_id}"
    gke_dispatcher = var.gke_enabled ? "projects/${var.project_id}/roles/${google_project_iam_custom_role.gke_dispatcher[0].role_id}" : ""
    gke_reaper     = var.gke_enabled ? "projects/${var.project_id}/roles/${google_project_iam_custom_role.gke_reaper[0].role_id}" : ""
  }

  # account key -> list of unconditioned project roles. Every element is known at
  # plan time, which is what lets the bindings below be keyed by role.
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
        local.custom_roles.job_dispatcher,
      ])
      "swarm-quota-broker" = concat(local.telemetry_roles, [
        # Reads its own metrics to drive the adaptive target. No write access to
        # anything outside Firestore.
        "roles/monitoring.viewer",
      ])
      "swarm-reconciler" = concat(local.telemetry_roles, [
        local.custom_roles.job_reaper,
        "roles/monitoring.viewer",
      ])
    }
  )

  # The two container.* roles are held out of `plain_roles` on purpose: they are
  # the only project-level grants in this module whose scope would otherwise
  # cross into another team's infrastructure, so they get a condition and a
  # resource of their own rather than riding along unconditioned.
  gke_role_grants = var.gke_enabled ? {
    "swarm-scheduler"  = local.custom_roles.gke_dispatcher
    "swarm-reconciler" = local.custom_roles.gke_reaper
  } : {}

  swarm_cluster_name = var.gke_cluster_name == "" ? "" : "projects/${var.project_id}/locations/${var.gke_location}/clusters/${var.gke_cluster_name}"

  # startsWith rather than ==: a Kubernetes API request is authorized against a
  # resource name under the cluster (a namespace, a Job, a Pod), not against the
  # bare cluster name, so an equality test would refuse the very calls the
  # dispatcher exists to make.
  gke_condition = var.scope_gke_to_cluster && local.swarm_cluster_name != "" ? [{
    title       = "swarm-cluster-only"
    description = "Restricts this grant to the swarm Autopilot cluster; agents-staging belongs to another team."
    expression  = "resource.name.startsWith(\"${local.swarm_cluster_name}\")"
  }] : []

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

# GKE dispatch and reap, pinned to the swarm's own cluster.
#
# A project-level container.* binding covers every cluster in the project, and
# saga-agents-staging holds a live `agents-staging` cluster owned by another
# team. Unconditioned, swarm-scheduler could create Jobs in it and read its Pod
# logs, and swarm-reconciler could delete its Jobs and Pods.
#
# The condition below is the only layer that can scope this at the IAM level.
# IAM has no notion of a Kubernetes namespace, so confining the dispatcher to
# the `swarm-tenant-*` namespaces INSIDE the swarm cluster is Kubernetes RBAC's
# job and lives with the cluster manifests, not here.
resource "google_project_iam_member" "gke" {
  for_each = local.gke_role_grants

  project = var.project_id
  role    = each.value
  member  = local.sa_member[each.key]

  dynamic "condition" {
    for_each = local.gke_condition
    content {
      title       = condition.value.title
      description = condition.value.description
      expression  = condition.value.expression
    }
  }

  lifecycle {
    precondition {
      condition     = !var.scope_gke_to_cluster || var.gke_cluster_name != ""
      error_message = "gke_cluster_name must name the swarm cluster when scope_gke_to_cluster is on, or the container.* grants would cover every cluster in this shared project."
    }
  }
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
