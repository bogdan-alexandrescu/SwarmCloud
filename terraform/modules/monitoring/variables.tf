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

variable "environment" {
  type = string
}

variable "service_names" {
  description = "Cloud Run services to chart and alert on."
  type        = list(string)
}

variable "wake_subscription" {
  type = string
}

variable "dead_letter_subscription" {
  type = string
}

variable "enable_safety_tick_alert" {
  description = <<-EOT
    Create the safety-tick-stopped alert.

    Separate from `create_alerts` because this one policy depends on a metric
    that must EXIST before it can be referenced, and terraform cannot express a
    dependency on runtime telemetry:

        Error 404: Cannot find metric(s) that match type =
        "cloudscheduler.googleapis.com/job/attempt_count"

    Verified on 2026-09-19 in saga-agents-staging: the scheduler tick job is
    ENABLED and attempting every minute, and the project still has ZERO
    cloudscheduler.googleapis.com metric descriptors -- checked against
    run.googleapis.com, which returns 49, so the query is sound and the absence
    is real.

    Until that changes, creating this policy fails the apply. Leaving it enabled
    means every `make deploy` exits non-zero, which teaches everyone to ignore
    the exit code -- a worse outcome than a missing alert, and exactly the habit
    the rest of this repository is trying to break.

    Check before flipping it back on:

        curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
          "https://monitoring.googleapis.com/v3/projects/<project>/metricDescriptors?filter=metric.type%3Dstarts_with%28%22cloudscheduler.googleapis.com%22%29"
  EOT
  type        = bool
  default     = true
}

variable "safety_tick_job" {
  description = "Cloud Scheduler job id of the one-minute tick. Its absence is the alert that matters most."
  type        = string
}

variable "alert_emails" {
  description = "Email addresses to create notification channels for."
  type        = list(string)
  default     = []
}

variable "extra_notification_channels" {
  description = "Pre-existing channel ids (PagerDuty, Slack) to attach to every policy."
  type        = list(string)
  default     = []
}

variable "error_rate_threshold" {
  description = "5xx responses per second, averaged over the alert window, before the API is considered unhealthy."
  type        = number
  default     = 0.5
}

variable "job_failure_threshold" {
  description = "Failed Cloud Run Job executions in the window before alerting."
  type        = number
  default     = 3
}

variable "undelivered_wake_threshold" {
  description = "Backlog on the wake subscription. Sustained backlog means the scheduler is not draining."
  type        = number
  default     = 50
}

variable "create_alerts" {
  description = "False leaves the dashboard and metrics but creates no policies -- useful before the apps emit anything."
  type        = bool
  default     = true
}

variable "labels" {
  type = map(string)
}
