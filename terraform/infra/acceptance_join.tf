# The join acceptance probe's person credential (docs/design/join-requests.md
# section 8).
#
# scripts/acceptance/groups/join.sh has to ask to join a workspace AS A PERSON:
# a service account cannot request membership the way the console's users do.
# It reads one authorized_user credential (a probe person's, never a real
# user's) from the secret below and exchanges it for an openid-email ID token.
#
# Patterned on egress.tf: terraform makes the container and its one reader and
# creates NO version, so the credential is never in state.
# scripts/create-secrets.sh --join-person --stdin stores it, exists-first.

# Dev-only switch: default false, true only in the dev settings, so prod's
# swarm-verify never gets the accessor.
variable "enable_acceptance_join_person" {
  description = "Create swarm-accept-join-person and let swarm-verify read it, for the join acceptance probe. Default false; true only in the dev settings, never in prod."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_acceptance_join_person || var.environment == "dev"
    error_message = "enable_acceptance_join_person is dev only: a probe person's credential is never stored in staging or prod, and swarm-verify there reads no secret."
  }
}

resource "google_secret_manager_secret" "accept_join_person" {
  count = var.enable_acceptance_join_person ? 1 : 0

  project   = var.project_id
  secret_id = "swarm-accept-join-person"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "acceptance-join-person"
  })

  annotations = {
    "swarm-populated-by" = "scripts/create-secrets.sh --join-person --stdin (docs/design/join-requests.md section 8)"
  }
}

# The one secret swarm-verify may read (verify.tf's "secret access is absent"
# comment is amended): the probe person's authorized_user credential,
# exchanged for an openid-email ID token by groups/join.sh. Authoritative for
# secretAccessor ON THIS SECRET only, so a grant made out of band is removed on
# the next apply.
resource "google_secret_manager_secret_iam_binding" "accept_join_person_accessor" {
  count = var.enable_acceptance_join_person ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.accept_join_person[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = ["serviceAccount:${local.verify_email}"]
}
