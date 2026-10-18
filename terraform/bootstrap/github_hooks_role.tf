# ---------------------------------------------------------------------------
# swarmGithubHooksDedup: the GitHub push receiver may create a delivery
# document, and nothing else (lane HOOKS-ROLE, part of #1384; owner decision
# 2026-10-16 on PR 1406's question 1).
# ---------------------------------------------------------------------------
#
# The receiver swarm-github-hooks (apps/swarm-github-hooks;
# terraform/infra/github_hooks.tf) runs as the rollup sweeper account and
# dedups GitHub's deliveries by creating github_deliveries/{X-GitHub-Delivery}:
# a redelivery answers 409 ALREADY_EXISTS. GS-HOOKS granted that account
# roles/datastore.user for it, which is read-write on every collection of the
# platform's database, held by a public-facing service. IAM conditions are not
# evaluated on Firestore's data plane (terraform/modules/iam/bindings.tf
# `firestore_condition`), so no condition could narrow it to one collection.
# The owner chose a custom role holding only what the dedup needs.
#
# THE ONE PERMISSION, from the receiver's code and Google's table
# (firestore/docs/security/iam, "Required permissions for API methods"):
#
#   apps/swarm-github-hooks/swarm_github_hooks/clients.py
#   FirestoreDeliveries.create is one REST call,
#     POST https://firestore.googleapis.com/v1/projects/<p>/databases/<db>/
#          documents/github_deliveries?documentId=<delivery id>
#   which is projects.databases.documents.createDocument, and Firestore checks
#   datastore.entities.create for it.
#
# NOT datastore.databases.getMetadata, nor datastore.databases.get. No client
# library sits in front of that call, so nothing resolves the database first
# (the worker's swarmTenantWorkerFirestore carries databases.get for exactly
# that, platform_roles.tf). getMetadata is projects.databases.get, and
# databases.get is what beginTransaction and rollback check
# (docs/workspaces.md records how the two names misled once); the receiver
# makes neither call. Nor entities.get, list, update or delete: the receiver
# never reads, queries, overwrites or removes a delivery, and Firestore's TTL
# policy (github_hooks.tf github_deliveries_ttl) deletes it as the service
# agent, not as the receiver.
#
# WHAT IT STILL REACHES (the residual, for the security review): create of a
# NEW document in any collection of any database in the project, the other
# team's included if they hold Firestore here, because Firestore has no IAM
# resource narrower than the project. A create never overwrites an existing
# document (that is the 409 the dedup relies on) and reads nothing, so the
# receiver's identity can add documents but cannot change, read or remove one.
# roles/datastore.user reached all of that plus read, query, update and delete.
#
# THE ROLE IS DEFINED HERE AND GRANTED IN terraform/infra. Every custom role
# lives in this root, which the owner applies: CI holds no iam.roles.*
# permission (#79, platform_roles.tf says why), and
# tests/terraform/custom_roles_out_of_ci.tftest.hcl fails if terraform/infra or
# a module defines one. Both roots read the id from
# terraform/modules/custom_role_ids. The CI deployer may grant it
# (deployer_conditions.tf, deployer_grantable_project_roles) only while this
# root defines it; the role carries no IAM permission, so CI holding the right
# to grant it gives CI nothing it could turn into more.
#
# ORDER: the owner applies this root with enable_github_hooks_dedup_role on
# BEFORE terraform/infra's enable_github_hooks is turned on. Otherwise that
# release's setIamPolicy names a role that does not exist and fails, and, once
# projectIamAdmin is scoped, a role its chunk conditions do not admit.
#
# NOT VERIFIED LIVE: that Firestore authorizes createDocument on
# datastore.entities.create alone for a named database. It is Google's table,
# not a measurement here. If it is wrong, every signed delivery's create is
# refused, the receiver answers 503 (`store_failed`, receiver.py) and forwards
# nothing, and adding the one missing permission is the next bootstrap apply.
#
# THE OTHER TEAM'S RESOURCES. This file defines one role and grants it to
# nobody; it names no cluster, network, bucket or account of theirs.

variable "enable_github_hooks_dedup_role" {
  description = <<-EOT
    Define swarmGithubHooksDedup (datastore.entities.create only) and let the
    CI deployer grant it (deployer_grantable_project_roles), for the GitHub
    push receiver's delivery dedup (github_hooks_role.tf; HOOKS-ROLE, part of
    #1384). terraform/infra/github_hooks.tf grants it to the rollup sweeper
    account. Off by default, so merging changes no plan: turn it on and apply
    BEFORE terraform/infra's enable_github_hooks.
  EOT
  type        = bool
  default     = false
}

locals {
  github_hooks_dedup_permissions = ["datastore.entities.create"]
}

resource "google_project_iam_custom_role" "github_hooks_dedup" {
  count = var.enable_github_hooks_dedup_role ? 1 : 0

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["github_hooks_dedup"]
  title       = "Swarm GitHub Hooks Dedup"
  description = "managed-by=swarm-terraform; the GitHub push receiver creates github_deliveries/{delivery id}, its redelivery dedup (HOOKS-ROLE, #1384). entities.create only: no read, query, update or delete."
  stage       = "GA"

  permissions = local.github_hooks_dedup_permissions
}

output "github_hooks_dedup_role" {
  description = "The GitHub push receiver's Firestore role, for the owner to read a plan against: its full name and its permissions. Empty while enable_github_hooks_dedup_role is false."
  value = {
    role        = var.enable_github_hooks_dedup_role ? "projects/${var.project_id}/roles/${google_project_iam_custom_role.github_hooks_dedup[0].role_id}" : ""
    permissions = var.enable_github_hooks_dedup_role ? local.github_hooks_dedup_permissions : []
  }
}
