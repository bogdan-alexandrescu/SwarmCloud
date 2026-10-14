# ---------------------------------------------------------------------------
# Tenant provider keys: swarm-api's Secret Manager grant (docs/provider-keys.md
# §6; owner decision 2026-10-14, lane KEYS-IAM).
# ---------------------------------------------------------------------------
#
# A tenant's provider key is one Secret Manager secret,
# `swarm-tenant-<tenant>-<provider>` (Tenant.secret_name in the frozen
# contract). swarm-api writes it, through credentials.SecretManagerCredentials,
# for `POST /v1/tenants/me/credentials` and the TypeSafe key routes
# (`PUT`/`DELETE /v1/provider-keys/typesafe/{user,tenant}`, KEYS-API, PR 1284).
# KEYS-API measured that swarm-api's account held none of the permissions that
# write needs, so every one of those routes answered 503 when deployed. The
# owner's answer, 2026-10-14: "Yes, scoped bootstrap role".
#
#   google_project_iam_custom_role.provider_key_writer   swarmProviderKeyWriter:
#       exactly the permissions credentials.py's calls need, and no other:
#
#         create_secret           secretmanager.secrets.create
#         get_iam_policy          secretmanager.secrets.getIamPolicy
#         set_iam_policy          secretmanager.secrets.setIamPolicy
#         add_secret_version      secretmanager.versions.add
#         list_secret_versions    secretmanager.versions.list
#         destroy_secret_version  secretmanager.versions.destroy
#
#       NOT secrets.get: credentials.py never reads a secret's metadata. A
#       second write to an existing secret is create_secret answering
#       ALREADY_EXISTS, which create authorises on the project. NOT
#       versions.access: the module has no read path, by design. NOT
#       secrets.delete: a deleted key keeps its secret and its IAM binding, so
#       setting it again is the ordinary write. tests/terraform/
#       provider_keys.tftest.hcl reads credentials.py and fails when a call is
#       added or removed there without this list following.
#   google_project_iam_member.provider_key_writer        swarm-api holds it,
#       under one condition (below).
#
# WHAT THE CONDITION NARROWS, AND WHAT IT CANNOT. Two arms:
#
#   * `resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")`.
#     The project NUMBER: IAM evaluates Secret Manager names with it, and the
#     project ID "can't be substituted" there (docs.cloud.google.com/iam/docs/
#     conditions-resource-attributes). getIamPolicy, setIamPolicy and the three
#     versions.* permissions are evaluated against the secret or the version
#     (`.../secrets/<id>/versions/<v>`, which starts with the same prefix), so
#     this arm NARROWS them to the platform's tenant secrets: never
#     swarm-github-app-*, never the other team's agents-* or promptlab-*.
#   * `resource.type == "cloudresourcemanager.googleapis.com/Project"`.
#     secrets.create is evaluated against the PARENT PROJECT, whose name
#     carries no secret id, so no resource-name condition can gate it: the
#     name arm alone would refuse every create and the routes would still
#     503. This arm is what lets create through, and the condition CANNOT
#     narrow create: swarm-api may create an empty secret with any name. It
#     cannot read, version, or set IAM on one outside the name arm. The other
#     five permissions are never checked against a project, so this arm
#     grants them nothing. browser_logins.tf's provisioner and
#     forge_user_slots.tf's unconditioned swarmForgeSlotCreator record the
#     same finding; while either is on, swarm-api already holds this create,
#     so this grant adds none.
#
# WHY NOT PRE-CREATED SECRETS, which would drop secrets.create and its
# residual. Terraform would have to name every key's secret, and it cannot:
# a personal tenant `u-<user>` is made by the workspace job outside Terraform
# (docs/workspaces.md §3.2), and `PUT /v1/provider-keys/typesafe/user` writes
# into exactly those. credentials.py would also have to treat a missing secret
# as an error rather than create it (apps/, not this lane). So the create
# residual is the narrowest design that lets a person store their own key.
#
# WHAT THE NAME ARM STILL REACHES (the residual the owner accepted with
# "swarm-tenant-*"). Every secret under the prefix, not only provider keys: a
# tenant's forge token (`-git`), its GitHub user and repository slots, its
# browser sessions. On any of them swarm-api may add a version, destroy
# versions and rewrite the IAM policy -- and setIamPolicy is effectively
# versions.access, because swarm-api could grant read to any member, itself
# included. swarm-api's own code refuses the forge and App providers before it
# calls Secret Manager (credentials.refuse_unbindable_provider), and only
# names `Tenant.secret_name(provider)` for an allow-listed provider. Narrowing
# the condition to the provider suffixes would restate validation's provider
# lists here, where nothing holds them in step; the question is in the
# KEYS-IAM pull request for the owner.
#
# WHY HERE AND NOT IN terraform/infra. A custom role cannot be defined there
# (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl), and a role carrying
# secrets.setIamPolicy on the CI deployer's grantable list
# (deployer_conditions.tf) would let CI grant itself the means to rewrite any
# secret's policy in saga-agents-staging, the other team's among them (#69):
# hasOnly() limits WHICH roles CI may grant, never to whom or under what
# condition. So the owner applies this (dev-iam).
#
# NOT VERIFIED LIVE: that IAM evaluates resource.name for getIamPolicy,
# setIamPolicy, versions.add, versions.list and versions.destroy in the full
# `projects/<n>/secrets/<id>[/versions/<v>]` form; that secrets.create is
# evaluated with resource.type `cloudresourcemanager.googleapis.com/Project`;
# and that create on an existing secret answers ALREADY_EXISTS without
# secrets.get. The mock-provider tests hold the expression's text and its
# behaviour under startsWith(), not IAM's answer. Lint the condition with
# iamPolicies:lintPolicy before applying (docs/provider-keys.md §6).
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so it can match none of them. The create arm can create a
# secret with their prefix, and can do nothing else to it.

variable "enable_provider_keys" {
  description = <<-EOT
    Define swarmProviderKeyWriter and grant it to swarm-api (provider_keys.tf):
    swarm-api creates a tenant's swarm-tenant-<tenant>-<provider> secret,
    binds the tenant's worker to read it, adds the key as a version, and
    destroys its versions on delete. Owner decision 2026-10-14, lane KEYS-IAM.
    Off by default: a grant nobody uses is only exposure. The account must
    exist (terraform/infra creates swarm-api).
  EOT
  type        = bool
  default     = false
}

data "google_project" "provider_keys" {
  count      = var.enable_provider_keys ? 1 : 0
  project_id = var.project_id
}

locals {
  provider_keys_on = var.enable_provider_keys ? 1 : 0

  # The project number, for the condition. "" while off, when nothing reads it.
  provider_keys_project_number = var.enable_provider_keys ? data.google_project.provider_keys[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam).
  provider_keys_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # The project, where secrets.create is evaluated, or a tenant secret by its
  # full name (the header: the create arm is the one this cannot narrow).
  # Literal "swarm-tenant-": Tenant.secret_name's spelling.
  provider_keys_condition = "resource.type == \"cloudresourcemanager.googleapis.com/Project\" || resource.name.startsWith(\"projects/${local.provider_keys_project_number}/secrets/swarm-tenant-\")"
}

resource "google_project_iam_custom_role" "provider_key_writer" {
  count = local.provider_keys_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["provider_key_writer"]
  title       = "Swarm Provider Key Writer"
  description = "managed-by=swarm-terraform; swarm-api stores and destroys tenant provider keys (KEYS-IAM). No versions.access, but setIamPolicy lets it grant read on swarm-tenant-* secrets to anyone, itself too."
  stage       = "GA"

  # credentials.py's calls, one each (the header's table). Not
  # roles/secretmanager.admin, which reads every value in the shared project.
  permissions = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
    "secretmanager.versions.add",
    "secretmanager.versions.destroy",
    "secretmanager.versions.list",
  ]
}

resource "google_project_iam_member" "provider_key_writer" {
  count = local.provider_keys_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.provider_key_writer[0].role_id}"
  member = local.provider_keys_api_member

  condition {
    title       = "swarm tenant provider keys"
    description = "managed-by=swarm-terraform; swarm-api stores and destroys tenant provider keys on swarm-tenant-* secrets (KEYS-IAM). The name arm narrows IAM and versions; create is evaluated on the project and is not narrowed."
    expression  = local.provider_keys_condition
  }
}

output "provider_key_grants" {
  description = "What swarm-api's provider-key grant is made on, for the owner to read a plan against and to lint (iamPolicies:lintPolicy). Empty while enable_provider_keys is false."
  value = {
    api_write = var.enable_provider_keys ? local.provider_keys_condition : ""
  }
}
