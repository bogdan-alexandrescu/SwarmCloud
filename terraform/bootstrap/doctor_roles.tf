# ---------------------------------------------------------------------------
# swarm-doctor's project roles: READ-ONLY (DAYONE-TF-2; D27, plan §1.2).
# ---------------------------------------------------------------------------
#
# The hourly doctor (terraform/infra/doctor.tf) re-runs the install's checks
# against the live deployment. It reads; it never repairs -- the fix verb is
# `swarm install --repair <check>`, run by a person with the install's lease
# and a typed confirmation (DAYONE-DOCTOR-1). So every role here is a viewer:
#
#   * roles/viewer: get/list across the project's services;
#   * roles/iam.securityReviewer: every IAM policy, to tell a missing grant
#     from one still propagating (lessons 19, 48);
#   * roles/secretmanager.viewer: that a secret and its versions EXIST, never
#     their payload (secretAccessor is not here);
#   * roles/container.clusterViewer: the swarm cluster's endpoint, conditioned
#     to that cluster exactly as swarm-verify's is (verify_cluster_read.tf), so
#     it cannot reach the other team's agents-staging in a shared project.
#
# Granted here, by the owner's bootstrap apply, never by CI: CI's scoped
# projectIamAdmin may grant only the roles on deployer_grantable_project_roles
# (deployer_conditions.tf), and none of these is on it.
#
# CHECKED, NOT TRUSTED. tests/unit/scripts/test_terraform_day_one_edge.py reads
# each role's permissions from doctor-role-permissions.json and fails on any
# that ends in create, update, delete, setIamPolicy or add. roles/viewer holds
# the fourteen listed below; each is a session or a self-review, not a write to
# a resource the platform keeps, and each is named so that the list cannot
# grow silently. Whether the doctor keeps roles/viewer with them or trades it
# for narrower viewers is asked in the TF-2 PR.

locals {
  doctor_project_roles = toset([
    "roles/viewer",
    "roles/iam.securityReviewer",
    "roles/secretmanager.viewer",
  ])

  # roles/viewer's write-shaped permissions (doctor-role-permissions.json,
  # 2026-10-29): query jobs and read sessions billed to the project, Kubernetes
  # self-reviews, a trace task, a Gemini feedback/investigation object.
  # tflint-ignore: terraform_unused_declarations
  # A record for the reviewer, not an input: test_doctor_roles_are_read_only holds the same list.
  doctor_viewer_write_shaped = [
    "bigquery.jobs.create",
    "bigquery.readsessions.create",
    "bigquery.readsessions.update",
    "bigquery.tables.createSnapshot",
    "chronicle.ais.createFeedback",
    "cloudaicompanion.topics.create",
    "cloudsql.instances.createBackupDrBackup",
    "cloudtrace.tasks.create",
    "container.selfSubjectAccessReviews.create",
    "container.selfSubjectRulesReviews.create",
    "container.tokenReviews.create",
    "geminicloudassist.investigations.create",
    "spanner.sessions.create",
    "spanner.sessions.delete",
  ]
}

resource "google_project_iam_member" "doctor_read_only" {
  # checkov:skip=CKV_GCP_117:roles/viewer is the plan's read-only doctor role (DAYONE-TF-2, D27); its write-shaped permissions are listed above and held by test_doctor_roles_are_read_only; narrower viewers are asked of the owner in the TF-2 PR.
  for_each = local.doctor_project_roles

  project = var.project_id
  role    = each.value
  member  = google_service_account.doctor.member
}

resource "google_project_iam_member" "doctor_views_swarm_cluster" {
  project = var.project_id
  role    = "roles/container.clusterViewer"
  member  = google_service_account.doctor.member

  condition {
    title       = "swarm-cluster-only"
    description = "managed-by=swarm-terraform; the doctor reads the swarm Autopilot cluster's endpoint and CA only. Owner-applied: CI never grants it."
    expression = join(" || ", [
      "resource.name == \"${local.verify_swarm_cluster}\"",
      "resource.name.startsWith(\"${local.verify_swarm_cluster}/\")",
    ])
  }
}
