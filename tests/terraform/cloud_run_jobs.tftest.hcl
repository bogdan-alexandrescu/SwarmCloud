# Cloud Run Jobs is the PRIMARY execution backend, and the two properties that
# matter most here are both invisible in a console screenshot: retries belong to
# the scheduler, and Spot does not exist anywhere.

mock_provider "google" {}

variables {
  project_id      = "saga-agents-staging"
  network         = "projects/saga-agents-staging/global/networks/swarm-vpc"
  subnetwork      = "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1"
  artifact_bucket = "swarm-artifacts-saga-agents-staging"
  labels          = { "managed-by" = "swarm-terraform" }

  # Mirrors swarm_common.profiles.RESOURCE_CLASSES.
  resource_classes = {
    standard = { cpu = 4, memory_gib = 8, disk_gib = 20 }
    browser  = { cpu = 8, memory_gib = 16, disk_gib = 40 }
    large    = { cpu = 8, memory_gib = 32, disk_gib = 100 }
  }

  jobs = {
    "swarm-job-eng-claude-code" = {
      tenant_id             = "eng"
      runner_profile        = "claude-code"
      resource_class        = "standard"
      service_account_email = "swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base:test"
      timeout_seconds       = 7200
      # checkov:skip=CKV_SECRET_6:a Secret Manager secret ID, not a credential. No key material exists in this repository.
      secret_env = { ANTHROPIC_API_KEY = "swarm-tenant-eng-anthropic" }
      env        = { RUNNER_PROFILE = "claude-code", TENANT_ID = "eng" }
    }
    "swarm-job-eng-large" = {
      tenant_id             = "eng"
      runner_profile        = "generic"
      resource_class        = "large"
      service_account_email = "swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base:test"
    }
  }
}

run "retries_belong_to_the_scheduler_not_to_cloud_run" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  # Cloud Run's own retry re-runs the SAME container with the SAME fencing
  # generation. Retries mint a new attempt and a new generation, which only the
  # scheduler can do (CONTRACT.md invariant 5).
  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.this :
      job.template[0].template[0].max_retries == 0
    ])
    error_message = "max_retries must be 0: a Cloud Run retry would resurrect a stale fencing generation"
  }

  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.this :
      job.template[0].task_count == 1 && job.template[0].parallelism == 1
    ])
    error_message = "fan-out is the scheduler's job; parallelism here would create demand no lease covers"
  }

  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.this :
      job.template[0].template[0].execution_environment == "EXECUTION_ENVIRONMENT_GEN2"
    ])
    error_message = "second-generation execution environment is required for volumes and full Linux compatibility"
  }
}

run "sizing_matches_the_frozen_resource_classes" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].containers[0].resources[0].limits["cpu"] == "4"
    error_message = "the standard class is 4 vCPU"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].containers[0].resources[0].limits["memory"] == "8Gi"
    error_message = "the standard class is 8 GiB"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-large"].template[0].template[0].containers[0].resources[0].limits["cpu"] == "8"
    error_message = "the large class is 8 vCPU"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-large"].template[0].template[0].containers[0].resources[0].limits["memory"] == "32Gi"
    error_message = "the large class is 32 GiB"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].timeout == "7200s"
    error_message = "the profile's timeout must reach the Job resource"
  }
}

run "the_workspace_volume_cannot_oom_the_agent" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  # Cloud Run only exposes memory-medium ephemeral volumes, so the workspace and
  # the agent process share one budget. A volume sized at the whole memory limit
  # turns a full workspace into a SIGKILL -- no checkpoint, no park, attempt
  # gone. Below the limit it is an ENOSPC the worker can act on.
  assert {
    condition = alltrue([
      for class, gib in output.workspace_size_gib_by_class :
      gib < var.resource_classes[class].memory_gib
    ])
    error_message = "a workspace sized at the container's full memory limit OOM-kills the agent instead of failing a write"
  }

  assert {
    condition = alltrue([
      for class, gib in output.workspace_size_gib_by_class : gib >= 1
    ])
    error_message = "every workspace needs at least a gibibyte"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].volumes[0].empty_dir[0].size_limit == "4Gi"
    error_message = "the standard class workspace is half of its 8 GiB memory limit"
  }

  assert {
    condition     = google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].containers[0].volume_mounts[0].mount_path == "/workspace"
    error_message = "the workspace must be mounted where the worker expects it"
  }
}

run "execution_is_private_and_tenant_scoped" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  # ALL_TRAFFIC, so provider calls leave through the swarm Cloud NAT carrying
  # the reserved addresses a provider allow-lists -- and so the execution never
  # holds a public address of its own.
  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.this :
      job.template[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC"
    ])
    error_message = "executions egress through the swarm VPC, never from a public address"
  }

  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.this :
      job.template[0].template[0].service_account == "swarm-agent-worker-${job.labels["swarm-tenant"]}@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "Cloud Run pins the SA on the JOB, so each tenant needs its own Job resource running as its own identity"
  }

  # A secret reference names the tenant's own secret. Another tenant's secret is
  # unreadable by this SA, so a doctored reference fails at start rather than
  # leaking (invariant 9).
  assert {
    condition = length([
      for e in google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].containers[0].env :
      e if length(e.value_source) > 0 && e.value_source[0].secret_key_ref[0].secret == "swarm-tenant-eng-anthropic"
    ]) == 1
    error_message = "the provider key must arrive as a Secret Manager reference to this tenant's own secret"
  }

  assert {
    condition = alltrue([
      for e in google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].containers[0].env :
      length(e.value_source) == 0 || startswith(e.value_source[0].secret_key_ref[0].secret, "swarm-tenant-eng-")
    ])
    error_message = "a job may only reference its own tenant's secrets"
  }

  assert {
    condition = alltrue([
      for e in google_cloud_run_v2_job.this["swarm-job-eng-claude-code"].template[0].template[0].containers[0].env :
      length(e.value_source) == 0 || e.value == null || e.value == ""
    ])
    error_message = "a provider key must never be a literal env value; it is read from Secret Manager at start"
  }

  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.this :
      job.labels["managed-by"] == "swarm-terraform" && job.labels["swarm-gc-exempt"] == "true"
    ])
    error_message = "terraform-managed Jobs must be labelled so the reconciler's garbage collection skips them"
  }
}

run "a_job_name_outside_the_swarm_prefix_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  variables {
    jobs = {
      "agents-staging-job" = {
        tenant_id             = "eng"
        runner_profile        = "generic"
        resource_class        = "standard"
        service_account_email = "swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
        image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base:test"
      }
    }
  }

  expect_failures = [var.jobs]
}

run "a_resource_class_cloud_run_cannot_honour_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  variables {
    resource_classes = {
      standard = { cpu = 3, memory_gib = 8, disk_gib = 20 }
    }
    jobs = {}
  }

  expect_failures = [var.resource_classes]
}

run "a_workspace_that_claims_all_of_memory_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run_jobs"
  }

  variables {
    workspace_memory_fraction = 1.0
  }

  expect_failures = [var.workspace_memory_fraction]
}
