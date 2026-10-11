# swarm-workspace-deployer, the one Cloud Run job that runs as it, who may
# start that job, Cloud Run's DATA_WRITE audit log, the job's logs and its
# alerts (terraform/bootstrap/workspace_deployer.tf, terraform/modules/
# monitoring/workspace_alerts.tf; docs/workspaces.md §2.1-2.6, lanes W4 and W4b
# of #847). The dispatch path is tests/terraform/workspace_dispatch.tftest.hcl.
#
# What these runs hold, as properties rather than spellings:
#
#   * nothing exists until the owner switches it on, and no Cloud Build trigger
#     exists at all;
#   * the identity has no key resource and an EMPTY account-level policy, so
#     nobody may act as it or mint its token there;
#   * the job runs as that identity, runs the pinned image by digest with the
#     fixed command `python3 -I /opt/swarm/entry.py` and no argument or
#     environment of its own, one task, no retries, 1800 seconds, egress
#     ALL_TRAFFIC into the swarm subnet with the worker tag;
#   * the job's IAM policy is authoritative and names swarm-workspace-dispatch
#     alone, with the run-with-overrides role and nothing else;
#   * Cloud Run's audit config is DATA_WRITE, and only DATA_WRITE: no
#     DATA_READ, no exempted member;
#   * its roles are exactly §2.3's list (on the WD9 fallback), with no image
#     pull; every grant is to the deployer; the five §2.3 leaves unconditioned
#     are the only unconditioned ones; projectIamAdmin's hasOnly() admits the
#     personal worker's three project roles and nothing else; the secret,
#     object and cluster grants are bounded to swarm-tenant-u-, tenants/ and
#     the swarm cluster; no custom role carries delete, disable, key, token,
#     signing or actAs power;
#   * swarm-api is the topic's only publisher;
#   * the job's own log is routed, by the job's name, to a restricted bucket
#     and excluded from _Default by the same filter, its audit entries left in
#     _Default, and its readers are granted that bucket's view and nothing wider;
#   * the four alerts watch the very accounts and job the bootstrap makes, and
#     the jobs.run alert reads the Data Access log the audit config turns on;
#   * and the refusals: an unreviewed role (the old image pull included), a
#     mutable image, an image from another project, no image at all, a
#     malformed subnet, the slot creator without the slots, a log reader who
#     is not a person or a group.
#
# A mock provider proves the configuration says what was meant. It does not
# prove what Cloud Run, Cloud Logging or IAM do with it live; the "NOT VERIFIED
# LIVE" lists in both files say what the first workspace settles.
#
# ORDER: the runs that only assert come first, the expect_failures runs last,
# because a run that ERRORS skips every run after it in this file.

mock_provider "google" {
  # The identities' empty policies and the job's policy are rendered by this
  # data source; left to the mock it is a random string the policy resources
  # cannot parse.
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
  image_digest = "sha256:${join("", [for i in range(8) : "0123abcd"])}"
}

run "nothing_exists_until_the_owner_switches_it_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      length(google_service_account.workspace_deployer) == 0,
      length(google_cloud_run_v2_job.workspace_apply) == 0,
      length(google_cloud_run_v2_job_iam_policy.workspace_apply) == 0,
      length(google_project_iam_audit_config.run_data_write) == 0,
      length(google_pubsub_topic.workspace_apply) == 0,
      length(google_project_iam_member.workspace_deployer) == 0,
      length(google_project_iam_custom_role.workspace) == 0,
      length(google_logging_project_sink.workspace_apply) == 0,
      length(google_logging_project_exclusion.workspace_apply) == 0,
      length(google_project_iam_member.forge_personal_slots) == 0,
      length(google_service_account.workspace_dispatch) == 0,
      length(google_workflows_workflow.workspace_apply) == 0,
      length(google_eventarc_trigger.workspace_apply) == 0,
    ])
    error_message = "with enable_workspace_deployer at its default (false) the bootstrap must create no part of the workspace job: the owner's one-time steps come first (docs/workspaces.md §10)"
  }

  assert {
    condition     = output.workspace_deployer == null
    error_message = "the workspace_deployer output is null while the job is off"
  }

  # The default role list IS §2.3's table on the WD9 fallback; no image pull.
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
    ])
    error_message = "workspace_deployer_roles' default must be exactly docs/workspaces.md §2.3's list on the WD9 fallback, without swarmImagePuller: Cloud Run pulls a job's image as its service agent"
  }
}

run "the_identity_is_usable_only_by_its_job" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
    workspace_log_readers     = ["group:swarm-admins@saga.xyz"]
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

  # Distinct strings, so an assertion that a policy resource renders a given
  # data source fails if the resource is pointed at another one.
  override_data {
    target = data.google_iam_policy.workspace_deployer_nobody
    values = {
      policy_data = "{\"deployer\":\"nobody\"}"
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

  assert {
    condition     = google_service_account.workspace_deployer[0].account_id == "swarm-workspace-deployer"
    error_message = "the identity is swarm-workspace-deployer, the name the guard and the alerts all expect"
  }

  # Its account-level policy is written, authoritatively, with no binding: no
  # actAs, no token creator, no workloadIdentityUser for anyone.
  assert {
    condition     = length(data.google_iam_policy.workspace_deployer_nobody.binding) == 0 && google_service_account_iam_policy.workspace_deployer[0].policy_data == "{\"deployer\":\"nobody\"}"
    error_message = "swarm-workspace-deployer's own IAM policy must be written authoritatively EMPTY: any member there could act as the identity outside its job (docs/workspaces.md §2.4, safeguard 1)"
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_job.workspace_apply[0].name == "swarm-workspace-apply",
      google_cloud_run_v2_job.workspace_apply[0].location == "us-central1",
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].service_account == "swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
      google_cloud_run_v2_job.workspace_apply[0].labels["managed-by"] == "swarm-terraform",
      google_cloud_run_v2_job.workspace_apply[0].template[0].labels["managed-by"] == "swarm-terraform",
    ])
    error_message = "the job is swarm-workspace-apply, in the platform's region, running as swarm-workspace-deployer, carrying managed-by=swarm-terraform"
  }

  # What it runs: the pinned image, the scrub first, nothing of its own that a
  # caller's override would merge with.
  assert {
    condition = alltrue([
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].image == var.workspace_apply_image,
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].command == tolist(["python3", "-I", "/opt/swarm/entry.py"]),
      try(length(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].args), 0) == 0,
      try(length(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].env), 0) == 0,
      length(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers) == 1,
      try(length(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].volumes), 0) == 0,
    ])
    error_message = "the job runs one container: the pinned image by digest, with the command `python3 -I /opt/swarm/entry.py` (the scrub, §2.2 step 0), and no argument, environment or volume of its own"
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_job.workspace_apply[0].template[0].task_count == 1,
      google_cloud_run_v2_job.workspace_apply[0].template[0].parallelism == 1,
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].max_retries == 0,
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].timeout == "1800s",
    ])
    error_message = "one task, no parallelism, no retries (a retry is an admin's action, §1.3), 1800 seconds"
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC",
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].vpc_access[0].network_interfaces[0].subnetwork == "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1",
      google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].vpc_access[0].network_interfaces[0].network == "projects/saga-agents-staging/global/networks/swarm-vpc",
      contains(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].vpc_access[0].network_interfaces[0].tags, "swarm-worker"),
    ])
    error_message = "the job's egress is ALL_TRAFFIC into the swarm subnet, tagged swarm-worker so modules/network's worker-ingress deny covers it (§2.1)"
  }

  # WHO MAY START IT: one binding, the run-with-overrides role, the dispatcher.
  assert {
    condition = alltrue([
      length(data.google_iam_policy.workspace_apply_job.binding) == 1,
      toset([for b in data.google_iam_policy.workspace_apply_job.binding : b.role]) == toset(["roles/run.jobsExecutorWithOverrides"]),
      toset(flatten([for b in data.google_iam_policy.workspace_apply_job.binding : tolist(b.members)])) == toset(["serviceAccount:swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com"]),
      google_cloud_run_v2_job_iam_policy.workspace_apply[0].policy_data == "{\"job\":\"dispatcher-only\"}",
      google_cloud_run_v2_job_iam_policy.workspace_apply[0].name == "swarm-workspace-apply",
      google_cloud_run_v2_job_iam_policy.workspace_apply[0].location == "us-central1",
    ])
    error_message = "the job's IAM policy must be written authoritatively with ONE member, swarm-workspace-dispatch, holding roles/run.jobsExecutorWithOverrides: any other member could start the job without the alert's caller check being the only line (docs/workspaces.md §2.3)"
  }

  # Cloud Run's audit config: DATA_WRITE, so a jobs.run is recorded, and
  # nothing else.
  assert {
    condition = alltrue([
      google_project_iam_audit_config.run_data_write[0].service == "run.googleapis.com",
      google_project_iam_audit_config.run_data_write[0].project == "saga-agents-staging",
      [for c in google_project_iam_audit_config.run_data_write[0].audit_log_config : c.log_type] == ["DATA_WRITE"],
      alltrue([for c in google_project_iam_audit_config.run_data_write[0].audit_log_config : try(length(c.exempted_members), 0) == 0]),
    ])
    error_message = "Cloud Run's audit config must enable DATA_WRITE, exactly once, exempting nobody: a jobs.run is a DATA_WRITE entry, and without it the foreign_run alert reads nothing (W0b (2))"
  }

  assert {
    condition     = !contains([for c in google_project_iam_audit_config.run_data_write[0].audit_log_config : c.log_type], "DATA_READ") && !contains([for c in google_project_iam_audit_config.run_data_write[0].audit_log_config : c.log_type], "ADMIN_READ")
    error_message = "no DATA_READ (or ADMIN_READ) for Cloud Run: it would log every get and list in the shared project, the other team's included, for nothing this platform reads"
  }

  assert {
    condition     = google_pubsub_topic_iam_binding.workspace_apply_publisher[0].role == "roles/pubsub.publisher" && google_pubsub_topic_iam_binding.workspace_apply_publisher[0].members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "swarm-api must be the topic's only publisher (docs/workspaces.md §2.1, option (ii)), through an authoritative binding for the role"
  }

  assert {
    condition     = google_pubsub_topic.workspace_apply[0].labels["managed-by"] == "swarm-terraform"
    error_message = "the topic carries managed-by=swarm-terraform"
  }

  assert {
    condition = alltrue([
      output.workspace_deployer.job == "swarm-workspace-apply",
      output.workspace_deployer.image == var.workspace_apply_image,
      output.workspace_deployer.dispatcher == "swarm-workspace-dispatch@saga-agents-staging.iam.gserviceaccount.com",
    ])
    error_message = "the workspace_deployer output names the job, the pinned image and the dispatcher the owner reads a plan against"
  }
}

run "its_roles_are_section_2_3_and_each_grant_is_bounded" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
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

run "its_logs_go_to_a_restricted_bucket_and_not_to_default" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
    workspace_log_readers     = ["group:swarm-admins@saga.xyz", "user:owner@saga.xyz"]
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
    condition     = google_logging_project_sink.workspace_apply[0].filter == "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"swarm-workspace-apply\" AND NOT logName:\"cloudaudit.googleapis.com\""
    error_message = "the sink selects the swarm-workspace-apply job's own entries by its resource type and job name, and not its audit entries: a wider filter would hide other logs from _Default, and routing the jobs.run audit entry away would hide who started an execution (§2.6 item 4)"
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
    condition     = toset(keys(google_monitoring_alert_policy.workspace)) == toset(["outside_personal_workers", "foreign_runtime", "job_changed", "foreign_run"])
    error_message = "the four alerts of docs/workspaces.md §2.4: outside personal workers, a foreign runtime as the deployer, a change to the job or its dispatch, and a jobs.run by another caller"
  }

  # Three read the Admin Activity audit log, which no sink or exclusion drops;
  # the jobs.run alert reads the Data Access log the audit config enables.
  assert {
    condition = alltrue([
      for k in ["outside_personal_workers", "foreign_runtime", "job_changed"] :
      startswith(google_monitoring_alert_policy.workspace[k].conditions[0].condition_matched_log[0].filter, "logName=\"projects/saga-agents-staging/logs/cloudaudit.googleapis.com%2Factivity\" AND ")
    ])
    error_message = "the deployer, runtime and change alerts match the project's Admin Activity audit log"
  }

  assert {
    condition     = startswith(google_monitoring_alert_policy.workspace["foreign_run"].conditions[0].condition_matched_log[0].filter, "logName=\"projects/saga-agents-staging/logs/cloudaudit.googleapis.com%2Fdata_access\" AND ")
    error_message = "a jobs.run is a DATA_WRITE (Data Access) entry, not Admin Activity (W0b (2)): the foreign_run alert must read the data_access log"
  }

  # The names the bootstrap creates, read from the earlier run's output, so a
  # rename on either side fails here.
  assert {
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter, "protoPayload.authenticationInfo.principalEmail=\"${run.the_identity_is_usable_only_by_its_job.workspace_deployer.service_account}\""),
      strcontains(google_monitoring_alert_policy.workspace["foreign_runtime"].conditions[0].condition_matched_log[0].filter, "\"${run.the_identity_is_usable_only_by_its_job.workspace_deployer.service_account}\""),
      strcontains(google_monitoring_alert_policy.workspace["foreign_runtime"].conditions[0].condition_matched_log[0].filter, "NOT protoPayload.resourceName=~\"/jobs/${run.the_identity_is_usable_only_by_its_job.workspace_deployer.job}$\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "protoPayload.serviceName=\"run.googleapis.com\" AND protoPayload.resourceName=~\"/jobs/${run.the_identity_is_usable_only_by_its_job.workspace_deployer.job}$\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "resource.labels.email_id=\"${run.the_identity_is_usable_only_by_its_job.workspace_deployer.service_account}\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "resource.labels.email_id=\"${run.the_identity_is_usable_only_by_its_job.workspace_deployer.dispatcher}\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "resource.labels.topic_id=\"${run.the_identity_is_usable_only_by_its_job.workspace_deployer.job}\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "protoPayload.resourceName=~\"/workflows/${run.the_identity_is_usable_only_by_its_job.workspace_deployer.workflow}$\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "protoPayload.resourceName=~\"/triggers/${run.the_identity_is_usable_only_by_its_job.workspace_deployer.workflow}$\""),
    ])
    error_message = "the alerts must name the accounts, the job, the workflow and the Eventarc trigger terraform/bootstrap creates; an alert on a name nothing uses never fires"
  }

  # The jobs.run alert: any RunJob of this job whose caller is not the
  # dispatcher. Read against the bootstrap's own output.
  assert {
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.workspace["foreign_run"].conditions[0].condition_matched_log[0].filter, "protoPayload.serviceName=\"run.googleapis.com\""),
      strcontains(google_monitoring_alert_policy.workspace["foreign_run"].conditions[0].condition_matched_log[0].filter, "protoPayload.methodName=~\"[.]RunJob$\""),
      strcontains(google_monitoring_alert_policy.workspace["foreign_run"].conditions[0].condition_matched_log[0].filter, "protoPayload.resourceName=~\"/jobs/${run.the_identity_is_usable_only_by_its_job.workspace_deployer.job}$\""),
      strcontains(google_monitoring_alert_policy.workspace["foreign_run"].conditions[0].condition_matched_log[0].filter, "NOT protoPayload.authenticationInfo.principalEmail=\"${run.the_identity_is_usable_only_by_its_job.workspace_deployer.dispatcher}\""),
    ])
    error_message = "the foreign_run alert fires on a RunJob of swarm-workspace-apply by any caller but swarm-workspace-dispatch (docs/workspaces.md §2.4)"
  }

  # Turning DATA_WRITE off would blind foreign_run, so job_changed watches the
  # audit config; and the invoker grant, which cannot be narrowed.
  assert {
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "protoPayload.serviceData.policyDelta.auditConfigDeltas.service=\"run.googleapis.com\""),
      strcontains(google_monitoring_alert_policy.workspace["job_changed"].conditions[0].condition_matched_log[0].filter, "protoPayload.serviceData.policyDelta.bindingDeltas.role=\"roles/workflows.invoker\""),
    ])
    error_message = "job_changed must page on a change to Cloud Run's audit config and to any roles/workflows.invoker grant"
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

# The build's image pull left with the build: putting it back is a review.
run "the_old_image_pull_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_deployer_roles = ["swarmWorkspaceAccountAdmin", "swarmImagePuller"]
  }

  expect_failures = [var.workspace_deployer_roles]
}

run "an_image_by_tag_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_apply_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply:dev"
  }

  expect_failures = [var.workspace_apply_image]
}

run "a_malformed_subnet_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_apply_subnetwork = "agents-staging-vpc"
  }

  expect_failures = [var.workspace_apply_subnetwork]
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

run "switching_it_on_without_an_image_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
  }

  expect_failures = [google_cloud_run_v2_job.workspace_apply]
}

run "an_image_from_another_project_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/someone-else/swarm-images/workspace-apply@${var.image_digest}"
  }

  expect_failures = [google_cloud_run_v2_job.workspace_apply]
}

run "the_slot_creator_without_the_slots_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = false
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  expect_failures = [google_project_iam_member.workspace_deployer]
}
