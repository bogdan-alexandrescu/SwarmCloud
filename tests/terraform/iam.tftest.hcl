# Read these assertions as the answer to "what could this component do if it
# were compromised?". The important ones are about what is ABSENT.

mock_provider "google" {}

variables {
  project_id      = "saga-agents-staging"
  artifact_bucket = "swarm-artifacts-saga-agents-staging"
  labels          = { "managed-by" = "swarm-terraform" }

  # The swarm's own Autopilot cluster. It has to be named: the container.* roles
  # are conditioned to it, and this project also holds a live agents-staging
  # cluster owned by another team.
  gke_cluster_name = "swarm-autopilot"
  gke_location     = "us-central1"
}

run "one_identity_per_component_and_no_static_keys" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  assert {
    condition = alltrue([
      for k, sa in google_service_account.platform : startswith(sa.account_id, "swarm-")
    ])
    error_message = "every platform service account carries the swarm- prefix"
  }

  assert {
    condition     = length(google_service_account.platform) == 4
    error_message = "four components, four identities: api, scheduler, quota-broker, reconciler"
  }

  assert {
    condition     = google_service_account.tick.account_id == "swarm-tick"
    error_message = "Cloud Scheduler and Pub/Sub push present their own identity, separate from every component"
  }

  # A downloadable key outlives the deployment, does not rotate, and cannot be
  # revoked without knowing every place it was copied to. There is no
  # google_service_account_key resource anywhere in this repository; this
  # asserts the module's own plan contains none.
  assert {
    condition     = length(output.service_account_emails) == 4
    error_message = "identity is attached to the workload by Cloud Run and GKE, never by a key file"
  }
}

run "the_api_holds_no_compute_permission_at_all" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  # swarm-api accepts a runner profile BY NAME and writes a Firestore document.
  # Everything that creates infrastructure is downstream of the scheduler
  # (CONTRACT.md invariant 10), so the API's roles must contain nothing from
  # run.*, container.* or compute.*.
  assert {
    condition = alltrue([
      for role in output.granted_roles["swarm-api"] :
      !strcontains(role, "roles/run.") && !strcontains(role, "roles/container.") && !strcontains(role, "roles/compute.")
    ])
    error_message = "swarm-api must hold no compute permission: it names a profile, it does not create infrastructure"
  }

  assert {
    condition = alltrue([
      for role in output.granted_roles["swarm-api"] :
      !strcontains(role, "swarmJobDispatcher") && !strcontains(role, "swarmJobReaper")
    ])
    error_message = "the API may neither dispatch nor reap executions"
  }

  assert {
    condition     = contains(output.granted_roles["swarm-api"], "roles/serviceusage.serviceUsageConsumer")
    error_message = "the API needs serviceUsageConsumer to send x-goog-user-project on Cloud Identity calls (CONTRACT.md, verified constraint)"
  }
}

run "only_the_reconciler_can_delete" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  assert {
    condition     = contains(google_project_iam_custom_role.job_reaper.permissions, "run.jobs.delete")
    error_message = "the reaper role is what makes deletion possible at all"
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.job_dispatcher.permissions, "run.jobs.delete")
    error_message = "the dispatcher may create and run Jobs; deleting them belongs to the reconciler alone"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.job_dispatcher.permissions :
      !strcontains(p, "setIamPolicy") && !strcontains(p, "delete")
    ])
    error_message = "no delete and no setIamPolicy in the dispatcher role, or it could grant itself anything"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.job_reaper.permissions :
      !strcontains(p, "setIamPolicy")
    ])
    error_message = "even the reaper may not rewrite an IAM policy"
  }

  assert {
    condition     = contains(output.granted_roles["swarm-reconciler"], output.job_reaper_role_id)
    error_message = "the reaper role is attached to the reconciler and to nothing else"
  }

  assert {
    condition = length([
      for account, roles in output.granted_roles : account
      if contains(roles, output.job_reaper_role_id)
    ]) == 1
    error_message = "exactly one identity may delete executions"
  }
}

run "firestore_access_is_pinned_to_the_swarm_database" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  # The project is SHARED. An unconditioned roles/datastore.user is read-write
  # over every other team's Firestore data in the project.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.firestore :
      length(b.condition) == 1 && strcontains(b.condition[0].expression, "databases/swarm")
    ])
    error_message = "every datastore.user grant must be conditioned to the swarm database"
  }

  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.firestore :
      !strcontains(b.condition[0].expression, "(default)")
    ])
    error_message = "no swarm identity may reach the (default) database"
  }
}

run "gke_dispatch_cannot_reach_another_teams_cluster" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  # This project is SHARED and holds a live `agents-staging` cluster owned by
  # another team. A project-level container.jobs.create covers EVERY cluster in
  # the project, so an unconditioned binding here would let swarm-scheduler
  # create workloads in their production cluster and read its Pod logs, and
  # swarm-reconciler delete their Jobs and Pods.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.gke :
      length(b.condition) == 1 && strcontains(b.condition[0].expression, "clusters/swarm-autopilot")
    ])
    error_message = "every container.* grant must be conditioned to the swarm cluster; project-level covers agents-staging too"
  }

  assert {
    # NOTE: match on "clusters/agents-staging", not "agents-staging" -- the bare
    # substring also appears inside the project id saga-agents-staging, which made
    # this assertion fail against a perfectly correct condition.
    condition     = !strcontains(output.gke_role_condition, "clusters/agents-staging")
    error_message = "the condition must name the swarm cluster, never another team's"
  }

  # Only the two components that dispatch and reap hold a container role, and
  # they hold different ones.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.gke : contains(["swarm-scheduler", "swarm-reconciler"], k)
    ])
    error_message = "only the dispatcher and the reconciler may touch the cluster"
  }

  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.plain : !strcontains(b.role, "swarmGke")
    ])
    error_message = "a container.* role must never ride along in the unconditioned bindings"
  }

  assert {
    condition     = contains(output.granted_roles["swarm-scheduler"], output.job_dispatcher_role_id)
    error_message = "granted_roles must still report every project role, conditioned ones included"
  }
}

run "an_unnamed_cluster_with_scoping_on_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    gke_enabled          = true
    scope_gke_to_cluster = true
    gke_cluster_name     = ""
  }

  # Fail-closed: without a cluster name there is nothing to pin the grant to,
  # and a silently unconditioned container.* binding is the failure mode this
  # whole condition exists to prevent.
  expect_failures = [google_project_iam_member.gke]
}

run "the_gke_roles_disappear_entirely_when_the_cluster_does" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    gke_enabled = false
  }

  assert {
    condition     = length(google_project_iam_member.gke) == 0
    error_message = "with Autopilot off, nothing in the swarm holds a container.* permission at all"
  }

  assert {
    condition = alltrue([
      for account, roles in output.granted_roles : alltrue([
        for role in roles : !strcontains(role, "swarmGke")
      ])
    ])
    error_message = "a disabled backend must leave no standing grant behind"
  }
}

run "the_api_reads_artifacts_but_cannot_write_them" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_reader.role == "roles/storage.objectViewer"
    error_message = "the worker produces artifacts; the API only hands them over"
  }

  assert {
    condition     = google_storage_bucket_iam_member.reconciler_admin.role == "roles/storage.objectAdmin"
    error_message = "the reconciler cleans up artifacts of tasks abandoned before any lifecycle rule reaches them"
  }
}

run "custom_role_ids_accept_only_legal_characters" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    custom_role_suffix = "not-legal"
  }

  expect_failures = [var.custom_role_suffix]
}
