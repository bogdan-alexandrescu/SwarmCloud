# Platform service accounts.
#
# One identity per component, never a shared one. That is not tidiness: the
# reconciler is the only thing in this system allowed to DELETE infrastructure,
# and the only way that statement can be enforced rather than merely intended is
# if the reconciler's permissions are attached to an identity nothing else runs
# as.
#
# There are no service account KEYS anywhere in this configuration. Cloud Run
# and GKE attach identity to the workload; a downloadable key is a credential
# that outlives the deployment, does not rotate, and cannot be revoked without
# knowing every place it was copied to.

locals {
  owner_marker = "managed-by=${lookup(var.labels, "managed-by", "swarm-terraform")}"

  platform_accounts = {
    "swarm-api" = {
      display_name = "Swarm API"
      description  = "Authenticates callers, resolves tenants, writes tasks. Creates no infrastructure."
    }
    "swarm-scheduler" = {
      display_name = "Swarm Scheduler"
      description  = "Admits work and dispatches executions. Cannot delete infrastructure."
    }
    "swarm-quota-broker" = {
      display_name = "Swarm Quota Broker"
      description  = "Owns provider quota state. Control-plane data only."
    }
    "swarm-reconciler" = {
      display_name = "Swarm Reconciler"
      description  = "The only identity permitted to delete executions and garbage-collect Job resources."
    }
  }
}

resource "google_service_account" "platform" {
  for_each = local.platform_accounts

  project      = var.project_id
  account_id   = each.key
  display_name = each.value.display_name
  description  = "${local.owner_marker}; ${each.value.description}"
}

# Cloud Scheduler and Pub/Sub push present this identity when they call the
# control plane. It is separate from every component SA and holds NO project
# role at all: its entire authority is run.invoker on two specific services,
# granted at the service resource. A compromised tick can ring the doorbell and
# nothing else.
resource "google_service_account" "tick" {
  project      = var.project_id
  account_id   = "swarm-tick"
  display_name = "Swarm Tick Invoker"
  description  = "${local.owner_marker}; OIDC identity for Cloud Scheduler and Pub/Sub push. No project roles."
}
