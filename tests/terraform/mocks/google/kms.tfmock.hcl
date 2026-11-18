# Shared mock-provider defaults for every suite that plans terraform/infra
# (`mock_provider "google" { source = "./mocks/google" }`).
#
# terraform/infra reads the step-spec key's versions at plan
# (terraform/infra/spec_signing.tf, contract request 34), and its
# `check "spec_signing_version_is_trusted"` fails when swarm-api's signing
# version (spec_signing_key_version, default 1) is not among the ENABLED
# versions. Left to the mock's generated values, the list is empty and every
# suite that plans the root fails that check. The default here is what a real
# freshly bootstrapped key has: version 1, ENABLED, with a P-256 public key.
#
# The version is named by NUMBER on purpose: modules/spec_signing_key builds
# the full version name from `version`, so this default serves every
# environment a suite plans. A suite that tests the filtering itself
# (spec_signing_infra.tftest.hcl) overrides it with override_data.
#
# google_kms_crypto_key_version (singular) is the SECOND data source
# modules/spec_signing_key reads, once per ENABLED version from the plural
# list above -- the plural data source's own `versions` entries never carry a
# public_key (main.tf explains why, with the provider source lines), so the
# module reads each version's key from here instead. Left to the mock's
# generated values, `public_key` comes back empty and
# `local.spec_verify_keys` is empty even though version 1 is "ENABLED" above,
# failing the same check this file exists to keep green. version/state here
# match the one default entry above (version 1, ENABLED) for every suite that
# does not override_data either data source itself.
mock_data "google_kms_crypto_key_version" {
  defaults = {
    version = 1
    state   = "ENABLED"
    public_key = [
      {
        algorithm = "EC_SIGN_P256_SHA256"
        pem       = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6ojZWToar3bAf33GAQzfub2F9rJL\nXmYMWuxVjGKJS8MlrJoz2dIlrz96sPwfmpvnKrhl81k4njMziRjXV9JLTg==\n-----END PUBLIC KEY-----\n"
      },
    ]
  }
}

mock_data "google_kms_crypto_key_versions" {
  defaults = {
    versions = [
      {
        id               = "mock-step-spec-version-1"
        name             = "mock-step-spec-version-1"
        crypto_key       = "mock-step-spec"
        version          = 1
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key = [
          {
            algorithm = "EC_SIGN_P256_SHA256"
            pem       = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6ojZWToar3bAf33GAQzfub2F9rJL\nXmYMWuxVjGKJS8MlrJoz2dIlrz96sPwfmpvnKrhl81k4njMziRjXV9JLTg==\n-----END PUBLIC KEY-----\n"
          },
        ]
      },
    ]
  }
}
