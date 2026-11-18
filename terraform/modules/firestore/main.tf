# Firestore control-plane database.
#
# Every durable fact the swarm owns lives here: tasks, leases, pools, quota,
# tenants, workflows. Infrastructure is downstream of these documents, never
# upstream of them -- a lease exists before a container does, and a container
# without a live lease is a bug the reconciler cleans up.

resource "google_firestore_database" "this" {
  project     = var.project_id
  name        = var.database_id
  location_id = var.location
  type        = "FIRESTORE_NATIVE"

  concurrency_mode = var.concurrency_mode

  point_in_time_recovery_enablement = (
    var.point_in_time_recovery ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"
  )

  delete_protection_state = (
    var.delete_protection ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  )

  deletion_policy = var.deletion_policy

  app_engine_integration_mode = "DISABLED"

  lifecycle {
    precondition {
      condition     = var.database_id != "(default)"
      error_message = "Refusing to manage the (default) database in a shared project."
    }
  }
}
