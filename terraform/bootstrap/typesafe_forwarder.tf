# ---------------------------------------------------------------------------
# The TypeSafe forwarder's read of TypeSafe keys
# (docs/design/workspace-secrets.md §3.5, the `inject` delivery mode; owner
# decision §10 Q8; lane SEC-INJECT-A, part of #1347).
# ---------------------------------------------------------------------------
#
# The forwarder (kubernetes/swarm-system/swarm-typesafe-fwd.yaml, its code
# lane SEC-INJECT-B) adds a TypeSafe key to an agent pod's judgment request so
# the key never enters the pod. It reads exactly the secret version an attempt
# ticket names -- `swarm-tenant-<t>-typesafe`, a person's
# (`swarm-tenant-u-<user>-typesafe`) or a workspace's -- with its OWN identity,
# the Workload Identity Federation for GKE principal of the KSA
# `swarm-typesafe-fwd` in `swarm-system` (terraform/infra/typesafe_fwd.tf).
# Which key a ticket names is swarm-api's decision (resolve_provider_key),
# MAC'd so the forwarder cannot be talked into another.
#
#   google_project_iam_custom_role.typesafe_forwarder_accessor
#       swarmTypesafeForwarderAccessor: secretmanager.versions.access and no
#       other permission. Not roles/secretmanager.secretAccessor, whose
#       contents are Google's to change. No versions.get or list: a ticket
#       names its version (a number, or `latest`).
#   google_project_iam_member.typesafe_forwarder_accessor
#       the forwarder's principal holds it, under one condition (below).
#
# THE CONDITION, one expression and no list:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-typesafe/versions/") != ""
#
#   * The project NUMBER: IAM evaluates Secret Manager names with it, and the
#     project ID "can't be substituted" there (workspace_secrets.tf says where
#     that is documented).
#   * extract(), never matches() (refused on some services;
#     terraform/modules/iam/bindings.tf), the form workspace_secrets.tf,
#     browser_logins.tf and forge_user_slots.tf use. extract("pre{t}suf") is
#     the text between the first `pre` and the first `suf` after it, "" when
#     either is missing.
#   * `-typesafe/versions/` as the suffix, not `-typesafe`. versions.access is
#     evaluated on the VERSION, `projects/<n>/secrets/<id>/versions/<v>`
#     (forge_user_slots.tf: "WHY BOTH NAME FORMS"), so the `/versions/` after
#     the suffix pins `-typesafe` to the END of the secret id. With `-typesafe`
#     alone, `swarm-tenant-a-typesafe-anything` would be admitted. The bare
#     secret name (no version) is refused, which costs nothing: the role holds
#     no permission evaluated on a secret.
#   * No hasOnly() and no tenant list: personal tenants the workspace job
#     creates outside Terraform hold keys too (docs/provider-keys.md), and a
#     list would meet hasOnly()'s 10-item limit (deployer_conditions.tf, #275).
#
# WHAT IT REFUSES. A tenant's forge token (`-git`) and its `-git-u-`/`-git-r-`
# slots, its workspace secrets (`-wsec-<hex>`: hex never spells `typesafe`),
# its other provider keys (`-anthropic`, ...), an empty tenant, the platform's
# swarm-github-app-* and swarm-account-* secrets, and the other team's
# agents-* and promptlab-* secrets, which do not start with the prefix.
# tests/terraform/typesafe_forwarder.tftest.hcl evaluates each.
#
# WHAT IT STILL REACHES (the residual, for the security review). Any secret
# under the prefix whose id ENDS in `-typesafe` after a non-empty tenant: a
# saved browser login named `typesafe` or `<x>-typesafe`
# (the browser-login secret id for login `typesafe`, which browser_logins.tf
# spells; browser_logins.NAME accepts it today). The forwarder reads only the secret a verified ticket names, and
# swarm-api mints only `Tenant.secret_name("typesafe")`, so its code never
# names one; closing it in IAM needs matches() or a list. The fix belongs where
# login names are made (refuse a login name ending `typesafe`), which is apps/,
# outside this lane; it is in the lane's pull request.
#
# THE TENANT GSA. Nothing here names a worker account. Under `inject` the
# worker reads no TypeSafe key at all; the worker's own grants on `-typesafe`
# (credentials.py's per-secret binding, typesafe_user_keys.tf) are retired by
# SEC-RETIRE once SEC-MIGRATE-TYPESAFE moves jev and the judge here.
#
# WHY HERE AND NOT IN terraform/infra. A custom role cannot be defined there
# (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl), and a role
# carrying versions.access on the CI deployer's grantable list would let CI
# grant itself read on any secret in saga-agents-staging, the other team's
# among them. So the OWNER applies this (dev-iam).
#
# NOT VERIFIED LIVE: that IAM evaluates versions.access with resource.name in
# the version form and that extract() answers as above for it, and that IAM
# accepts a `principal://` member under a condition on a project binding.
# Lint the condition with iamPolicies:lintPolicy before applying, and show it
# refuses a broken expression first, as docs/provider-keys.md §6 did.
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so it can match none of them. The grant is to the forwarder's
# principal alone and touches no cluster, network, bucket or account of theirs.

variable "enable_typesafe_forwarder_read" {
  description = <<-EOT
    Define swarmTypesafeForwarderAccessor (secretmanager.versions.access only)
    and grant it to the TypeSafe forwarder's Workload Identity principal
    (KSA swarm-typesafe-fwd in swarm-system) on versions of
    swarm-tenant-<t>-typesafe secrets (typesafe_forwarder.tf; design
    docs/design/workspace-secrets.md §3.5). Lane SEC-INJECT-A, part of #1347.
    Off by default: a read grant nobody uses is only exposure. Turn it on
    before terraform/infra's typesafe_forwarder.serve.
  EOT
  type        = bool
  default     = false
}

data "google_project" "typesafe_forwarder" {
  count      = var.enable_typesafe_forwarder_read ? 1 : 0
  project_id = var.project_id
}

locals {
  typesafe_forwarder_on = var.enable_typesafe_forwarder_read ? 1 : 0

  # The project number, for the condition and the principal. "" while off.
  typesafe_forwarder_project_number = var.enable_typesafe_forwarder_read ? data.google_project.typesafe_forwarder[0].number : ""

  # Named, not read: the KSA is kubernetes/swarm-system/swarm-typesafe-fwd.yaml's,
  # and terraform/infra/typesafe_fwd.tf spells the same principal.
  typesafe_forwarder_member = "principal://iam.googleapis.com/projects/${local.typesafe_forwarder_project_number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/swarm-system/sa/swarm-typesafe-fwd"

  # A version of a tenant's TypeSafe key, with a non-empty tenant before the
  # `-typesafe` that ends the secret id (the header).
  typesafe_forwarder_condition = "resource.name.startsWith(\"projects/${local.typesafe_forwarder_project_number}/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-typesafe/versions/\") != \"\""
}

resource "google_project_iam_custom_role" "typesafe_forwarder_accessor" {
  count = local.typesafe_forwarder_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["typesafe_forwarder_accessor"]
  title       = "Swarm TypeSafe Forwarder Accessor"
  description = "managed-by=swarm-terraform; the TypeSafe forwarder reads the swarm-tenant-<t>-typesafe version an attempt ticket names, so the key never enters an agent pod (SEC-INJECT-A, #1347). versions.access only."
  stage       = "GA"

  permissions = [
    "secretmanager.versions.access",
  ]
}

resource "google_project_iam_member" "typesafe_forwarder_accessor" {
  count = local.typesafe_forwarder_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.typesafe_forwarder_accessor[0].role_id}"
  member = local.typesafe_forwarder_member

  condition {
    title       = "swarm typesafe forwarder"
    description = "managed-by=swarm-terraform; the TypeSafe forwarder reads versions of swarm-tenant-<t>-typesafe secrets, for every workspace and person, to add the key to a ticketed request (SEC-INJECT-A, #1347). No tenant worker holds this."
    expression  = local.typesafe_forwarder_condition
  }
}

output "typesafe_forwarder_grants" {
  description = "What the TypeSafe forwarder's key read is granted on and to whom, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_typesafe_forwarder_read is false."
  value = {
    member = var.enable_typesafe_forwarder_read ? local.typesafe_forwarder_member : ""
    read   = var.enable_typesafe_forwarder_read ? local.typesafe_forwarder_condition : ""
  }
}
