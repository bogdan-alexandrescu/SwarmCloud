# ---------------------------------------------------------------------------
# swarm-api's read of the swarm-workers log view -- and of no other log
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS (docs/design/agent-autonomy.md §3.5, IAM row L-1; owner
# decision Q4, 2026-10-22: "A log view filtered to SwarmCloud resources,
# viewAccessor on the view only"). swarm-api serves `logs_query` to an agent
# diagnosing its own tenant's failure, and failure-review reads workers' logs
# the same way. Both read Cloud Logging as swarm-api's account, through the
# swarm-workers view terraform/infra creates (log_view.tf) on the
# swarm-worker-logs bucket a sink fills with SwarmCloud's worker logs and
# nothing else (log_bucket.tf).
#
# WHY HERE AND NOT IN terraform/infra (BGD's decision, 2026-10-23, #1960,
# lane LOG-VIEW-BOOT). THE OWNER APPLIES THIS (`make bootstrap`); CI never
# does. The binding was first in terraform/infra/log_view.tf, and release run
# 39888771458 (2026-10-23, head 2aa01cbe) failed it at
# `terraform apply, IAM (dev-iam)`:
#
#   Error 403: The caller does not have permission
#
# CI's deployer holds roles/logging.configWriter, which creates views but
# cannot set IAM on one, so every release after held at dev-iam on that row
# and then failed. Giving CI that power would let any CI run grant itself or
# anyone a read of any view in this SHARED project. Granted from here, CI's
# roles stay as they are: tests/terraform/log_view_read.tftest.hcl holds that
# no logging role is on deployer_grantable_project_roles and that this is the
# one log-view IAM resource in terraform/, and log_view.tftest.hcl that
# terraform/infra declares none. The precedent is verify_cluster_read.tf,
# which moved swarm-verify's clusterViewer here for the same reason.
#
# WHAT IT CAN READ. roles/logging.viewAccessor (logging.views.access) in the
# swarm-workers view's OWN IAM policy: the entries of that one view, which are
# what the sink routed -- swarm-tenant-* containers in the swarm cluster and
# swarm-job-* Cloud Run jobs. A binding in one view's policy cannot reach
# another view, another bucket or _Default, so it carries no condition: the
# resource is the scope. Invariant 9 is swarm-api's to keep inside the view: it
# builds every logs_query from the lease's tenant (§3.5).
#
# WHAT IT CANNOT READ. Any other log in the project. It is not
# roles/logging.viewer, privateLogViewer, or viewAccessor on the project, any
# of which would read the other team's logs on _Default. It names no other
# team's resource: the bucket and view are ours (the platform prefix), in our
# project, and nothing here names agents-staging or any entry on the shared
# deny-list (scripts/lib/common.sh).
#
# The view must exist before this is applied: dev's terraform/infra creates it
# (swarm_workers_log_view_enabled = true in dev.tfvars), and
# var.swarm_workers_log_view_exists says so. Set it false for a project whose
# infra has the view off, or the apply fails on a view that is not there. The account is terraform/infra's
# (modules/iam), spelled here as the other swarm-api grants in this root spell
# it. A log-view IAM member takes no labels and no condition, so nothing here
# can carry managed-by=swarm-terraform; the view it is on carries it in its
# description.

locals {
  # As terraform/infra names them (log_bucket.tf, log_view.tf): the bucket in
  # the project's region, by its bare id, and the view's id.
  log_view_read_location  = var.region
  log_view_read_bucket_id = "${var.name_prefix}-worker-logs"
  log_view_read_view_id   = "swarm-workers"
  log_view_read_member    = "serviceAccount:swarm-api@${var.project_id}.iam.gserviceaccount.com"
}

# IAM row L-1.
resource "google_logging_log_view_iam_member" "swarm_api_reads_swarm_workers" {
  count = var.swarm_workers_log_view_exists ? 1 : 0

  parent   = "projects/${var.project_id}"
  location = local.log_view_read_location
  bucket   = local.log_view_read_bucket_id
  name     = local.log_view_read_view_id
  role     = "roles/logging.viewAccessor"
  member   = local.log_view_read_member
}
