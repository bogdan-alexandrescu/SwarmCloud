# Workspace secret release rows and marks (docs/design/workspace-secrets.md
# §4.4) are an audit record, kept 90 days by owner decision Q6 (#1347), not for
# ever. swarm_api stamps each with `expire_at` = at + 90 days when it writes it
# (workspace_secrets.RELEASE_RETENTION: release_row for the rows, the release
# route for the per-attempt marks). Without a policy that field is inert and
# both collections grow by one row per secret per attempt. A mark that ages
# out cannot re-open a release: the attempt it names has long since ended.
# The field name is swarm_api's, not var.event_ttl_field, for
# account_holds_ttl's reason: that variable switches off the audit-trail TTLs
# and must not make these keep for ever.
#
# google_firestore_field takes no labels, so managed-by=swarm-terraform cannot
# be carried here; like every TTL field in indexes.tf, it is owned by the
# database it sits on.
resource "google_firestore_field" "workspace_secret_release_ttl" {
  for_each = toset(["workspace_secret_releases", "workspace_secret_release_marks"])

  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = each.value
  field      = "expire_at"

  ttl_config {}

  # Never queried or ordered on: the release route reads a mark by its
  # attempt-id document key and writes rows by id, and nothing else reads
  # either collection. The default single-field index would be a write
  # amplification on every release. A query on expire_at added later must
  # turn this back on.
  index_config {}
}
