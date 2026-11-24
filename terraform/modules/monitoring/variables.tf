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
  description = "The platform's own GKE Autopilot cluster. The spec-refusal metrics count a worker container's line only from this cluster, never from any other in the shared project -- the other team's peer-cluster among them (#346 box 51). Null means the name terraform/infra gives it, \"<name_prefix>-autopilot\"."
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

# ---------------------------------------------------------------------------
# The four operational monitors (alerts.tf, the end of the file). Every
# default below is explained next to its value from what the code emits.
# ---------------------------------------------------------------------------

variable "front_door_hostname" {
  description = <<-EOT
    The front door's hostname (terraform/infra's frontend_hostname, the name
    the managed certificate is issued for), bare: no scheme, no path. The
    uptime check reads https://<this>/healthz. Empty -- the default, and what
    terraform/infra passes while enable_frontend is off -- creates no uptime
    check and no front-door policy, as there is no host to check.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.front_door_hostname == "" || can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?[.])+[a-z]{2,}$", var.front_door_hostname))
    error_message = "front_door_hostname is a bare lower-case hostname (no scheme, port or path), or empty for no front door."
  }
}

variable "front_door_check_period" {
  description = <<-EOT
    How often each checker region requests /healthz. 60s, the shortest Cloud
    Monitoring offers: with front_door_down_seconds (300) the page arrives
    about six minutes after the front door stops answering. Allowed: 60s,
    300s, 600s, 900s.
  EOT
  type        = string
  default     = "60s"

  validation {
    condition     = contains(["60s", "300s", "600s", "900s"], var.front_door_check_period)
    error_message = "front_door_check_period is one of the periods Cloud Monitoring accepts: 60s, 300s, 600s, 900s."
  }
}

variable "front_door_check_timeout" {
  description = <<-EOT
    How long one request may take. 10s: IAP answers an unauthenticated
    request itself, with a redirect, without calling swarm-api, so a healthy
    answer costs one TLS handshake and one round trip; ten seconds of silence
    is the load balancer or the certificate, not a slow API.
  EOT
  type        = string
  default     = "10s"
}

variable "front_door_failing_regions" {
  description = <<-EOT
    The front-door policy fires when MORE than this many checker regions fail
    at once. 1, so two regions must agree: one region's own network trouble
    is not the front door going down.
  EOT
  type        = number
  default     = 1
}

variable "front_door_down_seconds" {
  description = <<-EOT
    How long the regions must keep failing before the policy fires. 300:
    five checks at the default 60s period. A managed certificate renewal or
    a URL-map update does not drop the front door for that long; a lost DNS
    record, an expired certificate or a deleted forwarding rule does.
  EOT
  type        = number
  default     = 300
}

variable "reconciler_pass_slow_seconds" {
  description = <<-EOT
    The reconciler's p99 pass duration, in seconds, above which a pass is
    slow. 60: the reconciler tick is */1 (scheduler module,
    reconciler_schedule), so a pass longer than 60s runs into the next tick,
    which then answers 409 already_running (reconciler/service.py, the pass
    lock) and is skipped. Measured over the 300 passes of 2026-09-24/25 (the
    same variable's description): p50 3.0s, p90 8.7s, max 111s -- so one
    slow pass happens and must not page on its own:
    reconciler_pass_slow_duration_seconds holds it for fifteen minutes.
    Keep it a bound of the metric's buckets (alerts.tf,
    reconciler_pass_duration), or the percentile interpolates across it.
  EOT
  type        = number
  default     = 60
}

variable "reconciler_pass_slow_duration_seconds" {
  description = <<-EOT
    How long the p99 must stay above reconciler_pass_slow_seconds. 900:
    fifteen ticks, three 300s windows in a row. The 111s outlier measured
    2026-09-24/25 fills one window and is gone; a reconciler that is slow
    on every pass -- a Firestore backlog, a backend listing that has grown --
    is still slow fifteen minutes later.
  EOT
  type        = number
  default     = 900
}

variable "nat_gateway_name" {
  description = "The platform's own Cloud NAT gateway. The NAT-drop policy reads only this gateway, in var.region, never another in the shared project. Null means the name terraform/modules/network gives it, \"<name_prefix>-nat\"."
  type        = string
  default     = null
}

variable "nat_drop_threshold" {
  description = <<-EOT
    Packets Cloud NAT dropped (router.googleapis.com/nat/
    dropped_sent_packets_count, summed per drop reason over
    nat_drop_window_seconds) above which a window counts. 0: with endpoint-
    independent mapping off (terraform/modules/network) every drop is
    OUT_OF_RESOURCES, a worker's provider connection that never opened, and
    swarm-nat logged 0 such drops over 3 days before 2026-10-09
    (docs/incidents/2026-10-09-egress-open-delay.md). The duration, not the
    count, is what tells a burst from exhaustion.
  EOT
  type        = number
  default     = 0
}

variable "nat_drop_window_seconds" {
  description = <<-EOT
    The window drops are summed over. 300: the NAT metric is sampled every
    60s, so a window holds five samples and one quiet minute between bursts
    does not reset the condition.
  EOT
  type        = number
  default     = 300
}

variable "nat_drop_duration_seconds" {
  description = <<-EOT
    How long every window must hold drops before the policy fires. 900: the
    one burst on record -- 20 DROPPED allocations within 27 ms, 2026-10-09,
    while dynamic port allocation doubled a VM's ports -- leaves one 300s
    window non-zero and never pages; ports exhausted for fifteen minutes do.
  EOT
  type        = number
  default     = 900
}

variable "browser_secret_grant_notify_seconds" {
  description = <<-EOT
    The browser-secret-grant policy notifies at most once per this many
    seconds. 300, as the workspace tripwires (workspace_alerts.tf): a log-
    match policy opens an incident per matching entry otherwise, and one
    bad actor's script re-granting in a loop must not flood the pager.
  EOT
  type        = number
  default     = 300
}
