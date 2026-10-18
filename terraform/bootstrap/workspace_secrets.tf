# ---------------------------------------------------------------------------
# Workspace secrets: swarm-api's read of `-wsec-` secrets
# (docs/design/workspace-secrets.md §3.3, option B; lane SEC-IAM, part of
# #1347).
# ---------------------------------------------------------------------------
#
# A workspace secret is one Secret Manager secret,
# `swarm-tenant-<tenant>-wsec-<16 hex>` (Tenant.secret_name("wsec-<hex>") in
# the frozen contract; the hex is minted by swarm-api, the display name is
# never in the resource name, design §2.1). Under option B the tenant's worker
# account holds NO read grant on any of them: swarm-api is the only reader,
# and it releases an attempt's granted secrets to that attempt's worker on a
# request signed with the attempt key and fenced on the generation
# (`POST /v1/attempts/{attempt_id}/secrets:release`, lane SEC-RELEASE). An
# agent that mints its tenant GSA's token therefore reads no workspace secret.
#
#   google_project_iam_custom_role.workspace_secret_accessor
#       swarmWorkspaceSecretAccessor: secretmanager.versions.access, and no
#       other permission. Not roles/secretmanager.secretAccessor, which is the
#       same permission today and whatever Google adds to it tomorrow. Not
#       versions.get or versions.list: the release reads the record's
#       `current_version` (a full version name) from Firestore and accesses
#       exactly that, so it never needs to look a version up.
#   google_project_iam_member.workspace_secret_accessor
#       swarm-api holds it, under one condition (below).
#
# THE CONDITION, one expression and no list:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-wsec-") != ""
#
#   * The project NUMBER: IAM evaluates Secret Manager names with it, and the
#     project ID "can't be substituted" there (docs.cloud.google.com/iam/docs/
#     conditions-resource-attributes). versions.access is evaluated on the
#     VERSION, `projects/<n>/secrets/<id>/versions/<v>`, which starts with the
#     same prefix and carries the same `-wsec-`, so one expression covers a
#     secret and its versions.
#   * extract(), not matches(): IAM refuses matches() on some services
#     (terraform/modules/iam/bindings.tf, release run 38114832799), and
#     extract() is the form browser_logins.tf and forge_user_slots.tf already
#     use. extract("pre{t}suf") is the text between the first `pre` and the
#     first `suf` after it, "" when either is missing, so the arm is true
#     exactly when a NON-EMPTY tenant sits between `swarm-tenant-` and
#     `-wsec-`.
#   * No hasOnly() and no tenant list: the tenant is found in the name, not
#     enumerated, so one condition covers every workspace, personal tenants
#     the workspace job creates outside Terraform included
#     (docs/workspaces.md §3.2), and never meets hasOnly()'s 10-item limit
#     (deployer_conditions.tf, #275).
#
# WHAT IT REFUSES. Every other secret family under the prefix: a tenant's
# forge token (`-git`) and its `-git-u-`/`-git-r-` slots (hex only,
# FORGE_CREDENTIAL in the frozen models), its provider keys (`-anthropic`,
# `-typesafe`, ... from an allow-list), the platform's swarm-github-app-* and
# swarm-account-* secrets, and the other team's agents-* and promptlab-*
# secrets, which do not start with the prefix at all. The legacy
# `-typesafe` keys are NOT covered: design §6.1 step 4 adds them as a second
# arm in this file, in its own lane, when SEC-MIGRATE-TYPESAFE needs it.
#
# WHAT IT STILL REACHES (the residual, for the security review). The tenant
# id and some suffixes are free text, and extract() finds the FIRST `-wsec-`
# after `swarm-tenant-`, so the arm is also true of a non-workspace secret
# whose name happens to carry `-wsec-` after a non-empty tenant:
#
#   * a saved browser login whose name contains `wsec` between hyphens (a
#     login named `x-wsec-y`), which browser_logins.NAME
#     (`^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$`) accepts today. That
#     secret's name is spelled only in browser_logins.tf, whose suite holds
#     it there;
#   * any secret of a tenant whose own id contains `-wsec-`.
#
# On such a secret swarm-api could read a value it never names: its code
# reads only the `secret_id`s it minted into `workspace_secrets/` (§3.3 step
# 4). swarm-api already holds secrets.setIamPolicy over every swarm-tenant-*
# secret (provider_keys.tf, browser_logins.tf), which "amounts to
# versions.access" (docs/provider-keys.md §6), so this is not new reach over
# those names. Closing it in IAM would need matches() or a list of the other
# families; closing it at its source is refusing `wsec` as a hyphen-separated
# token in a browser login name and in a tenant id, which is apps/ and the
# frozen contract, outside this lane. It is in this lane's pull request for
# the owner.
#
# THE TENANT GSA. Nothing here names a worker account, and
# tests/terraform/workspace_secrets.tftest.hcl holds that no bootstrap grant
# to a tenant worker admits a `-wsec-` name: typesafe_user_keys.tf's rows, and
# forge_user_slots.tf's forge_slot_reader, which stays while terraform/infra's
# `forge_broker` is off and is removed only after the broker has been on in
# dev (#1041). The test reads both, so a removal changes it too. credentials.py's
# per-secret worker binding is for provider keys; the store's writes
# (SEC-STORE-API) must not add one for a `-wsec-` secret.
#
# WHY HERE AND NOT IN terraform/infra. A custom role cannot be defined there
# (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl), and a role
# carrying versions.access on the CI deployer's grantable list would let CI
# grant itself read on any secret in saga-agents-staging, the other team's
# among them (variables.tf's deployer_secret_permissions refuses it). So the
# owner applies this (dev-iam).
#
# NOT VERIFIED LIVE: that IAM evaluates versions.access with resource.name in
# the full `projects/<n>/secrets/<id>/versions/<v>` form, and that extract()
# answers as above for it. The mock-provider tests hold the expression's text
# and its behaviour under an emulation of startsWith() and extract(), not
# IAM's answer. Lint the condition with iamPolicies:lintPolicy before
# applying, and show it refuses a broken expression first, as
# docs/provider-keys.md §6 did.
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so it can match none of them. The grant is to swarm-api alone
# and touches no cluster, network, bucket or account of theirs.

variable "enable_workspace_secrets" {
  description = <<-EOT
    Define swarmWorkspaceSecretAccessor (secretmanager.versions.access only)
    and grant it to swarm-api on swarm-tenant-<t>-wsec-* secrets and their
    versions (workspace_secrets.tf, design docs/design/workspace-secrets.md
    §3.3). Lane SEC-IAM, part of #1347. Off by default: a read grant nobody
    uses is only exposure, and swarm-api reads a workspace secret only once
    the release route (SEC-RELEASE) exists. The account must exist
    (terraform/infra creates swarm-api).
  EOT
  type        = bool
  default     = false
}

data "google_project" "workspace_secrets" {
  count      = var.enable_workspace_secrets ? 1 : 0
  project_id = var.project_id
}

locals {
  workspace_secrets_on = var.enable_workspace_secrets ? 1 : 0

  # The project number, for the condition. "" while off, when nothing reads it.
  workspace_secrets_project_number = var.enable_workspace_secrets ? data.google_project.workspace_secrets[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam).
  workspace_secrets_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # A tenant secret by its full name, with a non-empty tenant before the first
  # `-wsec-` (the header). Literal "swarm-tenant-" and "-wsec-":
  # Tenant.secret_name's spelling and the design's marker (§2.1).
  workspace_secrets_condition = "resource.name.startsWith(\"projects/${local.workspace_secrets_project_number}/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-wsec-\") != \"\""
}

resource "google_project_iam_custom_role" "workspace_secret_accessor" {
  count = local.workspace_secrets_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["workspace_secret_accessor"]
  title       = "Swarm Workspace Secret Accessor"
  description = "managed-by=swarm-terraform; swarm-api reads workspace secrets (swarm-tenant-<t>-wsec-*) to release an attempt's granted ones to its worker (SEC-IAM, #1347). versions.access only."
  stage       = "GA"

  # One permission (the header). Not roles/secretmanager.secretAccessor, whose
  # contents are Google's to change.
  permissions = [
    "secretmanager.versions.access",
  ]
}

resource "google_project_iam_member" "workspace_secret_accessor" {
  count = local.workspace_secrets_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.workspace_secret_accessor[0].role_id}"
  member = local.workspace_secrets_api_member

  condition {
    title       = "swarm workspace secrets"
    description = "managed-by=swarm-terraform; swarm-api reads swarm-tenant-<t>-wsec-* secrets and their versions, for every workspace, to release them per attempt (SEC-IAM, #1347). No tenant worker holds this."
    expression  = local.workspace_secrets_condition
  }
}

output "workspace_secret_grants" {
  description = "What swarm-api's workspace-secret read is granted on, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_workspace_secrets is false."
  value = {
    api_read = var.enable_workspace_secrets ? local.workspace_secrets_condition : ""
  }
}
