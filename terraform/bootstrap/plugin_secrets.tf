# ---------------------------------------------------------------------------
# Managed plugins: swarm-api's read of `-plg-` secrets
# (docs/design/managed-plugins.md §6.2; part of #2688).
# ---------------------------------------------------------------------------
#
# A plugin's secret field is one Secret Manager secret,
# `swarm-tenant-<tenant>-plg-<16 hex>` (Tenant.plugin_secret_name in the frozen
# contract, request 141). terraform/infra/plugins.tf grants swarm-api
# secretVersionAdder on those names, so it can store a value the console's
# one-time form sends. Design §6.2 has swarm-api, and no tenant worker,
# release each value to an attempt whose signed plugin set names it (the
# attempts_secrets pattern), which needs secretmanager.versions.access.
#
# WHY HERE AND NOT IN terraform/infra. A role carrying versions.access may not
# be on the CI deployer's grantable list (variables.tf,
# deployer_secret_permissions): on it, CI could grant itself read on any secret
# in saga-agents-staging, the other team's among them. So the owner applies
# this (his answer of 6 November 2026 to plugins.tf's open question).
#
#   google_project_iam_member.plugin_secret_reader
#       swarm-api holds roles/secretmanager.secretAccessor under one condition.
#       The predefined role, as the owner asked: it carries
#       secretmanager.versions.access alone today.
#
# THE CONDITION, one expression and no list, the same text as plugins.tf's
# version-add grant so the two can be compared line for line:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-plg-") != ""
#
#   * The project NUMBER, as workspace_secrets.tf and browser_logins.tf spell
#     it: IAM evaluates Secret Manager names with it, and the project ID
#     "can't be substituted" there. versions.access is evaluated on the
#     VERSION, `projects/<n>/secrets/<id>/versions/<v>`, which starts with the
#     same prefix and carries the same `-plg-`.
#   * extract(), not matches(): IAM refuses matches() on some services. The arm
#     is true exactly when a NON-EMPTY tenant sits between `swarm-tenant-` and
#     the first `-plg-` after it, so personal workspaces the workspace job
#     makes outside Terraform are covered without a tenant list or hasOnly().
#   * Within the condition limits (docs/design/day-one/lessons.md lesson 10):
#     one logical operator of the 12 allowed, no hasOnly(), no matches(); 136
#     characters with a 12-digit project number. The title is 25 characters
#     and the description 191, inside IAM's 100 and 256.
#
# WHAT IT REFUSES: a tenant's forge token (`-git`) and slots, its provider
# keys, its `-wsec-` secrets, the platform's `swarm-*` secrets, and the other
# team's `agents-*` and `promptlab-*` secrets, which do not start with the
# prefix. WHAT IT STILL REACHES (the residual): extract() finds the FIRST
# `-plg-`, so a non-plugin secret whose name carries `-plg-` after a non-empty
# tenant (a browser login named `x-plg-y`, a tenant id containing `-plg-`).
# swarm-api already holds secrets.setIamPolicy over swarm-tenant-* secrets
# (provider_keys.tf, browser_logins.tf), so that is no new reach.
#
# OFF UNTIL SOMETHING READS. On 7 November 2026 no swarm-api path reads a
# `-plg-` value: routes/attempts_secrets.py's SecretManagerReader serves the
# `-wsec-` release, and the plugin release (design §17 lane 6) is not built. A
# read grant nobody uses is only exposure, so enable_plugin_secrets defaults
# false and swarm install leaves it off; the lane that builds the release
# turns it on (the pull request's questions.json asks the owner).
#
# NOT VERIFIED LIVE: that IAM evaluates versions.access with resource.name in
# the full form, and that extract() answers as tests/terraform/
# plugin_secrets.tftest.hcl emulates. Lint the `plugin_secret_grants` output
# with iamPolicies:lintPolicy before applying.
#
# THE OTHER TEAM'S RESOURCES. The grant is to swarm-api alone, on names
# starting `projects/<n>/secrets/swarm-tenant-`; it names no cluster, network,
# bucket, secret or account of theirs. An IAM member takes no labels; the
# condition's description carries managed-by=swarm-terraform.

variable "enable_plugin_secrets" {
  description = <<-EOT
    Grant swarm-api roles/secretmanager.secretAccessor on
    swarm-tenant-<t>-plg-* secrets and their versions (plugin_secrets.tf,
    docs/design/managed-plugins.md §6.2), so it can release a plugin's secret
    to an attempt whose signed plugin set names it. Off by default: nothing
    reads a -plg- value until the plugin release is built, and a read grant
    nobody uses is only exposure. The account must exist (terraform/infra
    creates swarm-api).
  EOT
  type        = bool
  default     = false
}

data "google_project" "plugin_secrets" {
  count      = var.enable_plugin_secrets ? 1 : 0
  project_id = var.project_id
}

locals {
  plugin_secrets_on = var.enable_plugin_secrets ? 1 : 0

  # The project number, for the condition. "" while off, when nothing reads it.
  plugin_secrets_project_number = var.enable_plugin_secrets ? data.google_project.plugin_secrets[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam).
  plugin_secrets_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # A plugin secret by its full name, with a non-empty tenant before the first
  # `-plg-` (the header). terraform/infra/plugins.tf's plugin_secret_condition.
  plugin_secrets_condition = "resource.name.startsWith(\"projects/${local.plugin_secrets_project_number}/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-plg-\") != \"\""
}

resource "google_project_iam_member" "plugin_secret_reader" {
  count = local.plugin_secrets_on

  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = local.plugin_secrets_api_member

  condition {
    title       = "swarm plugin secrets read"
    description = "managed-by=swarm-terraform; swarm-api reads plugin secrets swarm-tenant-<t>-plg-* and their versions to release them per attempt (managed-plugins.md section 6.2). No tenant worker holds this."
    expression  = local.plugin_secrets_condition
  }
}

output "plugin_secret_grants" {
  description = "What swarm-api's plugin-secret read is granted on, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_plugin_secrets is false."
  value = {
    api_read = var.enable_plugin_secrets ? local.plugin_secrets_condition : ""
  }
}
