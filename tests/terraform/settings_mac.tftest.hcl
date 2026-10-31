# Tenant settings integrity, the deployment wiring (terraform/infra/settings_mac.tf,
# docs/design/tenant-settings-integrity.md; owner decisions on #1457,
# 2026-10-17).
#
# What holds: swarm-settings-key exists as a secret CONTAINER only -- no
# version resource anywhere in the root, because a managed version puts the
# plaintext in state; its readers are swarm-api's and the scheduler's service
# accounts and nobody else; both services read it as SWARM_SETTINGS_KEY; and
# the one cutover list reaches both services, empty by default.
#
# The setup below is child_tasks_infra.tftest.hcl's: the infra root needs the
# step-spec key versions mocked to plan at all. The platform service accounts
# get fixed emails so the accessor members are known at plan and can be
# pinned exactly.

mock_provider "google" {}

override_resource {
  target          = module.iam.google_service_account.platform["swarm-api"]
  override_during = plan
  values = {
    email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
  }
}

override_resource {
  target          = module.iam.google_service_account.platform["swarm-scheduler"]
  override_during = plan
  values = {
    email = "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    name  = "projects/saga-agents-staging/serviceAccounts/swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
  }
}

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
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
    smoke = {
      kind      = "group"
      principal = "swarm-smoke@example.com"
      providers = []
    }
  }

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

run "default_plan_adds_the_container_two_readers_and_the_env" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = google_secret_manager_secret.settings_mac_key.secret_id == "swarm-settings-key"
    error_message = "the secret is swarm-settings-key, the name scripts/create-secrets.sh --settings-mac-key adds versions to"
  }

  assert {
    condition     = google_secret_manager_secret.settings_mac_key.labels["managed-by"] == "swarm-terraform"
    error_message = "every resource carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  # No version resource: the value is never in Terraform state. Read from the
  # root's source, because a resource that does not exist cannot be asserted
  # absent any other way.
  assert {
    condition = google_secret_manager_secret.settings_mac_key.secret_id == "swarm-settings-key" && alltrue([
      for f in fileset("../../terraform/infra", "*.tf") :
      !strcontains(file("../../terraform/infra/${f}"), "google_secret_manager_secret_version")
      || !strcontains(file("../../terraform/infra/${f}"), "settings_mac_key")
    ])
    error_message = "swarm-settings-key has no google_secret_manager_secret_version in Terraform: a managed version puts the plaintext in a state file several people can read"
  }

  assert {
    condition     = toset(keys(google_secret_manager_secret_iam_member.settings_mac_key_accessor)) == toset(["swarm-api", "swarm-scheduler"])
    error_message = "swarm-settings-key has exactly two accessor members, swarm-api and the scheduler"
  }

  assert {
    condition = (
      google_secret_manager_secret_iam_member.settings_mac_key_accessor["swarm-api"].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && google_secret_manager_secret_iam_member.settings_mac_key_accessor["swarm-scheduler"].member == "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "the two members are swarm-api's and the scheduler's service accounts: no tenant account, no wildcard"
  }

  assert {
    condition = alltrue([
      for m in google_secret_manager_secret_iam_member.settings_mac_key_accessor :
      m.role == "roles/secretmanager.secretAccessor" && m.secret_id == "swarm-settings-key"
    ])
    error_message = "each member is secretAccessor on swarm-settings-key and nothing wider"
  }

  # The services' secret_env is these two locals, by name (main.tf); the
  # cloud_run module renders each entry as a secret_key_ref env. The module's
  # resources are not addressable from here, so the hand-off is read from
  # main.tf and the values from the locals.
  assert {
    # The variable reference is what makes this a check of the configuration:
    # terraform test refuses a condition that reads only files.
    condition = !var.settings_mac_key_ready && (
      can(regex("secret_env\\s*=\\s*local\\.api_secret_env\\n", file("../../terraform/infra/main.tf")))
      && can(regex("secret_env\\s*=\\s*local\\.scheduler_secret_env\\n", file("../../terraform/infra/main.tf")))
    )
    error_message = "swarm-api's secret_env is local.api_secret_env and the scheduler's is local.scheduler_secret_env (main.tf)"
  }

  assert {
    condition = (
      !contains(keys(local.api_secret_env), "SWARM_SETTINGS_KEY")
      && !contains(keys(local.scheduler_secret_env), "SWARM_SETTINGS_KEY")
      && !contains(keys(local.api_secret_env), "SWARM_SETTINGS_KEY_PREVIOUS")
      && !contains(keys(local.scheduler_secret_env), "SWARM_SETTINGS_KEY_PREVIOUS")
    )
    error_message = "while settings_mac_key_ready is false (the default) neither service references the key: the secret has no version yet"
  }

  assert {
    condition     = length(google_secret_manager_secret_iam_member.settings_mac_key_accessor) == 2
    error_message = "the container and both accessor members exist even while the key is not ready"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].SETTINGS_MAC_TENANTS == ""
      && local.service_env["swarm-scheduler"].SETTINGS_MAC_TENANTS == ""
      && local.service_env["swarm-scheduler"].SETTINGS_MAC_REQUIRED == "false"
    )
    error_message = "by default no tenant is cut over and the scheduler requires no MAC"
  }

  assert {
    condition     = !contains(keys(local.service_env["swarm-api"]), "SETTINGS_MAC_REQUIRED")
    error_message = "SETTINGS_MAC_REQUIRED is the scheduler's alone"
  }
}

run "a_ready_key_is_referenced_by_both_services_at_latest" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    settings_mac_key_ready = true
  }

  assert {
    condition = alltrue([
      for env in [local.api_secret_env, local.scheduler_secret_env] :
      env.SWARM_SETTINGS_KEY.secret == "swarm-settings-key" && env.SWARM_SETTINGS_KEY.version == "latest"
    ])
    error_message = "both swarm-api and the scheduler read SWARM_SETTINGS_KEY from swarm-settings-key at version latest once the key is ready"
  }

  assert {
    condition = (
      !contains(keys(local.api_secret_env), "SWARM_SETTINGS_KEY_PREVIOUS")
      && !contains(keys(local.scheduler_secret_env), "SWARM_SETTINGS_KEY_PREVIOUS")
    )
    error_message = "outside a rotation no service carries SWARM_SETTINGS_KEY_PREVIOUS"
  }
}

run "one_cutover_list_reaches_both_services_and_a_rotation_both_verifiers" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    settings_mac_key_ready            = true
    settings_mac_tenants              = ["eng", "smoke"]
    settings_mac_required             = true
    settings_mac_key_previous_version = "4"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].SETTINGS_MAC_TENANTS == "eng,smoke"
      && local.service_env["swarm-scheduler"].SETTINGS_MAC_TENANTS == "eng,smoke"
    )
    error_message = "settings_mac_tenants is rendered the same into both services as SETTINGS_MAC_TENANTS"
  }

  assert {
    condition     = local.service_env["swarm-scheduler"].SETTINGS_MAC_REQUIRED == "true"
    error_message = "settings_mac_required is rendered into the scheduler as SETTINGS_MAC_REQUIRED"
  }

  assert {
    condition = alltrue([
      for env in [local.api_secret_env, local.scheduler_secret_env] :
      env.SWARM_SETTINGS_KEY_PREVIOUS.secret == "swarm-settings-key" && env.SWARM_SETTINGS_KEY_PREVIOUS.version == "4"
    ])
    error_message = "during a rotation both verifiers read SWARM_SETTINGS_KEY_PREVIOUS at the replaced version"
  }

  assert {
    condition     = output.settings_mac_service_wiring.secret_env["swarm-scheduler"] == tolist(["SWARM_SETTINGS_KEY", "SWARM_SETTINGS_KEY_PREVIOUS"])
    error_message = "the scheduler's secret env carries the current and previous settings key"
  }
}

run "a_cutover_tenant_that_is_not_a_tenant_is_flagged" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    settings_mac_tenants = ["eng", "engg"]
  }

  expect_failures = [check.settings_mac_tenants_are_tenants]
}

run "a_previous_version_must_be_a_number" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    settings_mac_key_previous_version = "latest"
  }

  expect_failures = [var.settings_mac_key_previous_version]
}
