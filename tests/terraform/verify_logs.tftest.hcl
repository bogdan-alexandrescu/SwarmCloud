# swarm-verify's transcript, readable by the identity that runs the release.
#
# The release runs scripts/verify-remote.sh as swarm-tf-deployer, and on a
# failed gate that script prints the execution's own log -- which the deployer
# could not read: its only logging role, roles/logging.configWriter, carries no
# permission to read an entry. terraform/bootstrap/verify_logs.tf copies the
# job's stdout/stderr into a log bucket of its own and grants the deployer that
# bucket's _AllLogs view, and nothing wider.
#
# What these runs hold it to is the PROPERTY, not the spelling: the view the
# grant names is the view of the bucket the sink writes into, the sink selects
# the verification job and not its neighbours, and the deployer gains no
# project-wide log read along the way. The half of the property that lives in
# the script -- that verify-remote.sh reads through exactly this view and sees
# the transcript -- is tests/integration/test_verify_remote_prints_the_job_log.py,
# which reads the grant and the sink filter out of the same file.

mock_provider "google" {}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]
}

run "the_deployer_may_read_the_verify_transcript_and_nothing_wider" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  assert {
    condition     = google_logging_project_sink.verify.destination == "logging.googleapis.com/projects/${var.project_id}/locations/${google_logging_project_bucket_config.verify.location}/buckets/${google_logging_project_bucket_config.verify.bucket_id}"
    error_message = "the sink must write into the bucket this root creates for it, or the view the deployer may read stays empty"
  }

  assert {
    condition     = google_project_iam_member.deployer_reads_verify_logs[0].role == "roles/logging.viewAccessor"
    error_message = "reading one view is roles/logging.viewAccessor; roles/logging.viewer would read every log in this SHARED project"
  }

  # The grant is on the _AllLogs view of THAT bucket -- the view Logging creates
  # for every bucket -- and on nothing else.
  assert {
    condition     = google_project_iam_member.deployer_reads_verify_logs[0].condition[0].expression == "resource.name == \"projects/${var.project_id}/locations/${google_logging_project_bucket_config.verify.location}/buckets/${google_logging_project_bucket_config.verify.bucket_id}/views/_AllLogs\""
    error_message = "the deployer's log read must be conditioned on exactly the _AllLogs view of the bucket the sink writes into"
  }

  assert {
    condition = alltrue([
      for clause in [
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"swarm-verify\"",
        "logName:\"run.googleapis.com%2F\"",
      ] : strcontains(google_logging_project_sink.verify.filter, clause)
    ])
    error_message = "the sink must select the swarm-verify job's own stdout/stderr: without job_name it copies every Cloud Run job's transcript (tenant workers' included) into a bucket CI can read, and without the logName clause it copies the job's audit events"
  }

  # A disjunction would widen what the sink copies past the three clauses above.
  assert {
    condition     = !strcontains(google_logging_project_sink.verify.filter, " OR ") && !strcontains(google_logging_project_sink.verify.filter, "NOT ")
    error_message = "the sink filter is a conjunction; an OR or a NOT can route logs the three clauses were written to keep out"
  }

  assert {
    condition = alltrue([
      for r in keys(google_project_iam_member.deployer_roles) :
      !contains(["roles/logging.viewer", "roles/logging.privateLogViewer", "roles/logging.admin"], r)
    ]) && length(google_project_iam_member.deployer_roles) > 0
    error_message = "the deployer must not hold a project-wide log read: this project is SHARED, and its logs include the other team's"
  }
}

run "no_deployer_means_no_grant_and_the_transcript_is_still_routed" {
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
    condition     = google_logging_project_bucket_config.verify.bucket_id == "swarm-verify-logs" && google_logging_project_bucket_config.verify.location == "global"
    error_message = "the bucket verify-remote.sh reads is created with or without CI"
  }
}
