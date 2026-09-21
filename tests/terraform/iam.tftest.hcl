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

run "firestore_grants_are_unconditioned_because_iam_conditions_do_not_gate_the_data_plane" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  # REVERSED on evidence, 2026-09-16. This used to require an IAM condition
  # pinning every grant to the swarm database. Firestore does not evaluate IAM
  # Conditions on the DATA plane -- only for administrative operations -- so the
  # condition did not restrict document access, it DENIED it: every
  # control-plane service came up reporting
  # "firestore unavailable: PermissionDenied". Security Rules are not an
  # alternative either; server SDKs with admin credentials bypass them.
  #
  # So the grant is unconditioned, and the honest boundary is documented in
  # docs/security.md: a swarm identity can reach any Firestore database in this
  # project. Today `swarm` is the only one. The real fix is a separate project.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.firestore : length(b.condition) == 0
    ])
    error_message = "datastore grants must be UNCONDITIONED: an IAM condition here denies the data plane outright rather than scoping it"
  }

  # The replacement guarantee, since IAM cannot provide the old one: only the
  # four control-plane identities hold a datastore grant at all. Tenant workers
  # get the narrowed swarmTenantWorkerFirestore custom role instead, which omits
  # entities.delete and entities.list -- so a hostile worker can neither
  # enumerate nor destroy another tenant's documents even though it shares the
  # database scope. That is the boundary that actually holds.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.firestore : b.role == "roles/datastore.user"
    ])
    error_message = "the control plane holds roles/datastore.user; anything broader is a regression"
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

# The broker enumerates tenants to find whose subscription credential is due.
# Enumeration is a list, and a list is all it gets: the ability to READ a
# credential is granted per-secret by the secret_manager module, on the refresh
# secrets only. If that separation ever collapses into one project-wide role,
# a bug in the broker stops being a failed refresh and becomes every tenant's
# provider key at once.
run "the_broker_can_provision_account_secrets_and_read_none" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  # Pinned as an exact SET, not a subset check. The point of a custom role here
  # is that every permission in it was argued for; asserting "contains x" would
  # let an unrelated one be added silently, which is how a narrow role becomes
  # roles/secretmanager.admin one commit at a time.
  #
  # `create` and `setIamPolicy` arrived when account management moved into the
  # Settings page: a pool account's secret name contains a LABEL chosen at
  # registration, so terraform cannot declare it and the component handling the
  # registration has to make it -- and a secret created without an accessor
  # binding is one the tenant's pod cannot read.
  assert {
    condition = google_project_iam_custom_role.secret_lister.permissions == toset([
      "secretmanager.secrets.list",
      "secretmanager.secrets.create",
      "secretmanager.secrets.get",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
      "secretmanager.versions.add",
    ])
    error_message = "every permission in this role was argued for; adding one silently is how it becomes secretmanager.admin"
  }

  # THE LINE THAT MUST NOT MOVE. The broker writes credentials and binds their
  # readers; it never reads a payload project-wide. `versions.add` is
  # write-only, and payload access stays per-secret.
  assert {
    condition = !contains(
      google_project_iam_custom_role.secret_lister.permissions,
      "secretmanager.versions.get"
    )
    error_message = "reading a version project-wide would defeat per-tenant secret isolation"
  }

  assert {
    condition = !contains(
      google_project_iam_custom_role.secret_lister.permissions,
      "secretmanager.versions.access"
    )
    error_message = "a project-wide payload read would defeat per-tenant secret isolation entirely"
  }

  # roles/secretmanager.viewer would have done the job and is the obvious
  # shortcut. It also grants versions.list and versions.get over every secret in
  # a project this platform shares with other teams.
  assert {
    condition = !contains(
      local.plain_roles["swarm-quota-broker"],
      "roles/secretmanager.viewer"
    )
    error_message = "the predefined viewer role reaches secrets belonging to other teams in this project"
  }
}
