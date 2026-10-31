# The additional-repositories switch reaches swarm-api
# (docs/design/multi-repository-steps.md §6 "Feature flag", part of #1434).
#
# swarm_api.settings reads ADDITIONAL_REPOSITORIES_ENABLED with `_bool` (off
# by default; "true" is one of the spellings it reads as on). Until
# var.additional_repositories_enabled nothing in Terraform set it, so turning
# the field on was a hand edit the next apply put back. These runs hold the
# rendering: off by default, on from the variable, and swarm-api's alone. The
# count and size limits are platform settings in Firestore, not environment,
# so nothing here renders them.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "additional_repositories_are_off_by_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].ADDITIONAL_REPOSITORIES_ENABLED == "false"
    error_message = "additional repositories ship off: ADDITIONAL_REPOSITORIES_ENABLED renders \"false\" unless the tfvars sets additional_repositories_enabled"
  }
}

run "the_variable_turns_them_on_for_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    additional_repositories_enabled = true
  }

  assert {
    condition     = local.service_env["swarm-api"].ADDITIONAL_REPOSITORIES_ENABLED == "true"
    error_message = "additional_repositories_enabled = true renders ADDITIONAL_REPOSITORIES_ENABLED=\"true\", a spelling swarm_api.settings._bool reads as on"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "ADDITIONAL_REPOSITORIES_ENABLED")
    ])
    error_message = "the additional-repositories switch is swarm-api's alone; no other service reads it"
  }
}
