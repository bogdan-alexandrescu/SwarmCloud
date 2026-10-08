# swarm-workspace-deployer, its one trigger, its logs and its alerts
# (terraform/bootstrap/workspace_deployer.tf, terraform/modules/monitoring/
# workspace_alerts.tf; docs/workspaces.md §2.1-2.6, lane W4 of #847).
#
# What these runs hold, as properties rather than spellings:
#
#   * nothing exists until the owner switches it on;
#   * the identity has no key resource and an EMPTY account-level policy, so
#     nobody may act as it or mint its token there;
#   * the trigger runs as that identity, is fed by the topic, builds
#     refs/heads/main only and reads its build file from refs/heads/main, has no
#     push, pull-request or webhook event that could build another ref, passes
#     the message's two fields and the pinned image and nothing else, and
#     filters malformed messages out;
#   * its roles are exactly §2.3's list (on the WD9 fallback) plus the image
#     pull; every grant is to the deployer; the five §2.3 leaves unconditioned
#     are the only unconditioned ones; projectIamAdmin's hasOnly() admits the
#     personal worker's three project roles and nothing else; the secret,
#     object and cluster grants are bounded to swarm-tenant-u-, tenants/ and
#     the swarm cluster; no custom role carries delete, disable, key, token,
#     signing or actAs power;
#   * swarm-api is the topic's only publisher;
#   * the job's build log is routed, by the trigger's id, to a restricted
#     bucket and excluded from _Default by the same filter, and its readers are
#     granted that bucket's view and nothing wider;
#   * the three alerts watch the very account and trigger the bootstrap makes;
#   * and the refusals: an unreviewed role, a mutable image, a missing
#     repository, the slot creator without the slots, a log reader who is not a
#     person or a group.
#
# A mock provider proves the configuration says what was meant. It does not
# prove what Cloud Build, Cloud Logging or IAM do with it live; the "NOT
# VERIFIED LIVE" lists in both files say what the first workspace settles.
#
# ORDER: the runs that only assert come first, the expect_failures runs last,
# because a run that ERRORS skips every run after it in this file.

mock_provider "google" {
  # The identity's empty policy is rendered by this data source; left to the
  # mock it is a random string the policy resource cannot parse.
  mock_data "google_iam_policy" {
    defaults = {
      policy_data = "{}"
    }
  }
}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]

  # Built from the parts so no line holds a 64-hex literal on its own.
  builder_digest = "sha256:${join("", [for i in range(8) : "0123abcd"])}"
}

run "nothing_exists_until_the_owner_switches_it_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      length(google_service_account.workspace_deployer) == 0,
      length(google_cloudbuild_trigger.workspace_apply) == 0,
      length(google_pubsub_topic.workspace_apply) == 0,
      length(google_project_iam_member.workspace_deployer) == 0,
      length(google_project_iam_custom_role.workspace) == 0,
      length(google_logging_project_sink.workspace_apply) == 0,
      length(google_logging_project_exclusion.workspace_apply) == 0,
      length(google_project_iam_member.forge_personal_slots) == 0,
    ])
    error_message = "with enable_workspace_deployer at its default (false) the bootstrap must create no part of the workspace job: the owner's one-time steps come first (docs/workspaces.md §10)"
  }

  assert {
    condition     = output.workspace_deployer == null
    error_message = "the workspace_deployer output is null while the job is off"
  }

  # The default role list IS §2.3's table on the WD9 fallback, plus the pull.
  assert {
    condition = toset(var.workspace_deployer_roles) == toset([
      "swarmWorkspaceAccountAdmin",
      "swarmWorkspaceProjectReader",
      "roles/resourcemanager.projectIamAdmin",
      "swarmWorkspaceBucketIam",
      "roles/storage.objectCreator",
      "swarmWorkspaceSecretBinder",
      "swarmForgeSlotCreator",
      "swarmWorkspaceFirestore",
      "roles/container.clusterViewer",
      "roles/logging.logWriter",
      "swarmImagePuller",
    ])
    error_message = "workspace_deployer_roles' default must be exactly docs/workspaces.md §2.3's list on the WD9 fallback, plus swarmImagePuller"
  }
}

run "the_identity_is_usable_only_by_its_trigger_on_main" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer     = true
    enable_forge_user_slots       = true
    workspace_apply_repository    = "projects/saga-agents-staging/locations/us-central1/connections/github/repositories/SwarmCloud"
    workspace_apply_builder_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.builder_digest}"
    workspace_log_readers         = ["group:swarm-admins@saga.xyz"]
  }

  override_resource {
    target          = google_service_account.workspace_deployer
    override_during = plan
    values = {
      id    = "projects/saga-agents-staging/serviceAccounts/swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com"
      email = "swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com"
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
    target          = google_cloudbuild_trigger.workspace_apply
    override_during = plan
    values = {
      trigger_id = "0f0f0f0f-1111-2222-3333-444444444444"
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

  assert {
    condition     = google_service_account.workspace_deployer[0].account_id == "swarm-workspace-deployer"
    error_message = "the identity is swarm-workspace-deployer, the name the build file, the guard and the alerts all expect"
  }

  # Its account-level policy is written, authoritatively, with no binding: no
  # actAs, no token creator, no workloadIdentityUser for anyone.
  assert {
    condition     = length(data.google_iam_policy.workspace_deployer_nobody.binding) == 0 && google_service_account_iam_policy.workspace_deployer[0].policy_data == data.google_iam_policy.workspace_deployer_nobody.policy_data
    error_message = "swarm-workspace-deployer's own IAM policy must be written authoritatively EMPTY: any member there could act as the identity outside its trigger (docs/workspaces.md §2.4, safeguard 1)"
  }

  assert {
    condition     = google_cloudbuild_trigger.workspace_apply[0].service_account == google_service_account.workspace_deployer[0].id
    error_message = "the trigger must run as swarm-workspace-deployer"
  }

  assert {
    condition     = google_cloudbuild_trigger.workspace_apply[0].name == "swarm-workspace-apply" && google_cloudbuild_trigger.workspace_apply[0].pubsub_config[0].topic == google_pubsub_topic.workspace_apply[0].id
    error_message = "the trigger is swarm-workspace-apply, fed by the swarm-workspace-apply topic"
  }

  # MAIN, twice: what is built and the file that says how.
  assert {
    condition = alltrue([
      google_cloudbuild_trigger.workspace_apply[0].source_to_build[0].ref == "refs/heads/main",
      google_cloudbuild_trigger.workspace_apply[0].git_file_source[0].revision == "refs/heads/main",
      google_cloudbuild_trigger.workspace_apply[0].git_file_source[0].path == "scripts/cloudbuild/workspace-apply.yaml",
      google_cloudbuild_trigger.workspace_apply[0].source_to_build[0].repository == var.workspace_apply_repository,
      google_cloudbuild_trigger.workspace_apply[0].git_file_source[0].repository == var.workspace_apply_repository,
    ])
    error_message = "the trigger must build refs/heads/main of this repository and read scripts/cloudbuild/workspace-apply.yaml from refs/heads/main: any other ref runs unreviewed code with project-wide account-IAM power"
  }

  # No event of its own that could build another ref.
  assert {
    condition = alltrue([
      length(google_cloudbuild_trigger.workspace_apply[0].github) == 0,
      length(google_cloudbuild_trigger.workspace_apply[0].repository_event_config) == 0,
      length(google_cloudbuild_trigger.workspace_apply[0].trigger_template) == 0,
      length(google_cloudbuild_trigger.workspace_apply[0].webhook_config) == 0,
      length(google_cloudbuild_trigger.workspace_apply[0].build) == 0,
    ])
    error_message = "the trigger may have no push, pull-request, template or webhook event and no inline build: it is a Pub/Sub trigger that runs main's build file, nothing else"
  }

  assert {
    condition     = toset(keys(google_cloudbuild_trigger.workspace_apply[0].substitutions)) == toset(["_WORKSPACE_ID", "_MODE", "_BUILDER_IMAGE"]) && google_cloudbuild_trigger.workspace_apply[0].substitutions["_BUILDER_IMAGE"] == var.workspace_apply_builder_image
    error_message = "the build gets the message's workspace_id and mode and the bootstrap's pinned image, and nothing else (docs/workspaces.md §2.1)"
  }

  assert {
    condition     = strcontains(google_cloudbuild_trigger.workspace_apply[0].filter, "_WORKSPACE_ID.matches('^w-[0-9a-f]{6}$')") && strcontains(google_cloudbuild_trigger.workspace_apply[0].filter, "_MODE.matches('^(create|limits)$')")
    error_message = "the trigger's filter must refuse a message whose workspace id or mode has any other shape, before a build exists"
  }

  assert {
    condition     = google_cloudbuild_trigger.workspace_apply[0].location == "us-central1"
    error_message = "the trigger lives in the region of its second-generation repository connection"
  }

  assert {
    condition     = google_pubsub_topic_iam_binding.workspace_apply_publisher[0].role == "roles/pubsub.publisher" && google_pubsub_topic_iam_binding.workspace_apply_publisher[0].members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "swarm-api must be the topic's only publisher (docs/workspaces.md §2.1), through an authoritative binding for the role"
  }

  assert {
    condition     = google_pubsub_topic.workspace_apply[0].labels["managed-by"] == "swarm-terraform"
    error_message = "the topic carries managed-by=swarm-terraform"
  }
}

run "its_roles_are_section_2_3_and_each_grant_is_bounded" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer     = true
    enable_forge_user_slots       = true
    workspace_apply_repository    = "projects/saga-agents-staging/locations/us-central1/connections/github/repositories/SwarmCloud"
    workspace_apply_builder_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.builder_digest}"
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

  assert {
    condition = toset(keys(google_project_iam_member.workspace_deployer)) == toset([
      "swarmWorkspaceAccountAdmin",
      "swarmWorkspaceProjectReader",
      "swarmWorkspaceFirestore",
      "swarmForgeSlotCreator",
      "roles/logging.logWriter",
      "roles/resourcemanager.projectIamAdmin",
      "swarmWorkspaceSecretBinder",
      "roles/container.clusterViewer",
    ])
    error_message = "the deployer's project grants must be §2.3's project rows on the WD9 fallback and no others"
  }

  assert {
    condition = alltrue(concat(
      [for k, m in google_project_iam_member.workspace_deployer : m.member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com"],
      [
        google_storage_bucket_iam_member.workspace_deployer_bucket_iam[0].member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
        google_storage_bucket_iam_member.workspace_deployer_marker[0].member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
        google_artifact_registry_repository_iam_member.workspace_deployer_pull[0].member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
      ],
    ))
    error_message = "every grant in workspace_deployer.tf is to swarm-workspace-deployer and to nobody else"
  }

  assert {
    condition = toset([for k, m in google_project_iam_member.workspace_deployer : k if length(m.condition) == 0]) == toset([
      "swarmWorkspaceAccountAdmin",
      "swarmWorkspaceProjectReader",
      "swarmWorkspaceFirestore",
      "swarmForgeSlotCreator",
      "roles/logging.logWriter",
    ])
    error_message = "only §2.3's five unconditioned project grants may be unconditioned: the account admin IAM cannot narrow, the reader, Firestore (whose data plane ignores conditions), the slot creator and the log writer"
  }

  # hasOnly() over EXACTLY the personal worker's three project roles.
  assert {
    condition = toset(split(", ", regex("hasOnly\\(\\[(.*)\\]\\)$", google_project_iam_member.workspace_deployer["roles/resourcemanager.projectIamAdmin"].condition[0].expression)[0])) == toset([
      "\"projects/saga-agents-staging/roles/swarmTenantWorkerFirestore\"",
      "\"roles/logging.logWriter\"",
      "\"roles/monitoring.metricWriter\"",
    ]) && startswith(google_project_iam_member.workspace_deployer["roles/resourcemanager.projectIamAdmin"].condition[0].expression, "api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([")
    error_message = "the conditioned projectIamAdmin must admit, through hasOnly() over modifiedGrantsByRole, the worker Firestore role, logWriter and metricWriter and no other role: without it the identity could grant itself owner"
  }

  assert {
    condition     = google_project_iam_member.workspace_deployer["swarmWorkspaceSecretBinder"].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-u-\")"
    error_message = "the secret binder is bounded to personal tenants' secrets, by their full name with the project NUMBER (docs/workspaces.md §2.3)"
  }

  assert {
    condition     = google_project_iam_member.workspace_deployer["roles/container.clusterViewer"].condition[0].expression == "resource.name.startsWith(\"projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot\")"
    error_message = "clusterViewer is bounded to the swarm cluster; agents-staging belongs to another team"
  }

  assert {
    condition = alltrue([
      google_storage_bucket_iam_member.workspace_deployer_bucket_iam[0].bucket == "swarm-artifacts-saga-agents-staging",
      google_storage_bucket_iam_member.workspace_deployer_bucket_iam[0].role == "projects/saga-agents-staging/roles/swarmWorkspaceBucketIam",
      google_storage_bucket_iam_member.workspace_deployer_marker[0].bucket == "swarm-artifacts-saga-agents-staging",
      google_storage_bucket_iam_member.workspace_deployer_marker[0].role == "roles/storage.objectCreator",
      google_storage_bucket_iam_member.workspace_deployer_marker[0].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/\")",
    ])
    error_message = "the bucket grants are on the artifact bucket only, and the object creator on tenants/ only"
  }

  assert {
    condition = alltrue([
      google_artifact_registry_repository_iam_member.workspace_deployer_pull[0].repository == "swarm-images",
      google_artifact_registry_repository_iam_member.workspace_deployer_pull[0].location == "us-central1",
      google_artifact_registry_repository_iam_member.workspace_deployer_pull[0].role == "projects/saga-agents-staging/roles/swarmImagePuller",
    ])
    error_message = "the pull grant is swarmImagePuller on the repository the builder image is in, and nowhere else"
  }

  assert {
    condition = toset(google_project_iam_custom_role.workspace["workspace_account_admin"].permissions) == toset([
      "iam.serviceAccounts.create",
      "iam.serviceAccounts.get",
      "iam.serviceAccounts.getIamPolicy",
      "iam.serviceAccounts.list",
      "iam.serviceAccounts.setIamPolicy",
    ])
    error_message = "swarmWorkspaceAccountAdmin is exactly §2.3's five permissions"
  }

  assert {
    condition = alltrue(flatten([
      for k, r in google_project_iam_custom_role.workspace : [
        for p in r.permissions : length(regexall("(?i)(delete|disable|undelete|keys|getAccessToken|getOpenIdToken|signBlob|signJwt|actAs|implicitDelegation|versions\\.access|objects\\.(get|list))", p)) == 0
      ]
    ]))
    error_message = "no swarmWorkspace* role may delete, disable, make a key, mint or sign a token, act as an account, read a secret's payload or read an object"
  }

  assert {
    condition     = length(google_project_iam_custom_role.workspace) == 5 && alltrue([for k, r in google_project_iam_custom_role.workspace : startswith(r.description, "managed-by=swarm-terraform;")])
    error_message = "five swarmWorkspace* custom roles, each describing who manages it"
  }

  # swarm-api's grants over personal tenants' GitHub slots: the user-slot
  # prefix, never a person's provider key, and never a person by name.
  assert {
    condition = alltrue([
      for k, m in google_project_iam_member.forge_personal_slots :
      m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && startswith(m.condition[0].expression, "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-u-\") && resource.name.extract(\"/secrets/swarm-tenant-u-{tenant}-git-u-\") != \"\"")
    ]) && toset(keys(google_project_iam_member.forge_personal_slots)) == toset(["version_adder", "version_manager", "slot_reader"])
    error_message = "swarm-api's personal-slot grants are its three user-slot grants, bounded to swarm-tenant-u-*-git-u-* slots"
  }

  assert {
    condition     = google_project_iam_member.forge_personal_slots["slot_reader"].condition[0].expression == google_project_iam_member.forge_personal_slots["version_adder"].condition[0].expression && google_project_iam_member.forge_personal_slots["slot_reader"].role == "roles/secretmanager.secretAccessor"
    error_message = "swarm-api reads personal tenants' user slots, base and -refresh twin alike, on the same bound as its writes (owner decision 2026-10-08)"
  }
}

run "its_build_logs_go_to_a_restricted_bucket_and_not_to_default" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer     = true
    enable_forge_user_slots       = true
    workspace_apply_repository    = "projects/saga-agents-staging/locations/us-central1/connections/github/repositories/SwarmCloud"
    workspace_apply_builder_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.builder_digest}"
    workspace_log_readers         = ["group:swarm-admins@saga.xyz", "user:owner@saga.xyz"]
  }

  override_resource {
    target          = google_cloudbuild_trigger.workspace_apply
    override_during = plan
    values = {
      trigger_id = "0f0f0f0f-1111-2222-3333-444444444444"
    }
  }

  assert {
    condition     = google_logging_project_bucket_config.workspace_apply[0].bucket_id == "swarm-workspace-apply" && google_logging_project_bucket_config.workspace_apply[0].location == "global" && google_logging_project_bucket_config.workspace_apply[0].retention_days == 30
    error_message = "the restricted bucket is swarm-workspace-apply, global, keeping 30 days like _Default"
  }

  assert {
    condition     = google_logging_project_sink.workspace_apply[0].destination == "logging.googleapis.com/projects/saga-agents-staging/locations/global/buckets/swarm-workspace-apply"
    error_message = "the sink writes to the restricted bucket"
  }

  assert {
    condition     = google_logging_project_sink.workspace_apply[0].filter == "resource.type=\"build\" AND resource.labels.build_trigger_id=\"0f0f0f0f-1111-2222-3333-444444444444\""
    error_message = "the sink selects this trigger's builds by the trigger's id, and nothing else: a wider filter would hide other logs from _Default"
  }

  assert {
    condition     = google_logging_project_exclusion.workspace_apply[0].filter == google_logging_project_sink.workspace_apply[0].filter && google_logging_project_exclusion.workspace_apply[0].disabled != true
    error_message = "_Default must exclude exactly what the sink routes, or the log is either in both places (readable by the other team) or in neither"
  }

  assert {
    condition = alltrue([
      for m, g in google_project_iam_member.workspace_log_readers :
      g.role == "roles/logging.viewAccessor" && g.condition[0].expression == "resource.name == \"projects/saga-agents-staging/locations/global/buckets/swarm-workspace-apply/views/_AllLogs\""
    ]) && length(google_project_iam_member.workspace_log_readers) == 2
    error_message = "each log reader gets viewAccessor on the restricted bucket's _AllLogs view and nothing wider"
  }
}

run "the_alerts_watch_the_names_the_bootstrap_creates" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    environment              = "dev"
    labels                   = { "managed-by" = "swarm-terraform" }
    service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
    wake_subscription        = "swarm-scheduler-wake-sub"
    dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
    safety_tick_job          = "swarm-scheduler-tick"
  }

  assert {
    condition     = toset(keys(google_monitoring_alert_policy.workspace)) == toset(["outside_personal_workers", "foreign_build", "trigger_changed"])
    error_message = "the three alerts of docs/workspaces.md §2.4"
  }

  # Each reads the Admin Activity audit log, which no sink or exclusion drops.
  assert {
    condition = alltrue([
      for k, p in google_monitoring_alert_policy.workspace :
      startswith(p.conditions[0].condition_matched_log[0].filter, "logName=\"projects/saga-agents-staging/logs/cloudaudit.googleapis.com%2Factivity\" AND ")
    ])
    error_message = "every workspace alert matches the project's Admin Activity audit log"
  }

  # The names the bootstrap creates, read from the earlier run's output, so a
  # rename on either side fails here.
  assert {
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter, "protoPayload.authenticationInfo.principalEmail=\"${run.the_identity_is_usable_only_by_its_trigger_on_main.workspace_deployer.service_account}\""),
      strcontains(google_monitoring_alert_policy.workspace["foreign_build"].conditions[0].condition_matched_log[0].filter, run.the_identity_is_usable_only_by_its_trigger_on_main.workspace_deployer.service_account),
      strcontains(google_monitoring_alert_policy.workspace["foreign_build"].conditions[0].condition_matched_log[0].filter, "NOT protoPayload.request.trigger.name=\"${run.the_identity_is_usable_only_by_its_trigger_on_main.workspace_deployer.trigger}\""),
      strcontains(google_monitoring_alert_policy.workspace["trigger_changed"].conditions[0].condition_matched_log[0].filter, "resource.labels.topic_id=\"${run.the_identity_is_usable_only_by_its_trigger_on_main.workspace_deployer.trigger}\""),
      strcontains(google_monitoring_alert_policy.workspace["trigger_changed"].conditions[0].condition_matched_log[0].filter, "resource.labels.email_id=\"${run.the_identity_is_usable_only_by_its_trigger_on_main.workspace_deployer.service_account}\""),
    ])
    error_message = "the alerts must name the account and the trigger terraform/bootstrap/workspace_deployer.tf creates; an alert on a name nothing uses never fires"
  }

  # Members and accounts are judged against the personal-worker pattern, which
  # is what the guard admits (C2-C6).
  assert {
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter, "NOT resource.labels.email_id=~\"^swarm-agent-worker-u-[a-z0-9-]+@saga-agents-staging[.]iam[.]gserviceaccount[.]com$\""),
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter, "protoPayload.serviceData.policyDelta.bindingDeltas.member!~\"^serviceAccount:swarm-agent-worker-u-[a-z0-9-]+@saga-agents-staging[.]iam[.]gserviceaccount[.]com$\""),
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter, "bindingDeltas.action=\"REMOVE\""),
    ])
    error_message = "the deployer alert must fire on an account, or a member, not named swarm-agent-worker-u-*, and on any removal"
  }

  assert {
    condition = alltrue([
      for k, p in google_monitoring_alert_policy.workspace :
      p.alert_strategy[0].notification_rate_limit[0].period == "300s" && p.user_labels["managed-by"] == "swarm-terraform"
    ])
    error_message = "a log-match policy needs a notification rate limit, and each carries managed-by=swarm-terraform"
  }
}

# ---------------------------------------------------------------------------
# The refusals. Last: each must FAIL, and a run that errors skips the rest.
# ---------------------------------------------------------------------------

run "an_unreviewed_role_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_deployer_roles = ["swarmWorkspaceAccountAdmin", "roles/iam.serviceAccountTokenCreator"]
  }

  expect_failures = [var.workspace_deployer_roles]
}

run "a_builder_image_by_tag_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_apply_builder_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply:dev"
  }

  expect_failures = [var.workspace_apply_builder_image]
}

run "a_log_reader_that_is_not_a_person_or_a_group_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_log_readers = ["domain:saga.xyz"]
  }

  expect_failures = [var.workspace_log_readers]
}

run "switching_it_on_before_the_repository_is_connected_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer     = true
    enable_forge_user_slots       = true
    workspace_apply_builder_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.builder_digest}"
  }

  expect_failures = [google_cloudbuild_trigger.workspace_apply]
}

run "the_slot_creator_without_the_slots_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer     = true
    enable_forge_user_slots       = false
    workspace_apply_repository    = "projects/saga-agents-staging/locations/us-central1/connections/github/repositories/SwarmCloud"
    workspace_apply_builder_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.builder_digest}"
  }

  expect_failures = [google_project_iam_member.workspace_deployer]
}
