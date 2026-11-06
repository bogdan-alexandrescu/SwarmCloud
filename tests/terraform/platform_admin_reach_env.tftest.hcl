# A platform admin's reach reaches swarm-api (docs/design/workspace-roles.md
# §4.2; lane ROLES-ON-DEV, part of #1518).
#
# swarm_api.settings reads PLATFORM_ADMIN_REACH with `_bool` (off by default;
# "true" is one of the spellings it reads as on). Until var.platform_admin_reach
# nothing in Terraform set it, so the reach was off in every environment and
# turning it on was a hand edit the next apply put back. These runs hold the
# rendering: off by default, on from the variable, swarm-api's alone, and the
# committed tfvars: dev sets it true, prod leaves it unset (false). A terraform
# test cannot load a tfvars file into a run, so the dev and prod runs set the
# values those files hold and assert the files as text.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  # Read as text: the committed tfvars are held to the runs below.
  tf_root = "../../terraform/infra"

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

run "prod_leaves_the_reach_off" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "prod"
  }

  assert {
    condition     = length(regexall("(?m)^\\s*platform_admin_reach\\s*=", file("${var.tf_root}/../environments/prod/prod.tfvars"))) == 0
    error_message = "terraform/environments/prod/prod.tfvars leaves platform_admin_reach unset, so prod keeps the default, off (ROLES-ON-DEV)"
  }

  assert {
    condition     = local.service_env["swarm-api"].PLATFORM_ADMIN_REACH == "false"
    error_message = "prod does not render the reach on: PLATFORM_ADMIN_REACH renders \"false\" unless the tfvars sets platform_admin_reach"
  }
}

run "dev_turns_the_reach_on_for_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # dev's value, as committed (asserted as text below).
  variables {
    platform_admin_reach = true
  }

  assert {
    condition     = length(regexall("(?m)^platform_admin_reach\\s*=\\s*true\\b", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets platform_admin_reach = true (ROLES-ON-DEV, owner decision 2026-10-19)"
  }

  assert {
    condition     = local.service_env["swarm-api"].PLATFORM_ADMIN_REACH == "true"
    error_message = "platform_admin_reach = true renders PLATFORM_ADMIN_REACH=\"true\", a spelling swarm_api.settings._bool reads as on"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "PLATFORM_ADMIN_REACH")
    ])
    error_message = "the reach is swarm-api's alone; no other service reads it"
  }
}
