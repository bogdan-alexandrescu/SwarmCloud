# Firestore's managed daily backup of the platform database (#1818, the owner's
# option b, 2026-10-21).
#
# Why a backup on top of point-in-time recovery: point-in-time versions are
# part of the database and are deleted with it, so until this existed one
# `gcloud firestore databases delete` (or a console click) was total loss. A
# backup is not: "Deleting the source database does not automatically delete
# related backups" (Firestore backups doc). With a daily schedule a deleted
# database loses at most 24 h.
#
# Why 7 days and not 14 weeks: an offboarded tenant's documents stay in every
# backup taken before step 6 of docs/runbooks/tenant-offboarding.md. 7 days is
# the point-in-time window that already keeps them, so the backup adds a copy
# but no longer a stay. Longer retention is the owner's call (option c on #1818).
#
# Only the platform database: the bench database (bench.tf) is disposable and
# gets none, and `daily_backup = false` lets another instance of this module
# opt out.
#
# MANAGED-BY. google_firestore_backup_schedule has no labels attribute in the
# pinned provider, so it is in scripts/lib/unlabelable-types.json.

resource "google_firestore_backup_schedule" "daily" {
  count = var.daily_backup ? 1 : 0

  project  = var.project_id
  database = google_firestore_database.this.name

  retention = var.backup_retention

  daily_recurrence {}
}
