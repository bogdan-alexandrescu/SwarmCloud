# Child tasks, the deployment wiring (terraform/infra/child_tasks.tf,
# docs/design/child-tasks.md §9 item 2).
#
# OFF by default, and off means nothing references swarm-child-key: a Cloud
# Run revision that references a secret with no version fails to start, so a
# deployment that has not run `create-secrets.sh --child-key` must not
# reference it. The secret and its accessor binding exist either way, and the
# binding is the scheduler and swarm-api alone -- no tenant identity can read
# the key that mints and verifies registration nonces.
#
# The setup below is spec_signing_infra.tftest.hcl's: the infra root needs the
# step-spec key versions mocked to plan at all.

mock_provider "google" {}

override_data {
  target = data.google_kms_crypto_key_versions.step_spec
  values = {
    versions = [
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 1
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/2"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/2"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 2
        state            = "DISABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/3"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/3"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 3
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
    ]
  }
}

# The per-version reads the fix adds (terraform/modules/spec_signing_key):
# only the two ENABLED versions get one, keyed by version number.
override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["3"]
  values = {
    version    = 3
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-THREE" }]
  }
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@saga.xyz"
      providers = ["anthropic", "openai"]
    }
    smoke = {
      kind      = "group"
      principal = "swarm-smoke@saga.xyz"
      providers = []
    }
  }

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

run "off_by_default_nothing_references_the_key" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = google_secret_manager_secret.child_key.secret_id == "swarm-child-key"
    error_message = "the secret is swarm-child-key, the name scripts/create-secrets.sh --child-key adds versions to"
  }

  assert {
    condition     = google_secret_manager_secret.child_key.labels["managed-by"] == "swarm-terraform"
    error_message = "every resource carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  assert {
    # The members themselves are the two service accounts' emails, which the
    # mock provider leaves unknown at plan; child_tasks.tf lists exactly
    # module.iam's swarm-scheduler and swarm-api members in an AUTHORITATIVE
    # binding. What is known at plan is that the binding is accessor on this
    # secret and nothing wider.
    condition = (
      google_secret_manager_secret_iam_binding.child_key_accessor.role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.child_key_accessor.secret_id == "swarm-child-key"
    )
    error_message = "swarm-child-key's readers are one authoritative secretAccessor binding on that secret"
  }

  assert {
    condition = (
      length(output.child_tasks_service_wiring.secret_env["swarm-api"]) == 0
      && length(output.child_tasks_service_wiring.secret_env["swarm-scheduler"]) == 0
    )
    error_message = "with enable_child_tasks off no service references swarm-child-key: a reference to a secret with no version stops the revision starting"
  }

  assert {
    condition     = length(output.child_tasks_service_wiring.worker_invokers) == 0
    error_message = "with child tasks off no worker may invoke swarm-api"
  }

  # The address and audience are passed through either way; without a key the
  # scheduler mints no nonce and passes neither (scheduler.dispatch.worker_env).
  assert {
    condition = (
      output.child_tasks_service_wiring.api_audience == "https://swarm-api.dev.swarm.internal"
      && output.child_tasks_service_wiring.scheduler_audience == output.child_tasks_service_wiring.api_audience
    )
    error_message = "the audience the scheduler hands workers must be the one swarm-api's child routes pin"
  }
}

run "on_both_services_read_the_key_and_workers_may_call" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # swarm_api_url is left empty: under the mock provider swarm-api's URL is
  # unknown at plan, so the swarm_api_url_is_wired check could not be
  # evaluated for a declared one. Empty is itself the case worth holding:
  # switched on with no address, the plan says so.
  variables {
    enable_child_tasks         = true
    child_key_previous_version = "3"
  }

  expect_failures = [check.child_tasks_have_an_address]

  assert {
    condition     = output.child_tasks_service_wiring.secret_env["swarm-api"] == tolist(["SWARM_CHILD_KEY", "SWARM_CHILD_KEY_PREVIOUS"])
    error_message = "swarm-api verifies under the current and, during a rotation, the previous key version (design §5 F13)"
  }

  assert {
    condition     = output.child_tasks_service_wiring.secret_env["swarm-scheduler"] == tolist(["SWARM_CHILD_KEY"])
    error_message = "the scheduler mints with the current version only"
  }

  assert {
    condition     = output.child_tasks_service_wiring.worker_invokers == tolist(["worker-eng", "worker-smoke"])
    error_message = "every tenant's worker must be able to invoke swarm-api's child routes, keyed by tenant"
  }

}
