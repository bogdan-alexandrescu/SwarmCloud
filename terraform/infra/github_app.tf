# The SwarmCloud GitHub App, the deployment side (docs/onboarding.md §3.4,
# #780 lane OB2; the registration itself is docs/runbooks/github-app.md).
#
# Owner decision D1 (2026-10-07): SwarmCloud acts as the user through a GitHub
# App's user access tokens, with a fine-grained PAT as the fallback and the
# tenant token kept. What this file wires:
#
#   * the App's NON-SECRET settings, as tfvars: app id, client id, slug. A
#     client id is public by design -- it is in every authorise URL a browser
#     follows -- so it may sit in a tfvars file in a public repository. The
#     client secret and the private key may not, and are not here.
#   * the App's two platform secret SLOTS (modules/secret_manager), empty,
#     readable by swarm-api alone. Values arrive by
#     `scripts/create-secrets.sh --github-app <slot> --stdin`.
#   * the refresher job, swarm-forge-refresh (modules/scheduler jobs.tf), off
#     until swarm-api serves its route.
#
# What this file does NOT wire, and where it is:
#
#   * the user slots' IAM -- swarm-api's create and version-add grants and
#     each tenant worker's read (D3, D7) -- is PROJECT-LEVEL Secret Manager
#     IAM, and terraform/bootstrap makes it (forge_user_slots.tf), not this
#     root. A project-level secretAccessor grant made here would put
#     roles/secretmanager.secretAccessor on the CI deployer's grantable list
#     (deployer_conditions.tf), and hasOnly() limits which roles CI grants,
#     never to whom: CI could then grant itself read of every secret in this
#     shared project, the other team's 63 included. The same reasoning moved
#     the broker's swarmSecretLister grant to bootstrap (#69).
#   * swarm-api's environment. Nothing reads the App yet (lane OB3), and a
#     Cloud Run revision that references a secret with no version FAILS TO
#     START, so the client secret is put on swarm-api's environment by OB3,
#     after the runbook's store step -- the order child_tasks.tf spells out.

variable "enable_github_app" {
  description = <<-EOT
    Declare the SwarmCloud GitHub App's platform secret slots
    (swarm-github-app-client-secret, swarm-github-app-private-key), readable
    by swarm-api alone. Creates no version and changes no running service.
  EOT
  type        = bool
  default     = false
}

variable "github_app_id" {
  description = "The GitHub App's numeric App ID, from its settings page (docs/runbooks/github-app.md). Not a secret. Empty until the App is registered."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^([0-9]+)?$", var.github_app_id))
    error_message = "github_app_id is the App's numeric ID, or empty."
  }
}

variable "github_app_client_id" {
  description = <<-EOT
    The GitHub App's client ID (`Iv1.` or `Iv23` followed by letters and
    digits), from its settings page. Public by design: it is in every
    authorise URL. The CLIENT SECRET is never a tfvars value -- it goes to
    Secret Manager by `scripts/create-secrets.sh --github-app client-secret
    --stdin`. Empty until the App is registered.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^(Iv[0-9A-Za-z.]{6,40})?$", var.github_app_client_id))
    error_message = "github_app_client_id is the App's client ID (Iv1.<hex> or Iv23<letters and digits>), or empty. If you were about to paste the client secret here: stop, it goes to Secret Manager by create-secrets.sh --github-app client-secret --stdin."
  }
}

variable "github_app_slug" {
  description = "The App's URL slug (github.com/apps/<slug>), used to build its install link. Not a secret. Empty until the App is registered."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^([a-z0-9][a-z0-9-]{0,33})?$", var.github_app_slug))
    error_message = "github_app_slug is the App's lower-case URL slug, or empty."
  }
}

variable "enable_forge_refresh" {
  description = <<-EOT
    Create swarm-forge-refresh, the 15-minute Cloud Scheduler sweep that
    refreshes GitHub user access tokens (decision D2). Set it only once
    swarm-api serves POST /v1/admin/forge/refresh (lane OB3): before that the
    job answers 404 every tick.
  EOT
  type        = bool
  default     = false
}

locals {
  # Where GitHub sends the browser after the user authorises: the console's
  # callback page, which posts {state, code} to swarm-api's exchange with the
  # user's own ID token (docs/onboarding.md §3.2). Registered on the App by
  # hand; derived here so the runbook's value and the deployment's agree.
  github_app_callback_url = var.frontend_hostname == "" ? "" : "https://${var.frontend_hostname}/onboarding/github/callback"
}

check "github_app_settings_are_complete" {
  assert {
    condition     = (var.github_app_id == "") == (var.github_app_client_id == "") && (var.github_app_id == "") == (var.github_app_slug == "")
    error_message = "github_app_id, github_app_client_id and github_app_slug are set together, from the one App's settings page (docs/runbooks/github-app.md step 3); one without the others names half an App."
  }
}

check "github_app_has_a_callback_host" {
  assert {
    condition     = var.github_app_client_id == "" || local.github_app_callback_url != ""
    error_message = "github_app_client_id is set but frontend_hostname is empty: the App's callback URL is https://<frontend_hostname>/onboarding/github/callback, and without a console there is nowhere for GitHub to return the user."
  }
}

output "github_app" {
  description = "The GitHub App's non-secret settings, its callback URL and its secret slots' ids. Names only; never a value."
  value = {
    enabled        = var.enable_github_app
    app_id         = var.github_app_id
    client_id      = var.github_app_client_id
    slug           = var.github_app_slug
    callback_url   = local.github_app_callback_url
    secret_ids     = module.secret_manager.github_app_secret_ids
    refresh_job_on = var.enable_forge_refresh
  }
}
