# The admission-contention bench database (S32, owner decision OD-B17-1).
#
# scripts/bench-contention.sh drives the real frozen admission against pool
# documents named `global`, `tenant:...` -- the names pool_names_for() returns.
# Against the live database that would overwrite live limits and counters, so
# the harness refuses every database that is not `swarm-bench[-suffix]`, and
# this is where that database comes from. OFF by default: only an environment
# that sets `bench_database_id` gets one (dev, not prod).
#
# Same location, type and concurrency_mode as the live database above, because
# a bench on a different locking model measures a different system. No point-in-
# time recovery and no delete protection: everything in it is disposable, and a
# `terraform destroy` or turning the switch off removes it outright.
#
# MANAGED-BY. google_firestore_database has no labels attribute in the pinned
# provider (it is in scripts/lib/unlabelable-types.json, as the live database
# is), so the database cannot carry managed-by=swarm-terraform as a label. It
# carries it as the `bench_meta/managed-by` document below instead, which the
# harness's cleanup never deletes (it deletes only documents its own run wrote).

locals {
  bench_enabled = var.bench_database_id != ""
}

resource "google_firestore_database" "bench" {
  count = local.bench_enabled ? 1 : 0

  project     = var.project_id
  name        = var.bench_database_id
  location_id = var.location
  type        = google_firestore_database.this.type

  concurrency_mode = google_firestore_database.this.concurrency_mode

  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_DISABLED"
  delete_protection_state           = "DELETE_PROTECTION_DISABLED"
  deletion_policy                   = "DELETE"

  app_engine_integration_mode = "DISABLED"

  lifecycle {
    precondition {
      condition     = var.bench_database_id != var.database_id
      error_message = "The bench database must not be the live control-plane database."
    }
  }
}

resource "google_firestore_document" "bench_managed_by" {
  count = local.bench_enabled ? 1 : 0

  project     = var.project_id
  database    = google_firestore_database.bench[0].name
  collection  = "bench_meta"
  document_id = "managed-by"

  fields = jsonencode({
    managed_by = { stringValue = "swarm-terraform" }
    purpose    = { stringValue = "scripts/bench-contention.sh (S32)" }
  })
}
