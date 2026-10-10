# Post-deploy acceptance's identity, federated from exactly one workflow file
# on main.
#
# .github/workflows/accept.yml (owner decision 2026-10-08, cuts A and C of the
# release timing report; docs/ci.md, "The release timeline") warms the worker
# jobs, runs the smoke test, the GKE proof and the acceptance suite after a
# dev release, OUTSIDE the release's lock. Everything it proves runs INSIDE the
# VPC as the swarm-verify Cloud Run job, which has its own identity
# (terraform/infra/verify.tf); the workflow only has to start executions and
# read back what they said.
#
# WHY NOT THE DEPLOYER. Those steps ran as the deployer while they were jobs of
# release.yml, because the deployer was the identity release.yml had. The
# deployer holds run.admin, projectIamAdmin and the state bucket on a SHARED
# project; a workflow that only starts verification jobs and reads one record
# needs none of it, and adding accept.yml to wif.tf's deployer_workflows would
# make one more file on main that can become all of it. So accept.yml
# federates as this account directly, and nothing else does (the same shape
# as ci_fix.tf). tests/unit/scripts/test_workflow_id_token_scope.py holds
# accept.yml off the deployer's list.
#
# WHAT IT CAN DO, each grant below for one step of accept.yml:
#
#   * START swarm-verify and the per-tenant worker jobs (swarm-job-*) and read
#     their executions -- verify-remote.sh and warm-jobs.sh. A custom role
#     with exactly those permissions, granted at PROJECT level with NO
#     condition (owner decision 2026-10-09, #965; see the binding below). Not
#     create/update/delete and not IAM: it cannot change what a job runs, only
#     run it (warm-jobs.sh overrides the arguments to `--self-test`, which is
#     why runWithOverrides is in it).
#   * LIST jobs and read operations, unconditioned: warm-jobs.sh lists the
#     worker jobs by label, and a list is checked against the location, not a
#     job. Read-only; it shows job definitions, which
#     the swarm-verify identity can already read project-wide (run.viewer).
#   * READ the swarm-verify log view, the same conditioned grant the deployer
#     has (verify_logs.tf), so a failed group prints its own transcript.
#   * READ releases/ in the state bucket -- releases/dev/applied.json, the SHA
#     dev runs (scripts/lib/release-order.sh `deployed`). Conditioned to that
#     prefix: the bucket also holds every root's Terraform state, which carries
#     sensitive values, and this account reads none of it.
#
# It cannot plan, apply, promote, write a record, take the release lock or
# touch IAM.
#
# WHAT A MOCK PROVIDER CANNOT PROVE: that IAM evaluates the two remaining
# conditions (the log view and the releases/ prefix) the way Google documents
# them for Cloud Logging and Cloud Storage resource names. Both services DO
# expose resource.name to IAM Conditions; Cloud Run does NOT, which is why the
# runner grant carries none (#965). The first accept.yml run after
# `make bootstrap` is that proof.
#
# APPLIED BY THE OWNER (`make bootstrap`), never by the release. Then set the
# repository variable GCP_ACCEPT_SA to the `github_accept_service_account`
# output. Until both, accept.yml's first job fails at authentication and its
# report files that as a red acceptance.
#
# Nothing here carries a label because nothing here can: a service account, a
# custom role and an IAM member have no labels field. The account and the role
# say `managed-by=swarm-terraform` in their description, as the deployer does.
locals {
  # workflow_run fires only for the workflow file on the default branch, and
  # the dispatch is main-only too, so `@refs/heads/main` is spelled here rather
  # than taken from github_allowed_refs: widening the deployer's refs must not
  # widen this account's.
  accept_workflow_ref = "${var.github_repository}/.github/workflows/accept.yml@refs/heads/main"
}

resource "google_service_account" "acceptance" {
  count = local.wif_enabled

  project      = var.project_id
  account_id   = "${var.name_prefix}-accept"
  display_name = "Swarm post-deploy acceptance"
  description  = "managed-by=swarm-terraform; assumed by .github/workflows/accept.yml on main via WIF. Starts verification and warm executions; reads releases/ and the swarm-verify logs. No keys are ever created for it."
}

resource "google_service_account_iam_member" "acceptance_wif" {
  count = local.wif_enabled

  service_account_id = google_service_account.acceptance[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.job_workflow_ref/${local.accept_workflow_ref}"
}

resource "google_project_iam_custom_role" "acceptance_runner" {
  count = local.wif_enabled

  project     = var.project_id
  role_id     = "swarmAcceptanceRunner"
  title       = "Swarm Acceptance Runner"
  description = "managed-by=swarm-terraform; start an execution of a named job, read it and cancel a running one. No create, update, delete or IAM. Granted to the acceptance account at project level, unconditioned: Cloud Run exposes no resource.name to IAM Conditions (terraform/bootstrap/acceptance.tf)."
  stage       = "GA"

  permissions = [
    "run.jobs.get",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.executions.get",
    # accept.yml cancels swarm-verify executions an earlier, cancelled run
    # left running (a GitHub cancel does not stop Cloud Run). Cancel only
    # stops a running execution; it cannot create or delete one.
    "run.executions.cancel",
    "run.tasks.get",
  ]
}

resource "google_project_iam_custom_role" "acceptance_lister" {
  count = local.wif_enabled

  project     = var.project_id
  role_id     = "swarmAcceptanceLister"
  title       = "Swarm Acceptance Lister"
  description = "managed-by=swarm-terraform; list Cloud Run jobs and executions and read operations. Read-only (terraform/bootstrap/acceptance.tf)."
  stage       = "GA"

  permissions = [
    "run.jobs.list",
    "run.executions.list",
    "run.tasks.list",
    "run.operations.get",
  ]
}

# UNCONDITIONED, AT PROJECT LEVEL. Owner decision 2026-10-09 (#965).
#
# This binding used to carry a condition on the job name
# (`resource.name.endsWith("/jobs/swarm-verify")` and
# `extract("/jobs/{job}").startsWith("swarm-job-")`). It never matched any
# job: Cloud Run does not expose resource.name to IAM Conditions. Google's
# "Resource attributes for IAM Conditions" page lists no run.googleapis.com
# resource ("Other services and resource types do not recognize resource
# attributes"), and the IAM Policy Troubleshooter, measured read-only on
# 2026-10-09, evaluates the endsWith clause as false even with the job's name
# supplied. So swarm-accept was denied run.jobs.get on swarm-verify and
# `warm, smoke and GKE proof (dev)` was red on every run.
#
# THE TRADE-OFF THE OWNER ACCEPTED. Unconditioned, this role reaches EVERY
# Cloud Run job in saga-agents-staging, present and future: the other team's
# jobs in this shared project, and swarm's own merge job
# (swarm-job-eng-merge), as well as swarm-verify and the worker jobs. What it
# can do to any of them is what the role holds and no more: get a job, start
# an execution (with overridden arguments), read executions and tasks, and
# cancel a running execution. No create, update, delete or IAM permission, so
# it cannot change what any job runs or who may run it. What bounds the
# account is that only accept.yml on refs/heads/main can become it
# (acceptance_wif above), not a condition.
#
# Removing the condition replaces this binding (a condition is part of an IAM
# member's identity): the plan shows it destroyed and created.
resource "google_project_iam_member" "acceptance_runs_jobs" {
  count = local.wif_enabled

  project = var.project_id
  role    = google_project_iam_custom_role.acceptance_runner[0].name
  member  = "serviceAccount:${google_service_account.acceptance[0].email}"
}

resource "google_project_iam_member" "acceptance_lists_jobs" {
  count = local.wif_enabled

  project = var.project_id
  role    = google_project_iam_custom_role.acceptance_lister[0].name
  member  = "serviceAccount:${google_service_account.acceptance[0].email}"
}

resource "google_project_iam_member" "acceptance_reads_verify_logs" {
  count = local.wif_enabled

  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = "serviceAccount:${google_service_account.acceptance[0].email}"

  condition {
    title       = "swarm-verify log view only"
    description = "The swarm-verify view on _Default, which selects only the verification job's logs (verify_logs.tf)."
    expression  = "resource.name == \"${local.verify_log_view}\""
  }

  depends_on = [google_logging_log_view.verify]
}

resource "google_storage_bucket_iam_member" "acceptance_reads_release_records" {
  count = local.wif_enabled

  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.acceptance[0].email}"

  condition {
    title       = "releases/ only"
    description = "The release lanes' records (scripts/lib/release-order.sh), never a Terraform state object."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.state.name}/objects/releases/\")"
  }
}
