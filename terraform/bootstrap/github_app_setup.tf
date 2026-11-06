# ---------------------------------------------------------------------------
# Admin > Setup's GitHub App step, and the engine's two calls through IAP
# (DAYONE-TF-2; plan R3 and §5.3, review findings 2 and review-fix finding 2).
# ---------------------------------------------------------------------------
#
# 1. swarm-api STORES THE APP'S THREE SECRETS. Under D26 the console registers
#    the GitHub App: swarm-api performs the manifest code exchange and writes
#    what it returns -- the client secret, the private key and the webhook
#    secret -- into the three swarm-github-app-* containers that terraform/
#    infra creates under enable_console_app_setup. So swarm-api holds
#    secretVersionAdder (add a version, nothing else) and secretAccessor on
#    EXACTLY THOSE THREE, by name: a project grant conditioned on the secret's
#    resource name, so it exists before the containers do (infra pass 2) and
#    reaches no other secret. Not under swarm-tenant- or swarm-account-, the
#    broker's prefixes; no wildcard.
#
#    Granted here by the owner's apply, not by CI: secretVersionAdder is not on
#    deployer_grantable_project_roles, and must not be.
#
# 2. THE RELEASE DEPLOYER MAY PASS IAP ON THE API BACKEND ONLY. The install
#    engine runs as swarm-tf-deployer and calls swarm-api's two engine routes
#    (the install record and the self-check) through the front door. It holds
#    roles/iap.httpsResourceAccessor on "<name_prefix>-ui-backend" -- the API's
#    backend in modules/frontend -- and NEVER on the console's UI backend: the
#    deployer has no business in a browser session. Only once that backend is
#    listed in frontend_iap_backends (bootstrap pass 2, after infra made it),
#    looked up by name as frontend_accessors does (wif.tf).

locals {
  # The three containers, by id: modules/secret_manager's two
  # (github_app_slot_words) and terraform/infra/github_hooks.tf's webhook
  # secret. tests/unit/scripts/test_terraform_day_one_edge.py holds this list
  # to exactly these three names.
  github_app_setup_secret_ids = [
    "swarm-github-app-client-secret",
    "swarm-github-app-private-key",
    "swarm-github-app-webhook-secret",
  ]

  github_app_setup_roles = toset([
    "roles/secretmanager.secretVersionAdder",
    "roles/secretmanager.secretAccessor",
  ])

  # The API backend modules/frontend names "${name_prefix}-ui-backend" (its
  # local.name is "${name_prefix}-ui"); the UI's backend is never named here.
  deployer_api_backend = "${var.name_prefix}-ui-backend"
}

data "google_project" "github_app_setup" {
  project_id = var.project_id
}

resource "google_project_iam_member" "api_github_app_secrets" {
  for_each = local.github_app_setup_roles

  project = var.project_id
  role    = each.value
  member  = google_service_account.platform["swarm-api"].member

  condition {
    title       = "github-app-secrets-only"
    description = "managed-by=swarm-terraform; the three swarm-github-app-* secrets the console's App setup writes (D26). Owner-applied."
    # A secret's resource name is projects/<NUMBER>/secrets/<id>, a version's
    # that plus /versions/<v>. Exact name or a path under it, never a prefix
    # match that a longer secret id could share.
    expression = join(" || ", flatten([
      for id in local.github_app_setup_secret_ids : [
        "resource.name == \"projects/${data.google_project.github_app_setup.number}/secrets/${id}\"",
        "resource.name.startsWith(\"projects/${data.google_project.github_app_setup.number}/secrets/${id}/\")",
      ]
    ]))
  }
}

resource "google_iap_web_backend_service_iam_member" "deployer_api_accessor" {
  count = local.deployer_on && contains(var.frontend_iap_backends, local.deployer_api_backend) ? 1 : 0

  project             = var.project_id
  web_backend_service = data.google_compute_backend_service.frontend_iap[local.deployer_api_backend].name
  role                = "roles/iap.httpsResourceAccessor"
  member              = "serviceAccount:${google_service_account.deployer[0].email}"
}
