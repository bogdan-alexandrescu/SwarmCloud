# Which service accounts terraform/infra ADOPTS when one of the same name
# already exists (#334 security review, 2026-09-29).
#
# create_ignore_already_exists makes a create that hits a 409 take the existing
# account into state instead of failing. On an account somebody else made first,
# that is squatting: the release would then set IAM on, deploy as and grant
# roles to an identity whose history it does not know -- keys included. The
# platform accounts, swarm-tick and swarm-verify are already in state, so they
# never need it and do not set it (terraform/bootstrap's since DAYONE-BOOT-2,
# adopted from infra's state only by an `import` gated on
# adopt_from_infra_states). The tenant worker keeps it, because
# scripts/register-tenant.sh creates that account before the release that adds
# the tenant, and refuses when the account it finds already carries IAM members
# or user-managed keys it did not expect
# (tests/integration/test_register_tenant_squat.py).

# source: the shared defaults every suite that plans terraform/infra needs
# (mocks/google/kms.tfmock.hcl -- the step-spec key's enabled version 1).
mock_provider "google" {
  source = "./mocks/google"
}

# Since DAYONE-BOOT-2 the platform accounts, swarm-tick, swarm-verify,
# swarm-schedule-tick and swarm-rollup-sweeper are terraform/bootstrap's
# (platform_service_accounts.tf); terraform/infra and its modules only READ
# them (data "google_service_account") and forget their old addresses with
# `removed { destroy = false }` (terraform/infra/removed.tf). The promise
# stays: no account is ever taken over by name on a 409. Bootstrap adopts the
# accounts infra created only through its `import` blocks, and only while
# adopt_from_infra_states names infra's state; with it empty nothing is
# imported -- which this run proves on a mock provider, which refuses any
# import ("Invalid import request") and so could not plan otherwise.
run "platform_and_tick_accounts_are_adopted_only_under_the_infra_state_gate" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # terraform/bootstrap/state_bucket.tf imports the state bucket
  # unconditionally (DAYONE-BOOT-1); that import is not this suite's subject.
  override_resource {
    target = google_storage_bucket.state
  }

  variables {
    project_id              = "saga-agents-staging"
    frontend_iap_members    = ["domain:example.com"]
    adopt_from_infra_states = []
  }

  assert {
    condition     = alltrue([for k, sa in google_service_account.platform : sa.create_ignore_already_exists != true])
    error_message = "a platform service account adopts a pre-existing account of its name on a 409; only the import gated on adopt_from_infra_states may adopt it"
  }

  assert {
    condition = alltrue([
      for sa in [
        google_service_account.tick,
        google_service_account.verify,
        google_service_account.schedule_tick,
        google_service_account.rollup_sweeper,
        google_service_account.discovery,
        google_service_account.doctor,
      ] : sa.create_ignore_already_exists != true
    ])
    error_message = "swarm-tick, swarm-verify, swarm-schedule-tick, swarm-rollup-sweeper, swarm-discovery or swarm-doctor adopts a pre-existing account of its name on a 409"
  }

  # The control: the accounts are planned at all, under their names.
  assert {
    condition = (
      toset(keys(google_service_account.platform)) == toset(["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"])
      && google_service_account.tick.account_id == "swarm-tick"
      && google_service_account.verify.account_id == "swarm-verify"
    )
    error_message = "the four platform accounts, swarm-tick and swarm-verify are not planned, so the assertions above prove nothing"
  }
}

# modules/iam creates no account any more: it reads bootstrap's, so it cannot
# adopt one either.
run "modules_iam_reads_the_platform_and_tick_accounts_and_creates_none" {
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
    condition = (
      toset(keys(data.google_service_account.platform)) == toset(["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"])
      && data.google_service_account.tick.account_id == "swarm-tick"
      && length(regexall("resource\\s+\"google_service_account\"", file("../../terraform/modules/iam/service_accounts.tf"))) == 0
    )
    error_message = "modules/iam must read the four platform accounts and swarm-tick from terraform/bootstrap, and declare no google_service_account resource of its own"
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
      "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  # terraform/infra no longer creates swarm-verify (terraform/bootstrap does,
  # under the gate the first run above holds): it reads it, so it can adopt
  # nothing of that name.
  assert {
    condition = (
      data.google_service_account.verify.account_id == "swarm-verify"
      && length(regexall("resource\\s+\"google_service_account\"\\s+\"verify\"", file("../../terraform/infra/verify.tf"))) == 0
    )
    error_message = "terraform/infra must read swarm-verify from terraform/bootstrap by its name, and declare no google_service_account of its own for it"
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
        principal = "eng@example.com"
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
