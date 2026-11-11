# terraform/bootstrap/plugin_bundle_runner.tf: the two rows of the managed
# plugins' bundle job that terraform/infra cannot make
# (docs/design/managed-plugins.md §1.4.2, the owner's change of 11 November
# 2026). What is held here:
#
#   * swarmPluginBundleRunner holds exactly run.jobs.run,
#     run.jobs.runWithOverrides and run.executions.get: no update (so its
#     holder can never change the job's image, command or identity), no
#     cancel, no logs;
#   * the release deployer acts as the builder account alone, and only when
#     the deployer exists; nothing in this file makes swarm-api act as the
#     builder.

mock_provider "google" {}

override_resource {
  target = google_storage_bucket.state
}

variables {
  project_id           = "example-project"
  frontend_iap_members = ["domain:example.com"]
}

run "the_runner_role_holds_exactly_three_permissions" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = google_project_iam_custom_role.plugin_bundle_runner.role_id == "swarmPluginBundleRunner"
    error_message = "the bundle job's runner role is swarmPluginBundleRunner"
  }

  assert {
    condition     = toset(google_project_iam_custom_role.plugin_bundle_runner.permissions) == toset(["run.executions.get", "run.jobs.run", "run.jobs.runWithOverrides"])
    error_message = "swarmPluginBundleRunner holds exactly run.jobs.run, run.jobs.runWithOverrides and run.executions.get (managed-plugins.md section 1.4.2)"
  }

  assert {
    condition     = startswith(google_project_iam_custom_role.plugin_bundle_runner.description, "managed-by=swarm-terraform;")
    error_message = "the runner role is labelled managed-by=swarm-terraform in its description"
  }

  assert {
    condition     = length(google_service_account_iam_member.deployer_acts_as_plugin_bundler) == 0
    error_message = "without the deployer there is nobody to act as the builder"
  }
}

run "only_the_deployer_acts_as_the_builder" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_deployer = true
  }

  # The mock provider leaves every computed attribute unknown; these give the
  # two accounts the values the binding is compared against.
  override_resource {
    target          = google_service_account.plugin_bundler
    override_during = plan
    values = {
      name  = "projects/example-project/serviceAccounts/swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
      email = "swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.deployer
    override_during = plan
    values = {
      name  = "projects/example-project/serviceAccounts/swarm-tf-deployer@example-project.iam.gserviceaccount.com"
      email = "swarm-tf-deployer@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = (
      length(google_service_account_iam_member.deployer_acts_as_plugin_bundler) == 1
      && google_service_account_iam_member.deployer_acts_as_plugin_bundler[0].role == "roles/iam.serviceAccountUser"
      && google_service_account_iam_member.deployer_acts_as_plugin_bundler[0].service_account_id == "projects/example-project/serviceAccounts/swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
    )
    error_message = "the release deployer holds serviceAccountUser on the builder account alone, so infra can deploy a job that runs as it"
  }

  assert {
    condition     = google_service_account_iam_member.deployer_acts_as_plugin_bundler[0].member == "serviceAccount:swarm-tf-deployer@example-project.iam.gserviceaccount.com"
    error_message = "the act-as on the builder is the release deployer's, never swarm-api's"
  }
}
