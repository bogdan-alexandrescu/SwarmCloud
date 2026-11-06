# A claude-code step runs a pinned model, set here and nowhere else (#226),
# and since #1435 it is pinned PER MODEL CLASS.
#
# Owner decision of 2026-09-26: every claude-code step runs `claude-opus-5-5`,
# the model the operator's local lanes run, instead of whatever the CLI
# defaults to. The QA task that prompted it ran `claude-sonnet-5`, because no
# Job set MODEL at all.
#
# Owner decisions of 2026-10-17 (#1435, docs/design/model-per-step.md §4.2):
# a step names a model CLASS from the frozen vocabulary
# (`swarm_common.profiles.MODEL_CLASSES`), and the ID behind each class lives
# in ONE place, `local.runner_models` in terraform/infra/locals.tf, keyed by
# class: opus (the default), fable, sonnet, haiku. It reaches:
#
#   * the Jobs Terraform creates, as `MODEL` in their environment: the pin of
#     the profile's `default_model_class` (`output.job_models`, the
#     environment each Job's module entry carries, not the local echoed back);
#   * the scheduler's `WORKER_MODELS`, profile -> the default class's pin,
#     DERIVED from the class map for one release so that a scheduler image
#     rolled back alone still finds the map it reads (`output.worker_models`);
#   * the scheduler's and swarm-api's `WORKER_CLASS_MODELS`, the class map
#     itself, which each holds to the classes at start (MODEL-SCHED,
#     MODEL-API).
#
# The worker reads MODEL into `WorkerConfig.model` and the runner passes it to
# the CLI as `--model`. A caller's `input.model` is refused (invariant 10).
# Changing a model is a change to that local, and a release.

# source: the shared defaults every suite that plans terraform/infra needs
# (mocks/google/kms.tfmock.hcl -- the step-spec key's enabled version 1).
mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
    smoke = {
      kind      = "group"
      principal = "swarm-smoke@example.com"
      providers = []
    }
  }

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

run "the_claude_code_job_runs_the_pinned_model" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = output.job_models["swarm-job-eng-claude-code"] == "claude-opus-5-5"
    error_message = "the claude-code Job must set MODEL=claude-opus-5-5; without it the CLI runs its own default model"
  }

  # indexer is claude-code on the indexer image (contract request 48), so an
  # index run keeps claude-code's model. Both now run on GKE Autopilot
  # (contract requests 53 and 62); these are their rollback Jobs, which must
  # run the same model the pods do.
  assert {
    condition     = output.job_models["swarm-job-eng-indexer"] == output.job_models["swarm-job-eng-claude-code"]
    error_message = "the indexer Job must run claude-code's model: it is claude-code on another image"
  }

  # The control: the output covers every Job, so "no MODEL on the others" below
  # is a statement about seven Jobs and not about an empty map.
  assert {
    condition     = length(output.job_models) == length(output.job_names) && length(output.job_names) == 7
    error_message = "job_models must have one entry per Job: 5 Cloud Run Jobs for eng (claude-code's and indexer's rollback Jobs included) plus 2 credential-free ones for smoke"
  }

  # codex is an OpenAI CLI, where an Anthropic model name would fail every run;
  # mock and generic start no model at all.
  assert {
    condition = alltrue([
      for job, model in output.job_models : model == null if !contains(["swarm-job-eng-claude-code", "swarm-job-eng-indexer"], job)
    ])
    error_message = "only the claude-code and indexer Jobs carry MODEL"
  }
}

run "the_scheduler_hands_the_same_model_to_the_jobs_it_creates" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # Exactly today's map, entry for entry: deriving it from the class map
  # (#1435) must not move a single profile's model.
  assert {
    condition = output.worker_models == {
      "claude-code"            = "claude-opus-5-5"
      "claude-code-review"     = "claude-opus-5-5"
      "indexer"                = "claude-opus-5-5"
      "claude-code-browser"    = "claude-opus-5-5"
      "claude-code-large"      = "claude-opus-5-5"
      "claude-code-lightpanda" = "claude-opus-5-5"
    }
    error_message = "WORKER_MODELS must carry exactly the claude-code model, and claude-code-review's, indexer's, claude-code-browser's, claude-code-large's and claude-code-lightpanda's (the same: claude-code under its own account, #295; on the indexer image, contract request 48; on the browser image, contract request 67; on the large class, contract request 113; on the Lightpanda image, contract request 127). claude-code, indexer, claude-code-browser, claude-code-large and claude-code-lightpanda run on GKE Autopilot (contract requests 53, 62, 67, 113 and 127), so WORKER_MODELS is the only way their MODEL reaches a pod"
  }
}

# #1435: the class map. Four keys, exactly the frozen vocabulary's, and each ID
# carries its own class's family word, so a `haiku = "claude-opus-5-5"` typo
# fails here before it quietly bills Opus. The full rule (the family word AND a
# generation of at least 5) is `ModelClass.admits`, applied to this map by
# scripts/lib/check-contract-parity.sh and, at start, by the scheduler.
run "each_model_class_is_pinned_to_an_id_of_its_own_family" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = sort(keys(local.runner_models)) == tolist(["fable", "haiku", "opus", "sonnet"])
    error_message = "local.runner_models must be keyed by exactly the four model classes of swarm_common.profiles.MODEL_CLASSES (opus, fable, sonnet, haiku)"
  }

  assert {
    condition     = alltrue([for class, id in local.runner_models : startswith(id, "claude-${class}-")])
    error_message = "every pin in local.runner_models must be claude-<its own class>-...: an ID of another family runs a model nobody asked for"
  }

  # The owner's pins of 2026-10-17 (Q3).
  assert {
    condition = local.runner_models == {
      opus   = "claude-opus-5-5"
      fable  = "claude-fable-5-1"
      sonnet = "claude-sonnet-5-5"
      haiku  = "claude-haiku-5-5"
    }
    error_message = "the class pins are the owner's of 2026-10-17: opus claude-opus-5-5, fable claude-fable-5-1, sonnet claude-sonnet-5-5, haiku claude-haiku-5-5"
  }
}

run "the_scheduler_and_swarm_api_read_the_same_class_map" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = jsondecode(local.service_env["swarm-scheduler"].WORKER_CLASS_MODELS) == local.runner_models
    error_message = "the scheduler's WORKER_CLASS_MODELS must be local.runner_models: it renders MODEL for a step's class from it"
  }

  assert {
    condition     = jsondecode(local.service_env["swarm-api"].WORKER_CLASS_MODELS) == local.runner_models
    error_message = "swarm-api's WORKER_CLASS_MODELS must be local.runner_models: it fills TaskCreate.model from it and serves the classes"
  }

  # The Jobs' MODEL is the pin of the profile's default class, read from the
  # mirror, not a second copy of the ID.
  assert {
    condition = alltrue([
      for profile in ["claude-code", "indexer"] :
      output.job_models["swarm-job-eng-${profile}"] == local.runner_models[local.runner_profiles[profile].default_model_class]
    ])
    error_message = "a classed profile's Cloud Run Job must carry MODEL = runner_models[its default_model_class]"
  }

  # R4 of the owner's decisions of 2026-10-17: there is NO enablement flag.
  # Lane order alone keeps the classes unreachable until the scheduler, the
  # worker and the API have merged, so no service carries one.
  assert {
    condition = alltrue([
      for service, env in local.service_env : !contains(keys(env), "MODEL_CLASSES_ENABLED")
    ])
    error_message = "MODEL_CLASSES_ENABLED must not exist (owner decision R4, 2026-10-17: no model_classes_enabled flag)"
  }
}
