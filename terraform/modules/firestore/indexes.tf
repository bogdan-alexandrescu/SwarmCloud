# Composite indexes.
#
# Firestore has no joins and no ad-hoc query planner: a query either has an
# index or it fails. The scheduler's hot path is one query, so the first index
# below is the one the whole platform's throughput rests on:
#
#     tasks where state == READY order by priority DESC, created_at ASC
#
# priority DESC then created_at ASC is what makes the queue fair as well as
# prioritised -- within a priority band it is strictly FIFO, so a low-priority
# task cannot be starved forever by a steady trickle of its own peers.
#
# Every tenant-scoped variant is duplicated with tenant_id leading, because the
# API must never return another tenant's task and Firestore cannot filter a
# result set after the fact without an index that already includes the field.

locals {
  indexes = {
    # ---- the scheduler drain loop -------------------------------------------
    "tasks-state-priority-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "state", order = "ASCENDING" },
        { field_path = "priority", order = "DESCENDING" },
        { field_path = "created_at", order = "ASCENDING" },
      ]
    }

    # ---- the same query, scoped to one tenant -------------------------------
    "tasks-tenant-state-priority-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
        { field_path = "priority", order = "DESCENDING" },
        { field_path = "created_at", order = "ASCENDING" },
      ]
    }

    # ---- waking PARKED work whose retry window has arrived ------------------
    "tasks-state-next-eligible" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "state", order = "ASCENDING" },
        { field_path = "next_eligible_at", order = "ASCENDING" },
      ]
    }

    # ---- API listing: a tenant's own tasks, newest first --------------------
    "tasks-tenant-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    "tasks-tenant-state-updated" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
        { field_path = "updated_at", order = "DESCENDING" },
      ]
    }

    # ---- workflow fan-out: the steps of one workflow ------------------------
    "tasks-workflow-step" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "workflow_id", order = "ASCENDING" },
        { field_path = "created_at", order = "ASCENDING" },
      ]
    }

    # ---- reconciler: leases that were admitted but never dispatched ---------
    "leases-state-dispatch-deadline" = {
      collection  = "leases"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "state", order = "ASCENDING" },
        { field_path = "dispatch_deadline", order = "ASCENDING" },
      ]
    }

    # ---- reconciler: leases whose heartbeat stopped -------------------------
    "leases-released-expires" = {
      collection  = "leases"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "released_at", order = "ASCENDING" },
        { field_path = "expires_at", order = "ASCENDING" },
      ]
    }

    "leases-tenant-created" = {
      collection  = "leases"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # ---- attempts: the sizing-tuning report and per-task history ------------
    "attempts-task-created" = {
      collection  = "attempts"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "task_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    "attempts-tenant-created" = {
      collection  = "attempts"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # ---- event stream, across every task, scoped to a tenant ----------------
    "events-tenant-at" = {
      collection  = "events"
      query_scope = "COLLECTION_GROUP"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "at", order = "DESCENDING" },
      ]
    }

    "events-task-at" = {
      collection  = "events"
      query_scope = "COLLECTION_GROUP"
      fields = [
        { field_path = "task_id", order = "ASCENDING" },
        { field_path = "at", order = "ASCENDING" },
      ]
    }

    # ---- quota broker: per-provider health, per tenant ----------------------
    "quota-provider-updated" = {
      collection  = "quota"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "provider", order = "ASCENDING" },
        { field_path = "updated_at", order = "DESCENDING" },
      ]
    }

    "quota-tenant-state" = {
      collection  = "quota"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
      ]
    }

    # ---- workflows ----------------------------------------------------------
    "workflows-tenant-state-created" = {
      collection  = "workflows"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }
  }
}

resource "google_firestore_index" "this" {
  for_each = local.indexes

  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = each.value.collection
  query_scope = each.value.query_scope
  api_scope   = "ANY_API"

  dynamic "fields" {
    for_each = each.value.fields
    content {
      field_path = fields.value.field_path
      order      = fields.value.order
    }
  }
}

# Task events are an audit trail, not durable state. Without a TTL the
# subcollection grows without bound for the lifetime of the platform.
resource "google_firestore_field" "events_ttl" {
  count = var.event_ttl_field == "" ? 0 : 1

  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "events"
  field      = var.event_ttl_field

  ttl_config {}

  # The default single-field index on a TTL field is not useful and costs a
  # write amplification on every event.
  index_config {}
}
