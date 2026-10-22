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
#   * a log view on the project's _Default bucket -- where GKE containers and
#     Cloud Run already write -- whose filter admits only SwarmCloud's
#     resources;
#   * IAM row L-1: roles/logging.viewAccessor for swarm-api's account, bound on
#     THAT VIEW's own IAM policy.
#
# WHY NOT roles/logging.viewer. saga-agents-staging is SHARED. The other team's
# agents-staging cluster writes k8s_container entries to this same _Default
# bucket, and a transcript is where a secret turns up. A project-wide read --
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
# every tenant's containers and jobs with the platform's own services.
# swarm-api narrows each read further -- an agent's logs_query is built from
# its lease's tenant (the namespace swarm-tenant-<tenant> and that tenant's
# job names), and only failure-review, platform-scoped, reads the platform
# components (§3.5). This view is the outer wall that keeps the other team's
# logs out of everything swarm-api can read.
#
# ---------------------------------------------------------------------------
# OFF BY DEFAULT (lane AU5-VIEWOFF, part of #1933)
# ---------------------------------------------------------------------------
#
# Release run 39742374084 (2026-10-22) failed creating the view:
#
#   Error creating LogView: googleapi: Error 400: Filter exceeds the view
#   filter nesting depth limit of 1.
#
# Google's LogView reference allows a view filter only a logical AND of
# `=`/`!=` checks on the source (project, folder, organization, billing
# account), the resource type and the log id. No view on _Default can select
# by cluster or namespace, and the other team's cluster writes to that bucket,
# so this view cannot be built as designed. (verify_logs.tf records the
# guide's newer "flexible filter" text admitting labels; what the 400 measured
# is the nesting, and the reference text above is the restriction quoted.) Both resources below therefore
# carry count = var.swarm_workers_log_view_enabled (default false, variables.tf
# says why) and the output is null; logs_query answers logs_unavailable. The
# redesign is the owner's (#1933). Nothing existed to remove: the view's
# create failed and the binding was never attempted.
#
# ---------------------------------------------------------------------------
# WHAT IS NOT VERIFIED. Nothing here has been applied.
# ---------------------------------------------------------------------------
#
# THE FILTER'S GRAMMAR. A view filter that tests a label is what the Logging
# guide calls a FLEXIBLE filter (docs.cloud.google.com/logging/docs/logs-views,
# "Filters for log views"; label support added 2026-04-02, per verify_logs.tf).
# This filter also uses OR between branches and `=~` (RE2) for the prefixes.
# If the service refuses either, the APPLY fails -- INVALID_ARGUMENT on the
# view, at the dev-iam apply -- and the binding, which depends on the view, is
# never made. Nothing reads more than it should on that path; logs_query keeps
# answering logs_unavailable.
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
  # _Default's location is global in this project (`gcloud logging buckets
  # describe _Default --location=global`, read 2026-09-24 for verify_logs.tf).
  swarm_workers_log_location = "global"
  swarm_workers_log_bucket   = "projects/${var.project_id}/locations/${local.swarm_workers_log_location}/buckets/_Default"
  swarm_workers_log_view_id  = "swarm-workers"

  # The full resource name swarm-api reads entries through
  # (entries.list resourceNames).
  swarm_workers_log_view = "${local.swarm_workers_log_bucket}/views/${local.swarm_workers_log_view_id}"

  # These branches are a SINK filter's shape, not a view's: Cloud Logging
  # refused their OR of AND branches in a view filter (nesting depth limit of
  # 1; see OFF BY DEFAULT above).
  # They are kept, with the tftest that parses and evaluates them, for the
  # redesign to reuse.
  #
  # THE BRANCHES. The filter is these, each parenthesised, joined by OR -- and
  # nothing else; log_view.tftest.hcl holds it to that, parses every clause,
  # and evaluates the result against entries that must and must not be read.
  # A branch is a plain AND of `field="value"` / `field=~"regex"` clauses with
  # exactly one resource.type. Never write OR, NOT, `-` or `:` inside one:
  # a branch may only narrow.
  #
  #   * k8s_container in the swarm cluster -- the same name main.tf gives
  #     module.gke_autopilot -- because the other team's agents-staging cluster
  #     writes to this bucket too, and could in principle hold a namespace
  #     spelled like ours;
  #   * namespaces swarm-tenant-* (kubernetes/render.py's prefix, the one the
  #     scheduler dispatches into) and swarm-system (every manifest under
  #     kubernetes/swarm-system/), anchored, so `not-swarm-tenant-x` and
  #     `swarm-system-2` stay out;
  #   * cloud_run_job and cloud_run_revision whose names start with the
  #     platform prefix (every tenant's worker jobs, `swarm-verify`, and
  #     swarm-api, -scheduler, -reconciler, -quota-broker, -ui).
  swarm_workers_log_branches = [
    "resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${var.name_prefix}-autopilot\" AND resource.labels.namespace_name=~\"^swarm-tenant-\"",
    "resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${var.name_prefix}-autopilot\" AND resource.labels.namespace_name=\"swarm-system\"",
    "resource.type=\"cloud_run_job\" AND resource.labels.job_name=~\"^${var.name_prefix}-\"",
    "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^${var.name_prefix}-\"",
  ]
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
  filter      = join(" OR ", [for b in local.swarm_workers_log_branches : "(${b})"])
  description = "managed-by=swarm-terraform; SwarmCloud's GKE containers (swarm-tenant-*, swarm-system) and swarm-* Cloud Run jobs and services, read by swarm-api (terraform/infra/log_view.tf)."

  depends_on = [module.project_services]
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
