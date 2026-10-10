# Clone bundles and their branch-head pointers expire on an `age` clock,
# bucket-wide by suffix (issue #940, docs/clone-bundles.md).
#
# A bundle is a cache: a workflow's downstream steps clone the upstream's
# base within the same run, so it is useful for hours, and a miss costs only
# the old clone from GitHub. The customTime Delete alone would keep one for
# artifact_retention_days (180 in prod). A matches_prefix list cannot reach a
# runtime personal tenant (`u-<slug>`, never in the tenants map), so the rule
# is keyed on the two suffixes only the worker's bundle writer uses -- which
# no checkpoint, repos/ object, artifact or verdict ends with.

mock_provider "google" {}

variables {
  project_id    = "saga-agents-staging"
  labels        = { "managed-by" = "swarm-terraform" }
  bucket_suffix = "saga-agents-staging"
  tenants       = ["eng"]

  # Keys as the worker writes them: a bundle and a head pointer (one of them
  # under a personal tenant outside the map), and the objects an `age` Delete
  # must never reach.
  bundle_object        = "tenants/eng/bundles/repo_0123456789abcdef/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.swarm-clone.bundle"
  personal_head_object = "tenants/u-x/bundles/repo_0123456789abcdef/heads/main.swarm-clone.head"
  checkpoint_object    = "tenants/eng/tasks/task_2/attempts/att_1/checkpoints/ck_1/archive.tar.gz"
  repos_index_object   = "tenants/eng/repos/repo_0123456789abcdef/index/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/repo-index.json"
  task_artifact_object = "tenants/eng/tasks/task_1/attempts/att_1/artifacts/summary.md"
}

run "bundle_rule_matches_both_suffixes_and_expires_at_the_retention" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  assert {
    condition     = var.clone_bundle_retention_days == 7
    error_message = "clone bundles default to a 7-day retention: a week covers re-runs and resumes of a workflow"
  }

  # Exactly one rule, with both suffixes, no prefix (a prefix list cannot
  # reach a runtime personal tenant), deleting at the variable's age.
  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete"
      && contains(coalesce(one(r.condition).matches_suffix, []), ".swarm-clone.bundle")
      && contains(coalesce(one(r.condition).matches_suffix, []), ".swarm-clone.head")
      && length(coalesce(one(r.condition).matches_prefix, [])) == 0
      && coalesce(one(r.condition).age, 0) == var.clone_bundle_retention_days
    ]) == 1
    error_message = "one bucket-wide Delete rule expires *.swarm-clone.bundle and *.swarm-clone.head after clone_bundle_retention_days"
  }

  # The rule reaches a mapped tenant's bundle and a personal tenant's head.
  assert {
    condition = alltrue([
      for k in [var.bundle_object, var.personal_head_object] : anytrue(flatten([
        for r in google_storage_bucket.artifacts.lifecycle_rule : [
          for s in coalesce(one(r.condition).matches_suffix, []) : endswith(k, s)
        ]
        if one(r.action).type == "Delete" && coalesce(one(r.condition).age, 0) > 0
      ]))
    ])
    error_message = "every tenant's bundles and head pointers, a personal tenant's included, expire by age"
  }

  # Every `age` Delete is suffix-bound, and no suffix matches a checkpoint,
  # an index copy or a task artifact: those stay off the age clock.
  assert {
    condition = alltrue([
      for r in google_storage_bucket.artifacts.lifecycle_rule :
      length(coalesce(one(r.condition).matches_suffix, [])) > 0
      && alltrue([
        for s in coalesce(one(r.condition).matches_suffix, []) :
        !endswith(var.checkpoint_object, s) && !endswith(var.repos_index_object, s) && !endswith(var.task_artifact_object, s)
      ])
      if one(r.action).type == "Delete" && coalesce(one(r.condition).age, 0) > 0
    ])
    error_message = "an age-based Delete that is not bound to the clone-bundle suffixes can reach a checkpoint or tenants/<t>/repos/"
  }

  # Bundles stay STANDARD: no Nearline rule names them.
  assert {
    condition = alltrue(flatten([
      for r in google_storage_bucket.artifacts.lifecycle_rule : [
        for p in coalesce(one(r.condition).matches_prefix, []) : !startswith(var.bundle_object, p)
      ]
      if one(r.action).type == "SetStorageClass"
    ]))
    error_message = "a bundle is read within hours of being written; cold-storing it would charge a retrieval fee on every clone"
  }

  assert {
    condition     = google_storage_bucket.artifacts.labels["managed-by"] == "swarm-terraform"
    error_message = "the artifact bucket carries managed-by=swarm-terraform"
  }
}

# GCS reads `age = 0` as "delete on the next pass", so a bundle would vanish
# before the next step of the same workflow could clone from it.
run "retention_of_zero_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  variables {
    clone_bundle_retention_days = 0
  }

  expect_failures = [
    var.clone_bundle_retention_days,
  ]
}
