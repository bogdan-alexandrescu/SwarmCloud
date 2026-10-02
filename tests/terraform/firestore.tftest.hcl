# Firestore is the control plane's only durable state, and in a SHARED project
# the database name is a safety property: `(default)` belongs to whoever asks
# for it next, and a project gets exactly one.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"
}

run "named_database_in_region" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  assert {
    condition     = google_firestore_database.this.name == "swarm"
    error_message = "the control plane uses the NAMED `swarm` database"
  }

  assert {
    condition     = google_firestore_database.this.name != "(default)"
    error_message = "(default) must stay free for other teams in this shared project"
  }

  assert {
    condition     = google_firestore_database.this.location_id == "us-central1"
    error_message = "the database must be co-located with the workloads"
  }

  assert {
    condition     = google_firestore_database.this.type == "FIRESTORE_NATIVE"
    error_message = "Datastore mode has no transactions of the shape admission needs"
  }

  assert {
    condition     = google_firestore_database.this.concurrency_mode == "PESSIMISTIC"
    error_message = "admission relies on the losing transaction being aborted and retried, not on a client-visible contention error"
  }

  assert {
    condition     = google_firestore_database.this.delete_protection_state == "DELETE_PROTECTION_ENABLED"
    error_message = "delete protection defaults on: this database is the whole control plane"
  }
}

run "default_database_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    database_id = "(default)"
  }

  expect_failures = [var.database_id]
}

run "scheduler_drain_index_is_state_priority_desc_created_asc" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  # The scheduler's hot path is exactly one query:
  #   tasks where state == READY order by priority DESC, created_at ASC
  # Firestore has no query planner: without this index the query does not run
  # slowly, it fails.
  assert {
    condition     = length(output.scheduler_index_fields) == 3
    error_message = "the drain-loop index must have exactly three fields"
  }

  assert {
    condition     = output.scheduler_index_fields[0].field_path == "state" && output.scheduler_index_fields[0].order == "ASCENDING"
    error_message = "state must lead the drain-loop index: it is the equality filter"
  }

  assert {
    condition     = output.scheduler_index_fields[1].field_path == "priority" && output.scheduler_index_fields[1].order == "DESCENDING"
    error_message = "priority must be DESCENDING, or high-priority work sorts last"
  }

  assert {
    condition     = output.scheduler_index_fields[2].field_path == "created_at" && output.scheduler_index_fields[2].order == "ASCENDING"
    error_message = "created_at ASCENDING is what makes the queue FIFO within a priority band, so nothing starves"
  }

  assert {
    condition     = contains(output.index_names, "tasks-tenant-state-priority-created")
    error_message = "the tenant-scoped variant must exist: the API may never return another tenant's task"
  }

  assert {
    condition     = contains(output.index_names, "tasks-state-next-eligible")
    error_message = "parked work is woken by a state + next_eligible_at query"
  }

  assert {
    condition     = contains(output.index_names, "leases-state-dispatch-deadline")
    error_message = "the reconciler finds leases admitted but never dispatched by state + dispatch_deadline"
  }
}

run "every_tenant_scoped_collection_has_a_tenant_leading_index" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  # Firestore cannot filter a result set after the fact. A listing that is meant
  # to be tenant-scoped needs tenant_id INSIDE the index, or the only way to
  # honour the boundary is to over-read and drop rows in the app.
  assert {
    condition = alltrue([
      for name in ["tasks-tenant-created", "tasks-tenant-state-updated", "leases-tenant-created", "attempts-tenant-created", "events-tenant-at", "workflows-tenant-state-created"] :
      contains(output.index_names, name)
    ])
    error_message = "a tenant-scoped index is missing; some list endpoint would have to filter in the application"
  }
}

run "the_outcome_ledger_has_its_index_and_its_exemptions" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  # GET /v1/outcomes derives each tenant-day from
  #   tasks where tenant_id == T and D <= completed_at < D+1 order by completed_at
  # Without this index that query does not run slowly; it fails, and every day
  # comes back unread with read_failed.
  assert {
    condition     = contains(output.index_names, "tasks-tenant-completed")
    error_message = "the outcome ledger's ended-tasks query needs tasks-tenant-completed"
  }

  assert {
    condition = (
      google_firestore_index.this["tasks-tenant-completed"].collection == "tasks" &&
      google_firestore_index.this["tasks-tenant-completed"].fields[0].field_path == "tenant_id" &&
      google_firestore_index.this["tasks-tenant-completed"].fields[1].field_path == "completed_at" &&
      google_firestore_index.this["tasks-tenant-completed"].fields[1].order == "ASCENDING"
    )
    error_message = "tenant_id must lead (the equality filter, and the tenant boundary), then completed_at ASCENDING"
  }

  # The rollup's two payload fields are ~700 KB JSON strings that are only ever
  # read by document id; indexing them is pure write amplification.
  assert {
    condition = alltrue([
      for f in ["ended", "arrived"] :
      google_firestore_field.outcome_days_unindexed[f].collection == "outcome_days" &&
      google_firestore_field.outcome_days_unindexed[f].field == f
    ])
    error_message = "outcome_days.ended and outcome_days.arrived must be exempt from single-field indexing"
  }
}

run "account_hold_records_have_their_index_and_their_ttl" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  # The quota broker's history route runs exactly one query:
  #   account_holds where account_id == A and assigned_at in [F, T)
  #   order by assigned_at DESC
  # Without this index it fails rather than running slowly.
  assert {
    condition = (
      google_firestore_index.this["account-holds-account-assigned"].collection == "account_holds" &&
      google_firestore_index.this["account-holds-account-assigned"].fields[0].field_path == "account_id" &&
      google_firestore_index.this["account-holds-account-assigned"].fields[1].field_path == "assigned_at" &&
      google_firestore_index.this["account-holds-account-assigned"].fields[1].order == "DESCENDING"
    )
    error_message = "account_holds needs (account_id, assigned_at DESC): account_id is the equality filter, assigned_at the window and the order"
  }

  # Records name tasks; they are kept 90 days, not for ever.
  assert {
    condition = (
      google_firestore_field.account_holds_ttl.collection == "account_holds" &&
      google_firestore_field.account_holds_ttl.field == "expires_at"
    )
    error_message = "account_holds must expire on expires_at, the field quota_broker.accounts.hold_log_entry writes"
  }
}

run "account_hold_ttl_survives_turning_the_audit_ttls_off" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    event_ttl_field = ""
  }

  assert {
    condition     = google_firestore_field.account_holds_ttl.field == "expires_at"
    error_message = "disabling the audit-trail TTLs must not make hold records, which name tasks, permanent"
  }
}

run "pools_and_tenants_are_materialised_at_provisioning_time" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    pools = {
      "global"      = { hard_limit = 20 }
      "tenant:eng"  = { hard_limit = 10 }
      "runner:mock" = { hard_limit = 20 }
    }
    tenant_documents = {
      eng = { kind = "group", principal = "eng@saga.xyz", credentials = ["anthropic"] }
    }
  }

  # evaluate_capacity() treats a MISSING pool document as unlimited. The global
  # pool must therefore exist before the first scheduler pass, or that pass
  # admits with no ceiling at all.
  assert {
    condition     = contains(output.pool_names, "global")
    error_message = "the global pool document must exist before the first scheduler pass"
  }

  assert {
    condition     = google_firestore_document.pool["global"].collection == "pools"
    error_message = "pool documents live in the pools collection"
  }

  assert {
    condition     = strcontains(google_firestore_document.pool["global"].fields, "\"active\":{\"integerValue\":\"0\"}")
    error_message = "a freshly created pool starts with zero active leases"
  }

  assert {
    condition     = contains(output.tenant_document_ids, "eng")
    error_message = "tenants declared in tfvars must exist in Firestore before the API resolves anyone into them"
  }
}

run "global_pool_may_not_be_omitted" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    pools = {
      "tenant:eng" = { hard_limit = 10 }
    }
  }

  expect_failures = [var.pools]
}

# The contention bench database (S32, OD-B17-1). Off unless an environment
# names it, a copy of the live database's locking model when on, and carrying
# managed-by the only way a google_firestore_database can (it has no labels).
run "no_bench_database_by_default" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  assert {
    condition     = length(google_firestore_database.bench) == 0 && length(google_firestore_document.bench_managed_by) == 0
    error_message = "the bench database is opt-in: an environment that does not name one must get none"
  }

  assert {
    condition     = output.bench_database_name == null
    error_message = "no bench database means no bench database name"
  }
}

run "bench_database_matches_the_live_locking_model" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    bench_database_id = "swarm-bench"
  }

  assert {
    condition     = google_firestore_database.bench[0].name == "swarm-bench"
    error_message = "the bench database must carry the name the harness accepts"
  }

  assert {
    condition     = google_firestore_database.bench[0].concurrency_mode == google_firestore_database.this.concurrency_mode
    error_message = "a bench on a different concurrency mode measures a different locking model"
  }

  assert {
    condition     = google_firestore_database.bench[0].type == "FIRESTORE_NATIVE" && google_firestore_database.bench[0].location_id == google_firestore_database.this.location_id
    error_message = "the bench database must have the live database's type and location"
  }

  assert {
    condition     = google_firestore_database.bench[0].delete_protection_state == "DELETE_PROTECTION_DISABLED"
    error_message = "the bench database is disposable"
  }

  assert {
    condition     = google_firestore_document.bench_managed_by[0].database == "swarm-bench" && strcontains(google_firestore_document.bench_managed_by[0].fields, "\"managed_by\":{\"stringValue\":\"swarm-terraform\"}")
    error_message = "the bench database must carry managed-by=swarm-terraform (as a document: the type has no labels)"
  }
}

run "a_bench_database_with_a_live_name_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    bench_database_id = "swarm"
  }

  expect_failures = [var.bench_database_id]
}
