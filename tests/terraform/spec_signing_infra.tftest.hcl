# Contract request 34 (#342): the public keys reach every worker, and swarm-api
# knows which version to sign with.
#
# terraform/infra reads the step-spec key's versions (the key itself is
# bootstrap's, applied by the owner) and renders:
#
#   * SPEC_VERIFY_KEYS -- {full version name: PEM} over ENABLED versions only --
#     with SPEC_SIGNING_KEY, SPEC_SIGNATURE_MODE and SPEC_LEGACY_CUTOVER onto
#     every Cloud Run worker Job it creates and onto the scheduler, which hands
#     the same four to the Jobs it creates itself (#353,
#     scheduler.dispatch.spec_job_env);
#   * the full name of the version swarm-api signs with, as an output; #353
#     puts it in swarm-api's environment as SPEC_SIGNING_KEY_VERSION beside
#     the code that reads it;
#   * the same map as an output, which kubernetes/render.py turns into each
#     tenant namespace's swarm-spec-verify-keys ConfigMap for GKE pods.
#
# The versions come from override_data, so "enabled only" is a statement about
# a list holding a DISABLED version, not about an empty one.

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
        public_key       = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
      },
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/2"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/2"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 2
        state            = "DISABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-TWO-REVOKED" }]
      },
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/3"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/3"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 3
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-THREE" }]
      },
    ]
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
  }
}

run "workers_trust_every_enabled_version_and_no_other" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = output.spec_verify_keys == {
      "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1" = "PEM-ONE"
      "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/3" = "PEM-THREE"
    }
    error_message = "SPEC_VERIFY_KEYS must hold every ENABLED version by its full name, and a DISABLED version (a revoked one) must not be trusted"
  }

  # The control for every Job assertion below: six Jobs, not an empty map.
  assert {
    condition     = length(output.job_spec_env) == 6
    error_message = "job_spec_env must cover every worker Job: 4 Cloud Run profiles for eng plus 2 credential-free ones for smoke"
  }

  assert {
    condition = alltrue([
      for job, env in output.job_spec_env :
      env.SPEC_VERIFY_KEYS == jsonencode(output.spec_verify_keys)
    ])
    error_message = "every worker Job must carry SPEC_VERIFY_KEYS, the JSON map the worker parses (agent_worker.specverify.parse_verify_keys)"
  }

  assert {
    condition = alltrue([
      for job, env in output.job_spec_env :
      env.SPEC_SIGNING_KEY == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
    ])
    error_message = "every worker Job must name the key it trusts versions of; a version outside it is foreign_key_version"
  }

  assert {
    condition = alltrue([
      for job, env in output.job_spec_env :
      env.SPEC_SIGNATURE_MODE == "legacy" && env.SPEC_LEGACY_CUTOVER == "2026-10-01T12:00:00Z"
    ])
    error_message = "the rollout mode and cutover must reach every worker Job"
  }

  # The scheduler passes the same four onto the Jobs it creates itself.
  assert {
    condition = (
      output.spec_service_env["swarm-scheduler"].SPEC_VERIFY_KEYS == jsonencode(output.spec_verify_keys)
      && output.spec_service_env["swarm-scheduler"].SPEC_SIGNING_KEY == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
      && output.spec_service_env["swarm-scheduler"].SPEC_SIGNATURE_MODE == "legacy"
      && output.spec_service_env["swarm-scheduler"].SPEC_LEGACY_CUTOVER == "2026-10-01T12:00:00Z"
    )
    error_message = "the scheduler must carry the same four settings terraform puts on its own Jobs, for the Jobs it creates"
  }

  # swarm-api signs with one named version -- an asymmetric key has no primary.
  # The value is derived here, and swarm-api carries it (#353, beside the code
  # that reads it): a hardened swarm-api without it refuses to start.
  assert {
    condition     = output.spec_signing_key_version == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
    error_message = "the signing version must be a full version name of this environment's step-spec key"
  }

  assert {
    condition     = lookup(output.spec_service_env["swarm-api"], "SPEC_SIGNING_KEY_VERSION", "") == output.spec_signing_key_version
    error_message = "swarm-api must carry SPEC_SIGNING_KEY_VERSION as the full signing version name; a hardened swarm-api without it refuses to start"
  }

  # swarm-api signs; it has no business holding the verification map.
  assert {
    condition     = !contains(keys(output.spec_service_env["swarm-api"]), "SPEC_VERIFY_KEYS")
    error_message = "swarm-api signs and never verifies; it does not carry SPEC_VERIFY_KEYS"
  }

  # The GKE copy, for kubernetes/render.py: the same map, the same key.
  assert {
    condition = (
      output.spec_verify_keys_configmap.SPEC_VERIFY_KEYS == jsonencode(output.spec_verify_keys)
      && output.spec_verify_keys_configmap.SPEC_SIGNING_KEY == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
    )
    error_message = "the ConfigMap payload must carry the same map as SPEC_VERIFY_KEYS on the Cloud Run Jobs"
  }
}

run "the_default_mode_is_enforce_and_carries_no_cutover" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    spec_signature_mode = "enforce"
    spec_legacy_cutover = ""
  }

  assert {
    condition = alltrue([
      for job, env in output.job_spec_env :
      env.SPEC_SIGNATURE_MODE == "enforce" && env.SPEC_LEGACY_CUTOVER == null
    ])
    error_message = "in enforce mode the Jobs carry no cutover at all"
  }
}

run "legacy_without_a_cutover_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    spec_signature_mode = "legacy"
    spec_legacy_cutover = ""
  }

  # A worker in legacy with no cutover admits nothing unsigned
  # (specverify._legacy_admits), so every parked task would fail the moment
  # the verifying worker shipped. Refused at plan instead.
  expect_failures = [var.spec_legacy_cutover]
}

run "a_cutover_without_a_zone_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    spec_legacy_cutover = "2026-10-01T12:00:00"
  }

  expect_failures = [var.spec_legacy_cutover]
}

run "an_unknown_mode_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    spec_signature_mode = "permissive"
  }

  expect_failures = [var.spec_signature_mode]
}
