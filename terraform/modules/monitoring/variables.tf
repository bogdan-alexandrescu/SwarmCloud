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
    Create the safety-tick-stopped alert. A kill switch; on by default.

    HISTORY, because the reason this flag exists has changed. The policy used to
    watch `cloudscheduler.googleapis.com/job/attempt_count`, and creating it
    failed with

        Error 404: Cannot find metric(s) that match type =
        "cloudscheduler.googleapis.com/job/attempt_count"

    so dev turned it off on 2026-09-19 "until the metric appears". It could not
    appear: Google publishes no Cloud Scheduler metric at all (checked
    2026-09-24 against the published metric list and the project's own
    descriptors -- zero, against 49 for run.googleapis.com). The policy now
    watches a logs-based metric this module creates from Cloud Scheduler's
    attempt logs (metrics.tf, safety_tick_attempts), which exists as soon as the
    apply that creates the alert has created it.
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
