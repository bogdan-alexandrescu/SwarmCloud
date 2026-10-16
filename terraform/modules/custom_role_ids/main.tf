# The ids of the platform's custom roles, spelled once for both roots: the
# eight terraform/bootstrap/platform_roles.tf defines, swarmForgeSlotCreator
# and swarmForgeSlotVersionManager (terraform/bootstrap/forge_user_slots.tf,
# #780), and the five swarmWorkspace* roles of swarm-workspace-deployer
# (terraform/bootstrap/workspace_deployer.tf, #847), and swarmHistoryPurgeDeleter
# (terraform/bootstrap/history_purge.tf, the audited history purge), the two
# browser-session roles (terraform/bootstrap/browser_logins.tf),
# swarmProviderKeyWriter (terraform/bootstrap/provider_keys.tf, KEYS-IAM),
# swarmWorkspaceSecretAccessor (terraform/bootstrap/workspace_secrets.tf,
# SEC-IAM), swarmTypesafeForwarderAccessor
# (terraform/bootstrap/typesafe_forwarder.tf) and swarmGithubHooksDedup
# (terraform/bootstrap/github_hooks_role.tf, HOOKS-ROLE).
#
# terraform/bootstrap DEFINES these roles, and the owner applies it.
# terraform/infra and its modules GRANT them, and CI applies that. The two roots
# share no state, so the only thing that can keep "the role bootstrap made" and
# "the role infra binds" the same string is that both read it from here
# (docs/mirrored-values.md, answer (a): derived, nothing to compare).
#
# WHY THE ROLES LEFT terraform/infra (#79, owner decision 2026-09-25). Defining
# them there needed roles/iam.roleAdmin on the CI deployer, and roleAdmin's
# iam.roles.update cannot be conditioned: IAM names no role in a condition and
# a role carries no allow policy of its own. With it, CI could add
# resourcemanager.projects.setIamPolicy to a custom role it already held
# unconditioned, and its next project setIamPolicy -- roles/owner included --
# was authorised by that binding, so no condition on its other grants was ever
# evaluated. Changing one of these roles is now an owner bootstrap apply, and
# the owner accepted that cost.
#
# No resource and no data source here, deliberately: CI holds no iam.roles.*
# permission once roleAdmin is gone (measured 2026-09-25: of the deployer's 18
# live project bindings, only roleAdmin carried any; swarmDeployerProjectBuckets
# is not live yet and carries none by its definition in bootstrap's wif.tf), so
# infra must not so much as read a role. These are plain strings, known at plan.

locals {
  # Appended to every id as "_<suffix>" when non-empty. It exists for one case:
  # a role that was deleted is kept for 7 days and its id cannot be reused, so a
  # re-create inside that window needs a different id.
  #
  # A CONSTANT, not a variable, on purpose. It used to be a variable of
  # terraform/infra; with the roles defined in one root and granted in the
  # other, a suffix set in only one of them makes infra bind roles bootstrap
  # never made -- or, worse, keep binding the ones bootstrap has just deleted.
  # Changing it here changes both roots in the same commit.
  custom_role_suffix = ""

  role_suffix = local.custom_role_suffix == "" ? "" : "_${local.custom_role_suffix}"

  ids = {
    job_dispatcher         = "swarmJobDispatcher${local.role_suffix}"
    job_reaper             = "swarmJobReaper${local.role_suffix}"
    gke_dispatcher         = "swarmGkeDispatcher${local.role_suffix}"
    gke_reaper             = "swarmGkeReaper${local.role_suffix}"
    secret_lister          = "swarmSecretLister${local.role_suffix}"
    worker_firestore       = "swarmTenantWorkerFirestore${local.role_suffix}"
    bucket_metadata_reader = "swarmBucketMetadataReader${local.role_suffix}"
    image_puller           = "swarmImagePuller${local.role_suffix}"
    # Granted by terraform/bootstrap itself, never by terraform/infra: see
    # forge_user_slots.tf there.
    forge_slot_creator         = "swarmForgeSlotCreator${local.role_suffix}"
    forge_slot_version_manager = "swarmForgeSlotVersionManager${local.role_suffix}"
    # swarm-workspace-deployer's (docs/workspaces.md §2.3, #847). Defined and
    # granted by terraform/bootstrap/workspace_deployer.tf alone; terraform/infra
    # never names them.
    workspace_account_admin  = "swarmWorkspaceAccountAdmin${local.role_suffix}"
    workspace_project_reader = "swarmWorkspaceProjectReader${local.role_suffix}"
    workspace_bucket_iam     = "swarmWorkspaceBucketIam${local.role_suffix}"
    workspace_secret_binder  = "swarmWorkspaceSecretBinder${local.role_suffix}"
    workspace_firestore      = "swarmWorkspaceFirestore${local.role_suffix}"
    # Defined by terraform/bootstrap/history_purge.tf; granted by
    # terraform/modules/iam to swarm-api on the artifact bucket, conditioned to
    # tenants/<t>/tasks/ and tenants/<t>/checkpoints/ (owner decision 2026-10-11).
    history_purge_deleter = "swarmHistoryPurgeDeleter${local.role_suffix}"
    # Defined and granted by terraform/bootstrap/browser_logins.tf alone (live
    # browser LB-7, lanes L8b and L8c of #1030); terraform/infra never names
    # them. Both are swarm-api's: no worker holds a browser-session role (L8c).
    browser_session_provisioner = "swarmBrowserSessionProvisioner${local.role_suffix}"
    browser_login_revoker       = "swarmBrowserLoginRevoker${local.role_suffix}"
    # Defined and granted by terraform/bootstrap/provider_keys.tf alone
    # (KEYS-IAM, owner decision 2026-10-14): swarm-api's write and destroy of
    # tenant provider keys. terraform/infra never names it.
    provider_key_writer = "swarmProviderKeyWriter${local.role_suffix}"
    # Defined and granted by terraform/bootstrap/workspace_secrets.tf alone
    # (SEC-IAM, #1347): swarm-api's read of swarm-tenant-<t>-wsec-* secrets,
    # versions.access only. terraform/infra never names it, and no tenant
    # worker holds it.
    workspace_secret_accessor = "swarmWorkspaceSecretAccessor${local.role_suffix}"
    # Defined and granted by terraform/bootstrap/typesafe_forwarder.tf alone
    # (SEC-INJECT-A, #1347): the TypeSafe forwarder's read of
    # swarm-tenant-<t>-typesafe versions, versions.access only. terraform/infra
    # never names it, and no tenant worker holds it.
    typesafe_forwarder_accessor = "swarmTypesafeForwarderAccessor${local.role_suffix}"
    # Defined by terraform/bootstrap/github_hooks_role.tf (HOOKS-ROLE, part of
    # #1384, owner decision 2026-10-16): datastore.entities.create alone, for
    # the GitHub push receiver's delivery dedup. Granted by
    # terraform/infra/github_hooks.tf to the rollup sweeper account the
    # receiver runs as, in place of roles/datastore.user.
    github_hooks_dedup = "swarmGithubHooksDedup${local.role_suffix}"
  }
}
