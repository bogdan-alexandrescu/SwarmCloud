# The join acceptance probe's person credential (terraform/infra/acceptance_join.tf,
# docs/design/join-requests.md section 8). These runs hold:
#
#   * OFF BY DEFAULT (and so in prod, whose settings never turn it on) there is
#     no secret and no accessor binding: swarm-verify reads no secret at all;
#   * on, swarm-accept-join-person exists with deletion protection, the
#     managed-by=swarm-terraform label and its populated-by annotation, and
#     terraform creates no version of it;
#   * on, the accessor binding is AUTHORITATIVE for secretAccessor on that one
#     secret, and swarm-verify is its only member.
mock_provider "google" {}

override_data {
  target = data.google_kms_crypto_key_versions.step_spec
  values = {
    versions = [
      {
        id               = "projects/example-project/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/example-project/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/example-project/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
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
    number = "123456789012"
  }
}

variables {
  project_id  = "example-project"
  environment = "dev"

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
    "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-egress"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_swarm_verify_reads_no_secret" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      !var.enable_acceptance_join_person
      && length(google_secret_manager_secret.accept_join_person) == 0
      && length(google_secret_manager_secret_iam_binding.accept_join_person_accessor) == 0
    )
    error_message = "enable_acceptance_join_person defaults to false, and while it is there is no swarm-accept-join-person and nothing may read it"
  }
}

run "on_swarm_verify_alone_reads_the_join_person_secret" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_acceptance_join_person = true
    deletion_protection           = true
  }

  assert {
    condition = alltrue([
      google_secret_manager_secret.accept_join_person[0].secret_id == "swarm-accept-join-person",
      google_secret_manager_secret.accept_join_person[0].project == "example-project",
      google_secret_manager_secret.accept_join_person[0].deletion_protection == true,
      google_secret_manager_secret.accept_join_person[0].labels["managed-by"] == "swarm-terraform",
      google_secret_manager_secret.accept_join_person[0].labels["component"] == "acceptance-join-person",
      startswith(google_secret_manager_secret.accept_join_person[0].annotations["swarm-populated-by"], "scripts/create-secrets.sh --join-person --stdin"),
    ])
    error_message = "swarm-accept-join-person is deletion-protected, labelled managed-by=swarm-terraform, and says create-secrets.sh --join-person --stdin populates it"
  }

  # Terraform creates no version: a version's payload would sit in state.
  assert {
    condition = length(regexall(
      "(?m)^\\s*resource\\s+\"google_secret_manager_secret_version\"",
      file("../../terraform/infra/acceptance_join.tf"),
    )) == 0 && length(google_secret_manager_secret.accept_join_person) == 1
    error_message = "acceptance_join.tf creates no secret version: create-secrets.sh --join-person --stdin stores the credential"
  }

  assert {
    condition = (
      length(google_secret_manager_secret_iam_binding.accept_join_person_accessor) == 1
      && google_secret_manager_secret_iam_binding.accept_join_person_accessor[0].secret_id == "swarm-accept-join-person"
      && google_secret_manager_secret_iam_binding.accept_join_person_accessor[0].role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.accept_join_person_accessor[0].members == toset(["serviceAccount:swarm-verify@example-project.iam.gserviceaccount.com"])
    )
    error_message = "secretAccessor on swarm-accept-join-person is held by swarm-verify alone, authoritatively"
  }
}
