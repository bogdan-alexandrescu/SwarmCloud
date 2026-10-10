# An approved personal workspace can never again wait silently (#847, the
# incident read 2026-10-10: w-752763 sat `approved` for ~19 hours with no
# dispatch and nothing saying so).
#
# Three pieces, each held here:
#   * swarm-workspace-sweep, a Cloud Scheduler job, POSTs swarm-api's dispatch
#     sweep every 10 minutes as the rollup-sweeper account -- the identity
#     swarm_api.auth.ROLLUP_SWEEPER_ROUTES admits to that route -- and is
#     ALWAYS on, because the sweep is also the detector;
#   * swarm-api's environment carries WORKSPACE_APPLY_PUBLISH (off by default,
#     var.workspace_apply_publish) and WORKSPACE_APPLY_TOPIC;
#   * a log-based metric counts swarm-api's `workspace_stuck` entries and an
#     alert pages on one within 30 minutes.

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

run "the_workspace_sweep_runs_every_ten_minutes_as_the_sweeper" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_workspace_sweep = true
    api_endpoint           = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = google_cloud_scheduler_job.workspace_sweep[0].name == "swarm-workspace-sweep"
    error_message = "the dispatch sweep's caller is one job named swarm-workspace-sweep"
  }

  assert {
    condition     = google_cloud_scheduler_job.workspace_sweep[0].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/workspaces/sweep"
    error_message = "the job must call the route swarm_api/routes/people.py serves, POST /v1/admin/workspaces/sweep, with no tenant_id"
  }

  assert {
    condition     = google_cloud_scheduler_job.workspace_sweep[0].http_target[0].http_method == "POST"
    error_message = "the sweep route is a POST"
  }

  assert {
    condition     = google_cloud_scheduler_job.workspace_sweep[0].schedule == "*/10 * * * *"
    error_message = "the sweep runs every 10 minutes, so two ticks fall inside the 15- and 30-minute stuck thresholds and the alert's 30-minute window"
  }

  # The identity swarm-api admits on this route (ROLLUP_SWEEPER_USERS ->
  # ROLLUP_SWEEPER_ROUTES), and the edge admits (rollup_sweeper_invokes_api).
  # Never swarm-tick: swarm-api admits it to no admin route, so the job would
  # be a 403 every ten minutes and the detector would detect nothing.
  assert {
    condition = (
      google_cloud_scheduler_job.workspace_sweep[0].http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_scheduler_job.workspace_sweep[0].http_target[0].oidc_token[0].service_account_email == output.rollup_sweeper_email
      && google_cloud_scheduler_job.workspace_sweep[0].http_target[0].oidc_token[0].service_account_email != var.tick_service_account
    )
    error_message = "the workspace sweep presents the rollup-sweeper account, the address swarm-api admits to POST /v1/admin/workspaces/sweep, never the platform tick"
  }

  assert {
    condition     = google_cloud_scheduler_job.workspace_sweep[0].http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    error_message = "the token is minted for swarm-api's own URL, as for every other job that calls swarm-api"
  }

  # Never paused: a detector that can be switched off with the thing it
  # detects is no detector. (The root's literal enable_workspace_sweep = true
  # is held by the infra run below.)
  assert {
    condition     = !google_cloud_scheduler_job.workspace_sweep[0].paused
    error_message = "the workspace sweep is never paused by default"
  }

  assert {
    condition     = startswith(google_cloud_scheduler_job.workspace_sweep[0].description, "managed-by=swarm-terraform;")
    error_message = "the job carries managed-by=swarm-terraform in its description, the label a scheduler job can carry"
  }

  assert {
    condition     = google_cloud_scheduler_job.workspace_sweep[0].attempt_deadline == "300s" && google_cloud_scheduler_job.workspace_sweep[0].retry_config[0].retry_count == 0
    error_message = "the sweep answers inside 300 s and is not retried: the next tick is the retry"
  }

  assert {
    condition     = strcontains(google_service_account.rollup_sweeper.description, "workspace-sweep")
    error_message = "the sweeper account's description names every job that presents it"
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-workspace-sweep")
    error_message = "scheduler_job_names must list the workspace sweep"
  }
}

# The switch on with no endpoint is refused at plan, never a job that calls
# nowhere.
run "the_workspace_sweep_refuses_an_empty_endpoint" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_workspace_sweep = true
  }

  expect_failures = [google_cloud_scheduler_job.workspace_sweep]
}

# With no tenants at all the job still exists: it is not one of the per-tenant
# jobs, and a personal workspace is no tenant of var.tenants.
run "the_workspace_sweep_needs_no_registered_tenant" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_workspace_sweep = true
    api_endpoint           = "https://swarm-api-abcdef-uc.a.run.app"
    rollup_tenant_ids      = []
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-workspace-sweep")
    error_message = "the workspace sweep is one job, made whether or not any tenant is registered"
  }
}

run "swarm_api_carries_the_publish_switch_off_and_the_topic" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_APPLY_PUBLISH == "false"
    error_message = "with workspace_apply_publish unset swarm-api's WORKSPACE_APPLY_PUBLISH is false: the topic exists only after the owner's bootstrap apply"
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_APPLY_TOPIC == "swarm-workspace-apply"
    error_message = "swarm-api publishes to swarm-workspace-apply, the topic terraform/bootstrap's workspace_deployer.tf creates"
  }

  assert {
    condition = alltrue([
      for svc, env in local.service_env :
      svc == "swarm-api" || (!contains(keys(env), "WORKSPACE_APPLY_PUBLISH") && !contains(keys(env), "WORKSPACE_APPLY_TOPIC"))
    ])
    error_message = "only swarm-api publishes a workspace id"
  }

  # The root wires the module's job in: removing the job fails here as well as
  # in the module runs above.
  assert {
    condition     = contains(output.scheduler_jobs, "swarm-workspace-sweep")
    error_message = "the infra root must create swarm-workspace-sweep: without it nothing calls the dispatch sweep and an approved workspace waits silently"
  }
}

run "workspace_apply_publish_turns_it_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    workspace_apply_publish = true
  }

  assert {
    condition     = local.service_env["swarm-api"].WORKSPACE_APPLY_PUBLISH == "true"
    error_message = "workspace_apply_publish = true renders WORKSPACE_APPLY_PUBLISH=true, the value swarm_api.settings reads as on"
  }
}

run "the_stuck_metric_counts_swarm_apis_workspace_stuck_line" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.event=\"workspace_stuck\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=(\"swarm-api\")",
        "resource.labels.location=\"us-central1\"",
      ] : strcontains(google_logging_metric.workspace_stuck.filter, f)
    ])
    error_message = "the metric must match swarm-api's workspace_stuck entries, from swarm-api alone in this region: the project is shared"
  }

  assert {
    condition     = google_logging_metric.workspace_stuck.label_extractors["reason"] == "EXTRACT(jsonPayload.reason)"
    error_message = "reason is a top-level field of the entry"
  }

  # workspace_id is opaque, but a per-record label would still be a series per
  # person; it is read from the entry, not grouped by.
  assert {
    condition     = !contains(keys(google_logging_metric.workspace_stuck.label_extractors), "workspace_id")
    error_message = "the metric is labelled by reason only"
  }

  assert {
    condition     = google_logging_metric.workspace_stuck.metric_descriptor[0].metric_kind == "DELTA" && google_logging_metric.workspace_stuck.metric_descriptor[0].value_type == "INT64"
    error_message = "a count of stuck entries is a DELTA INT64 series"
  }

  assert {
    condition     = contains(output.log_metric_names, google_logging_metric.workspace_stuck.name) && output.log_metric_filters[google_logging_metric.workspace_stuck.name] == google_logging_metric.workspace_stuck.filter
    error_message = "the metric must be listed with the module's other log metrics"
  }
}

run "one_stuck_entry_in_thirty_minutes_pages" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      strcontains(google_monitoring_alert_policy.workspace_stuck[0].conditions[0].condition_threshold[0].filter, "metric.type = \"logging.googleapis.com/user/${google_logging_metric.workspace_stuck.name}\"")
      && strcontains(google_monitoring_alert_policy.workspace_stuck[0].conditions[0].condition_threshold[0].filter, "resource.type = \"cloud_run_revision\"")
    )
    error_message = "the policy watches the workspace_stuck metric, with the resource.type restriction Monitoring requires"
  }

  assert {
    condition = (
      google_monitoring_alert_policy.workspace_stuck[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.workspace_stuck[0].conditions[0].condition_threshold[0].threshold_value == 0
      && google_monitoring_alert_policy.workspace_stuck[0].conditions[0].condition_threshold[0].aggregations[0].alignment_period == "1800s"
      && google_monitoring_alert_policy.workspace_stuck[0].conditions[0].condition_threshold[0].aggregations[0].per_series_aligner == "ALIGN_DELTA"
    )
    error_message = "one workspace_stuck entry within 30 minutes is enough to fire"
  }

  assert {
    condition = (
      toset(google_monitoring_alert_policy.workspace_stuck[0].notification_channels) == toset(google_monitoring_alert_policy.worker_action_ended[0].notification_channels)
      && contains(google_monitoring_alert_policy.workspace_stuck[0].notification_channels, "projects/saga-agents-staging/notificationChannels/1234567890")
    )
    error_message = "the policy notifies the channels every other alert in the module uses"
  }

  assert {
    condition     = google_monitoring_alert_policy.workspace_stuck[0].user_labels["managed-by"] == "swarm-terraform"
    error_message = "the policy must carry the marker make destroy keys on"
  }

  assert {
    condition     = contains(output.alert_policy_names, "swarm-dev-workspace-stuck")
    error_message = "the policy must be listed with the module's other alert policies"
  }
}
