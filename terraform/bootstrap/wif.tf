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

  # THE SAME PIN A THIRD TIME, through the claim GitHub itself constructs.
  #
  # `sub` is minted by GitHub as `repo:<owner>/<name>:<context>:<value>`, and
  # for a branch run that is `repo:owner/name:ref:refs/heads/main`. Pinning it
  # is STRICTER than the repository and ref clauses above rather than a
  # restatement of them: those two are satisfied by any token carrying the
  # right repository and ref attributes, while this one also fixes the CONTEXT
  # segment to `ref:`. A token minted for `repo:owner/name:environment:prod` or
  # `repo:owner/name:pull_request` does not match it, whatever else it carries.
  #
  # It is also the only form checkov's CKV_GCP_125 recognises, and that check
  # is right to insist: it reads `assertion.sub`, rejects the abusable claims
  # (`actor`, `workflow`, and the rest -- any of which an attacker controls by
  # naming a workflow), rejects a wildcard in either half, and requires the
  # repo value to be a real `org/name`. A condition it cannot read is a
  # condition nobody is checking on our behalf.
  #
  # Derived from the same two variables as the clauses above, so the three
  # cannot drift apart.
  sub_condition = join(" || ", [
    for ref in var.github_allowed_refs :
    "assertion.sub == \"repo:${var.github_repository}:ref:${ref}\""
  ])
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
  # checkov:skip=CKV_GCP_125:The pin this check looks for IS present -- `local.sub_condition` below composes `assertion.sub == "repo:<owner>/<name>:ref:<ref>"` for every allowed ref -- but checkov reads `attribute_condition` as written text and does not resolve a `join()` over a `for` comprehension, so it sees `${local.sub_condition}` and reports the pin as absent. The property is gated instead in tests/terraform/bootstrap.tftest.hcl, against the RENDERED condition, where terraform has evaluated the local: it asserts the sub clause is present for the allowed ref AND that the condition never contains `refs/pull/`. That is strictly more than this check tests, and it is run by `make test` and by the terraform workflow.
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

  # All three clauses matter, and each stops something the others do not.
  #
  #   repository   stops any other repo on GitHub. Without it the pool trusts
  #                GitHub's issuer, which is to say every repository on it.
  #   ref          stops a branch anyone can push, and a pull request from a
  #                fork, from minting a deploy token.
  #   sub          stops a token minted for a different CONTEXT on an allowed
  #                ref -- an environment or a pull_request subject -- and is
  #                the form CKV_GCP_125 reads (see `sub_condition` above).
  #
  # WHAT IS MEASURED AND WHAT IS NOT, as of 2026-09-24.
  #
  # REJECT DIRECTION: VERIFIED. A pull_request run on
  # fix/silent-failures-env-parity-and-audits was refused with
  #
  #     unauthorized_client: The given credential is rejected by the
  #     attribute condition.
  #
  # which is the ref clause doing its job on `refs/pull/<n>/merge`.
  #
  # ACCEPT DIRECTION: VERIFIED FOR repository+ref, NOT YET FOR sub. A push to
  # main authenticated successfully as swarm-tf-deployer@ -- but that ran
  # against the condition BEFORE the sub clause was applied, so what it proves
  # is the first two clauses. The `sub` format is GitHub's documented
  # `repo:<owner>/<name>:ref:<git-ref>` for a branch run and the next main run
  # is what confirms it.
  #
  # IF THAT RUN IS REJECTED: the sub format is wrong, not the pin. Read the
  # run's OIDC claims and correct the format here. Do NOT widen the condition,
  # and never to `refs/pull/*` -- application.yml's `build` job spends twenty
  # lines on why, and this repository has already had one required check that
  # existed only to create pressure for that change.
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && (${local.ref_condition}) && (${local.sub_condition})"

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
