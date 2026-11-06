# ---------------------------------------------------------------------------
# qa-console: the identity a QA browser agent signs in to the console as
# (docs/design/qa-identity.md, option B; lane QI-IAM, part of #1649; owner
# decision 2026-10-19)
# ---------------------------------------------------------------------------
#
# A QA agent sweeping the console in a live browser has to pass IAP, and a
# person typing each sign-in is what #1649 set out to remove. Option B: a
# service account IAP admits, whose short-lived JWT (15 minutes, renewed at
# 10) swarm-api signs and the gate adds to the browser's requests. The agent
# never holds a key, a token it could mint again, or anything that outlives
# its attempt. Inside the console the account has member rights in its own
# service workspace (the new-workspace flow, O3), granted by a platform admin
# to eng only (O4); nothing is lent from a person.
#
# WHAT THIS FILE MAKES, all of it behind enable_qa_identity (default false, so
# merging changes no plan; the owner's bootstrap apply O2 turns it on):
#
#   * THE ACCOUNT, qa-console, with NO KEY. Nothing in this root declares a
#     google_service_account_key, and nothing should: a key is a credential
#     that outlives every token and every policy below. swarm-api signs
#     through iamcredentials signJwt, where Google holds the key.
#   * swarmQaJwtSigner, a custom role holding iam.serviceAccounts.signJwt and
#     nothing else. Not roles/iam.serviceAccountTokenCreator, which also
#     carries getAccessToken, getOpenIdToken, signBlob and implicitDelegation:
#     an access token works against every Google API the account can reach,
#     an ID token or a blob signature for any audience, and delegation chains
#     through the account to another. A JWT swarm-api signs carries the claims
#     swarm-api writes, and IAP checks the audience.
#   * THE ACCOUNT'S OWN POLICY, written AUTHORITATIVELY with that one binding
#     for swarm-api's runtime account. So swarm-api may sign as qa-console and,
#     through this file, as no other account; and nobody -- not swarm-api, not
#     the CI deployer, not a tenant's worker -- holds actAs, token creator or
#     workloadIdentityUser on qa-console at the account level. A member added
#     there by hand is removed by the next apply.
#
# WHY HERE, AND NOT IN terraform/infra. Every custom role is defined in this
# root (github_hooks_role.tf says why: CI holds no iam.roles.* permission),
# and this grant decides who can sign in as a console user. If it lived in a
# root CI applies, the deployer would need setIamPolicy on qa-console and could
# grant itself the signer role. qa-console is not in
# modules/service_account_ids, so the deployer holds no serviceAccountAdmin on
# it (deployer_service_accounts.tf), and the role is not on
# deployer_grantable_project_roles, so no projectIamAdmin chunk admits it.
#
# NEVER AT THE PROJECT. signJwt granted at the project reaches every service
# account in a project shared with another team, theirs included. The role is
# bound on qa-console alone.
#
# THE FRONT DOOR. IAP admits a principal only if it holds
# roles/iap.httpsResourceAccessor, which frontend_iap_members grants (wif.tf).
# A QA account not on that list gets 403 "Access denied. For user ..." on
# every signed request, which a QA run would report as a console it cannot
# reach rather than a configuration mistake. The precondition below refuses
# that at plan, the way ci_fix.tf does for the CI fixer: O2 adds
# `serviceAccount:qa-console@<project>.iam.gserviceaccount.com` to
# frontend_iap_members in terraform/bootstrap/terraform.tfvars in the same
# apply that sets enable_qa_identity.
#
# WHAT IT STILL ALLOWS, for the security review. Whoever can run code as
# swarm-api can sign a JWT as qa-console and so reach the console as it, with
# qa-console's member rights; that is the design's trade, and swarm-api already
# holds more than qa-console can. A project Owner or Editor of the shared
# project can redeploy swarm-api or add a project-level token creator grant
# this file cannot see; spec_signing.tf records the same residual for the
# spec key. A project-level iam.serviceAccountKeys.create holder could still
# make a key for qa-console: no key exists, and none is declared here.
#
# signJwt signs whatever claims the caller writes, and Google APIs accept a
# service account's self-signed JWT as a credential. So a JWT signed as
# qa-console reaches exactly what qa-console holds, and qa-console must hold NOTHING
# at the project: its one grant is roles/iap.httpsResourceAccessor from
# frontend_iap_members, on the platform's own backends (wif.tf), the UI's and
# the API's. Past the front door, swarm-api is still the tenant boundary and
# qa-console has its member rights in its own workspace and no more.
#
# NOT VERIFIED LIVE (each fails closed -- a 403 at the front door, no session):
#
#   * that IAP accepts a JWT self-signed through signJwt by a service account
#     on its accessor list, with the audience the gate writes. That is
#     measurement M1 of the design, recorded in
#     docs/design/qa-identity-measurements.md before QI-API and QI-GATE are
#     built; if it fails the owner is asked, not this file changed;
#   * that a project-level custom role is bindable on a service account for
#     signJwt (Google documents service account as the lowest grant level for
#     the permission's predefined roles; not measured here).
#
# THE OTHER TEAM'S RESOURCES. This file makes one account, one role and that
# account's own policy; it names no cluster, network, bucket or account of
# theirs.

variable "enable_qa_identity" {
  description = <<-EOT
    Make the QA identity (qa_identity.tf; QI-IAM, part of #1649): the key-less
    service account qa-console, the custom role swarmQaJwtSigner
    (iam.serviceAccounts.signJwt only), and qa-console's own policy granting that
    role to swarm-api alone. Off by default, so merging changes no plan. Turn
    it on in the owner's bootstrap apply together with the
    `serviceAccount:qa-console@<project>.iam.gserviceaccount.com` line in
    frontend_iap_members; without that line the plan is refused.
  EOT
  type        = bool
  default     = false
}

locals {
  qa_identity_on = var.enable_qa_identity ? 1 : 0

  # NOT `swarm-*`. The QA identity needs a service workspace of its own (its
  # isolation rests on it), and the workspace flow refuses every account with
  # the platform's prefix (swarm_api.workspaces.PLATFORM_SA_PREFIX,
  # WORKSPACE_SERVICE_PLATFORM). The first apply made `swarm-qa` and was
  # refused there on 2026-10-20; tests/unit/scripts/test_qa_identity_account_name.py
  # holds this name to that rule.
  qa_account_id = "qa-console"
  qa_email      = "${local.qa_account_id}@${var.project_id}.iam.gserviceaccount.com"
  qa_member     = "serviceAccount:${local.qa_email}"

  qa_jwt_signer_permissions = ["iam.serviceAccounts.signJwt"]
  qa_jwt_signer_role        = "projects/${var.project_id}/roles/${module.custom_role_ids.ids["qa_jwt_signer"]}"

  # The one identity that signs, taken from the list of accounts terraform/infra
  # creates rather than typed, so a rename there fails here at plan instead of
  # granting the signer role to an account that does not exist.
  qa_signer_account_id = one([for id in keys(module.service_account_ids.platform) : id if id == "swarm-api"])
  qa_signer_member     = "serviceAccount:${local.qa_signer_account_id}@${var.project_id}.iam.gserviceaccount.com"
}

# No labels: google_service_account has none (scripts/lib/unlabelable-types.json).
# No create_ignore_already_exists: an account someone made first under this
# name is a squat to investigate, not one to adopt and grant a signer on.
resource "google_service_account" "qa_identity" {
  count = local.qa_identity_on

  project      = var.project_id
  account_id   = local.qa_account_id
  display_name = "Swarm QA browser"
  description  = "managed-by=swarm-terraform; the identity a QA browser agent signs in to the console as, through IAP, with a short-lived JWT swarm-api signs (docs/design/qa-identity.md, option B). No key, no WIF."

  lifecycle {
    precondition {
      condition     = contains(var.frontend_iap_members, local.qa_member)
      error_message = "enable_qa_identity needs serviceAccount:qa-console@<project>.iam.gserviceaccount.com in frontend_iap_members, in the same apply, or IAP refuses every signed QA request."
    }
  }
}

resource "google_project_iam_custom_role" "qa_jwt_signer" {
  count = local.qa_identity_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["qa_jwt_signer"]
  title       = "Swarm QA JWT Signer"
  description = "managed-by=swarm-terraform; swarm-api signs the QA browser's IAP JWT as qa-console (QI-IAM, #1649). signJwt only: no access or ID token, no signBlob, no actAs, no delegation. Bound on qa-console alone, never the project."
  stage       = "GA"

  permissions = local.qa_jwt_signer_permissions
}

# AUTHORITATIVE: this is qa-console's whole account-level policy. Its own data
# source, so the tests hold it apart from every other account's.
data "google_iam_policy" "qa_identity" {
  binding {
    role    = local.qa_jwt_signer_role
    members = [local.qa_signer_member]
  }
}

resource "google_service_account_iam_policy" "qa_identity" {
  count = local.qa_identity_on

  service_account_id = google_service_account.qa_identity[0].name
  policy_data        = data.google_iam_policy.qa_identity.policy_data

  # The binding names the role, so the role exists first; and swarm-api by a
  # string (qa_signer_member), so the account exists first too (audit 3 A10).
  depends_on = [google_project_iam_custom_role.qa_jwt_signer, google_service_account.platform]
}

output "qa_identity" {
  description = "The QA identity, for the owner to read a plan against: the account's email, the signer role's full name and its permissions. Empty while enable_qa_identity is false."
  value = {
    # The spelled email, not the resource's: known at plan, so the owner reads
    # it on the first apply too.
    email       = var.enable_qa_identity ? local.qa_email : ""
    role        = var.enable_qa_identity ? local.qa_jwt_signer_role : ""
    permissions = var.enable_qa_identity ? local.qa_jwt_signer_permissions : []
  }
}
