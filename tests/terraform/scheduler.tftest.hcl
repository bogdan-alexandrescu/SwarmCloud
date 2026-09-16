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
