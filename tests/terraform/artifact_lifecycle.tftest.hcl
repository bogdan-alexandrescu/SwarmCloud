# The artifact bucket's lifecycle ages task artifacts and never the repository
# index (docs/repo-index.md §2, lane IX3, owner decision 2026-10-06).
#
# Under tenants/<t>/repos/<repo_id>/ live the index copies and the
# content-addressed graph blobs that planners read on every run. The rule that
# cold-stored `tenants/` put all of them in Nearline after 14 days (a retrieval
# fee on every read, a 30-day minimum on every deletion), and the customTime
# Delete, unprefixed, would delete an index copy the worker stamped. GCS
# matchesPrefix can only include, so the rules list tasks/ and verdicts/ per
# tenant. These assertions evaluate each rule's prefixes against real keys:
# one under repos/ must match none, one under tasks/ must match.

mock_provider "google" {}

variables {
  project_id    = "saga-agents-staging"
  labels        = { "managed-by" = "swarm-terraform" }
  bucket_suffix = "saga-agents-staging"
  tenants       = ["eng", "research"]

  # Keys as the worker writes them: the index copy and a graph blob under
  # repos/, an artifact and a checkpoint under tasks/, a verdict.
  repos_index_key = "tenants/eng/repos/repo_0123456789abcdef/index/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/repo-index.json"
  repos_blob_key  = "tenants/research/repos/repo_0123456789abcdef/graph/blobs/0000000000000000000000000000000000000000000000000000000000000000.jsonl.gz"
  task_key        = "tenants/eng/tasks/task_1/attempts/att_1/artifacts/summary.md"
  checkpoint_key  = "tenants/research/tasks/task_2/attempts/att_1/checkpoints/ck_1/archive.tar.gz"
  verdict_key     = "tenants/eng/verdicts/wf_1/task_3/review.json"
}

run "nothing_under_repos_is_cold_stored_or_deleted_by_the_clock" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  # Every rule that acts on LIVE objects -- SetStorageClass, or a Delete that
  # is not one of the two noncurrent-version rules -- names prefixes, and no
  # prefix it names is a prefix of a key under repos/.
  assert {
    condition = alltrue([
      for r in google_storage_bucket.artifacts.lifecycle_rule :
      length(coalesce(one(r.condition).matches_prefix, [])) > 0
      if one(r.action).type == "SetStorageClass"
      || (one(r.action).type == "Delete" && one(r.condition).with_state != "ARCHIVED")
    ])
    error_message = "a lifecycle rule on live objects with no matches_prefix matches every object, repos/ included"
  }

  assert {
    condition = alltrue(flatten([
      for r in google_storage_bucket.artifacts.lifecycle_rule : [
        for p in coalesce(one(r.condition).matches_prefix, []) :
        !startswith(var.repos_index_key, p) && !startswith(var.repos_blob_key, p)
      ]
      if one(r.action).type == "SetStorageClass" || one(r.action).type == "Delete"
    ]))
    error_message = "a lifecycle rule matches tenants/<t>/repos/: the index and its graph blobs would be cold-stored or deleted by age"
  }

  assert {
    condition = alltrue([
      for p in flatten([
        for r in google_storage_bucket.artifacts.lifecycle_rule :
        coalesce(one(r.condition).matches_prefix, [])
      ]) : p != "tenants/" && p != "tenants/eng/" && p != "tenants/research/"
    ])
    error_message = "a whole-tenant prefix includes repos/; only tasks/ and verdicts/ may be aged"
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
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.task_key, p)])
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.checkpoint_key, p)])
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.verdict_key, p)])
    ]) == 1
    error_message = "task artifacts, checkpoints and verdicts are still cold-stored after nearline_after_days"
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete"
      && one(r.condition).with_state == "LIVE"
      && coalesce(one(r.condition).days_since_custom_time, 0) > 0
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.task_key, p)])
      && anytrue([for p in coalesce(one(r.condition).matches_prefix, []) : startswith(var.verdict_key, p)])
    ]) == 1
    error_message = "a task artifact and a verdict still expire artifact_retention_days after their customTime"
  }

  # Both tenants of the map, each by its own tasks/ prefix.
  assert {
    condition = alltrue([
      for t in ["eng", "research"] : length([
        for r in google_storage_bucket.artifacts.lifecycle_rule : r
        if contains(coalesce(one(r.condition).matches_prefix, []), "tenants/${t}/tasks/")
      ]) == 2
    ])
    error_message = "every tenant in the map has its tasks/ prefix on both the Nearline and the customTime rule"
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

# An empty matches_prefix is no condition at all: the rule would match every
# object, repos/ included. With no tenant the two rules are omitted instead.
run "no_tenant_means_no_aged_rule_rather_than_a_rule_on_everything" {
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
      || (one(r.action).type == "Delete" && one(r.condition).with_state == "LIVE")
    ]) == 0
    error_message = "with no tenant there is nothing to age; a rule with no prefix would age every object"
  }
}
