# The user slots' IAM: who may create, write and read the per-user GitHub
# credential slots swarm-api creates at onboarding (docs/onboarding.md §3.4
# items 3-5 and 8; #780, lane OB2).
#
# A user slot is `swarm-tenant-<tenant>-git-u-<16 hex>` (Tenant.secret_name of
# the user's provider suffix), holding that user's GitHub user access token,
# with a `-refresh` twin holding the refresh token that mints the next one.
# Owner decisions, 2026-10-07:
#
#   D3  swarm-api creates each user's slot at onboarding -- self-service is the
#       point of #780 -- with a project-level create grant that carries no
#       read. So no slot is in any Terraform state, and no grant can name one.
#   D7  U1: the tenant's worker account reads every member's slot in its own
#       tenant; swarm-api's resolver hands each task only its submitter's.
#   D2  swarm-api refreshes: it reads the -refresh twins and writes both.
#       It originally never read a base slot -- the split the quota broker
#       has today. Revised by the owner on 2026-10-08, below.
#
# Owner decision 2026-10-08: swarm-api also READS the base slots, so a request
# acting as the person reuses their current access token instead of
# refreshing on every call. Measured that morning: the owner's slot reached 35
# versions within minutes, and since each refresh makes GitHub end the access
# token it replaces, a task holding the previous version failed 401. Reading
# the access token adds no power: swarm-api already reads the refresh token,
# which mints access tokens. The worker's grant is unchanged -- base slots
# only, never a twin.
#
# Owner decision 2026-10-07 (OB3, PR #821): "Disconnect GitHub" disables the
# user's secret versions, so a disconnected user leaves no usable token behind,
# and a reconnect re-enables them.
#
# These grants make that, and every one but the create is narrowed to one
# tenant's prefix in
# the FULL resource-name form IAM evaluates for Secret Manager,
# `projects/<NUMBER>/secrets/<id>` -- the project number, because the project
# ID "can't be substituted" there (docs.cloud.google.com/iam/docs/
# conditions-resource-attributes), the same form modules/iam's
# broker_version_adder uses:
#
#   google_project_iam_custom_role.forge_slot_creator   swarmForgeSlotCreator:
#       secretmanager.secrets.create and nothing else.
#   google_project_iam_member.forge_slot_creator        swarm-api holds it,
#       UNCONDITIONED: creation is checked against the project, which carries
#       no secret name, so a name condition cannot narrow it. What it buys is
#       the ability to create an empty secret, anywhere in the project -- not
#       to read, version, delete or set IAM on one, this platform's or the
#       other team's.
#   google_project_iam_member.forge_slot_version_adder  swarm-api,
#       secretVersionAdder, on `<prefix>` (base slots and their twins): it
#       publishes the user's token at onboarding and on every refresh.
#   google_project_iam_custom_role.forge_slot_version_manager
#                                                       swarmForgeSlotVersionManager:
#       secretmanager.versions.disable and secretmanager.versions.enable and
#       nothing else. No versions.destroy (a disconnect is reversible, and a
#       destroyed version is not), no versions.access (reading is
#       forge_refresh_reader's, a separate and separately conditioned
#       grant). There is no predefined role that disables without also
#       destroying or reading: secretVersionManager carries destroy.
#   google_project_iam_member.forge_slot_version_manager
#                                                       swarm-api, that role,
#       on `<prefix>` -- the version adder's own condition, so it reaches
#       exactly the slots swarm-api already writes, base and twin alike.
#   google_project_iam_member.forge_refresh_reader      swarm-api,
#       secretAccessor, on `<prefix>` -- base slots and -refresh twins alike,
#       the version adder's own condition (owner decision 2026-10-08): the
#       refresh token, which the sweep spends, and the current access token,
#       which a request reuses. The address keeps its old name so the change
#       reads as an in-place condition change in the plan.
#   google_project_iam_member.forge_slot_reader         the tenant's worker,
#       secretAccessor, on `<prefix>` AND NOT a -refresh twin: the access
#       token, read at runtime. A worker that could read a refresh token could
#       mint itself access for as long as the refresh token lasts.
#
# WHY HERE AND NOT IN terraform/infra (where §3.4 item 4 put the worker's
# grant, "from the tenancy module"). Every project-level role terraform/infra
# grants must be on the CI deployer's scoped projectIamAdmin list
# (deployer_conditions.tf, deployer_grantable_project_roles), and hasOnly()
# limits WHICH roles CI may grant, never to WHOM. Putting
# roles/secretmanager.secretAccessor on that list would let CI grant itself,
# unconditioned, read of every secret in saga-agents-staging -- the other
# team's 63 among them. That is why the broker's swarmSecretLister grant moved
# here (#69), and these move here for the same reason. The cost is the same as
# theirs: a new tenant's user-slot grants are an owner bootstrap apply, the
# one docs/ci.md already requires for its worker account (#334).
#
# WHY BOTH NAME FORMS IN THE -refresh TEST. accessSecretVersion is checked on
# the VERSION, whose resource.name is `projects/<n>/secrets/<id>/versions/<v>`
# -- which never ends in "-refresh". `resource.name.endsWith("-refresh")`
# alone (§3.4 item 4 as written) would therefore exclude nothing at access
# time, and the worker would read the refresh twins. So the test is made on
# the secret id extracted from either form:
# `resource.name.extract("/secrets/{name}/versions/")` is the id for a version
# and "" for the secret itself, and `resource.name.endsWith(...)` covers the
# secret itself.
#
# NOT VERIFIED LIVE: that IAM evaluates resource.name for versions.access,
# versions.add, versions.disable and versions.enable in exactly these forms, and that extract() answers as above for
# Secret Manager names. The mock-provider tests hold the expressions' text,
# not IAM's answer. The proof is the runbook's last step
# (docs/runbooks/github-app.md, step 8): a worker reading a base slot
# succeeds, and reading the twin is PERMISSION_DENIED.
#
# THE OTHER TEAM'S RESOURCES. Every prefix below starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# promptlab-*, so no condition here can match one. The unconditioned create
# grant can create a secret with any name, and can do nothing else to it.

variable "enable_forge_user_slots" {
  description = <<-EOT
    Define swarmForgeSlotCreator and swarmForgeSlotVersionManager and make
    the user-slot grants (forge_user_slots.tf): swarm-api creates GitHub user
    slots at onboarding, refreshes them, and disables and re-enables their
    versions on disconnect and reconnect; each tenant's worker reads its own
    tenant's base slots. Owner decisions D2, D3 and D7 on #780, and OB3's
    disable of 2026-10-07. The accounts must
    exist (terraform/infra creates swarm-api, register-tenant.sh each worker).
  EOT
  type        = bool
  default     = false
}

data "google_project" "forge_user_slots" {
  count      = var.enable_forge_user_slots ? 1 : 0
  project_id = var.project_id
}

locals {
  forge_user_slots_on = var.enable_forge_user_slots ? 1 : 0

  # The project number, for the conditions. "" while the slots are off, when
  # nothing reads it.
  forge_project_number = var.enable_forge_user_slots ? data.google_project.forge_user_slots[0].number : ""

  # Named, not read: the account is terraform/infra's (modules/iam), and this
  # root cannot depend on that one. The id comes from the module both roots
  # read, as broker_account_id's does in platform_roles.tf.
  forge_api_member = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"

  # The tenants the release applies, from the same parse of dev.tfvars the
  # deployer's per-account grants use (deployer_service_accounts.tf). Literal
  # "swarm-tenant-": Tenant.secret_name's spelling, not a configurable prefix.
  forge_user_slot_tenants = var.enable_forge_user_slots ? toset(local.infra_tenant_ids) : toset([])

  forge_user_slot_prefixes = {
    for t in local.forge_user_slot_tenants :
    t => "projects/${local.forge_project_number}/secrets/swarm-tenant-${t}-git-u-"
  }

  # `any`: the tenant's user slots, base and -refresh twin. `base`: the base
  # slots only, the secret id's "-refresh" test made in both resource-name
  # forms (above).
  forge_slot_conditions = {
    for t, p in local.forge_user_slot_prefixes : t => {
      any  = "resource.name.startsWith(\"${p}\")"
      base = "resource.name.startsWith(\"${p}\") && !resource.name.endsWith(\"-refresh\") && !resource.name.extract(\"/secrets/{name}/versions/\").endsWith(\"-refresh\")"
    }
  }

  # A tenant whose slot names start with another tenant's prefix -- `eng` and
  # `eng-git-u-x` -- would have its slots readable by the other's worker. Read
  # from the tenant ids rather than the rendered prefixes, which share an
  # unknown project number at plan.
  forge_nested_tenants = [
    for pair in setproduct(local.forge_user_slot_tenants, local.forge_user_slot_tenants) :
    "${pair[1]} inside ${pair[0]}"
    if pair[0] != pair[1] && startswith("swarm-tenant-${pair[1]}-git-u-", "swarm-tenant-${pair[0]}-git-u-")
  ]
}

resource "google_project_iam_custom_role" "forge_slot_creator" {
  count = local.forge_user_slots_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["forge_slot_creator"]
  title       = "Swarm Forge Slot Creator"
  description = "managed-by=swarm-terraform; create a GitHub user credential slot at onboarding (#780). Create only: no read, no version, no delete, no IAM."
  stage       = "GA"

  # secretmanager.secrets.create alone. Not roles/secretmanager.admin, which
  # reads every secret in the project; there is no predefined role that
  # creates without reading. Labels and replication are fields of the create
  # call and need nothing further.
  permissions = ["secretmanager.secrets.create"]
}

resource "google_project_iam_member" "forge_slot_creator" {
  count = local.forge_user_slots_on

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.forge_slot_creator[0].role_id}"
  member = local.forge_api_member
}

resource "google_project_iam_member" "forge_slot_version_adder" {
  for_each = local.forge_user_slot_tenants

  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = local.forge_api_member

  condition {
    title       = "swarm forge user slots ${each.key}"
    description = "managed-by=swarm-terraform; swarm-api publishes tenant ${each.key}'s GitHub user tokens and refresh tokens (#780 D2, D3)."
    expression  = local.forge_slot_conditions[each.key].any
  }
}

resource "google_project_iam_custom_role" "forge_slot_version_manager" {
  count = local.forge_user_slots_on

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["forge_slot_version_manager"]
  title       = "Swarm Forge Slot Version Manager"
  description = "managed-by=swarm-terraform; disable a GitHub user slot's versions on disconnect and enable them on reconnect (#780 OB3). No destroy, no access."
  stage       = "GA"

  # Not roles/secretmanager.secretVersionManager, which also destroys
  # versions. Disable and enable alone: a disconnect leaves no usable token
  # and can be undone.
  permissions = [
    "secretmanager.versions.disable",
    "secretmanager.versions.enable",
  ]
}

resource "google_project_iam_member" "forge_slot_version_manager" {
  for_each = local.forge_user_slot_tenants

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.forge_slot_version_manager[0].role_id}"
  member = local.forge_api_member

  condition {
    title       = "swarm forge user slot versions ${each.key}"
    description = "managed-by=swarm-terraform; swarm-api disables tenant ${each.key}'s GitHub user tokens on disconnect and enables them on reconnect (#780 OB3)."
    expression  = local.forge_slot_conditions[each.key].any
  }
}

resource "google_project_iam_member" "forge_refresh_reader" {
  for_each = local.forge_user_slot_tenants

  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = local.forge_api_member

  condition {
    title       = "swarm forge user slots api ${each.key}"
    description = "managed-by=swarm-terraform; swarm-api reads tenant ${each.key}'s GitHub user slots: the refresh token to refresh, the current access token to reuse instead of refreshing per call (#780 D2, owner decision 2026-10-08)."
    expression  = local.forge_slot_conditions[each.key].any
  }

  # A condition is part of a binding's identity, so changing it replaces the
  # binding. The new one is made first, so the refresh sweep never loses the
  # twins between the two.
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_project_iam_member" "forge_slot_reader" {
  for_each = local.forge_user_slot_tenants

  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${module.service_account_ids.worker_ids[each.key]}@${var.project_id}.iam.gserviceaccount.com"

  condition {
    title       = "swarm forge user slots ${each.key}"
    description = "managed-by=swarm-terraform; tenant ${each.key}'s worker reads its own users' GitHub access tokens, never a refresh token (#780 D7, U1)."
    expression  = local.forge_slot_conditions[each.key].base
  }

  lifecycle {
    precondition {
      condition     = length(local.forge_nested_tenants) == 0
      error_message = "tenant ids ${join(", ", local.forge_nested_tenants)}: one tenant's user-slot names start with another's prefix (swarm-tenant-<t>-git-u-), so that tenant's worker would read them. Rename the tenant."
    }
  }
}

output "forge_user_slot_grants" {
  description = "What the user-slot grants are made on, per tenant, for the owner to read a plan against: the prefix each condition narrows to. Empty while enable_forge_user_slots is false."
  value       = local.forge_user_slot_prefixes
}
