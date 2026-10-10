# swarm-workspace-dispatch: how an approval starts the workspace job under
# option (ii) of docs/workspaces.md §2.1 (terraform/bootstrap/
# workspace_dispatch.tf and workflows/workspace-apply.yaml; lane W4b of #847).
#
# What these runs hold:
#
#   * nothing exists until the owner switches it on;
#   * the dispatch account has an EMPTY account-level policy, written
#     authoritatively, so nobody may act as it or mint its token there;
#   * its grants are the run role ON THE JOB (the job's policy names it alone)
#     and roles/workflows.invoker at the project, which W0b (7) found cannot be
#     granted narrower, and nothing else;
#   * the workflow runs as it, logs no call, carries managed-by, and its source
#     checks the id against ^w-[0-9a-f]{6}$ and the mode against create|limits
#     before calling jobs.run on swarm-workspace-apply with those two values as
#     the arguments and no other override;
#   * the Eventarc trigger reads the existing topic, delivers to that workflow,
#     runs as the dispatch account and carries managed-by.
#
# Each assertion fails if its resource is removed or widened. A mock provider
# proves the configuration, not what Eventarc or Workflows do with it live.

mock_provider "google" {
  mock_data "google_iam_policy" {
    defaults = {
      policy_data = "{}"
    }
  }
}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]

  image_digest = "sha256:${join("", [for i in range(8) : "4567cdef"])}"
}

run "nothing_is_dispatched_until_the_owner_switches_it_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      length(google_service_account.workspace_dispatch) == 0,
      length(google_service_account_iam_policy.workspace_dispatch) == 0,
      length(google_project_iam_member.workspace_dispatch_invoker) == 0,
      length(google_workflows_workflow.workspace_apply) == 0,
      length(google_eventarc_trigger.workspace_apply) == 0,
    ])
    error_message = "with enable_workspace_deployer false the bootstrap creates no part of the dispatch path"
  }
}

run "the_dispatch_path_forwards_a_checked_id_and_mode_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_resource {
    target          = google_service_account.workspace_dispatch
    override_during = plan
    values = {
      id    = "projects/saga-agents-staging/serviceAccounts/swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com"
      email = "swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_pubsub_topic.workspace_apply
    override_during = plan
    values = {
      id = "projects/saga-agents-staging/topics/swarm-workspace-apply"
    }
  }

  override_resource {
    target          = google_workflows_workflow.workspace_apply
    override_during = plan
    values = {
      id = "projects/saga-agents-staging/locations/us-central1/workflows/swarm-workspace-apply"
    }
  }

  # Distinct strings, so pointing a policy resource at another data source
  # fails the equality below.
  override_data {
    target = data.google_iam_policy.workspace_dispatch_nobody
    values = {
      policy_data = "{\"dispatch\":\"nobody\"}"
    }
  }

  override_data {
    target = data.google_iam_policy.workspace_apply_job
    values = {
      policy_data = "{\"job\":\"dispatcher-only\"}"
    }
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  # ---- the account ----

  assert {
    condition     = google_service_account.workspace_dispatch[0].account_id == "swarm-workspace-dispatch" && startswith(google_service_account.workspace_dispatch[0].description, "managed-by=swarm-terraform;")
    error_message = "the dispatch account is swarm-workspace-dispatch, the name the job's policy and the alerts expect"
  }

  assert {
    condition     = length(data.google_iam_policy.workspace_dispatch_nobody.binding) == 0 && google_service_account_iam_policy.workspace_dispatch[0].policy_data == "{\"dispatch\":\"nobody\"}"
    error_message = "swarm-workspace-dispatch's own IAM policy must be written authoritatively EMPTY: a member there could start the job as the dispatcher, past the foreign_run alert"
  }

  # ---- its two grants ----

  assert {
    condition = alltrue([
      length(data.google_iam_policy.workspace_apply_job.binding) == 1,
      toset([for b in data.google_iam_policy.workspace_apply_job.binding : b.role]) == toset(["roles/run.jobsExecutorWithOverrides"]),
      toset(flatten([for b in data.google_iam_policy.workspace_apply_job.binding : tolist(b.members)])) == toset([google_project_iam_member.workspace_dispatch_invoker[0].member]),
      google_cloud_run_v2_job_iam_policy.workspace_apply[0].policy_data == "{\"job\":\"dispatcher-only\"}",
    ])
    error_message = "the dispatcher's run role is on the job only, in the job's authoritative policy, and that policy names the dispatcher alone"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.workspace_dispatch_invoker[0].role == "roles/workflows.invoker",
      google_project_iam_member.workspace_dispatch_invoker[0].member == "serviceAccount:swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.workspace_dispatch_invoker[0].project == "saga-agents-staging",
      length(google_project_iam_member.workspace_dispatch_invoker[0].condition) == 0,
    ])
    error_message = "roles/workflows.invoker goes to swarm-workspace-dispatch, the Eventarc trigger's identity, and nobody else; at the project, because Workflows accepts nothing narrower (W0b (7))"
  }

  # ---- the workflow ----

  assert {
    condition = alltrue([
      google_workflows_workflow.workspace_apply[0].name == "swarm-workspace-apply",
      google_workflows_workflow.workspace_apply[0].region == "us-central1",
      google_workflows_workflow.workspace_apply[0].service_account == google_service_account.workspace_dispatch[0].id,
      google_workflows_workflow.workspace_apply[0].call_log_level == "LOG_NONE",
      google_workflows_workflow.workspace_apply[0].labels["managed-by"] == "swarm-terraform",
      google_workflows_workflow.workspace_apply[0].source_contents == file("../../terraform/bootstrap/workflows/workspace-apply.yaml"),
    ])
    error_message = "the workflow is swarm-workspace-apply in the platform's region, runs as swarm-workspace-dispatch, logs no call (§2.6 item 4), carries managed-by and deploys the reviewed source file"
  }

  # The id regex, exactly: widening it is how a malformed id would reach the
  # job's arguments.
  assert {
    condition = alltrue([
      strcontains(google_workflows_workflow.workspace_apply[0].source_contents, "text.match_regex(workspace_id, \"^w-[0-9a-f]{6}$\")"),
      length(regexall("match_regex\\(workspace_id, ", google_workflows_workflow.workspace_apply[0].source_contents)) == 1,
      strcontains(google_workflows_workflow.workspace_apply[0].source_contents, "get_type(workspace_id) != \"string\""),
    ])
    error_message = "the workflow must check workspace_id is a string matching ^w-[0-9a-f]{6}$, with that regex and no other, before it calls the job"
  }

  assert {
    condition     = strcontains(google_workflows_workflow.workspace_apply[0].source_contents, "text.match_regex(mode, \"^(create|limits)$\")") && length(regexall("match_regex\\(mode, ", google_workflows_workflow.workspace_apply[0].source_contents)) == 1
    error_message = "the workflow must check mode against ^(create|limits)$ and nothing wider"
  }

  # The call: jobs.run on this job, the two values as the arguments, and no
  # other override a caller could steer.
  assert {
    condition = alltrue([
      length(regexall("call: googleapis[.]run[.]v2[.]projects[.]locations[.]jobs[.]run\\n", google_workflows_workflow.workspace_apply[0].source_contents)) == 1,
      length(regexall("call: ", google_workflows_workflow.workspace_apply[0].source_contents)) == 1,
      strcontains(google_workflows_workflow.workspace_apply[0].source_contents, "\"/jobs/swarm-workspace-apply\"}"),
      length(regexall("containerOverrides:\\n +- args:\\n +- \\$\\{workspace_id\\}\\n +- \\$\\{mode\\}\\n", google_workflows_workflow.workspace_apply[0].source_contents)) == 1,
      length(regexall("(?m)^\\s*(env|taskCount|timeout|clearArgs|name: \\$\\{event)\\b", google_workflows_workflow.workspace_apply[0].source_contents)) == 0,
      strcontains(google_workflows_workflow.workspace_apply[0].source_contents, "skip_polling: true"),
    ])
    error_message = "the workflow makes one call, jobs.run on swarm-workspace-apply, overriding only the arguments, as [workspace_id, mode], with no environment, task count or timeout override and no job name read from the event"
  }

  # ---- the Eventarc trigger ----

  assert {
    condition = alltrue([
      google_eventarc_trigger.workspace_apply[0].name == "swarm-workspace-apply",
      google_eventarc_trigger.workspace_apply[0].location == "us-central1",
      google_eventarc_trigger.workspace_apply[0].service_account == "swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com",
      google_eventarc_trigger.workspace_apply[0].transport[0].pubsub[0].topic == "projects/saga-agents-staging/topics/swarm-workspace-apply",
      google_eventarc_trigger.workspace_apply[0].destination[0].workflow == "projects/saga-agents-staging/locations/us-central1/workflows/swarm-workspace-apply",
      length(google_eventarc_trigger.workspace_apply[0].destination[0].cloud_run_service) == 0,
      length(google_eventarc_trigger.workspace_apply[0].destination[0].http_endpoint) == 0,
      google_eventarc_trigger.workspace_apply[0].labels["managed-by"] == "swarm-terraform",
    ])
    error_message = "the Eventarc trigger reads the swarm-workspace-apply topic, delivers to the swarm-workspace-apply workflow and nowhere else, runs as swarm-workspace-dispatch and carries managed-by"
  }

  assert {
    condition = (
      length(google_eventarc_trigger.workspace_apply[0].matching_criteria) == 1
      && alltrue([for m in google_eventarc_trigger.workspace_apply[0].matching_criteria : m.attribute == "type" && m.value == "google.cloud.pubsub.topic.v1.messagePublished"])
    )
    error_message = "the trigger matches Pub/Sub's messagePublished event and nothing else"
  }

  # swarm-api stays the topic's only publisher: option (ii) changes no
  # swarm-api grant.
  assert {
    condition     = google_pubsub_topic_iam_binding.workspace_apply_publisher[0].members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "swarm-api keeps only pubsub.publisher on the topic, and is its only publisher"
  }
}
