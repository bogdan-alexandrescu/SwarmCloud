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

variable "execution_cancel_path" {
  description = "MUST match the route reconciler/service.py serves for a stop request (#627)."
  type        = string
  default     = "/stop-execution"
}

variable "execution_cancel_topic_name" {
  description = <<-EOT
    Name of the execution-cancel topic (#627). Passed in for the wake topic's
    reason: the root puts the same string in swarm-api's environment
    (EXECUTION_CANCEL_TOPIC) without a dependency on this module.
  EOT
  type        = string
  default     = ""
}

variable "execution_cancel_publisher_members" {
  description = "Identities allowed to publish a stop request, keyed by component name. Only swarm-api writes a cancel."
  type        = map(string)
  default     = {}
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

variable "api_endpoint" {
  description = "HTTPS base URL of the swarm-api service, which the workflow-rollup jobs call and the task_finished push subscription (#748) pushes to. The push subscription is gated by enable_task_finished_push, not by this value; the rollup jobs are made per rollup_tenant_ids."
  type        = string
  default     = ""

  validation {
    condition     = var.api_endpoint == "" || startswith(var.api_endpoint, "https://")
    error_message = "the API endpoint must be https; the rollup jobs carry an OIDC token."
  }
}

variable "enable_task_finished_push" {
  description = "Create the subscription that pushes `task_finished` wakes to swarm-api (#748). A bool, not derived from api_endpoint: the root passes api_endpoint from a Cloud Run resource attribute, which is unknown at plan time, and a count cannot depend on an unknown."
  type        = bool
  default     = false
}

variable "api_audience" {
  description = "OIDC audience the rollup jobs mint their token for. Empty means the endpoint itself, which is what a Cloud Run ID token names and what swarm-api's API_AUDIENCE is set to for its other direct callers (infra verify.tf)."
  type        = string
  default     = ""
}

variable "rollup_tenant_ids" {
  description = <<-EOT
    The registered tenants whose workflows are swept, whose issue runs
    are advanced and whose registered repositories are polled, one Cloud
    Scheduler job of each kind per tenant
    (POST /v1/admin/workflows/rollup?tenant_id=<t>,
    POST /v1/admin/runs/advance?tenant_id=<t> and
    POST /v1/admin/repositories/poll?tenant_id=<t>). The root passes the
    keys of var.tenants: a set the configuration knows at plan, so the
    for_each never depends on a value that exists only after apply.
  EOT
  type        = set(string)
  default     = []
}

variable "enable_forge_refresh" {
  description = <<-EOT
    Create swarm-forge-refresh, the 15-minute sweep that refreshes GitHub user
    access tokens (docs/onboarding.md §3.4 item 6, decision D2). Off until
    swarm-api serves the route (lane OB3): see jobs.tf.
  EOT
  type        = bool
  default     = false
}

variable "forge_refresh_path" {
  description = "swarm-api's refresh-sweep route, which the forge_refresh job POSTs."
  type        = string
  default     = "/v1/admin/forge/refresh"

  validation {
    condition     = startswith(var.forge_refresh_path, "/v1/admin/")
    error_message = "the refresh sweep is an admin route under /v1/admin/, which swarm-api admits the rollup-sweeper account to by name."
  }
}

variable "forge_refresh_schedule" {
  description = <<-EOT
    How often the GitHub user-token refresh sweep runs. Every 15 minutes: a
    user access token lives 8 hours and is refreshed with at least two left,
    so eight ticks fall inside that margin and a missed one costs nothing.
  EOT
  type        = string
  default     = "*/15 * * * *"
}

variable "enable_workspace_sweep" {
  description = "Create swarm-workspace-sweep, the 10-minute caller of swarm-api's personal-workspace dispatch sweep (docs/workspaces.md §2.2). A bool, not derived from api_endpoint, for enable_task_finished_push's reason. The root sets it to true in every environment: the sweep is also the stuck-workspace detector."
  type        = bool
  default     = false
}

variable "workspace_sweep_path" {
  description = "swarm-api's personal-workspace dispatch sweep (docs/workspaces.md §2.2), which the workspace_sweep job POSTs."
  type        = string
  default     = "/v1/admin/workspaces/sweep"

  validation {
    condition     = startswith(var.workspace_sweep_path, "/v1/admin/")
    error_message = "the workspace sweep is an admin route under /v1/admin/, which swarm-api admits the rollup-sweeper account to by name."
  }
}

variable "workspace_sweep_schedule" {
  description = <<-EOT
    How often the personal-workspace dispatch sweep runs. Every 10 minutes:
    the sweep calls a record stuck after 15 minutes with no dispatch attempt
    and 30 with one nobody claimed, and the workspace-stuck alert looks back
    30 minutes, so a 10-minute tick puts at least two sweeps inside each of
    those windows. Each tick reads the approved records only.
  EOT
  type        = string
  default     = "*/10 * * * *"
}

variable "workflow_rollup_schedule" {
  description = <<-EOT
    How often each tenant's stored workflow states are converged.

    Every fifteen minutes. Nothing a READER sees waits on this: every workflow
    read derives the state from its steps (docs/workflows.md). What waits is
    the queryable stored copy of a workflow nobody has opened, so the bound
    is on how stale "list my failed workflows" can be, and a quarter of an
    hour is well inside what anyone asking that question needs. Each sweep
    reads one page of a tenant's live workflows and their step tasks, so a
    tighter schedule multiplies Firestore reads by the tenant count for no
    reader-visible gain.
  EOT
  type        = string
  default     = "*/15 * * * *"
}

variable "issue_run_advance_schedule" {
  description = <<-EOT
    How often each tenant's issue runs (#454) are advanced without a reader.

    Every minute. This one IS reader-visible, unlike the workflow rollup: an
    `auto` run waits on it between its planner finishing and its workflow
    being submitted, and every run's GitHub status comment waits on it when
    nobody has the console open. A minute matches the platform's own safety
    tick and is short next to a planner or a workflow, which run for many
    minutes. A tick that finds nothing to move costs two Firestore queries
    per registered tenant and writes nothing; a PLANNED run waiting for a person
    is not even read (swarm_api.issueruns.IssueRuns.tickable).
  EOT
  type        = string
  default     = "* * * * *"
}

variable "issue_sweep_schedule" {
  description = <<-EOT
    How often each tenant's registered repositories are swept for open issues
    to start issue runs on (POST /v1/admin/issues/sweep, docs/issue-runs.md
    "Sweeper").

    Every 30 minutes, at :07 and :37 -- never on :00 or :30, where other
    schedules bunch. A planner runs for minutes and a run for hours, so a new
    candidate waiting up to half an hour costs nothing anyone sees; each sweep
    reads every registered repository's open issues and pull requests with
    the tenant's token, so a tighter schedule spends that token's rate limit
    for no gain. The sweep itself is off until SWEEP_ENABLED and the tenant's
    own switch are on.
  EOT
  type        = string
  default     = "7,37 * * * *"

  # The minute field must list explicit minutes, none of them :00 or :30: a
  # step like "*/30" or a wildcard would land on both.
  validation {
    condition = try(alltrue([
      for m in split(",", split(" ", trimspace(var.issue_sweep_schedule))[0]) :
      can(regex("^[0-9]{1,2}$", m)) && !contains([0, 30], tonumber(m)) && tonumber(m) < 60
    ]), false)
    error_message = "issue_sweep_schedule's minute field lists explicit minutes, none of them :00 or :30 (owner decision 2026-10-08), e.g. \"7,37 * * * *\"."
  }
}

variable "repo_index_poll_schedule" {
  description = <<-EOT
    How often each tenant's registered repositories are polled for a moved
    default branch and an elapsed index interval (docs/repo-index.md §3.3).

    Every five minutes, the design's figure: it bounds how old "the planner has
    today's index" can be after a merge, and an unchanged branch is read with
    the last ETag, which GitHub answers 304 without spending the token's rate
    limit, so forty repositories every five minutes cost almost nothing. A
    tighter schedule would not index sooner than a registration's
    `min_change_interval_minutes` (default 30) allows anyway.
  EOT
  type        = string
  default     = "*/5 * * * *"
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

variable "worker_publisher_members" {
  description = <<-EOT
    The identities tasks run as -- each tenant's worker account and its #295
    per-profile accounts -- allowed to publish a wake message, keyed by a name
    known at plan time ("worker:<tenant>", "action:<tenant>:<profile>").

    The worker publishes `task_finished` once it has ended its task, so the
    scheduler releases that task's dependants at once instead of on the next
    safety tick (#636; agent_worker/finishwake.py). A wake is a doorbell: it
    carries ids, the scheduler re-reads everything it acts on, and these
    accounts can already write task documents directly, which is more than a
    wake can do. Kept apart from `publisher_members` so the platform services
    and the task identities stay separately visible in a plan.
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
    then 300 s dispatch deadline it replaces (480 s since contract request
    37). At */1 it is about a minute and a half.

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
