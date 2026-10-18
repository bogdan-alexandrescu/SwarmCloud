# The per-run MCP credential sweep and the NAT addresses a caller allowlists
# (docs/design/registered-mcp-servers.md §4.4 and §4.9; lane MCP-IAM, part of
# #1433).
#
# swarm-api's POST /v1/admin/mcp-credentials/sweep (MCP-API) deletes the
# secret of every per-run MCP credential whose task is terminal or whose
# expires_at has passed. Held here:
#   * swarm-mcp-credentials-sweep, a Cloud Scheduler job, POSTs that route and
#     no other, with no tenant and no query, every 10 minutes, as the
#     rollup-sweeper account -- the identity swarm_api.auth.
#     ROLLUP_SWEEPER_ROUTES admits to it -- never as the platform tick, with
#     an OIDC token for swarm-api's own URL;
#   * the route is a literal in the job, not a variable a caller of the module
#     could point elsewhere;
#   * the job carries managed-by=swarm-terraform, the one marker a scheduler
#     job can carry, and the sweeper account's description names it;
#   * the module makes it only when enable_mcp_credentials_sweep is set, and
#     refuses the switch without an endpoint;
#   * the infra root exports the NAT addresses, sorted, as
#     mcp_egress_addresses, the same addresses nat_egress_addresses lists.
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

run "the_mcp_credentials_sweep_runs_every_10_minutes_as_the_sweeper" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_mcp_credentials_sweep = true
    api_endpoint                 = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].name == "swarm-mcp-credentials-sweep"
    error_message = "the job is swarm-mcp-credentials-sweep"
  }

  # Exactly its route: no tenant, no query, no other path.
  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/mcp-credentials/sweep"
    error_message = "the job calls swarm-api's MCP credential sweep and nothing else, with no tenant: the route sweeps every workspace"
  }

  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].http_target[0].http_method == "POST"
    error_message = "the MCP credential sweep is a POST"
  }

  # The path cannot be moved by a module input: the only variable the URI
  # reads is the endpoint.
  assert {
    condition = length(google_cloud_scheduler_job.mcp_credentials_sweep) == 1 && [
      for m in regexall("(?m)^\\s*uri\\s*=\\s*(.+)$", file("../../terraform/modules/scheduler/jobs.tf")) : m[0]
      if strcontains(m[0], "mcp-credentials")
    ] == ["\"$${trimsuffix(var.api_endpoint, \"/\")}/v1/admin/mcp-credentials/sweep\""]
    error_message = "the MCP credential sweep's URI is the endpoint and the literal /v1/admin/mcp-credentials/sweep, once, never a path variable"
  }

  # Every 10 minutes, off :00 and :30.
  assert {
    condition     = google_cloud_scheduler_job.mcp_credentials_sweep[0].schedule == "3,13,23,33,43,53 * * * *"
    error_message = "the MCP credential sweep runs every 10 minutes, at :03, :13, ...: a terminal task's credential lives at most 10 minutes past the task"
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
    error_message = "the sweep answers inside 300 s and is not retried: the next run is the retry"
  }

  assert {
    condition = (
      strcontains(google_service_account.rollup_sweeper.description, "mcp-credentials-sweep")
      && startswith(google_service_account.rollup_sweeper.description, "managed-by=swarm-terraform;")
      && length(google_service_account.rollup_sweeper.description) <= 256
    )
    error_message = "the sweeper account's description names every job that presents it, carries managed-by=swarm-terraform, and fits the provider's 256 characters"
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

run "the_mcp_credentials_sweep_is_off_in_the_module_by_default" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    api_endpoint = "https://swarm-api-abcdef-uc.a.run.app"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.mcp_credentials_sweep) == 0
    error_message = "the module makes the job only when enable_mcp_credentials_sweep is set: the feature ships dark"
  }
}

# The NAT addresses a caller allowlists for its MCP server. The mock leaves an
# address unknown at plan, so each is given one; two different ones, in
# reverse order, so the sort is observable.
run "the_infra_root_exports_the_nat_addresses_for_mcp_callers" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_resource {
    target          = module.network.google_compute_address.nat[0]
    override_during = plan
    values = {
      address = "203.0.113.20"
    }
  }

  override_resource {
    target          = module.network.google_compute_address.nat[1]
    override_during = plan
    values = {
      address = "203.0.113.10"
    }
  }

  variables {
    nat_static_ip_count = 2
  }

  assert {
    condition     = join(",", output.mcp_egress_addresses) == "203.0.113.10,203.0.113.20"
    error_message = "mcp_egress_addresses lists every NAT address, sorted, so the list a caller allowlists is stable between applies"
  }

  # The control: the same addresses as the existing output, in the NAT's order,
  # so the output above is the NAT's and not a list of its own.
  assert {
    condition     = join(",", output.nat_egress_addresses) == "203.0.113.20,203.0.113.10" && toset(output.mcp_egress_addresses) == toset(output.nat_egress_addresses)
    error_message = "mcp_egress_addresses must be the NAT's addresses, the ones nat_egress_addresses lists"
  }
}
