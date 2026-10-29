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
  # Listed in modules/service_account_ids, which terraform/bootstrap reads too:
  # it grants the release deployer roles/iam.serviceAccountAdmin on each of
  # these accounts and on no other (#334).
  platform_accounts = module.service_account_ids.platform
}

module "service_account_ids" {
  source = "../service_account_ids"
}

# The accounts are terraform/bootstrap's (platform_service_accounts.tf,
# DAYONE-BOOT-2, design Q7): bootstrap grants roles to them, so it creates
# them, and a fresh project's first bootstrap apply needs no "later" switch.
# This module READS them. The live deployment's accounts were forgotten here,
# never destroyed (terraform/infra/removed.tf), and adopted by bootstrap's
# gated import blocks; no email or unique id changed, so no grant moved.
#
# A data source, not a string: a plan against a project whose bootstrap has
# not created an account fails here, by name, instead of granting on an
# account that does not exist.
data "google_service_account" "platform" {
  for_each = local.platform_accounts

  project    = var.project_id
  account_id = each.key
}

# Cloud Scheduler and Pub/Sub push present this identity when they call the
# control plane. It is separate from every component SA and holds NO project
# role at all: its entire authority is run.invoker on two specific services,
# granted at the service resource. A compromised tick can ring the doorbell and
# nothing else.
data "google_service_account" "tick" {
  project    = var.project_id
  account_id = module.service_account_ids.tick_id
}
