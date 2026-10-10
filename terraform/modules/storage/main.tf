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

  # WHAT NEARLINE MAY TOUCH: every mapped tenant's tasks/ and verdicts/, and
  # never its repos/ (docs/repo-index.md §2.3, owner decision 2026-10-06). A
  # GCS lifecycle condition can only INCLUDE prefixes, never exclude one, so
  # the cold-stored prefixes are listed per tenant from the tenants map
  # instead of the old single `tenants/`. Under repos/ live the index copies
  # and the content-addressed graph blobs that planners read on every run and
  # that stay referenced for months: Nearline would charge a retrieval fee on
  # each read and a 30-day minimum on each deletion. Retention there is the
  # index-run sweep's (images/agent-runtime-indexer/repo-index/
  # repo_graph_shards.py `sweep`): the last 20 versions, then unreferenced
  # blobs.
  #
  # This list governs Nearline ONLY. The customTime Delete below stays
  # bucket-wide, so every tenant's task objects still expire -- including
  # the personal tenants (`u-<slug>`) the API creates at runtime, which are
  # never in this map. Those tenants' task objects are simply not
  # cold-stored: a storage-class cost difference, nothing kept forever.
  # repos/ is off the Delete clock because the worker never stamps customTime
  # under it (agent_worker.objectstore `is_repos_key`), the same way a
  # checkpoint is.
  aged_prefixes = flatten([
    for t in var.tenants : ["tenants/${t}/tasks/", "tenants/${t}/verdicts/"]
  ])
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

  # Cold-store what nobody reads: tasks/ and verdicts/, never repos/ (see
  # `aged_prefixes`). With no tenant the rule is omitted, because a
  # condition with an empty matches_prefix matches EVERY object.
  dynamic "lifecycle_rule" {
    for_each = length(local.aged_prefixes) > 0 ? [local.aged_prefixes] : []
    content {
      condition {
        age            = var.nearline_after_days
        with_state     = "LIVE"
        matches_prefix = lifecycle_rule.value
      }
      action {
        type          = "SetStorageClass"
        storage_class = "NEARLINE"
      }
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
  #
  # OBJECTS UPLOADED BEFORE THIS RULE carry no customTime either, so once it is
  # applied they too are kept until deleted by name. A one-time backfill puts
  # the existing artifacts and logs back on the clock, stamping their upload
  # time and skipping every key under an `attempts/<a>/checkpoints/` directory
  # (the same test `is_checkpoint_key` makes):
  #
  #   gcloud storage ls "gs://<bucket>/tenants/**" \
  #     | grep -v '/attempts/[^/]*/checkpoints/' \
  #     | while read -r url; do
  #         created="$(gcloud storage objects describe "$url" --format='value(creation_time)')"
  #         gcloud storage objects update "$url" --custom-time="$created"
  #       done
  #
  # Stamping a checkpoint by mistake would put it back on the clock this rule
  # exists to take it off, which is why the filter is on the key and not a guess.
  #
  # BUCKET-WIDE ON PURPOSE (lane IX3). A per-tenant prefix list would drop
  # every runtime personal tenant off the clock, keeping its task objects
  # forever. repos/ is protected instead by carrying no customTime: the
  # worker does not stamp anything under tenants/<t>/repos/
  # (agent_worker.objectstore `is_repos_key`), and the graph writer uploads
  # without one, so this rule cannot match an index copy or a graph blob.
  lifecycle_rule {
    condition {
      days_since_custom_time = var.artifact_retention_days
      with_state             = "LIVE"
    }
    action {
      type = "Delete"
    }
  }

  # Clone bundles and branch-head pointers expire on an `age` clock (issue
  # #940, docs/clone-bundles.md). The worker writes a one-commit bundle to
  # tenants/<t>/bundles/<repo_id>/<sha>.swarm-clone.bundle after a clone, and
  # a later step that clones that sha reads it instead of contacting GitHub.
  # It is a cache, useful for hours: a miss costs only the old clone.
  #
  # The customTime Delete above would keep a bundle artifact_retention_days
  # (180 in prod), so this rule is separate and keyed on `age`.
  #
  # BUCKET-WIDE BY SUFFIX ON PURPOSE. A matches_prefix list cannot reach the
  # personal `u-<slug>` tenants the API creates at runtime, for the same
  # reason the customTime rule is bucket-wide. The two suffixes are written
  # only by the bundle writer, so no checkpoint, repos/ object, artifact or
  # verdict ends with either; this is the one `age` Delete that cannot reach
  # a PARKED task's checkpoint (tests/terraform/clone_bundle_lifecycle.tftest.hcl).
  # Bundles are not on the Nearline rule: they are read soon after writing.
  #
  # with_state = "ANY", stated rather than left to the provider's default (a
  # computed value no plan-time test can read): a head pointer is overwritten
  # per push, and its noncurrent versions are as stale as the live one.
  lifecycle_rule {
    condition {
      age            = var.clone_bundle_retention_days
      matches_suffix = [".swarm-clone.bundle", ".swarm-clone.head"]
      with_state     = "ANY"
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
