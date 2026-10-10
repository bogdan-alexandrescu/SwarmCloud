# The artifact bucket's lifecycle ages task artifacts and never the repository
# index (docs/repo-index.md §2.3, lane IX3, owner decision 2026-10-06).
#
# Under tenants/<t>/repos/<repo_id>/ live the index copies and the
# content-addressed graph blobs that planners read on every run. The rule that
# cold-stored `tenants/` put all of them in Nearline after 14 days (a retrieval
# fee on every read, a 30-day minimum on every deletion). GCS matchesPrefix can
# only include, so the Nearline rule lists tasks/ and verdicts/ per mapped
# tenant.
#
# The customTime Delete stays BUCKET-WIDE: a per-tenant prefix list would take
# every runtime personal tenant (`u-<slug>`, never in the tenants map) off the
# clock for good. repos/ is kept off it by carrying no customTime -- the
# worker never stamps one there (agent_worker.objectstore `is_repos_key`,
# tests/unit/worker/test_repos_objects_carry_no_custom_time.py) -- so the
# assertions below hold the Delete rule to customTime alone: no `age`, no
# `created_before`, nothing that could match an unstamped object.

mock_provider "google" {}

variables {
  project_id    = "saga-agents-staging"
  labels        = { "managed-by" = "swarm-terraform" }
  bucket_suffix = "saga-agents-staging"
  tenants       = ["eng", "research"]

  # Keys as the worker writes them: the index copy and a graph blob under
  # repos/, an artifact and a checkpoint under tasks/, a verdict, and a task
  # artifact of a runtime personal tenant that is not in the tenants map.
  repos_index_object   = "tenants/eng/repos/repo_0123456789abcdef/index/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/repo-index.json"
  repos_blob_object    = "tenants/research/repos/repo_0123456789abcdef/graph/blobs/0000000000000000000000000000000000000000000000000000000000000000.jsonl.gz"
  task_object          = "tenants/eng/tasks/task_1/attempts/att_1/artifacts/summary.md"
  checkpoint_object    = "tenants/research/tasks/task_2/attempts/att_1/checkpoints/ck_1/archive.tar.gz"
  verdict_object       = "tenants/eng/verdicts/wf_1/task_3/review.json"
  personal_task_object = "tenants/u-x/tasks/task_4/attempts/att_1/artifacts/summary.md"
}

run "nothing_under_repos_is_cold_stored_or_deleted_by_age" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  # A SetStorageClass rule with no matches_prefix matches every object,
  # repos/ included.
  assert {
    condition = alltrue([
      for r in google_storage_bucket.artifacts.lifecycle_rule :
      length(coalesce(one(r.condition).matches_prefix, [])) > 0
      if one(r.action).type == "SetStorageClass"
    ])
    error_message = "a Nearline rule with no matches_prefix cold-stores every object, repos/ included"
  }

  assert {
    condition = alltrue(flatten([
      for r in google_storage_bucket.artifacts.lifecycle_rule : [
        for p in coalesce(one(r.condition).matches_prefix, []) :
        !startswith(var.repos_index_object, p) && !startswith(var.repos_blob_object, p)
      ]
      if one(r.action).type == "SetStorageClass"
    ]))
    error_message = "a Nearline rule matches tenants/<t>/repos/: the index and its graph blobs would pay a retrieval fee on every planner read"
  }

  assert {
    condition = alltrue([
      for p in flatten([
        for r in google_storage_bucket.artifacts.lifecycle_rule :
        coalesce(one(r.condition).matches_prefix, [])
        if one(r.action).type == "SetStorageClass"
      ]) : p != "tenants/" && p != "tenants/eng/" && p != "tenants/research/"
    ])
    error_message = "a whole-tenant Nearline prefix includes repos/; only tasks/ and verdicts/ may be cold-stored"
  }

  # Every Delete on LIVE objects is keyed on customTime alone, which an
  # object under repos/ never carries: no `age`, no `created_before`, no
  # `num_newer_versions`. Any of those would match an unstamped index copy.
  # The one exception is the clone-bundle rule, bound to suffixes no repos/
  # key ends with (clone_bundle_lifecycle.tftest.hcl holds it there).
  assert {
    condition = alltrue([
      for r in google_storage_bucket.artifacts.lifecycle_rule :
      coalesce(one(r.condition).days_since_custom_time, 0) > 0
      && coalesce(one(r.condition).age, 0) == 0
      && (one(r.condition).created_before == null || one(r.condition).created_before == "")
      if one(r.action).type == "Delete" && one(r.condition).with_state != "ARCHIVED"
      && length(coalesce(one(r.condition).matches_suffix, [])) == 0
    ])
    error_message = "a Delete rule on live objects that is not keyed on customTime alone can reach tenants/<t>/repos/"
  }
}

run "task_artifacts_and_verdicts_are_still_cold_stored_and_expired" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "SetStorageClass"
      && one(r.action).storage_class == "NEARLINE"
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.task_object, p)])
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.checkpoint_object, p)])
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.verdict_object, p)])
    ]) == 1
    error_message = "task artifacts, checkpoints and verdicts are still cold-stored after nearline_after_days"
  }

  # The customTime Delete reaches a mapped tenant's task artifact and verdict
  # AND a runtime personal tenant's task artifact: a rule with no prefix
  # matches every key, one with prefixes must name one of the key's.
  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete"
      && one(r.condition).with_state == "LIVE"
      && coalesce(one(r.condition).days_since_custom_time, 0) > 0
      && alltrue([
        for k in [var.task_object, var.verdict_object, var.personal_task_object] :
        length(coalesce(one(r.condition).matches_prefix, [])) == 0
        || anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(k, p)])
      ])
    ]) == 1
    error_message = "every tenant's task artifacts and verdicts, a personal tenant's outside the map included, still expire artifact_retention_days after their customTime"
  }

  # Both mapped tenants, each by its own tasks/ prefix, on the Nearline rule.
  assert {
    condition = alltrue([
      for t in ["eng", "research"] : length([
        for r in google_storage_bucket.artifacts.lifecycle_rule : r
        if one(r.action).type == "SetStorageClass"
        && contains(coalesce(one(r.condition).matches_prefix, []), "tenants/${t}/tasks/")
      ]) == 1
    ])
    error_message = "every tenant in the map has its tasks/ prefix on the Nearline rule"
  }

  # The noncurrent-version cleanup is unchanged, and still bucket-wide.
  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete" && one(r.condition).with_state == "ARCHIVED"
    ]) == 2
    error_message = "versioning without expiry is how a bucket quietly becomes the largest line on the bill"
  }

  assert {
    condition     = google_storage_bucket.artifacts.labels["managed-by"] == "swarm-terraform"
    error_message = "the artifact bucket carries managed-by=swarm-terraform"
  }
}

# An empty matches_prefix is no condition at all: the Nearline rule would
# match every object, repos/ included. With no tenant it is omitted instead,
# and the bucket-wide customTime Delete is still there.
run "no_tenant_means_no_nearline_rule_and_the_delete_clock_stays" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  variables {
    tenants = []
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "SetStorageClass"
    ]) == 0
    error_message = "with no tenant there is nothing to cold-store; a rule with no prefix would cold-store every object"
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete" && one(r.condition).with_state == "LIVE"
      && coalesce(one(r.condition).days_since_custom_time, 0) > 0
    ]) == 1
    error_message = "the customTime Delete does not depend on the tenants map: personal tenants are created at runtime"
  }
}
