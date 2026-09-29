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

  # Listed in modules/service_account_ids, which terraform/bootstrap reads too:
  # it grants the release deployer roles/iam.serviceAccountAdmin on each of
  # these accounts and on no other (#334).
  platform_accounts = module.service_account_ids.platform
}

module "service_account_ids" {
  source = "../service_account_ids"
}

# create_ignore_already_exists: an account made ahead of the release -- the
# step that has to come before bootstrap can grant the deployer on it
# (docs/ci.md, "The deployer's service-account grants") -- is adopted, not
# refused with a 409.
resource "google_service_account" "platform" {
  for_each = local.platform_accounts

  project      = var.project_id
  account_id   = each.key
  display_name = each.value.display_name
  description  = "${local.owner_marker}; ${each.value.description}"

  create_ignore_already_exists = true
}

# Cloud Scheduler and Pub/Sub push present this identity when they call the
# control plane. It is separate from every component SA and holds NO project
# role at all: its entire authority is run.invoker on two specific services,
# granted at the service resource. A compromised tick can ring the doorbell and
# nothing else.
resource "google_service_account" "tick" {
  project      = var.project_id
  account_id   = module.service_account_ids.tick_id
  display_name = "Swarm Tick Invoker"
  description  = "${local.owner_marker}; OIDC identity for Cloud Scheduler and Pub/Sub push. No project roles."

  create_ignore_already_exists = true
}
