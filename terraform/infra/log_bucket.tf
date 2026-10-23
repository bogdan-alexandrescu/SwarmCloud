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
# THAT THE SINK WRITES WITH NO GRANT. Google says it does (below); the first
# entries in the bucket after the apply show it, and a refused write shows in
# `gcloud logging sinks describe swarm-worker-logs`.
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

# NO GRANT TO THE SINK'S WRITER (BGD's decision, 2026-10-22, #1960). The
# destination is a log bucket in the sink's own project, and Google authorizes
# such a sink itself. "Route logs to supported destinations", section "Set
# destination permissions"
# (https://cloud.google.com/logging/docs/export/configure_export_v2): "When the
# destination is a log bucket in the same project as the sink, the sink is
# automatically authorized", and "All sinks have a writer identity except for
# sinks that write to a log bucket in the same Google Cloud project in which
# the log entries originate". So there is no writer identity to grant to.
# terraform/bootstrap/workspace_deployer.tf's `workspace_apply` sink already
# writes to a same-project bucket with no grant anywhere.
#
# unique_writer_identity is left at the provider's default. It only chooses
# which identity a sink that HAS one writes as; neither Google nor the
# provider (it requires true only across projects or with bigquery_options)
# needs it for this destination, and there is no grant left for it to bound.
#
# log_view.tftest.hcl holds that nothing under terraform/infra or
# terraform/modules grants roles/logging.bucketWriter, so the release adds no
# IAM row for this path beyond L-1 (swarm-api's viewAccessor on the view).
#
# No labels: google_logging_project_sink has none
# (scripts/lib/unlabelable-types.json).
resource "google_logging_project_sink" "swarm_worker_logs" {
  count = local.swarm_worker_logs_on

  project     = var.project_id
  name        = local.swarm_worker_logs_bucket_id
  destination = "logging.googleapis.com/${local.swarm_worker_logs_bucket}"
  filter      = join(" OR ", [for b in local.swarm_workers_log_branches : "(${b})"])
  description = "managed-by=swarm-terraform; SwarmCloud's worker logs only, to the swarm-worker-logs bucket (terraform/infra/log_bucket.tf)."

  depends_on = [google_logging_project_bucket_config.swarm_worker_logs]
}
