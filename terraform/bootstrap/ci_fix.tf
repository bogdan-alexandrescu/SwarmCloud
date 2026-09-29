# The CI fixer's identity, federated from exactly one workflow file on main.
#
# ci-fix.yml (#263, #273) submits a fix step to the swarm API when a SwarmCloud
# pull request goes red. It needs an identity that passes the API's IAP front
# door and whose tenant owns the swarm tasks it continues (docs/ci.md, "The CI
# fixer").
#
# WHY NOT THE DEPLOYER, HOPPING. The first version authenticated as the deployer
# (GCP_DEPLOY_SA) and impersonated the fixer's account from there, which needed
# the deployer to hold roles/iam.serviceAccountTokenCreator on it. That makes
# every workflow run that can become the deployer -- release.yml, terraform.yml,
# anything on main -- able to become the fixer as well, and it gives a
# workflow that only comments on pull requests a token for an account holding
# projectIamAdmin on a SHARED project. Owner decision, 2026-09-28: the fixer's
# workflow federates as the fixer's account directly, and nothing else does.
#
# WHY THE PRINCIPAL IS THE WORKFLOW FILE. `attribute.job_workflow_ref` is
# `<owner>/<repo>/.github/workflows/ci-fix.yml@refs/heads/main`: GitHub sets it
# from the file the job runs, so another workflow in this repository -- on main
# or anywhere -- presents a different value and is refused. A principalSet on
# `attribute.repository` or `attribute.repo_ref` would admit every workflow
# that runs on main. tests/terraform/bootstrap.tftest.hcl compares the member
# whole and refuses both of those forms separately.
#
# `@refs/heads/main` is spelled here rather than taken from github_allowed_refs:
# workflow_run fires only for the workflow file on the default branch, so no
# other ref ever runs this workflow in the way it is meant to run, and widening
# the deployer's refs must not widen the fixer's.
#
# THE ACCOUNT IS NOT CREATED HERE. It is an existing account named by
# `ci_fix_service_account`; empty (the default) binds nothing and the fixer
# stays unconfigured, which it reports on the pull request. Nothing here carries
# a label because nothing here can: an IAM member has no labels field (the
# managed-by marker is on the pool, the provider and the accounts this root
# creates).
locals {
  ci_fix_workflow_ref = "${var.github_repository}/.github/workflows/ci-fix.yml@refs/heads/main"

  ci_fix_enabled = var.enable_github_wif && var.ci_fix_service_account != ""
}

resource "google_service_account_iam_member" "ci_fix_wif" {
  count = local.ci_fix_enabled ? 1 : 0

  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.ci_fix_service_account}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.job_workflow_ref/${local.ci_fix_workflow_ref}"

  lifecycle {
    # Compared by account id, which is known at plan; the deployer's email is
    # not until it exists.
    precondition {
      condition     = split("@", var.ci_fix_service_account)[0] != google_service_account.deployer[0].account_id
      error_message = "ci_fix_service_account names the deployer. Binding the fixer's workflow to it would give a job that only comments on pull requests projectIamAdmin on a shared project."
    }

    # The fixer reaches the API only through IAP. An account that is not an
    # accessor gets 403 "Access denied. For user ..." on every attempt, which
    # the pull request would show as a fixer that runs and never fixes.
    precondition {
      condition     = contains(var.frontend_iap_members, "serviceAccount:${var.ci_fix_service_account}")
      error_message = "ci_fix_service_account must also be in frontend_iap_members as serviceAccount:<email>, or IAP refuses every fix attempt."
    }
  }
}
