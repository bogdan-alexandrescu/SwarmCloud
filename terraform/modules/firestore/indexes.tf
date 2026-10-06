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
    # Firestore serves `<equality filters> ORDER BY created_at DESC` ONLY from an
    # index whose ordered field follows the equality fields IMMEDIATELY. A
    # near-miss index does not degrade the query, it fails it with
    # FAILED_PRECONDITION, so each of these four maps to a required endpoint
    # that returns 500 without it.

    # GET /v1/tasks?state=...   (tasks-tenant-state-updated orders on updated_at;
    # tasks-tenant-state-priority-created puts priority between state and created_at)
    "tasks-tenant-state-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # GET /v1/workflows/{id} -- lists that workflow's tasks, tenant-scoped.
    "tasks-tenant-workflow-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "workflow_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # GET /v1/tasks?parent_task_id=... -- a parent's children, tenant-scoped
    # (contract request 14, docs/design/child-tasks.md §6.3). The same equality
    # pair also serves the children route's dedupe and fan-out count, the API's
    # cancel cascade and the scheduler's await sweep.
    "tasks-tenant-parent-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "parent_task_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # The scheduler's child-cascade sweep: every live child, `state in [...]`
    # and `parent_task_id > ""`, ordered by parent then document id so the
    # sweep pages through them (docs/design/child-tasks.md §3.4 step 2).
    "tasks-state-parent" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "state", order = "ASCENDING" },
        { field_path = "parent_task_id", order = "ASCENDING" },
      ]
    }

    # GET /v1/tasks?runner_profile=...
    "tasks-tenant-runner-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "runner_profile", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # GET /v1/workflows -- workflows-tenant-state-created has state in the middle.
    "workflows-tenant-created" = {
      collection  = "workflows"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    "tasks-tenant-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # GET /v1/tasks?submitted_by=... (U5) -- the owner filter, applied IN the
    # query when it is the only filter besides the tenant, so pages are full
    # and the cursor is exact. Combined with state, workflow_id or
    # runner_profile it is a post-filter instead (Store.list_tasks), so no
    # four-field index is needed. A google_firestore_index takes no labels, so
    # managed-by=swarm-terraform cannot be written on it; the type is listed in
    # scripts/lib/unlabelable-types.json (:78) for the destroy and plan guards.
    "tasks-tenant-submitter-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "submitted_by", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # GET /v1/admin/failures (U19) -- FAILED tasks across every tenant, newest
    # first. The one task listing with no tenant filter (full admin only), so
    # no tenant-leading index can serve it. No labels possible, as above
    # (scripts/lib/unlabelable-types.json:78).
    "tasks-state-created" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "state", order = "ASCENDING" },
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

    # ---- GET /v1/outcomes: one tenant's tasks that ENDED in a UTC day -------
    # swarm_api.outcomes derives each tenant-day from
    #   tasks where tenant_id == T and D <= completed_at < D+1
    #   order by completed_at ASC
    # and folds a live day's delta with the same query from its cut. Arrivals
    # (created_at) are served by tasks-tenant-created above. ASCENDING because
    # the derive and the delta both read forwards from a cut.
    "tasks-tenant-completed" = {
      collection  = "tasks"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "completed_at", order = "ASCENDING" },
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

    # ---- which account each agent runs on (#379) ----------------------------
    # swarm_api.task_accounts: tenant_id == T AND task_id IN <<=30 ids> AND
    # "account_" <= detail.cause < "account`". The cause is a RANGE, so it is
    # the last field: Firestore serves an inequality only from an index whose
    # final field it is. One query per 30 rows of a task list page.
    "events-tenant-task-cause" = {
      collection  = "events"
      query_scope = "COLLECTION_GROUP"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "task_id", order = "ASCENDING" },
        { field_path = "detail.cause", order = "ASCENDING" },
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

    # ---- account_holds (#379) ------------------------------------------------
    # GET /v1/accounts/{id}/holds/history on the quota broker:
    #   account_holds where account_id == A and F <= assigned_at < T
    #   order by assigned_at DESC
    # An equality on one field and a range plus order on another: Firestore
    # serves that only from a composite index, and without it the history tab
    # does not load slowly -- it fails.
    "account-holds-account-assigned" = {
      collection  = "account_holds"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "account_id", order = "ASCENDING" },
        { field_path = "assigned_at", order = "DESCENDING" },
      ]
    }

    # ---- issue runs (#454) ---------------------------------------------------
    # GET /v1/runs -- the caller's tenant's runs, newest first
    # (swarm_api.issueruns.IssueRuns.list). The collection is the run
    # document's own, tenant-scoped like tasks, so tenant_id leads.
    "issue-runs-tenant-created" = {
      collection  = "issue_runs"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "created_at", order = "DESCENDING" },
      ]
    }

    # POST /v1/admin/runs/advance -- the per-tenant Cloud Scheduler tick
    # (swarm_api.issueruns.IssueRuns.tickable): the tenant's runs in a state a
    # tick can move, OLDEST first so none is starved.
    #   issue_runs where tenant_id == T and state in [...] order by created_at
    "issue-runs-tenant-state-created" = {
      collection  = "issue_runs"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
        { field_path = "created_at", order = "ASCENDING" },
      ]
    }

    # The same tick's second query: the PLANNED runs whose approval is `auto`,
    # which the tick approves. A PLANNED `required` run waits for a person and
    # is deliberately not read.
    #   issue_runs where tenant_id == T and state == PLANNED
    #     and plan_approval == auto order by created_at
    "issue-runs-tenant-state-approval-created" = {
      collection  = "issue_runs"
      query_scope = "COLLECTION"
      fields = [
        { field_path = "tenant_id", order = "ASCENDING" },
        { field_path = "state", order = "ASCENDING" },
        { field_path = "plan_approval", order = "ASCENDING" },
        { field_path = "created_at", order = "ASCENDING" },
      ]
    }

    # ---- workflows ----------------------------------------------------------
    # GET /v1/workflows?active=true / ?state=... -- `tenant_id ==, state IN
    # [...] ORDER BY created_at DESC` (Store.list_workflows, stored_states).
    # The IN lists every stored state but the final ones, because the stored
    # state is a cache that can lag the steps; the route derives each row
    # after. It is what makes a tenant's running workflows one page however
    # long its finished history is (owner decision 2026-10-06, P4).
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

# Reconciliation passes are a record of what the platform repaired, written once
# a minute. Without a TTL that is ~525,000 documents a year, and the value of a
# pass decays fast -- it answers "what happened last Tuesday", not "what is
# true". The reconciler sets `expires_at` from PASS_RETENTION_HOURS.
resource "google_firestore_field" "reconciler_passes_ttl" {
  count = var.event_ttl_field == "" ? 0 : 1

  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "reconciler_passes"
  field      = var.event_ttl_field

  ttl_config {}

  # Same reasoning as events: the default single-field index on a TTL field is
  # not useful and costs write amplification on every pass.
  index_config {}
}

# The outcome rollup (swarm_api.outcomes) keeps each tenant-day's tuples as ONE
# JSON string field per kind, up to ~700 KB. Firestore indexes every field by
# default, and a single-field index entry on a string that long is both
# useless -- every read of outcome_days is a get_all of deterministic ids, never
# a query on these fields -- and a write amplification on every rollup write.
# Past 1,500 bytes Firestore also stops indexing the value anyway. So both are
# exempted, the events_ttl way: index_config {} with no ttl_config. tenant_id,
# day and sealed keep their default indexes, so the collection can still be
# listed by tenant.
resource "google_firestore_field" "outcome_days_unindexed" {
  for_each = toset(["ended", "arrived"])

  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "outcome_days"
  field      = each.value

  index_config {}
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

# Account hold records (#379) name which task held which subscription, and are
# kept to answer "who was on this account" over a billing month -- not for
# ever. The quota broker sets `expires_at` to released_at + 90 days when a hold
# ends, and to the hold's own deadline + 90 days while it is open, so a record
# whose hold is never closed still ages out. The field name is the broker's
# (quota_broker.accounts.hold_log_entry), not var.event_ttl_field: that
# variable switches off the AUDIT-trail TTLs, and turning it off must not make
# this collection keep task ids indefinitely.
resource "google_firestore_field" "account_holds_ttl" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "account_holds"
  field      = "expires_at"

  ttl_config {}

  # Never queried on; the history query orders on assigned_at. The default
  # single-field index would be write amplification on every hold.
  index_config {}
}
