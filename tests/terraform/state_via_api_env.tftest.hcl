# The per-tenant state switch reaches swarm-scheduler (ISO-SCHED phase 2,
# docs/design/tenant-settings-integrity.md §4.6; part of #1457).
#
# scheduler.settings reads SWARM_STATE_VIA_API_TENANTS as a comma-separated
# list of tenant ids, and scheduler.dispatch switches every attempt of a
# listed tenant onto swarm-api's state routes. These runs hold the rendering:
# empty by default (nobody switched, so the variable changes no live
# behaviour), the list from the variable, the scheduler's alone, and refused
# at plan without child tasks -- a switched worker spends the child path's
# registration nonce at state/open, so without SWARM_CHILD_KEY it cannot start.

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

run "nobody_is_switched_by_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    # MUTATION: default state_via_api_tenants to anything but [] and this
    # names a tenant nobody switched.
    condition     = local.service_env["swarm-scheduler"].SWARM_STATE_VIA_API_TENANTS == ""
    error_message = "state_via_api_tenants ships empty: SWARM_STATE_VIA_API_TENANTS renders \"\" on swarm-scheduler, so no tenant is switched"
  }
}

run "the_variable_switches_the_listed_tenants_on_the_scheduler_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks    = true
    state_via_api_tenants = ["eng", "u-alice"]
  }

  assert {
    # MUTATION: render the list with any separator but "," and
    # scheduler.settings reads one tenant named "eng u-alice".
    condition     = local.service_env["swarm-scheduler"].SWARM_STATE_VIA_API_TENANTS == "eng,u-alice"
    error_message = "state_via_api_tenants renders onto swarm-scheduler as the comma-separated SWARM_STATE_VIA_API_TENANTS scheduler.settings reads"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-api", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "SWARM_STATE_VIA_API_TENANTS")
    ])
    error_message = "the state switch list is the scheduler's alone: it is the only component that writes a worker's environment"
  }
}

run "switching_a_tenant_needs_child_tasks" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    state_via_api_tenants = ["eng"]
  }

  # MUTATION: drop the enable_child_tasks validation and this plans, and every
  # switched worker exits at startup with no nonce to open its attempt with.
  expect_failures = [var.state_via_api_tenants]
}

run "an_entry_that_is_not_a_tenant_id_is_refused_at_plan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks    = true
    state_via_api_tenants = ["*"]
  }

  expect_failures = [var.state_via_api_tenants]
}
