# Tenant settings integrity, the deployment side
# (docs/design/tenant-settings-integrity.md; #1457, owner decisions
# 2026-10-17 ~03:00 UTC).
#
# Every tenant worker's service account can write any `workspace_settings`
# document whose id it can name (terraform/modules/tenancy/main.tf, the
# residual no IAM condition can close), so a value the control plane ACTS on
# is trusted only when it carries a MAC under a key no worker holds:
# HMAC-SHA256, the key as UTF-8, over `canonical_settings_block`, stored as
# `mac: {v: 1, key, sig}`. swarm-api signs; the scheduler and swarm-api
# verify. This file is the key and who can read it:
#
#   * `swarm-settings-key`, the secret. Its VALUE is never here -- a managed
#     secret version would put the plaintext in a state file several people
#     can read. `scripts/create-secrets.sh --settings-mac-key` generates and
#     adds it, and nobody sees it.
#   * secretAccessor for swarm-api's and the scheduler's service accounts, and
#     NOBODY ELSE: no tenant account, no human, no wildcard. A tenant identity
#     that could read this key could sign a forged setting for any tenant,
#     which is the whole attack this closes.
#   * SWARM_SETTINGS_KEY (version latest) on both services once
#     var.settings_mac_key_ready is true, and, during a rotation only,
#     SWARM_SETTINGS_KEY_PREVIOUS (var.settings_mac_key_previous_version).
#   * ONE cutover list, var.settings_mac_tenants, rendered into both services
#     as SETTINGS_MAC_TENANTS, so the signer and the verifiers can never
#     disagree about which tenants are signed; and the scheduler's
#     SETTINGS_MAC_REQUIRED (var.settings_mac_required). Both default to
#     nothing switched: no tenant is cut over before the worker wiring
#     (ISO-WIRE) has merged.
#
# A CLOUD RUN REVISION THAT REFERENCES A SECRET WITH NO VERSION FAILS TO
# START, so the reference is behind var.settings_mac_key_ready (owner
# decision 2026-10-17, "Gate it"), as swarm-child-key's is behind
# enable_child_tasks. The first apply creates the secret container, the two
# members and the cutover env, and no service references the key. Then:
# `scripts/create-secrets.sh --settings-mac-key`, set
# `settings_mac_key_ready = true`, apply again.

resource "google_secret_manager_secret" "settings_mac_key" {
  project   = var.project_id
  secret_id = "swarm-settings-key"

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
    "component" = "settings-mac-key"
  })

  annotations = {
    "swarm-populated-by" = "scripts/create-secrets.sh --settings-mac-key"
  }
}

# Exactly the two control-plane services that sign and verify. Keyed by the
# service, not the member: an email is unknown until apply, and a for_each
# key must be known at plan.
resource "google_secret_manager_secret_iam_member" "settings_mac_key_accessor" {
  for_each = toset(["swarm-api", "swarm-scheduler"])

  project   = var.project_id
  secret_id = google_secret_manager_secret.settings_mac_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = module.iam.service_account_members[each.key]
}

locals {
  # The Secret Manager references both services read the key through; merged
  # into api_secret_env (locals.tf) and the scheduler's secret_env (main.tf).
  # Both verify, so both get the previous version during a rotation: a value
  # signed a moment before the rotation must still verify after it.
  settings_mac_secret_env = !var.settings_mac_key_ready ? {} : merge(
    {
      SWARM_SETTINGS_KEY = {
        secret  = google_secret_manager_secret.settings_mac_key.secret_id
        version = "latest"
      }
    },
    var.settings_mac_key_previous_version == null ? {} : {
      SWARM_SETTINGS_KEY_PREVIOUS = {
        secret  = google_secret_manager_secret.settings_mac_key.secret_id
        version = var.settings_mac_key_previous_version
      }
    },
  )

  # One list, rendered the same into both services (local.service_env).
  settings_mac_env = {
    SETTINGS_MAC_TENANTS = join(",", var.settings_mac_tenants)
  }

  # The scheduler alone refuses to act on an unsigned value; swarm-api signs
  # what it writes for a listed tenant and needs no switch to do so.
  scheduler_settings_mac_env = merge(local.settings_mac_env, {
    SETTINGS_MAC_REQUIRED = var.settings_mac_required ? "true" : "false"
  })
}

check "settings_mac_tenants_are_tenants" {
  assert {
    condition     = alltrue([for t in var.settings_mac_tenants : contains(keys(var.tenants), t)])
    error_message = "settings_mac_tenants names a tenant that is not in var.tenants. A typo there leaves the tenant believed cut over while neither service signs or verifies its settings."
  }
}

output "settings_mac_service_wiring" {
  description = "What settings integrity puts on the services: the Secret Manager env each reads and the cutover list. Names only; never a value."
  value = {
    secret_env = {
      "swarm-api"       = sort([for k in keys(local.api_secret_env) : k if startswith(k, "SWARM_SETTINGS_KEY")])
      "swarm-scheduler" = sort([for k in keys(local.scheduler_secret_env) : k if startswith(k, "SWARM_SETTINGS_KEY")])
    }
    tenants  = var.settings_mac_tenants
    required = var.settings_mac_required
  }
}
