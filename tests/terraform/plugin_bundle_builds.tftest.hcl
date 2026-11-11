# swarm-api starts a managed plugin's bundle build as the builder account and
# reads its status, and can do nothing else with Cloud Build
# (docs/design/managed-plugins.md §1.4.2, §15.2).
# terraform/bootstrap/plugin_bundle_builds.tf makes the two rows
# terraform/infra/plugin-bundles.tf leaves out. These runs hold:
#
#   * swarmPluginBuildStarter carries cloudbuild.builds.create and
#     cloudbuild.builds.get and nothing else;
#   * swarm-api alone holds it, through one project grant;
#   * swarm-api's roles/iam.serviceAccountUser is on swarm-plugin-bundler
#     alone: an account-level row, never project-wide;
#   * nothing in either row names another team's resource or a tenant.
#
# None of this proves what Cloud Build does live; see the file's "NOT
# VERIFIED LIVE".

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
  project_id = "example-project"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  # modules/iam's.
  artifact_bucket  = "swarm-artifacts-example-project"
  labels           = { "managed-by" = "swarm-terraform" }
  gke_cluster_name = "swarm-autopilot"
  gke_location     = "us-central1"
}

run "the_build_starter_role_carries_create_and_get_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = google_project_iam_custom_role.plugin_build_starter.permissions == toset(["cloudbuild.builds.create", "cloudbuild.builds.get"])
    error_message = "swarmPluginBuildStarter must carry cloudbuild.builds.create and cloudbuild.builds.get and nothing else: no list, update, cancel or log read"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.plugin_build_starter.role_id == "swarmPluginBuildStarter",
      google_project_iam_custom_role.plugin_build_starter.project == "example-project",
      google_project_iam_custom_role.plugin_build_starter.stage == "GA",
    ])
    error_message = "the role is swarmPluginBuildStarter, GA, in this project"
  }

  assert {
    condition     = startswith(google_project_iam_custom_role.plugin_build_starter.description, "managed-by=swarm-terraform;")
    error_message = "a custom role has no labels; it says managed-by=swarm-terraform in its description"
  }

  # The API caps a custom role's description at 256 characters.
  assert {
    condition     = length(google_project_iam_custom_role.plugin_build_starter.description) <= 256
    error_message = "swarmPluginBuildStarter's description is over IAM's 256-character cap"
  }
}

run "swarm_api_alone_starts_builds" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.plugin_build_starter.project == "example-project",
      google_project_iam_member.plugin_build_starter.member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com",
      google_project_iam_member.plugin_build_starter.role == "projects/example-project/roles/swarmPluginBuildStarter",
    ])
    error_message = "swarm-api, alone, holds swarmPluginBuildStarter, granted through the role resource"
  }

  # Cloud Build evaluates no resource attribute, and a build does not exist
  # before its create (the file's NO CONDITION): a condition here would only
  # look like a bound.
  assert {
    condition     = length(google_project_iam_member.plugin_build_starter.condition) == 0
    error_message = "the build starter grant carries no condition; the bound is the actAs row on swarm-plugin-bundler"
  }
}

run "the_act_as_row_is_on_the_builder_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # A mock account's name is a random string; the builder's is given its own,
  # so the row below matches only if it is on the builder.
  override_resource {
    target          = google_service_account.plugin_bundler
    override_during = plan
    values = {
      name   = "projects/example-project/serviceAccounts/swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
      email  = "swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
      member = "serviceAccount:swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = alltrue([
      google_service_account_iam_member.api_acts_as_plugin_bundler.service_account_id == "projects/example-project/serviceAccounts/swarm-plugin-bundler@example-project.iam.gserviceaccount.com",
      google_service_account_iam_member.api_acts_as_plugin_bundler.role == "roles/iam.serviceAccountUser",
      google_service_account_iam_member.api_acts_as_plugin_bundler.member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com",
    ])
    error_message = "swarm-api's roles/iam.serviceAccountUser is on swarm-plugin-bundler, and on that one account"
  }

  assert {
    condition     = length(google_service_account_iam_member.api_acts_as_plugin_bundler.condition) == 0
    error_message = "the actAs row carries no condition: it is already on one account"
  }

  # Neither row reaches a tenant worker or the other team's resources: the
  # members are swarm-api, the scope is this project's builder account.
  assert {
    condition = alltrue([
      for s in [
        google_service_account_iam_member.api_acts_as_plugin_bundler.service_account_id,
        google_service_account_iam_member.api_acts_as_plugin_bundler.member,
        google_project_iam_member.plugin_build_starter.member,
        google_project_iam_member.plugin_build_starter.role,
        google_project_iam_custom_role.plugin_build_starter.description,
        ] : alltrue([
          for f in ["swarm-tenant-", "swarm-worker", "agents-", "peer-app-", "gke-", "default"] :
          !strcontains(s, f)
      ])
    ])
    error_message = "a plugin build row names a tenant worker or another team's resource"
  }
}
