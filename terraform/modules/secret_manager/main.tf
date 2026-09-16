# Per-tenant provider credentials.
#
# The isolation rule this module exists to enforce (CONTRACT.md invariant 9):
# a tenant's provider key must never be reachable from another tenant's
# workload. That is why the accessor grant below is an authoritative
# `iam_binding` and not an additive `iam_member` -- a binding removes anything
# that was granted out of band, so the members list here IS the complete set of
# identities that can read the key.
#
# No secret VERSION is created anywhere in this configuration. Putting a key in
# a tfvars file or a terraform variable would write it to state in clear text,
# and terraform state is a file many more people can read than the secret was
# meant for. Versions are added out of band, by a human or by CI.

locals {
  # tenant/provider pairs flattened into one map keyed by the real secret id.
  secrets = {
    for pair in flatten([
      for tenant_id, cfg in var.tenant_secrets : [
        for provider in cfg.providers : {
          secret_id     = "swarm-tenant-${tenant_id}-${provider}"
          tenant_id     = tenant_id
          provider      = provider
          accessor      = cfg.accessor
          admin_members = cfg.admin_members
        }
      ]
    ]) : pair.secret_id => pair
  }

  admin_grants = {
    for k, v in local.secrets : k => v if length(v.admin_members) > 0
  }
}

resource "google_secret_manager_secret" "this" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = each.key

  # User-managed replication pinned to the workload region. Automatic
  # replication would copy provider keys into regions this platform never runs
  # in, which is data residency exposure bought for nothing.
  replication {
    user_managed {
      replicas {
        location = var.region

        dynamic "customer_managed_encryption" {
          for_each = var.kms_key_name == "" ? [] : [var.kms_key_name]
          content {
            kms_key_name = customer_managed_encryption.value
          }
        }
      }
    }
  }

  version_destroy_ttl = var.version_destroy_ttl
  deletion_protection = var.deletion_protection

  labels = merge(var.labels, {
    "swarm-tenant"   = each.value.tenant_id
    "swarm-provider" = each.value.provider
  })

  annotations = {
    "swarm-populated-by" = "out-of-band"
  }

  lifecycle {
    # A key rotated by hand or by CI must not be reverted by the next apply.
    ignore_changes = [annotations["swarm-last-rotated"]]
  }
}

# Exactly one identity may read each secret: the owning tenant's worker SA.
resource "google_secret_manager_secret_iam_binding" "accessor" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = [each.value.accessor]
}

# Humans and CI may ADD a version. They deliberately cannot read one back:
# secretVersionAdder carries no access permission, so rotating a key does not
# require the ability to exfiltrate the key already in place.
resource "google_secret_manager_secret_iam_binding" "version_adder" {
  for_each = local.admin_grants

  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.key].secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  members   = each.value.admin_members
}
