# ---------------------------------------------------------------------------
# LLM providers: the quota broker destroys a deleted API key's secret at once
# (docs/design/llm-providers.md §4.4 and §8.3; owner decision Q3, 9 November
# 2026; lane LP-T1).
# ---------------------------------------------------------------------------
#
# An API key in the pool is one Secret Manager secret the broker creates at
# registration, `swarm-account-<tenant>--key-<label>` (design §4.1): the plan
# accounts' naming family, with a `key-` mark before the label and no
# `-refresh` half. The owner decided that deleting a key destroys its secret on
# confirmation, not after the last hold closes. The broker already adds and
# destroys versions on the platform's secrets (modules/iam custom_roles.tf,
# `broker_version_adder`; swarmSecretLister, platform_roles.tf) but holds no
# secretmanager.secrets.delete, which removing the secret itself needs.
#
#   google_project_iam_custom_role.account_key_deleter
#       swarmAccountKeyDeleter: secretmanager.secrets.delete and no other
#       permission. Not roles/secretmanager.admin, which reads every value in
#       the shared project, and not a widening of swarmSecretLister, which
#       reaches every secret project-wide and is held byte for byte to its live
#       definition (platform_roles.tftest.hcl).
#   google_project_iam_member.account_key_deleter
#       swarm-quota-broker holds it, under one condition (below).
#
# THE GRANT IS MADE HERE, NOT IN terraform/modules/iam, on the pattern of
# swarmMcpCredentialAccessor (mcp_credentials.tf). The secrets are named by
# the broker at runtime, so the grant is a project IAM member with a name
# condition, and a project grant CI makes must be on
# deployer_grantable_project_roles (deployer_conditions.tf). Its hasOnly()
# limits which roles CI modifies, never whose member it adds or under what
# condition: on that list, CI could grant itself this role unconditioned and
# delete any secret in the project, the other team's among them. So the owner
# applies both halves, and the release carries no part of this change.
#
# THE ROLE ID is spelled here, as mcp_credentials.tf spells its own, and not in
# terraform/modules/custom_role_ids: that module exists so a role this root
# defines and terraform/infra grants is one string in both roots, and
# terraform/infra never names this role. tests/terraform/account_key_secrets
# .tftest.hcl holds that modules/iam grants it to nobody.
#
# THE CONDITION, one expression and no list (design §8.3):
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-account-")
#     && resource.name.extract("/secrets/swarm-account-{t}--key-") != ""
#
#   * The project NUMBER: IAM evaluates Secret Manager names with it, never the
#     project ID. secrets.delete is checked on the secret.
#   * extract(), not matches(), which IAM refuses on some services
#     (terraform/modules/iam/bindings.tf, release run 38114832799). The arm is
#     true exactly when a NON-EMPTY tenant sits between `swarm-account-` and the
#     first `--key-` after it.
#   * No hasOnly() and no tenant list: the tenant is found in the name, so the
#     condition covers every workspace, those made outside Terraform included.
#
# WHAT IT REFUSES. A plan account's secret, `swarm-account-<t>--<label>` and
# its `-refresh` half, so plan removal keeps its `"secret": "retained"`; every
# `swarm-tenant-*` secret (the workspace's own provider keys, its forge token,
# its workspace secrets); the dedicated Voice it key registered before the
# design, which keeps its `--<label>` name (§4.4, Q9); and the other team's
# agents-* and peer-app-* secrets, which do not start with the prefix at all.
#
# WHAT IT STILL REACHES (the residual, for the security review): a plan whose
# label begins `key-` or holds `--key-`, which would be named like a key. A
# tenant id cannot hold `--` (quota_broker accounts.py refuses one), so the
# first `--` after `swarm-account-` ends the tenant; the broker refuses such a
# plan label (LP-B1 stage (a)), and that stage lists the existing accounts to
# show no plan's secret is so named before this grant is applied.
#
# ORDER: none against a release. Nothing in terraform/infra names the role, so
# this root may be applied before or after any release. The broker's delete
# (LP-B1 stage (c)) must not ship before this is applied: until then its
# delete is refused, which is the 502 and the sweep retry of design §4.4,
# never a silent retain.
#
# THE OWNER'S TARGETED PLAN (the runbook's step) shows exactly the two objects:
#
#   terraform -chdir=terraform/bootstrap plan \
#     -target=google_project_iam_custom_role.account_key_deleter \
#     -target=google_project_iam_member.account_key_deleter
#
# data.google_project.account_key_secrets is read, never changed, and the
# broker's account (platform_service_accounts.tf) already exists, so neither
# appears as a change.
#
# NOT VERIFIED LIVE: that IAM evaluates the condition as the mock-provider
# tests emulate it. Lint it with iamPolicies:lintPolicy before applying, as
# docs/provider-keys.md §6 did; the first key delete that logs its secret's
# deletion is the live proof.
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-account-`; their secrets are agents-* and
# peer-app-*, so it can match none of them. The grant is to swarm-quota-broker
# alone and touches no cluster, network, bucket or account of theirs.

data "google_project" "account_key_secrets" {
  project_id = var.project_id
}

locals {
  # A pool secret by its full name, with a non-empty tenant before the first
  # `--key-` (the header). Literal "swarm-account-" and "--key-":
  # quota_broker.accounts' spelling and the design's mark (§4.1).
  account_key_secrets_condition = "resource.name.startsWith(\"projects/${data.google_project.account_key_secrets.number}/secrets/swarm-account-\") && resource.name.extract(\"/secrets/swarm-account-{t}--key-\") != \"\""
}

resource "google_project_iam_custom_role" "account_key_deleter" {
  project     = var.project_id
  role_id     = "swarmAccountKeyDeleter"
  title       = "Swarm Account Key Deleter"
  description = "managed-by=swarm-terraform; the quota broker deletes a deleted API key's secret (swarm-account-<t>--key-*) at once (LLM providers, Q3). secrets.delete only: no read, create, version or IAM."
  stage       = "GA"

  # One permission (the header).
  permissions = ["secretmanager.secrets.delete"]
}

resource "google_project_iam_member" "account_key_deleter" {
  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.account_key_deleter.role_id}"
  member = google_service_account.platform["swarm-quota-broker"].member

  condition {
    title       = "swarm account key secrets"
    description = "managed-by=swarm-terraform; the quota broker deletes swarm-account-<t>--key-* secrets, the pool's API keys, and no plan's or tenant's secret (LLM providers, Q3)."
    expression  = local.account_key_secrets_condition
  }
}

output "account_key_deleter_grant" {
  description = "The condition the quota broker's delete of API key secrets is granted under, for the owner to read a plan against and to lint (iamPolicies:lintPolicy)."
  value       = local.account_key_secrets_condition
}
