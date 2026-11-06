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

variable "gke_cluster_name" {
  description = "The platform's own GKE Autopilot cluster. The spec-refusal metrics count a worker container's line only from this cluster, never from any other in the shared project -- the other team's agents-staging among them (#346 box 51). Null means the name terraform/infra gives it, \"<name_prefix>-autopilot\"."
  type        = string
  default     = null
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
  description = "Pre-existing channel ids (an Email channel made in the console) to attach to every policy, whatever its severity."
  type        = list(string)
  default     = []
}

variable "urgent_notification_channels" {
  description = <<-EOT
    Pre-existing channel ids (Slack, made in the console) attached to the
    CRITICAL and ERROR policies only, on top of the channels every policy gets.
    WARNING policies never reach them: the owner's severity split of
    2026-10-21 (#1817, option (b)), so the seven WARNINGs cannot bury an ERROR
    like the 10-20 GKE dispatch outage. Ids only
    (projects/<p>/notificationChannels/<n>): the repository is public, and a
    Slack channel made in Terraform would put its auth token in state.
  EOT
  type        = list(string)
  default     = []
}

variable "require_alert_channels" {
  description = <<-EOT
    Fail the plan when alerts are created and no policy has a channel at all
    (alert_emails, extra_notification_channels and urgent_notification_channels
    all empty). False by default only because the channel ids do not exist yet:
    the operator sets it true in the same change that commits them, after
    which an emptied list is a red plan rather than #1817 again (21 policies
    that reached nobody).
  EOT
  type        = bool
  default     = false
}

variable "fencing_alert_excluded_tenants" {
  description = <<-EOT
    Tenants whose generation_fenced events the generation-fencing policy does
    not count: the verification and acceptance tenants, whose own runs fence
    at startup by design (#1817: 215 of 270 fences in 7 days were
    verify-remote's). `smoke` is release acceptance's tenant; swarm-verify's
    personal tenant is the other, an id derived from its project
    (swarm_common.identity), so it differs per install.
    The metric still counts them; only the alert ignores them.

    EMPTY BY DEFAULT: the default used to name the live deployment's own
    verify tenant (DAYONE-TF-1, plan R9). terraform/infra always passes the
    value, derived from its declared tenants; [] here only means a caller of
    this module alone excludes nobody.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for t in var.fencing_alert_excluded_tenants : can(regex("^[a-z0-9-]+$", t))])
    error_message = "fencing_alert_excluded_tenants are tenant keys, [a-z0-9-] only: each is written into a Monitoring filter unescaped."
  }
}

variable "fencing_alert_excluded_submitters" {
  description = <<-EOT
    Service accounts, by short name, whose generation_fenced events the
    generation-fencing policy does not count. The worker writes the
    submitter's short name on the fence's line (`swarm-verify` for
    swarm-verify@<project>.iam.gserviceaccount.com; `person` for anyone
    else, never an address) and the metric extracts it as submitter_service.
    `swarm-verify` because verify-remote's race and concurrency tests fence
    on purpose but submit as eng: over the 7 days measured 2026-10-22 eng had
    347 fences and smoke 57, so no tenant filter could drop them without
    hiding eng's real ones (FENCE-SUBMITTER, part of #1822).
    The metric still counts them; only the alert ignores them.
  EOT
  type        = list(string)
  default     = ["swarm-verify"]

  validation {
    condition     = alltrue([for s in var.fencing_alert_excluded_submitters : can(regex("^[a-z0-9-]+$", s))])
    error_message = "fencing_alert_excluded_submitters are service-account short names, [a-z0-9-] only: each is written into a Monitoring filter unescaped."
  }
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

variable "tenant_ids" {
  description = "The keys of terraform/infra's var.tenants. workspace_alerts.tf pages when swarm-workspace-deployer writes IAM on any of their -git or -git-refresh secrets (docs/design/workspace-kinds.md §4.5). Empty by default so the module's other callers need not pass it; empty creates no such alert, as there is no slot to name."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for t in var.tenant_ids : can(regex("^[a-z0-9-]+$", t))])
    error_message = "tenant_ids are tenant keys, [a-z0-9-] only: each is written into a Logging regex unescaped."
  }
}

variable "labels" {
  type = map(string)
}
