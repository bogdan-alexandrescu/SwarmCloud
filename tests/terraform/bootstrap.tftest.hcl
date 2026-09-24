# The bootstrap root: the state bucket, the prerequisite APIs, and the keyless
# CI identity. It runs on LOCAL state because a root cannot create the bucket
# its own backend lives in -- terraform initialises the backend before it plans
# a single resource.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"
}

run "state_survives_the_ways_a_platform_is_usually_lost" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = google_storage_bucket.state.name == "swarm-tfstate-saga-agents-staging"
    error_message = "the state bucket carries the swarm prefix and is unique by project id"
  }

  assert {
    condition     = google_storage_bucket.state.versioning[0].enabled == true
    error_message = "versioning is what makes a corrupted or truncated state file recoverable"
  }

  assert {
    condition     = google_storage_bucket.state.uniform_bucket_level_access == true
    error_message = "object ACLs must not be able to widen access to state"
  }

  assert {
    condition     = google_storage_bucket.state.public_access_prevention == "enforced"
    error_message = "state contains every resource id in the platform; no future IAM change may make it public"
  }

  assert {
    condition     = google_storage_bucket.state.force_destroy == false
    error_message = "this bucket holds the state of every other root"
  }

  assert {
    condition     = google_storage_bucket.state.soft_delete_policy[0].retention_duration_seconds >= 604800
    error_message = "a bucket deleted by accident is a platform that can no longer be managed"
  }

  assert {
    condition = length([
      for r in google_storage_bucket.state.lifecycle_rule : r
      if one(r.action).type == "Delete" && coalesce(one(r.condition).num_newer_versions, 0) > 0
    ]) == 1
    error_message = "keep a deep version history, but not an infinite one"
  }

  # A retention_policy would make objects immutable for its duration, and
  # terraform rewrites the state object on every single apply.
  assert {
    condition     = length(google_storage_bucket.state.retention_policy) == 0
    error_message = "a retention policy on the state bucket would block terraform's own writes"
  }
}

run "too_few_state_versions_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    state_noncurrent_versions_to_keep = 2
  }

  expect_failures = [var.state_noncurrent_versions_to_keep]
}

run "ci_is_keyless_and_bound_to_one_repository_and_ref" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  # Without an attribute condition the pool trusts GitHub's issuer -- which is
  # to say every repository on GitHub, including one created five minutes from
  # now.
  assert {
    condition     = strcontains(google_iam_workload_identity_pool_provider.github[0].attribute_condition, "assertion.repository == \"saga/agent-swarm-infra\"")
    error_message = "the provider must be pinned to this repository"
  }

  assert {
    condition     = strcontains(google_iam_workload_identity_pool_provider.github[0].attribute_condition, "assertion.ref == \"refs/heads/main\"")
    error_message = "without a ref condition a fork's pull request could mint a deploy token"
  }

  # THERE IS DELIBERATELY NO `assertion.sub` ASSERTION HERE.
  #
  # One existed, requiring
  # `assertion.sub == "repo:saga/agent-swarm-infra:ref:refs/heads/main"` in the
  # rendered condition, to gate a pin checkov could not read through a `join()`
  # over a `for`. The pin itself was then REMOVED from wif.tf for two measured
  # reasons: it rejected a legitimate `refs/heads/main` run
  # (`unauthorized_client: The given credential is rejected by the attribute
  # condition`), and it could never have admitted `release.yml`'s
  # environment-gated `deploy` job at all, because GitHub mints those runs with
  # `sub = repo:<owner>/<name>:environment:<env>` rather than `:ref:`.
  #
  # The assertion went with it rather than being widened to accept both context
  # forms -- at which point it would add nothing the repository and ref clauses
  # do not already say.

  # AND NOTHING WIDENS IT TO A PULL REQUEST. The single line in this file that
  # would matter most if it were ever deleted: a PR ref minting a token that
  # holds resourcemanager.projectIamAdmin on a SHARED project is the failure
  # this whole pool exists to prevent, and "fix CI by adding refs/pull/*" is
  # the exact pressure that would do it.
  assert {
    condition = !strcontains(
      google_iam_workload_identity_pool_provider.github[0].attribute_condition,
      "refs/pull/",
    )
    error_message = "the trust policy admits a pull request ref; anyone can open a pull request"
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github[0].oidc[0].issuer_uri == "https://token.actions.githubusercontent.com"
    error_message = "the issuer must be GitHub's own OIDC endpoint"
  }

  assert {
    condition     = google_service_account.deployer[0].account_id == "swarm-tf-deployer"
    error_message = "CI gets its own swarm-prefixed identity, not a reused one"
  }

  assert {
    condition     = alltrue([for m in values(google_service_account_iam_member.deployer_wif) : m.role == "roles/iam.workloadIdentityUser"])
    error_message = "federation replaces a downloadable key entirely"
  }

  # State access is granted on the bucket, not on the project, so CI cannot
  # reach another team's buckets in this shared project.
  assert {
    condition     = google_storage_bucket_iam_member.deployer_state[0].bucket == google_storage_bucket.state.name
    error_message = "the deployer's storage grant is scoped to the state bucket"
  }
}

run "ci_is_never_granted_owner" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles    = ["roles/owner"]
  }

  expect_failures = [var.deployer_roles]
}

run "a_malformed_repository_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "not-a-repo-spec"
  }

  expect_failures = [var.github_repository]
}

run "the_prerequisite_apis_are_the_ones_terraform_itself_needs" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    manage_project_services = true
  }

  assert {
    condition = alltrue([
      for s in ["cloudresourcemanager.googleapis.com", "iam.googleapis.com", "serviceusage.googleapis.com", "storage.googleapis.com", "sts.googleapis.com"] :
      contains(output.enabled_services, s)
    ])
    error_message = "without these, terraform/infra cannot authenticate or even read the project"
  }
}

run "api_enablement_can_never_cascade_in_a_shared_project" {
  command = plan

  module {
    source = "../../terraform/modules/project_services"
  }

  variables {
    project_id = "saga-agents-staging"
    services   = ["run.googleapis.com", "container.googleapis.com"]
  }

  # A destroy that disabled container.googleapis.com would take down the other
  # team's live agents-staging cluster -- the exact blast radius this platform
  # is required never to have.
  assert {
    condition = alltrue([
      for k, s in google_project_service.this :
      s.disable_on_destroy == false && s.disable_dependent_services == false
    ])
    error_message = "API disablement must never cascade, and destroy must never disable anything"
  }
}

run "turning_api_disablement_on_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/project_services"
  }

  variables {
    project_id         = "saga-agents-staging"
    services           = ["run.googleapis.com"]
    disable_on_destroy = true
  }

  expect_failures = [var.disable_on_destroy]
}
