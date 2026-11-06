# swarm-api no longer carries the issue sweeper's switch (Schedules S9, second
# change, 2026-10-12; docs/schedules.md §8.1 step 4).
#
# SWEEP_ENABLED gated POST /v1/admin/issues/sweep, the route the per-tenant
# issue_sweep Cloud Scheduler job called. The tenant's `issue-sweep` schedule
# replaced the job, and the job, the route, the switch and var.enable_issue_sweep
# were deleted together. A switch left rendered would gate nothing and read as
# though the old sweep could still be turned on.


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

run "swarm_api_renders_no_sweep_switch" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = !contains(keys(local.service_env["swarm-api"]), "SWEEP_ENABLED")
    error_message = "swarm-api's environment carries no SWEEP_ENABLED: the switch and the route it gated were removed on 2026-10-12"
  }

  # The control: swarm-api's environment is rendered at all, so the absence
  # above is not an empty map's.
  assert {
    condition     = contains(keys(local.service_env["swarm-api"]), "ROLLUP_SWEEPER_USERS")
    error_message = "swarm-api's environment still names the sweeper identity its remaining admin routes admit"
  }
}
