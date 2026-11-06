# The workspace-secret grant attestation key (docs/design/workspace-secrets.md,
# "What SEC-GRANT-ATTEST built"; part of #1347): terraform/infra/
# workspace_grant_key.tf.
#
# What is held here:
#
#   * the container swarm-workspace-grant-key exists with NO VERSION, labelled
#     managed-by=swarm-terraform, read by swarm-api alone;
#   * off by default swarm-api references nothing (a reference to a
#     versionless secret would stop it starting) and GRANT_ATTESTATION_REQUIRED
#     is "false";
#   * `serve` references the key at latest, `previous_version` adds the
#     rotation's previous version, `required` turns the switch on.
#
# The infra root's setup is typesafe_fwd.tftest.hcl's.

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

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  # Read by the dev run's assertions only (live.tftest.hcl does the same).
  tf_root = "../../terraform/infra"

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
  }

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-egress"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "swarm-typesafe-fwd"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-typesafe-fwd@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_the_container_exists_and_nothing_references_it" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      google_secret_manager_secret.workspace_grant_key.secret_id == "swarm-workspace-grant-key"
      && google_secret_manager_secret.workspace_grant_key.labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the grant key is swarm-workspace-grant-key, labelled managed-by=swarm-terraform"
  }

  assert {
    # swarm-api's member comes from the iam module and is unknown at plan, as
    # in typesafe_fwd.tftest.hcl; the role, the secret and the member count
    # are what plan can show.
    condition = (
      google_secret_manager_secret_iam_binding.workspace_grant_key_accessor.role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.workspace_grant_key_accessor.secret_id == "swarm-workspace-grant-key"
      && length(google_secret_manager_secret_iam_binding.workspace_grant_key_accessor.members) == 1
    )
    error_message = "swarm-workspace-grant-key has one authoritative secretAccessor binding with one member, swarm-api"
  }

  assert {
    condition = (
      !contains(keys(local.api_secret_env), "SWARM_WORKSPACE_GRANT_KEY")
      && !contains(keys(local.api_secret_env), "SWARM_WORKSPACE_GRANT_KEY_PREVIOUS")
    )
    error_message = "off by default swarm-api references no swarm-workspace-grant-key version: the secret has none yet"
  }

  assert {
    condition     = local.service_env["swarm-api"]["GRANT_ATTESTATION_REQUIRED"] == "false"
    error_message = "grant_attestation_required ships off: grants written before SEC-GRANT-ATTEST carry no attestation until the attest route signs them"
  }

  # No version, ever, from terraform: scripts/create-secrets.sh owns the value.
  assert {
    condition     = length(regexall("google_secret_manager_secret_version|secret_data", file("${var.tf_root}/workspace_grant_key.tf"))) == 0
    error_message = "terraform/infra/workspace_grant_key.tf declares no secret version and no secret data: the key's value is generated by scripts/create-secrets.sh --workspace-grant-key"
  }
}

run "served_during_a_rotation_and_required" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    workspace_grant_attestation = {
      serve            = true
      previous_version = "3"
      required         = true
    }
  }

  assert {
    condition = (
      local.api_secret_env["SWARM_WORKSPACE_GRANT_KEY"].secret == "swarm-workspace-grant-key"
      && local.api_secret_env["SWARM_WORKSPACE_GRANT_KEY"].version == "latest"
      && local.api_secret_env["SWARM_WORKSPACE_GRANT_KEY_PREVIOUS"].secret == "swarm-workspace-grant-key"
      && local.api_secret_env["SWARM_WORKSPACE_GRANT_KEY_PREVIOUS"].version == "3"
    )
    error_message = "served, swarm-api reads the key at latest and, during a rotation, the replaced version by number"
  }

  assert {
    condition     = local.service_env["swarm-api"]["GRANT_ATTESTATION_REQUIRED"] == "true"
    error_message = "required renders GRANT_ATTESTATION_REQUIRED=true on swarm-api"
  }
}
