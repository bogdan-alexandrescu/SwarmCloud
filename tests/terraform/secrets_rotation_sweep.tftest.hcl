# The workspace secrets store's 24-hour rotation grace ends by itself (#1347,
# owner decision Q5: a rotated-out version stays ENABLED for 24 hours, then
# is DISABLED, never destroyed).
#
# swarm-api's POST /v1/admin/workspace-secrets/rotation-sweep
# (routes/workspace_secrets.py, SEC-STORE-API) disables every version whose
# grace has passed. Until this job nothing called it, so a rotated-out value
# stayed readable for ever. Held here:
#   * swarm-workspace-secrets-rotation-sweep, a Cloud Scheduler job, POSTs
#     that route once an hour as the rollup-sweeper account -- the identity
#     swarm_api.auth.ROLLUP_SWEEPER_ROUTES admits to it -- never as the
#     platform tick, with an OIDC token for swarm-api's own URL;
#   * it is one job, made whether or not a tenant is registered (the route
#     takes no tenant: a personal workspace is no tenant of var.tenants);
#   * the infra root creates it in every environment.
# The permission the sweep's disable needs is provider_keys.tftest.hcl's.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  labels      = { "managed-by" = "swarm-terraform" }

  # The scheduler module's own inputs.
  wake_topic_name         = "swarm-scheduler-wake"
  scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
  reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
  quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
  tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
  publisher_members = {
    api = "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
  }

  # The monitoring module's own inputs.
  service_names               = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
  wake_subscription           = "swarm-scheduler-wake-sub"
  dead_letter_subscription    = "swarm-scheduler-wake-dlq-sub"
  safety_tick_job             = "swarm-scheduler-tick"
  extra_notification_channels = ["projects/saga-agents-staging/notificationChannels/1234567890"]

  # The infra root's.
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

run "the_rotation_sweep_runs_hourly_as_the_sweeper" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_secrets_rotation_sweep = true
    api_endpoint                  = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = google_cloud_scheduler_job.secrets_rotation_sweep[0].name == "swarm-workspace-secrets-rotation-sweep"
    error_message = "the job is swarm-workspace-secrets-rotation-sweep"
  }

  assert {
    condition     = google_cloud_scheduler_job.secrets_rotation_sweep[0].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/workspace-secrets/rotation-sweep"
    error_message = "the job calls swarm-api's rotation sweep, with no tenant: the route sweeps every workspace"
  }

  assert {
    condition     = google_cloud_scheduler_job.secrets_rotation_sweep[0].http_target[0].http_method == "POST"
    error_message = "the rotation sweep is a POST"
  }

  # Hourly: a version is disabled at most an hour after its 24-hour grace,
  # and an idle sweep reads the active records only.
  assert {
    condition     = google_cloud_scheduler_job.secrets_rotation_sweep[0].schedule == "41 * * * *"
    error_message = "the rotation sweep runs once an hour, at :41, clear of the other sweeps' minutes"
  }

  assert {
    condition = (
      google_cloud_scheduler_job.secrets_rotation_sweep[0].http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_scheduler_job.secrets_rotation_sweep[0].http_target[0].oidc_token[0].service_account_email == output.rollup_sweeper_email
      && google_cloud_scheduler_job.secrets_rotation_sweep[0].http_target[0].oidc_token[0].service_account_email != var.tick_service_account
    )
    error_message = "the rotation sweep presents the rollup-sweeper account, the address swarm-api admits to POST /v1/admin/workspace-secrets/rotation-sweep, never the platform tick"
  }

  assert {
    condition     = google_cloud_scheduler_job.secrets_rotation_sweep[0].http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    error_message = "the token is minted for swarm-api's own URL, as for every other job that calls swarm-api"
  }

  assert {
    condition     = startswith(google_cloud_scheduler_job.secrets_rotation_sweep[0].description, "managed-by=swarm-terraform;")
    error_message = "the job carries managed-by=swarm-terraform in its description, the label a scheduler job can carry"
  }

  assert {
    condition     = google_cloud_scheduler_job.secrets_rotation_sweep[0].attempt_deadline == "300s" && google_cloud_scheduler_job.secrets_rotation_sweep[0].retry_config[0].retry_count == 0
    error_message = "the sweep answers inside 300 s and is not retried: the next hour is the retry, and a failed disable is retried by it"
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-workspace-secrets-rotation-sweep")
    error_message = "scheduler_job_names must list the rotation sweep"
  }
}

# The switch on with no endpoint is refused at plan, never a job that calls
# nowhere.
run "the_rotation_sweep_refuses_an_empty_endpoint" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_secrets_rotation_sweep = true
  }

  expect_failures = [google_cloud_scheduler_job.secrets_rotation_sweep]
}

run "the_rotation_sweep_needs_no_registered_tenant" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_secrets_rotation_sweep = true
    api_endpoint                  = "https://swarm-api-abcdef-uc.a.run.app"
    rollup_tenant_ids             = []
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-workspace-secrets-rotation-sweep")
    error_message = "the rotation sweep is one job, made whether or not any tenant is registered"
  }
}

run "the_rotation_sweep_is_off_in_the_module_by_default" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    api_endpoint = "https://swarm-api-abcdef-uc.a.run.app"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.secrets_rotation_sweep) == 0
    error_message = "the module makes the job only when enable_secrets_rotation_sweep is set; the root sets it"
  }
}

# The root wires the module's job in: removing it fails here as well as in
# the module runs above.
run "the_infra_root_creates_the_rotation_sweep" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = contains(output.scheduler_jobs, "swarm-workspace-secrets-rotation-sweep")
    error_message = "the infra root must create swarm-workspace-secrets-rotation-sweep: without it a rotated-out workspace secret stays enabled for ever"
  }
}

# The rollup sweeper account is terraform/bootstrap's since DAYONE-BOOT-2
# (platform_service_accounts.tf); modules/scheduler only reads it, so its
# description is held on the root that writes it.
run "the_sweeper_account_names_the_secrets_rotation_sweep" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1);
  # a mock provider cannot import.
  override_resource {
    target = google_storage_bucket.state
  }

  variables {
    project_id           = "saga-agents-staging"
    frontend_iap_members = ["domain:example.com"]
  }

  assert {
    condition     = strcontains(google_service_account.rollup_sweeper.description, "secrets-rotation-sweep")
    error_message = "the sweeper account's description names every job that presents it"
  }
}
