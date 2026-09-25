# Let a developer's OWN sign-in through IAP: the programmatic-client allowlist.
#
# OWNER DECISION 2026-09-25. The `sc` plugin is a deployment-agnostic client,
# and any developer must be able to act AS THEMSELVES through IAP with ONE OAuth
# client per deployment and no per-developer setup. Until now the only way a
# laptop got past this deployment's IAP was impersonating a service account,
# because a gcloud user token is refused 401 with IAP error code 900 (measured
# 2026-09-24).
#
# WHY THAT HAPPENS, AND WHAT GOOGLE SAYS TO DO ABOUT IT. modules/frontend turns
# IAP on with no oauth2_client_id, so IAP uses a Google-managed OAuth client:
#
#   "Google-managed OAuth clients cannot programmatically access IAP-protected
#    applications. However, IAP-protected applications that use the Google-
#    managed OAuth client can still be accessed programmatically using a
#    separate OAuth client configured through the programmatic_clients setting
#    or a service account JWT."
#                    -- cloud.google.com/iap/docs/custom-oauth-configuration
#
# So one Desktop app OAuth client is created once, by hand (the IAP OAuth Admin
# API that could create clients is deprecated, and a Desktop client is not an
# IAP client anyway -- docs/runbooks/iap-desktop-client.md has the exact
# console steps), and its client ID is allowlisted here. Each developer then
# runs `sc login`, which mints an ID token for that client; IAP admits it and
# forwards the developer's identity to swarm-api.
#
# WHY PER BACKEND SERVICE, AND WHY THIS ROOT
# ------------------------------------------
# IAP settings can be set at the organisation, folder, project, `iap_web`,
# `compute` or single-SERVICE level. saga-agents-staging is shared: nine of its
# ten IAP backends are another team's, their Keycloak and ArgoCD among them.
# Anything above the service level would change what those accept. The service
# level is documented for exactly this -- `gcloud iap settings set
# --resource-type=compute --service=BACKEND_SERVICE_NAME` in
# cloud.google.com/iap/docs/deprecations/migrate-oauth-client -- and it is the
# only level written here: one resource per backend in var.frontend_iap_backends,
# whose validation already refuses any backend not prefixed `swarm`.
#
# It lives in bootstrap, next to the accessor list (wif.tf, frontend_accessors),
# and NOT in terraform/infra, for the same reason that list moved here on
# 2026-09-24: the release's deployer holds no IAP role at all, and every way of
# giving it one was measured and refused (modules/frontend/main.tf, "Who may
# pass IAP"). Putting this in infra would fail every release at plan.
#
# WHO CAN APPLY IT -- MEASURED, AND NOT YET TRUE
# ----------------------------------------------
# Writing IAP settings takes iap.webServices.updateSettings (and reading them,
# which every plan does, iap.webServices.getSettings). Those are in
# roles/iap.settingsAdmin and NOT in roles/iap.admin (`gcloud iam roles
# describe`, 2026-09-25). The owner holds roles/iap.admin on the project and
# `gcloud iap settings get --resource-type=compute --service=swarm-ui-backend`
# answers PERMISSION_DENIED for iap.webServices.getSettings. The runbook says
# how to get the permission scoped to these backends before applying; until
# then leave var.frontend_iap_programmatic_clients empty, which creates nothing.
#
# NO LABELS: google_iap_settings has no labels attribute in provider 6.50.0 (its
# arguments are name, access_settings and application_settings). It is scoped by
# its NAME instead, which the tests pin to `.../compute/services/swarm*`.
#
# Tested in tests/terraform/iap_programmatic_clients.tftest.hcl.

variable "frontend_iap_programmatic_clients" {
  description = <<-EOT
    Desktop app OAuth client IDs that IAP on the platform's own backends
    (var.frontend_iap_backends) admits for programmatic access -- the client
    `sc login` signs developers in with. One per deployment is the intent.

    Empty, the default, writes no IAP setting at all. See
    docs/runbooks/iap-desktop-client.md for creating the client and for the
    permission the applier needs.
  EOT
  type        = list(string)
  default     = []

  # A client ID, never its secret and never a URL. The secret starts GOCSPX-
  # and must not be in Terraform at all (CLAUDE.md: never put secret material
  # in Terraform -- state is readable by several people).
  validation {
    condition = alltrue([
      for c in var.frontend_iap_programmatic_clients :
      can(regex("^[0-9]+-[a-z0-9]+\\.apps\\.googleusercontent\\.com$", c))
    ])
    error_message = "each entry must be an OAuth client ID of the form <number>-<id>.apps.googleusercontent.com -- never a client secret (GOCSPX-...) or a URL."
  }
}

locals {
  iap_programmatic_backends = length(var.frontend_iap_programmatic_clients) > 0 ? toset(var.frontend_iap_backends) : toset([])
}

# The IAP resource name is keyed by project NUMBER, as the provider's own
# example builds it.
data "google_project" "iap_programmatic" {
  count      = length(local.iap_programmatic_backends) > 0 ? 1 : 0
  project_id = var.project_id
}

resource "google_iap_settings" "frontend_programmatic_clients" {
  for_each = local.iap_programmatic_backends

  # The backend-SERVICE level, and only that. The name is built from the
  # backend this root already looked up for its accessors, so a backend that
  # does not exist fails the plan at the data source rather than here.
  name = "projects/${data.google_project.iap_programmatic[0].number}/iap_web/compute/services/${data.google_compute_backend_service.frontend_iap[each.key].name}"

  access_settings {
    oauth_settings {
      programmatic_clients = var.frontend_iap_programmatic_clients
    }
  }
}
