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
#
# So var.deployer_roles (variables.tf) refuses two things: every predefined
# role MEASURED to read log entries -- log-reading-roles.json, 50 of the 2,397
# predefined roles on 2026-09-24, among them roles/iam.securityReviewer and
# seven roles/firebase.* roles, not only the logging ones -- and every role not on
# local.deployer_roles_reviewed below, which is what catches a role Google
# changes after that date. That keeps this the only log read this root GRANTS.
# It is not the only log read the deployer can REACH: see WHAT THIS DOES NOT
# BOUND.
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
#     scoping roles/logging.configWriter (deployer_conditions.tf) would stop CI
#     editing it. A sink is not, and CI can rewrite a sink's filter under any
#     condition this project could write. That closes one route among several;
#     see WHAT THIS DOES NOT BOUND.
#
# PR #50's own objection to a view on _Default was that _Default holds the other
# team's logs and is "not ours to hang things off". A view adds nothing to the
# bucket and changes nothing its owners read; it is a filter the deployer reads
# through. It is created by this root, which the OWNER applies. CI can still
# edit it until roles/logging.configWriter is scoped (deployer_scoped_roles is
# [] on 2026-09-24).
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
# WHAT THIS DOES NOT BOUND -- THE DEPLOYER CAN REACH EVERY LOG IN THE PROJECT
# ---------------------------------------------------------------------------
#
# "swarm-verify ONLY" is what THIS GRANT gives. It is not what the deployer can
# read, and no condition written in deployer_conditions.tf makes it so -- not
# even with every scopable role scoped. A workflow on an allowed ref can read
# every log in this project, the other team's Data Access audit logs included,
# by any of the routes below. Measured 2026-09-24 with read-only gcloud
# (`projects get-iam-policy`, `iam roles describe`, `iam service-accounts
# get-iam-policy`); none of them has been exercised, and the list is what was
# found, not a proof that there is nothing else.
#
# OPEN, AND CLOSED BY NO CONDITION IN deployer_conditions.tf:
#
#   1. It needs nothing it does not already hold. The deployer has
#      roles/iam.serviceAccountUser on 209012342332-compute@developer
#      (wif.tf, deployer_acts_as_cloudbuild -- it is what `gcloud builds
#      submit` runs as), and that account holds roles/editor project-wide,
#      which carries logging.logEntries.list. A build step, or a Cloud Run job
#      (run.admin), running `gcloud logging read` as that account reads
#      everything.
#   2. roles/iam.roleAdmin (UNSCOPABLE, variables.tf) carries iam.roles.update.
#      CI can add logging.logEntries.list to a custom role it holds:
#      swarmSecretProvisioner, whose scoped form's type guard still admits
#      every non-secret resource; swarmDeployerProjectBuckets, whose binding in
#      wif.tf is never conditioned (in this root, not yet applied); or one of
#      terraform/infra's six custom roles, which the SCOPED projectIamAdmin
#      still lets it grant itself (deployer_grantable_project_roles).
#   3. roles/iam.serviceAccountAdmin (UNSCOPABLE) carries
#      iam.serviceAccounts.setIamPolicy. CI can grant itself
#      roles/iam.serviceAccountTokenCreator on any account that reads logs --
#      the compute account above, or 209012342332@cloudbuild, which holds
#      roles/cloudbuild.builds.builder (logging.logEntries.list and
#      logging.views.access) -- and act as it.
#   4. roles/logging.configWriter's sinks and exclusions stay project-wide when
#      the role is scoped (deployer_conditions.tf says so). A sink can route
#      every log in the project to a destination CI administers: a swarm-
#      bucket (storage.admin) or a Pub/Sub topic (pubsub.admin).
#
# CLOSED ONLY ONCE THE ROLE IS SCOPED (deployer_scoped_roles, not yet applied):
#
#   5. roles/logging.configWriter unconditioned holds logging.views.update, so
#      CI can rewrite this view's filter, or make another view, and read the
#      result through the grant. Its scoped form refuses every LogBucket and
#      LogView operation.
#   6. roles/resourcemanager.projectIamAdmin unconditioned lets CI grant itself
#      roles/logging.viewer outright. Its scoped form refuses that role -- but
#      not route 2.
#
# What bounds all six today is the ref pin, not IAM: only a workflow on
# refs/heads/main (terraform.tfvars, github_allowed_refs) can mint the
# deployer's token, so each route has to be merged to main first. Closing 1
# means building as an account without roles/editor; 2 and 3 mean taking
# roles/iam.roleAdmin and roles/iam.serviceAccountAdmin off the deployer, or
# replacing them with resource-level grants on swarm-* roles and accounts;
# 4 means moving sink management out of CI. Each is a change to what CI can
# do, and none is made here.
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
  verify_log_view_filter = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"swarm-verify\""

  # EVERY PREDEFINED ROLE MEASURED TO READ LOG ENTRIES, read from the file the
  # measurement wrote rather than restated: all 2,397 predefined roles listed
  # with their permissions on 2026-09-24, keeping the 50 that carry
  # logging.logEntries.list, logging.privateLogEntries.list or
  # logging.views.access (the file says how). var.deployer_roles refuses every
  # one, and tests/terraform/verify_logs.tftest.hcl reads the same file.
  #
  # A DENYLIST MEASURED ON A DATE. Google adds roles and adds permissions to
  # existing roles; a role that reads nothing today may read logs next year.
  # The list below is what catches that.
  log_reading_roles = keys(jsondecode(file("${path.module}/log-reading-roles.json")).roles)

  # THE ROLES var.deployer_roles MAY NAME AT ALL. Each was checked on
  # 2026-09-24 against the same full listing that wrote log-reading-roles.json,
  # and carries none of the three permissions above. A role not listed here is
  # refused at plan, so adding one to deployer_roles means adding it here too
  # -- and adding it here is the review: describe the role, and confirm it
  # carries none of the three and is not in log-reading-roles.json.
  #
  # This is a second list of the same names as deployer_roles' default, on
  # purpose: two keys, one of which is the review. Drift is loud rather than
  # silent -- a default missing from here fails every plan, CI's
  # `terraform test` included.
  #
  # "Reviewed" means the role does not READ a log itself. It does not mean it
  # cannot be used to GET one: roles/iam.roleAdmin and
  # roles/iam.serviceAccountAdmin are here, and WHAT THIS DOES NOT BOUND says
  # how each can.
  deployer_roles_reviewed = [
    "roles/artifactregistry.admin",
    "roles/cloudbuild.builds.editor",
    "roles/cloudscheduler.admin",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/container.admin",
    "roles/datastore.owner",
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/logging.configWriter",
    "roles/monitoring.editor",
    "roles/pubsub.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/serviceusage.serviceUsageAdmin",
  ]
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
    expression  = "resource.name == \"${local.verify_log_view}\""
  }

  # A grant naming a view that does not exist yet reads nothing; created in
  # this order, it never names one.
  depends_on = [google_logging_log_view.verify]
}
