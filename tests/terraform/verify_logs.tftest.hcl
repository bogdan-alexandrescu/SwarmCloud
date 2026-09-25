# swarm-verify's logs, readable by the identity that runs the release, and no
# other log -- through the grant this root makes.
#
# OWNER DECISION 2026-09-24: the release's deployer (swarm-tf-deployer) may read
# the swarm-verify job's logs ONLY, so scripts/verify-remote.sh can print why an
# in-VPC check failed. terraform/bootstrap/verify_logs.tf puts a log view on the
# project's _Default bucket, filtered to that one job, and grants the deployer
# roles/logging.viewAccessor with a condition naming that view.
#
# What these runs hold it to is the PROPERTY, not the spelling:
#
#   * the view is on the bucket the job's stdout/stderr already land in, and it
#     cannot select past the job: every job it names is swarm-verify, and it
#     has no disjunction or negation that could admit anything else;
#   * the grant names exactly the view this root creates, and nothing wider;
#   * no project role this root grants the deployer is one MEASURED to read log
#     entries (terraform/bootstrap/log-reading-roles.json: 50 of the 2,397
#     predefined roles on 2026-09-24), and var.deployer_roles refuses both a
#     measured reader and any role nobody has reviewed.
#
# WHAT THEY DO NOT HOLD IT TO. The last point is about the roles this root
# GRANTS, not about what the deployer can REACH. It holds roles that let it get
# a log read another way -- iam.roleAdmin, iam.serviceAccountAdmin, an actAs on
# a service account holding roles/editor, and log sinks -- and verify_logs.tf,
# "WHAT THIS DOES NOT BOUND", lists them. No assertion here covers them.
#
# The measured list is a denylist as of its date: a role Google changes after it
# is caught by the reviewed-roles allowlist, not by the file.
#
# The half of the property that lives in the script -- that verify-remote.sh
# reads through exactly this view and sees the transcript -- is
# tests/integration/test_verify_remote_prints_the_job_log.py, which reads the
# grant and the view's filter out of the same file.
#
# A mock provider proves the configuration says what was meant. It never proves
# that Cloud Logging accepts the filter or that IAM evaluates the condition the
# way Google documents it; verify_logs.tf says what would show each.
#
# ORDER MATTERS FOR PROVING THESE RED. A run that ERRORS -- "Missing expected
# failure" is an error, a failed assertion is not -- skips every run after it in
# this file. So the runs that only assert come first, and the expect_failures
# runs last; each of those can be shown red only in a CI run of its own.

mock_provider "google" {}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]
}

run "the_deployer_may_read_the_verify_job_s_logs_and_nothing_wider" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  # _Default is where Cloud Run already writes the job's stdout and stderr. A
  # view on any other bucket would be a view of nothing.
  assert {
    condition     = google_logging_log_view.verify.bucket == "projects/${var.project_id}/locations/global/buckets/_Default"
    error_message = "the view must be on this project's _Default bucket (location global), which is where the job's stdout/stderr land; the owner decided on a view there rather than a copy"
  }

  assert {
    condition = alltrue([
      for clause in ["resource.type=\"cloud_run_job\"", "resource.labels.job_name=\"swarm-verify\""] :
      contains([for c in split(" AND ", google_logging_log_view.verify.filter) : trimspace(c)], clause)
    ])
    error_message = "the view's filter must AND together resource.type=\"cloud_run_job\" and resource.labels.job_name=\"swarm-verify\": without the job clause the deployer reads every Cloud Run job's transcript, tenant workers' included"
  }

  # Every job the filter mentions is swarm-verify. A second job_name clause
  # naming anything else could only matter if something widened it.
  assert {
    condition     = length(regexall("job_name", google_logging_log_view.verify.filter)) == length(regexall("job_name=\"swarm-verify\"", google_logging_log_view.verify.filter))
    error_message = "every job the view's filter names must be swarm-verify"
  }

  # A conjunction can only narrow. OR, NOT, and the `-` prefix (Logging's other
  # spelling of NOT) are the three ways a filter can admit what its clauses
  # were written to keep out.
  assert {
    condition     = length(regexall("(?i)(^|[\\s(])(OR|NOT)([\\s(]|$)|(^|[\\s(])-[a-z]", google_logging_log_view.verify.filter)) == 0
    error_message = "the view's filter must be a plain conjunction; an OR or a negation can admit other jobs' logs"
  }

  assert {
    condition     = google_project_iam_member.deployer_reads_verify_logs[0].role == "roles/logging.viewAccessor"
    error_message = "reading one view is roles/logging.viewAccessor; roles/logging.viewer would read every log in this SHARED project"
  }

  # The grant names the view THIS root creates -- built from the view's own
  # bucket and id, so a rename on either side fails here.
  assert {
    condition     = google_project_iam_member.deployer_reads_verify_logs[0].condition[0].expression == "resource.name == \"${google_logging_log_view.verify.bucket}/views/${google_logging_log_view.verify.name}\""
    error_message = "the deployer's log read must be conditioned on exactly the view this root creates, and on nothing else"
  }

  # Every predefined project role this root grants the deployer, against every
  # role measured to read log entries. The measured file is read, not restated:
  # the validation on var.deployer_roles reads the same one.
  assert {
    condition = length(setintersection(
      toset(concat(
        keys(google_project_iam_member.deployer_roles),
        [google_project_iam_member.deployer_storage[0].role],
      )),
      toset(keys(jsondecode(file("../../terraform/bootstrap/log-reading-roles.json")).roles)),
    )) == 0 && length(google_project_iam_member.deployer_roles) > 0
    error_message = "the deployer holds a predefined role measured to read log entries project-wide (terraform/bootstrap/log-reading-roles.json): this project is SHARED, and its logs include the other team's"
  }

  # The two custom roles the deployer holds must not smuggle one in either.
  assert {
    condition = alltrue([
      for p in concat(
        tolist(google_project_iam_custom_role.secret_provisioner[0].permissions),
        tolist(google_project_iam_custom_role.deployer_project_buckets[0].permissions),
      ) : !startswith(p, "logging.")
    ])
    error_message = "a custom role on the deployer carries a logging permission; its one log read is the conditioned grant in verify_logs.tf"
  }

  # Unlabelable, so the id carries the platform prefix -- how the guards
  # recognise an unlabelable resource as ours. `swarm` is name_prefix's default
  # and the prefix its validation requires.
  assert {
    condition     = startswith(google_logging_log_view.verify.name, "swarm-")
    error_message = "the view's id must begin with the platform prefix; it cannot carry a managed-by label"
  }
}

run "no_deployer_means_no_grant_and_the_view_still_exists" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = false
  }

  assert {
    condition     = length(google_project_iam_member.deployer_reads_verify_logs) == 0
    error_message = "with no CI identity there is nobody to grant the view to"
  }

  # An operator running verify-remote.sh from a workstation reads through the
  # same view, so it exists whether or not CI does.
  assert {
    condition     = google_logging_log_view.verify.bucket == "projects/${var.project_id}/locations/global/buckets/_Default"
    error_message = "the view verify-remote.sh reads is created with or without CI"
  }
}

# ---------------------------------------------------------------------------
# The refusals on var.deployer_roles: not only this root's defaults, but a role
# added to terraform.tfvars by hand is refused at plan.
# ---------------------------------------------------------------------------

# The tempting shortcut: the same role as verify_logs.tf, minus the condition.
run "an_unconditioned_view_accessor_for_the_deployer_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles    = ["roles/run.admin", "roles/logging.viewAccessor"]
  }

  expect_failures = [var.deployer_roles]
}

run "a_project_wide_log_read_for_the_deployer_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles    = ["roles/run.admin", "roles/logging.viewer"]
  }

  expect_failures = [var.deployer_roles]
}

# A reader that is not a logging role. roles/iam.securityReviewer carries
# logging.logEntries.list AND logging.privateLogEntries.list -- every log in
# the project, Data Access audit logs included -- and the first version of the
# refusal, a list of five logging role names, let it through. The plausible
# route in is "CI should be able to audit IAM".
run "a_log_reader_that_is_not_a_logging_role_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles    = ["roles/run.admin", "roles/iam.securityReviewer"]
  }

  expect_failures = [var.deployer_roles]
}

# A role nobody reviewed. roles/cloudsql.admin reads no log (it is not in the
# measured file), so only the reviewed-roles allowlist refuses it -- which is
# the point: the measured file is only as current as its date.
run "a_role_nobody_reviewed_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles    = ["roles/run.admin", "roles/cloudsql.admin"]
  }

  expect_failures = [var.deployer_roles]
}
