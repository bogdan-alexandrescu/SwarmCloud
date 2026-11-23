# ---------------------------------------------------------------------------
# The roles and join acceptance probes' identities (docs/design/workspace-roles.md
# section 8, docs/design/join-requests.md section 8)
# ---------------------------------------------------------------------------
#
# scripts/acceptance/groups/roles.sh and groups/join.sh act as two service
# accounts: a non-admin viewer and a scoped probe admin. swarm-verify mints
# their ID tokens (scripts/acceptance/lib.sh, _acc_mint_id_token, which calls
# generateIdToken), so neither account has a key and neither is lent from a
# person.
#
# Patterned on qa_identity.tf: an owner-applied identity whose account-level
# policy is written WHOLE, so a token creator or actAs member added by hand is
# removed on the next apply, and tests/terraform/bootstrap.tftest.hcl pins
# that swarm-verify is the only principal granted on either account.
#
# Behind enable_acceptance_probes (default false, so merging changes no plan);
# true goes only in the owner's bootstrap settings. The release deployer does
# NOT administer these accounts: they are not in modules/service_account_ids'
# platform list or the infra tenants, so deployer_service_accounts.tf grants it
# no serviceAccountAdmin on them and a release cannot change their policies.
# Nothing here is granted at the project, so the deployer's grantable roles
# (deployer_conditions.tf) are unchanged.

variable "enable_acceptance_probes" {
  description = "Create the two identities the roles and join acceptance probes act as, and let swarm-verify mint ID tokens (only) for them. Default false: a new install has no probes until its owner provisions their workspaces (docs/design/workspace-roles.md section 8)."
  type        = bool
  default     = false
}

locals {
  probes_on           = var.enable_acceptance_probes ? 1 : 0
  probe_verify_member = "serviceAccount:${module.service_account_ids.verify_id}@${var.project_id}.iam.gserviceaccount.com"
}

# No labels: google_service_account has none (scripts/lib/unlabelable-types.json).
# No create_ignore_already_exists: a 409 on either is a squat and must fail the apply.
resource "google_service_account" "probe_viewer" {
  count = local.probes_on

  project      = var.project_id
  account_id   = "roles-probe-viewer"
  display_name = "Swarm acceptance: roles probe viewer"
  description  = "managed-by=swarm-terraform; the non-admin identity scripts/acceptance/groups/roles.sh acts as, a member of the roles-probe group only. No key; swarm-verify mints its ID tokens and nothing else. No admin of any kind."
}

# The design's wording was 280 characters; the provider refuses a description
# over 256 at plan, so it is said here in fewer words.
resource "google_service_account" "probe_admin" {
  count = local.probes_on

  project      = var.project_id
  account_id   = "accept-probe-admin"
  display_name = "Swarm acceptance: scoped probe admin"
  description  = "managed-by=swarm-terraform; scoped probe admin of the roles and join probes: in probe_admin_users of the roles-probe and join-probe workspaces only, never admin_users; in those two groups only. No key; swarm-verify mints its ID tokens and nothing else."
}

# swarm-verify mints ID tokens for both (scripts/acceptance/lib.sh,
# _acc_mint_id_token, which calls generateIdToken).
# roles/iam.serviceAccountOpenIdTokenCreator holds
# iam.serviceAccounts.getOpenIdToken only: no access token, no signJwt, no
# signBlob. Never roles/iam.serviceAccountTokenCreator.
data "google_iam_policy" "probe_minting" {
  count = local.probes_on

  binding {
    role    = "roles/iam.serviceAccountOpenIdTokenCreator"
    members = [local.probe_verify_member]
  }
}

# Each account's whole policy, written in one piece (as qa_identity.tf does),
# so the bootstrap test can pin that swarm-verify is the only principal granted
# ON THE ACCOUNT.
resource "google_service_account_iam_policy" "probe_viewer" {
  count = local.probes_on

  service_account_id = google_service_account.probe_viewer[0].name
  policy_data        = data.google_iam_policy.probe_minting[0].policy_data

  # The binding names swarm-verify by a string, so its account exists first.
  depends_on = [google_service_account.verify]
}

resource "google_service_account_iam_policy" "probe_admin" {
  count = local.probes_on

  service_account_id = google_service_account.probe_admin[0].name
  policy_data        = data.google_iam_policy.probe_minting[0].policy_data

  depends_on = [google_service_account.verify]
}
