# The managed plugins' bundle job: the two rows terraform/infra cannot make
# (docs/design/managed-plugins.md §1.4.2, "Day One, rollback and the owner's
# rows"; the owner, 11 November 2026: the bundle build is one execution of the
# Cloud Run job swarm-plugin-bundle, under the builder account, and no Cloud
# Build).
#
#   google_project_iam_custom_role.plugin_bundle_runner
#       swarmPluginBundleRunner: run.jobs.run and run.jobs.runWithOverrides
#       (the two checks a run carrying arguments passes) and
#       run.executions.get (to read the execution it started), nothing else.
#       Not roles/run.jobsExecutorWithOverrides, which carries more than these
#       three. No run.jobs.update, so the holder can never change the job's
#       image, command, identity or network; no run.executions.cancel or
#       delete; no logging permission. Custom roles
#       are defined only here (tests/terraform/custom_roles_out_of_ci.tftest.hcl);
#       terraform/infra/plugin-bundles.tf binds it for swarm-api on the job's
#       own IAM policy and nowhere else.
#   google_service_account_iam_member.deployer_acts_as_plugin_bundler
#       the release deployer's roles/iam.serviceAccountUser on the builder
#       account ALONE: creating or updating a job that runs as an account needs
#       actAs on it, and the builder is not on deployer_service_accounts.tf's
#       per-account list. Never project-wide.
#
# NOT HERE, ON PURPOSE: swarm-api's actAs on the builder (running a job checks
# no actAs on its identity, so swarm-api needs none), and any Cloud Build role
# or permission (the rows that would have let swarm-api start builds were
# reverted on main and stay reverted).
#
# THE OWNER APPLIES THIS, never a lane or CI. A fresh install's bootstrap pass
# applies it with the rest of this root; on an existing deployment it is a
# targeted apply from the runbook written after this merges:
#
#   terraform -chdir=terraform/bootstrap apply \
#     -target=google_project_iam_custom_role.plugin_bundle_runner \
#     -target='google_service_account_iam_member.deployer_acts_as_plugin_bundler[0]'
#
# Until it is applied, infra's binding of the role on the job fails at apply
# (the role does not exist) and the deployer cannot deploy the job; nothing
# else in either root depends on it.

resource "google_project_iam_custom_role" "plugin_bundle_runner" {
  project     = var.project_id
  role_id     = module.custom_role_ids.ids["plugin_bundle_runner"]
  title       = "Swarm Plugin Bundle Runner"
  description = "managed-by=swarm-terraform; swarm-api runs the plugin bundle job with arguments and reads its executions, bound on that job only (managed-plugins.md section 1.4.2). No update, no cancel, no logs, no actAs."
  stage       = "GA"

  permissions = [
    "run.executions.get",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
  ]
}

resource "google_service_account_iam_member" "deployer_acts_as_plugin_bundler" {
  count = local.deployer_on ? 1 : 0

  service_account_id = google_service_account.plugin_bundler.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer[0].email}"
}
