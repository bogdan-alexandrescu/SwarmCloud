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
#     with exactly those permissions, conditioned to those job names. Not the
#     merge Job, not any other job, not create/update/delete: it cannot change
#     what a job runs, only run it (warm-jobs.sh overrides the arguments to
#     `--self-test`, which is why runWithOverrides is in it).
#   * LIST jobs and read operations, unconditioned: warm-jobs.sh lists the
#     worker jobs by label, and a list is checked against the location, which
#     no job-name condition matches. Read-only; it shows job definitions, which
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
# WHAT A MOCK PROVIDER CANNOT PROVE: that IAM evaluates the conditions below
# the way Google documents them for Cloud Run and Cloud Storage resource names.
# The first accept.yml run after `make bootstrap` is that proof; a condition
# that matches nothing shows there as `Permission 'run.jobs.runWithOverrides'
# denied on resource ...` in `warm, smoke and GKE proof (dev)`.
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

  # The jobs it may execute. `swarm-verify` is the literal terraform/infra/
  # verify.tf names its job; worker jobs are `<prefix>-job-<tenant>-<profile>`
  # (terraform/infra/locals.tf, job_matrix). An execution's name is the job's
  # with `/executions/<id>` after it, so the job part is extracted and
  # compared rather than the whole name.
  accept_job_condition = join(" || ", [
    "resource.name.endsWith(\"/jobs/swarm-verify\")",
    "resource.name.extract(\"/jobs/{job}\").startsWith(\"swarm-verify/\")",
    "resource.name.extract(\"/jobs/{job}\").startsWith(\"${var.name_prefix}-job-\")",
  ])
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
  description = "managed-by=swarm-terraform; start an execution of a named job and read it. No create, update, delete or IAM. Granted to the acceptance account, conditioned to swarm-verify and the worker jobs (terraform/bootstrap/acceptance.tf)."
  stage       = "GA"

  permissions = [
    "run.jobs.get",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.executions.get",
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

resource "google_project_iam_member" "acceptance_runs_jobs" {
  count = local.wif_enabled

  project = var.project_id
  role    = google_project_iam_custom_role.acceptance_runner[0].name
  member  = "serviceAccount:${google_service_account.acceptance[0].email}"

  condition {
    title       = "swarm-verify and worker jobs only"
    description = "Executions of swarm-verify and the per-tenant worker jobs, for accept.yml. Owner decision 2026-10-08."
    expression  = local.accept_job_condition
  }
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
