# ---------------------------------------------------------------------------
# Registered MCP servers: swarm-api's read and delete of per-run `-mcpc-`
# credentials (docs/design/registered-mcp-servers.md §4.4; lane MCP-IAM, part
# of #1433).
# ---------------------------------------------------------------------------
#
# A caller that dispatches a task naming a `dispatch`-mode MCP server supplies
# a credential for it with the submission. swarm-api stores it as one Secret
# Manager secret per (submission, server), `swarm-tenant-<tenant>-mcpc-<16 hex>`
# (Tenant.secret_name("mcpc-<hex>") in the frozen contract; the hex is random,
# minted by swarm-api), created with Secret Manager's own `ttl` at the
# submission's expires_in plus 5 minutes. swarm-api is the ONLY reader: the
# worker's attach (`POST /v1/attempts/{attempt_id}/mcp:attach`, lane MCP-API)
# hands the value to that attempt's relay on a request signed with the attempt
# key and fenced on the generation, and the relay keeps it out of the agent's
# reach (design §4.7, §4.8). A tenant worker account holds NOTHING on
# `-mcpc-`, so an agent that mints its tenant GSA's token reads no credential.
#
#   google_project_iam_custom_role.mcp_credential_accessor
#       swarmMcpCredentialAccessor: secretmanager.versions.access and
#       secretmanager.secrets.delete, and no other permission.
#         versions.access   the attach reads the row's version by its full
#                           name, as the workspace-secret release does; no
#                           versions.get or versions.list.
#         secrets.delete    the sweep (`POST /v1/admin/mcp-credentials/sweep`,
#                           every 10 minutes, modules/scheduler/jobs.tf) deletes
#                           a terminal task's credential secret (design §4.4,
#                           owner decision Q2). swarmProviderKeyWriter, the
#                           reach swarm-api creates the secret with, holds
#                           secrets.create and versions.add but NOT
#                           secrets.delete (provider_keys.tf; read 2026-10-18),
#                           so the design's "asks for any it lacks" is answered
#                           here, scoped to `-mcpc-` alone rather than widening
#                           a role that reaches every swarm-tenant-* secret.
#       Not roles/secretmanager.secretAccessor, whose contents are Google's to
#       change, and not roles/secretmanager.admin, which reads every value in
#       the shared project.
#   google_project_iam_member.mcp_credential_accessor
#       swarm-api holds it, under one condition (below).
#
# THE ROLE ID is spelled here, as acceptance.tf and wif.tf spell theirs, and
# not in terraform/modules/custom_role_ids: that module exists so a role this
# root defines and terraform/infra grants is one string in both roots, and
# terraform/infra never names this role. Moving it there is a line in that
# module, outside this lane, and the pull request asks for it.
#
# THE CONDITION, one expression and no list, workspace_secrets.tf's shape with
# its marker:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-mcpc-") != ""
#
#   * The project NUMBER: IAM evaluates Secret Manager names with it, never the
#     project ID. versions.access is evaluated on the VERSION,
#     `projects/<n>/secrets/<id>/versions/<v>`, and secrets.delete on the
#     secret; both start with the prefix and carry `-mcpc-`, so one expression
#     covers a secret and its versions.
#   * extract(), not matches(), which IAM refuses on some services
#     (terraform/modules/iam/bindings.tf, release run 38114832799). The arm is
#     true exactly when a NON-EMPTY tenant sits between `swarm-tenant-` and the
#     first `-mcpc-` after it.
#   * No hasOnly() and no tenant list: the tenant is found in the name, so the
#     condition covers personal tenants the workspace job creates outside
#     Terraform too (docs/workspaces.md §3.2).
#
# WHAT IT REFUSES. Every other secret family: a workspace secret (`-wsec-`,
# workspace_secrets.tf's own grant), the tenant's forge token (`-git`) and
# its `-git-u-`/`-git-r-` slots, its provider keys (`-anthropic`,
# `-typesafe`, ...), the platform's swarm-github-app-* and swarm-account-*
# secrets, and the other team's agents-* and promptlab-* secrets, which do
# not start with the prefix at all.
#
# WHAT IT STILL REACHES (the residual, for the security review), the same
# shape as workspace_secrets.tf's: extract() finds the FIRST `-mcpc-` after
# `swarm-tenant-`, so the arm is also true of a secret whose free-text part
# carries `-mcpc-` after a non-empty tenant -- a saved browser login named
# `x-mcpc-y` (browser_logins.NAME accepts it today), or any secret of a tenant
# whose own id contains `-mcpc-`. swarm-api's code reads and deletes only the
# `secret_id`s it minted into `mcp_dispatch_credentials/`, and it already holds
# secrets.setIamPolicy over every swarm-tenant-* secret (provider_keys.tf),
# which amounts to versions.access, so the read is no new reach over those
# names; the delete is, and closing it at its source is refusing `mcpc` as a
# hyphen-separated token in a browser login name and a tenant id (apps/ and
# the frozen contract, outside this lane; asked in the pull request).
#
# THE TENANT GSA. Nothing here names a worker account, and
# tests/terraform/mcp_credentials.tftest.hcl holds that no bootstrap grant to
# a tenant worker admits a `-mcpc-` name: typesafe_user_keys.tf's rows, and
# forge_user_slots.tf's forge_slot_reader, which stays while terraform/infra's
# `forge_broker` is off and is removed only after the broker has been on in
# dev (#1041). The test reads both, so a removal changes it too.
#
# WHY HERE AND NOT IN terraform/infra. A custom role cannot be defined there
# (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl), and a role
# carrying versions.access on the CI deployer's grantable list would let CI
# grant itself read on any secret in saga-agents-staging, the other team's
# among them. So the owner applies this (dev-iam).
#
# NOT VERIFIED LIVE: that IAM evaluates the condition as the mock-provider
# tests emulate it. Lint it with iamPolicies:lintPolicy before applying, as
# docs/provider-keys.md §6 did.
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so it can match none of them. The grant is to swarm-api alone
# and touches no cluster, network, bucket or account of theirs.

variable "enable_mcp_credentials" {
  description = <<-EOT
    Define swarmMcpCredentialAccessor (secretmanager.versions.access and
    secretmanager.secrets.delete only) and grant it to swarm-api on
    swarm-tenant-<t>-mcpc-* secrets and their versions (mcp_credentials.tf,
    design docs/design/registered-mcp-servers.md §4.4). Lane MCP-IAM, part of
    #1433. Off by default: a read grant nobody uses is only exposure, and
    swarm-api reads a per-run MCP credential only once the attach route
    (MCP-API) exists and SWARM_MCP_SERVERS is on. The account must exist
    (terraform/infra creates swarm-api).
  EOT
  type        = bool
  default     = false
}

data "google_project" "mcp_credentials" {
  count      = var.enable_mcp_credentials ? 1 : 0
  project_id = var.project_id
}

locals {
  mcp_credentials_on = var.enable_mcp_credentials ? 1 : 0

  # The project number, for the condition. "" while off, when nothing reads it.
  mcp_credentials_project_number = var.enable_mcp_credentials ? data.google_project.mcp_credentials[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam).
  mcp_credentials_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # A tenant secret by its full name, with a non-empty tenant before the first
  # `-mcpc-` (the header). Literal "swarm-tenant-" and "-mcpc-":
  # Tenant.secret_name's spelling and the design's marker (§4.4).
  mcp_credentials_condition = "resource.name.startsWith(\"projects/${local.mcp_credentials_project_number}/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-mcpc-\") != \"\""
}

resource "google_project_iam_custom_role" "mcp_credential_accessor" {
  count = local.mcp_credentials_on

  project     = var.project_id
  role_id     = "swarmMcpCredentialAccessor"
  title       = "Swarm MCP Credential Accessor"
  description = "managed-by=swarm-terraform; swarm-api reads per-run MCP credentials (swarm-tenant-<t>-mcpc-*) for an attempt's relay and deletes them when the task ends (MCP-IAM, #1433). versions.access and secrets.delete only."
  stage       = "GA"

  # Two permissions (the header). Not roles/secretmanager.secretAccessor or
  # roles/secretmanager.admin.
  permissions = [
    "secretmanager.secrets.delete",
    "secretmanager.versions.access",
  ]
}

resource "google_project_iam_member" "mcp_credential_accessor" {
  count = local.mcp_credentials_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.mcp_credential_accessor[0].role_id}"
  member = local.mcp_credentials_api_member

  condition {
    title       = "swarm mcp credentials"
    description = "managed-by=swarm-terraform; swarm-api reads and deletes swarm-tenant-<t>-mcpc-* secrets and their versions, for every workspace, to attach them per attempt (MCP-IAM, #1433). No tenant worker holds this."
    expression  = local.mcp_credentials_condition
  }
}

output "mcp_credential_grants" {
  description = "What swarm-api's per-run MCP credential read and delete is granted on, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_mcp_credentials is false."
  value = {
    api_read = var.enable_mcp_credentials ? local.mcp_credentials_condition : ""
  }
}
