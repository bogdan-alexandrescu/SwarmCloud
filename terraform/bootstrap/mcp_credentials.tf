# ---------------------------------------------------------------------------
# Per-run MCP credentials: swarm-api's read and delete of `-mcpc-` secrets
# (docs/design/registered-mcp-servers.md §4.4; lane MCP-IAM, part of #1433).
# ---------------------------------------------------------------------------
#
# A dispatch that names a registered MCP server with `auth.mode: dispatch`
# carries a per-run credential. swarm-api stores it as one Secret Manager
# secret per (submission, server), `swarm-tenant-<tenant>-mcpc-<16 hex>`
# (Tenant.secret_name("mcpc-<hex>") in the frozen contract; the hex is minted
# by swarm-api), created with Secret Manager's own `ttl` at expires_in plus 5
# minutes. swarm-api is its ONLY reader: the attach route
# (`POST /v1/attempts/{attempt_id}/mcp:attach`, lane MCP-API) hands the value
# to the attempt's worker on a request signed with the attempt key and fenced
# on the generation, and the worker keeps it in its relay's memory, never in
# a file the agent can read (§4.7). The tenant's worker account holds NO grant
# on any of them, so an agent that mints its tenant GSA's token reads none.
#
#   google_project_iam_custom_role.mcp_credential_handler
#       swarmMcpCredentialHandler: secretmanager.versions.access and
#       secretmanager.secrets.delete, and no other permission.
#         versions.access   the attach route reads the row's `version` (a full
#                           version name) and accesses exactly that; never
#                           versions.get or versions.list.
#         secrets.delete    POST /v1/admin/mcp-credentials/sweep deletes a
#                           terminal task's secret (§4.4, "Destroyed when").
#                           swarmProviderKeyWriter (provider_keys.tf), which
#                           §4.4 has swarm-api create and version these
#                           secrets with, does NOT hold it: it carries
#                           secrets.create and versions.add (enough for the
#                           write, its project arm admitting the create and
#                           its swarm-tenant- arm the version) and
#                           versions.destroy, but its header refuses
#                           secrets.delete for provider keys. It is granted
#                           here instead, under this file's -mcpc- condition,
#                           so the delete reaches no provider key, forge
#                           token or workspace secret.
#       Not roles/secretmanager.secretAccessor, which is the same read today
#       and whatever Google adds to it tomorrow.
#   google_project_iam_member.mcp_credential_handler
#       swarm-api holds it, under one condition (below).
#
# THE CONDITION, one expression and no list, workspace_secrets.tf's shape with
# its own marker:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-mcpc-") != ""
#
#   * The project NUMBER: IAM evaluates Secret Manager names with it, and the
#     project ID "can't be substituted" there (docs.cloud.google.com/iam/docs/
#     conditions-resource-attributes). versions.access is evaluated on the
#     VERSION, `projects/<n>/secrets/<id>/versions/<v>`, and secrets.delete on
#     the SECRET, `projects/<n>/secrets/<id>`; both start with the same prefix
#     and carry the same `-mcpc-`, so one expression covers both.
#   * extract(), not matches(): IAM refuses matches() on some services
#     (terraform/modules/iam/bindings.tf, release run 38114832799). The arm is
#     true exactly when a NON-EMPTY tenant sits between `swarm-tenant-` and
#     the first `-mcpc-` after it.
#   * No hasOnly() and no tenant list: one condition covers every workspace,
#     personal tenants the workspace job creates outside Terraform included,
#     and never meets hasOnly()'s 10-item limit (deployer_conditions.tf, #275).
#
# WHAT IT REFUSES. Every other secret family under the prefix: a workspace
# secret (`-wsec-`, workspace_secrets.tf's own grant), a tenant's forge token
# (`-git`) and its `-git-u-`/`-git-r-` slots, its provider keys (`-anthropic`,
# `-typesafe`, ...), its saved browser logins, the platform's
# swarm-github-app-* and swarm-account-* secrets, and the other team's agents-*
# and promptlab-* secrets, which do not start with the prefix at all.
#
# WHAT IT STILL REACHES (the residual, for the security review). As in
# workspace_secrets.tf: the tenant id and some suffixes are free text, so the
# arm is also true of a non-MCP secret whose name carries `-mcpc-` after a
# non-empty tenant -- a saved browser login named `x-mcpc-y`, which
# browser_logins.NAME accepts today, or any secret of a tenant whose own id
# contains `-mcpc-`. swarm-api's code reads and deletes only the `secret_id`s
# it minted into `mcp_dispatch_credentials/`, and it already holds
# secrets.setIamPolicy over every swarm-tenant-* secret (provider_keys.tf),
# which amounts to versions.access, so the read is not new reach over those
# names. The DELETE is: closing it at its source is refusing `mcpc` as a
# hyphen-separated token in a browser login name and a tenant id, which is
# apps/ and the frozen contract, outside this lane. It is in this lane's pull
# request for the owner.
#
# THE TENANT GSA. Nothing here names a worker account, and
# tests/terraform/mcp_credentials.tftest.hcl holds that no bootstrap grant to
# a tenant worker (forge_user_slots.tf's forge_slot_reader,
# typesafe_user_keys.tf's rows) admits a `-mcpc-` name.
#
# WHY HERE AND NOT IN terraform/infra. A custom role cannot be defined there
# (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl), and a role
# carrying versions.access on the CI deployer's grantable list would let CI
# grant itself read on any secret in saga-agents-staging, the other team's
# among them. So the owner applies this (dev-iam). The role id is spelled
# here, as acceptance.tf's are, because terraform/infra never names it.
#
# NOT VERIFIED LIVE: that IAM evaluates versions.access and secrets.delete
# with resource.name in the forms above, and that extract() answers as
# described for them. The mock-provider tests hold the expression's text and
# its behaviour under an emulation of startsWith() and extract(), not IAM's
# answer. Lint the condition with iamPolicies:lintPolicy before applying.
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so it can match none of them. The grant is to swarm-api alone
# and touches no cluster, network, bucket or account of theirs.

variable "enable_mcp_credentials" {
  description = <<-EOT
    Define swarmMcpCredentialHandler (secretmanager.versions.access and
    secretmanager.secrets.delete only) and grant it to swarm-api on
    swarm-tenant-<t>-mcpc-* secrets and their versions (mcp_credentials.tf,
    docs/design/registered-mcp-servers.md §4.4). Lane MCP-IAM, part of #1433.
    Off by default: a read grant nobody uses is only exposure, and swarm-api
    reads a per-run MCP credential only once the attach route (MCP-API) exists
    and SWARM_MCP_SERVERS is on. The account must exist (terraform/infra
    creates swarm-api).
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

resource "google_project_iam_custom_role" "mcp_credential_handler" {
  count = local.mcp_credentials_on

  project     = var.project_id
  role_id     = "swarmMcpCredentialHandler"
  title       = "Swarm MCP Credential Handler"
  description = "managed-by=swarm-terraform; swarm-api reads per-run MCP credentials (swarm-tenant-<t>-mcpc-*) to attach them to an attempt's worker relay, and deletes them once the task is terminal (MCP-IAM, #1433). versions.access and secrets.delete only."
  stage       = "GA"

  # Two permissions (the header). Not roles/secretmanager.secretAccessor or
  # roles/secretmanager.admin, whose contents are Google's to change.
  permissions = [
    "secretmanager.secrets.delete",
    "secretmanager.versions.access",
  ]
}

resource "google_project_iam_member" "mcp_credential_handler" {
  count = local.mcp_credentials_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.mcp_credential_handler[0].role_id}"
  member = local.mcp_credentials_api_member

  condition {
    title       = "swarm mcp credentials"
    description = "managed-by=swarm-terraform; swarm-api reads and deletes swarm-tenant-<t>-mcpc-* secrets and their versions, for every workspace, to attach them per attempt and destroy them at task end (MCP-IAM, #1433). No tenant worker holds this."
    expression  = local.mcp_credentials_condition
  }
}

output "mcp_credential_grants" {
  description = "What swarm-api's per-run MCP credential read and delete is granted on, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_mcp_credentials is false."
  value = {
    api_read_delete = var.enable_mcp_credentials ? local.mcp_credentials_condition : ""
  }
}
