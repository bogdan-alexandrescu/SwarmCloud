# swarm-api's search tool switch reaches its environment (lane SEARCH-TF,
# part of #1030; docs/search-tool.md).
#
# swarm_api.settings reads SEARCH_TOOL_ENABLED (off by default, owner decision
# 2026-10-13) and SEARCH_PROVIDER (one of swarm_api.search.PROVIDERS, refused
# at start otherwise). var.search_tool_enabled and var.search_provider render
# them, so turning the tool on is a tfvars change, not a hand edit an apply
# puts back. These runs hold the rendering: off by default, on from the
# variable, swarm-api only, and an unknown provider refused at plan rather
# than at swarm-api's start.


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

run "the_tool_is_off_by_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SEARCH_TOOL_ENABLED == "false"
    error_message = "the search tool ships off: SEARCH_TOOL_ENABLED renders \"false\" unless the tfvars turns it on"
  }
  assert {
    condition     = local.service_env["swarm-api"].SEARCH_PROVIDER == "brave"
    error_message = "the provider defaults to brave, the owner's choice (2026-10-13) and swarm_api.settings' default"
  }
}

run "the_variable_turns_it_on_for_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    search_tool_enabled = true
  }

  assert {
    condition     = local.service_env["swarm-api"].SEARCH_TOOL_ENABLED == "true"
    error_message = "search_tool_enabled = true renders SEARCH_TOOL_ENABLED=\"true\", the spelling swarm_api.settings._bool reads"
  }
  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "SEARCH_TOOL_ENABLED") &&
      !contains(keys(local.service_env[svc]), "SEARCH_PROVIDER")
    ])
    error_message = "the search switch is swarm-api's alone; no other service reads it"
  }
}

run "an_unknown_provider_is_refused_at_plan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    search_provider = "bing"
  }

  expect_failures = [var.search_provider]
}
