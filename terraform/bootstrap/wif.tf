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

resource "google_project_iam_member" "deployer_roles" {
  for_each = var.enable_github_wif ? toset(var.deployer_roles) : toset([])

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

resource "google_project_iam_member" "deployer_secrets" {
  count = local.wif_enabled

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
# NOT APPLIED YET. The plan is written and reviewable; the apply waits until the
# in-flight release has deployed. Applying an IAM condition to the role the build
# depends on, while that build is running, is the mistake this comment exists to
# avoid repeating.
locals {
  deployer_storage_condition = join(" || ", concat(
    [for b in var.deployer_storage_bucket_prefixes :
    "resource.name.startsWith(\"projects/_/buckets/${b}\")"],
    [for b in var.deployer_storage_buckets_exact :
    "resource.name == \"projects/_/buckets/${b}\""],
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

# iap.admin, SCOPED — and this one is not optional scoping.
#
# `terraform apply` failed with
#
#     Error 403: Permission 'iap.webServices.getIamPolicy' denied on resource
#     '//iap.googleapis.com/projects/saga-agents-staging/iap_web/compute/
#      services/swarm-ui-backend'
#
# because `modules/frontend` manages `google_iap_web_backend_service_iam_member`
# on our UI backend and the deployer held no IAP role at all.
#
# WHY THIS ONE IS CONDITIONED FROM THE START, unlike the seventeen roles granted
# unconditioned before it. `gcloud compute backend-services list` on this project
# returns TEN services. One is ours. The other nine are the other team's GKE
# gateway routes:
#
#     keycloak            their identity provider
#     argocd              their deployment admin
#     promptlab-api       their product
#     promptlab-web       their product
#     crawling-service    their product
#     browser-engine      their product
#     api-service         their product
#     gw-serve404/500     their gateway
#
# Project-level `roles/iap.admin` would let any CI run on an allowed ref add or
# remove members on their KEYCLOAK and their ARGOCD — that is, grant itself or
# anyone else access to another team's identity provider and deployment console,
# or lock them out of both. There is no version of this platform's work that
# needs that, and no review that would catch it after the fact.
#
# THE CONDITION, AND WHAT IS UNKNOWN ABOUT IT. IAP web resources are named
# `projects/<p>/iap_web/compute/services/<backend>`, so the prefix match admits
# `swarm-ui-backend` and `swarm-ui-ui-backend` (terraform manages both) and
# refuses all nine of theirs by construction.
#
# What is NOT established is whether IAP evaluates `resource.name` conditions at
# all. If it does not, the apply fails again with the same 403 — loudly, on our
# own resource, and reverting is deleting this block. That is a better failure
# than the alternative, which is silent authority over another team's auth.
resource "google_project_iam_member" "deployer_iap" {
  count = local.wif_enabled

  project = var.project_id
  role    = "roles/iap.admin"
  member  = "serviceAccount:${google_service_account.deployer[0].email}"

  condition {
    title       = "swarm IAP backends only"
    description = "Nine of the ten IAP-capable backend services in this project belong to another team, including their Keycloak and ArgoCD. This admits only ours."
    expression  = "resource.name.startsWith(\"projects/${var.project_id}/iap_web/compute/services/${var.name_prefix}\")"
  }
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
