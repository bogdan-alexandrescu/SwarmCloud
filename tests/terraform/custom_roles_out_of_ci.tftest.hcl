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
# Each run holds one half of that. They read the configuration as merged, not
# the live policy: what the policy holds changes only when the owner applies
# bootstrap, and docs/runbooks/custom-roles-to-bootstrap.md is that step.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]
}

# The files CI applies define no custom role and read none. CI holds no
# iam.roles.* permission once roleAdmin is gone, so either would 403.
run "ci_defines_and_reads_no_custom_role" {
  command = plan

  module {
    source = "./custom_role_inventory"
  }

  # The control: the scan read files and matches real declarations. bootstrap
  # defines swarmSecretProvisioner and swarmDeployerProjectBuckets at least.
  assert {
    condition     = output.terraform_dir != "" && output.ci_files_read > 0 && length(output.bootstrap_declarations) >= 2
    error_message = "the inventory read no terraform/infra or terraform/modules file, or found no custom role even in terraform/bootstrap: the assertion below would be comparing against nothing"
  }

  assert {
    condition     = length(output.ci_declarations) == 0
    error_message = "terraform/infra or a module under terraform/modules defines or reads a custom IAM role. CI holds no iam.roles.* permission (roles/iam.roleAdmin is off the deployer, #79), so the release would 403 on it. Custom roles are defined in terraform/bootstrap, which the owner applies; infra names them by the ids in terraform/modules/custom_role_ids."
  }
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

# Putting it back by hand is refused at plan, not discovered in a review.
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

run "terraform_infra_no_longer_grants_the_broker_swarm_secret_lister" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    artifact_bucket  = "swarm-artifacts-saga-agents-staging"
    labels           = { "managed-by" = "swarm-terraform" }
    gke_cluster_name = "swarm-autopilot"
    gke_location     = "us-central1"
  }

  assert {
    condition = !anytrue([
      for k, b in google_project_iam_member.plain : endswith(b.role, "/roles/swarmSecretLister")
    ])
    error_message = "modules/iam still grants the quota broker swarmSecretLister; that grant is terraform/bootstrap's (#69), and a CI-applied grant of it needs the role on the grantable list"
  }

  # The control: the broker keeps the grants that stay in terraform/infra.
  assert {
    condition = alltrue([
      contains(keys(google_project_iam_member.plain), "swarm-quota-broker:roles/monitoring.viewer"),
      contains(keys(google_project_iam_member.plain), "swarm-quota-broker:roles/logging.logWriter"),
    ])
    error_message = "the broker lost a project grant other than swarmSecretLister"
  }
}
