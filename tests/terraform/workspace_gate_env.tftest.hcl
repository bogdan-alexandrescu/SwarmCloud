# swarm-api's WORKSPACE_GATE reaches its environment (W10, WD8;
# docs/workspaces.md §5.5).
#
# swarm_api.workspaces.gate_from_env reads it and ships off. Until W10 nothing
# here rendered it, so turning it on meant editing a revision by hand, which
# the next release undid. These runs hold the rendering: off by default, on
# from var.workspace_gate, and swarm-api's alone.
# tests/unit/control_plane/test_workspace_gate_dev.py holds that dev's tfvars
# turns it on and that the two words are ones the gate reads.

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

run "the_gate_renders_off_by_default_on_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_GATE == "off"
    error_message = "with workspace_gate unset swarm-api's WORKSPACE_GATE is off: on refuses every person without a ready workspace record"
  }

  assert {
    condition = alltrue([
      for svc, env in local.service_env :
      svc == "swarm-api" || !contains(keys(env), "WORKSPACE_GATE")
    ])
    error_message = "the workspace gate is swarm-api's alone"
  }
}

run "workspace_gate_turns_it_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    workspace_gate = true
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_GATE == "on"
    error_message = "workspace_gate = true renders WORKSPACE_GATE=on, the word swarm_api.workspaces.gate_from_env reads as on"
  }
}
