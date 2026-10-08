# The GitHub App's public settings reach swarm-api's environment (#780).
#
# Until 2026-10-07 github_app_id / _client_id / _slug went only to the
# `github_app` output. swarm_api.forgeapp reads GITHUB_APP_ID,
# GITHUB_APP_CLIENT_ID and GITHUB_APP_SLUG from its environment, so the App
# registered that day answered 503 "not configured" on every onboarding call
# until the owner's live test 1 found it. These runs hold the wiring.


mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}

run "the_app_settings_reach_swarm_api" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    frontend_hostname    = "swarm.saga.xyz"
    enable_github_app    = true
    github_app_id        = "5229127"
    github_app_client_id = "Iv23lipzgYrbQvuJdmZg"
    github_app_slug      = "swarmcloud-saga"
  }

  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_ID == "5229127"
    error_message = "swarm-api reads GITHUB_APP_ID; without it forgeapp cannot name the App"
  }
  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_CLIENT_ID == "Iv23lipzgYrbQvuJdmZg"
    error_message = "swarm-api reads GITHUB_APP_CLIENT_ID; without it every /v1/onboarding/github call is a 503"
  }
  assert {
    condition     = local.service_env["swarm-api"].GITHUB_APP_SLUG == "swarmcloud-saga"
    error_message = "swarm-api reads GITHUB_APP_SLUG for the install link"
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
      local.service_env["swarm-api"].GITHUB_APP_SLUG == ""
    )
    error_message = "with no App registered the three settings are empty, which forgeapp reads as not configured"
  }
}
