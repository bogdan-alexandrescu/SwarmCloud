# The work-contract switch reaches swarm-api (part of #2688).
#
# swarm_api.workcontract.enabled() reads SWARM_WORK_CONTRACTS as on only when
# it is exactly "on". Until var.work_contracts nothing in Terraform set it, so
# swarm-api ran with contracts off and refused every step naming a plugin,
# even after the owner enabled gstack and security-audit on Config > Plugins
# (BGD, 2026-11-06). These runs hold the rendering: "off" by default, both
# values rendered as given, swarm-api's alone (no other service reads it),
# the validation refusing any other value, and dev's committed value, "on",
# beside dev's contract_review_mode, "report" (BGD, 2026-11-06). A terraform
# test cannot load a tfvars file into a run, so the dev run sets the values
# the file holds and asserts the file as text.

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

run "the_default_renders_off_on_swarm_api_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_WORK_CONTRACTS == "off"
    error_message = "unset, work_contracts renders SWARM_WORK_CONTRACTS=\"off\": no contract is read, as before the variable"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "SWARM_WORK_CONTRACTS")
    ])
    error_message = "work contracts are read at submission, in swarm-api alone; no other service reads SWARM_WORK_CONTRACTS"
  }
}

run "on_renders_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    work_contracts = "on"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_WORK_CONTRACTS == "on"
    error_message = "work_contracts = \"on\" renders SWARM_WORK_CONTRACTS=\"on\": a step's signed contract and its plugins are read"
  }
}

run "dev_turns_contracts_on_and_review_to_report" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # dev's values, as committed (asserted as text below).
  variables {
    work_contracts       = "on"
    contract_review_mode = "report"
  }

  assert {
    condition     = length(regexall("(?m)^work_contracts\\s*=\\s*\"on\"", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets work_contracts = \"on\" (BGD, 2026-11-06)"
  }

  assert {
    condition     = length(regexall("(?m)^contract_review_mode\\s*=\\s*\"report\"", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets contract_review_mode = \"report\" (BGD, 2026-11-06)"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_WORK_CONTRACTS == "on"
    error_message = "dev renders SWARM_WORK_CONTRACTS=\"on\""
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_CONTRACT_REVIEW == "report"
    error_message = "dev renders SWARM_CONTRACT_REVIEW=\"report\""
  }
}

run "an_unknown_value_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # workcontract.enabled() reads anything but "on" as off; the plan refuses
  # it instead, so a misspelt "On" or "true" never ships as a silent off.
  variables {
    work_contracts = "On"
  }

  expect_failures = [var.work_contracts]
}

run "a_boolean_word_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    work_contracts = "true"
  }

  expect_failures = [var.work_contracts]
}

run "an_empty_value_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    work_contracts = ""
  }

  expect_failures = [var.work_contracts]
}
