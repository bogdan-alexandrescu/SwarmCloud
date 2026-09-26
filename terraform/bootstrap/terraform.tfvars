# Bootstrap inputs for saga-agents-staging.
#
# LOCAL STATE, on purpose (versions.tf): this root creates the bucket the other
# roots keep their state in, so it cannot keep its own state there.

project_id = "saga-agents-staging"
region     = "us-central1"
location   = "US-CENTRAL1"

# Keyless CI. Turned on 2026-09-24 so testing, building and deploying run in
# GitHub Actions rather than on a laptop.
enable_github_wif = true
github_repository = "bogdan-alexandrescu/SwarmCloud"

# MAIN ONLY, and this is the security boundary rather than a convenience.
# A branch anyone can push -- or a pull request from a fork -- must not be able
# to mint a token holding projectIamAdmin on a SHARED project. The cost is that
# the terraform PLAN job on a pull request cannot authenticate; that is the
# correct trade and terraform.yml handles it rather than widening this.
github_allowed_refs = ["refs/heads/main"]

# The deployer's roles that have traded their project-wide grant for a
# conditioned one (deployer_conditions.tf).
#
# ADD ONE PER RELEASE, apply bootstrap between releases, and let the next
# release's plan -- which refreshes everything terraform/infra manages -- prove
# it. A wrong condition fails that plan with a 403 on our own resource; the
# revert is deleting the line and applying again.
#
# Accepted names: roles/compute.networkAdmin, roles/compute.securityAdmin,
# roles/container.admin, roles/datastore.owner, roles/logging.configWriter,
# roles/resourcemanager.projectIamAdmin, swarmSecretProvisioner.
#
# 2026-09-25: roles/resourcemanager.projectIamAdmin, FIRST (owner-approved queue
# item 16j, #68). Merging this changes nothing live; the owner's apply does.
#
# WHY THIS ONE FIRST. It is the role through which CI grants itself any other
# role directly, roles/owner included, so while it is unconditioned every
# other scoping is one setIamPolicy call from being undone. (Not the only
# route: see NOT CLOSED BY IT.) And its condition is the least likely to
# break a release: it tests no resource name and no resource type
# (rules 1 and 2 in deployer_conditions.tf, the ones the IAP condition broke),
# only which roles a policy change modifies -- modifiedGrantsByRole with
# hasOnly, zero logical operators. A read modifies nothing and passes. What CI
# may still modify is the 15 roles terraform/infra grants: measured 2026-09-24
# from the code (8 grant resources) and from the live policy (43 grants to
# terraform/infra's identities, the same 15 roles), and held by
# tests/terraform/deployer_iam.tftest.hcl for every project grant CI applies.
#
# NOT CLOSED BY IT -- so #68's goal, no route from CI to roles/owner, is NOT
# reached by this line alone:
#
#   * roles/iam.roleAdmin BYPASSES THIS CONDITION while it is on the deployer
#     (#79). One iam.roles.update adds resourcemanager.projects.setIamPolicy,
#     which custom roles accept (measured 2026-09-25), to a custom role CI
#     already holds unconditioned: swarmSecretProvisioner today (wif.tf,
#     deployer_secrets; scoped, its type guard still admits the project), or
#     swarmDeployerProjectBuckets once bootstrap applies it. Every later
#     project setIamPolicy, roles/owner included, is then authorised by THAT
#     binding, and modifiedGrantsByRole is never evaluated. The six custom
#     roles on the list work the same way, granted through this condition
#     after being widened (docs/ci.md, route 2). This scoping stops a direct
#     grant, not one made through a custom-role update.
#   * hasOnly limits which roles, never whose or with what condition, so CI
#     can still grant ITSELF any of the 15 (#69).
#
# APPLY, targeted and between releases, from the checkout holding bootstrap's
# local state:
#   scripts/bootstrap.sh \
#     --target 'google_project_iam_member.deployer_roles["roles/resourcemanager.projectIamAdmin"]' \
#     --target 'google_project_iam_member.deployer_project_iam_admin[0]'
# REVERT: delete the entry, leaving `[]`, and run the same command. The plan is
# the mirror image: the conditioned grant destroyed, the project-wide one back.
deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]

# Who may pass IAP on the front door (wif.tf, frontend_accessors).
#   domain:saga.xyz -- people; swarm-api still enforces the tenant boundary.
#   swarm-verify    -- owner decision 2026-09-24: the in-VPC checks, PR #15's
#                      end-to-end check, and operator scripts through
#                      SWARM_IMPERSONATE_SA reach the API through the load
#                      balancer. Before this IAP answered 403 "Access denied.
#                      For user swarm-verify@...".
frontend_iap_members = [
  "domain:saga.xyz",
  "serviceAccount:swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
]

# The Desktop OAuth client `sc login` signs developers in with
# (iap_programmatic_clients.tf, docs/runbooks/iap-desktop-client.md). Created
# 2026-09-25 as "SwarmCloud sc CLI" in this project. A client ID is not secret;
# its secret is in the team's password manager and never in Terraform.
frontend_iap_programmatic_clients = [
  "209012342332-deadkn6c5s1ghe0s5lnq3khmekogn2tv.apps.googleusercontent.com",
]
