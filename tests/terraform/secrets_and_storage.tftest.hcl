# Provider keys and the artifact bucket. The property under test in both cases
# is that one tenant cannot reach another's data.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"
  labels     = { "managed-by" = "swarm-terraform" }
}

run "a_provider_key_is_readable_by_exactly_one_identity" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers     = ["anthropic", "openai"]
        accessor      = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
        admin_members = ["group:eng@saga.xyz"]
      }
      research = {
        providers = ["anthropic"]
        accessor  = "serviceAccount:swarm-agent-worker-research@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
  }

  # The id must be exactly what swarm_common.models.Tenant.secret_name() builds,
  # because the worker looks the name up rather than being told it.
  assert {
    condition     = contains(output.secret_ids, "swarm-tenant-eng-anthropic")
    error_message = "secret ids must match Tenant.secret_name(): swarm-tenant-<tenant>-<provider>"
  }

  assert {
    condition     = length(output.secret_ids) == 3
    error_message = "one secret per (tenant, provider) pair"
  }

  # An authoritative binding, not an additive member: the members list here IS
  # the complete set of identities that can read the key, so a grant made out of
  # band is removed on the next apply.
  assert {
    condition     = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-anthropic"].members == toset(["serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "exactly one identity may read a tenant's provider key"
  }

  assert {
    condition     = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-research-anthropic"].members != google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-anthropic"].members
    error_message = "one tenant's key must never be readable from another tenant's workload"
  }

  # secretVersionAdder carries no access permission: rotating a key does not
  # require the ability to read the key already in place.
  assert {
    condition     = google_secret_manager_secret_iam_binding.version_adder["swarm-tenant-eng-anthropic"].role == "roles/secretmanager.secretVersionAdder"
    error_message = "admins add versions; they do not read them back"
  }

  assert {
    condition     = length(google_secret_manager_secret.this["swarm-tenant-eng-anthropic"].replication[0].user_managed[0].replicas) == 1
    error_message = "user-managed replication pins the key to the workload region"
  }

  assert {
    condition     = google_secret_manager_secret.this["swarm-tenant-eng-anthropic"].replication[0].user_managed[0].replicas[0].location == "us-central1"
    error_message = "automatic replication would copy provider keys into regions this platform never runs in"
  }
}

run "no_secret_version_is_ever_written_by_terraform" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers = ["anthropic"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
  }

  # A key passed through a terraform variable is written to state in clear text,
  # and state is readable by far more people than the key was meant for. Every
  # secret here is created empty and populated out of band.
  assert {
    condition     = google_secret_manager_secret.this["swarm-tenant-eng-anthropic"].annotations["swarm-populated-by"] == "out-of-band"
    error_message = "secret versions are added by a human or by CI, never by terraform"
  }
}

run "a_public_accessor_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers     = ["anthropic"]
        accessor      = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
        admin_members = ["allAuthenticatedUsers"]
      }
    }
  }

  expect_failures = [var.tenant_secrets]
}

run "the_artifact_bucket_is_private_versioned_and_expires_what_nobody_reads" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  variables {
    bucket_suffix = "saga-agents-staging"
    tenants       = ["eng", "research"]
  }

  assert {
    condition     = google_storage_bucket.artifacts.name == "swarm-artifacts-saga-agents-staging"
    error_message = "the bucket name carries the swarm prefix the destroy guard looks for"
  }

  assert {
    condition     = google_storage_bucket.artifacts.uniform_bucket_level_access == true
    error_message = "UBLA is required: per-tenant prefix conditions cannot be expressed with ACLs"
  }

  assert {
    condition     = google_storage_bucket.artifacts.public_access_prevention == "enforced"
    error_message = "public access prevention must be enforced, not inherited"
  }

  assert {
    condition     = google_storage_bucket.artifacts.versioning[0].enabled == true
    error_message = "a checkpoint is what stands between an interrupted two-hour run and starting over"
  }

  assert {
    condition     = google_storage_bucket.artifacts.force_destroy == false
    error_message = "force_destroy would let a destroy take every checkpoint with it"
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete" && coalesce(one(r.condition).num_newer_versions, 0) > 0
    ]) == 1
    error_message = "versioning without expiry is how a bucket quietly becomes the largest line on the bill"
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "SetStorageClass"
    ]) == 1
    error_message = "checkpoints are written constantly and read almost never; they belong in cold storage"
  }

  assert {
    condition     = output.tenant_prefixes["eng"] == "tenants/eng/"
    error_message = "the per-tenant prefix must match WorkerConfig.gcs_prefix"
  }

  assert {
    condition     = google_storage_bucket.artifacts.logging[0].log_bucket == google_storage_bucket.access_logs.name
    error_message = "the artifact bucket logs access to a bucket that is not itself"
  }

  assert {
    condition     = google_storage_bucket.access_logs.public_access_prevention == "enforced"
    error_message = "the log bucket is private too"
  }
}

# A checkpoint is deleted by reference, never by the clock (D14, owner decision:
# days_since_custom_time). The worker stamps customTime on every object it
# uploads EXCEPT a checkpoint's (agent_worker.objectstore.is_checkpoint_key), and
# GCS never matches days_since_custom_time against an object with no customTime.
# So the only Delete rule on LIVE objects must be that one: an `age` Delete
# would match a PARKED task's only checkpoint on the same day as a finished
# task's leftovers, which reconciler/checkpoints.py exists to prevent.
run "a_live_object_is_deleted_by_custom_time_never_by_age" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  variables {
    bucket_suffix           = "saga-agents-staging"
    artifact_retention_days = 14
  }

  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete"
      && one(r.condition).with_state == "LIVE"
      && coalesce(one(r.condition).days_since_custom_time, 0) == 14
    ]) == 1
    error_message = "artifacts and logs must expire artifact_retention_days after the customTime the worker stamps on them"
  }

  # The clone-bundle rule (issue #940) is the only `age` Delete, and it is
  # bound to the two bundle suffixes, which no checkpoint key ends with.
  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete" && coalesce(one(r.condition).age, 0) > 0
      && sort(coalesce(one(r.condition).matches_suffix, [])) != sort([".swarm-clone.bundle", ".swarm-clone.head"])
    ]) == 0
    error_message = "an age-based Delete rule deletes a PARKED task's only checkpoint on a clock; checkpoints are removed by reconciler/checkpoints.py alone"
  }

  # Every Delete rule is one of four shapes: the customTime clock on LIVE
  # objects, one of the two noncurrent-version rules on ARCHIVED ones, or the
  # clone-bundle suffix rule (decided in issue #940). A fifth shape
  # (created_before, matches_prefix, a different with_state) is a new way to
  # reach a checkpoint and has to be decided, not slipped in.
  assert {
    condition = alltrue([
      for r in google_storage_bucket.artifacts.lifecycle_rule :
      one(r.condition).with_state == "ARCHIVED" || coalesce(one(r.condition).days_since_custom_time, 0) > 0
      || sort(coalesce(one(r.condition).matches_suffix, [])) == sort([".swarm-clone.bundle", ".swarm-clone.head"])
      if one(r.action).type == "Delete"
    ])
    error_message = "a Delete rule on live objects that is not keyed on customTime can match a checkpoint"
  }
}

run "artifacts_must_outlive_the_investigation_that_needs_them" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  variables {
    bucket_suffix           = "saga-agents-staging"
    artifact_retention_days = 3
  }

  expect_failures = [var.artifact_retention_days]
}

# -- the long-lived half of a subscription credential ----------------------
#
# A refresh token is a standing grant on the tenant's Claude account; an access
# token expires. The whole point of splitting them is that a job holds only the
# expiring one, so a prompt-injected agent cannot walk out with durable access.
# These assertions are that split, stated as a test.

run "a_worker_cannot_read_the_refresh_token_it_runs_on" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    enable_subscription_refresh = true
    refresher_member            = "serviceAccount:swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com"
    tenant_secrets = {
      "u-bogdan" = {
        providers     = ["anthropic"]
        accessor      = "serviceAccount:swarm-agent-worker-u-bogdan@saga-agents-staging.iam.gserviceaccount.com"
        admin_members = ["group:platform@saga.xyz"]
      }
    }
  }

  assert {
    condition = google_secret_manager_secret_iam_binding.refresh_accessor["swarm-tenant-u-bogdan-anthropic-refresh"].members == toset([
      "serviceAccount:swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "only the broker may read a refresh token; a worker that could read one could mint itself credentials after its job ended"
  }

  # The broker writes both halves: the rotated refresh token back to the
  # sibling, and the access token it exchanged for into the base secret.
  assert {
    condition = alltrue([
      for id in ["swarm-tenant-u-bogdan-anthropic", "swarm-tenant-u-bogdan-anthropic-refresh"] :
      contains(google_secret_manager_secret_iam_binding.version_adder[id].members,
      "serviceAccount:swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com")
    ])
    error_message = "the broker must be able to add a version to both halves, or a refreshed credential has nowhere to go"
  }

  # Discovery reads labels rather than splitting the name, because `u-bogdan`
  # contains a dash and the name does not split unambiguously.
  assert {
    condition = (
      google_secret_manager_secret.refresh["swarm-tenant-u-bogdan-anthropic-refresh"].labels["tenant"] == "u-bogdan" &&
      google_secret_manager_secret.refresh["swarm-tenant-u-bogdan-anthropic-refresh"].labels["provider"] == "anthropic" &&
      google_secret_manager_secret.refresh["swarm-tenant-u-bogdan-anthropic-refresh"].labels["component"] == "tenant-credential"
    )
    error_message = "the broker discovers tenants by label; without these it cannot find this secret"
  }

  # Same protection as the base secret. It is the more sensitive of the two.
  assert {
    condition     = google_secret_manager_secret.refresh["swarm-tenant-u-bogdan-anthropic-refresh"].version_destroy_ttl == "86400s"
    error_message = "a refresh token destroyed by mistake must be recoverable for as long as a provider key is"
  }
}

run "an_api_key_only_deployment_grows_no_refresh_secrets" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers = ["anthropic"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
  }

  assert {
    condition     = length(google_secret_manager_secret.refresh) == 0
    error_message = "refresh secrets exist only where a refresher does"
  }
}

# #454 (intake mock-up 1A, 2026-10-02): swarm-api previews an issue with the
# caller's tenant's forge token, so it reads every tenant's `-git` secret --
# that secret alone, in its own authoritative binding, beside the tenant's
# worker. Not a provider key, not an App key, not a project grant.
run "swarm_api_reads_each_tenants_git_secret_and_no_other" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers = ["anthropic", "git", "git-merge"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
        accessor_overrides = {
          git-merge = ["serviceAccount:swarm-eng-merge@saga-agents-staging.iam.gserviceaccount.com"]
        }
      }
      research = {
        providers = ["git"]
        accessor  = "serviceAccount:swarm-agent-worker-research@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
    forge_readers = ["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"]
  }

  assert {
    condition = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-git"].members == toset([
      "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com",
      "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
    ])
    error_message = "a tenant's -git secret is read by its worker and by swarm-api, and by nobody else"
  }

  assert {
    condition     = contains(google_secret_manager_secret_iam_binding.accessor["swarm-tenant-research-git"].members, "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com")
    error_message = "the preview reads every tenant's -git secret, each by its own binding"
  }

  assert {
    condition     = !contains(google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-anthropic"].members, "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com")
    error_message = "swarm-api must never read a tenant's provider key"
  }

  assert {
    condition     = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-git-merge"].members == toset(["serviceAccount:swarm-eng-merge@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "the merge App key keeps its sole reader; swarm-api is not added to it"
  }
}

run "a_forge_reader_must_be_a_service_account" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers = ["git"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
    forge_readers = ["user:someone@saga.xyz"]
  }

  expect_failures = [var.forge_readers]
}
