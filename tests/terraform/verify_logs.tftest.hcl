# swarm-verify's logs, readable by the identity that runs the release, and no
# other log.
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
#   * nothing in this root gives the deployer a project-wide log read, and the
#     variable listing its project roles refuses one.
#
# The half of the property that lives in the script -- that verify-remote.sh
# reads through exactly this view and sees the transcript -- is
# tests/integration/test_verify_remote_prints_the_job_log.py, which reads the
# grant and the view's filter out of the same file.
#
# A mock provider proves the configuration says what was meant. It never proves
# that Cloud Logging accepts the filter or that IAM evaluates the condition the
# way Google documents it; verify_logs.tf says what would show each.

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

  assert {
    condition = length(setintersection(toset(keys(google_project_iam_member.deployer_roles)), toset([
      "roles/logging.admin",
      "roles/logging.privateLogViewer",
      "roles/logging.viewAccessor",
      "roles/logging.viewer",
      "roles/viewer",
    ]))) == 0 && length(google_project_iam_member.deployer_roles) > 0
    error_message = "the deployer must not hold a project-wide log read: this project is SHARED, and its logs include the other team's"
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

# The validation on deployer_roles, not only this root's defaults: a project-wide
# log read added to terraform.tfvars by hand is refused at plan.
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
