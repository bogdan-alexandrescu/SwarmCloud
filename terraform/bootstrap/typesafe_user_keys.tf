# A group tenant's worker may read ONE member's personal TypeSafe key, per row
# the owner lists (JEV-RUNNER, owner decision 2026-10-14; part of #1030).
#
# A live-browser task gets the jev-browser MCP tools when its submitter or its
# tenant has a TypeSafe key, and the submitter's own key wins
# (docs/provider-keys.md §2). That key is `swarm-tenant-u-<user>-typesafe`, in
# the person's personal tenant, and swarm-api's write grants it to that
# personal tenant's worker alone. A task the person submits in a GROUP tenant
# (eng) runs as eng's worker account, which cannot read it: the worker logs
# `jev_key_unreadable` and falls to eng's own key (apps/agent-worker/
# agent_worker/jevkey.py). This file is the grant that lets one listed group
# read one listed person's key.
#
# WHAT IT COSTS, SAID PLAINLY. Every member of a group tenant runs as that one
# worker account, and the agent runs in the worker's container. So a row
# `eng = ["u-bogdan"]` lets ANY eng member's task read u-bogdan's TypeSafe key,
# not only bogdan's tasks -- the trade docs/provider-keys.md §2 warns of, and
# the same one the owner accepted for GitHub user slots (forge_user_slots.tf,
# #780 D7 U1: the tenant's worker reads every member's slot). The worker
# itself uses the key only for a task its owner submitted; the IAM grant
# cannot say that. Hence a row per (group, person), never a prefix: a person
# not listed is never readable, and a row is the person's own consent.
#
# THE CONDITION is the one secret by its full resource name, in both forms
# IAM evaluates for Secret Manager: the secret itself, and its versions
# (`.../versions/<v>`, which is what accessSecretVersion is checked on). The
# project NUMBER, never the ID, which "can't be substituted" there
# (docs/provider-keys.md §6). No other secret of the person's tenant -- their
# Anthropic key, their GitHub slots, their browser logins -- can match.
#
# WHY BOOTSTRAP. roles/secretmanager.secretAccessor must never be on the CI
# deployer's grantable list (forge_user_slots.tf, "WHY HERE"), so this is an
# owner apply, like every per-tenant secret grant in this root.
#
# NOT VERIFIED LIVE: that IAM answers the version form as written. The proof
# is a task submitted in eng by a listed person whose worker log says
# `jev_key_resolved` with `"scope": "user"`, not `jev_key_unreadable`.

variable "typesafe_user_key_readers" {
  description = <<-EOT
    Group tenant id -> the personal tenants (u-<user>) whose TypeSafe key that
    tenant's worker may read (typesafe_user_keys.tf). One row is one person's
    key, readable by every task in that group. Empty: no group reads anyone's.
  EOT
  type        = map(list(string))
  default     = {}

  validation {
    condition = alltrue(flatten([
      for group, users in var.typesafe_user_key_readers : [
        for u in users : can(regex("^u-[a-z0-9][a-z0-9-]*$", u)) && !startswith(group, "u-")
      ]
    ]))
    error_message = "typesafe_user_key_readers maps a group tenant id (not u-...) to personal tenant ids (u-<user>)."
  }
}

data "google_project" "typesafe_user_keys" {
  count      = length(var.typesafe_user_key_readers) > 0 ? 1 : 0
  project_id = var.project_id
}

locals {
  typesafe_project_number = length(var.typesafe_user_key_readers) > 0 ? data.google_project.typesafe_user_keys[0].number : ""

  # "<group>/<user>" -> the pair, one binding each.
  typesafe_user_key_rows = {
    for row in flatten([
      for group, users in var.typesafe_user_key_readers : [
        for u in users : { group = group, user = u }
      ]
    ]) : "${row.group}/${row.user}" => row
  }

  # Literal "swarm-tenant-" and "-typesafe": Tenant.secret_name's spelling of
  # the provider-key provider (swarm_api.validation.TYPESAFE_PROVIDER).
  typesafe_user_key_secrets = {
    for k, row in local.typesafe_user_key_rows :
    k => "projects/${local.typesafe_project_number}/secrets/swarm-tenant-${row.user}-typesafe"
  }
}

resource "google_project_iam_member" "typesafe_user_key_reader" {
  for_each = local.typesafe_user_key_rows

  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${module.service_account_ids.worker_ids[each.value.group]}@${var.project_id}.iam.gserviceaccount.com"

  condition {
    title       = "swarm typesafe key ${each.value.user} for ${each.value.group}"
    description = "managed-by=swarm-terraform; tenant ${each.value.group}'s worker reads ${each.value.user}'s TypeSafe key for the jev tools (JEV-RUNNER). Every ${each.value.group} task can read it."
    expression  = "resource.name == \"${local.typesafe_user_key_secrets[each.key]}\" || resource.name.startsWith(\"${local.typesafe_user_key_secrets[each.key]}/versions/\")"
  }

  lifecycle {
    precondition {
      condition     = contains(local.infra_tenant_ids, each.value.group)
      error_message = "typesafe_user_key_readers names ${each.value.group}, which is not a tenant in the infra tfvars: it has no worker account to grant."
    }
  }
}

output "typesafe_user_key_grants" {
  description = "Which group's worker may read which person's TypeSafe key, as the secret each condition names, for the owner to read a plan against. Empty while typesafe_user_key_readers is."
  value       = local.typesafe_user_key_secrets
}
