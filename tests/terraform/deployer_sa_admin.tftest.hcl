# roles/iam.serviceAccountAdmin is granted to the release deployer on each
# service account terraform/infra manages, never on the project.
#
# OWNER DECISION 2026-09-29 (#334, from the security review of contract request
# 30, #314). Project-wide, the role let CI set the IAM policy of every service
# account in saga-agents-staging -- the other team's promptlab-runner, the
# swarm-ci-fix account, and swarm-tf-deployer itself -- and so make itself any
# of them. A resource.name condition cannot narrow it: "IAM resources don't
# provide the resource name" (docs.cloud.google.com/iam/docs/conditions-
# attribute-reference), so a condition would revoke it. The narrowing is
# resource-level grants instead, plus roles/iam.serviceAccountCreator on the
# project so a release can still create an account.
#
# The list of accounts has one source: terraform/modules/service_account_ids
# (the platform accounts, swarm-tick, swarm-verify and the worker prefix, which
# terraform/infra reads too) and the tenants block of
# terraform/environments/dev/dev.tfvars, which bootstrap parses from the file.
# The fixture runs below hold that parse to exact answers.

mock_provider "google" {}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]
  enable_github_wif    = true
  github_repository    = "saga/agent-swarm-infra"
}

run "service_account_admin_is_granted_per_account_and_never_project_wide" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = !contains(var.deployer_roles, "roles/iam.serviceAccountAdmin")
    error_message = "roles/iam.serviceAccountAdmin is still in deployer_roles' default, which grants it on the whole project"
  }

  assert {
    condition     = !contains(keys(google_project_iam_member.deployer_roles), "roles/iam.serviceAccountAdmin")
    error_message = "the deployer is still granted roles/iam.serviceAccountAdmin project-wide"
  }

  assert {
    condition     = contains(keys(google_project_iam_member.deployer_roles), "roles/iam.serviceAccountCreator")
    error_message = "without roles/iam.serviceAccountCreator on the project a release cannot create an account it manages"
  }

  assert {
    condition     = contains(local.deployer_roles_reviewed, "roles/iam.serviceAccountCreator") && !contains(local.deployer_roles_reviewed, "roles/iam.serviceAccountAdmin")
    error_message = "the reviewed-roles list must name serviceAccountCreator and no longer name serviceAccountAdmin"
  }

  # Every account terraform/infra manages today, by name. A control that the
  # modules and dev.tfvars were read at all; the exact set is the fixture run's.
  assert {
    condition = alltrue([
      for id in [
        "swarm-api",
        "swarm-scheduler",
        "swarm-quota-broker",
        "swarm-reconciler",
        "swarm-tick",
        "swarm-verify",
        "swarm-rollup-sweeper",
        "swarm-agent-worker-eng",
        "swarm-agent-worker-smoke",
        "swarm-agent-worker-u-bogdan",
      ] : contains(keys(google_service_account_iam_member.deployer_admin), id)
    ])
    error_message = "an account terraform/infra manages has no per-account serviceAccountAdmin grant, so the release cannot set its IAM policy"
  }

  assert {
    condition = alltrue([
      for id, m in google_service_account_iam_member.deployer_admin :
      m.role == "roles/iam.serviceAccountAdmin" &&
      m.service_account_id == "projects/saga-agents-staging/serviceAccounts/${id}@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "a per-account grant names another role, or another account than its key"
  }

  # Nothing outside what infra manages: the platform accounts, the three
  # singletons, and tenant workers.
  assert {
    condition = alltrue([
      for id in keys(google_service_account_iam_member.deployer_admin) :
      contains(["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler", "swarm-tick", "swarm-verify", "swarm-rollup-sweeper"], id) || startswith(id, "swarm-agent-worker-")
    ])
    error_message = "the deployer is granted serviceAccountAdmin on an account terraform/infra does not manage"
  }

  # THE EXCLUSION. Admin on either account is admin over CI's own identity.
  assert {
    condition     = !contains(keys(google_service_account_iam_member.deployer_admin), "swarm-ci-fix") && !contains(keys(google_service_account_iam_member.deployer_admin), "swarm-tf-deployer")
    error_message = "the deployer is granted serviceAccountAdmin on swarm-ci-fix or on itself"
  }

  assert {
    condition     = contains(local.deployer_admin_excluded, "swarm-ci-fix") && contains(local.deployer_admin_excluded, "swarm-tf-deployer")
    error_message = "the exclusion list must name swarm-ci-fix and the deployer's own account"
  }
}

# The parse of the tenants block, held to an exact answer on a file built to
# trip it: blocks before and after tenants, nested objects, a quoted key and a
# comment shaped like a tenant.
run "the_worker_accounts_are_exactly_the_tenants_the_tfvars_names" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    infra_tenants_tfvars = "../../tests/terraform/tenants_tfvars_fixture/fixture.tfvars"
  }

  assert {
    condition     = toset(local.infra_tenant_ids) == toset(["alpha", "b-2", "quoted"])
    error_message = "the tenants parse found other tenants than alpha, b-2 and quoted"
  }

  assert {
    condition = toset([
      for id in keys(google_service_account_iam_member.deployer_admin) : id if startswith(id, "swarm-agent-worker-")
    ]) == toset(["swarm-agent-worker-alpha", "swarm-agent-worker-b-2", "swarm-agent-worker-quoted"])
    error_message = "the per-account grants on worker accounts are not exactly one per tenant"
  }
}

# A file with no tenants block is refused, not read as "no tenants": the
# latter would drop every worker grant and fail the next release on a 403.
run "a_tfvars_without_a_tenants_block_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    infra_tenants_tfvars = "../../tests/terraform/tenants_tfvars_fixture/no_tenants.tfvars"
  }

  expect_failures = [google_service_account_iam_member.deployer_admin]
}

# A heredoc inside the tenants block is refused (#334 security review): its
# body is free text, so a line in it shaped like `  name = {` would be parsed as
# a tenant and granted on. The owner applies bootstrap with a copy of a pull
# request's dev.tfvars, so the file is not trusted to be only tenants.
run "a_heredoc_in_the_tenants_block_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    infra_tenants_tfvars = "../../tests/terraform/tenants_tfvars_fixture/heredoc_tenants.tfvars"
  }

  expect_failures = [google_service_account_iam_member.deployer_admin]
}

# The owner checks a plan's account list against the tenants they meant to
# add, so the list is an output, and it is exactly the grants' keys.
run "the_granted_accounts_are_an_output_the_owner_can_read" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    infra_tenants_tfvars = "../../tests/terraform/tenants_tfvars_fixture/fixture.tfvars"
  }

  assert {
    condition     = output.deployer_admin_accounts == sort(keys(google_service_account_iam_member.deployer_admin))
    error_message = "output.deployer_admin_accounts is not exactly the accounts the deployer is granted serviceAccountAdmin on"
  }

  assert {
    condition     = contains(output.deployer_admin_accounts, "swarm-agent-worker-alpha") && !contains(output.deployer_admin_accounts, "swarm-agent-worker-smuggled")
    error_message = "the output does not list the fixture's tenants"
  }
}

# Putting the project-wide grant back by hand is refused at plan.
run "project_wide_service_account_admin_cannot_be_put_back" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    deployer_roles = ["roles/run.admin", "roles/iam.serviceAccountAdmin"]
  }

  expect_failures = [var.deployer_roles]
}
