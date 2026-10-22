# ---------------------------------------------------------------------------
# swarm-workers: SwarmCloud's own logs, readable by swarm-api -- and no other
# log in this SHARED project
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS (docs/design/agent-autonomy.md §3.5, lane AU-5, part of
# #1933). swarm-api serves `logs_query` to an agent diagnosing its own tenant's
# failure, and failure-review (docs/design/failure-review.md §3) reads the
# platform components' entries. Both read Cloud Logging as swarm-api's account,
# which today reads no log at all.
#
# OWNER DECISION, 2026-10-22 (Q4): "A log view filtered to SwarmCloud
# resources, viewAccessor on the view only". So:
#
#   * a log view, swarm-workers, that holds only SwarmCloud's worker logs. It
#     lives on the swarm-worker-logs bucket (log_bucket.tf), which a sink fills
#     with those logs and nothing else -- BGD's decision of 2026-10-22 (#1960),
#     after Cloud Logging refused the view on _Default (below);
#   * IAM row L-1: roles/logging.viewAccessor for swarm-api's account, bound on
#     THAT VIEW's own IAM policy.
#
# WHY NOT roles/logging.viewer. saga-agents-staging is SHARED. The other team's
# agents-staging cluster writes k8s_container entries to this project's
# _Default bucket, and a transcript is where a secret turns up. A project-wide read --
# logging.viewer, privateLogViewer, or viewAccessor granted on the project
# without a condition, which reads EVERY view -- would hand swarm-api all of it.
# tests/terraform/log_view.tftest.hcl holds every project role this root grants
# against terraform/bootstrap/log-reading-roles.json.
#
# WHY A VIEW-LEVEL BINDING AND NOT A CONDITIONED PROJECT ONE. terraform/
# bootstrap/verify_logs.tf grants viewAccessor on the project with
# `resource.name == <view>` as its condition, because a grant to the deployer
# lives in the root the owner applies. This one is a binding in the view's own
# policy (google_logging_log_view_iam_member): there is no project binding to
# get wrong, and a binding in one view's policy cannot reach another view or
# bucket. It carries no condition for that reason -- the resource is the scope.
# It is an `_iam_member`, so scripts/lib/iam-plan.jq classifies it as an IAM
# change and a dev release holds it at `dev-iam` for the owner (docs/ci.md).
#
# Invariant 9 (per-tenant isolation) is NOT what this view enforces: it mixes
# every tenant's containers and jobs. swarm-api narrows each read further --
# an agent's logs_query is built from its lease's tenant (the namespace
# swarm-tenant-<tenant> and that tenant's job names) (§3.5). This view, and
# the sink that alone fills its bucket, are the outer wall that keeps the
# other team's logs out of everything swarm-api can read. The platform's own
# components (swarm-system, the swarm-* services) are not routed here: the
# sink carries workers only (log_bucket.tf).
#
# ---------------------------------------------------------------------------
# ON A BUCKET OF ITS OWN (lane LOG-BUCKET, part of #1960)
# ---------------------------------------------------------------------------
#
# Release run 39742374084 (2026-10-22) failed creating this view on _Default:
#
#   Error creating LogView: googleapi: Error 400: Filter exceeds the view
#   filter nesting depth limit of 1.
#
# Google's LogView reference allows a view filter only a logical AND of
# `=`/`!=` checks on the source (project, folder, organization, billing
# account), the resource type and the log id. No view on _Default can select
# by cluster or namespace, and the other team's cluster writes to that bucket.
# AU5-VIEWOFF switched the view off; BGD then decided (2026-10-22, #1960) a
# dedicated bucket fed by a sink. The sink (log_bucket.tf) does the selecting
# in the full query language, so this view's filter is the simplest one a
# view accepts: the one source check, SOURCE("projects/<project>"), no nesting.
#
# The whole path -- bucket, sink, writer grant, view, L-1 -- is behind
# var.swarm_workers_log_view_enabled (default false; on in dev.tfvars, off in
# prod until BGD turns it on there). While it is off the output is null and
# logs_query answers logs_unavailable.
#
# ---------------------------------------------------------------------------
# WHAT IS NOT VERIFIED. Nothing here has been applied.
# ---------------------------------------------------------------------------
#
# THE FILTER. SOURCE() is the form Google's log-views guide gives for a view
# filter; it has no nesting. If the service refuses it, the APPLY fails at the
# view and the binding, which depends on it, is never made.
#
# THE BINDING. The member's bucket is the view's full bucket name, as the
# provider's own example for google_logging_log_view_iam_member passes it.
# Whether the release's deployer may set a view's IAM policy
# (logging.views.setIamPolicy) has not been read off its roles; if it may not,
# the dev-iam apply 403s on this one resource and the view still exists.
#
# The first logs_query after the apply settles both: entries back, or the
# refusal with Cloud Logging's own error.

locals {
  swarm_workers_log_location = local.swarm_worker_logs_location
  swarm_workers_log_bucket   = local.swarm_worker_logs_bucket
  swarm_workers_log_view_id  = "swarm-workers"

  # The full resource name swarm-api reads entries through
  # (entries.list resourceNames).
  swarm_workers_log_view = "${local.swarm_workers_log_bucket}/views/${local.swarm_workers_log_view_id}"
}

# No labels: google_logging_log_view has none in the pinned provider and is in
# scripts/lib/unlabelable-types.json. Its id begins with the platform prefix,
# which is how the guards recognise an unlabelable resource as ours, and its
# description says who manages it.
#
# `bucket` is the bucket's full name, as verify_logs.tf writes it; the provider
# derives `parent` and `location` from it.
resource "google_logging_log_view" "swarm_workers" {
  count = var.swarm_workers_log_view_enabled ? 1 : 0

  name        = local.swarm_workers_log_view_id
  bucket      = local.swarm_workers_log_bucket
  filter      = "SOURCE(\"projects/${var.project_id}\")"
  description = "managed-by=swarm-terraform; SwarmCloud's worker logs, as the swarm-worker-logs sink routes them (swarm-tenant-* containers, swarm-job-* Cloud Run jobs), read by swarm-api (terraform/infra/log_view.tf)."

  depends_on = [google_logging_project_bucket_config.swarm_worker_logs]
}

# IAM row L-1 (owner decision Q4, applied by the owner at dev-iam).
resource "google_logging_log_view_iam_member" "swarm_api_reads_swarm_workers" {
  count = var.swarm_workers_log_view_enabled ? 1 : 0

  parent   = "projects/${var.project_id}"
  location = local.swarm_workers_log_location
  bucket   = google_logging_log_view.swarm_workers[0].bucket
  name     = google_logging_log_view.swarm_workers[0].name
  role     = "roles/logging.viewAccessor"
  member   = module.iam.service_account_members["swarm-api"]
}

output "swarm_workers_log_view" {
  description = "The full resource name of the swarm-workers log view, the one view swarm-api may read entries through (agent-autonomy.md §3.5); null while swarm_workers_log_view_enabled is false."
  value       = var.swarm_workers_log_view_enabled ? local.swarm_workers_log_view : null
}
