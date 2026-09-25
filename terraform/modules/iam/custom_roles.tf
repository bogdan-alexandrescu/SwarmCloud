# The platform's custom roles, as this module GRANTS them -- no longer as it
# defines them.
#
# UNTIL 2026-09-25 THIS FILE DEFINED FIVE CUSTOM ROLES: swarmJobDispatcher,
# swarmJobReaper, swarmGkeDispatcher, swarmGkeReaper and swarmSecretLister. They
# are defined in terraform/bootstrap/platform_roles.tf now, with their
# permission lists and the reasoning behind every permission moved there
# verbatim (#79, owner decision 2026-09-25). terraform/bootstrap is applied by
# the owner, never by CI. Defining a role here needed roles/iam.roleAdmin on the
# CI deployer, and roleAdmin's iam.roles.update cannot be conditioned: CI could
# add resourcemanager.projects.setIamPolicy to a custom role it already held and
# grant itself anything, roles/owner included, without any condition on its
# other grants ever being evaluated. Changing one of these roles is therefore an
# owner bootstrap apply, not a release.
#
# What stays here is the module's side: the role NAMES the bindings in
# bindings.tf use, read from ../custom_role_ids -- the one spelling both roots
# share -- and never from a resource or a data source, because CI can no longer
# read a role at all. The roles must exist before this module's first apply on a
# fresh project; docs/runbooks/custom-roles-to-bootstrap.md gives the order.
#
# THE QUOTA BROKER'S swarmSecretLister GRANT IS NOT MADE HERE ANY MORE EITHER
# (#69, owner decision 2026-09-25). It carries project-wide
# secrets.setIamPolicy, which reaches the other team's 63 secrets, and while CI
# made the grant the role had to stay on the scoped projectIamAdmin's grantable
# list -- which let CI grant it to itself, unconditioned. The owner grants it
# from terraform/bootstrap, and the role is off that list.
#
# terraform/infra/custom_roles_moved_to_bootstrap.tf holds the `removed` blocks
# that make the release FORGET the live roles and the grant rather than delete
# them.

module "custom_role_ids" {
  source = "../custom_role_ids"

  project_id = var.project_id
}

# ---------------------------------------------------------------------------
# secretmanager.versions.add, SCOPED TO THE PLATFORM'S OWN SECRETS.
#
# The broker adds versions to two kinds of secret, and they reach it by two
# different routes:
#
#   swarm-tenant-<t>-<provider>[-refresh]   created by terraform; the broker is
#       bound secretVersionAdder ON EACH ONE by modules/secret_manager
#       (`version_adder`). Already per-secret.
#   swarm-account-<t>--<label>[-refresh]    created by the BROKER at
#       registration, with a name terraform cannot know in advance. These were
#       reachable only through `versions.add` in the project-wide
#       swarmSecretLister role -- and so was every one of the 63 secrets in this
#       project that belong to another team (live listing, 2026-09-24: 57
#       agents-*, 6 promptlab-*).
#
# A literal per-secret binding cannot cover the second kind without the broker
# granting itself adder on every secret it creates, which it could only do
# through the project-wide setIamPolicy -- the larger grant. So the scope is
# drawn where the platform already draws it: the names
# `quota_broker.secretstore.owned_by_this_platform` accepts,
# \Aswarm-(?:tenant|account)-..., as two prefixes IAM evaluates per secret. Of
# the 77 secrets in the project that admits exactly our 14 and refuses the 63.
#
# `projects/<NUMBER>/secrets/`: Secret Manager names secrets by project number
# in a condition, and the project ID "can't be substituted"
# (docs.cloud.google.com/iam/docs/conditions-resource-attributes).
# secretVersionAdder is {versions.add, secrets.rotate,
# resourcemanager.projects.get/list} (gcloud iam roles describe, 2026-09-24);
# both secretmanager permissions are checked on the secret, so no type guard is
# needed -- and none is used, so nothing depends on how IAM types a version.
#
# WHY THIS IS TWO RELEASES AND NOT ONE. Every refresh of an account ROTATES its
# refresh token (76 exchanges, 76 rotations, measured 2026-09-22 in
# quota_broker.credentials), and the new token is written with versions.add
# immediately after the exchange that consumed the old one. If that write is
# refused, the only valid refresh token is lost and the account needs a human.
# Granting the scoped permission and removing the project-wide one in the same
# apply leaves a window, as wide as IAM's propagation, in which the removal can
# land first -- at ~34 exchanges a day across seven accounts, a few minutes of
# window is a real chance of bricking one. So:
#
#   release 1  this grant exists; swarmSecretLister still carries versions.add
#   release 2  swarmSecretLister loses versions.add, after release 1 has been
#              live long enough to propagate -- 7 minutes is IAM's documented
#              worst case
#
# Release 1 has shipped: this grant is in the live policy, condition title
# "swarm tenant and account secrets only" (read-only
# `gcloud projects get-iam-policy`, 2026-09-25 08:52 UTC). Step 2 is no longer
# a release.
# swarmSecretLister is defined in terraform/bootstrap since #79, so dropping
# versions.add from it is the owner's bootstrap apply of
# broker_secret_lister_project_wide_versions_add = false
# (terraform/bootstrap/platform_roles.tf), made at least 7 minutes after this
# grant is live -- which it already is.
#
# NOT VERIFIED LIVE: that IAM evaluates this condition as documented for
# versions.add. Release 1 proves nothing about it -- the project-wide grant is
# still there -- so the proof is the first account refresh after step 2,
# logged as "refresh token rotated and persisted".
# ---------------------------------------------------------------------------

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # The two name families quota_broker.secretstore.owned_by_this_platform
  # accepts. Literal "swarm-", like that regex: the broker's names do not follow
  # a configurable prefix, so neither does this.
  broker_secret_prefixes = [
    "projects/${data.google_project.this.number}/secrets/swarm-tenant-",
    "projects/${data.google_project.this.number}/secrets/swarm-account-",
  ]
}

resource "google_project_iam_member" "broker_version_adder" {
  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = local.sa_member["swarm-quota-broker"]

  condition {
    title       = "swarm tenant and account secrets only"
    description = "The broker may add versions to secrets this platform owns and to no other. 63 of the 77 secrets in this project belong to another team."
    expression  = join(" || ", [for p in local.broker_secret_prefixes : "resource.name.startsWith(\"${p}\")"])
  }
}
