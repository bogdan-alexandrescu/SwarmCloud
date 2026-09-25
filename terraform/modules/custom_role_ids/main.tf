# The ids of the platform's eight custom roles, spelled once for both roots.
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
  }
}
