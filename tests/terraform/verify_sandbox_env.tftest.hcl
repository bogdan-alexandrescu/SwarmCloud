# The swarm-verify acceptance job reads its sandbox repository from its own
# environment (DAYONE-VERIFY-SANDBOX-ENV, part of #2688).
#
# scripts/acceptance/config.sh reads SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL
# and falls back to the live deployment's sandbox. verify-remote.sh runs the
# job with --args only, so its environment is the job template's: unless
# terraform renders the variable there, another company's acceptance would
# push to the live deployment's sandbox.
#
#   1. unset (null, the default): the job carries no such env, so the live
#      deployment's plan shows no change;
#   2. set: the job's container env carries it, with the value given.

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

run "verify_job_carries_no_sandbox_env_when_unset" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # MUTATION: render the env unconditionally (an empty value, or the live
  # sandbox as a default) and the live deployment's plan changes.
  assert {
    condition = !contains(
      [for e in google_cloud_run_v2_job.verify.template[0].template[0].containers[0].env : e.name],
      "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL",
    )
    error_message = "with acceptance_sandbox_repository_url unset, the swarm-verify job carries no SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL: the live deployment's plan must show no change"
  }

  # The control: the job's env is rendered at all, so the absence above is
  # not an empty list's.
  assert {
    condition = contains(
      [for e in google_cloud_run_v2_job.verify.template[0].template[0].containers[0].env : e.name],
      "API_URL",
    )
    error_message = "the swarm-verify job's env still carries API_URL"
  }
}

run "verify_job_carries_the_sandbox_env_when_set" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    acceptance_sandbox_repository_url = "https://github.com/example-org/swarm-sandbox.git"
  }

  # MUTATION: drop the dynamic env block and the job never receives the
  # value, so config.sh falls back to the live deployment's sandbox.
  assert {
    condition = [
      for e in google_cloud_run_v2_job.verify.template[0].template[0].containers[0].env : e.value
      if e.name == "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL"
    ] == ["https://github.com/example-org/swarm-sandbox.git"]
    error_message = "with acceptance_sandbox_repository_url set, the swarm-verify job carries SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL once, with that value"
  }
}

run "sandbox_repository_url_must_be_an_https_git_url" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    acceptance_sandbox_repository_url = "git@github.com:example-org/swarm-sandbox.git"
  }

  expect_failures = [var.acceptance_sandbox_repository_url]
}
