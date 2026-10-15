# The workspace-secret grant attestation key (docs/design/workspace-secrets.md,
# "What SEC-GRANT-ATTEST built"; owner decision 2026-10-15; part of #1347).
# The pattern is child_tasks.tf's.
#
# Three things, and only these:
#
#   * `swarm-workspace-grant-key`, the HMAC key swarm-api MACs every
#     workspace-secret grant with (swarm_api.grant_attest) and verifies it with
#     when it releases a secret. Its own key, not swarm-child-key: a grant is a
#     document a tenant's agent can write, and the key is what stops that write
#     widening what is released. Its container and its one reader are here; its
#     VALUE never is (scripts/create-secrets.sh --workspace-grant-key owns it);
#   * swarm-api's side: SWARM_WORKSPACE_GRANT_KEY by reference once served, and
#     SWARM_WORKSPACE_GRANT_KEY_PREVIOUS during a rotation;
#   * GRANT_ATTESTATION_REQUIRED, the switch that stops an UNATTESTED grant
#     being released. A grant whose attestation is present but wrong is
#     dropped whatever it says.
#
# WHO READS IT: swarm-api, nobody else. Not the scheduler, not a worker, not a
# person. An identity that could read it could sign a grant for any secret.
# The accessor binding is a secret-level binding this root already makes for
# swarm-child-key and swarm-typesafe-fwd-key, so it lives here, not in
# terraform/bootstrap: the deployer can grant on a secret it created.
#
# THE ORDER (docs/runbooks/workspace-grant-attestation.md):
#
#   1. apply: the container and its reader exist. Nothing references it.
#   2. scripts/create-secrets.sh --workspace-grant-key, then
#      workspace_grant_attestation.serve = true, apply: swarm-api signs every
#      grant it writes and verifies every attestation it is shown.
#   3. POST /v1/admin/workspace-secrets/attest-grants: every grant written
#      before step 2 whose fields match its audit row is signed; the rest are
#      listed by id for a person to re-tick.
#   4. workspace_grant_attestation.required = true, apply: an unattested grant
#      is released nothing.
#
# The container is NOT behind a switch: it is one empty secret and one binding,
# referenced by nothing until step 2, so creating it changes no behaviour.

variable "workspace_grant_attestation" {
  description = <<-EOT
    Workspace-secret grant attestation (docs/design/workspace-secrets.md,
    SEC-GRANT-ATTEST). `serve` puts SWARM_WORKSPACE_GRANT_KEY on swarm-api by
    reference: set it only once swarm-workspace-grant-key has a version
    (scripts/create-secrets.sh --workspace-grant-key). `previous_version` is,
    during a rotation only, the version NUMBER the new one replaced, read as
    SWARM_WORKSPACE_GRANT_KEY_PREVIOUS so grants signed under it keep
    verifying until the attest route re-signs them. `required` drops every
    unattested grant at release: set it only after the attest route has run.
    All off by default.
  EOT
  type = object({
    serve            = optional(bool, false)
    previous_version = optional(string, "")
    required         = optional(bool, false)
  })
  default = {}

  validation {
    condition     = can(regex("^([0-9]+)?$", var.workspace_grant_attestation.previous_version))
    error_message = "workspace_grant_attestation.previous_version must be a version NUMBER, or empty; never a value."
  }
}

resource "google_secret_manager_secret" "workspace_grant_key" {
  project   = var.project_id
  secret_id = "swarm-workspace-grant-key"

  # Pinned to the workload region, as swarm-child-key is: automatic
  # replication would copy a platform key into regions nothing here runs in.
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "workspace-grant-key"
  })

  annotations = {
    "swarm-populated-by" = "scripts/create-secrets.sh --workspace-grant-key"
  }
}

# swarm-api, and NOBODY ELSE. Authoritative, so a grant made out of band is
# removed on the next apply.
resource "google_secret_manager_secret_iam_binding" "workspace_grant_key_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.workspace_grant_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = [
    module.iam.service_account_members["swarm-api"],
  ]
}

locals {
  # The references swarm-api reads the key through. Empty until served, so a
  # deployment that has not added a version references nothing and starts as
  # it always has. Merged into api_secret_env (locals.tf).
  workspace_grant_key_secret_env = var.workspace_grant_attestation.serve ? merge(
    {
      SWARM_WORKSPACE_GRANT_KEY = {
        secret  = google_secret_manager_secret.workspace_grant_key.secret_id
        version = "latest"
      }
    },
    var.workspace_grant_attestation.previous_version == "" ? {} : {
      SWARM_WORKSPACE_GRANT_KEY_PREVIOUS = {
        secret  = google_secret_manager_secret.workspace_grant_key.secret_id
        version = var.workspace_grant_attestation.previous_version
      }
    },
  ) : {}

  # swarm-api's switch (swarm_api.settings.grant_attestation_required).
  api_grant_attestation_env = {
    GRANT_ATTESTATION_REQUIRED = var.workspace_grant_attestation.required ? "true" : "false"
  }
}

check "workspace_grant_attestation_required_needs_the_key" {
  assert {
    condition     = !var.workspace_grant_attestation.required || var.workspace_grant_attestation.serve
    error_message = "workspace_grant_attestation.required is set but serve is not: swarm-api has no key to verify with, so every grant would be dropped and every required slot would park CREDENTIAL_MISSING."
  }
}

check "workspace_grant_previous_needs_the_key" {
  assert {
    condition     = var.workspace_grant_attestation.previous_version == "" || var.workspace_grant_attestation.serve
    error_message = "workspace_grant_attestation.previous_version is set but serve is not: a previous key is only read beside a current one."
  }
}
