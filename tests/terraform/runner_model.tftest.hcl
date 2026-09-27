# The claude-code profile runs a pinned model, set here and nowhere else (#226).
#
# Owner decision of 2026-09-26: every claude-code step runs `claude-opus-5-5`,
# the model the operator's local lanes run, instead of whatever the CLI
# defaults to. The QA task that prompted it ran `claude-sonnet-5`, because no
# Job set MODEL at all. The value lives in ONE place, `local.runner_models` in
# terraform/infra/locals.tf, and reaches both kinds of Job that run agents:
#
#   * the Jobs Terraform creates, as `MODEL` in their environment
#     (`output.job_models`, the environment each Job's module entry carries,
#     not the local echoed back);
#   * the Jobs the scheduler creates for tenants Terraform does not list, as the
#     scheduler's `WORKER_MODELS` (`output.worker_models`), which
#     `CloudRunJobDispatcher._build_job` reads.
#
# The worker reads MODEL into `WorkerConfig.model` and the runner passes it to
# the CLI as `--model`. A caller's `input.model` is refused (invariant 10).
# Changing the model is a change to that local, and a release.

mock_provider "google" {}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@saga.xyz"
      providers = ["anthropic", "openai"]
    }
    smoke = {
      kind      = "group"
      principal = "swarm-smoke@saga.xyz"
      providers = []
    }
  }

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}

run "the_claude_code_job_runs_the_pinned_model" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = output.job_models["swarm-job-eng-claude-code"] == "claude-opus-5-5"
    error_message = "the claude-code Job must set MODEL=claude-opus-5-5; without it the CLI runs its own default model"
  }

  # The control: the output covers every Job, so "no MODEL on the others" below
  # is a statement about six Jobs and not about an empty map.
  assert {
    condition     = length(output.job_models) == length(output.job_names) && length(output.job_names) == 6
    error_message = "job_models must have one entry per Job: 4 Cloud-Run profiles for eng plus 2 credential-free ones for smoke"
  }

  # codex is an OpenAI CLI, where an Anthropic model name would fail every run;
  # mock and generic start no model at all.
  assert {
    condition = alltrue([
      for job, model in output.job_models : model == null if job != "swarm-job-eng-claude-code"
    ])
    error_message = "only the claude-code Job carries MODEL"
  }
}

run "the_scheduler_hands_the_same_model_to_the_jobs_it_creates" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = length(output.worker_models) == 1 && output.worker_models["claude-code"] == "claude-opus-5-5"
    error_message = "WORKER_MODELS must carry exactly the claude-code model, so a Job the scheduler creates runs what a Terraform Job runs"
  }
}
