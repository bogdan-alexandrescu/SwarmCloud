# Artifact and checkpoint storage.
#
# Two buckets. The access-log bucket exists so the artifact bucket can log to
# something that is not itself, which would be a loop that grows forever.
#
# Tenant isolation is by object prefix -- `tenants/<tenant_id>/...`, exactly the
# layout agent_worker.config.WorkerConfig.gcs_prefix produces -- and enforced by
# IAM conditions in the tenancy module, not by convention.

locals {
  artifact_bucket_name = "${var.name_prefix}-artifacts-${var.bucket_suffix}"
  log_bucket_name      = "${var.name_prefix}-access-logs-${var.bucket_suffix}"
}

resource "google_storage_bucket" "access_logs" {
  # checkov:skip=CKV_GCP_62:This IS the access-log sink. Pointing it at itself would log its own writes forever; pointing it at a third bucket only moves the same question one hop.
  # checkov:skip=CKV_GCP_78:Access logs are append-only by nature and are expired by lifecycle rule; versioning them doubles the cost for no recovery value.

  project  = var.project_id
  name     = local.log_bucket_name
  location = var.location

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  force_destroy = var.force_destroy

  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age = var.log_retention_days
    }
    action {
      type = "Delete"
    }
  }

  dynamic "soft_delete_policy" {
    for_each = var.soft_delete_retention_seconds > 0 ? [1] : []
    content {
      retention_duration_seconds = var.soft_delete_retention_seconds
    }
  }

  dynamic "encryption" {
    for_each = var.kms_key_name == "" ? [] : [var.kms_key_name]
    content {
      default_kms_key_name = encryption.value
    }
  }

  labels = merge(var.labels, { "swarm-purpose" = "access-logs" })
}

resource "google_storage_bucket" "artifacts" {
  project  = var.project_id
  name     = local.artifact_bucket_name
  location = var.location

  storage_class = "STANDARD"

  # No ACLs. Every grant is an IAM binding, which is the only way the per-tenant
  # prefix conditions can be expressed at all.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # A checkpoint is the only thing standing between an interrupted two-hour run
  # and starting over, so destroy must never take the bucket with it.
  force_destroy = var.force_destroy

  versioning {
    enabled = true
  }

  logging {
    log_bucket        = google_storage_bucket.access_logs.name
    log_object_prefix = "${var.name_prefix}-artifacts"
  }

  # Cold-store what nobody reads.
  lifecycle_rule {
    condition {
      age            = var.nearline_after_days
      with_state     = "LIVE"
      matches_prefix = ["tenants/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  # Artifacts and logs expire on a clock; checkpoints do not.
  #
  # Keyed on customTime, never on `age`. The worker stamps customTime on every
  # object it uploads EXCEPT a checkpoint's (agent_worker.objectstore
  # `is_checkpoint_key`), and GCS never matches days_since_custom_time against
  # an object with no customTime. The `age` rule this replaced could not see a
  # task, so it deleted a PARKED task's only checkpoint on the same day as a
  # finished task's leftovers -- 14 days in dev, while a provider quota window
  # can outlast that. A checkpoint is now removed only by reference, by
  # apps/reconciler/reconciler/checkpoints.py (`classify`), which keeps every
  # checkpoint of a non-terminal task and the one a FAILED task may resume
  # from, and floors the orphans it cannot attribute.
  #
  # An object written by anything other than the worker carries no customTime
  # either, and is therefore kept until something deletes it by name: the
  # tenant prefix marker scripts/register-tenant.sh writes (which the old rule
  # deleted after 14 days in dev), and anything an operator copies in by hand.
  lifecycle_rule {
    condition {
      days_since_custom_time = var.artifact_retention_days
      with_state             = "LIVE"
    }
    action {
      type = "Delete"
    }
  }

  # Versioning without expiry is how a bucket quietly becomes the largest line
  # on the bill.
  lifecycle_rule {
    condition {
      num_newer_versions = var.keep_noncurrent_versions
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = var.noncurrent_version_retention_days
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  dynamic "soft_delete_policy" {
    for_each = var.soft_delete_retention_seconds > 0 ? [1] : []
    content {
      retention_duration_seconds = var.soft_delete_retention_seconds
    }
  }

  dynamic "encryption" {
    for_each = var.kms_key_name == "" ? [] : [var.kms_key_name]
    content {
      default_kms_key_name = encryption.value
    }
  }

  labels = merge(var.labels, { "swarm-purpose" = "artifacts" })
}

# The access-log bucket is written by a Google-owned service agent, not by any
# swarm identity.
resource "google_storage_bucket_iam_member" "log_writer" {
  bucket = google_storage_bucket.access_logs.name
  role   = "roles/storage.objectCreator"
  member = "group:cloud-storage-analytics@google.com"
}
