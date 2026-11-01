# ---------------------------------------------------------------------------
# Admin > Setup's Sign-in client step (issue #2964).
# ---------------------------------------------------------------------------
#
# swarm-api STORES THE DESKTOP CLIENT'S SECRET. No Google API creates the OAuth
# brand or a Desktop client since the IAP OAuth Admin API shut down on
# 2026-03-19, so the person makes the client in Google's console and gives its
# id and secret to the console's Sign-in client step; swarm-api writes the
# secret as a new version of swarm-iap-desktop-client-secret, the container the
# installer's edge.desktop-client step creates. So swarm-api holds
# secretVersionAdder (add a version, nothing else) on EXACTLY THAT ONE secret,
# by name: a project grant conditioned on the secret's resource name, so it
# exists before the container does and reaches no other secret. No accessor:
# swarm-api never reads the secret back (swarm-discovery's accessor, behind
# discovery_serves_client_secret, is terraform/infra/discovery.tf's).
#
# Granted here by the owner's apply, not by CI: secretVersionAdder is not on
# deployer_grantable_project_roles, and must not be.

locals {
  sign_in_client_secret_id = "swarm-iap-desktop-client-secret"
}

resource "google_project_iam_member" "api_sign_in_client_secret" {
  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = google_service_account.platform["swarm-api"].member

  condition {
    title       = "sign-in-client-secret-only"
    description = "managed-by=swarm-terraform; swarm-iap-desktop-client-secret, which the console's Sign-in client step writes (#2964). Owner-applied."
    # Exact name or a path under it, never a prefix match that a longer
    # secret id could share (github_app_setup.tf's form).
    expression = join(" || ", [
      "resource.name == \"projects/${data.google_project.github_app_setup.number}/secrets/${local.sign_in_client_secret_id}\"",
      "resource.name.startsWith(\"projects/${data.google_project.github_app_setup.number}/secrets/${local.sign_in_client_secret_id}/\")",
    ])
  }
}
