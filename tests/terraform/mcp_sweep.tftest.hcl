# A per-run MCP credential is deleted when its task ends, by a sweep that runs
# by itself (docs/design/registered-mcp-servers.md §4.4, owner decision Q2;
# lane MCP-IAM, part of #1433), and the console can show a caller the
# addresses its MCP server must allowlist (§4.9).
#
# swarm-api's POST /v1/admin/mcp-credentials/sweep (lane MCP-API) deletes the
# Secret Manager secret of every `mcp_dispatch_credentials` row whose task is
# terminal or whose expires_at has passed, and marks the row destroyed. Held
# here:
#   * swarm-mcp-credentials-sweep, a Cloud Scheduler job, POSTs that route and
#     no other -- no query, no tenant, no second path -- every 10 minutes, as
#     the rollup-sweeper account (the identity swarm_api.auth.
#     ROLLUP_SWEEPER_ROUTES admits to it), never as the platform tick, with an
#     OIDC token for swarm-api's own URL;
#   * it carries managed-by=swarm-terraform, is not retried, and is one job
#     whether or not a tenant is registered;
#   * the module makes it only when enable_mcp_credentials_sweep is set, and
#     refuses the switch with no endpoint;
#   * the infra root's mcp_egress_addresses output is the NAT's reserved
#     addresses, the ones every worker's MCP call leaves through.
# The permission the sweep's delete needs is mcp_credentials.tftest.hcl's.

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

run "the_mcp_credentials_sweep_runs_every_ten_minutes_as_the_sweeper" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_mcp_credentials_sweep = true
    api_endpoint                 = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.mcp_credentials_sweep) == 1 && google_cloud_scheduler_job.mcp_credentials_sweep[0].name == "swarm-mcp-credentials-sweep"
    error_message = "the job is swarm-mcp-credentials-sweep, one of it"
  }

  # Only its route: the exact URI, so a query string, a tenant, another admin
  # route or a second path fails here.
  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/mcp-credentials/sweep"
    error_message = "the job calls swarm-api's MCP credential sweep and nothing else, with no tenant: the route sweeps every tenant's rows"
  }

  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].http_method == "POST"
    error_message = "the MCP credential sweep is a POST"
  }

  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].body == null && google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].headers == null
    error_message = "the job sends no body and no headers: the route takes nothing from its caller"
  }

  # Every 10 minutes (design §4.4), offset to :03, :13, ... so it is clear of
  # :00 and :30, where schedules bunch, and of the other sweeps' minutes.
  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].schedule == "3-59/10 * * * *"
    error_message = "the MCP credential sweep runs every 10 minutes, at :03, :13, :23, :33, :43 and :53"
  }

  assert {
    condition = (
      google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].oidc_token[0].service_account_email == output.rollup_sweeper_email
      && google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].oidc_token[0].service_account_email != var.tick_service_account
    )
    error_message = "the MCP credential sweep presents the rollup-sweeper account, the address swarm-api admits to POST /v1/admin/mcp-credentials/sweep, never the platform tick"
  }

  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    error_message = "the token is minted for swarm-api's own URL, as for every other job that calls swarm-api"
  }

  assert {
    condition     = startswith(google_cloud_scheduler_job.mcp_credentials_sweep[0].description, "managed-by=swarm-terraform;")
    error_message = "the job carries managed-by=swarm-terraform in its description, the label a scheduler job can carry"
  }

  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].attempt_deadline == "300s" && google_cloud_scheduler_job.mcp_credentials_sweep[0].retry_config[0].retry_count == 0
    error_message = "the sweep answers inside 300 s and is not retried: the next tick, ten minutes on, is the retry"
  }
}

# The switch on with no endpoint is refused at plan, never a job that calls
# nowhere.
run "the_mcp_credentials_sweep_refuses_an_empty_endpoint" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_mcp_credentials_sweep = true
  }

  expect_failures = [google_cloud_scheduler_job.mcp_credentials_sweep]
}

run "the_mcp_credentials_sweep_needs_no_registered_tenant" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_mcp_credentials_sweep = true
    api_endpoint                 = "https://swarm-api-abcdef-uc.a.run.app"
    rollup_tenant_ids            = []
  }

  assert {
    condition     = length(google_cloud_scheduler_job.mcp_credentials_sweep) == 1
    error_message = "the MCP credential sweep is one job, made whether or not any tenant is registered"
  }
}

# Off until swarm-api serves the route (MCP-API): until then every call would
# be refused, and the feature ships dark.
run "the_mcp_credentials_sweep_is_off_in_the_module_by_default" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    api_endpoint = "https://swarm-api-abcdef-uc.a.run.app"
  }

  assert {
    condition     = !var.enable_mcp_credentials_sweep && length(google_cloud_scheduler_job.mcp_credentials_sweep) == 0
    error_message = "the module makes the job only when enable_mcp_credentials_sweep is set"
  }
}

# The addresses a caller allowlists on its MCP server: the NAT's reserved
# addresses, which GKE pods and Cloud Run Jobs both leave through (§4.9).
run "the_infra_root_outputs_the_mcp_egress_addresses" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_resource {
    target          = module.network.google_compute_address.nat
    override_during = plan
    values = {
      address = "203.0.113.7"
    }
  }

  assert {
    condition     = output.mcp_egress_addresses == ["203.0.113.7", "203.0.113.7"]
    error_message = "mcp_egress_addresses must be every reserved NAT address (nat_static_ip_count, 2 by default), the addresses a registered MCP server allowlists"
  }

  assert {
    condition     = output.mcp_egress_addresses == output.nat_egress_addresses
    error_message = "the MCP egress addresses are the NAT's, the same ones model providers allowlist: an MCP call has no other way out"
  }
}

# The same addresses reach swarm-api's environment as MCP_EGRESS_ADDRESSES,
# which swarm_api.settings reads and GET /v1/mcp-servers serves as
# `egress_addresses` (lane MCP-API-B). Rendered from the NAT's reserved
# addresses themselves, the resource the output reads, so the page and the
# output cannot disagree and nobody types an address by hand.
run "swarm_api_env_carries_the_mcp_egress_addresses" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_resource {
    target          = module.network.google_compute_address.nat
    override_during = plan
    values = {
      address = "198.51.100.23"
    }
  }

  assert {
    condition     = local.service_env["swarm-api"].MCP_EGRESS_ADDRESSES == "198.51.100.23,198.51.100.23"
    error_message = "swarm-api's MCP_EGRESS_ADDRESSES must be every reserved NAT address, comma-separated, read from google_compute_address.nat"
  }

  assert {
    condition     = local.service_env["swarm-api"].MCP_EGRESS_ADDRESSES == join(",", output.mcp_egress_addresses)
    error_message = "the addresses swarm-api serves are the mcp_egress_addresses output's, never a second list"
  }

  assert {
    condition     = !contains(keys(local.service_env["swarm-scheduler"]), "MCP_EGRESS_ADDRESSES")
    error_message = "only swarm-api serves the addresses; no other service reads them"
  }
}

# The rollup sweeper account is terraform/bootstrap's since DAYONE-BOOT-2
# (platform_service_accounts.tf); modules/scheduler only reads it, so its
# description is held on the root that writes it.
run "the_sweeper_account_names_the_mcp_credentials_sweep" {
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
    condition     = strcontains(google_service_account.rollup_sweeper.description, "mcp-credentials-sweep") && length(google_service_account.rollup_sweeper.description) <= 256
    error_message = "the sweeper account's description names every job that presents it, inside the provider's 256 characters"
  }
}
