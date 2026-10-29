# The QA identity (lane QI-IAM, part of #1649; docs/design/qa-identity.md,
# option B, owner decision 2026-10-19). terraform/bootstrap/qa_identity.tf
# makes a service account a QA agent signs in to the console AS, through IAP,
# with a short-lived JWT that swarm-api signs and the gate adds. These runs
# hold:
#
#   * OFF BY DEFAULT nothing exists: no account, no role, no policy, and the
#     deployer's grantable list is what it was;
#   * on, the account is qa-console, says managed-by=swarm-terraform, and has NO
#     KEY: no google_service_account_key is declared anywhere in this root;
#   * on, swarmQaJwtSigner holds iam.serviceAccounts.signJwt and NOTHING else --
#     no getAccessToken, getOpenIdToken, signBlob, actAs or implicitDelegation;
#   * on, the account's own IAM policy is written AUTHORITATIVELY with one
#     binding, that role, for swarm-api's runtime account alone, so swarm-api
#     may sign as the QA account and as no other, and nobody else may sign,
#     mint a token or act as it at the account level;
#   * the role is granted on the QA account only: never on the project, and
#     the CI deployer may not grant it (it is not on the grantable list) and
#     holds no serviceAccountAdmin on the account;
#   * on, an account that is not in frontend_iap_members is refused at plan:
#     IAP would answer every signed request 403.
#
# None of this proves what IAM or IAP do live; see the file's
# "NOT VERIFIED LIVE".

mock_provider "google" {
  source = "./mocks/google"
}

# terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1):
# a mock provider cannot import, so the bucket's provider values are the
# override's and its configuration is still planned and asserted.
override_resource {
  target = google_storage_bucket.state
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  infra_tenants_tfvars = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"

  # The grantable list and the deployer's account grants exist only with the
  # GitHub WIF on (github_hooks_role.tftest.hcl does the same).
  enable_github_wif     = true
  github_repository     = "saga/agent-swarm-infra"
  deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

# A distinct string, so pointing the QA account's policy resource at another
# data source fails the equality in on_swarm_api_alone_may_sign_as_the_qa_account.
# File-wide because the mock's generated policy_data is not JSON, and the
# provider refuses it at plan in every run that turns the identity on.
override_data {
  target = data.google_iam_policy.qa_identity
  values = {
    policy_data = "{\"qa\":\"swarm-api-signs\"}"
  }
}

run "off_by_default_nothing_exists" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = (
      length(google_service_account.qa_identity) == 0
      && length(google_project_iam_custom_role.qa_jwt_signer) == 0
      && length(google_service_account_iam_policy.qa_identity) == 0
    )
    error_message = "with enable_qa_identity off (the default) there is no QA account, no swarmQaJwtSigner and no policy on either"
  }

  assert {
    condition     = !var.enable_qa_identity && output.qa_identity.email == "" && output.qa_identity.role == "" && length(output.qa_identity.permissions) == 0
    error_message = "enable_qa_identity defaults to false, and the output names no account, role or permission while it is"
  }
}

run "on_the_account_exists_with_no_key" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_qa_identity   = true
    frontend_iap_members = ["domain:example.com", "serviceAccount:qa-console@saga-agents-staging.iam.gserviceaccount.com"]
  }

  assert {
    condition = alltrue([
      google_service_account.qa_identity[0].account_id == "qa-console",
      google_service_account.qa_identity[0].project == "saga-agents-staging",
      startswith(google_service_account.qa_identity[0].description, "managed-by=swarm-terraform;"),
      !coalesce(google_service_account.qa_identity[0].create_ignore_already_exists, false),
    ])
    error_message = "the QA account is qa-console in this project, says managed-by=swarm-terraform, and is never adopted: an account someone made first under the name is a squat to investigate"
  }

  # No key, in this file or anywhere in the root: a key is a credential that
  # outlives every token and every policy here. swarm-api signs through IAM,
  # where Google holds the key.
  assert {
    condition = length(flatten([
      for f in fileset("../../terraform/bootstrap", "**/*.tf") :
      regexall("(?m)^\\s*resource\\s+\"google_service_account_key\"", file("../../terraform/bootstrap/${f}"))
    ])) == 0 && length(google_service_account.qa_identity) == 1
    error_message = "terraform/bootstrap declares no google_service_account_key: the QA account has no key"
  }
}

run "on_the_role_holds_sign_jwt_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_qa_identity   = true
    frontend_iap_members = ["domain:example.com", "serviceAccount:qa-console@saga-agents-staging.iam.gserviceaccount.com"]
  }

  # The exact set. getAccessToken or getOpenIdToken would mint a token for any
  # audience, signBlob signs anything, actAs deploys code as the account, and
  # implicitDelegation chains through it to another.
  assert {
    condition     = google_project_iam_custom_role.qa_jwt_signer[0].permissions == toset(["iam.serviceAccounts.signJwt"])
    error_message = "swarmQaJwtSigner holds iam.serviceAccounts.signJwt and nothing else"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.qa_jwt_signer[0].role_id == "swarmQaJwtSigner",
      google_project_iam_custom_role.qa_jwt_signer[0].project == "saga-agents-staging",
      google_project_iam_custom_role.qa_jwt_signer[0].stage == "GA",
      startswith(google_project_iam_custom_role.qa_jwt_signer[0].description, "managed-by=swarm-terraform"),
    ])
    error_message = "the role id is the one terraform/modules/custom_role_ids spells, defined in this project at GA, and its description says managed-by=swarm-terraform"
  }

  assert {
    condition = (
      output.qa_identity.email == "qa-console@saga-agents-staging.iam.gserviceaccount.com"
      && output.qa_identity.role == "projects/saga-agents-staging/roles/swarmQaJwtSigner"
      && output.qa_identity.permissions == tolist(["iam.serviceAccounts.signJwt"])
    )
    error_message = "the output gives the owner the account, the role's full name and its one permission, the rows to read the plan against"
  }
}

run "on_swarm_api_alone_may_sign_as_the_qa_account" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_qa_identity   = true
    frontend_iap_members = ["domain:example.com", "serviceAccount:qa-console@saga-agents-staging.iam.gserviceaccount.com"]
  }

  override_resource {
    target          = google_service_account.qa_identity
    override_during = plan
    values = {
      name  = "projects/saga-agents-staging/serviceAccounts/qa-console@saga-agents-staging.iam.gserviceaccount.com"
      email = "qa-console@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = alltrue([
      length(data.google_iam_policy.qa_identity.binding) == 1,
      toset([for b in data.google_iam_policy.qa_identity.binding : b.role]) == toset(["projects/saga-agents-staging/roles/swarmQaJwtSigner"]),
      toset(flatten([for b in data.google_iam_policy.qa_identity.binding : tolist(b.members)])) == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"]),
      alltrue([for b in data.google_iam_policy.qa_identity.binding : length(b.condition) == 0]),
    ])
    error_message = "the QA account's policy has one binding: swarmQaJwtSigner, for swarm-api's runtime account alone"
  }

  assert {
    condition = (
      length(google_service_account_iam_policy.qa_identity) == 1
      && google_service_account_iam_policy.qa_identity[0].service_account_id == "projects/saga-agents-staging/serviceAccounts/qa-console@saga-agents-staging.iam.gserviceaccount.com"
      && google_service_account_iam_policy.qa_identity[0].policy_data == "{\"qa\":\"swarm-api-signs\"}"
    )
    error_message = "the policy is written AUTHORITATIVELY on the QA account, so a token creator or actAs member added there by hand is removed on the next apply"
  }

  # Granted on the account, never on the project: a project grant of signJwt
  # reaches every account in a project shared with another team. Read as text:
  # qa_identity.tf declares no project-level IAM at all, and no other file of
  # this root names the role.
  assert {
    condition = (
      length(regexall("(?m)^\\s*resource\\s+\"google_project_iam_(member|binding|policy)\"", file("../../terraform/bootstrap/qa_identity.tf"))) == 0
      && length([
        for f in fileset("../../terraform/bootstrap", "**/*.tf") : f
        if f != "qa_identity.tf" && strcontains(file("../../terraform/bootstrap/${f}"), "qa_jwt_signer")
      ]) == 0
      && length(google_service_account_iam_policy.qa_identity) == 1
    )
    error_message = "swarmQaJwtSigner is never granted at the project: qa_identity.tf binds it on the QA account alone"
  }
}

run "on_the_deployer_cannot_reach_the_qa_account" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_qa_identity   = true
    frontend_iap_members = ["domain:example.com", "serviceAccount:qa-console@saga-agents-staging.iam.gserviceaccount.com"]
  }

  assert {
    condition = alltrue([
      for r in local.deployer_grantable_project_roles : !strcontains(r, "swarmQaJwtSigner")
    ]) && length(google_service_account.qa_identity) == 1
    error_message = "the CI deployer may not grant swarmQaJwtSigner: only this root, which the owner applies, binds it"
  }

  assert {
    condition = (
      toset(output.deployer_grantable_project_roles) == toset(run.off_by_default_nothing_exists.deployer_grantable_project_roles)
      && !contains(output.deployer_admin_accounts, "qa-console")
      && length(output.deployer_admin_accounts) == length(run.off_by_default_nothing_exists.deployer_admin_accounts)
    )
    error_message = "turning the QA identity on changes neither the deployer's grantable roles nor the accounts it administers: CI holds no serviceAccountAdmin on qa-console and could not grant itself the signer role there"
  }
}

run "on_an_account_iap_does_not_admit_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_qa_identity = true
  }

  expect_failures = [google_service_account.qa_identity]
}
