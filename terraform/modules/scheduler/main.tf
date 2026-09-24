# Scheduler wake path.
#
# The scheduler is not a daemon. It wakes on a Pub/Sub message, runs a bounded
# drain loop while admissible work exists, and exits -- which is why the Cloud
# Run service can sit at min-instances 0 and cost nothing while a backlog of
# ten thousand QUEUED tasks waits in Firestore.
#
# Two independent triggers, on purpose:
#   * Pub/Sub, published by the API the moment work is submitted. Low latency.
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
