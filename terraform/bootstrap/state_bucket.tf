# Terraform state.
#
# Four properties, each protecting against a specific way of losing a platform:
#
#   versioning         a corrupted or truncated state file is recoverable,
#                      because the previous ones are still there.
#   UBLA               object ACLs cannot quietly widen access to state, which
#                      contains every resource id and some sensitive values.
#   public access
#     prevention       enforced, so no future IAM change can make it public.
#   prevent_destroy    `terraform destroy` on this root cannot delete the bucket
#                      holding the state of everything else.
#
# There is deliberately no retention_policy: it would make objects immutable for
# its duration, and terraform rewrites the state object on every apply.

resource "google_storage_bucket" "state_logs" {
  project  = var.project_id
  name     = local.log_bucket_name
  location = var.location

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  force_destroy = false

  # checkov:skip=CKV_GCP_62:This IS the access-log sink. Pointing it at itself would log its own writes forever; pointing it at a third bucket only moves the same question one hop.
  # checkov:skip=CKV_GCP_78:Access logs are append-only by nature and are expired by lifecycle; versioning them doubles cost for no recovery value.
  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age = 400
    }
    action {
      type = "Delete"
    }
  }

  labels = merge(local.labels, { "swarm-purpose" = "state-access-logs" })
}

resource "google_storage_bucket" "state" {
  project  = var.project_id
  name     = local.state_bucket_name
  location = var.location

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Never true. This bucket holds the state of every other root.
  force_destroy = false

  versioning {
    enabled = true
  }

  logging {
    log_bucket        = google_storage_bucket.state_logs.name
    log_object_prefix = "tfstate"
  }

  # Keep a deep history, but not an infinite one.
  lifecycle_rule {
    condition {
      num_newer_versions = var.state_noncurrent_versions_to_keep
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = var.state_noncurrent_retention_days
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

  # A bucket deleted by accident is a platform that can no longer be managed.
  soft_delete_policy {
    retention_duration_seconds = 2592000
  }

  labels = merge(local.labels, { "swarm-purpose" = "terraform-state" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket_iam_member" "log_writer" {
  bucket = google_storage_bucket.state_logs.name
  role   = "roles/storage.objectCreator"
  member = "group:cloud-storage-analytics@google.com"
}
