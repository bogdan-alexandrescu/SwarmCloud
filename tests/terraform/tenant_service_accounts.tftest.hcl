# Contract request 30: a tenant may list service accounts that resolve to it
# by an exact match on the email AND unique id their verified token carries.
#
# Terraform is the plan-time half of the rule settings.py enforces at startup:
# only a user-managed service account in THIS project, under ONE tenant, never
# an admin, never another tenant's principal. Every refusal below is a plan
# that must not happen; the last two runs pin what is rendered for swarm-api.
#
# A file of its own rather than more runs in tenancy.tftest.hcl: that file's
# file-level variables are the tenancy MODULE's, and these runs plan the whole
# terraform/infra root, which needs the image digests below instead.
#
# `data.google_service_account.listed` is overridden, so no run makes a real
# GCP call for the account's unique id.

# source: the shared defaults every suite that plans terraform/infra needs
# (mocks/google/kms.tfmock.hcl -- the step-spec key's enabled version 1).
mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}

run "an_account_under_two_tenants_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # It decides the account's secrets, GCS prefix and namespace; two listings
  # would make that depend on rendering order. Case differs on purpose.
  variables {
    tenants = {
      eng      = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
      research = { kind = "group", principal = "research@saga.xyz", providers = [], service_accounts = ["SWARM-CI-FIX@saga-agents-staging.iam.gserviceaccount.com"] }
    }
  }

  expect_failures = [var.tenants]
}

run "a_human_address_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # A person reaches a tenant through its group, never through a listing.
  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["bogdan@saga.xyz"] }
    }
  }

  expect_failures = [var.tenants]
}

run "an_iam_member_string_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # swarm-api compares the bare email from the token; a prefix matches nobody.
  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["serviceAccount:swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
    }
  }

  expect_failures = [var.tenants]
}

run "a_google_managed_account_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # Every workload in the project can run as the compute default account, so
  # listing it would put the whole project in the tenant.
  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["123456789012-compute@developer.gserviceaccount.com"] }
    }
  }

  expect_failures = [var.tenants]
}

run "an_account_in_another_project_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # The shape alone fits any project; the pin to var.project_id refuses this.
  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@other-project.iam.gserviceaccount.com"] }
    }
  }

  expect_failures = [var.tenants]
}

run "an_account_in_admin_users_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # A listing gives exactly one tenant's rights; an admin entry adds every tenant's.
  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
    }
    admin_users = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
  }

  expect_failures = [var.tenants]
}

run "an_account_in_admin_pool_users_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
    }
    admin_pool_users = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
  }

  expect_failures = [var.tenants]
}

run "an_account_in_secret_admin_members_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # secret_admin_members holds IAM member strings; the prefix is stripped
  # before comparing, so the IAM spelling of the same account is still caught.
  # Reported by secret_admin_members' own validation, not by var.tenants: that
  # variable already reads var.tenants, so the reverse read would be a cycle.
  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
    }
    secret_admin_members = ["serviceAccount:swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
  }

  expect_failures = [var.secret_admin_members]
}

run "an_account_in_a_tenants_own_secret_admins_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # The per-tenant list is refused too, including the listing tenant's own:
  # a listing never carries the ability to replace a provider key.
  variables {
    tenants = {
      eng = {
        kind             = "group"
        principal        = "eng@saga.xyz"
        providers        = []
        service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
        secret_admins    = ["serviceAccount:swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
      }
    }
  }

  expect_failures = [var.tenants]
}

run "an_account_that_is_another_tenants_principal_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  # It already has a tenant of its own; a listing would give it a second.
  variables {
    tenants = {
      eng     = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
      "u-bot" = { kind = "user", principal = "swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com", providers = [] }
    }
  }

  expect_failures = [var.tenants]
}

run "the_listing_renders_as_a_json_list_with_the_accounts_uid" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_data {
    target          = data.google_service_account.listed
    override_during = plan
    values = {
      unique_id = "104857600000000000001"
    }
  }

  variables {
    tenants = {
      eng      = { kind = "group", principal = "eng@saga.xyz", providers = [], service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"] }
      research = { kind = "group", principal = "research@saga.xyz", providers = [] }
    }
  }

  # A LIST, not an object keyed by email: a duplicate key in an object would
  # silently collapse, where a list carries it through to settings.py's own
  # explicit refusal.
  assert {
    condition = jsondecode(local.service_env["swarm-api"].TENANT_SERVICE_ACCOUNTS) == [
      {
        email     = "swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"
        kind      = "group"
        principal = "eng@saga.xyz"
        uid       = "104857600000000000001"
      }
    ]
    error_message = "TENANT_SERVICE_ACCOUNTS must be a JSON list of {email, kind, principal, uid}, carrying the account's unique id beside its email"
  }
}

run "no_listing_renders_an_empty_list" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [] }
    }
  }

  assert {
    condition     = local.service_env["swarm-api"].TENANT_SERVICE_ACCOUNTS == "[]"
    error_message = "a deployment that lists no service account renders [] and settings.py reads it as ()"
  }
}
