# swarm-api's refusal switches reach its environment (observer proposal I).
#
# A refusal added to swarm-api ships off, report-only (swarm_api.refusals,
# docs/api-refusals.md); var.api_refusals turns one on by rendering
# REFUSAL_<CODE>=on into swarm-api's environment. These runs hold the
# rendering: nothing by default, one name per entry, and only on swarm-api.


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

run "no_entry_renders_no_switch" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = length([for k in keys(local.service_env["swarm-api"]) : k if startswith(k, "REFUSAL_")]) == 0
    error_message = "with api_refusals empty every switched refusal stays at its default, off; nothing is rendered"
  }
}

run "an_entry_renders_its_switch_on_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    api_refusals = {
      probe_refusal = true
      Probe_Other   = false
    }
  }

  assert {
    condition     = local.service_env["swarm-api"].REFUSAL_PROBE_REFUSAL == "on"
    error_message = "true renders REFUSAL_<CODE>=on, the name swarm_api.refusals.env_name derives"
  }
  assert {
    condition     = local.service_env["swarm-api"].REFUSAL_PROBE_OTHER == "off"
    error_message = "false renders =off, upper-cased like the code's env name"
  }
  assert {
    condition     = !contains(keys(local.service_env["swarm-scheduler"]), "REFUSAL_PROBE_REFUSAL")
    error_message = "the switches are swarm-api's alone"
  }
}

run "a_key_that_is_not_a_code_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    api_refusals = {
      "not a code" = true
    }
  }

  expect_failures = [var.api_refusals]
}
