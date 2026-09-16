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
