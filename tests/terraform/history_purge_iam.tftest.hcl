# The audited history purge may delete a purged run's own files, and nothing
# else (owner decision 2026-10-11).
#
# terraform/bootstrap/history_purge.tf defines swarmHistoryPurgeDeleter;
# terraform/modules/iam/bindings.tf grants it to swarm-api on the artifact
# bucket, conditioned to tenants/<t>/tasks/ and tenants/<t>/checkpoints/. These
# runs hold the role to its one permission, the grant to swarm-api on that
# bucket, and the condition to exactly those two prefixes: they fail if the
# condition is dropped, if a clause is added or loosened, or if it admits a
# verdict, a repository index, a marker or a bucket-root object.
#
# The condition is evaluated here, not only compared. Its two clauses are read
# back out of the planned expression -- which also refuses any other shape, an
# `||` included -- and applied to object names the way IAM applies them:
# startsWith literally, and matches() as an RE2 search, which Terraform's
# regex() also is.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  # modules/iam's.
  artifact_bucket  = "swarm-artifacts-saga-agents-staging"
  labels           = { "managed-by" = "swarm-terraform" }
  gke_cluster_name = "swarm-autopilot"
  gke_location     = "us-central1"
}

run "the_role_deletes_objects_and_does_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = google_project_iam_custom_role.history_purge_deleter.permissions == toset(["storage.objects.delete"])
    error_message = "swarmHistoryPurgeDeleter must carry storage.objects.delete and nothing else: swarm-api already reads and lists through objectViewer, and must never create, overwrite or set IAM"
  }

  assert {
    condition     = google_project_iam_custom_role.history_purge_deleter.role_id == "swarmHistoryPurgeDeleter"
    error_message = "the role id is the one terraform/modules/custom_role_ids spells"
  }

  assert {
    condition     = startswith(google_project_iam_custom_role.history_purge_deleter.description, "managed-by=swarm-terraform;")
    error_message = "a custom role has no labels; it says managed-by=swarm-terraform in its description"
  }
}

run "swarm_api_holds_it_on_the_artifact_bucket_only" {
  command = plan

  # The accounts' emails are computed, so unknown at plan. Each one is given
  # its own, so the member below can only match if it is swarm-api's.
  override_resource {
    target          = google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_service_account.platform["swarm-scheduler"]
    override_during = plan
    values = {
      email = "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_service_account.platform["swarm-reconciler"]
    override_during = plan
    values = {
      email = "swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_service_account.platform["swarm-quota-broker"]
    override_during = plan
    values = {
      email = "swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  module {
    source = "../../terraform/modules/iam"
  }

  assert {
    condition = alltrue([
      google_storage_bucket_iam_member.api_history_purge_deleter.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_service_account.platform["swarm-api"].account_id == "swarm-api",
    ])
    error_message = "the purge runs in swarm-api; no other account may hold its delete"
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_history_purge_deleter.bucket == "swarm-artifacts-saga-agents-staging"
    error_message = "the grant is a member of the artifact bucket's policy, never the project's"
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_history_purge_deleter.role == "projects/saga-agents-staging/roles/swarmHistoryPurgeDeleter"
    error_message = "the grant names swarmHistoryPurgeDeleter by the id terraform/modules/custom_role_ids spells"
  }

  # The role reaches no project-level policy in this module.
  assert {
    condition = alltrue([
      for account, roles in output.granted_roles : alltrue([
        for role in roles : !strcontains(role, "swarmHistoryPurgeDeleter")
      ])
    ])
    error_message = "swarmHistoryPurgeDeleter must never be granted project-wide: that would let swarm-api delete every object in every bucket of this shared project"
  }

  assert {
    condition     = strcontains(google_storage_bucket_iam_member.api_history_purge_deleter.condition[0].description, "managed-by=swarm-terraform")
    error_message = "an IAM member has no labels; its condition's description says managed-by=swarm-terraform"
  }

  # The exact string, so any edit is a deliberate one this file has to follow.
  assert {
    condition     = google_storage_bucket_iam_member.api_history_purge_deleter.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/\") && resource.name.matches(\"^projects/_/buckets/[^/]+/objects/tenants/[^/]+/(tasks|checkpoints)/\")"
    error_message = "the purge's delete condition changed; it must admit tenants/<t>/tasks/ and tenants/<t>/checkpoints/ of the artifact bucket and nothing else"
  }

  # ADMITTED: a run's artifacts, logs and checkpoints, under any tenant --
  # personal tenants created at runtime included -- and the owner-named
  # tenants/<t>/checkpoints/.
  assert {
    condition = alltrue([
      for path in [
        "tenants/acme/tasks/task_1/result.json",
        "tenants/acme/tasks/task_1/attempts/att_1/checkpoints/ckpt-00001/archive.tar.gz",
        "tenants/acme/tasks/task_1/attempts/att_1/logs/stdout.log",
        "tenants/u-someone/tasks/task_2/result.json",
        "tenants/acme/checkpoints/ckpt-00001/manifest.json",
      ] :
      startswith(
        "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/${path}",
        regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.matches\\(\"([^\"]+)\"\\)$", google_storage_bucket_iam_member.api_history_purge_deleter.condition[0].expression)[0],
      ) &&
      can(regex(
        regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.matches\\(\"([^\"]+)\"\\)$", google_storage_bucket_iam_member.api_history_purge_deleter.condition[0].expression)[1],
        "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/${path}",
      ))
    ])
    error_message = "the condition must admit every object under tenants/<t>/tasks/ and tenants/<t>/checkpoints/, or the purge is refused and audited as history_purge_failed"
  }

  # REFUSED: every other prefix of the bucket, a near-miss of each admitted
  # one, and the same paths in any other bucket.
  assert {
    condition = alltrue([
      for name in concat(
        [
          for path in [
            "tenants/acme/verdicts/wf_1/task_9/verdict.json",
            "tenants/acme/repos/repo_1/index/0123abc/repo_graph_shards.json",
            "tenants/acme/repo-index/v1/graph.json",
            "tenants/acme/markers/done",
            "tenants/acme/result.json",
            "tenants/acme/tasks",
            "tenants/acme/tasksx/task_1/result.json",
            "tenants/acme/checkpoints-old/ckpt-00001/manifest.json",
            "tenants/acme/sub/tasks/task_1/result.json",
            "tenants//tasks/task_1/result.json",
            "tasks/task_1/result.json",
            "checkpoints/ckpt-00001/manifest.json",
            "markers/done",
            "repo-index/v1/graph.json",
            "releases/main/manifest.json",
          ] : "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/${path}"
        ],
        [
          "projects/_/buckets/other-bucket/objects/tenants/acme/tasks/task_1/result.json",
          "projects/_/buckets/swarm-artifacts-saga-agents-staging-x/objects/tenants/acme/tasks/task_1/result.json",
          "projects/_/buckets/swarm-artifacts-saga-agents-staging",
        ],
      ) :
      !(
        startswith(
          name,
          regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.matches\\(\"([^\"]+)\"\\)$", google_storage_bucket_iam_member.api_history_purge_deleter.condition[0].expression)[0],
        ) &&
        can(regex(
          regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.matches\\(\"([^\"]+)\"\\)$", google_storage_bucket_iam_member.api_history_purge_deleter.condition[0].expression)[1],
          name,
        ))
      )
    ])
    error_message = "the purge's delete condition admits an object outside tenants/<t>/tasks/ and tenants/<t>/checkpoints/ of the artifact bucket: a verdict, a repository index, a marker, a bucket-root object or another bucket's"
  }
}
