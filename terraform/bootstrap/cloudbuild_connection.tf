# What Google's Cloud Build service agent needs to hold the second-generation
# GitHub connection the swarm-workspace-apply trigger builds from
# (docs/workspaces.md §10, "Connect this repository to Cloud Build").
#
# Creating the connection (`gcloud builds connections create github
# swarm-github --region=us-central1`) makes the agent store the GitHub
# authorization in a Secret Manager secret it creates, and then set that
# secret's IAM policy. Without both, the create fails with "could not assert
# Secret Manager permissions" (read 2026-10-10, operation
# operation-1791598789708-...). Google's own instruction is roles/secretmanager.admin
# on the project. This project is SHARED and holds another team's secrets, and
# this repository keeps secretmanager.admin away from every identity
# (variables.tf), so the agent gets exactly the two powers, split by what an
# IAM Condition can actually see (owner decision 2026-10-10):
#
#   * secretmanager.secrets.create, UNCONDITIONED. A create is authorised on
#     the parent project, where resource.name is the project, not the secret
#     being made, so a name condition here could never match. Creating a secret
#     reads and changes no existing secret.
#   * secretmanager.secrets.setIamPolicy and getIamPolicy, CONDITIONED to
#     secrets whose name starts with "<connection>-github-oauthtoken-", the
#     name the agent gives the connection's secret, so it admits that secret
#     and no other, not even swarm-github-app-*. Secret Manager does expose
#     resource.name to IAM Conditions, as projects/<NUMBER>/secrets/<id>
#     (deployer_conditions.tf, forge_user_slots.tf).
#
# WHAT A MOCK PROVIDER CANNOT PROVE: that the agent asks for nothing beyond
# these. The proof is the connection create succeeding after `make bootstrap`.
# If it asks for more, the error names the permission, and the answer is to add
# that one permission here, not to widen the grant to admin.
#
# APPLIED BY THE OWNER (`make bootstrap`). A custom role and an IAM member have
# no labels; the role says managed-by=swarm-terraform in its description.
locals {
  # The connection's name, and so the prefix of the secret the agent creates.
  # A literal: widening it is a reviewed change, not a tfvars edit.
  cloudbuild_connection_name = "swarm-github"
  # The exact prefix of the secret Google's agent creates for the connection,
  # "<connection>-github-oauthtoken-<hex>". NOT "<connection>-": that also
  # matched swarm-github-app-client-secret and swarm-github-app-private-key,
  # the platform's own GitHub App credentials (found 2026-10-10 right after the
  # first apply, narrowed the same night). A prefix here must never be a
  # prefix of another secret's name: check `gcloud secrets list` before widening it.
  cloudbuild_connection_secret_prefix = "${local.cloudbuild_connection_name}-github-oauthtoken-"
  cloudbuild_agent_member             = local.wif_enabled == 1 ? "serviceAccount:service-${data.google_project.this[0].number}@gcp-sa-cloudbuild.iam.gserviceaccount.com" : ""
}

resource "google_project_iam_custom_role" "cloudbuild_connection_secret_create" {
  count = local.wif_enabled

  project     = var.project_id
  role_id     = "swarmCloudBuildConnectionSecretCreate"
  title       = "Swarm Cloud Build connection: create its secret"
  description = "managed-by=swarm-terraform; lets Google's Cloud Build agent create the secret a GitHub connection stores its authorization in. Create only: no read, update, delete or version access (terraform/bootstrap/cloudbuild_connection.tf)."
  stage       = "GA"
  permissions = ["secretmanager.secrets.create"]
}

resource "google_project_iam_custom_role" "cloudbuild_connection_secret_policy" {
  count = local.wif_enabled

  project     = var.project_id
  role_id     = "swarmCloudBuildConnectionSecretPolicy"
  title       = "Swarm Cloud Build connection: its secret's policy"
  description = "managed-by=swarm-terraform; lets Google's Cloud Build agent read and set the IAM policy of the connection's own secret, granted with a condition on the secret's name (terraform/bootstrap/cloudbuild_connection.tf)."
  stage       = "GA"
  permissions = [
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
  ]
}

resource "google_project_iam_member" "cloudbuild_connection_secret_create" {
  count = local.wif_enabled

  project = var.project_id
  role    = google_project_iam_custom_role.cloudbuild_connection_secret_create[0].id
  member  = local.cloudbuild_agent_member
}

resource "google_project_iam_member" "cloudbuild_connection_secret_policy" {
  count = local.wif_enabled

  project = var.project_id
  role    = google_project_iam_custom_role.cloudbuild_connection_secret_policy[0].id
  member  = local.cloudbuild_agent_member

  condition {
    title       = "the ${local.cloudbuild_connection_name} connection's own secret"
    description = "Secrets the Cloud Build agent created for the ${local.cloudbuild_connection_name} connection, and no other."
    expression  = "resource.type == \"secretmanager.googleapis.com/Secret\" && resource.name.startsWith(\"projects/${data.google_project.this[0].number}/secrets/${local.cloudbuild_connection_secret_prefix}\")"
  }
}
