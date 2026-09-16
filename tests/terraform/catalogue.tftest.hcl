# The execution catalogue, mirrored from the frozen Python contract.
#
# terraform/infra/locals.tf restates swarm_common.profiles because terraform
# cannot import Python. This file is the drift detector for that restatement:
# every number below is copied from swarm_common/profiles.py, so if the two ever
# disagree, this fails rather than a worker being sized for a profile it is not
# running.

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
}

run "resource_classes_match_the_frozen_catalogue" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # RESOURCE_CLASSES: standard 4/8/20, browser 8/16/40, large 8/32/100.
  assert {
    condition     = output.resource_classes["standard"].cpu == 4 && output.resource_classes["standard"].memory_gib == 8 && output.resource_classes["standard"].disk_gib == 20
    error_message = "the standard class is 4 vCPU / 8 GiB / 20 GiB"
  }

  assert {
    condition     = output.resource_classes["browser"].cpu == 8 && output.resource_classes["browser"].memory_gib == 16 && output.resource_classes["browser"].disk_gib == 40
    error_message = "the browser class is 8 vCPU / 16 GiB / 40 GiB"
  }

  assert {
    condition     = output.resource_classes["large"].cpu == 8 && output.resource_classes["large"].memory_gib == 32 && output.resource_classes["large"].disk_gib == 100
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
