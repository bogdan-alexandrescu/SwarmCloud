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
  #
  # `accessors` is the complete accessor list: the tenant's worker account,
  # unless the tenant names this provider's readers itself -- for git-merge and
  # git-review that is the merge or post-verdict account ALONE, and the worker
  # account is not on it (#295, contract request 35's MAJOR 1).
  #
  # `refreshable` is false for an App key: it does not expire, and the broker
  # has no business writing one (see refresh_secrets).
  secrets = {
    for pair in flatten([
      for tenant_id, cfg in var.tenant_secrets : [
        for provider in cfg.providers : {
          secret_id     = "swarm-tenant-${tenant_id}-${provider}"
          tenant_id     = tenant_id
          provider      = provider
          accessors     = lookup(cfg.accessor_overrides, provider, [cfg.accessor])
          admin_members = cfg.admin_members
          refreshable   = !contains(var.action_providers, provider)
        }
      ]
    ]) : pair.secret_id => pair
  }

  refresher_enabled = var.enable_subscription_refresh

  # The long-lived half. One per tenant/provider pair, created empty: a tenant
  # on a static API key simply never has a version added, and the broker's
  # sweep skips it. That keeps "this tenant uses a subscription" an operator
  # action (scripts/create-secrets.sh --subscription) rather than a terraform
  # change, which matters because the two are done by different people at
  # different times.
  #
  # NEVER FOR git-merge OR git-review (#295, contract request 35's MAJOR 1). A
  # GitHub App key is not a rotating subscription credential, and the
  # refresher's grants -- versionAdder on the base secret, a `-refresh` twin --
  # are a second writer of the one secret only the merge or post-verdict
  # account may read. So neither provider gets a twin, and the refresher gets
  # no grant on either base secret.
  refresh_secrets = local.refresher_enabled ? {
    for k, v in local.secrets : "${k}-refresh" => v if v.refreshable
  } : {}

  # The broker publishes the short-lived half into the BASE secret, so it needs
  # versionAdder there too -- but never accessor. It writes access tokens; it
  # has no reason to read back the one already in place.
  #
  # The condition is structural on purpose -- it asks whether the refresher is
  # enabled, never what its email is. Filtering on the member's VALUE would make
  # these keys unknown until apply, and an unknown for_each key set cannot be
  # planned at all.
  admin_grants = {
    for k, v in merge(local.secrets, local.refresh_secrets) : k => v
    if(local.refresher_enabled && v.refreshable) || length(v.admin_members) > 0
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

  # `component`/`tenant`/`provider` are the same keys scripts/create-secrets.sh
  # writes, and the broker's discovery reads them rather than taking the secret
  # name apart -- `swarm-tenant-u-bogdan-anthropic-refresh` does not split
  # unambiguously, because personal tenant ids are `u-<user>` and contain a
  # dash. Two provisioning paths that label differently would make half the
  # fleet invisible to the refresher, so they are kept identical here.
  labels = merge(var.labels, {
    "swarm-tenant"   = each.value.tenant_id
    "swarm-provider" = each.value.provider
    "component"      = "tenant-credential"
    "tenant"         = each.value.tenant_id
    "provider"       = each.value.provider
  })

  annotations = {
    "swarm-populated-by" = "out-of-band"
  }

  lifecycle {
    # A key rotated by hand or by CI must not be reverted by the next apply.
    ignore_changes = [annotations["swarm-last-rotated"]]
  }
}

# The owning tenant's identities, and nobody else, may read each secret: its
# worker account, or -- for a provider the tenant names readers for -- exactly
# those (#295: the merge account alone on -git-merge, the post-verdict account
# alone on -git-review, the worker and the review account on the review
# agent's provider key). Authoritative, so a grant made out of band is removed
# on the next apply.
resource "google_secret_manager_secret_iam_binding" "accessor" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = each.value.accessors
}

# Humans and CI may ADD a version. They deliberately cannot read one back:
# secretVersionAdder carries no access permission, so rotating a key does not
# require the ability to exfiltrate the key already in place.
resource "google_secret_manager_secret_iam_binding" "version_adder" {
  for_each = local.admin_grants

  project = var.project_id
  secret_id = contains(keys(local.refresh_secrets), each.key) ? (
    google_secret_manager_secret.refresh[each.key].secret_id
  ) : google_secret_manager_secret.this[each.key].secret_id
  role = "roles/secretmanager.secretVersionAdder"
  members = distinct(concat(
    each.value.admin_members,
    local.refresher_enabled && each.value.refreshable ? [var.refresher_member] : [],
  ))
}

# -- the long-lived half ---------------------------------------------------
#
# Same replication, CMEK and destroy-delay as the base secret: it is strictly
# MORE sensitive, being a standing grant on the tenant's Claude account rather
# than a token that expires.

resource "google_secret_manager_secret" "refresh" {
  for_each = local.refresh_secrets

  project   = var.project_id
  secret_id = each.key

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
    "component"      = "tenant-credential"
    "tenant"         = each.value.tenant_id
    "provider"       = each.value.provider
  })

  annotations = {
    "swarm-populated-by" = "out-of-band"
  }

  lifecycle {
    ignore_changes = [annotations["swarm-last-rotated"]]
  }
}

# The tenant's worker is ABSENT from this list on purpose, and that absence is
# the isolation property: a job can use the subscription for as long as its
# access token lives, and cannot mint itself a new one afterwards.
resource "google_secret_manager_secret_iam_binding" "refresh_accessor" {
  for_each = local.refresh_secrets

  project   = var.project_id
  secret_id = google_secret_manager_secret.refresh[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = [var.refresher_member]
}
