# The CI deployer may ACT AS the service accounts this root attaches to what it
# deploys -- each one individually, never project-wide.
#
# Release 36023801562 (2026-09-24) was the first release to get past planning, and
# its apply failed on all five Cloud Run services with
#
#     Error 403: Permission 'iam.serviceaccounts.actAs' denied on service account
#     swarm-api@... / swarm-scheduler@... / swarm-reconciler@... / ...
#
# Deploying a service or job that runs as account X requires actAs on X. Every
# earlier deploy ran from a laptop as a project owner, who has it implicitly, so
# the grant was never needed until the release did the deploying.
#
# NOT roles/iam.serviceAccountUser on the project: that would let CI act as every
# service account in saga-agents-staging, including the other team's twelve.
# These bindings are on OUR accounts only -- every platform account modules/iam
# creates (keys are static, so a fresh project still plans), the tick account the
# scheduler jobs mint OIDC tokens as, and swarm-verify. Tenant worker accounts get
# the same grant through modules/tenancy's dispatcher_members (main.tf), so a new
# tenant is covered the day it is created.
#
# The deployer grants these to itself: it already holds roles/iam.serviceAccountAdmin
# (terraform/bootstrap), which includes setIamPolicy on service accounts. That is
# stated here rather than hidden -- it is why scoping individual roles is only as
# strong as the conditioning of the IAM-admin roles themselves.
#
# FIRST APPLY. A service update in the same apply as its new binding can race IAM
# propagation and fail once; re-running the failed release jobs clears it. A
# module-level depends_on would buy ordering but not propagation, and it defers
# data sources to apply time (see the note above module "frontend" in main.tf).
locals {
  deployer_member = var.deployer_service_account == "" ? "" : "serviceAccount:${var.deployer_service_account}"

  deployer_acts_as = var.deployer_service_account == "" ? {} : merge(
    module.iam.service_account_emails,
    {
      "swarm-tick"   = module.iam.tick_service_account
      "swarm-verify" = google_service_account.verify.email
    },
  )
}

resource "google_service_account_iam_member" "deployer_acts_as" {
  for_each = local.deployer_acts_as

  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member
}
