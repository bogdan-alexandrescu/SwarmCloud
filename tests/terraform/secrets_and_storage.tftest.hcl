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
