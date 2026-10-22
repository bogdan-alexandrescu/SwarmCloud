# The QA target credential secret, one per tenant
# (docs/design/visual-qa-any-app.md §2.1 and §7; lane VQ-3 QA-SECRET of
# docs/design/agent-autonomy.md §6; owner answer VQ-Q2 (a), 2026-10-22;
# part of #1933).
#
# Three things, and only these:
#
#   * `swarm-tenant-<t>-qa` for EVERY tenant this root provisions -- not per
#     provider, so a tenant holding no provider key gets one too. Its value is
#     a JSON map keyed by QA target id, each entry the test account's
#     username and password for an own-login target. Created EMPTY: swarm-api
#     adds every version from the console's one-time credential form
#     (`PUT /v1/qa-targets/{id}/credential`), so no value passes through
#     Terraform, its state, a chat or a prompt;
#   * V-1: swarm-api's account is secretVersionAdder on each one, to store
#     what the form submits;
#   * V-2: swarm-api's account is secretAccessor on each one, to read the
#     sign-in it hands the browser gate (`POST /v1/attempts/{id}/qa-target`,
#     §2.2) at the moment a browser attempt asks.
#
# WHO READS IT: swarm-api, nobody else. NOT the tenant's worker account: the
# worker asks swarm-api for its target's sign-in with the attempt key, hands
# it to the gate and drops it, so the agent -- which runs as that worker
# account -- can never read a password out of Secret Manager itself. Both
# bindings are authoritative and per secret, as the provider keys' are
# (modules/secret_manager), so a grant made out of band is removed on the
# next apply, and neither role is granted project-wide, where it would reach
# every tenant's provider keys.
#
# Tenants this root does not provision -- personal tenants swarm-api creates
# at runtime, and any registered by scripts/register-tenant.sh -- get no QA
# secret here, so no own-login credential can be stored for them until one
# is declared for them (a question in the lane's PR).
#
# Not behind a switch: an empty secret and two bindings, read by nothing until
# VQ-2's routes are deployed, change no behaviour. The owner approves V-1, V-2
# and the secrets at dev-iam.

resource "google_secret_manager_secret" "qa_target" {
  for_each = toset(keys(var.tenants))

  project   = var.project_id
  secret_id = "swarm-tenant-${each.key}-qa"

  # Pinned to the workload region, as the provider keys are: automatic
  # replication would copy a test account's password into regions nothing
  # here runs in.
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  # `tenant` is the label scripts/offboard-tenant.sh finds a tenant's secrets
  # by. `component` is NOT tenant-credential: that label is how the quota
  # broker's refresh sweep and create-secrets.sh's listing find provider
  # keys, and this is not one.
  labels = merge(local.labels, {
    "component"    = "qa-target-credential"
    "tenant"       = each.key
    "swarm-tenant" = each.key
  })

  annotations = {
    "swarm-populated-by" = "swarm-api PUT /v1/qa-targets/{id}/credential"
  }

  lifecycle {
    # A provider named `qa` would make modules/secret_manager declare the same
    # secret id, read by the tenant's worker.
    precondition {
      condition     = !contains(var.tenants[each.key].providers, "qa")
      error_message = "tenant ${each.key} lists a provider named \"qa\": swarm-tenant-${each.key}-qa is the tenant's QA target credential secret, which no worker may read. Rename the provider."
    }
  }
}

# V-1. swarm-api adds a version from the console form; no person and not
# var.secret_admin_members, which may add to provider keys. Two project-level
# grants conditioned on `swarm-tenant-*` also reach this name and are not
# this file's: terraform/bootstrap/provider_keys.tf's swarmProviderKeyWriter
# (swarm-api again; it holds the versions.disable that §2.1's "disables the
# previous version" needs, and no versions.access) and modules/iam's
# broker_version_adder (the quota broker, which writes only secrets labelled
# component=tenant-credential). Neither can read a version.
resource "google_secret_manager_secret_iam_binding" "qa_target_adder" {
  for_each = google_secret_manager_secret.qa_target

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  members = [
    module.iam.service_account_members["swarm-api"],
  ]
}

# V-2. swarm-api, and NOBODY ELSE, reads it. No worker account, the tenant's
# own included.
resource "google_secret_manager_secret_iam_binding" "qa_target_accessor" {
  for_each = google_secret_manager_secret.qa_target

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = [
    module.iam.service_account_members["swarm-api"],
  ]
}
