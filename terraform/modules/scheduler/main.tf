# Scheduler wake path.
#
# The scheduler is not a daemon. It wakes on a Pub/Sub message, runs a bounded
# drain loop while admissible work exists, and exits -- which is why the Cloud
# Run service can sit at min-instances 0 and cost nothing while a backlog of
# ten thousand QUEUED tasks waits in Firestore.
#
# Two independent triggers, on purpose:
#   * Pub/Sub, published by the API the moment work is submitted, and by the
#     worker the moment it ends a task (`task_finished`, #636). Low latency.
#   * A one-minute Cloud Scheduler tick. Bounded staleness if a message is ever
#     lost, a subscription is misconfigured, or a push is rejected.
# Losing either one degrades latency. Losing both is what would stall the queue,
# and they share no failure mode.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  wake_topic_name = var.wake_topic_name == "" ? "${var.name_prefix}-scheduler-wake" : var.wake_topic_name
  dlq_topic_name  = "${local.wake_topic_name}-dlq"
}

resource "google_pubsub_topic" "wake" {
  # checkov:skip=CKV_GCP_83:A wake message carries no tenant data -- it is a "there is work" doorbell with a one-hour retention. CMEK is supported via `kms_key_name` for environments that require it; it is unset by default because this platform owns no key ring in a shared project.

  project = var.project_id
  name    = local.wake_topic_name

  kms_key_name = var.kms_key_name == "" ? null : var.kms_key_name

  # A wake message is worthless once it is stale -- the safety tick will
  # produce another within a minute -- so retention is short by design.
  message_retention_duration = "3600s"

  labels = var.labels
}

resource "google_pubsub_topic" "dead_letter" {
  # checkov:skip=CKV_GCP_83:Same payload as the wake topic, which carries no tenant data. CMEK is available through `kms_key_name`.

  project = var.project_id
  name    = local.dlq_topic_name

  kms_key_name = var.kms_key_name == "" ? null : var.kms_key_name

  message_retention_duration = "604800s"

  labels = merge(var.labels, { "swarm-purpose" = "dead-letter" })
}

resource "google_pubsub_subscription" "wake" {
  project = var.project_id
  name    = "${local.wake_topic_name}-sub"
  topic   = google_pubsub_topic.wake.id

  ack_deadline_seconds = var.ack_deadline_seconds

  # Never let the subscription be reaped for inactivity: an idle swarm is the
  # normal state, and losing the subscription would silently disable the fast
  # wake path while everything still looked healthy.
  expiration_policy {
    ttl = ""
  }

  message_retention_duration = "3600s"
  retain_acked_messages      = false

  # Ordering is deliberately OFF. Admission order is decided by the Firestore
  # query (priority DESC, created_at ASC), not by message arrival, and ordering
  # would serialise delivery for no benefit.
  enable_message_ordering = false

  push_config {
    push_endpoint = "${trimsuffix(var.scheduler_push_endpoint, "/")}${var.scheduler_push_path}"

    # Cloud Run ingress is internal-and-cloud-load-balancing, and the service
    # requires an invoker. Pub/Sub presents this OIDC identity, so there is no
    # unauthenticated entry point anywhere.
    oidc_token {
      service_account_email = var.tick_service_account
      audience              = coalesce(var.scheduler_push_audience, var.scheduler_push_endpoint)
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }
}

# The same `task_finished` wakes, to swarm-api (#748). For a finished task's
# gated integrator whose review said MERGE, swarm-api opens the pull request
# itself and ends the step without a worker (apps/swarm-api/swarm_api/
# verdictpublish.py); the scheduler, receiving the same message on the
# subscription above, holds that step PARKED meanwhile
# (CONTROL_PUBLISH_HOLD_SECONDS). Only `task_finished`: every other wake is
# the scheduler's alone.
#
# The rollup sweeper's identity, which swarm-api admits to this one push route
# beside its four ticks (auth.ROLLUP_SWEEPER_ROUTES) and which already holds
# run.invoker on swarm-api (infra main.tf, rollup_sweeper_invokes_api).
#
# No dead-letter topic and a short retry: a lost push costs only the hold, at
# the end of which the scheduler promotes the step to its worker as before.
resource "google_pubsub_subscription" "api_task_finished" {
  count = var.api_endpoint == "" ? 0 : 1

  project = var.project_id
  name    = "${local.wake_topic_name}-api-task-finished"
  topic   = google_pubsub_topic.wake.id

  filter = "attributes.reason = \"task_finished\""

  ack_deadline_seconds = var.ack_deadline_seconds

  expiration_policy {
    ttl = ""
  }

  message_retention_duration = "600s"
  retain_acked_messages      = false
  enable_message_ordering    = false

  push_config {
    push_endpoint = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/tasks/finished"

    oidc_token {
      service_account_email = local.rollup_sweeper_email
      audience              = coalesce(var.api_audience, trimsuffix(var.api_endpoint, "/"))
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }

  labels = var.labels
}

# A dead letter nobody can read is just a deletion with extra steps.
resource "google_pubsub_subscription" "dead_letter" {
  project = var.project_id
  name    = "${local.dlq_topic_name}-sub"
  topic   = google_pubsub_topic.dead_letter.id

  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }

  labels = var.labels
}

# --------------------------------------------------------------------------
# Pub/Sub IAM
# --------------------------------------------------------------------------

# Keyed by component, not by member: the member strings are service account
# emails that are unknown until apply, and a for_each with unknown KEYS cannot
# be planned at all.
resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = var.publisher_members

  project = var.project_id
  topic   = google_pubsub_topic.wake.name
  role    = "roles/pubsub.publisher"
  member  = each.value
}

# The task identities, for the `task_finished` wake (#636). The wake topic
# only: never the dead-letter topic, never a subscription.
resource "google_pubsub_topic_iam_member" "worker_publishers" {
  for_each = var.worker_publisher_members

  project = var.project_id
  topic   = google_pubsub_topic.wake.name
  role    = "roles/pubsub.publisher"
  member  = each.value
}

# Dead lettering is Pub/Sub itself republishing on your behalf, so its service
# agent needs both of these. Without them messages silently retry forever
# instead of dead-lettering.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.wake.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}
