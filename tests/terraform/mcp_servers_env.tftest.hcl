# The registered-MCP-servers switch reaches swarm-api, and brings its sweep
# (docs/design/registered-mcp-servers.md §4.4 and §6, part of #1433).
#
# swarm_api.mcp_servers reads SWARM_MCP_SERVERS and treats exactly "on" as on
# (mcp_servers_enabled). Until var.mcp_servers_enabled nothing in Terraform
# set it, so the only way to turn the feature on was a hand edit the next
# apply put back. These runs hold the rendering and the wiring:
#   * off by default: swarm-api carries "off" and the credential sweep job
#     does not exist;
#   * on from the variable: swarm-api carries "on", the one spelling read as
#     on, no other service carries the key, and the root creates
#     swarm-mcp-credentials-sweep through module.scheduler.
# The job's own shape (route, identity, schedule) is mcp_sweep.tftest.hcl's;
# swarm-api's read and delete grant is mcp_credentials.tftest.hcl's.

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

run "mcp_servers_are_off_by_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_MCP_SERVERS == "off"
    error_message = "registered MCP servers ship dark: SWARM_MCP_SERVERS renders \"off\" unless the tfvars sets mcp_servers_enabled"
  }

  assert {
    condition     = !contains(output.scheduler_jobs, "swarm-mcp-credentials-sweep")
    error_message = "with the feature off the root creates no MCP credential sweep: no credential is ever stored for it to delete"
  }
}

run "the_variable_turns_them_on_for_swarm_api_and_creates_the_sweep" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    mcp_servers_enabled = true
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_MCP_SERVERS == "on"
    error_message = "mcp_servers_enabled = true renders SWARM_MCP_SERVERS=\"on\", the only value swarm_api.mcp_servers.mcp_servers_enabled reads as on"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "SWARM_MCP_SERVERS")
    ])
    error_message = "the MCP switch is swarm-api's alone; no other service reads it"
  }

  # The root wires the module's job in: without it a per-run credential
  # outlives its task until its Secret Manager ttl, not ten minutes (Q2).
  assert {
    condition     = contains(output.scheduler_jobs, "swarm-mcp-credentials-sweep")
    error_message = "mcp_servers_enabled = true must create swarm-mcp-credentials-sweep through module.scheduler"
  }
}
