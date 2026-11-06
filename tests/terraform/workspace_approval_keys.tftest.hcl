# The workspace job verifies approval signatures against public keys in its
# own environment, so its identity needs no Cloud KMS grant (lane
# WK-APPROVAL-KEYS of #847; owner decision 2026-10-19: no new IAM for approval
# verification). terraform/bootstrap/workspace_deployer.tf reads the step-spec
# key's ENABLED versions at the owner's plan and hands them to the job. These
# runs hold:
#
#   * the job's environment carries SPEC_VERIFY_KEYS, the {full version name:
#     PEM} shape workers get, over dev's step-spec key -- the key bootstrap's
#     own module.spec_signing_key["dev"] names -- and ENABLED versions only;
#   * the job's fixed command ends with that value's SHA-256, which is what
#     makes the value the job spec's: an execution override may set an
#     environment variable, never the command (images/workspace-apply/entry.py
#     refuses a SPEC_VERIFY_KEYS whose digest is not the command's);
#   * no google_kms_crypto_key_iam_member names swarm-workspace-deployer.
#
# The versions come from override_data, so "enabled only" is a statement about
# a list holding a DISABLED version, not about an empty one. The PEMs are
# placeholders: what is held is which values reach the job, not their bytes.

mock_provider "google" {
  # Rendered by this data source for the identities' and the job's policies;
  # left to the mock it is a random string the policy resources cannot parse.
  mock_data "google_iam_policy" {
    defaults = {
      policy_data = "{}"
    }
  }
}

# terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1):
# a mock provider cannot import, so the bucket's provider values are the
# override's and its configuration is still planned and asserted.
override_resource {
  target = google_storage_bucket.state
}

override_data {
  target = data.google_kms_crypto_key_versions.workspace_spec_versions[0]
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

override_data {
  target = module.workspace_spec_keys[0].data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target = module.workspace_spec_keys[0].data.google_kms_crypto_key_version.enabled["3"]
  values = {
    version    = 3
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-THREE" }]
  }
}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]

  enable_workspace_deployer = true
  enable_forge_user_slots   = true
  enable_github_wif         = true
  github_repository         = "saga/agent-swarm-infra"

  # Built from the parts so no line holds a 64-hex literal on its own.
  workspace_apply_image = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@sha256:${join("", [for i in range(8) : "0123abcd"])}"
}

run "the_job_carries_spec_verify_keys_from_the_module" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # The versions are read for dev's key, by the name bootstrap's own module
  # gives it -- the key swarm-api signs approvals with.
  assert {
    condition = alltrue([
      data.google_kms_crypto_key_versions.workspace_spec_versions[0].crypto_key == module.spec_signing_key["dev"].crypto_key_id,
      data.google_kms_crypto_key_versions.workspace_spec_versions[0].crypto_key == "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec",
      data.google_kms_crypto_key_versions.workspace_spec_versions[0].filter == "state=ENABLED",
    ])
    error_message = "the job's keys are read from dev's step-spec key, the one module.spec_signing_key[\"dev\"] names, ENABLED versions only"
  }

  # The control: the module rendered the two ENABLED versions and not the
  # DISABLED one, so the assertions below are over a non-empty map.
  assert {
    condition = module.workspace_spec_keys[0].verify_keys == {
      "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1" = "PEM-ONE"
      "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/3" = "PEM-THREE"
    }
    error_message = "the module renders {full version name: PEM} over the ENABLED versions only: a DISABLED version is a revoked one"
  }

  assert {
    condition = [
      for e in google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].env : e.value
      if e.name == "SPEC_VERIFY_KEYS"
    ] == [jsonencode(module.workspace_spec_keys[0].verify_keys)]
    error_message = "the workspace job's environment carries SPEC_VERIFY_KEYS, exactly once, as the module's verify_keys JSON: the shape workers get"
  }

  # Nothing else: every other variable an execution sees is one a caller may
  # override, and entry.py discards them all.
  assert {
    condition     = [for e in google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].env : e.name] == ["SPEC_VERIFY_KEYS"]
    error_message = "SPEC_VERIFY_KEYS is the job's only environment variable"
  }

  # The pin: the command carries the value's SHA-256, so an override that
  # replaces the variable is refused by entry.py; and it names the key whose
  # versions count, the same module's key (DAYONE-BOOT-1), so an environment
  # variable never chooses it.
  assert {
    condition = google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].command == tolist([
      "python3", "-I", "/opt/swarm/entry.py",
      "--spec-verify-keys-sha256=${sha256(jsonencode(module.workspace_spec_keys[0].verify_keys))}",
      "--spec-signing-key=${module.workspace_spec_keys[0].crypto_key_id}",
    ])
    error_message = "the job's fixed command ends with --spec-verify-keys-sha256=<SHA-256 of SPEC_VERIFY_KEYS> and --spec-signing-key=<this project's step-spec key>, the values no execution override reaches"
  }

  assert {
    condition     = module.workspace_spec_keys[0].crypto_key_id == "projects/${var.project_id}/locations/${var.region}/keyRings/${var.name_prefix}-dev-specs/cryptoKeys/step-spec"
    error_message = "the job trusts this project's own dev step-spec key, and no other"
  }

  assert {
    condition     = tolist(output.workspace_deployer.command) == google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].command
    error_message = "the output the owner reads a plan against shows the command the job runs"
  }
}

run "no_kms_grant_names_the_workspace_deployer" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # The deployer readers' member is the CI deployer's email, computed and so
  # unknown at plan; an unknown condition cannot pass. Its real value, then.
  override_resource {
    target          = google_service_account.deployer
    override_during = plan
    values = {
      email = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # The control: there are KMS grants to look through, both kinds.
  assert {
    condition     = length(google_kms_crypto_key_iam_member.swarm_api_signer) > 0 && length(google_kms_crypto_key_iam_member.deployer_key_readers) > 0
    error_message = "the control: bootstrap makes key grants, so the check below is over a non-empty set"
  }

  assert {
    condition = alltrue([
      for m in concat(values(google_kms_crypto_key_iam_member.swarm_api_signer), values(google_kms_crypto_key_iam_member.deployer_key_readers)) :
      m.member != "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "a Cloud KMS grant names swarm-workspace-deployer: the job verifies against SPEC_VERIFY_KEYS and must hold no role on the key (owner decision 2026-10-19)"
  }

  # The control for the check above: it can see a member, and the members it
  # sees are the two identities that should be there.
  assert {
    condition = toset([
      for m in concat(values(google_kms_crypto_key_iam_member.swarm_api_signer), values(google_kms_crypto_key_iam_member.deployer_key_readers)) : m.member
      ]) == toset([
      "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com",
    ])
    error_message = "the step-spec key's grants go to swarm-api (signer) and the CI deployer (readers), and to nobody else"
  }
}
