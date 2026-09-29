# Which service accounts terraform/infra ADOPTS when one of the same name
# already exists (#334 security review, 2026-09-29).
#
# create_ignore_already_exists makes a create that hits a 409 take the existing
# account into state instead of failing. On an account somebody else made first,
# that is squatting: the release would then set IAM on, deploy as and grant
# roles to an identity whose history it does not know -- keys included. The
# platform accounts, swarm-tick and swarm-verify are already in state, so they
# never need it and do not set it. The tenant worker keeps it, because
# scripts/register-tenant.sh creates that account before the release that adds
# the tenant, and refuses when the account it finds already carries IAM members
# or user-managed keys it did not expect
# (tests/integration/test_register_tenant_squat.py).

# source: the shared defaults every suite that plans terraform/infra needs
# (mocks/google/kms.tfmock.hcl -- the step-spec key's enabled version 1).
mock_provider "google" {
  source = "./mocks/google"
}

run "platform_and_tick_accounts_are_never_adopted" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    project_id       = "saga-agents-staging"
    artifact_bucket  = "swarm-artifacts-saga-agents-staging"
    labels           = { "managed-by" = "swarm-terraform" }
    gke_cluster_name = "swarm-autopilot"
    gke_location     = "us-central1"
  }

  assert {
    condition     = alltrue([for k, sa in google_service_account.platform : sa.create_ignore_already_exists != true])
    error_message = "a platform service account adopts a pre-existing account of its name; it is already in state and must fail on a 409 instead"
  }

  assert {
    condition     = google_service_account.tick.create_ignore_already_exists != true
    error_message = "swarm-tick adopts a pre-existing account of its name; it is already in state and must fail on a 409 instead"
  }

  # The control: the accounts are planned at all.
  assert {
    condition     = length(google_service_account.platform) == 4
    error_message = "the four platform accounts are not planned, so the assertions above prove nothing"
  }
}

run "the_verify_account_is_never_adopted" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    project_id  = "saga-agents-staging"
    environment = "dev"
    tenants     = {}

    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  assert {
    condition     = google_service_account.verify.create_ignore_already_exists != true
    error_message = "swarm-verify adopts a pre-existing account of its name; it is already in state and must fail on a 409 instead"
  }

  assert {
    condition     = google_service_account.verify.account_id == "swarm-verify"
    error_message = "the verify account is not planned under its name, so the assertion above proves nothing"
  }
}

run "the_tenant_worker_is_adopted_because_register_tenant_creates_it_first" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    project_id             = "saga-agents-staging"
    artifact_bucket        = "swarm-artifacts-saga-agents-staging"
    workload_identity_pool = "saga-agents-staging.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }

    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = []
      }
    }

    dispatcher_members = {
      scheduler = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_service_account.worker["eng"].create_ignore_already_exists == true
    error_message = "the tenant worker must adopt the account scripts/register-tenant.sh created before the release, or that release fails on a 409"
  }
}
