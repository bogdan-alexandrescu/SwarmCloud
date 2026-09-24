# ---------------------------------------------------------------------------
# swarm-verify's logs, readable by the identity that runs the release -- and
# no other log
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS
#
# The release runs the in-VPC verification gate with scripts/verify-remote.sh,
# as swarm-tf-deployer. `gcloud run jobs execute --wait` tells it only THAT the
# gate failed. WHY is in the job's own stdout, which goes to Cloud Logging.
# Release 36038727721 printed "smoke-test FAILED (exit 1)" and nothing else, and
# the two failing cases were found by a second person reading the execution's
# log by hand.
#
# verify-remote.sh prints that log when a target fails. The deployer cannot
# read it: none of its 18 project roles carries logging.logEntries.list,
# logging.privateLogEntries.list, logging.views.access or
# logging.logEntries.download (each role's permissions listed with
# `gcloud iam roles describe`, 2026-09-24). Its one logging role,
# roles/logging.configWriter, administers configuration and reads no entry.
#
# OWNER DECISION, 2026-09-24: the deployer may read the swarm-verify job's logs
# ONLY. It gets:
#
#   * a log view on the project's _Default bucket -- where Cloud Run already
#     writes the job's stdout and stderr -- whose filter selects that job and
#     nothing else;
#   * roles/logging.viewAccessor, conditioned on that view's resource name.
#
# WHY NOT roles/logging.viewer. This project is SHARED. A project-wide log read
# would let any workflow on an allowed ref read every log the other team's
# cluster, services and jobs write, and a transcript is where a secret turns up.
# variables.tf refuses every role that reads log entries project-wide on
# var.deployer_roles, so this stays the deployer's only log read.
#
# WHAT THIS REPLACES. PR #50 wrote the same grant against a copy: a sink routing
# the job's lines into a swarm-verify-logs bucket, and the grant on that
# bucket's _AllLogs view. It was never applied -- on 2026-09-24
# `gcloud logging buckets list` and `gcloud logging sinks list` showed only
# _Default and _Required, and the owner's bootstrap state held no logging
# resource -- so replacing it destroys nothing. The view is the better shape for
# three reasons:
#
#   * no second copy of a transcript, which would be a second, separately
#     retained home for whatever a transcript leaked;
#   * every execution still inside _Default's 30-day retention is readable the
#     moment the view exists, not only those after a sink was created;
#   * a view is a resource IAM can name (logging.googleapis.com/LogView), so
#     scoping roles/logging.configWriter (deployer_conditions.tf) stops CI
#     editing it. A sink is not, and CI can rewrite a sink's filter under any
#     condition this project could write. See WHAT THIS DOES NOT BOUND.
#
# PR #50's own objection to a view on _Default was that _Default holds the other
# team's logs and is "not ours to hang things off". A view adds nothing to the
# bucket and changes nothing its owners read; it is a filter the deployer reads
# through. It is created by this root, which the OWNER applies, and CI is kept
# from touching it by the configWriter condition that objection pointed to.
#
# WHY HERE AND NOT IN terraform/infra. This is a grant TO the deployer. Grants to
# the deployer come from this root, which the owner applies (`make bootstrap`),
# and never from the root the deployer applies to itself.
#
# ---------------------------------------------------------------------------
# WHAT GOOGLE DOCUMENTS, AND WHERE. Each of these is copied, not inferred.
# ---------------------------------------------------------------------------
#
# THE CONDITION. Two Google pages give the same form:
#
#   docs.cloud.google.com/logging/docs/logs-views, "Control access to a log
#   view" -- project-level roles/logging.viewAccessor with
#       expression: "resource.name == \"projects/PROJECT_ID/locations/LOCATION/
#                    buckets/BUCKET_NAME/views/LOG_VIEW_ID\""
#   docs.cloud.google.com/iam/docs/conditions-resource-attributes, the
#   resource.name table -- "Cloud Logging log views:
#   projects/project-id/locations/location-id/buckets/bucket-id/views/view-id"
#   (the project ID, not the number), resource type
#   logging.googleapis.com/LogView.
#
# THE READ. entries.list takes a view as its resource and "requires one or more
# of these permissions on the specified resource: logging.logEntries.list,
# logging.privateLogEntries.list, logging.views.access"
# (docs.cloud.google.com/logging/docs/reference/v2/rest/v2/entries/list).
# roles/logging.viewAccessor is logging.views.access, views.listLogs,
# views.listResourceKeys, views.listResourceValues and logEntries.download
# (`gcloud iam roles describe`, 2026-09-24). `gcloud logging read --bucket B
# --location L --view V` sends exactly
# projects/<p>/locations/L/buckets/B/views/V as that resource
# (googlecloudsdk surface/logging/read.py, Cloud SDK 483.0.0), and that is the
# form verify-remote.sh uses. So viewAccessor alone should be enough.
#
# THE FILTER. A view filter that tests a label is what the Logging guide calls
# a FLEXIBLE filter (docs.cloud.google.com/logging/docs/logs-views, "Filters for
# log views"), and its release notes list label support as added on
# 2026-04-02. Two older texts still say a view filter may use only SOURCE(),
# resource.type and LOG_ID(): the REST reference for LogView.filter, and the
# `filter` description in the pinned provider (6.50.0,
# `terraform providers schema -json`). If the service holds to the older
# rule, it is the APPLY that fails -- INVALID_ARGUMENT on the view, at the
# owner's terminal -- and nothing is left half-made, because the grant
# depends on the view.
#
# MEASURED, 2026-09-24, over 30 days: the filter's two clauses matched, in
# _Default, 331 run.googleapis.com/stderr, 34 stdout and 29 varlog/system
# entries and nothing else. The job's audit events are not in _Default: its 39
# activity and 62 system_event entries are in _Required, which Cloud Logging
# routes those two audit logs to and which this view is not on.
#
# ---------------------------------------------------------------------------
# WHAT IS NOT VERIFIED -- THE CONDITION IS PROVEN ONLY WHEN A FAILED RELEASE
# PRINTS THE LOG.
# ---------------------------------------------------------------------------
#
# This repository has twice shipped an IAM condition written from
# documentation that matched nothing: the iap.admin condition on
# ".../services/swarm", which IAP names by project NUMBER and numeric id
# (release 35972131246, wif.tf "WHO MAY PASS IAP"), and storage.admin on an
# exact bucket name, which admitted the bucket and refused the object `gcloud
# builds submit` uploads (wif.tf, "storage.admin, SCOPED"). This condition is
# in the documented form, from two pages that agree, and that is ALL that can
# be said for it until it has been exercised. Nothing here has been applied.
#
# The first failed gate after `make bootstrap` settles it. verify-remote.sh
# either prints the execution's transcript -- the view exists, the filter was
# accepted, IAM evaluated the condition as documented, and viewAccessor was
# enough -- or prints the refusal with gcloud's own error, which says which of
# those was wrong. A failed gate stays failed either way.
#
# ---------------------------------------------------------------------------
# WHAT THIS DOES NOT BOUND
# ---------------------------------------------------------------------------
#
# The view is only as narrow as the deployer's ability to edit it. While
# roles/logging.configWriter is granted to the deployer unconditioned --
# deployer_scoped_roles is [] in terraform.tfvars on 2026-09-24 -- it holds
# logging.views.update project-wide, so a workflow on an allowed ref could
# rewrite this view's filter to anything and then read that through the grant.
# Naming roles/logging.configWriter in deployer_scoped_roles refuses CI every
# LogBucket and LogView operation (deployer_conditions.tf), and that is what
# makes "swarm-verify ONLY" hold against the deployer itself. Likewise, an
# unconditioned roles/resourcemanager.projectIamAdmin lets CI grant itself
# roles/logging.viewer outright; its scoped form refuses that role.
#
# ---------------------------------------------------------------------------
#
# logging.googleapis.com is not in var.prerequisite_services. It is enabled by
# default in every project, and terraform/infra enables it too.

locals {
  # verify-remote.sh reads the view at these values (LOG_BUCKET, LOG_LOCATION
  # and LOG_VIEW there). tests/integration/test_verify_remote_prints_the_job_log.py
  # reads the grant's view and the view's filter out of THIS file, checks that
  # the view granted is the view created, and lets its fake release identity
  # read that one view -- so a rename here that the script does not follow
  # fails that test rather than a release.
  #
  # Literals rather than "${var.name_prefix}-...": the view belongs to the job,
  # and the job's name is a literal in terraform/infra/verify.tf. _Default's
  # location is global in this project (`gcloud logging buckets describe
  # _Default --location=global`, 2026-09-24).
  verify_log_location = "global"
  verify_log_bucket   = "projects/${var.project_id}/locations/${local.verify_log_location}/buckets/_Default"
  verify_log_view_id  = "swarm-verify"
  verify_log_view     = "${local.verify_log_bucket}/views/${local.verify_log_view_id}"

  # The owner's filter, as decided. `swarm-verify` is the name
  # terraform/infra/verify.tf gives google_cloud_run_v2_job.verify; the
  # integration test above builds its log entries from that name, so a filter
  # naming any other job shows the deployer nothing.
  verify_log_view_filter = "resource.type=\"cloud_run_job\""
}

# No labels: google_logging_log_view has none in the pinned provider (6.50.0,
# `terraform providers schema -json`, 2026-09-24: bucket, create_time,
# description, filter, id, location, name, parent, update_time), and is listed
# in scripts/lib/unlabelable-types.json for that reason. Its id begins with the
# platform prefix, which is how the guards recognise an unlabelable resource as
# ours, and its description says who manages it.
#
# `bucket` is the bucket's full name, the form the provider's own example uses;
# it derives `parent` and `location` from it.
resource "google_logging_log_view" "verify" {
  name        = local.verify_log_view_id
  bucket      = local.verify_log_bucket
  filter      = local.verify_log_view_filter
  description = "managed-by=swarm-terraform; the swarm-verify job's logs and no other, read by the release's deployer (terraform/bootstrap/verify_logs.tf)."
}

resource "google_project_iam_member" "deployer_reads_verify_logs" {
  count = local.wif_enabled

  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm-verify log view only"
    description = "The swarm-verify view on _Default, which selects only the verification job's logs. Owner decision 2026-09-24."
    expression  = "resource.name == \"${local.verify_log_bucket}/views/_AllLogs\""
  }

  # A grant naming a view that does not exist yet reads nothing; created in
  # this order, it never names one.
  depends_on = [google_logging_log_view.verify]
}
