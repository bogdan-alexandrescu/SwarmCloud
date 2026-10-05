# The wake path. Two independent triggers that share no failure mode: Pub/Sub
# for latency, a one-minute Cloud Scheduler tick for bounded staleness.

mock_provider "google" {}

variables {
  project_id              = "saga-agents-staging"
  wake_topic_name         = "swarm-scheduler-wake"
  scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
  reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
  quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
  tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
  labels                  = { "managed-by" = "swarm-terraform" }

  publisher_members = {
    api        = "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    reconciler = "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
  }
}

run "the_fast_path_is_authenticated_push_with_a_dead_letter" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  assert {
    condition     = google_pubsub_topic.wake.name == "swarm-scheduler-wake"
    error_message = "the wake topic name is derived in the root so the API can carry it without a module dependency"
  }

  assert {
    condition     = google_pubsub_subscription.wake.push_config[0].push_endpoint == "https://swarm-scheduler-abcdef-uc.a.run.app/pubsub/push"
    error_message = "the subscription must push to /pubsub/push -- the route scheduler/main.py actually serves. \"/pubsub/wake\" 404d every delivery and no task was ever admitted."
  }

  # Cloud Run ingress is internal-and-cloud-load-balancing and the service
  # requires an invoker, so Pub/Sub has to present an OIDC identity. There is no
  # unauthenticated entry point anywhere in this platform.
  assert {
    condition     = google_pubsub_subscription.wake.push_config[0].oidc_token[0].service_account_email == var.tick_service_account
    error_message = "push must carry an OIDC token; the scheduler has no unauthenticated entry point"
  }

  assert {
    condition     = google_pubsub_subscription.wake.expiration_policy[0].ttl == ""
    error_message = "an idle swarm is the normal state; an expiring subscription would silently disable the fast wake path"
  }

  assert {
    condition     = google_pubsub_subscription.wake.dead_letter_policy[0].max_delivery_attempts >= 5
    error_message = "messages the scheduler cannot accept must land somewhere a human can read them"
  }

  assert {
    condition     = google_pubsub_subscription.dead_letter.name == "swarm-scheduler-wake-dlq-sub"
    error_message = "a dead letter nobody can read is just a deletion with extra steps"
  }

  assert {
    condition = alltrue([
      for k, m in google_pubsub_topic_iam_member.publishers :
      m.role == "roles/pubsub.publisher" && m.member != "allUsers" && m.member != "allAuthenticatedUsers"
    ])
    error_message = "only named service accounts may publish a wake message"
  }
}

run "the_safety_tick_really_is_every_minute" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  # Pub/Sub is the fast path; this is what bounds staleness when Pub/Sub fails.
  # Losing either degrades latency. Losing both stalls the queue.
  assert {
    condition     = google_cloud_scheduler_job.safety_tick.schedule == "* * * * *"
    error_message = "the safety tick must run every minute"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.safety_tick.pubsub_target) == 1 && length(google_cloud_scheduler_job.safety_tick.http_target) == 0
    error_message = "the tick publishes onto the same wake topic the API uses, so there is one wake code path, not two"
  }

  assert {
    condition     = strcontains(base64decode(google_cloud_scheduler_job.safety_tick.pubsub_target[0].data), "safety-tick")
    error_message = "the tick's payload must say where it came from, so a wake can be attributed in the logs"
  }

  assert {
    condition     = google_cloud_scheduler_job.safety_tick.attempt_deadline == "60s"
    error_message = "a tick that has not started within its own interval should be superseded, not queued"
  }

  assert {
    condition     = google_cloud_scheduler_job.safety_tick.name == "swarm-scheduler-tick"
    error_message = "the tick job carries the swarm prefix"
  }

  assert {
    condition     = google_cloud_scheduler_job.reconciler.http_target[0].oidc_token[0].service_account_email == var.tick_service_account
    error_message = "the reconciler tick authenticates as the tick identity"
  }

  assert {
    condition     = google_cloud_scheduler_job.reconciler.http_target[0].uri == "https://swarm-reconciler-abcdef-uc.a.run.app/reconcile"
    error_message = "the reconciler tick must call the reconcile endpoint"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.quota_refresh) == 1
    error_message = "the quota refresh tick is created when enabled"
  }
}

run "the_quota_tick_is_gated_by_a_flag_not_by_an_unknown_url" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    enable_quota_refresh = false
  }

  # The endpoint is a Cloud Run URI, which does not exist until apply. A `count`
  # derived from it cannot be planned at all -- terraform refuses rather than
  # guessing -- so the switch has to be a value the configuration already knows.
  assert {
    condition     = length(google_cloud_scheduler_job.quota_refresh) == 0
    error_message = "disabling the quota tick must not require an empty endpoint string"
  }
}

run "a_plaintext_push_endpoint_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    scheduler_push_endpoint = "http://swarm-scheduler-abcdef-uc.a.run.app"
  }

  expect_failures = [var.scheduler_push_endpoint]
}

# D17. POST /v1/admin/workflows/rollup converges the STORED Workflow.state of
# workflows nobody reads (docs/workflows.md, "Workflow state"). It had no
# periodic caller, so a workflow nobody listed kept a stale stored state for
# ever. One job per registered tenant, because the route takes exactly one
# tenant_id and refuses to guess it from the caller.
run "the_workflow_rollup_runs_per_tenant_as_its_own_identity" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    rollup_tenant_ids = ["eng", "research"]
    api_endpoint      = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = toset(keys(google_cloud_scheduler_job.workflow_rollup)) == toset(["eng", "research"])
    error_message = "every registered tenant gets exactly one rollup job, keyed by its tenant id"
  }

  assert {
    condition     = google_cloud_scheduler_job.workflow_rollup["eng"].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/workflows/rollup?tenant_id=eng"
    error_message = "the job must call the route swarm_api/routes/admin.py serves, with the tenant as the query parameter it requires"
  }

  assert {
    condition     = google_cloud_scheduler_job.workflow_rollup["research"].http_target[0].http_method == "POST"
    error_message = "the rollup route is a POST"
  }

  # Its own identity, never the tick: the tick reaches the scheduler and the
  # reconciler, and the API's narrow capability (auth.ROLLUP_SWEEPER_ROUTES) is
  # granted to this one address alone.
  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.workflow_rollup :
      job.http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
      && job.http_target[0].oidc_token[0].service_account_email != var.tick_service_account
    ])
    error_message = "the rollup jobs must present the dedicated rollup-sweeper identity, not the tick"
  }

  assert {
    condition     = output.rollup_sweeper_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the root hands this address to swarm-api as ROLLUP_SWEEPER_USERS; it must be the account the jobs present"
  }

  assert {
    condition     = google_service_account.rollup_sweeper.account_id == "swarm-rollup-sweeper"
    error_message = "the account id is spelled in modules/service_account_ids so bootstrap grants the deployer on it"
  }

  # The audience defaults to the endpoint, which is what a Cloud Run ID token
  # names and what verify.tf sets swarm-api's API_AUDIENCE to for its caller.
  assert {
    condition     = google_cloud_scheduler_job.workflow_rollup["eng"].http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    error_message = "the OIDC audience is the API's own URL unless one is set"
  }

  # A Cloud Scheduler job has no labels; the destroy guard reads the marker from
  # its description, as it does for the three ticks above.
  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.workflow_rollup :
      startswith(job.description, "managed-by=swarm-terraform;")
    ])
    error_message = "every rollup job carries managed-by=swarm-terraform in its description"
  }

  assert {
    condition     = startswith(google_service_account.rollup_sweeper.description, "managed-by=swarm-terraform;")
    error_message = "the sweeper account carries managed-by=swarm-terraform in its description"
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-workflow-rollup-eng") && contains(output.scheduler_job_names, "swarm-workflow-rollup-research")
    error_message = "scheduler_job_names must list every job this module makes"
  }
}

# #454, owner decision "Advancing runs: swarm-api, on a Cloud Scheduler tick".
# A run moved only when somebody read it, so a run nobody watched -- and every
# `plan_approval: auto` run -- never moved. One job per registered tenant, as
# the rollup sweeper, which swarm-api admits to POST /v1/admin/runs/advance by
# name (swarm_api.auth.ROLLUP_SWEEPER_ROUTES).
run "the_issue_run_tick_runs_per_tenant_every_minute_as_the_sweeper" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    rollup_tenant_ids = ["eng", "research"]
    api_endpoint      = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = toset(keys(google_cloud_scheduler_job.issue_run_advance)) == toset(["eng", "research"])
    error_message = "every registered tenant gets exactly one issue-run tick, keyed by its tenant id"
  }

  assert {
    condition     = google_cloud_scheduler_job.issue_run_advance["eng"].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/runs/advance?tenant_id=eng"
    error_message = "the tick must call the route swarm_api/routes/admin.py serves, with the tenant as the query parameter it requires"
  }

  assert {
    condition     = google_cloud_scheduler_job.issue_run_advance["research"].http_target[0].http_method == "POST"
    error_message = "the advance route is a POST"
  }

  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.issue_run_advance :
      job.http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
      && job.http_target[0].oidc_token[0].service_account_email != var.tick_service_account
      && job.http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    ])
    error_message = "the issue-run tick presents the rollup-sweeper identity, for the API's own URL, never the platform tick"
  }

  # An auto run waits on this between its planner and its workflow.
  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.issue_run_advance : job.schedule == "* * * * *"
    ]) && output.issue_run_advance_schedule == "* * * * *"
    error_message = "issue runs advance every minute by default"
  }

  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.issue_run_advance :
      startswith(job.description, "managed-by=swarm-terraform;")
    ])
    error_message = "every issue-run tick carries managed-by=swarm-terraform in its description"
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-issue-run-advance-eng") && contains(output.scheduler_job_names, "swarm-issue-run-advance-research")
    error_message = "scheduler_job_names must list every issue-run tick"
  }
}

# docs/repo-index.md §3.3 (lane RI4): every five minutes, per registered tenant,
# POST /v1/admin/repositories/poll reads each registration's default-branch
# head with the last ETag and queues an index run where it moved or the
# interval passed. As the rollup sweeper, which swarm-api admits to that route
# by name (swarm_api.auth.ROLLUP_SWEEPER_ROUTES) and to nothing wider; its one
# grant, run.invoker on swarm-api, is already the rollup's (terraform/infra
# main.tf rollup_sweeper_invokes_api, held by infra_guards.tftest.hcl), so the
# job adds no IAM member.
run "the_repo_index_poll_runs_per_tenant_every_five_minutes_as_the_sweeper" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    rollup_tenant_ids = ["eng", "research"]
    api_endpoint      = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = toset(keys(google_cloud_scheduler_job.repo_index_poll)) == toset(["eng", "research"])
    error_message = "every registered tenant gets exactly one repo_index_poll job, keyed by its tenant id"
  }

  assert {
    condition     = google_cloud_scheduler_job.repo_index_poll["eng"].name == "swarm-repo-index-poll-eng"
    error_message = "the poll job is named for its tenant"
  }

  assert {
    condition     = google_cloud_scheduler_job.repo_index_poll["eng"].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/repositories/poll?tenant_id=eng"
    error_message = "the job must call the route swarm_api/routes/admin.py serves, with the tenant as the query parameter it requires"
  }

  assert {
    condition     = google_cloud_scheduler_job.repo_index_poll["research"].http_target[0].http_method == "POST"
    error_message = "the poll route is a POST"
  }

  # The OIDC grant: the rollup-sweeper identity, minted for the API's own URL.
  # swarm-api's poll route refuses every other caller, admins included.
  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.repo_index_poll :
      job.http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
      && job.http_target[0].oidc_token[0].service_account_email == output.rollup_sweeper_email
      && job.http_target[0].oidc_token[0].service_account_email != var.tick_service_account
      && job.http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    ])
    error_message = "the poll presents the rollup-sweeper identity, for the API's own URL, never the platform tick"
  }

  # §3.3: "every 5 minutes". An unchanged branch answers 304, which costs no
  # rate limit, so forty repositories every five minutes cost almost nothing.
  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.repo_index_poll : job.schedule == "*/5 * * * *"
    ]) && output.repo_index_poll_schedule == "*/5 * * * *"
    error_message = "repositories are polled every five minutes by default"
  }

  # The route stops starting reads at 240 s (repoindex.POLL_BUDGET_SECONDS);
  # the deadline must leave it room to answer.
  assert {
    condition     = google_cloud_scheduler_job.repo_index_poll["eng"].attempt_deadline == "300s"
    error_message = "the poll's attempt deadline is 300s, above the route's 240s read budget"
  }

  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.repo_index_poll : job.retry_config[0].retry_count == 0
    ])
    error_message = "the next tick is the poll's retry"
  }

  # A Cloud Scheduler job has no labels; the destroy guard reads the marker
  # from its description.
  assert {
    condition = alltrue([
      for t, job in google_cloud_scheduler_job.repo_index_poll :
      startswith(job.description, "managed-by=swarm-terraform;")
    ])
    error_message = "every repo_index_poll job carries managed-by=swarm-terraform in its description"
  }

  assert {
    condition     = strcontains(google_service_account.rollup_sweeper.description, "POST /v1/admin/repositories/poll")
    error_message = "the sweeper account's description names every route it is admitted to"
  }

  assert {
    condition     = contains(output.scheduler_job_names, "swarm-repo-index-poll-eng") && contains(output.scheduler_job_names, "swarm-repo-index-poll-research")
    error_message = "scheduler_job_names must list every repo_index_poll job"
  }
}

run "no_registered_tenant_means_no_rollup_job" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.workflow_rollup) == 0
    error_message = "a rollup job for a tenant nobody registered sweeps nothing"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.issue_run_advance) == 0
    error_message = "an issue-run tick for a tenant nobody registered advances nothing"
  }

  assert {
    condition     = length(google_cloud_scheduler_job.repo_index_poll) == 0
    error_message = "a repo_index_poll job for a tenant nobody registered polls nothing"
  }
}

run "a_rollup_job_without_an_https_api_endpoint_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    rollup_tenant_ids = ["eng"]
    api_endpoint      = "http://swarm-api-abcdef-uc.a.run.app"
  }

  expect_failures = [var.api_endpoint]
}
