# Bootstrap inputs for saga-agents-staging.
#
# LOCAL STATE, on purpose (versions.tf): this root creates the bucket the other
# roots keep their state in, so it cannot keep its own state there.

project_id = "saga-agents-staging"
region     = "us-central1"
location   = "US-CENTRAL1"

# The platform's eight custom roles and the broker's swarmSecretLister grant
# were created by terraform/infra's dev state and are ADOPTED by this root
# (platform_roles.tf; #79, #69, 2026-09-25). Naming the state does two things:
# the import blocks adopt the live objects, and every plan refuses to manage
# them until that state's custom_roles_owner output shows the release has
# already made infra forget them. docs/runbooks/custom-roles-to-bootstrap.md.
adopt_from_infra_states = ["infra/dev"]

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

# The CI fixer's account (ci_fix.tf): bound to .github/workflows/ci-fix.yml on
# refs/heads/main and to nothing else. UNSET until the owner names the account;
# set it to the same email as the repository variable SWARM_CI_FIX_SA, and add
# it to frontend_iap_members below as serviceAccount:<email>, in one apply.
ci_fix_service_account = "swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"

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
# may still modify is the 14 roles terraform/infra grants
# (deployer_grantable_project_roles in deployer_conditions.tf), held by
# tests/terraform/deployer_iam.tftest.hcl for every project grant CI applies.
# The list was measured 2026-09-24 at 15 from the code (8 grant resources) and
# from the live policy (43 grants to terraform/infra's identities); it was 15
# until #150 took swarmSecretLister off, because bootstrap now makes that grant.
#
# NOT CLOSED BY IT -- so #68's goal, no route from CI to roles/owner, is NOT
# reached by this line alone:
#
#   * roles/iam.roleAdmin bypassed this condition while it was on the deployer:
#     one iam.roles.update adds resourcemanager.projects.setIamPolicy, which
#     custom roles accept (measured 2026-09-25), to a custom role CI holds
#     unconditioned, and every later project setIamPolicy, roles/owner
#     included, is then authorised by THAT binding -- modifiedGrantsByRole is
#     never evaluated. roleAdmin came off deployer_roles on 2026-09-25 (owner
#     decision, #79), and a validation in variables.tf refuses it there, so
#     the bypass is CLOSED IN CODE. Live, it held until the owner's bootstrap
#     apply destroyed the deployer's roleAdmin binding, which
#     docs/runbooks/custom-roles-to-bootstrap.md ("What actually happened")
#     records on 2026-09-28 at 23:21Z. A live fact read on a date goes stale:
#     a get-iam-policy read-back of the deployer's roles, showing no
#     roles/iam.roleAdmin, is how to confirm it still holds. This is route 2
#     in docs/ci.md -- including widening one of the grantable custom roles
#     and granting it through this condition.
#   * hasOnly limits which roles, never whose or with what condition, so CI
#     can still grant ITSELF any of the 14 (#69); nothing above closes that.
#
# APPLY, in this exact order (owner decision 2026-09-28, after #275): create
# the chunks FIRST, prove they are live, and ONLY THEN remove the live
# project's unconditioned projectIamAdmin grant BY HAND with gcloud -- never by
# importing it into bootstrap state and letting terraform destroy it. #275's
# outage was a one-minute gap where the old grant was destroyed before the new
# one existed; targeting deployer_roles's unconditioned resource in the same
# apply as the chunks would recreate that exact race (a parallel destroy and
# create of the same role, on the same principal, with no ordering between
# them). Doing it by hand instead means CI never lacks projectIamAdmin for any
# interval: the chunks exist and are provable before the unconditioned grant
# is ever touched.
#
#   1. DEPLOYER=$(terraform -chdir=terraform/bootstrap output -raw github_deployer_service_account)
#   2. scripts/bootstrap.sh --target 'google_project_iam_member.deployer_project_iam_admin'
#      The plan must read exactly "2 to add, 0 to change, 0 to destroy" --
#      0 to destroy because the live unconditioned grant is not in bootstrap
#      state (it was restored by hand, never imported); abort on anything else.
#   3. gcloud projects get-iam-policy saga-agents-staging --flatten=bindings \
#        --filter="bindings.role=roles/resourcemanager.projectIamAdmin AND bindings.members:serviceAccount:${DEPLOYER}" \
#        --format='value(bindings.condition.title)'
#      Expect the two chunk titles plus one empty line (the empty line is the
#      unconditioned grant, which carries no condition title).
#   4. gcloud projects remove-iam-policy-binding saga-agents-staging \
#        --member="serviceAccount:${DEPLOYER}" \
#        --role=roles/resourcemanager.projectIamAdmin --condition=None --format=none
#      This removes ONLY the unconditioned binding (--condition=None matches
#      the binding with no condition); the two chunk bindings are untouched.
#   5. Re-run step 3. Expect exactly the two chunk titles, nothing else.
#   6. Prove the admitted side with a terraform/infra plan or a release apply:
#      CI must still be able to grant/revoke the 14 roles it needs (15 before
#      #150 also took swarmSecretLister off deployer_grantable_project_roles),
#      now through the chunked conditions alone.
#
# REVERT: delete the entry, leaving `[]`, and run
# `scripts/bootstrap.sh --target 'google_project_iam_member.deployer_project_iam_admin'`.
# This destroys the chunked bindings; it does NOT restore the unconditioned
# grant, which must be re-added by hand (the mirror image of step 4) before
# reverting, or CI is left with no projectIamAdmin grant at all.
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
  "serviceAccount:swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com",
]

# The Desktop OAuth client `sc login` signs developers in with
# (iap_programmatic_clients.tf, docs/runbooks/iap-desktop-client.md). Created
# 2026-09-25 as "SwarmCloud sc CLI" in this project. A client ID is not secret;
# its secret is in the team's password manager and never in Terraform.
frontend_iap_programmatic_clients = [
  "209012342332-deadkn6c5s1ghe0s5lnq3khmekogn2tv.apps.googleusercontent.com",
]

# The GitHub user slots' IAM (forge_user_slots.tf; #780 lane OB2, owner
# decisions D2, D3 and D7 of 2026-10-07): swarmForgeSlotCreator for swarm-api,
# and per tenant in dev.tfvars, swarm-api's version-add on the user slots and
# read on their -refresh twins, and the worker's read on the base slots. Read
# the plan's four kinds of grant against docs/runbooks/github-app.md, step 2,
# before applying.
enable_forge_user_slots = true
