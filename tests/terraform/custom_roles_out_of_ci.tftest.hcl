# CI can no longer change a custom role: terraform/infra and its modules
# define none any more.
#
# Owner decision, 2026-09-25 (#79): every custom role terraform/infra and its
# modules defined moves to terraform/bootstrap, which the owner applies.
# Split from PR #150 (2026-09-27): this file has only the runs that assert
# something about terraform/infra and terraform/modules alone, provable
# without any change to terraform/bootstrap. The runs that assert
# roles/iam.roleAdmin is off the CI deployer, and that swarmSecretLister is not
# on deployer_grantable_project_roles, need terraform/bootstrap's own change
# (dropping roleAdmin from deployer_roles) and land with that change, in the
# follow-up PR that also carries the owner's bootstrap apply.
#
# Each run reads the configuration as merged, not the live policy: what the
# policy holds changes only when the owner applies bootstrap.

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
  # defines swarmSecretProvisioner and swarmDeployerProjectBuckets at least --
  # true on main today, before this PR's platform_roles.tf adds the other
  # eight, so this control does not depend on the bootstrap-side change.
  assert {
    condition     = output.terraform_dir != "" && output.ci_files_read > 0 && length(output.bootstrap_declarations) >= 2
    error_message = "the inventory read no terraform/infra or terraform/modules file, or found no custom role even in terraform/bootstrap: the assertion below would be comparing against nothing"
  }

  assert {
    condition     = length(output.ci_declarations) == 0
    error_message = "terraform/infra or a module under terraform/modules defines or reads a custom IAM role. CI holds no iam.roles.* permission (roles/iam.roleAdmin is off the deployer, #79), so the release would 403 on it. Custom roles are defined in terraform/bootstrap, which the owner applies; infra names them by the ids in terraform/modules/custom_role_ids."
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
