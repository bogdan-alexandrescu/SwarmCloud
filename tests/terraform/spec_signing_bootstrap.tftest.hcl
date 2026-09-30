# Contract request 34 (#342): the key that signs every step spec.
#
# The key ring, the key and the one `signer` grant live in terraform/bootstrap,
# applied by the owner, never by CI (the entry's decision 4): that keeps the CI
# deployer off setIamPolicy on the key, so the release pipeline cannot grant
# itself a signature. These runs pin what that decision rests on:
#
#   * the key is an asymmetric P-256 signing key in software, one per
#     environment, in that environment's own ring (the canonical form carries
#     no environment, so a shared key would let dev's specs verify in prod);
#   * `swarm-api` is the only identity that can sign, and it holds `signer`
#     ON THE KEY, not on the project, and never `signerVerifier`;
#   * the deployer holds only what reading the public keys at plan needs;
#   * the key carries managed-by=swarm-terraform, and cloudkms is enabled.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"

  # Required by this root; see bootstrap.tftest.hcl.
  frontend_iap_members = ["domain:example.com"]

  enable_github_wif = true
  github_repository = "saga/agent-swarm-infra"
}

run "the_step_spec_key_is_a_p256_signing_key_per_environment" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # The control: the assertions below are over a non-empty set, and it is the
  # set of environments the release deploys.
  assert {
    condition     = length(google_kms_crypto_key.step_spec) > 0 && contains(keys(google_kms_crypto_key.step_spec), "dev")
    error_message = "bootstrap must create a step-spec key for dev, the environment the release deploys"
  }

  assert {
    condition = alltrue([
      for k in google_kms_crypto_key.step_spec : k.purpose == "ASYMMETRIC_SIGN"
    ])
    error_message = "the step-spec key must be an ASYMMETRIC_SIGN key: workers verify with the public half and never call KMS"
  }

  assert {
    condition = alltrue([
      for k in google_kms_crypto_key.step_spec :
      k.version_template[0].algorithm == "EC_SIGN_P256_SHA256" && k.version_template[0].protection_level == "SOFTWARE"
    ])
    error_message = "the worker verifies ECDSA P-256 over SHA-256 (agent_worker.specverify); any other algorithm is a key no worker can use"
  }

  assert {
    condition = alltrue([
      for env, k in google_kms_crypto_key.step_spec :
      k.name == "step-spec" && k.key_ring == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-${env}-specs"
    ])
    error_message = "each environment's key is `step-spec` in that environment's own ring, swarm-<env>-specs, in us-central1"
  }

  assert {
    condition = alltrue([
      for env, r in google_kms_key_ring.step_spec :
      r.name == "swarm-${env}-specs" && r.location == "us-central1" && r.project == "saga-agents-staging"
    ])
    error_message = "one key ring per environment, named swarm-<env>-specs, in the platform's region"
  }

  # `labels` is the configured map; `default_labels` from the provider block
  # is merged on top at apply and may add, never remove.
  assert {
    condition = alltrue([
      for k in google_kms_crypto_key.step_spec : k.labels["managed-by"] == "swarm-terraform"
    ])
    error_message = "the crypto key must carry managed-by=swarm-terraform; the key ring cannot carry a label at all and is in unlabelable-types.json instead"
  }

  assert {
    condition     = contains(output.enabled_services, "cloudkms.googleapis.com")
    error_message = "cloudkms.googleapis.com must be enabled by bootstrap, before the key ring can be created"
  }
}

run "only_swarm_api_can_sign_and_only_on_the_key" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = length(google_kms_crypto_key_iam_member.swarm_api_signer) == length(google_kms_crypto_key.step_spec) && length(google_kms_crypto_key_iam_member.swarm_api_signer) > 0
    error_message = "exactly one signer grant per step-spec key"
  }

  # ON THE KEY: the grant's resource is the crypto key's own id, not the ring
  # and not the project.
  assert {
    condition = alltrue([
      for env, m in google_kms_crypto_key_iam_member.swarm_api_signer :
      m.crypto_key_id == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-${env}-specs/cryptoKeys/step-spec"
    ])
    error_message = "the signer grant must be on the step-spec key itself"
  }

  # `signer`, not `signerVerifier`: swarm-api signs and never verifies.
  assert {
    condition = alltrue([
      for m in google_kms_crypto_key_iam_member.swarm_api_signer : m.role == "roles/cloudkms.signer"
    ])
    error_message = "swarm-api holds roles/cloudkms.signer (useToSign only) and nothing wider"
  }

  assert {
    condition = alltrue([
      for m in google_kms_crypto_key_iam_member.swarm_api_signer :
      m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "the only identity that may sign a step spec is swarm-api"
  }

  # Nobody else holds anything that can sign: the deployer's grants on the key
  # read public keys and version state, nothing more.
  # (The deployer's email is computed, so unknown at plan; the roles are what
  # decide whether it can sign.)
  assert {
    condition = length(google_kms_crypto_key_iam_member.deployer_key_readers) == 2 * length(google_kms_crypto_key.step_spec) && alltrue([
      for m in google_kms_crypto_key_iam_member.deployer_key_readers :
      contains(["roles/cloudkms.publicKeyViewer", "roles/cloudkms.viewer"], m.role)
    ])
    error_message = "the deployer reads the public keys (publicKeyViewer) and the version states (viewer) on each key, and holds no role that can sign"
  }

  # And no project-level grant hands out a KMS role: a project grant would
  # reach every key in the shared project, the other team's included.
  assert {
    condition = alltrue([
      for r in keys(google_project_iam_member.deployer_roles) : !startswith(r, "roles/cloudkms.")
    ])
    error_message = "a Cloud KMS role is granted on the project; step-spec grants are made on the key only"
  }
}

run "a_signer_grant_without_a_known_environment_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    spec_signing_environments = ["dev", "Prod!"]
  }

  expect_failures = [var.spec_signing_environments]
}
