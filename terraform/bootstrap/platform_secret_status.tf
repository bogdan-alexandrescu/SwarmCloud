# ---------------------------------------------------------------------------
# Admin › Platform secrets: swarm-api's METADATA read of swarm-* secrets
# (docs/design/secrets-scopes.md §5.2, BGD's Q7 answer of 2026-10-23 in §11;
# lane SSC-PLATFORM-STATUS, part of #2158).
# ---------------------------------------------------------------------------
#
# `GET /v1/admin/platform-secrets` (apps/swarm-api/swarm_api/platform_secrets.py)
# tells an admin whether each platform secret is set, how many versions are
# enabled and how old the newest is. It reads metadata and never a value:
#
#   google_project_iam_custom_role.secret_status_viewer
#       swarmSecretStatusViewer: secretmanager.secrets.get and
#       secretmanager.versions.list, and no other permission. Not
#       roles/secretmanager.viewer, which also carries versions.get,
#       secrets.list and locations, and whatever Google adds to it tomorrow.
#       Not versions.access: the page shows no value, and the role could not
#       read one if the code asked. Not secrets.list: the names come from
#       swarm-api's catalogue and the broker's account documents, so nothing
#       enumerates the project's secrets -- the other team's among them.
#   google_project_iam_member.secret_status_viewer
#       swarm-api holds it, under one condition (below).
#
# THE CONDITION, one expression:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-")
#
#   * The project NUMBER, read from data.google_project and never written as
#     a literal: IAM evaluates Secret Manager names with it, and the project
#     ID "can't be substituted" there (docs.cloud.google.com/iam/docs/
#     conditions-resource-attributes). A condition spelled with the ID matches
#     nothing. secrets.get is evaluated on the secret and versions.list on its
#     parent, the secret, `projects/<n>/secrets/<id>`: one prefix covers both.
#   * `swarm-` and not a narrower family: the page lists the platform's
#     secrets (swarm-github-app-*, swarm-live-key, ...) and the broker's
#     swarm-account-* ones, which share no longer prefix. It also admits
#     swarm-tenant-* METADATA (a name and its versions' dates), which design
#     §5.2 records as harmless and which swarm-api's secrets.setIamPolicy over
#     swarm-tenant-* (provider_keys.tf, browser_logins.tf) already exceeds.
#
# WHAT IT REFUSES. Every secret not named swarm-*: the other team's agents-*
# and promptlab-* secrets in this SHARED project (CLAUDE.md rule 2), and any
# secret of another project, whose number differs. And every value: the role
# has no versions.access, so even a swarm-* payload stays unread.
#
# WHY HERE AND NOT IN terraform/infra. A custom role cannot be defined there
# (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl). The role id is
# spelled here, as mcp_credentials.tf spells its own: terraform/infra never
# names this role, so it has no place in terraform/modules/custom_role_ids.
# The owner applies this root.
#
# NOT VERIFIED LIVE: that IAM evaluates versions.list with resource.name as
# the parent secret's full name. The mock-provider suite
# (tests/terraform/platform_secret_status.tftest.hcl) holds the expression's
# text and its verdict under an emulation of startsWith(), not IAM's answer.
# Lint it with iamPolicies:lintPolicy before applying, as every condition
# here has been. Until it is applied, the page reads every row as
# `unreadable: permission_denied` and serves no 500.
#
# THE OTHER TEAM'S RESOURCES. The arm starts `projects/<n>/secrets/swarm-`;
# their secrets are agents-* and promptlab-*, so it matches none of them. The
# grant is to swarm-api alone and touches no cluster, network, bucket or
# account of theirs.

variable "enable_platform_secret_status" {
  description = <<-EOT
    Define swarmSecretStatusViewer (secretmanager.secrets.get and
    secretmanager.versions.list only) and grant it to swarm-api on swarm-*
    secrets of this project (platform_secret_status.tf, design
    docs/design/secrets-scopes.md §5.2 and §11 Q7). Lane SSC-PLATFORM-STATUS,
    part of #2158. Off by default, as every bootstrap grant here starts: the
    owner turns it on in terraform.tfvars when applying, after linting the
    condition. Off, GET /v1/admin/platform-secrets serves each row as
    unreadable (permission_denied). The account must exist (terraform/infra
    creates swarm-api).
  EOT
  type        = bool
  default     = false
}

data "google_project" "platform_secret_status" {
  count      = var.enable_platform_secret_status ? 1 : 0
  project_id = var.project_id
}

locals {
  platform_secret_status_on = var.enable_platform_secret_status ? 1 : 0

  # The project number, for the condition. "" while off, when nothing reads it.
  platform_secret_status_project_number = var.enable_platform_secret_status ? data.google_project.platform_secret_status[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam).
  platform_secret_status_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # A secret of this project whose id starts `swarm-` (the header).
  platform_secret_status_condition = "resource.name.startsWith(\"projects/${local.platform_secret_status_project_number}/secrets/swarm-\")"
}

resource "google_project_iam_custom_role" "secret_status_viewer" {
  count = local.platform_secret_status_on

  project     = var.project_id
  role_id     = "swarmSecretStatusViewer"
  title       = "Swarm Secret Status Viewer"
  description = "managed-by=swarm-terraform; swarm-api reads swarm-* secrets' METADATA for Admin > Platform secrets (SSC-PLATFORM-STATUS, #2158). secrets.get and versions.list only; never a value."
  stage       = "GA"

  # Two permissions (the header). Not roles/secretmanager.viewer.
  permissions = [
    "secretmanager.secrets.get",
    "secretmanager.versions.list",
  ]
}

resource "google_project_iam_member" "secret_status_viewer" {
  count = local.platform_secret_status_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.secret_status_viewer[0].role_id}"
  member = local.platform_secret_status_api_member

  condition {
    title       = "swarm secret status"
    description = "managed-by=swarm-terraform; swarm-api reads the metadata of swarm-* secrets in this project, by project number, for Admin > Platform secrets (SSC-PLATFORM-STATUS, #2158). No value; no other team's secret."
    expression  = local.platform_secret_status_condition
  }
}

output "platform_secret_status_grants" {
  description = "What swarm-api's platform-secret metadata read is granted on, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_platform_secret_status is false."
  value = {
    api_metadata = var.enable_platform_secret_status ? local.platform_secret_status_condition : ""
  }
}
