# The GitHub App's public settings reach swarm-api's environment (#780).
#
# Until 2026-10-07 github_app_id / _client_id / _slug went only to the
# `github_app` output. swarm_api.forgeapp reads GITHUB_APP_ID,
# GITHUB_APP_CLIENT_ID and GITHUB_APP_SLUG from its environment, so the App
# registered that day answered 503 "not configured" on every onboarding call
# until the owner's live test 1 found it. These runs hold the wiring.
# GITHUB_APP_NAME joined them on 2026-10-19 (#1647): the console names the
# App by the name the deployment is configured with, never a hard-coded one.


mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "example-project"
  environment = "dev"

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "the_app_settings_reach_swarm_api" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    frontend_hostname    = "swarm.example.com"
    enable_github_app    = true
    github_app_id        = "123456"
    github_app_client_id = "Iv23liexampleclient0"
    github_app_slug      = "swarmcloud-example"
    github_app_name      = "SwarmCloud Example"
  }

  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_ID == "123456"
    error_message = "swarm-api reads GITHUB_APP_ID; without it forgeapp cannot name the App"
  }
  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_CLIENT_ID == "Iv23liexampleclient0"
    error_message = "swarm-api reads GITHUB_APP_CLIENT_ID; without it every /v1/onboarding/github call is a 503"
  }
  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_SLUG == "swarmcloud-example"
    error_message = "swarm-api reads GITHUB_APP_SLUG for the install link"
  }
  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_NAME == "SwarmCloud Example"
    error_message = "swarm-api reads GITHUB_APP_NAME and serves it as app_name: the console names the App by it, never by a hard-coded name"
  }
}

run "no_app_renders_empty_settings" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].GITHUB_APP_ID == "" &&
      local.service_env["swarm-api"].GITHUB_APP_CLIENT_ID == "" &&
      local.service_env["swarm-api"].GITHUB_APP_SLUG == "" &&
      local.service_env["swarm-api"].GITHUB_APP_NAME == ""
    )
    error_message = "with no App registered the four settings are empty, which forgeapp reads as not configured"
  }
}
