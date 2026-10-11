# ---------------------------------------------------------------------------
# Signed-in browser sessions: the IAM that keeps them a person's own
# (docs/design/live-browser.md LB-7 and §5.5.1; lane L8b of #1030).
# ---------------------------------------------------------------------------
#
# In a personal tenant, `u-<user>`, a browser session signed in once is kept as
# one Secret Manager secret, written by the worker at run time:
#
#   swarm-tenant-u-<user>-browser-wf-<workflow>   a workflow's carry, 24-hour TTL
#   swarm-tenant-u-<user>-browser-login-<name>    a named login, at most 30 days
#
# IAM IS THE WHOLE BOUNDARY. Every worker of a group tenant runs as one account
# (`swarm-agent-worker-eng`), so no grant could let Alice's agents read a
# session while refusing Bob's. A personal tenant's worker is that person's
# alone. So:
#
#   google_project_iam_member.browser_session_reader   the personal worker,
#       secretAccessor, on `projects/<n>/secrets/swarm-tenant-<t>-browser-`:
#       the next step of a workflow, or a task naming `input.browser_login`,
#       imports the jar.
#   google_project_iam_member.browser_session_writer   the same worker,
#       secretVersionAdder, on the same prefix: a refreshed login is a new
#       version.
#   google_project_iam_member.browser_session_creator  the same worker,
#       swarmBrowserSessionCreator (secrets.create alone), UNCONDITIONED.
#       Creation is checked against the project, which carries no secret name,
#       so a name condition cannot narrow it (forge_user_slots.tf, the same
#       finding for swarm-api's swarmForgeSlotCreator). What it buys is an empty
#       secret anywhere in the project: no read, version, delete or IAM on it,
#       this platform's or the other team's.
#   google_project_iam_member.browser_login_revoker    swarm-api,
#       swarmBrowserLoginRevoker, on every personal tenant's `-browser-`
#       secrets: Revoke destroys every version of a named login, and a
#       workflow's carry is deleted at its terminal state. No versions.access:
#       swarm-api never returns a value, so it never holds one.
#
# NO GROUP TENANT HOLDS ANY OF IT. A tenant is personal when it is
# `kind = "user"` AND its id starts `u-` (identity.tenant_id_for_user's shape,
# the prefix forge_user_slots.tf's personal block keys on). A group whose id
# happens to start `u-`, and a `kind = "user"` service tenant whose id does not,
# get nothing. swarm-api's condition admits only `swarm-tenant-u-...-browser-`.
#
# WHY HERE AND NOT IN terraform/infra (where the build plan's row put this
# file). Every project-level role terraform/infra grants must be on the CI
# deployer's scoped projectIamAdmin list (deployer_conditions.tf,
# deployer_grantable_project_roles), and hasOnly() limits WHICH roles CI may
# grant, never to WHOM or under what condition. secretAccessor on that list
# would let CI grant itself read of every secret in saga-agents-staging, the
# other team's among them -- the reason forge_user_slots.tf and the broker's
# swarmSecretLister grant live here (#69). And the two custom roles cannot be
# defined there at all (#79; tests/terraform/custom_roles_out_of_ci.tftest.hcl).
# So the owner applies this, like every other secret grant to a worker.
#
# PERSONAL WORKSPACES ARE NOT IN TERRAFORM (docs/workspaces.md §3.2, WD3), so
# the per-tenant worker grants above reach only the `kind = "user"` tenants
# the release's tfvars names -- today u-sw-c90291, swarm-verify's. A person's
# workspace made by the workspace job gets swarm-api's revoke grant (it names
# no person), but not the worker's three: a project-level grant per person is
# a project setIamPolicy the job does not hold, and holding it under hasOnly()
# would bound the role and never the member. How a person's worker gets its
# grant is the owner's question, asked in the L8b pull request.
#
# NOT VERIFIED LIVE: that IAM evaluates resource.name for versions.access,
# versions.add, versions.destroy, versions.list, secrets.get and
# secrets.delete in the full `projects/<n>/secrets/<id>[/versions/<v>]` form,
# and that extract() answers as tests/terraform/browser_logins.tftest.hcl
# emulates. The mock-provider tests hold the expressions' text and their
# behaviour under that emulation, not IAM's answer.
#
# THE OTHER TEAM'S RESOURCES. Every condition starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so no condition here can match one.

variable "enable_browser_logins" {
  description = <<-EOT
    Define swarmBrowserSessionCreator and swarmBrowserLoginRevoker and make the
    browser-session grants (browser_logins.tf): each personal tenant's worker
    creates, writes and reads its own swarm-tenant-<t>-browser- secrets, and
    swarm-api destroys any personal tenant's. Live browser LB-7, lane L8b of
    #1030. Off until the worker that writes sessions (lane L4c) and the route
    that revokes them (lane L9b) are deployed: a grant nobody uses is only
    exposure. The accounts must exist (terraform/infra creates swarm-api and
    each tenant's worker).
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

  # Each tenant's block of the same tfvars text deployer_service_accounts.tf
  # parses for its ids: a two-space `name = {` line through the two-space `}`
  # that closes it. The kind is read from the block's own four-space line, so
  # a nested object's `kind` is never taken for the tenant's.
  browser_tenant_blocks = {
    for m in regexall("(?ms)^  \"?([a-z0-9][a-z0-9-]*)\"?[ \\t]*=[ \\t]*\\{[ \\t]*$(.*?)^  \\}[ \\t]*$", local.infra_tenants_body) :
    m[0] => m[1]
  }

  browser_tenant_kinds = {
    for t, body in local.browser_tenant_blocks :
    t => one(concat([for k in regexall("(?m)^    kind[ \\t]*=[ \\t]*\"([a-z]+)\"", body) : k[0]], []))
  }

  # Personal: kind = "user" and a `u-` id. Both, so neither a group nor a
  # service tenant is ever taken for a person's.
  browser_login_tenants = var.enable_browser_logins ? toset([
    for t, k in local.browser_tenant_kinds : t if k == "user" && startswith(t, "u-")
  ]) : toset([])

  browser_session_prefixes = {
    for t in local.browser_login_tenants :
    t => "projects/${local.browser_project_number}/secrets/swarm-tenant-${t}-browser-"
  }

  # startsWith alone covers the secret and its versions
  # (`.../swarm-tenant-<t>-browser-<x>/versions/<v>`) alike.
  browser_session_conditions = {
    for t, p in local.browser_session_prefixes : t => "resource.name.startsWith(\"${p}\")"
  }

  # A personal tenant whose browser secrets start with another's prefix --
  # `u-a` and `u-a-browser-x` -- would have its sessions readable by the other's
  # worker. Read from the ids, which are known at plan.
  #
  # UNREACHABLE TODAY, and kept: the nesting tenant's id is at least 13
  # characters, so its worker `swarm-agent-worker-<t>` is at least 32, past the
  # 30 a service account id allows, and modules/service_account_ids refuses
  # the plan first. That is why tests/terraform/browser_logins.tftest.hcl has
  # no run for it. A shorter worker prefix there would make it reachable, and
  # this is what then stops the plan.
  browser_nested_tenants = [
    for pair in setproduct(local.browser_login_tenants, local.browser_login_tenants) :
    "${pair[1]} inside ${pair[0]}"
    if pair[0] != pair[1] && startswith("swarm-tenant-${pair[1]}-browser-", "swarm-tenant-${pair[0]}-browser-")
  ]

  # Every personal tenant's -browser- secrets, including workspaces made
  # outside Terraform: the `swarm-tenant-u-` prefix and a non-empty tenant
  # between it and the first `-browser-`, in either resource-name form.
  browser_revoke_condition = "resource.name.startsWith(\"projects/${local.browser_project_number}/secrets/swarm-tenant-u-\") && resource.name.extract(\"/secrets/swarm-tenant-u-{tenant}-browser-\") != \"\""
}

resource "google_project_iam_custom_role" "browser_session_creator" {
  count = local.browser_logins_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["browser_session_creator"]
  title       = "Swarm Browser Session Creator"
  description = "managed-by=swarm-terraform; a personal tenant's worker creates its signed-in browser session secret (live browser LB-7, #1030). Create only: no read, no version, no delete, no IAM."
  stage       = "GA"

  # secretmanager.secrets.create alone; the TTL and labels are fields of the
  # create call. A separate role from swarmForgeSlotCreator, though the same
  # permission, so switching either feature off removes only its own grant.
  permissions = ["secretmanager.secrets.create"]
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
  # secretVersionManager, which adds versions: swarm-api never holds a jar.
  permissions = [
    "secretmanager.secrets.delete",
    "secretmanager.secrets.get",
    "secretmanager.versions.destroy",
    "secretmanager.versions.list",
  ]
}

resource "google_project_iam_member" "browser_session_creator" {
  for_each = local.browser_login_tenants

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.browser_session_creator[0].role_id}"
  member = "serviceAccount:${module.service_account_ids.worker_ids[each.key]}@${var.project_id}.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "browser_session_reader" {
  for_each = local.browser_login_tenants

  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${module.service_account_ids.worker_ids[each.key]}@${var.project_id}.iam.gserviceaccount.com"

  condition {
    title       = "swarm browser sessions ${each.key}"
    description = "managed-by=swarm-terraform; personal tenant ${each.key}'s worker reads its own signed-in browser sessions (live browser LB-7, #1030)."
    expression  = local.browser_session_conditions[each.key]
  }

  lifecycle {
    precondition {
      condition     = length(local.browser_nested_tenants) == 0
      error_message = "tenant ids ${join(", ", local.browser_nested_tenants)}: one personal tenant's browser-session names start with another's prefix (swarm-tenant-<t>-browser-), so that tenant's worker would read them. Rename the tenant."
    }
  }
}

resource "google_project_iam_member" "browser_session_writer" {
  for_each = local.browser_login_tenants

  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = "serviceAccount:${module.service_account_ids.worker_ids[each.key]}@${var.project_id}.iam.gserviceaccount.com"

  condition {
    title       = "swarm browser sessions ${each.key}"
    description = "managed-by=swarm-terraform; personal tenant ${each.key}'s worker writes its own signed-in browser sessions: a workflow's carry, a refreshed login (live browser LB-7, #1030)."
    expression  = local.browser_session_conditions[each.key]
  }

  lifecycle {
    precondition {
      condition     = length(local.browser_nested_tenants) == 0
      error_message = "tenant ids ${join(", ", local.browser_nested_tenants)}: one personal tenant's browser-session names start with another's prefix (swarm-tenant-<t>-browser-), so that tenant's worker would write them. Rename the tenant."
    }
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
  description = "What the browser-session grants are made on, for the owner to read a plan against: each personal tenant's worker prefix, and swarm-api's revoke condition. Empty while enable_browser_logins is false."
  value = {
    worker_prefixes = local.browser_session_prefixes
    api_revoke      = var.enable_browser_logins ? local.browser_revoke_condition : ""
  }
}
