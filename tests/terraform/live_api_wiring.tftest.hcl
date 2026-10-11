# swarm-api's live-browser environment (docs/design/live-browser.md §3.3; lane
# LB-L8d, part of #1030; owner decisions 2026-10-11):
#
#   * SWARM_LIVE_URL is the relay's reserved internal address (live.tf's
#     `live_relay` output) as plain ws on port 8080 -- decision (b);
#   * swarm-live-key reaches swarm-api as SWARM_LIVE_KEY by Secret Manager
#     REFERENCE through the cloud_run module's `secret_env`, beside the child
#     key, and its value is in no environment variable, plan or state.
#
# The setup is live.tftest.hcl's: the step-spec key versions are mocked so the
# root plans at all, the project number and the relay's address are fixed.

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
    ]
  }
}

override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

# The relay's internal load balancer address is computed by GCE; a fixed one
# lets the rendered manifest be read at plan.
override_resource {
  target          = google_compute_address.live_relay
  override_during = plan
  values = {
    address = "10.10.0.42"
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
  }

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"            = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}

run "off_swarm_api_has_no_relay_and_no_live_key" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == ""
    error_message = "with enable_live_browser off swarm-api has no relay address, and every live socket closes 'no relay'"
  }

  assert {
    condition     = !contains(keys(local.api_secret_env), "SWARM_LIVE_KEY")
    error_message = "with enable_live_browser off swarm-api references no live key: the secret may have no version yet, and a revision referencing one would not start"
  }
}

run "on_swarm_api_dials_the_relay_and_reads_the_key_by_reference" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_live_browser = true
  }

  # Decision (b): plain ws, port 8080, the internal load balancer's reserved
  # address -- the one live.tf's output names.
  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == "ws://10.10.0.42:8080"
    error_message = "SWARM_LIVE_URL is ws://<live_relay internal address>:8080 (docs/design/live-browser.md §3.3, decision (b))"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == "ws://${output.live_relay.internal_address}:8080"
    error_message = "SWARM_LIVE_URL must be the address the live_relay output names, so the relay's Service and swarm-api agree"
  }

  # The key by reference, next to the child key's, never by value.
  assert {
    condition = (
      local.api_secret_env["SWARM_LIVE_KEY"].secret == "swarm-live-key"
      && local.api_secret_env["SWARM_LIVE_KEY"].version == "latest"
    )
    error_message = "swarm-api reads SWARM_LIVE_KEY from Secret Manager secret swarm-live-key, version latest, through secret_env"
  }

  assert {
    condition     = length([for k in keys(local.service_env["swarm-api"]) : k if startswith(k, "SWARM_LIVE_KEY")]) == 0
    error_message = "the live key is never a plain environment variable: only a secret_env reference"
  }

  # The child key still rides the same merge.
  assert {
    condition     = alltrue([for k in keys(local.child_key_secret_env) : contains(keys(local.api_secret_env), k)])
    error_message = "swarm-api's secret_env keeps every child key reference beside the live key"
  }
}
