# The TypeSafe forwarder, the platform side (docs/design/workspace-secrets.md
# §3.5; lane SEC-INJECT-A, part of #1347): terraform/infra/typesafe_fwd.tf and
# kubernetes/swarm-system/swarm-typesafe-fwd.yaml as it renders it.
#
# What is held here:
#
#   * OFF BY DEFAULT nothing exists: no key secret, no reader, no reference on
#     swarm-api, no manifest;
#   * enable_typesafe_forwarder creates swarm-typesafe-fwd-key with NO VERSION,
#     read by swarm-api and the forwarder's KSA principal and nobody else, and
#     still references nothing (a reference to a versionless secret would stop
#     swarm-api starting);
#   * typesafe_forwarder.serve puts the key on swarm-api by reference and
#     renders the manifest with the image, the key's version reference, the
#     project and the setting, whose kill switch ships OFF;
#   * served without a digest renders nothing and fails its check; served or
#     switched on without the layer below it is refused by its check;
#   * every labelable resource carries managed-by=swarm-terraform;
#   * the scheduler's switch (TYPESAFE_FORWARDER_ENABLED/_TENANTS, lane
#     TS-MIGRATE-POD) is the forwarder's own kill switch and tenants, and is
#     "false" whenever the forwarder is not served, so the dispatcher never
#     labels a pod `swarm-typesafe: inject` for a forwarder that is not there.
#
# The infra root's setup is egress.tftest.hcl's, with a swarm-typesafe-fwd
# digest added to image_refs.

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

run "off_by_default_nothing_exists" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_secret_manager_secret.typesafe_fwd_key) == 0
      && length(google_secret_manager_secret_iam_binding.typesafe_fwd_key_accessor) == 0
    )
    error_message = "with enable_typesafe_forwarder off (the default) no key secret and no reader exist"
  }

  assert {
    condition     = !contains(keys(local.api_secret_env), "SWARM_TYPESAFE_FWD_KEY")
    error_message = "with the forwarder off swarm-api carries no swarm-typesafe-fwd-key reference, so it mints no TypeSafe ticket (503)"
  }

  assert {
    condition = (
      output.swarm_typesafe_fwd_manifest == null
      && output.typesafe_forwarder.created == false
      && output.typesafe_forwarder.serving == false
      && output.typesafe_forwarder.key_secret == ""
    )
    error_message = "with the forwarder off there is no manifest to apply"
  }

  assert {
    condition     = local.typesafe_fwd_config.enabled == false && length(local.typesafe_fwd_config.tenants) == 0
    error_message = "the forwarder's kill switch ships OFF with no tenant named"
  }

  assert {
    condition = (
      local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_ENABLED"] == "false"
      && local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_TENANTS"] == ""
    )
    error_message = "the scheduler's switch ships OFF: no pod is labelled swarm-typesafe: inject"
  }
}

run "created_the_key_has_two_readers_and_nothing_references_it" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_typesafe_forwarder = true
  }

  assert {
    condition = (
      google_secret_manager_secret.typesafe_fwd_key[0].secret_id == "swarm-typesafe-fwd-key"
      && google_secret_manager_secret.typesafe_fwd_key[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the ticket key is swarm-typesafe-fwd-key, labelled managed-by=swarm-terraform"
  }

  assert {
    # swarm-api's member comes from the iam module and is unknown at plan, as
    # in egress.tftest.hcl; the role, the secret and the principal it adds are
    # what plan can show.
    condition = (
      google_secret_manager_secret_iam_binding.typesafe_fwd_key_accessor[0].role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.typesafe_fwd_key_accessor[0].secret_id == "swarm-typesafe-fwd-key"
      && output.typesafe_forwarder.principal == "principal://iam.googleapis.com/projects/209012342332/locations/global/workloadIdentityPools/saga-agents-staging.svc.id.goog/subject/ns/swarm-system/sa/swarm-typesafe-fwd"
    )
    error_message = "swarm-typesafe-fwd-key is read by swarm-api (mints) and the forwarder's KSA principal (verifies), nobody else"
  }

  assert {
    condition = (
      !contains(keys(local.api_secret_env), "SWARM_TYPESAFE_FWD_KEY")
      && output.swarm_typesafe_fwd_manifest == null
    )
    error_message = "created but not served: the key has no version yet, so swarm-api must not reference it and nothing is rendered"
  }

  # No version, ever, from terraform: scripts/create-secrets.sh owns the value.
  assert {
    condition     = length(regexall("google_secret_manager_secret_version|secret_data", file("${var.tf_root}/typesafe_fwd.tf"))) == 0
    error_message = "terraform/infra/typesafe_fwd.tf declares no secret version and no secret data: the key's value is generated by scripts/create-secrets.sh --typesafe-fwd-key"
  }
}

run "served_swarm_api_mints_and_the_manifest_renders" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_typesafe_forwarder = true
    typesafe_forwarder = {
      serve = true
    }
  }

  assert {
    condition     = local.api_secret_env["SWARM_TYPESAFE_FWD_KEY"].secret == "swarm-typesafe-fwd-key" && local.api_secret_env["SWARM_TYPESAFE_FWD_KEY"].version == "latest"
    error_message = "served, swarm-api reads swarm-typesafe-fwd-key by reference"
  }

  assert {
    condition = (
      output.swarm_typesafe_fwd_manifest != null
      && length(regexall("__[A-Z0-9_]+__", output.swarm_typesafe_fwd_manifest)) == 0
      && strcontains(output.swarm_typesafe_fwd_manifest, "swarm-typesafe-fwd@sha256:9999")
      && strcontains(output.swarm_typesafe_fwd_manifest, "projects/saga-agents-staging/secrets/swarm-typesafe-fwd-key/versions/latest")
      && strcontains(output.swarm_typesafe_fwd_manifest, "value: saga-agents-staging")
      && strcontains(output.swarm_typesafe_fwd_manifest, jsonencode(local.typesafe_fwd_config))
    )
    error_message = "the rendered manifest has no placeholder left, the image digest, the key by version reference, the project and the setting"
  }

  assert {
    condition = (
      local.typesafe_fwd_config.enabled == false
      && local.typesafe_fwd_config.requests_per_minute == 60
      && local.typesafe_fwd_config.max_request_bytes == 2097152
      && local.typesafe_fwd_config.max_response_seconds == 120
    )
    error_message = "served is not forwarding: the kill switch stays off until typesafe_forwarder.enabled, with the documented limits"
  }
}

run "the_switch_without_the_service_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    typesafe_forwarder = {
      enabled = true
      tenants = ["eng"]
    }
  }

  expect_failures = [check.typesafe_forwarder_switch_needs_the_service]

  assert {
    condition     = local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_ENABLED"] == "false"
    error_message = "switched on without the service, the scheduler still labels nothing: a labelled pod would dial a forwarder that is not there"
  }
}

run "switched_on_the_scheduler_labels_by_the_forwarders_own_setting" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_typesafe_forwarder = true
    typesafe_forwarder = {
      serve   = true
      enabled = true
      tenants = ["eng", "u-alice"]
    }
  }

  assert {
    condition = (
      local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_ENABLED"] == "true"
      && local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_TENANTS"] == "eng,u-alice"
    )
    error_message = "served and enabled, the scheduler labels the listed tenants' TypeSafe-backed pods"
  }

  assert {
    condition = (
      local.typesafe_fwd_config.enabled == true
      && join(",", local.typesafe_fwd_config.tenants) == local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_TENANTS"]
    )
    error_message = "the scheduler's tenants are the forwarder's own: a pod labelled for a tenant the forwarder refuses fails every request, and one left unlabelled never reaches it"
  }
}

run "served_but_not_enabled_labels_nothing" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_typesafe_forwarder = true
    typesafe_forwarder = {
      serve   = true
      tenants = ["eng"]
    }
  }

  assert {
    condition     = local.service_env["swarm-scheduler"]["TYPESAFE_FORWARDER_ENABLED"] == "false"
    error_message = "served with the kill switch off, the scheduler labels nothing even for a listed tenant"
  }
}

run "serving_without_the_key_container_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    typesafe_forwarder = {
      serve = true
    }
  }

  expect_failures = [check.typesafe_forwarder_serves_only_when_created]

  assert {
    condition     = !contains(keys(local.api_secret_env), "SWARM_TYPESAFE_FWD_KEY") && output.swarm_typesafe_fwd_manifest == null
    error_message = "serve without enable_typesafe_forwarder references no key and renders nothing"
  }
}

# Served with the file's map minus swarm-typesafe-fwd: a run's variables replace
# the whole map.
run "served_without_a_digest_renders_no_manifest_and_fails_its_check" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_typesafe_forwarder = true
    typesafe_forwarder = {
      serve = true
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
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  expect_failures = [check.typesafe_forwarder_has_an_image]

  assert {
    condition     = output.swarm_typesafe_fwd_manifest == null
    error_message = "with no swarm-typesafe-fwd digest there is no manifest to apply, never one naming an empty image"
  }
}
