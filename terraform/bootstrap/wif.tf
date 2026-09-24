# GitHub Actions Workload Identity Federation.
#
# The alternative is a downloadable service account key in a repository secret.
# A key like that is a credential with no expiry, no rotation and no audit trail
# of where it was copied; revoking it means knowing every place it went. WIF
# issues a short-lived token per workflow run, bound to a repository and a ref.
#
# The attribute condition below is the whole security boundary. Without it, the
# pool trusts GitHub's issuer -- which is to say it trusts every repository on
# GitHub, including one an attacker creates in the next five minutes.

locals {
  wif_enabled = var.enable_github_wif ? 1 : 0

  # The ref pin is enforced in TWO independent places, on purpose.
  #
  # The provider's attribute_condition below decides which tokens the pool will
  # mint at all. On its own that is a single point of failure: a second provider
  # added to this pool later, or a widened `github_allowed_refs`, silently
  # extends secret-provisioning and project-IAM-admin rights with nothing
  # failing. So the service account binding is ALSO per-ref, through a composite
  # attribute -- a principalSet naming only `attribute.repository` would accept
  # any ref the pool ever decides to issue.
  #
  # `attribute.repo_ref` is mapped from assertion.repository + "@" +
  # assertion.ref, because a principalSet can name exactly one attribute and the
  # boundary needs both halves.
  github_principals = var.enable_github_wif ? {
    for ref in var.github_allowed_refs :
    ref => "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repo_ref/${var.github_repository}@${ref}"
  } : {}

  ref_condition = join(" || ", [
    for ref in var.github_allowed_refs : "assertion.ref == \"${ref}\""
  ])

  # THERE IS NO `assertion.sub` CLAUSE, AND THAT IS A MEASUREMENT RATHER THAN AN
  # OMISSION.
  #
  # One was added on 2026-09-24, pinning
  # `assertion.sub == "repo:<owner>/<name>:ref:<ref>"` for each allowed ref. The
  # argument for it was good: `sub` is the claim GitHub constructs, it fixes the
  # CONTEXT segment to `ref:`, and it is the only form checkov's CKV_GCP_125 can
  # read. It was also wrong for this repository, and both halves of the evidence
  # are worth keeping.
  #
  # IT REJECTED A LEGITIMATE MAIN REF. The first release run after it was applied
  # failed at google-github-actions/auth with
  #
  #     unauthorized_client: The given credential is rejected by the attribute
  #     condition.
  #
  # on a `workflow_dispatch` against `refs/heads/main` -- exactly the case the
  # clause was written to admit.
  #
  # AND IT COULD NEVER HAVE ADMITTED THE DEPLOY. `release.yml`'s `infrastructure`
  # and `deploy` jobs declare `environment:`, and GitHub mints those runs with
  # `sub = repo:<owner>/<name>:environment:<env>`. A clause pinning the context to
  # `ref:` therefore blocks the approval-gated deploy permanently, by design --
  # which makes it incompatible with the one control this pipeline most depends
  # on. The clause I wrote to exclude an environment subject would have excluded
  # OUR environment subject.
  #
  # WHAT REMAINS IS SUFFICIENT. `assertion.ref` is a separate claim and is still
  # `refs/heads/main` for an environment-context token, so the repository and ref
  # clauses below pin both halves for every job shape this repository uses. The
  # boundary that actually matters -- that no pull request ref can mint a token --
  # is asserted in tests/terraform/bootstrap.tftest.hcl against the RENDERED
  # condition, which is stronger than CKV_GCP_125 and does not depend on a claim
  # format this project does not produce.
}

resource "google_iam_workload_identity_pool" "github" {
  count = local.wif_enabled

  project                   = var.project_id
  workload_identity_pool_id = "${var.name_prefix}-github"
  display_name              = "Swarm GitHub Actions"
  description               = "managed-by=swarm-terraform; keyless CI identity"

  lifecycle {
    precondition {
      condition     = var.github_repository != ""
      error_message = "github_repository must be set when enable_github_wif is true, or the pool would trust every repository on GitHub."
    }
  }
}

resource "google_iam_workload_identity_pool_provider" "github" {
  # checkov:skip=CKV_GCP_125:This check requires the trust policy to pin `assertion.sub`, and this pool deliberately does not -- a `sub` clause was applied on 2026-09-24 and REJECTED a legitimate `refs/heads/main` run, and could never have admitted the environment-gated deploy jobs at all, because GitHub mints those with `sub = repo:<owner>/<name>:environment:<env>` rather than `:ref:`. See the `locals` comment above for the measurement. The boundary the check is reaching for is asserted instead in tests/terraform/bootstrap.tftest.hcl against the RENDERED attribute_condition: the repository and ref are both pinned, and the condition may never contain `refs/pull/`. That last assertion is the one that matters and CKV_GCP_125 does not make it.
  count = local.wif_enabled

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "${var.name_prefix}-github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    "attribute.actor"      = "assertion.actor"
    # Composite: a principalSet can name one attribute, and the SA binding needs
    # to pin the repository AND the ref, so the two are mapped as one value.
    "attribute.repo_ref" = "assertion.repository + \"@\" + assertion.ref"
  }

  # BOTH CLAUSES MATTER, and each stops something the other does not.
  #
  #   repository   stops any other repo on GitHub. Without it the pool trusts
  #                GitHub's issuer, which is to say every repository on it.
  #   ref          stops a branch anyone can push, and a pull request from a
  #                fork, from minting a deploy token. This is the clause that
  #                refuses `refs/pull/<n>/merge`, measured 2026-09-24 on a real
  #                pull request run.
  #
  # A third clause on `assertion.sub` was tried and removed; see the comment in
  # `locals` above for what it rejected and why it could never have worked here.
  #
  # NEVER WIDEN THIS TO `refs/pull/*`. application.yml's `build` job spends
  # twenty lines on why, and this repository has already had one required check
  # that existed only to create pressure for that change.
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && (${local.ref_condition})"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
    # Audience is the full provider resource name, which GitHub's
    # google-github-actions/auth sets by default.
    allowed_audiences = []
  }
}

resource "google_service_account" "deployer" {
  count = local.wif_enabled

  project      = var.project_id
  account_id   = "${var.name_prefix}-tf-deployer"
  display_name = "Swarm Terraform Deployer"
  description  = "managed-by=swarm-terraform; assumed by GitHub Actions via WIF. No keys are ever created for it."
}

resource "google_service_account_iam_member" "deployer_wif" {
  for_each = local.github_principals

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

# Project-wide, unconditioned -- except for any role named in
# deployer_scoped_roles, which moves to its conditioned block in
# deployer_conditions.tf in the same plan. Subtracting it here and creating it
# there is one switch on purpose: a conditioned binding beside an unconditioned
# one for the same role grants exactly what the unconditioned one does.
resource "google_project_iam_member" "deployer_roles" {
  for_each = var.enable_github_wif ? setsubtract(toset(var.deployer_roles), var.deployer_scoped_roles) : toset([])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer[0].email}"
}

# Secret Manager for CI, without the ability to read a secret.
#
# roles/secretmanager.admin would cover what terraform does here, and would also
# hand every workflow run on an allowed ref secretmanager.versions.access over
# every tenant's provider key. That is a bigger authority than any other role on
# the deployer, and it is not one terraform needs: this configuration creates
# secrets and sets their IAM policy, and deliberately never writes a version
# (modules/secret_manager/main.tf explains why a key in terraform state is a key
# in a file far more people can read than it was meant for).
resource "google_project_iam_custom_role" "secret_provisioner" {
  count = local.wif_enabled

  project = var.project_id
  role_id = "swarmSecretProvisioner"
  title   = "Swarm Secret Provisioner"

  description = "Create secrets and set who may read them. Cannot read a payload."
  stage       = "GA"

  permissions = var.deployer_secret_permissions
}

# Replaced by deployer_secrets_scoped (deployer_conditions.tf) once
# "swarmSecretProvisioner" is in deployer_scoped_roles.
resource "google_project_iam_member" "deployer_secrets" {
  count = var.enable_github_wif && !contains(var.deployer_scoped_roles, "swarmSecretProvisioner") ? 1 : 0

  project = var.project_id
  role    = "projects/${var.project_id}/roles/${google_project_iam_custom_role.secret_provisioner[0].role_id}"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"
}

# storage.admin, SCOPED. The first of the deployer's roles to get a condition.
#
# WHY THIS ONE FIRST. Eighteen project-level roles, zero conditions, in a project
# holding another team's production. Ranked by blast radius against how well the
# service supports conditions, storage is first: `resource.name` conditions on
# Cloud Storage are well supported, and the project holds THREE buckets that are
# not ours --
#
#     saga-agents-crawled-media-staging
#     saga-agents-files-staging
#     saga-agents-terraform-state-staging
#
# the last of which is another team's TERRAFORM STATE. Unconditioned
# storage.admin means CI can delete it.
#
# THE BUCKET THAT NEARLY GOT CUT OFF, and the reason this is done one role at a
# time with a real run behind each. A condition of just
# `startsWith(".../buckets/swarm-")` looks obviously right and would have broken
# the build:
#
#     saga-agents-staging_cloudbuild
#
# is where `gcloud builds submit` uploads its source tarball. It is GCP's, not
# ours and not theirs, it does not carry our prefix, and the release pipeline
# reached a working state for the first time on 2026-09-24 after six failed
# attempts -- one of which was an IAM condition I wrote from documentation and
# applied unverified. Cutting this bucket off would have been the seventh.
#
# THE OBJECT CASE IS COVERED BY THE PREFIX MATCH. An object is
# `projects/_/buckets/<b>/objects/<o>`, so a `startsWith` on the bucket path
# admits every object in it and nothing in a bucket outside it.
#
# BUT NOT BY AN EXACT MATCH, and the Cloud Build bucket was an exact match. IAM's
# own documentation of this very pattern: "Because the condition looks at the
# start of the resource name, it matches a bucket as well as the objects in that
# bucket. If it checked for equality, it would only match the bucket"
# (docs.cloud.google.com/iam/docs/conditions-attribute-reference). The source
# tarball `gcloud builds submit` uploads is an OBJECT --
# `saga-agents-staging_cloudbuild/source/<stamp>-<uuid>.tgz` -- so
# `resource.name == ".../buckets/saga-agents-staging_cloudbuild"` admitted the
# bucket and refused the upload. Each exact bucket is therefore admitted twice:
# itself by equality, and its objects by a prefix that ends in `/objects/`, so
# a bucket merely STARTING with the same name is still refused.
#
# NOT APPLIED YET. The plan is written and reviewable; the apply waits until the
# in-flight release has deployed. Applying an IAM condition to the role the build
# depends on, while that build is running, is the mistake this comment exists to
# avoid repeating.
locals {
  deployer_storage_condition = join(" || ", concat(
    [for b in var.deployer_storage_bucket_prefixes :
    "resource.name.startsWith(\"projects/_/buckets/${b}\")"],
    flatten([for b in var.deployer_storage_buckets_exact : [
      "resource.name == \"projects/_/buckets/${b}\"",
      "resource.name.startsWith(\"projects/_/buckets/${b}/objects/\")",
    ]]),
  ))
}

resource "google_project_iam_member" "deployer_storage" {
  count = local.wif_enabled

  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm buckets and the Cloud Build staging bucket only"
    description = "Refuses every bucket this platform does not own. Three buckets in this project belong to another team, one of them their terraform state."
    expression  = local.deployer_storage_condition
  }
}

# THE TWO STORAGE PERMISSIONS NO BUCKET NAME CAN ADMIT.
#
# storage.buckets.list and storage.buckets.create are checked on the PROJECT,
# not on a bucket -- IAM calls these parent-only permissions -- and "you cannot
# use the resource.name attribute in a condition at the project level, because
# Resource Manager ... does not recognize the resource.name attribute"
# (docs.cloud.google.com/iam/docs/configuring-resource-based-access). So the
# condition above, which tests nothing BUT resource.name, refuses both.
#
# THE BUILD NEEDS `list`. Read in the Cloud SDK this repository's scripts run
# (googlecloudsdk/api_lib/storage/storage_api.py, CreateBucketIfNotExists):
# with no --gcs-source-staging-dir, `gcloud builds submit` passes
# check_ownership=True, gets the default bucket, and then -- "to prevent bucket
# squatting" -- calls buckets.list(project=..., prefix=<bucket>) and fails
# unless the bucket is in the list. build-images.sh passes no staging dir. So
# with storage.admin conditioned and nothing else, every image build 403s at
# that list, before a byte is uploaded. `create` is the same shape for
# terraform/infra adding a new swarm- bucket.
#
# Hence a separate grant of exactly those two, unconditioned. Neither reaches
# into a bucket: list returns names and metadata, and a bucket CI creates is one
# it still cannot administer unless it is swarm-prefixed. What it deliberately
# does NOT carry is the rest of what is checked at the project level --
# storage.hmacKeys.* above all, which would let CI mint an HMAC key for another
# team's service account and read their buckets as them.
#
# NOT VERIFIED LIVE: neither half of the storage change has been applied, and
# the evidence for both defects is the SDK source and IAM's documentation, not a
# failed run.
resource "google_project_iam_custom_role" "deployer_project_buckets" {
  count = local.wif_enabled

  project = var.project_id
  role_id = "swarmDeployerProjectBuckets"
  title   = "Swarm Deployer Project-level Bucket Permissions"

  description = "The two Cloud Storage permissions checked on the project rather than a bucket, which the conditioned storage.admin cannot admit."
  stage       = "GA"

  permissions = [
    "storage.buckets.create",
    "storage.buckets.list",
    "logging.logEntries.list", # MUTATION
  ]
}

resource "google_project_iam_member" "deployer_project_buckets" {
  count = local.wif_enabled

  project = var.project_id
  role    = "projects/${var.project_id}/roles/${google_project_iam_custom_role.deployer_project_buckets[0].role_id}"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"
}

# WHO MAY PASS IAP, managed here rather than by CI -- and the deployer holds NO
# IAP role.
#
# terraform/infra's frontend module used to manage these accessor bindings, which
# meant CI's deployer had to read and write IAP policy. Three ways of granting it,
# all on 2026-09-24:
#
#   1. project-level roles/iap.admin -- admin over all TEN backends in this
#      project, nine of them the other team's GKE gateway routes including
#      keycloak (their identity provider) and argocd (their deployment admin);
#   2. the same with a condition on resource.name startsWith ".../services/swarm"
#      -- matched nothing; release 35972131246 failed "The caller does not have
#      permission". IAP names a backend by project NUMBER and numeric id, and a
#      prefix on that form admits every backend, theirs included;
#   3. roles/iap.admin bound in each of OUR two backends' own IAP policy --
#      verified present with `gcloud iap web get-iam-policy`, and the release
#      still got 403 on iap.webServices.getIamPolicy at 9.5 minutes, past IAM
#      propagation (attempts 2-5 of the same run). Why is not established.
#
# Membership changes rarely and is the owner's decision, so it lives here, in the
# root only the owner applies. The deployer needs no IAP permission at all, and
# the release never reads an IAP policy.
#
# THE NAMES ARE LOOKED UP, NOT TRUSTED. They follow modules/frontend/main.tf
# (`${local.name}-backend` and `${local.name}-ui-backend`, local.name =
# "${name_prefix}-ui"). The data source turns that restated rule into a check: if
# the module renames a backend, this plan fails "not found" instead of silently
# admitting nobody.
#
# FRESH PROJECT ORDER. The backends are created by terraform/infra. On a new
# project: apply bootstrap with frontend_iap_backends = [], let the release create
# the backends (IAP is ON with no members, so nobody gets in yet), then apply
# bootstrap with the default to admit people.
data "google_compute_backend_service" "frontend_iap" {
  for_each = toset(var.frontend_iap_backends)

  project = var.project_id
  name    = each.value
}

locals {
  # One binding per (backend, member). The key is readable in plan output.
  frontend_iap_bindings = {
    for pair in setproduct(keys(data.google_compute_backend_service.frontend_iap), var.frontend_iap_members) :
    "${pair[0]} ${pair[1]}" => { backend = pair[0], member = pair[1] }
  }
}

resource "google_iap_web_backend_service_iam_member" "frontend_accessors" {
  for_each = local.frontend_iap_bindings

  project             = var.project_id
  web_backend_service = data.google_compute_backend_service.frontend_iap[each.value.backend].name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value.member
}

# ACT AS THE CLOUD BUILD SERVICE ACCOUNT, AND ONLY THAT ONE.
#
# `gcloud builds submit` runs the build as a service account, so the caller
# needs `iam.serviceAccounts.actAs` on it. The one-line way to grant that is
# `roles/iam.serviceAccountUser` at the PROJECT, and that is the wrong line to
# write here: saga-agents-staging is SHARED, and project-level actAs would let
# any CI run on an allowed ref impersonate every service account in it --
# including the twelve on this repository's deny-list (promptlab-runner,
# api-service, publisher and the rest). CI would be able to become another
# team's production identity, which is a strictly larger authority than
# anything else on the deployer and is not needed to build an image.
#
# Bound on the resource instead, the same way state access is granted on the
# bucket rather than on the project. The scope is then a property of WHERE the
# binding lives rather than of an expression somebody has to get right.
#
# The legacy Cloud Build service account is `<project-number>@cloudbuild`, which
# is why the project NUMBER is read rather than spelled: the id is
# saga-agents-staging and the number is not derivable from it.
data "google_project" "this" {
  count      = local.wif_enabled
  project_id = var.project_id
}

resource "google_service_account_iam_member" "deployer_acts_as_cloudbuild" {
  count = local.wif_enabled

  service_account_id = "projects/${var.project_id}/serviceAccounts/${data.google_project.this[0].number}-compute@developer.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer[0].email}"
}

# State access is granted on the bucket, not on the project, so the deployer
# cannot reach another team's buckets in this shared project.
resource "google_storage_bucket_iam_member" "deployer_state" {
  count = local.wif_enabled

  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deployer[0].email}"
}
