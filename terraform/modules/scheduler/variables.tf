variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name_prefix" {
  type    = string
  default = "swarm"
}

variable "wake_topic_name" {
  description = <<-EOT
    Name of the wake topic. Passed in rather than derived so the composing root
    can put the same string in the API's environment without taking a dependency
    on this module -- the API publishes to the topic, this module subscribes the
    service the API would otherwise have to wait for.
  EOT
  type        = string
  default     = ""
}

variable "scheduler_push_endpoint" {
  description = "HTTPS endpoint of the swarm-scheduler Cloud Run service that Pub/Sub pushes wake messages to."
  type        = string

  validation {
    condition     = startswith(var.scheduler_push_endpoint, "https://")
    error_message = "the push endpoint must be https."
  }
}

variable "scheduler_push_path" {
  description = <<-EOT
    MUST match a route scheduler/main.py actually serves. It was "/pubsub/wake"
    while the application served "/pubsub/push", so every Pub/Sub delivery and
    every Cloud Scheduler tick returned 404. Nothing alerted: a push
    subscription treats 404 as a delivery failure and retries quietly, so the
    scheduler was simply never woken and every submitted task sat in READY
    forever while the control plane looked healthy.
  EOT
  type        = string
  default     = "/pubsub/push"
}

variable "reconciler_endpoint" {
  description = "HTTPS base URL of the swarm-reconciler service."
  type        = string
}

variable "reconciler_path" {
  type    = string
  default = "/reconcile"
}

variable "quota_broker_endpoint" {
  description = "HTTPS base URL of the swarm-quota-broker service."
  type        = string
  default     = ""
}

variable "enable_quota_refresh" {
  description = <<-EOT
    Create the quota-broker refresh tick.

    A boolean rather than an is-the-endpoint-empty test: the endpoint is a Cloud
    Run URI that is unknown until apply, and terraform cannot plan a `count`
    derived from a value it does not yet have.
  EOT
  type        = bool
  default     = true
}

variable "quota_broker_path" {
  description = <<-EOT
    MUST match a route quota_broker/main.py actually serves. It was "/refresh",
    which the application has never served -- its sweep endpoint is
    POST /v1/quota/sweep -- so the periodic quota refresh 404'd on every run.
    The visible symptom is nothing at all: provider state simply goes stale, and
    an EXHAUSTED provider is never observed to have recovered.
  EOT
  type        = string
  default     = "/v1/quota/sweep"
}

variable "tick_service_account" {
  description = "Email of the OIDC identity Cloud Scheduler and Pub/Sub push present."
  type        = string
}

variable "publisher_members" {
  description = <<-EOT
    Identities allowed to publish a wake message, keyed by component name. The
    API publishes on every task submission.

    A map keyed by component rather than a list of members: the members are
    service account emails that are unknown until apply, and terraform cannot
    plan a for_each whose keys it cannot compute.
  EOT
  type        = map(string)
  default     = {}
}

variable "safety_tick_schedule" {
  description = <<-EOT
    The one-minute safety tick. Pub/Sub is the fast path; this exists so a
    dropped or unacked wake message delays admission by at most a minute instead
    of stalling the queue until someone notices.
  EOT
  type        = string
  default     = "* * * * *"
}

variable "reconciler_schedule" {
  description = <<-EOT
    How often a reconciliation pass runs. Every minute, written */1 so that
    tests/unit/worker/test_recovery_after_a_dead_worker.py can still read it.

    It was */5. A pass is what notices a dead attempt, so the tick is added
    to every recovery bound: a silent lease was repaired within 390 s (90 s
    grace + a 300 s tick) and is now repaired within 150 s. The rule #198
    added, which requeues a task whose execution ended before its runner
    started, waits 30 s past the execution's end and then for the next
    pass: at */5 that was up to five and a half minutes, no sooner than the
    300 s dispatch deadline it replaces. At */1 it is about a minute and a
    half.

    What a pass costs, measured over the 300 passes of 2026-09-24/25: p50
    3.0 s, p90 8.7 s, max 111 s. A pass that runs past the next tick makes
    that tick's request answer 409 (the service's one-pass lock), which
    Cloud Scheduler retries and which does nothing. Where the service may
    run two instances (the default max of 2; dev runs 1), two passes can
    overlap. Every repair step is a compare-and-set transaction and the
    lease release is the frozen idempotent one, so the second pass's
    fence, release and requeue each find the work done and write nothing.
  EOT
  type        = string
  default     = "*/1 * * * *"
}

variable "quota_refresh_schedule" {
  type    = string
  default = "*/5 * * * *"
}

variable "ack_deadline_seconds" {
  description = "The scheduler drains and exits; it does not hold the message for the whole drain."
  type        = number
  default     = 60

  validation {
    condition     = var.ack_deadline_seconds >= 10 && var.ack_deadline_seconds <= 600
    error_message = "ack_deadline_seconds must be between 10 and 600."
  }
}

variable "max_delivery_attempts" {
  type    = number
  default = 5

  validation {
    condition     = var.max_delivery_attempts >= 5 && var.max_delivery_attempts <= 100
    error_message = "Pub/Sub requires max_delivery_attempts between 5 and 100."
  }
}

variable "kms_key_name" {
  description = <<-EOT
    Optional CMEK for the wake and dead-letter topics. Empty uses Google-managed
    keys. A wake message is a doorbell -- `{"source": "..."}` -- and carries no
    tenant data, so the default is deliberately not a customer key this platform
    would then have to own, rotate and pay for in a shared project.
  EOT
  type        = string
  default     = ""
}

variable "time_zone" {
  type    = string
  default = "Etc/UTC"
}

variable "paused" {
  description = "Create the Cloud Scheduler jobs paused. Useful for a first apply before images exist."
  type        = bool
  default     = false
}

variable "labels" {
  type = map(string)
}

# The OIDC audience each target accepts. Defaults to the target's own URL, which
# is what a Cloud Run service accepts with no extra configuration. It is set
# explicitly when the receiving service must ALSO be told its own audience, so
# that it can check the `aud` claim itself: the URL is unknowable to the service
# it belongs to, a constant is not. See `push_audiences` in infra/locals.tf.
variable "scheduler_push_audience" {
  type    = string
  default = ""
}

variable "quota_broker_audience" {
  type    = string
  default = ""
}
