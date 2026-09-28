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

  # DOES NOT RESTRICT DOCUMENT ACCESS. Verified against a live deployment on
  # 2026-09-16: with this condition attached, every control-plane service was
  # denied Firestore reads and writes and /readyz reported
  # "firestore unavailable: PermissionDenied".
  #
  # IAM Conditions are not evaluated on the Firestore DATA plane. They govern
  # administrative operations (creating a database, indexes, backups) only, so a
  # condition here either denies the data plane outright, as it did, or -- once
  # granted unconditionally -- constrains nothing about which database the
  # identity can read. Firestore Security Rules are the documented alternative
  # and do NOT apply either: server SDKs using admin credentials bypass Rules
  # entirely, and every component here is a server SDK.
  #
  # So this defaults OFF, and the boundary is stated honestly rather than
  # claimed falsely: a swarm identity can reach ANY Firestore database in this
  # project. Today that is only `swarm`, so nothing else is exposed. ANYONE
  # CREATING A SECOND DATABASE IN saga-agents-staging MUST KNOW THIS -- the real
  # fix is to move Firestore into its own project, where project-level IAM is
  # the database boundary. See docs/security.md.
  #
  # What DOES still hold is the SHAPE of the access: tenant workers get the
  # narrowed swarmTenantWorkerFirestore role, which omits entities.delete and
  # entities.list, so a hostile worker can neither enumerate nor destroy
  # another tenant's documents even though it shares the database scope.
  firestore_condition = var.scope_firestore_to_database ? [{
    title       = "swarm-database-only-admin-ops"
    description = "Admin-plane only. Firestore does NOT evaluate IAM conditions for document reads or writes."
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
    job_dispatcher = "projects/${var.project_id}/roles/${local.custom_role_ids.job_dispatcher}"
    job_reaper     = "projects/${var.project_id}/roles/${local.custom_role_ids.job_reaper}"
    secret_lister  = "projects/${var.project_id}/roles/${local.custom_role_ids.secret_lister}"
    gke_dispatcher = var.gke_enabled ? "projects/${var.project_id}/roles/${local.custom_role_ids.gke_dispatcher}" : ""
    gke_reaper     = var.gke_enabled ? "projects/${var.project_id}/roles/${local.custom_role_ids.gke_reaper}" : ""
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
        # Discovers which tenants hold a subscription credential. Metadata only;
        # the ability to READ one is granted per-secret by the secret_manager
        # module, and only on the refresh secrets.
        local.custom_roles.secret_lister,
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

  # Explicit, because the role strings above are now built from configuration
  # rather than read off the resources. That is what keeps the for_each keys
  # known, and it also removes the implicit edge -- without this, a grant can be
  # attempted before the custom role it names exists.
  depends_on = [
    google_project_iam_custom_role.job_dispatcher,
    google_project_iam_custom_role.job_reaper,
    google_project_iam_custom_role.secret_lister,
    google_project_iam_custom_role.gke_dispatcher,
    google_project_iam_custom_role.gke_reaper,
  ]
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

  # Same reason as the `plain` grants: the role string is configuration now, so
  # the ordering edge has to be stated rather than inferred.
  depends_on = [
    google_project_iam_custom_role.gke_dispatcher,
    google_project_iam_custom_role.gke_reaper,
  ]

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

# --------------------------------------------------------------------------
# Signing as itself, for domain-wide delegation without a key.
# --------------------------------------------------------------------------

# Cloud Identity's Groups API does not authorize through GCP IAM, so swarm-api
# must ACT AS a real Workspace user to read a group at all. The usual way to do
# that -- `credentials.with_subject(user)` -- works only on credentials loaded
# from a service-account KEY FILE, and this repository refuses to hold one.
#
# The keyless route is to build the assertion and have Google sign it:
# `iamcredentials ...:signJwt`. That call requires
# roles/iam.serviceAccountTokenCreator ON THE SERVICE ACCOUNT, GRANTED TO THAT
# SAME SERVICE ACCOUNT. It reads like a tautology and is not: being an identity
# does not imply permission to mint signed assertions as it, and without this
# binding signJwt returns 403 while every group lookup fails with Cloud
# Identity's Error(2028) -- which looks exactly like the missing-role problem
# that delegation was introduced to solve. That misreading cost a live outage
# on 2026-09-20, so the binding lives next to the reason.
#
# It grants no new reach. The service account can already act as itself by
# existing; this only lets it say so in a form Google will sign. The delegation
# it then performs is bounded by the Admin console grant, which is scoped to
# cloud-identity.groups.readonly.
resource "google_service_account_iam_member" "api_signs_as_itself" {
  service_account_id = google_service_account.platform["swarm-api"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.sa_member["swarm-api"]
}
