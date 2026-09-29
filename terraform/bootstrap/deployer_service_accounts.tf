# roles/iam.serviceAccountAdmin for the release deployer: on each service
# account terraform/infra manages, and on no other account.
#
# OWNER DECISION 2026-09-29 (#334, from the security review of contract request
# 30, #314). Granted on the project, the role let CI set the IAM policy of every
# service account in saga-agents-staging -- the other team's promptlab-runner,
# swarm-ci-fix, and swarm-tf-deployer itself -- and so grant itself
# tokenCreator on any of them. IAM conditions cannot narrow it ("IAM resources
# don't provide the resource name",
# docs.cloud.google.com/iam/docs/conditions-attribute-reference), but the role
# can be granted on a single service account ("Lowest-level resources where you
# can grant this role: Service Account", docs.cloud.google.com/iam/docs/
# roles-permissions/iam, read 2026-09-28). So it is granted here, once per
# account. Creating an account is checked on the project, which is why
# deployer_roles holds roles/iam.serviceAccountCreator (create, get, list).
#
# WHAT THE RELEASE DOES WITH IT. terraform/infra sets IAM on its own accounts:
# deployer.tf (the deployer's actAs on each platform account, swarm-tick and
# swarm-verify), modules/tenancy (act_as and workload_identity on each worker)
# and modules/iam/bindings.tf (swarm-api signing as itself). Every one of those
# accounts is on this list, and deployer.tf's grant of actAs to itself still
# works, now through these grants instead of the project-wide one.
#
# WHAT IT STILL ALLOWS, said so it is not mistaken for more. Admin on swarm-api
# lets CI grant itself tokenCreator on swarm-api and act as it. That was always
# true of the accounts CI deploys as; what is gone is the same on accounts it
# does not.
#
# THE LIST HAS ONE SOURCE. modules/service_account_ids spells the platform
# accounts, swarm-tick, swarm-verify and the worker prefix, and terraform/infra
# reads the same module. The tenant keys are read from the tenants block of
# var.infra_tenants_tfvars -- terraform/environments/dev/dev.tfvars, the file the
# release applies. Terraform has no HCL decoder, so the keys are found by
# pattern: the file is `terraform fmt`ed (make lint checks it), so the block
# opens on a line that is exactly `tenants = {`, closes on the first line that
# is exactly `}`, and each tenant is a two-space-indented `<key> = {`.
# tests/terraform/deployer_sa_admin.tftest.hcl holds that parse to exact
# answers on a fixture built to trip it, and a missing block fails the plan.
#
# ORDERING. A grant needs its account to exist, and the release sets a new
# account's IAM in the same apply that creates it. So a tenant's account is made
# BEFORE the release that adds it: scripts/register-tenant.sh creates it (and
# refuses one somebody else made first); the owner applies this file's grant
# on it FROM MAIN, reading the pull request's dev.tfvars as data through
# -var infra_tenants_tfvars=<a copy taken with git show> -- never running this
# root's code from an unmerged branch; then the release runs, and
# modules/tenancy's create_ignore_already_exists adopts the account. docs/ci.md,
# "A new account exists before the release that adds it", has the commands and
# the plan checks.
#
# A HEREDOC IN THE TENANTS BLOCK IS REFUSED. The file is a pull request's data,
# and a heredoc's body is free text: a line in it shaped like `  name = {` would
# parse as a tenant and be granted on. dev.tfvars has no reason to hold one.

module "service_account_ids" {
  source = "../modules/service_account_ids"

  tenant_ids = local.infra_tenant_ids
}

locals {
  # Relative to this root, or absolute: the owner points it at a copy outside
  # the checkout.
  infra_tenants_tfvars_path = startswith(var.infra_tenants_tfvars, "/") ? var.infra_tenants_tfvars : "${path.module}/${var.infra_tenants_tfvars}"
  infra_tenants_tfvars_text = file(local.infra_tenants_tfvars_path)

  # One match, whose one capture is the body of the top-level tenants block.
  infra_tenants_block = regexall("(?ms)^tenants[ \\t]*=[ \\t]*\\{[ \\t]*$(.*?)^\\}[ \\t]*$", local.infra_tenants_tfvars_text)

  # "" when there is no single tenants block; the first precondition below
  # refuses that case, so "" never means "no tenants".
  infra_tenants_body = length(local.infra_tenants_block) == 1 ? local.infra_tenants_block[0][0] : ""

  infra_tenant_ids = [
    for m in regexall("(?m)^  \"?([a-z0-9][a-z0-9-]*)\"?[ \\t]*=[ \\t]*\\{", local.infra_tenants_body) : m[0]
  ]

  # Admin on either is admin over CI's own identity: the fix bot's account
  # (contract request 30) and the deployer itself. Neither is managed by
  # terraform/infra, so neither should ever be on the list; the precondition
  # below makes it loud if one is, instead of quietly subtracting it.
  deployer_admin_excluded = distinct([
    "swarm-ci-fix",
    "${var.name_prefix}-ci-fix",
    "swarm-tf-deployer",
    "${var.name_prefix}-tf-deployer",
  ])

  deployer_admin_accounts = setsubtract(toset(module.service_account_ids.infra_managed), toset(local.deployer_admin_excluded))
}

resource "google_service_account_iam_member" "deployer_admin" {
  for_each = var.enable_github_wif ? local.deployer_admin_accounts : toset([])

  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.key}@${var.project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.serviceAccountAdmin"
  member             = "serviceAccount:${google_service_account.deployer[0].email}"

  lifecycle {
    precondition {
      condition     = length(local.infra_tenants_block) == 1 && length(local.infra_tenant_ids) > 0
      error_message = "no top-level `tenants = { ... }` block with at least one tenant was found in ${var.infra_tenants_tfvars}. Granting on the platform accounts alone would drop every worker grant and fail the next release on a 403; run `terraform fmt` on the file, or point infra_tenants_tfvars at the tfvars the release applies."
    }

    precondition {
      condition     = !strcontains(local.infra_tenants_body, "<<")
      error_message = "the tenants block of ${var.infra_tenants_tfvars} holds a heredoc (`<<`). Its body is free text, and a line in it shaped like `  name = {` would be read as a tenant and granted serviceAccountAdmin on (#334). Take the heredoc out of the tenants block."
    }

    precondition {
      condition     = length(setintersection(toset(module.service_account_ids.infra_managed), toset(local.deployer_admin_excluded))) == 0
      error_message = "terraform/infra would manage swarm-ci-fix or the deployer's own account. The deployer is never granted serviceAccountAdmin on either (#334): that is admin over CI's own identity."
    }
  }
}

# What the owner checks a plan against: exactly the accounts the deployer is
# granted serviceAccountAdmin on, so a tenant the pull request did not mean to
# add -- or one a malformed tenants block invented -- shows up by name.
output "deployer_admin_accounts" {
  description = "Every service account id the release deployer holds roles/iam.serviceAccountAdmin on, sorted."
  value       = sort(keys(google_service_account_iam_member.deployer_admin))
}
