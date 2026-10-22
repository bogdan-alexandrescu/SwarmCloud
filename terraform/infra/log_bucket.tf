# ---------------------------------------------------------------------------
# swarm-worker-logs: the bucket the swarm-workers view reads, and the sink
# that fills it with SwarmCloud's worker logs and nothing else
# ---------------------------------------------------------------------------
#
# WHY A BUCKET OF ITS OWN (lane LOG-BUCKET, part of #1960). The swarm-workers
# view (log_view.tf) was first declared on _Default, and Cloud Logging refused
# it (release run 39742374084, 2026-10-22: "Filter exceeds the view filter
# nesting depth limit of 1"). A view filter may only AND `=`/`!=` checks on
# the source, the resource type and the log id, so no view on _Default can
# select by cluster or namespace -- and the other team's cluster writes
# k8s_container entries to _Default too.
#
# BGD'S DECISION, 2026-10-22 (#1960): "a dedicated log bucket fed by a sink,
# with the view on that bucket". A SINK filter accepts the full query
# language, so the selecting moves here, and the view on this bucket needs no
# selecting at all.
#
# WHAT IT ROUTES. Only where a worker runs (invariant 9 is narrowed further by
# swarm-api, per tenant, at read time; agent-autonomy.md §3.5):
#
#   * k8s_container in the swarm cluster -- the name main.tf gives
#     module.gke_autopilot -- in namespaces swarm-tenant-* (kubernetes/
#     render.py's prefix, the scheduler's `namespace_template`), anchored, so
#     `not-swarm-tenant-x` stays out. The cluster clause is there because the
#     other team's cluster writes in this project too and could hold a
#     namespace spelled like ours; the filter never names that cluster, even
#     to exclude it.
#   * cloud_run_job named swarm-job-* -- the per-tenant worker jobs
#     (locals.tf job_matrix, `<prefix>-job-<tenant>-<profile>`), anchored, so
#     swarm-verify, swarm-workspace-apply and every non-swarm job stay out.
#
# NOT routed: swarm-system (the egress proxy), the platform's Cloud Run
# services (swarm-api, -scheduler, ...) and every other job. They are not
# workers.
#
# IT IS A COPY. The sink adds a route; it does not take entries out of
# _Default, which keeps its own copy at its own retention. Worker logs are
# therefore stored twice: in _Default as before, and here for 30 days.
#
# ---------------------------------------------------------------------------
# WHAT IS NOT VERIFIED. Nothing here has been applied.
# ---------------------------------------------------------------------------
#
# THE WRITER'S GRANT. Google's routing guide says a sink writing to a log
# bucket in its own project needs no grant (terraform/bootstrap/
# workspace_deployer.tf's sink relies on that). The grant below is made
# anyway, at BGD's instruction, so the sink's identity is named and bounded
# rather than implicit. If Cloud Logging returns no writer identity for a
# same-project bucket, the grant's apply fails at dev-iam with an empty
# member; the sink and bucket still exist and still fill.
#
# THE CONDITION. `resource.name` for a log bucket is written with the project
# ID, as verify_logs.tf and workspace_deployer.tf write their view
# conditions. If IAM evaluates it with the project number instead, the grant
# never matches; with no grant needed for a same-project bucket, the sink
# still writes.
#
# THE FILTER. `=~` (RE2) and OR between parenthesised branches are the
# documented sink query language. The first entries in the bucket after the
# apply show it; `gcloud logging sinks describe swarm-worker-logs` shows any
# write error the sink records.

locals {
  swarm_worker_logs_on = var.swarm_workers_log_view_enabled ? 1 : 0

  # The project's region: workers run there, and a regional bucket keeps their
  # entries in it.
  swarm_worker_logs_location  = var.region
  swarm_worker_logs_bucket_id = "${var.name_prefix}-worker-logs"
  swarm_worker_logs_bucket    = "projects/${var.project_id}/locations/${local.swarm_worker_logs_location}/buckets/${local.swarm_worker_logs_bucket_id}"

  # 30 days: BGD's decision (#1960). Long enough for a failure-review or an
  # agent's logs_query (a 24 h window inside the agent's life, §3.5) to read a
  # week-old failure; anything older is in _Default at its own retention.
  swarm_worker_logs_retention_days = 30

  # THE BRANCHES. The sink filter is these, each parenthesised, joined by OR
  # -- and nothing else; log_view.tftest.hcl holds it to that, parses every
  # clause, and evaluates the result against entries that must and must not
  # be routed. A branch is a plain AND of `field="value"` / `field=~"regex"`
  # clauses with exactly one resource.type. Never write OR, NOT, `-` or `:`
  # inside one: a branch may only narrow.
  swarm_workers_log_branches = [
    "resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${var.name_prefix}-autopilot\" AND resource.labels.namespace_name=~\"^swarm-tenant-\"",
    "resource.type=\"cloud_run_job\" AND resource.labels.job_name=~\"^${var.name_prefix}-job-\"",
  ]
}

# No labels: google_logging_project_bucket_config has none in the pinned
# provider (scripts/lib/unlabelable-types.json). Its id carries the platform
# prefix and its description says who manages it.
resource "google_logging_project_bucket_config" "swarm_worker_logs" {
  count = local.swarm_worker_logs_on

  project        = var.project_id
  location       = local.swarm_worker_logs_location
  bucket_id      = local.swarm_worker_logs_bucket_id
  retention_days = local.swarm_worker_logs_retention_days
  description    = "managed-by=swarm-terraform; SwarmCloud's worker logs (swarm-tenant-* containers, swarm-job-* Cloud Run jobs), read by swarm-api through the swarm-workers view (terraform/infra/log_bucket.tf)."

  depends_on = [module.project_services]
}

# No labels: google_logging_project_sink has none
# (scripts/lib/unlabelable-types.json).
resource "google_logging_project_sink" "swarm_worker_logs" {
  count = local.swarm_worker_logs_on

  project     = var.project_id
  name        = local.swarm_worker_logs_bucket_id
  destination = "logging.googleapis.com/${local.swarm_worker_logs_bucket}"
  filter      = join(" OR ", [for b in local.swarm_workers_log_branches : "(${b})"])
  description = "managed-by=swarm-terraform; SwarmCloud's worker logs only, to the swarm-worker-logs bucket (terraform/infra/log_bucket.tf)."

  # Its own identity, never the project's shared logging account, so the
  # grant below names this sink and nothing else.
  unique_writer_identity = true

  depends_on = [google_logging_project_bucket_config.swarm_worker_logs]
}

# The sink's writer identity: roles/logging.bucketWriter on THIS bucket only.
# A log bucket has no IAM policy of its own, so the binding is on the project
# with the bucket's exact name as its condition. It is a google_project_iam_
# member, so scripts/lib/iam-plan.jq classifies it as an IAM change and a dev
# release holds it at `dev-iam` for BGD.
resource "google_project_iam_member" "swarm_worker_logs_writer" {
  count = local.swarm_worker_logs_on

  project = var.project_id
  role    = "roles/logging.bucketWriter"
  member  = google_logging_project_sink.swarm_worker_logs[0].writer_identity

  condition {
    title       = "swarm-worker-logs bucket only"
    description = "managed-by=swarm-terraform; the swarm-worker-logs sink writes this bucket and no other (terraform/infra/log_bucket.tf)."
    expression  = "resource.name == \"${local.swarm_worker_logs_bucket}\""
  }
}
