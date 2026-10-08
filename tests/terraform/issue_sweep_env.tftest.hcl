# swarm-api's issue-sweep switch reaches its environment (owner decisions
# 2026-10-08, docs/issue-runs.md "Sweeper").
#
# POST /v1/admin/issues/sweep starts nothing unless SWEEP_ENABLED is on
# (swarm_api.settings.sweep_enabled); var.enable_issue_sweep renders it. Off
# by default, by the new-refusals-ship-off rule: a swept run auto-approves its
# plan and auto-merges its pull request.


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

run "the_issue_sweep_ships_off" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWEEP_ENABLED == "false"
    error_message = "with enable_issue_sweep unset swarm-api's SWEEP_ENABLED is false: the sweep starts nothing until an operator turns it on"
  }
  assert {
    condition     = !contains(keys(local.service_env["swarm-scheduler"]), "SWEEP_ENABLED")
    error_message = "the switch is swarm-api's alone"
  }
}

run "enable_issue_sweep_turns_it_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_issue_sweep = true
  }

  assert {
    condition     = local.service_env["swarm-api"].SWEEP_ENABLED == "true"
    error_message = "enable_issue_sweep = true renders SWEEP_ENABLED=true, the value swarm_api.settings reads as on"
  }
}
