# Workspace secret release rows and marks expire 90 days after they are
# written (owner decision Q6, #1347). swarm_api stamps both with `expire_at`
# (workspace_secrets.RELEASE_RETENTION); without a TTL policy that field is
# inert and both collections grow without bound. The collection and field
# names are held to swarm_api's constants by
# tests/unit/control_plane/test_workspace_secrets_ttl_parity.py.

mock_provider "google" {}

run "release_rows_and_marks_expire_on_expire_at" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    project_id = "saga-agents-staging"
  }

  assert {
    condition = (
      toset(keys(google_firestore_field.workspace_secret_release_ttl)) == toset(["workspace_secret_releases", "workspace_secret_release_marks"])
      && alltrue([
        for c, f in google_firestore_field.workspace_secret_release_ttl :
        f.collection == c && f.field == "expire_at" && length(f.ttl_config) == 1
      ])
    )
    error_message = "workspace_secret_releases.expire_at and workspace_secret_release_marks.expire_at each need a TTL policy (Q6), or both collections keep every release for ever"
  }

  # Nothing queries or orders on expire_at, so the default single-field index
  # is switched off; a query added later on it must turn the index back on.
  assert {
    condition = alltrue([
      for c, f in google_firestore_field.workspace_secret_release_ttl :
      length(f.index_config) == 1 && length(f.index_config[0].indexes) == 0
    ])
    error_message = "the release TTL fields carry an empty index_config: nothing queries on expire_at"
  }
}

run "the_release_ttls_survive_the_audit_ttl_switch" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    project_id      = "saga-agents-staging"
    event_ttl_field = ""
  }

  assert {
    condition     = length(google_firestore_field.workspace_secret_release_ttl) == 2 && length(google_firestore_field.events_ttl) == 0
    error_message = "switching off the audit-trail TTLs must leave the release TTLs in place"
  }
}
