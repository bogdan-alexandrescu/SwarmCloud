# The execution catalogue, mirrored from the frozen Python contract.
#
# terraform/infra/locals.tf restates swarm_common.profiles because terraform
# cannot import Python. This file checks that restatement two different ways,
# and only one of them is a real drift detector:
#
#   The hand-written runs below ("resource_classes_match_the_frozen_catalogue",
#   "runner_backends_match_resolve_backend") state what the catalogue is SUPPOSED
#   to be. They are documentation with teeth -- they catch a terraform edit -- but
#   they compare terraform against terraform, so if profiles.py is edited they
#   drift away from Python alongside locals.tf and still pass.
#
#   "the_terraform_mirror_matches_the_python_catalogue" is the actual detector.
#   ./catalogue_mirror reads apps/common/swarm_common/profiles.py off disk and
#   parses it, and the run compares the parse against terraform's outputs. That
#   is the one that fails when Python moves and terraform does not -- the case
#   whose symptom in production is a Job resource sized for a profile the worker
#   is not running, which is exactly the class of bug nobody finds by reading.

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

  # Every image by digest, because the root refuses to plan without one. See
  # image_refs.tftest.hcl.
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

run "resource_classes_match_the_frozen_catalogue" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # RESOURCE_CLASSES: standard 4/8/20, browser 8/16/40, large 8/32/100.
  assert {
    condition     = output.resource_classes["standard"].cpu == 4 && output.resource_classes["standard"].memory_gib == 8 && output.resource_classes["standard"].disk_gib == 4
    error_message = "the standard class is 4 vCPU / 8 GiB / 20 GiB"
  }

  assert {
    condition     = output.resource_classes["browser"].cpu == 8 && output.resource_classes["browser"].memory_gib == 16 && output.resource_classes["browser"].disk_gib == 8
    error_message = "the browser class is 8 vCPU / 16 GiB / 40 GiB"
  }

  assert {
    condition     = output.resource_classes["large"].cpu == 8 && output.resource_classes["large"].memory_gib == 32 && output.resource_classes["large"].disk_gib == 16
    error_message = "the large class is 8 vCPU / 32 GiB / 100 GiB"
  }

  # units: 1, 2, 4 -- heavier classes cost more scheduling budget.
  assert {
    condition     = output.resource_classes["standard"].units == 1 && output.resource_classes["browser"].units == 2 && output.resource_classes["large"].units == 4
    error_message = "capacity units must match RESOURCE_CLASSES: 1, 2 and 4"
  }

  assert {
    condition = alltrue([
      for name, rc in output.resource_classes : rc.cpu <= 8 && rc.memory_gib <= 32
    ])
    error_message = "no class may exceed the Cloud Run Jobs ceiling; anything larger must be GKE-only"
  }
}

run "runner_backends_match_resolve_backend" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # Cloud Run Jobs takes everything it can hold. Chromium needs a large
  # /dev/shm that only GKE lets us control, so browser work stays on Autopilot.
  assert {
    condition = alltrue([
      for profile in ["mock", "generic", "claude-code", "codex"] :
      output.runner_backends[profile] == "CLOUD_RUN_JOB"
    ])
    error_message = "every profile Cloud Run can hold must run on Cloud Run Jobs"
  }

  assert {
    condition     = output.runner_backends["browser"] == "GKE_AUTOPILOT"
    error_message = "the browser profile runs on Autopilot: Chromium needs a /dev/shm size Cloud Run does not expose"
  }

  assert {
    condition     = length(output.runner_backends) == 5
    error_message = "five runner profiles: mock, generic, claude-code, codex, browser"
  }
}

run "every_pool_name_the_contract_can_produce_is_materialised" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # swarm_common.models.pool_names_for() produces exactly these shapes, and
  # evaluate_capacity() treats a MISSING pool document as unlimited -- so a pool
  # nobody created is not "unlimited by intent", it is a ceiling nobody chose.
  assert {
    condition     = contains(output.pool_names, "global")
    error_message = "the global pool must exist"
  }

  assert {
    condition = alltrue([
      for t in ["tenant:eng", "tenant:smoke"] : contains(output.pool_names, t)
    ])
    error_message = "every declared tenant needs its own pool"
  }

  assert {
    condition = alltrue([
      for c in ["resource:standard", "resource:browser", "resource:large"] : contains(output.pool_names, c)
    ])
    error_message = "every resource class needs a pool"
  }

  assert {
    condition = alltrue([
      for r in ["runner:mock", "runner:generic", "runner:claude-code", "runner:codex", "runner:browser"] :
      contains(output.pool_names, r)
    ])
    error_message = "every runner profile needs a pool"
  }

  assert {
    condition = alltrue([
      for b in ["backend:CLOUD_RUN_JOB", "backend:GKE_AUTOPILOT"] : contains(output.pool_names, b)
    ])
    error_message = "every backend needs a pool"
  }

  assert {
    condition     = contains(output.pool_names, "provider:anthropic") && contains(output.pool_names, "provider:openai")
    error_message = "every provider in the catalogue needs a pool"
  }

  # Provider quota is tracked per tenant because tenants bring their own keys:
  # one tenant's 429 must never throttle another's.
  assert {
    condition     = contains(output.pool_names, "provider:anthropic:tenant:eng") && contains(output.pool_names, "provider:openai:tenant:eng")
    error_message = "a per-tenant provider pool is required for every credential a tenant holds"
  }

  assert {
    condition = !anytrue([
      for name in output.pool_names : startswith(name, "provider:anthropic:tenant:smoke")
    ])
    error_message = "a tenant with no credential for a provider must not get that provider's per-tenant pool"
  }

  assert {
    condition = alltrue([
      for name, limit in output.pool_limits : limit > 0
    ])
    error_message = "every pool needs a positive, finite ceiling"
  }
}

run "jobs_exist_only_where_a_credential_does" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # A profile that needs a provider is only materialised for tenants holding a
  # credential for it. The rest would produce Job resources whose every
  # execution fails at start, when the correct behaviour is for the task to PARK
  # as CREDENTIAL_MISSING and cost nothing.
  assert {
    condition = alltrue([
      for name in ["swarm-job-eng-mock", "swarm-job-eng-generic", "swarm-job-eng-claude-code", "swarm-job-eng-codex"] :
      contains(output.job_names, name)
    ])
    error_message = "a tenant with anthropic and openai keys gets every Cloud Run profile"
  }

  assert {
    condition     = contains(output.job_names, "swarm-job-smoke-mock") && contains(output.job_names, "swarm-job-smoke-generic")
    error_message = "the credential-free profiles must exist for every tenant, so a smoke test works before anyone registers a key"
  }

  assert {
    condition     = !contains(output.job_names, "swarm-job-smoke-claude-code")
    error_message = "a tenant with no anthropic key must not get a claude-code Job resource"
  }

  assert {
    condition = !anytrue([
      for name in output.job_names : strcontains(name, "browser")
    ])
    error_message = "the browser profile runs on Autopilot; it has no Cloud Run Job"
  }

  assert {
    condition     = length(output.job_names) == 6
    error_message = "4 Cloud-Run profiles for eng plus 2 credential-free ones for smoke"
  }

  # Cloud Run only exposes memory-medium ephemeral volumes, so a workspace sized
  # at the full memory limit would OOM-kill the agent instead of failing a write.
  assert {
    condition = alltrue([
      for job, gib in output.workspace_size_gib : gib >= 1 && gib <= 4
    ])
    error_message = "every Cloud Run job here is the standard class: a 4 GiB workspace inside an 8 GiB container"
  }
}

run "every_resource_carries_the_destroy_guard_label" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # `make destroy` ABORTS if a plan would delete anything without this label.
  assert {
    condition     = output.labels["managed-by"] == "swarm-terraform"
    error_message = "destroy.sh refuses to delete anything lacking managed-by=swarm-terraform"
  }

  assert {
    condition     = output.labels["swarm-env"] == "dev"
    error_message = "the environment label is what separates dev from prod resources in one project"
  }

  assert {
    condition     = output.wake_topic == "swarm-scheduler-wake"
    error_message = "the wake topic name is derived in locals so the API can carry it without a module dependency"
  }
}

run "extra_labels_cannot_overwrite_the_destroy_guard_label" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    extra_labels = {
      "managed-by" = "someone-else"
    }
  }

  expect_failures = [var.extra_labels]
}

# ---------------------------------------------------------------------------
# The drift detector proper: terraform's mirror against the Python source.
# ---------------------------------------------------------------------------

run "the_python_catalogue_is_readable" {
  command = plan

  module {
    source = "./catalogue_mirror"
  }

  # A text parser that finds nothing returns an empty map, and an empty map
  # compares equal to nothing and fails no assertion. So the parse is checked
  # before it is trusted: if profiles.py moved or was reformatted past the
  # parser, this run fails here rather than the comparison below passing
  # vacuously.
  assert {
    condition     = output.profiles_file != ""
    error_message = "swarm_common/profiles.py was not found at any candidate path: the drift detector is reading nothing, so the catalogue comparison below would pass vacuously"
  }

  assert {
    condition     = length(output.resource_classes) == 3
    error_message = "expected 3 resource classes in profiles.py (standard, browser, large); the parser read a different number, so it no longer matches the file's literal form"
  }

  assert {
    condition     = length(output.runner_profiles) == 5
    error_message = "expected 5 runner profiles in profiles.py (mock, generic, claude-code, codex, browser); the parser read a different number"
  }

  assert {
    condition     = output.default_timeout_seconds > 0
    error_message = "RunnerProfile.timeout_seconds default could not be read; a profile that states no timeout would be compared against 0"
  }
}

run "the_terraform_mirror_matches_the_python_catalogue" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = length(output.resource_classes) == length(run.the_python_catalogue_is_readable.resource_classes) && alltrue([
      for name, rc in run.the_python_catalogue_is_readable.resource_classes :
      contains(keys(output.resource_classes), name)
      && output.resource_classes[name].cpu == rc.cpu
      && output.resource_classes[name].memory_gib == rc.memory_gib
      && output.resource_classes[name].disk_gib == rc.disk_gib
      && output.resource_classes[name].units == rc.units
    ])
    error_message = "local.resource_classes in terraform/infra/locals.tf no longer matches RESOURCE_CLASSES in swarm_common/profiles.py: the Job resources would be sized for a class the worker is not running"
  }

  assert {
    condition = length(output.runner_profiles) == length(run.the_python_catalogue_is_readable.runner_profiles) && alltrue([
      for name, rp in run.the_python_catalogue_is_readable.runner_profiles :
      contains(keys(output.runner_profiles), name)
      && output.runner_profiles[name].image == rp.image
      && output.runner_profiles[name].resource_class == rp.resource_class
      && output.runner_profiles[name].backend == rp.backend
      && output.runner_profiles[name].timeout_seconds == rp.timeout_seconds
    ])
    error_message = "local.runner_profiles in terraform/infra/locals.tf no longer matches RUNNER_PROFILES in swarm_common/profiles.py: image, resource class, backend or timeout has drifted"
  }

  # provider and the secret env-var names decide which tenants get a Job at all
  # and which Secret Manager entry each Job's environment is wired to, so they
  # are compared separately from the sizing. Terraform models "no provider" as
  # null and the parser as "", which is the same statement.
  assert {
    condition = alltrue([
      for name, rp in run.the_python_catalogue_is_readable.runner_profiles :
      (output.runner_profiles[name].provider == null ? "" : output.runner_profiles[name].provider) == rp.provider
      && output.runner_profiles[name].secret_env_names == rp.secret_env_names
    ])
    error_message = "the provider or the secret env-var names in terraform/infra/locals.tf no longer match swarm_common/profiles.py: a Job would be created for a tenant holding no key, or wired to the wrong secret"
  }
}
