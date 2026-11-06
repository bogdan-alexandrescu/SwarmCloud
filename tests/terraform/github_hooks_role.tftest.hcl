# The GitHub push receiver's Firestore role (lane HOOKS-ROLE, part of #1384;
# owner decision 2026-10-16 on PR 1406's question 1).
# terraform/bootstrap/github_hooks_role.tf defines swarmGithubHooksDedup; the
# owner applies it. terraform/infra/github_hooks.tf grants it to the rollup
# sweeper account the receiver runs as, in place of roles/datastore.user
# (tests/terraform/github_hooks.tftest.hcl holds that half). These runs hold:
#
#   * OFF BY DEFAULT nothing changes: no role, and the deployer's grantable
#     list -- so every projectIamAdmin chunk condition -- is what it was;
#   * on, the role carries datastore.entities.create and NOTHING else: no get,
#     list, update or delete, and no databases.* permission (the receiver's one
#     Firestore call is a REST createDocument, apps/swarm-github-hooks
#     clients.py FirestoreDeliveries.create, which no client library precedes);
#   * on, the role id is the one terraform/modules/custom_role_ids spells, its
#     description says managed-by=swarm-terraform, and its stage is GA;
#   * on, the CI deployer may grant it: it is in deployer_grantable_project_roles,
#     in exactly one hasOnly() chunk, no chunk holds more than 10 roles, and the
#     list gains that one role and nothing else;
#   * this root grants it to nobody: terraform/infra does.
#
# None of this proves what Firestore checks live; see the file's
# "NOT VERIFIED LIVE".

mock_provider "google" {
  source = "./mocks/google"
}

# terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1):
# a mock provider cannot import, so the bucket's provider values are the
# override's and its configuration is still planned and asserted.
override_resource {
  target = google_storage_bucket.state
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  infra_tenants_tfvars = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"

  # The grantable list is read through the deployer's conditions, which exist
  # only with the GitHub WIF on (deployer_iam.tftest.hcl does the same).
  enable_github_wif     = true
  github_repository     = "saga/agent-swarm-infra"
  deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

run "off_by_default_no_role_and_the_grantable_list_is_unchanged" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = length(google_project_iam_custom_role.github_hooks_dedup) == 0
    error_message = "with enable_github_hooks_dedup_role off (the default) swarmGithubHooksDedup is not defined"
  }

  assert {
    condition = alltrue([
      for r in local.deployer_grantable_project_roles : !strcontains(r, "swarmGithubHooksDedup")
    ])
    error_message = "with the role off it is not on the deployer's grantable list, so no projectIamAdmin chunk changes and the bootstrap plan is what it was"
  }

  assert {
    condition     = output.github_hooks_dedup_role.role == "" && length(output.github_hooks_dedup_role.permissions) == 0
    error_message = "with the role off the output names no role and no permission"
  }

  # The variable's DEFAULT is off (this run sets nothing). The owner turned the
  # role on at the bootstrap apply that created it, and a -var that is not
  # committed to terraform/bootstrap/terraform.tfvars makes the next plan from
  # main read it as false and plan to destroy it, so that one file records it
  # as true. No other tfvars file may set it.
  # Both halves in one condition: an assertion that names no object from the
  # configuration is refused by terraform (tests/terraform/settings_mac.tftest.hcl).
  assert {
    condition     = !var.enable_github_hooks_dedup_role && can(regex("(?m)^enable_github_hooks_dedup_role\\s*=\\s*true\\s*$", file("../../terraform/bootstrap/terraform.tfvars")))
    error_message = "enable_github_hooks_dedup_role must default to false, and terraform/bootstrap/terraform.tfvars must record it = true, the value the owner applied; left out, a plan from main plans to destroy the role"
  }
}

run "on_the_role_carries_entities_create_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_hooks_dedup_role = true
  }

  # The exact set: a get, list, update or delete added here would give a
  # public-facing service's identity read or write on every collection of the
  # platform's database, which no IAM condition can narrow on Firestore's data
  # plane (terraform/modules/iam/bindings.tf firestore_condition).
  assert {
    condition     = google_project_iam_custom_role.github_hooks_dedup[0].permissions == toset(["datastore.entities.create"])
    error_message = "swarmGithubHooksDedup holds datastore.entities.create and nothing else"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.github_hooks_dedup[0].role_id == "swarmGithubHooksDedup",
      google_project_iam_custom_role.github_hooks_dedup[0].project == "saga-agents-staging",
      google_project_iam_custom_role.github_hooks_dedup[0].stage == "GA",
      startswith(google_project_iam_custom_role.github_hooks_dedup[0].description, "managed-by=swarm-terraform"),
    ])
    error_message = "the role id is the one terraform/modules/custom_role_ids spells, defined in this project at GA, and its description says managed-by=swarm-terraform"
  }

  assert {
    condition = (
      output.github_hooks_dedup_role.role == "projects/saga-agents-staging/roles/swarmGithubHooksDedup"
      && output.github_hooks_dedup_role.permissions == tolist(["datastore.entities.create"])
    )
    error_message = "the output gives the owner the role's full name and its one permission, the rows to read the plan against"
  }
}

run "on_the_deployer_may_grant_it_in_one_chunk" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_hooks_dedup_role = true
  }

  assert {
    condition     = contains(local.deployer_grantable_project_roles, "projects/saga-agents-staging/roles/swarmGithubHooksDedup")
    error_message = "terraform/infra grants swarmGithubHooksDedup, so the scoped projectIamAdmin must allow it; add it to deployer_grantable_project_roles"
  }

  # One more role than the list with the role off, and that role alone.
  assert {
    condition = (
      toset(output.deployer_grantable_project_roles) == setunion(
        toset(run.off_by_default_no_role_and_the_grantable_list_is_unchanged.deployer_grantable_project_roles),
        toset(["projects/saga-agents-staging/roles/swarmGithubHooksDedup"]),
      )
      && length(output.deployer_grantable_project_roles) == length(run.off_by_default_no_role_and_the_grantable_list_is_unchanged.deployer_grantable_project_roles) + 1
    )
    error_message = "turning the dedup role on adds exactly swarmGithubHooksDedup to the grantable list and nothing else"
  }

  # hasOnly() refuses a list over 10 elements (#275), found only at apply.
  assert {
    condition = alltrue([
      for chunk in local.deployer_project_iam_admin_chunks : length(chunk) <= 10
    ])
    error_message = "a projectIamAdmin chunk holds more than 10 roles once swarmGithubHooksDedup is added; hasOnly() would refuse it at apply"
  }

  assert {
    condition = length([
      for k, c in local.deployer_project_iam_admin_conditions : k
      if strcontains(c, "\"projects/saga-agents-staging/roles/swarmGithubHooksDedup\"")
    ]) == 1
    error_message = "swarmGithubHooksDedup is in exactly one projectIamAdmin chunk's hasOnly() list"
  }

  assert {
    condition = alltrue([
      for k, m in google_project_iam_member.deployer_project_iam_admin :
      m.condition[0].expression == local.deployer_project_iam_admin_conditions[k]
    ]) && length(google_project_iam_member.deployer_project_iam_admin) == length(local.deployer_project_iam_admin_chunks)
    error_message = "every projectIamAdmin chunk binding carries its own chunk's condition, the one that now names swarmGithubHooksDedup among them"
  }
}

run "this_root_grants_the_role_to_nobody" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_hooks_dedup_role = true
  }

  # terraform/infra/github_hooks.tf is the one grant, behind enable_github_hooks.
  assert {
    condition = length(google_project_iam_custom_role.github_hooks_dedup) == 1 && !strcontains(
      replace(file("../../terraform/bootstrap/github_hooks_role.tf"), "/(?m)^\\s*#.*$/", ""),
      "google_project_iam_member",
    )
    error_message = "terraform/bootstrap/github_hooks_role.tf grants the role; it only defines it, and terraform/infra/github_hooks.tf grants it to the receiver's identity"
  }
}
