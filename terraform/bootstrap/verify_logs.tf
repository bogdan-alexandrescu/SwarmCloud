# ---------------------------------------------------------------------------
# swarm-verify's transcript, readable by the identity that runs the release
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
# verify-remote.sh now prints that log when a target fails. The deployer cannot
# read it. Its only logging role is roles/logging.configWriter, which can list
# log NAMES (logging.logs.list) but cannot read an entry. This was measured on
# 2026-09-24 with `gcloud projects get-iam-policy` and
# `gcloud iam roles describe`. So until this is applied, the release log prints
# the refusal and a command to run by hand.
#
# WHY NOT roles/logging.viewer. This project is SHARED. A project-wide log read
# would let any workflow on an allowed ref read every log the other team's
# cluster, services and jobs write, and a transcript is where a secret turns up.
# The gate needs one job's stdout.
#
# WHY NOT A VIEW ON _Default. A log view there could be filtered to this job.
# But _Default is the project's one default bucket and it holds the other
# team's logs, so it is not ours to hang things off. The deployer-scoping
# work in PR #34 conditions CI's configWriter to refuse every bucket and view
# operation there. A bucket of our own touches nothing shared.
#
# SO: a sink copies the job's stdout, stderr and the platform's exit line into
# a bucket of its own. The sink's filter can name the job, and does. The
# deployer may read that bucket's _AllLogs view and nothing else:
# roles/logging.viewAccessor, conditioned on the view's resource name in the
# format the Logging documentation gives for exactly this grant. The entries
# also stay in _Default, as they always have. This is a copy, not a move.
#
# WHY HERE AND NOT IN terraform/infra. This is a grant TO the deployer. Grants to
# the deployer come from this root, which the owner applies (`make bootstrap`),
# and never from the root the deployer applies to itself. It is also the only
# place it could work once PR #34's conditions land: they stop CI creating a log
# bucket, and stop it modifying the grants of any role outside the list
# terraform/infra hands out.
#
# WHAT IS NOT VERIFIED. Nothing here has been applied. The following are what
# Google documents, and nobody has yet watched them happen in this project:
#   * a sink into a bucket in its own project is "automatically authorized"
#     and needs no writer grant;
#   * Logging creates an _AllLogs view for every bucket;
#   * viewAccessor on that view is enough for `gcloud logging read --view`,
#     with no logging.logEntries.list.
# The first failed gate after `make bootstrap` either prints the transcript
# or prints the refusal. Either way, the release log says which.
#
# logging.googleapis.com is not in var.prerequisite_services. It is enabled by
# default in every project, and terraform/infra enables it too.

locals {
  # verify-remote.sh reads the view at these two values (LOG_BUCKET and
  # LOG_LOCATION there). tests/integration/test_verify_remote_prints_the_job_log.py
  # reads the grant's view and the sink's filter out of THIS file and lets its
  # fake release identity read that one view, so a rename here that the script
  # does not follow fails that test rather than a release.
  #
  # A literal rather than "${var.name_prefix}-...": it belongs to the job, and
  # the job's name is a literal in terraform/infra/verify.tf.
  verify_log_bucket_id = "swarm-verify-logs"
  verify_log_location  = "global"
  verify_log_view      = "projects/${var.project_id}/locations/${local.verify_log_location}/buckets/${local.verify_log_bucket_id}/views/_AllLogs"

  # The job's stdout, its stderr and run.googleapis.com/varlog/system, which
  # carries "Container called exit(1)." It does NOT include the job's audit
  # events. They share the job's labels, they are a JSON blob per state change,
  # and they say nothing about which case failed. `logName:` keeps them out.
  #
  # `swarm-verify` is the name terraform/infra/verify.tf gives
  # google_cloud_run_v2_job.verify. The integration test above builds its log
  # entries from that name, so a sink filter naming any other job routes
  # nothing there.
  #
  # MEASURED on 2026-09-24. The same three clauses plus the execution label
  # matched swarm-verify-m9prt's 41 transcript lines: 40 stderr and 1
  # varlog/system. The only entry they left out was its one
  # cloudaudit system_event.
  verify_log_sink_filter = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"swarm-verify\" AND logName:\"run.googleapis.com%2F\""
}

# No labels: google_logging_project_bucket_config has no labels attribute in the
# pinned provider (6.50.0, `terraform providers schema -json`, 2026-09-24), and
# is listed in scripts/lib/unlabelable-types.json for that reason. Its id begins
# with the platform prefix, which is how the guards recognise an unlabelable
# resource as ours.
#
# A deleted log bucket is not gone at once. It waits 7 days in DELETE_REQUESTED,
# where it can be undeleted. Plan a re-create of the same id with that in mind.
resource "google_logging_project_bucket_config" "verify" {
  project   = var.project_id
  location  = local.verify_log_location
  bucket_id = local.verify_log_bucket_id

  # 30 days is what _Default keeps the same entries for, and a failed gate is
  # diagnosed within the release that failed. A longer copy would only be a
  # second, longer-lived home for whatever a transcript leaked.
  retention_days = 30

  description = "swarm-verify's stdout/stderr, copied by the swarm-verify-logs sink so the release's deployer can read one job's transcript and no other log."
}

resource "google_logging_project_sink" "verify" {
  project = var.project_id
  name    = local.verify_log_bucket_id

  # Built from the same locals rather than from the bucket's computed id, so a
  # plan against an empty project -- and the mock provider in tests/terraform --
  # knows it. depends_on supplies the ordering the reference would have.
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/${local.verify_log_location}/buckets/${local.verify_log_bucket_id}"
  filter      = local.verify_log_sink_filter

  description = "Copies the swarm-verify job's transcript into its own bucket; see terraform/bootstrap/verify_logs.tf."

  depends_on = [google_logging_project_bucket_config.verify]
}

resource "google_project_iam_member" "deployer_reads_verify_logs" {
  count = local.wif_enabled

  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm-verify transcript only"
    description = "The _AllLogs view of swarm-verify-logs, which holds only the verification job's stdout and stderr."
    expression  = "resource.name == \"${local.verify_log_view}\""
  }
}
