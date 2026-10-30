# The deployer has its own switch, and the installer may impersonate it
# (DAYONE-BOOTSTRAP-DEPLOYER, N2 and N3, part of #2688).
#
# A Day One install renders enable_github_wif = false: it has no GitHub pool
# unless the CI add-on asks for one. Every pass after bootstrap pass 1 runs as
# the deployer, so the deployer and every grant it holds must exist without the
# pool (enable_deployer), and the person running the install must be able to
# mint its tokens (deployer_impersonator). Only the pool, the provider and the
# workloadIdentityUser bindings stay behind enable_github_wif.
#
# The last run is SwarmCloud's own bootstrap: enable_github_wif = true and
# neither new input set, as terraform/bootstrap/terraform.tfvars is committed.
# Every deployer address keeps its count, and the impersonation grant is
# absent, so its plan shows no change.

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
  project_id           = "swarm-install-test"
  frontend_iap_members = ["user:admin@example.com"]
}

run "an_install_creates_the_deployer_and_its_grants_without_the_pool" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = false
    enable_deployer       = true
    deployer_impersonator = "user:admin@example.com"
  }

  assert {
    condition     = length(google_service_account.deployer) == 1 && google_service_account.deployer[0].account_id == "swarm-tf-deployer"
    error_message = "enable_deployer must create swarm-tf-deployer without the GitHub pool: every install pass after bootstrap-1 runs as it (N2)"
  }

  assert {
    condition = alltrue([
      length(google_iam_workload_identity_pool.github) == 0,
      length(google_iam_workload_identity_pool_provider.github) == 0,
      length(google_service_account_iam_member.deployer_wif) == 0,
    ])
    error_message = "the pool, the provider and workloadIdentityUser stay behind enable_github_wif: an install federates nothing from GitHub"
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.deployer_roles) == length(setsubtract(toset(var.deployer_roles), var.deployer_scoped_roles)),
      length(google_project_iam_member.deployer_roles) > 0,
      length(google_project_iam_custom_role.secret_provisioner) == 1,
      length(google_project_iam_member.deployer_secrets) == 1,
      length(google_project_iam_member.deployer_storage) == 1,
      length(google_project_iam_custom_role.deployer_project_buckets) == 1,
      length(google_project_iam_member.deployer_project_buckets) == 1,
      length(google_service_account_iam_member.deployer_acts_as_cloudbuild) == 1,
      length(google_storage_bucket_iam_member.deployer_state) == 1,
      length(google_project_iam_member.deployer_reads_verify_logs) == 1,
      length(google_service_account_iam_member.deployer_admin) == length(local.deployer_admin_accounts),
      length(google_kms_crypto_key_iam_member.deployer_key_readers) == length(local.spec_deployer_reader_grants),
      length(google_kms_crypto_key_iam_member.deployer_key_readers) > 0,
    ])
    error_message = "the deployer's grants (deployer_roles, the secret provisioner, its storage, state, Cloud Build, verify-log, service-account-admin and spec-key grants) share its switch, not the pool's"
  }

  assert {
    condition = alltrue([
      length(google_service_account_iam_member.deployer_impersonator) == 1,
      google_service_account_iam_member.deployer_impersonator[0].role == "roles/iam.serviceAccountTokenCreator",
      google_service_account_iam_member.deployer_impersonator[0].member == "user:admin@example.com",
    ])
    error_message = "deployer_impersonator must grant roles/iam.serviceAccountTokenCreator to the installer, so every later pass can run as the deployer (N3)"
  }

  assert {
    # The account's name is unknown at plan, so the block is read from the file.
    condition = strcontains(
      replace(regex("(?s)resource \"google_service_account_iam_member\" \"deployer_impersonator\" \\{.*?\n\\}", file("../../terraform/bootstrap/deployer_impersonation.tf")), "/[ \t]+/", " "),
      "service_account_id = google_service_account.deployer[0].name",
    ) && length(google_service_account_iam_member.deployer_impersonator) == 1
    error_message = "the token-creator grant is on the deployer alone: at the project it would let the installer become every service account in it"
  }
}

run "neither_switch_creates_no_deployer_and_no_grant" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      length(google_service_account.deployer) == 0,
      length(google_project_iam_member.deployer_roles) == 0,
      length(google_project_iam_member.deployer_storage) == 0,
      length(google_storage_bucket_iam_member.deployer_state) == 0,
      length(google_service_account_iam_member.deployer_impersonator) == 0,
    ])
    error_message = "with enable_github_wif and enable_deployer both false, bootstrap creates no deployer and grants it nothing"
  }
}

run "an_impersonator_must_be_one_person" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_deployer       = true
    deployer_impersonator = "group:admins@example.com"
  }

  expect_failures = [var.deployer_impersonator]
}

run "an_impersonator_needs_a_deployer" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    deployer_impersonator = "user:admin@example.com"
  }

  expect_failures = [var.deployer_impersonator]
}

run "swarmclouds_own_bootstrap_plans_no_change" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # terraform/bootstrap/terraform.tfvars as committed: federation on, and
  # neither enable_deployer nor deployer_impersonator named.
  variables {
    enable_github_wif     = true
    github_repository     = "bogdan-alexandrescu/SwarmCloud"
    deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
  }

  assert {
    condition = alltrue([
      length(google_iam_workload_identity_pool.github) == 1,
      length(google_iam_workload_identity_pool_provider.github) == 1,
      length(google_service_account_iam_member.deployer_wif) == length(local.github_principals),
      length(google_service_account.deployer) == 1,
      length(google_project_iam_custom_role.secret_provisioner) == 1,
      length(google_project_iam_member.deployer_storage) == 1,
      length(google_project_iam_custom_role.deployer_project_buckets) == 1,
      length(google_project_iam_member.deployer_project_buckets) == 1,
      length(google_service_account_iam_member.deployer_acts_as_cloudbuild) == 1,
      length(google_storage_bucket_iam_member.deployer_state) == 1,
      length(google_project_iam_member.deployer_reads_verify_logs) == 1,
    ])
    error_message = "federation implies the deployer: every address SwarmCloud's bootstrap holds keeps its [0] instance, or its plan would destroy and recreate the deployer"
  }

  assert {
    condition     = length(google_service_account_iam_member.deployer_impersonator) == 0
    error_message = "SwarmCloud's own bootstrap names no deployer_impersonator, so no token-creator grant enters its plan"
  }

  assert {
    condition     = google_service_account.deployer[0].account_id == "swarm-tf-deployer"
    error_message = "the federated deployer keeps its account id"
  }
}
