# The execution-cancel path (#627).
#
# A cancel used to be a flag: the worker saw it at its next control poll, or the
# reconciler stopped the execution once the worker went silent. Some executions
# ran 7-13 h past a cancel that way (four mock steps for 12.8 h on 2026-10-04,
# two browser steps for 7.6 h on 2026-09-24), holding leases and pools all the
# while. So on the FIRST cancel of a task whose attempt may be executing,
# swarm-api publishes that attempt's id here, and the reconciler stops the
# execution at once.
#
# WHY THROUGH THE RECONCILER AND NOT FROM swarm-api. swarm-api holds no
# compute permission at all -- no run.*, no container.*, no swarm-reaper
# RoleBinding (tests/terraform/iam.tftest.hcl asserts it) -- and it is the
# internet-facing service. The reconciler already holds exactly the grants a
# stop needs (swarmJobReaper, swarmGkeReaper, the namespaced swarm-reaper
# Role) and the backend clients that use them, with their checks: a Cloud Run
# name must be a platform Job's execution in this project and region, and a
# GKE Job must be in a swarm-tenant- namespace and carry the managed-by label.
# Granting swarm-api a second copy of those powers would widen the identity a
# browser talks to; a topic widens nothing.
#
# The message carries ids only. The reconciler re-reads the task and the
# attempt from Firestore and stops the execution the ATTEMPT recorded, so a
# publisher cannot name a resource -- least of all one of the other team's in
# this shared project.

locals {
  execution_cancel_topic_name = var.execution_cancel_topic_name == "" ? "${var.name_prefix}-execution-cancel" : var.execution_cancel_topic_name
}

resource "google_pubsub_topic" "execution_cancel" {
  # checkov:skip=CKV_GCP_83:A stop request carries a task id, an attempt id and a tenant id -- no tenant data, no credential. CMEK is supported via `kms_key_name`, unset by default as for the wake topic.

  project = var.project_id
  name    = local.execution_cancel_topic_name

  kms_key_name = var.kms_key_name == "" ? null : var.kms_key_name

  # A stop request older than an hour has been overtaken by the reconciler's
  # own pass, which stops the execution of any cancelled task whose worker went
  # silent. Retaining it longer would only replay a stale request.
  message_retention_duration = "3600s"

  labels = var.labels
}

resource "google_pubsub_subscription" "execution_cancel" {
  project = var.project_id
  name    = "${local.execution_cancel_topic_name}-sub"
  topic   = google_pubsub_topic.execution_cancel.id

  # The reconciler reads two documents and makes one backend call, each
  # bounded well inside this.
  ack_deadline_seconds = 60

  # Never reaped for inactivity: cancels are rare, and a subscription that
  # expired would silently put every cancel back on the 7-13 h path.
  expiration_policy {
    ttl = ""
  }

  message_retention_duration = "3600s"
  retain_acked_messages      = false

  push_config {
    push_endpoint = "${trimsuffix(var.reconciler_endpoint, "/")}${var.execution_cancel_path}"

    # The reconciler's only invoker is the tick identity (infra main.tf), and
    # its tick job already presents exactly this token to /reconcile.
    oidc_token {
      service_account_email = var.tick_service_account
      audience              = var.reconciler_endpoint
    }
  }

  # The reconciler answers 503 only when it could not read Firestore or the
  # backend call raised; anything it decided (stopped, already over, refused)
  # is acknowledged. Retried until the retention above, then dropped: the
  # reconciler's pass is the backstop, so no dead-letter topic is kept.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "120s"
  }

  labels = var.labels
}

# swarm-api alone publishes here: it is the only writer of a cancel.
resource "google_pubsub_topic_iam_member" "execution_cancel_publishers" {
  for_each = var.execution_cancel_publisher_members

  project = var.project_id
  topic   = google_pubsub_topic.execution_cancel.name
  role    = "roles/pubsub.publisher"
  member  = each.value
}
