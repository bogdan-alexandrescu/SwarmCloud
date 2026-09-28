# CI can no longer change a custom role, and cannot grant swarmSecretLister.
#
# Owner decisions, 2026-09-25:
#
#   #79  roles/iam.roleAdmin comes off the CI deployer, and every custom role
#        terraform/infra and its modules defined moves to terraform/bootstrap,
#        which the owner applies. While CI held roleAdmin, one iam.roles.update
#        could add resourcemanager.projects.setIamPolicy to a custom role it
#        already held unconditioned, and its next project setIamPolicy --
#        roles/owner included -- was authorised by that binding, so the scoped
#        projectIamAdmin condition (#68) was never evaluated.
#   #69  the quota broker's swarmSecretLister grant moves to terraform/bootstrap
#        too, and the role leaves deployer_grantable_project_roles, so the scoped
#        projectIamAdmin cannot admit a grant of it -- to the broker or to CI
#        itself. The role carries project-wide secrets.setIamPolicy, which
#        reaches the other team's 63 secrets.
#
# Split from #150 at the owner's request (2026-09-27): this file now holds
# only the runs that assert something about terraform/bootstrap's OWN
# configuration -- roles/iam.roleAdmin off deployer_roles, and
# swarmSecretLister off deployer_grantable_project_roles -- which can only be
# true once this change lands, after lane/deployer-roles-forget-in-infra (the
# infra-side runs, which needed no bootstrap change) is released and the owner
# has applied bootstrap. Until then, and until this branch is rebased onto a
# main that has that release, "the_deployer_is_not_granted_role_admin" and the
# runs after it read a terraform/infra that still defines these roles and so
# do not apply cleanly -- expected, and the reason this PR stays a draft.
#
# Each run reads the configuration as merged, not the live policy: what the
# policy holds changes only when the owner applies bootstrap, and
# docs/runbooks/custom-roles-to-bootstrap.md is that step.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]
}

run "the_deployer_is_not_granted_role_admin" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  assert {
    condition     = !contains(keys(google_project_iam_member.deployer_roles), "roles/iam.roleAdmin")
    error_message = "the CI deployer is still granted roles/iam.roleAdmin: its iam.roles.update can widen any custom role CI holds, which bypasses every condition on the deployer's other grants (#79)"
  }

  # The reviewed list is the second key: a role not on it is refused at plan.
  assert {
    condition     = !contains(local.deployer_roles_reviewed, "roles/iam.roleAdmin")
    error_message = "roles/iam.roleAdmin is still on the list of roles deployer_roles may name"
  }

  # The control: every other role the deployer held is still granted, so this
  # run is not passing because WIF, or the whole list, went away.
  assert {
    condition = alltrue([
      for r in [
        "roles/artifactregistry.admin",
        "roles/cloudbuild.builds.editor",
        "roles/cloudscheduler.admin",
        "roles/compute.networkAdmin",
        "roles/compute.securityAdmin",
        "roles/container.admin",
        "roles/datastore.owner",
        "roles/iam.serviceAccountAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
        "roles/logging.configWriter",
        "roles/monitoring.editor",
        "roles/pubsub.admin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/run.admin",
        "roles/serviceusage.serviceUsageAdmin",
      ] : contains(keys(google_project_iam_member.deployer_roles), r)
    ])
    error_message = "a deployer role other than roles/iam.roleAdmin stopped being granted; taking roleAdmin off must move nothing else"
  }
}

run "swarm_secret_lister_is_not_a_role_ci_may_grant" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  assert {
    condition = !anytrue([
      for r in local.deployer_grantable_project_roles : endswith(r, "/roles/swarmSecretLister")
    ])
    error_message = "swarmSecretLister is still on deployer_grantable_project_roles: once projectIamAdmin is scoped, CI could still grant itself project-wide secrets.setIamPolicy over the other team's 63 secrets (#69)"
  }

  # The control: the five custom roles terraform/infra still grants at the
  # project level stay grantable, or the next release that touches one 403s.
  assert {
    condition = alltrue([
      for id in [
        "swarmGkeDispatcher",
        "swarmGkeReaper",
        "swarmJobDispatcher",
        "swarmJobReaper",
        "swarmTenantWorkerFirestore",
      ] : contains(local.deployer_grantable_project_roles, "projects/saga-agents-staging/roles/${id}")
    ])
    error_message = "a custom role terraform/infra still grants at the project level is no longer grantable; the release that next adds or removes one of those grants would 403"
  }
}

# Putting it back by hand is refused at plan, not discovered in a review.
#
# LAST IN THE FILE ON PURPOSE: an unmet expect_failures is an error, not an
# assertion failure, and terraform test skips every run after an error in the
# same file -- so anywhere earlier it would hide the runs below it whenever it
# fails.
run "role_admin_cannot_be_put_back_on_the_deployer" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles    = ["roles/artifactregistry.admin", "roles/iam.roleAdmin"]
  }

  # roleAdmin was on the reviewed list, so this used to plan cleanly; the
  # refusal is the change.
  expect_failures = [var.deployer_roles]
}
