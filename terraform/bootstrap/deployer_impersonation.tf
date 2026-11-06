# The person who runs `swarm install` may mint tokens for the deployer (N3,
# #2688).
#
# Every install pass after bootstrap pass 1 runs as the deployer (lessons 22,
# 23): the engine sets GOOGLE_IMPERSONATE_SERVICE_ACCOUNT, and the google
# provider and the gcs backend then call generateAccessToken on the deployer
# as the person. That call needs iam.serviceAccounts.getAccessToken on the
# deployer, and nothing else in this root grants it. Whether the project's
# roles/owner already carries it is not something the install relies on: the
# grant is made either way, so the next pass works whichever way it answers.
#
# ON THE DEPLOYER ALONE, NEVER AT THE PROJECT. A project-level
# serviceAccountTokenCreator lets its holder become every service account in
# the project, the tenants' among them. The grant is on one account, to one
# `user:`, and `swarm uninstall` removes exactly this binding.
#
# SwarmCloud's own bootstrap leaves deployer_impersonator empty: its deployer is
# reached through GitHub federation (wif.tf), and nobody impersonates it by
# hand, so this resource is absent from its plan.

resource "google_service_account_iam_member" "deployer_impersonator" {
  count = local.deployer_on && var.deployer_impersonator != "" ? 1 : 0

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = var.deployer_impersonator
}
