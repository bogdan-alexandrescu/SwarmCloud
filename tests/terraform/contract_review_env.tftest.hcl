# The contract review's mode reaches swarm-api (docs/design/agent-quality/
# jev-contract.md Q7, owner's answer 7; part of #2688).
#
# swarm_api.contractreview reads SWARM_CONTRACT_REVIEW as off, report or
# enforce, and anything else (unset included) as off. Until
# var.contract_review_mode nothing in Terraform set it, so turning the review
# on was a hand edit the next apply put back. These runs hold the rendering:
# "off" by default, each of the three values rendered as given, swarm-api's
# alone, the validation refusing any other value, and dev's committed value,
# "report" (BGD, 2026-11-06; "off" from 2026-11-04). A terraform test cannot
# load a tfvars file into a run, so the dev run sets the value the file holds
# and asserts the file as text.

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
    condition     = local.service_env["swarm-api"].SWARM_CONTRACT_REVIEW == "off"
    error_message = "unset, contract_review_mode renders SWARM_CONTRACT_REVIEW=\"off\": no review runs, as before the variable"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "SWARM_CONTRACT_REVIEW")
    ])
    error_message = "the contract review runs at submission, in swarm-api alone; no other service reads SWARM_CONTRACT_REVIEW"
  }
}

run "dev_reviews_in_report_mode" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # dev's value, as committed (asserted as text below).
  variables {
    contract_review_mode = "report"
  }

  assert {
    condition     = length(regexall("(?m)^contract_review_mode\\s*=\\s*\"report\"", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets contract_review_mode = \"report\" (BGD, 2026-11-06)"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_CONTRACT_REVIEW == "report"
    error_message = "dev renders SWARM_CONTRACT_REVIEW=\"report\""
  }
}

run "report_renders_report" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    contract_review_mode = "report"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_CONTRACT_REVIEW == "report"
    error_message = "contract_review_mode = \"report\" renders SWARM_CONTRACT_REVIEW=\"report\": review, log, never refuse"
  }
}

run "enforce_renders_enforce" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    contract_review_mode = "enforce"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_CONTRACT_REVIEW == "enforce"
    error_message = "contract_review_mode = \"enforce\" renders SWARM_CONTRACT_REVIEW=\"enforce\": the review refuses"
  }
}

run "an_unknown_mode_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # contractreview.mode() reads a typo as off; the plan refuses it instead,
  # so a misspelt "enforce" never ships as a silent off.
  variables {
    contract_review_mode = "Enforce"
  }

  expect_failures = [var.contract_review_mode]
}

run "an_empty_mode_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    contract_review_mode = ""
  }

  expect_failures = [var.contract_review_mode]
}
