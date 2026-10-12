# ---------------------------------------------------------------------------
# Signed-in browser sessions: the IAM that keeps them a person's own
# (docs/design/live-browser.md LB-7 and §5.5.1; lane L8b of #1030).
# ---------------------------------------------------------------------------
#
# In a personal tenant, `u-<user>`, a browser session signed in once is kept as
# one Secret Manager secret:
#
#   swarm-tenant-u-<user>-browser-wf-<workflow>   a workflow's carry, 24-hour TTL
#   swarm-tenant-u-<user>-browser-login-<name>    a named login, at most 30 days
#
# IAM IS THE WHOLE BOUNDARY. Every worker of a group tenant runs as one account
# (`swarm-agent-worker-eng`), so no grant could let Alice's agents read a
# session while refusing Bob's. A personal tenant's worker is that person's
# alone.
#
# SWARM-API PROVISIONS EACH SECRET AND GRANTS IT PER SECRET (owner decision
# 2026-10-11, lane L8c, answering the question L8b asked). Before a worker
# writes a session it asks swarm-api for the slot
# (`POST /v1/tasks/<task>/browser-session/slot`, signed with the attempt key
# and fenced on the generation). swarm-api creates the secret with its TTL or
# expiry and labels, then sets THAT SECRET's IAM policy to exactly two
# bindings: secretAccessor and secretVersionAdder, each to
# `swarm-agent-worker-<tenant>` alone. So:
#
#   NO WORKER HOLDS ANY PROJECT-LEVEL -browser- GRANT. L8b's
#       browser_session_reader, browser_session_writer and the unconditioned
#       browser_session_creator (swarmBrowserSessionCreator, secrets.create)
#       are gone. A worker never holds secrets.create, so a worker can no
#       longer create an empty secret anywhere in the shared project.
#   google_project_iam_member.browser_session_provisioner   swarm-api,
#       swarmBrowserSessionProvisioner: secrets.create, secrets.get,
#       secrets.getIamPolicy, secrets.setIamPolicy.
#   google_project_iam_member.browser_login_revoker         swarm-api,
#       swarmBrowserLoginRevoker, unchanged: Revoke and the carry's delete.
#
# WHY NOT TERRAFORM, OR A WORKSPACE-JOB GRANT. (1) Terraform cannot name a
# person: a workspace made by the workspace job is a tenant outside Terraform
# (docs/workspaces.md §3.2, WD3), so a per-worker grant here reached only the
# tfvars' `kind = "user"` tenants and never the people who use the feature.
# (2) secrets.create is checked against the PROJECT, which carries no secret
# name, so a project-level create cannot be narrowed to a person's prefix
# (forge_user_slots.tf records the same finding). (3) A per-person project IAM
# grant made by the workspace job would need project setIamPolicy, and a
# hasOnly()-bounded grant of that bounds WHICH roles the job may grant, never
# TO WHOM or under what condition.
#
# WHAT THE PROVISIONER'S CONDITION NARROWS, AND WHAT IT CANNOT. The grant's
# condition has two arms:
#
#   * the revoker's own name test -- `projects/<n>/secrets/swarm-tenant-u-`
#     and a non-empty tenant before the first `-browser-`, by extract() (IAM
#     refuses matches() on some services). secrets.get, secrets.getIamPolicy
#     and secrets.setIamPolicy are evaluated against the secret, so this arm
#     NARROWS them: swarm-api can read the metadata of, and rewrite the IAM
#     policy of, personal tenants' -browser- secrets and no others -- never a
#     provider key, a forge token, the platform's or the other team's.
#   * `resource.type == "cloudresourcemanager.googleapis.com/Project"`.
#     secrets.create is evaluated against the project, where the name arm
#     can never hold, so this arm is what lets create through at all, and the
#     condition CANNOT narrow create: swarm-api may create an empty secret with
#     any name. That is the residual, and it is swarm-api's, not every
#     worker's. The other three permissions do nothing on a project resource.
#     swarm-api's own code refuses any name outside the caller's
#     `swarm-tenant-u-<user>-browser-(wf|login)-` before it calls Secret
#     Manager (apps/swarm-api/swarm_api/browser_slots.py).
#
# A GROUP TENANT GETS NOTHING. swarm-api refuses a slot to any tenant that is
# not personal: `kind = "user"` AND an id starting `u-`. A group whose id
# happens to start `u-`, and a `kind = "user"` service tenant whose id does
# not, are refused.
#
# WHY HERE AND NOT IN terraform/infra (where the build plan's row put this
# file). Every project-level role terraform/infra grants must be on the CI
# deployer's scoped projectIamAdmin list (deployer_conditions.tf,
# deployer_grantable_project_roles), and hasOnly() limits WHICH roles CI may
# grant, never to whom or under what condition. A custom role carrying
# secrets.setIamPolicy on that list would let CI grant itself the means to
# rewrite any secret's policy in saga-agents-staging, the other team's among
# them (#69). And custom roles cannot be defined there at all (#79;
# tests/terraform/custom_roles_out_of_ci.tftest.hcl). So the owner applies
# this.
#
# NOT VERIFIED LIVE: that IAM evaluates resource.name for secrets.get,
# secrets.getIamPolicy, secrets.setIamPolicy, secrets.delete, versions.destroy
# and versions.list in the full `projects/<n>/secrets/<id>[/versions/<v>]`
# form; that secrets.create is evaluated with resource.type
# `cloudresourcemanager.googleapis.com/Project`; and that extract() answers as
# tests/terraform/browser_logins.tftest.hcl emulates. The mock-provider tests
# hold the expressions' text and their behaviour under that emulation, not
# IAM's answer.
#
# THE OTHER TEAM'S RESOURCES. Every name arm starts
# `projects/<n>/secrets/swarm-tenant-u-`; their secrets are agents-* and
# promptlab-*, so no name arm can match one. The create arm can create a
# secret with their prefix, and can do nothing else to it: no read, no
# version, and setIamPolicy only where the name arm holds.

variable "enable_browser_logins" {
  description = <<-EOT
    Define swarmBrowserSessionProvisioner and swarmBrowserLoginRevoker and
    grant them to swarm-api (browser_logins.tf): swarm-api creates each
    personal tenant's swarm-tenant-u-<user>-browser- secret, grants that
    tenant's worker read and version-add on that one secret, and destroys it
    on Revoke or at its workflow's end. Live browser LB-7, lanes L8b and L8c
    of #1030. No worker holds a project-level grant for it. Off until the
    worker that writes sessions (lane L4c) and swarm-api's slot and revoke
    routes (lanes L8c, L9b) are deployed: a grant nobody uses is only
    exposure. The account must exist (terraform/infra creates swarm-api).
  EOT
  type        = bool
  default     = false
}

data "google_project" "browser_logins" {
  count      = var.enable_browser_logins ? 1 : 0
  project_id = var.project_id
}

locals {
  browser_logins_on = var.enable_browser_logins ? 1 : 0

  # The project number, for the conditions: IAM evaluates Secret Manager names
  # with the number, and the project ID "can't be substituted" there. "" while
  # off, when nothing reads it.
  browser_project_number = var.enable_browser_logins ? data.google_project.browser_logins[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam).
  browser_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # Every personal tenant's -browser- secrets, including workspaces made
  # outside Terraform: the `swarm-tenant-u-` prefix and a non-empty tenant
  # between it and the first `-browser-`, in either resource-name form.
  browser_revoke_condition = "resource.name.startsWith(\"projects/${local.browser_project_number}/secrets/swarm-tenant-u-\") && resource.name.extract(\"/secrets/swarm-tenant-u-{tenant}-browser-\") != \"\""

  # The provisioner's: the same name test, or the project itself, which is
  # what secrets.create is evaluated against (the header: the create arm is
  # the one this condition cannot narrow).
  browser_provision_condition = "resource.type == \"cloudresourcemanager.googleapis.com/Project\" || (${local.browser_revoke_condition})"
}

resource "google_project_iam_custom_role" "browser_session_provisioner" {
  count = local.browser_logins_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["browser_session_provisioner"]
  title       = "Swarm Browser Session Provisioner"
  description = "managed-by=swarm-terraform; swarm-api creates a personal browser session secret and grants its tenant worker on it. No direct versions.access, but setIamPolicy lets it grant read to anyone, itself too. Detection deferred to epic 476."
  stage       = "GA"

  # create, with its TTL or expiry and labels as fields of the create call;
  # get, to read back a secret that already exists (a refreshed login, a
  # second step of a workflow); getIamPolicy and setIamPolicy, to write the
  # secret's own two bindings under the policy's etag. Not
  # roles/secretmanager.admin, which reads every value. swarm-api holds no
  # direct versions.access here, but that is NOT the same as being unable to
  # read: setIamPolicy on a personal -browser- secret is effectively
  # versions.access on it, because swarm-api can grant read to any member,
  # itself included. The name condition bounds WHICH secrets, not WHO it may
  # grant. Accepted residual (owner decision: the capability stays); detection
  # is deferred to the security epic #476, an alert on SetIamPolicy whose new
  # bindings name anything other than swarm-agent-worker-u-*
  # (https://github.com/bogdan-alexandrescu/SwarmCloud/issues/476#issuecomment-6114767079).
  # A separate role from swarmBrowserLoginRevoker, so the plan shows the
  # IAM-writing permission on its own line.
  permissions = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "browser_login_revoker" {
  count = local.browser_logins_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["browser_login_revoker"]
  title       = "Swarm Browser Login Revoker"
  description = "managed-by=swarm-terraform; swarm-api revokes a named browser login and deletes a workflow's session carry (live browser LB-7, #1030). No access, no version add, no IAM."
  stage       = "GA"

  # Revoke destroys every version (versions.list to find them,
  # versions.destroy), and a carry is deleted whole at its workflow's terminal
  # state (secrets.delete); secrets.get reads the metadata both start from.
  # Not roles/secretmanager.admin, which reads every value, and not
  # secretVersionManager, which adds versions: this role has no
  # IAM permission and no versions.access, so it cannot read a jar (the
  # provisioner above can, through setIamPolicy).
  permissions = [
    "secretmanager.secrets.delete",
    "secretmanager.secrets.get",
    "secretmanager.versions.destroy",
    "secretmanager.versions.list",
  ]
}

resource "google_project_iam_member" "browser_session_provisioner" {
  count = local.browser_logins_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.browser_session_provisioner[0].role_id}"
  member = local.browser_api_member

  condition {
    title       = "swarm browser sessions provision"
    description = "managed-by=swarm-terraform; swarm-api creates personal tenants' browser session secrets and sets each one's IAM to its own worker (live browser LB-7, #1030). The name arm narrows get and IAM; create is evaluated on the project and is not narrowed."
    expression  = local.browser_provision_condition
  }
}

resource "google_project_iam_member" "browser_login_revoker" {
  count = local.browser_logins_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.browser_login_revoker[0].role_id}"
  member = local.browser_api_member

  condition {
    title       = "swarm browser sessions personal"
    description = "managed-by=swarm-terraform; swarm-api revokes personal tenants' named browser logins and deletes their workflow session carries (live browser LB-7, #1030)."
    expression  = local.browser_revoke_condition
  }
}

output "browser_login_grants" {
  description = "What the browser-session grants are made on, for the owner to read a plan against: swarm-api's provision and revoke conditions. No worker holds a project-level browser grant; swarm-api grants each worker on its own secret. Empty while enable_browser_logins is false."
  value = {
    api_provision = var.enable_browser_logins ? local.browser_provision_condition : ""
    api_revoke    = var.enable_browser_logins ? local.browser_revoke_condition : ""
  }
}
