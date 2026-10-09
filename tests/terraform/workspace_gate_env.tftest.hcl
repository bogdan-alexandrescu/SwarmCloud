# swarm-api's personal-workspace gate reaches its environment (docs/workspaces.md
# §5.5, owner decision WD8; W10 of #847).
#
# swarm_api.workspaces.gate_from_env reads WORKSPACE_GATE; var.workspace_gate
# renders it. Off by default, because on refuses every personal tenant without
# a `ready` workspace record. The value dev runs is held by
# tests/unit/scripts/test_dev_workspace_gate.py, which reads dev.tfvars: a
# terraform test cannot load an environment's tfvars file.


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

run "the_workspace_gate_ships_off" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_GATE == "off"
    error_message = "with workspace_gate unset swarm-api's WORKSPACE_GATE is off: on refuses every personal tenant without a ready record (WD8)"
  }
  assert {
    condition     = !contains(keys(local.service_env["swarm-scheduler"]), "WORKSPACE_GATE")
    error_message = "the gate is swarm-api's alone: it is checked at submission, never at admission (docs/workspaces.md §5.4)"
  }
}

run "workspace_gate_on_renders_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    workspace_gate = "on"
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_GATE == "on"
    error_message = "workspace_gate = \"on\" renders WORKSPACE_GATE=on, the value swarm_api.workspaces.gate_from_env reads as on"
  }
}

run "workspace_gate_refuses_anything_but_off_or_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    workspace_gate = "onn"
  }

  expect_failures = [var.workspace_gate]
}
