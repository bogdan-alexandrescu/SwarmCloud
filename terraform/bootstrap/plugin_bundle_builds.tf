# ---------------------------------------------------------------------------
# Managed plugins: swarm-api starts a bundle build as the builder account and
# reads its status (docs/design/managed-plugins.md §1.4.2, §1.4.3, §15.2).
# ---------------------------------------------------------------------------
#
# The two rows terraform/infra/plugin-bundles.tf leaves out on purpose (its
# WHAT IS NOT HERE): the owner answered on 10 November 2026 that they live in
# this root, which the owner applies from a runbook. When a platform admin
# applies an update to a bundled plugin, swarm-api starts one Cloud Build build
# in this project, running as swarm-plugin-bundler (plugin_bundler.tf), and
# polls it until it finishes. That needs exactly two things, one row each:
#
#   google_service_account_iam_member.api_acts_as_plugin_bundler
#       roles/iam.serviceAccountUser ON swarm-plugin-bundler alone, to
#       swarm-api: Cloud Build checks iam.serviceAccounts.actAs on the
#       account a build names. Never project-wide, which would let swarm-api
#       act as every account in the shared project, the other team's included.
#       The builder holds only plugin-bundles.tf's grants (read its sources and
#       images, fetch through the mirrors, push to one repository, write its
#       log), so acting as it reaches no more than those.
#   google_project_iam_custom_role.plugin_build_starter
#       swarmPluginBuildStarter: cloudbuild.builds.create to start the build
#       and cloudbuild.builds.get to read its status, and no other permission.
#       Not roles/cloudbuild.builds.editor, which also lists, updates and
#       cancels every build in the project, the other team's among them.
#       §1.4.3's "the build's progress" is a get by the build id swarm-api
#       recorded; the failure it shows is a fixed code and the build id, never
#       the build's log, so no log read either.
#   google_project_iam_member.plugin_build_starter
#       swarm-api holds it, unconditioned (below).
#
# NO CONDITION. Cloud Build is not among the services IAM evaluates resource
# attributes for, and a build does not exist until builds.create makes it, so
# no resource.name could name "a build that runs as the builder". What bounds
# the create is the actAs check: a build may run only as an account its caller
# may act as. swarm-api acts as swarm-plugin-bundler (above) and swarm-speech
# (platform_service_accounts.tf, row I2), and as no other account here.
#
# WHAT IT STILL REACHES (the residual, for the security review): a build that
# names NO account runs as the project's default build account. Where that is
# the legacy Cloud Build account (<number>@cloudbuild.gserviceaccount.com),
# Cloud Build does not check actAs on it, and it holds
# roles/cloudbuild.builds.builder project-wide. Where it is the Compute Engine
# default account (projects created since 2024), actAs is checked and
# swarm-api holds none. Read which one this project uses before applying
# (`gcloud builds get-default-service-account`); swarm-api's builds always
# name swarm-plugin-bundler (§1.4.2).
#
# THE SWITCH. This root has no builtins_managed variable (it is
# terraform/infra's), so both rows are planned always: with the switch off no
# swarm-api code path starts a build, and an actAs on an account that holds no
# grant, beside a role that runs nothing by itself, costs nothing.
#
# THE ROLE ID is spelled here, as account_key_secrets.tf spells its own: no
# file in terraform/infra names it, so modules/custom_role_ids has no use for
# it.
#
# ORDER: none against a release. Nothing in terraform/infra names either row.
# Until both are applied, swarm-api's builds.create is refused and no bundle
# build can start; nothing else depends on them.
#
# THE OWNER'S TARGETED PLAN (the runbook's step) shows exactly the three
# objects:
#
#   terraform -chdir=terraform/bootstrap plan \
#     -target=google_service_account_iam_member.api_acts_as_plugin_bundler \
#     -target=google_project_iam_custom_role.plugin_build_starter \
#     -target=google_project_iam_member.plugin_build_starter
#
# swarm-plugin-bundler and swarm-api already exist, so neither appears as a
# change.
#
# NOT VERIFIED LIVE: that Cloud Build accepts a create carrying these two
# permissions and actAs alone. The first bundle build that starts is the live
# proof.
#
# THE OTHER TEAM'S RESOURCES. Nothing here names one: the actAs row is on
# swarm-plugin-bundler, the role is in this project and held by swarm-api
# alone. No tenant worker gets either row.

resource "google_service_account_iam_member" "api_acts_as_plugin_bundler" {
  service_account_id = google_service_account.plugin_bundler.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.platform_sa_email["swarm-api"]}"

  depends_on = [google_service_account.platform]
}

resource "google_project_iam_custom_role" "plugin_build_starter" {
  project     = var.project_id
  role_id     = "swarmPluginBuildStarter"
  title       = "Swarm Plugin Build Starter"
  description = "managed-by=swarm-terraform; swarm-api starts a managed plugin's bundle build as swarm-plugin-bundler and reads its status (managed-plugins.md section 1.4.2). builds.create and builds.get only: no list, update, cancel or log read."
  stage       = "GA"

  # Two permissions (the header).
  permissions = [
    "cloudbuild.builds.create",
    "cloudbuild.builds.get",
  ]
}

resource "google_project_iam_member" "plugin_build_starter" {
  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.plugin_build_starter.role_id}"
  member = "serviceAccount:${local.platform_sa_email["swarm-api"]}"

  depends_on = [google_service_account.platform]
}
