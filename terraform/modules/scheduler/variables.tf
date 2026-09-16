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
  type    = string
  default = "/pubsub/wake"
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
  description = "HTTPS base URL of the swarm-quota-broker service. Empty disables its refresh tick."
  type        = string
  default     = ""
}

variable "quota_broker_path" {
  type    = string
  default = "/refresh"
}

variable "tick_service_account" {
  description = "Email of the OIDC identity Cloud Scheduler and Pub/Sub push present."
  type        = string
}

variable "publisher_members" {
  description = "Identities allowed to publish a wake message. The API publishes on every task submission."
  type        = list(string)
  default     = []
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
  type    = string
  default = "*/5 * * * *"
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
