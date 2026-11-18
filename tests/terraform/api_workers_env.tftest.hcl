# swarm-api's uvicorn worker count reaches its environment (owner's choice,
# 2026-11-10: two workers on dev, once the per-process caps hold per
# instance -- swarm_api.workers).
#
# var.api_workers renders SWARM_API_WORKERS into swarm-api's environment,
# which images/swarm-api/Dockerfile passes to uvicorn --workers and
# swarm_api.workers divides each per-instance cap by. These runs hold the
# rendering: "1" by default, the variable's value when set, only on
# swarm-api, and a count below 1 refused.


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

run "the_default_renders_one_worker" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_API_WORKERS == "1"
    error_message = "with api_workers unset swarm-api runs the one worker it always ran"
  }
}

run "the_variable_renders_its_count_on_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    api_workers = 2
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_API_WORKERS == "2"
    error_message = "api_workers = 2 renders SWARM_API_WORKERS=2 into swarm-api's environment"
  }
  assert {
    condition     = !contains(keys(local.service_env["swarm-scheduler"]), "SWARM_API_WORKERS")
    error_message = "the worker count is swarm-api's alone"
  }
}

run "a_count_below_one_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    api_workers = 0
  }

  expect_failures = [var.api_workers]
}
