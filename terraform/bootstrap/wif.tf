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

  # principalSet bound to this repository only.
  github_principal = var.enable_github_wif ? "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repository}" : ""

  ref_condition = join(" || ", [
    for ref in var.github_allowed_refs : "assertion.ref == \"${ref}\""
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
  }

  # Both clauses matter. The repository check stops any other repo; the ref
  # check stops a pull request from a fork, or a branch anyone can push, from
  # minting a deploy token.
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
  count = local.wif_enabled

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.github_principal
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = var.enable_github_wif ? toset(var.deployer_roles) : toset([])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer[0].email}"
}

# State access is granted on the bucket, not on the project, so the deployer
# cannot reach another team's buckets in this shared project.
resource "google_storage_bucket_iam_member" "deployer_state" {
  count = local.wif_enabled

  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deployer[0].email}"
}
