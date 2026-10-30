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
# may still modify is the 15 roles terraform/infra grants
# (deployer_grantable_project_roles in deployer_conditions.tf; 14 until #1430
# turned enable_github_hooks_dedup_role on, below), held by
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
#     and granting it through this condition. This scoping by itself stops a
#     direct grant, not one made through a custom-role update, which is why
#     roleAdmin had to go rather than be scoped.
#   * hasOnly limits which roles, never whose or with what condition, so CI
#     can still grant ITSELF any of the 15 (#69); nothing above closes that.
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
#      CI must still be able to grant/revoke the 15 roles it needs (14 until #1430 turned
#      swarmGithubHooksDedup on, and 15 before #150 also took swarmSecretLister off deployer_grantable_project_roles),
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
#   oracle-api      -- owner decision 2026-10-18 (#1568): the orchestrating
#                      client application submits workflows as its own tenant,
#                      u-or-9a0792 (terraform/environments/dev/dev.tfvars),
#                      the swarm-verify pattern, and never as a member of
#                      eng@saga.xyz. The owner applies this himself; the line
#                      is the record of that apply, because a -var not
#                      written here makes the next bootstrap plan remove it.
#   qa-console      -- owner decision 2026-10-19 (#1649, QA-IDENTITY option B):
#                      the key-less QA identity a live-browser QA agent signs
#                      in to the console as, through a 15-minute JWT swarm-api
#                      signs (qa_identity.tf). It passes IAP here only; it is
#                      a member of no group and no other IAP app.
frontend_iap_members = [
  "domain:saga.xyz",
  "serviceAccount:swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
  "serviceAccount:oracle-api@saga-agents-staging.iam.gserviceaccount.com",
  "serviceAccount:swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com",
  "serviceAccount:qa-console@saga-agents-staging.iam.gserviceaccount.com",
]

# QA identity (qa_identity.tf, QI-IAM PR 1657; docs/design/qa-identity.md
# option B, owner 2026-10-19): the key-less service account qa-console, the
# custom role swarmQaJwtSigner holding iam.serviceAccounts.signJwt alone, and
# qa-console's own IAM policy granting that role to swarm-api alone. Applied as
# its own bootstrap apply, after the workspace-kinds one (owner, 2026-10-19).
enable_qa_identity = true

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

# W4b (2026-10-11, owner): the workspace job (a Cloud Run job, WD2 re-decided
# 2026-10-10) is switched on, pinned to the workspace-apply image the release
# promoted to :dev after W6b (PR 1015) merged. Re-pinned 2026-10-16 to the
# image release 38949156529 (0a5c7ede) promoted: it carries the tenant
# allow-typesafe-fwd policy in render.py's TENANT_FILES (PR 1409), the IAP
# sign-in path in scripts/lib/common.sh (PR 1413) and today's scheduler
# changes, so a workspace created now renders what every existing tenant got.
# Re-pinned 2026-10-19 to the image release 39326139658 (7c2e148d) promoted:
# it carries WK-SIGNED-APPROVAL (PR 1632), WK-APPROVAL-KEYS (PR 1639: the job
# verifies approvals against the SPEC_VERIFY_KEYS its command pins) and
# WK-FAILCOPY (PR 1638), which group and service workspaces need.
enable_workspace_deployer = true
workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@sha256:7a03edc768e67fea9f791ab65ec28c726b8e8420799ece96f51169a7fc46028a"

# Workspace kinds (docs/design/workspace-kinds.md §4.5, #847; owner 2026-10-19):
# K2 (the deployer's second swarmWorkspaceSecretBinder grant, on any tenant's
# -git team slot and its -git-refresh twin) and K3 (swarm-api's three forge-slot
# grants over members' -git-u- slots). K1 is NOT granted: O1 step 2 showed IAP
# refuses the number-form condition on both backends (docs/workspaces.md, read
# 2026-10-19 20:20-20:35Z), so swarmFrontDoorAdmitter stays off
# workspace_deployer_roles and a service workspace's front-door admit (S2) stays
# an owner step.
enable_workspace_kinds = true

# L8c (2026-10-12, owner): saved browser logins on. swarm-api provisions each
# personal tenant's -browser- secret and grants it to that tenant's worker
# alone (browser_logins.tf). Applied 2026-10-12 15:25 PDT from a plan of
# 4 adds / 0 changes / 0 destroys whose two conditions linted clean
# (iamPolicies:lintPolicy). Recorded here so the next bootstrap plan keeps it.
enable_browser_logins = true

# KEYS-IAM (2026-10-14, owner: "Yes, scoped bootstrap role"): swarm-api may
# write and destroy tenant provider keys, on swarm-tenant-* secrets only
# (provider_keys.tf). NOT YET APPLIED when this line was committed: the owner
# applies it after linting the condition (docs/provider-keys.md §6). The plan
# is 2 adds -- the role and the conditioned grant.
enable_provider_keys = true

# JEV-RUNNER (2026-10-14, owner: "ship ... jev-browser ... by morning"): eng's
# worker may read u-bogdan's personal TypeSafe key, so a jev task bogdan
# submits in eng uses his key (typesafe_user_keys.tf). Every eng task can read
# it -- the row is the consent. NOT YET APPLIED when this line was committed:
# the owner applies it. The plan is 1 add,
# google_project_iam_member.typesafe_user_key_reader["eng/u-bogdan"].
typesafe_user_key_readers = {
  eng = ["u-bogdan"]
}

# SEC-IAM (PR 1356, #1347): swarm-api alone may read swarm-tenant-<t>-wsec-*
# secrets (versions.access only, one extract() condition), so the release
# route (SEC-RELEASE, PR 1369) can hand an attempt its granted workspace
# secrets. Applied 2026-10-15 ~17:05Z from a plan of 2 adds / 0 changes /
# 0 destroys equal to PR 1356's listed rows; the condition linted {} with
# iamPolicies:lintPolicy. Recorded here so the next bootstrap plan keeps it.
enable_workspace_secrets = true

# The TypeSafe forwarder's conditioned read of swarm-tenant-<t>-typesafe
# versions (typesafe_forwarder.tf; SEC-INJECT-A, #1347). APPLIED 2026-10-16
# ~10:45Z with the owner's approval, from a saved plan (2 adds: the
# swarmTypesafeForwarderAccessor role holding secretmanager.versions.access only,
# and its conditioned member for swarm-system/swarm-typesafe-fwd). That apply
# passed the flag on the command line and it was not written here, so a later
# bootstrap plan from main read it as false and planned to destroy both. It is
# recorded here so the state and this file agree.
enable_typesafe_forwarder_read = true

# The GitHub push receiver's create-only Firestore role (HOOKS-ROLE, PR 1423;
# github_hooks_role.tf; owner decision 2026-10-16 on #1384): swarmGithubHooksDedup
# holds datastore.entities.create and nothing else, and the deployer may grant
# it. ON 2026-10-16, before terraform/infra's enable_github_hooks, whose binding
# names this role (docs/runbooks/github-app.md, Step 9 step 2).
enable_github_hooks_dedup_role = true

# MCP-IAM (PR 1503, #1433; mcp_credentials.tf): swarm-api alone may read and
# delete swarm-tenant-<t>-mcpc-* secrets and their versions, under one
# extract() condition, so an attempt's mcp:attach can hand its relay the
# caller's per-run credential and the sweep can delete it when the task ends.
# NOT YET APPLIED when this line was committed (read 2026-10-18 22:03Z:
# swarmMcpCredentialAccessor does not exist). The owner applies it before the
# release that sets dev's mcp_servers_enabled; lint the condition with
# iamPolicies:lintPolicy first. The plan is 2 adds / 0 changes / 0 destroys:
# the role (secretmanager.secrets.delete, secretmanager.versions.access) and
# its conditioned member for swarm-api. Recorded here in the same change so a
# later bootstrap plan from main keeps them rather than destroying them.
enable_mcp_credentials = true

# SSC-PLATFORM-STATUS (PR 2218, #2158 secrets-scopes.md §5.2 and §11 Q7;
# platform_secret_status.tf): swarm-api may read Secret Manager METADATA
# (secretmanager.secrets.get, secretmanager.versions.list -- never a payload)
# on swarm-* secrets of this project, conditioned by project number, so Admin ›
# Platform secrets stops reading every row as unreadable (permission_denied).
# BGD, 2026-10-24: committed ahead of his `make bootstrap`, so a later plan
# from main keeps the grant instead of destroying it. NOT YET APPLIED when this
# line was committed. Lint the platform_secret_status_grants output with
# iamPolicies:lintPolicy first; the plan is 2 adds / 0 changes / 0 destroys:
# the role (swarmSecretStatusViewer) and its conditioned member for swarm-api.
enable_platform_secret_status = true

# Defaults emptied by DAYONE-TF-1 (a default named this project), kept here so
# this deployment's bootstrap plan is unchanged: Cloud Build's source-staging
# bucket, and the two IAP backends terraform/infra created.
deployer_storage_buckets_exact = ["saga-agents-staging_cloudbuild"]
frontend_iap_backends          = ["swarm-ui-backend", "swarm-ui-ui-backend"]
