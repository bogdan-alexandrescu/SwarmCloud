# The decision-api tailnet egress, the platform side (lane JUDGE-NET; owner
# decisions 2026-10-14): terraform/infra/tailscale.tf and
# kubernetes/swarm-system/tailscale-egress.yaml as it renders it.
#
# What is held here:
#
#   * `swarm-tailscale-oauth` is a CONTAINER with NO VERSION: the OAuth client
#     secret is added by the owner on stdin, never by terraform, so it is in
#     no plan and no state;
#   * it carries managed-by=swarm-terraform, like every labelable resource;
#   * the switch ships OFF: nobody reads the secret and nothing is rendered;
#   * ON, it has ONE reader -- the deployer that runs the sync in
#     kubernetes/apply.sh --swarm-system -- and the manifest is rendered with
#     the tailnet name and no placeholder left;
#   * ON with no deployer named (an owner applying by hand), there is no
#     binding at all rather than an empty one;
#   * the tailnet name is one MagicDNS name, or the plan is refused.
#
# The infra root's setup is egress.tftest.hcl's.

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

  # No default since DAYONE-TF-1: the tailnet is the deployment's, so a test
  # that renders the egress names it.
  tailscale_egress_target = "decision-api-staging.taile0bda9.ts.net"

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
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_the_container_exists_and_nobody_reads_it" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = google_secret_manager_secret.tailscale_oauth.secret_id == "swarm-tailscale-oauth"
    error_message = "the OAuth client secret is swarm-tailscale-oauth (owner decision 2026-10-14)"
  }

  assert {
    condition     = google_secret_manager_secret.tailscale_oauth.labels["managed-by"] == "swarm-terraform"
    error_message = "every resource carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  assert {
    condition     = length(google_secret_manager_secret_iam_binding.tailscale_oauth_accessor) == 0
    error_message = "with enable_tailscale_egress off nobody may read the Tailscale OAuth secret"
  }

  assert {
    condition     = output.tailscale_egress_manifest == null && output.tailscale_egress.enabled == false
    error_message = "with enable_tailscale_egress off nothing about the tailnet is rendered for apply.sh"
  }

  assert {
    condition = (
      output.tailscale_egress.service == "decision-api"
      && output.tailscale_egress.port == 443
      && output.tailscale_egress.target == "decision-api-staging.taile0bda9.ts.net"
      && output.tailscale_egress.kubernetes_secret == "operator-oauth"
    )
    error_message = "one egress Service, decision-api on 443, to decision-api-staging.taile0bda9.ts.net, mounting operator-oauth"
  }
}

run "on_one_reader_the_deployer_and_a_rendered_manifest" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress  = true
    deployer_service_account = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
  }

  assert {
    condition = (
      length(google_secret_manager_secret_iam_binding.tailscale_oauth_accessor) == 1
      && google_secret_manager_secret_iam_binding.tailscale_oauth_accessor[0].role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.tailscale_oauth_accessor[0].secret_id == "swarm-tailscale-oauth"
    )
    error_message = "on, swarm-tailscale-oauth has exactly one accessor binding"
  }

  assert {
    condition = toset(google_secret_manager_secret_iam_binding.tailscale_oauth_accessor[0].members) == toset([
      "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com",
    ])
    error_message = "the deployer that runs the sync is the secret's ONE reader: no service, no tenant, no pod"
  }

  assert {
    condition = (
      output.tailscale_egress.enabled
      && strcontains(output.tailscale_egress_manifest, "- decision-api-staging.taile0bda9.ts.net")
      && strcontains(output.tailscale_egress_manifest, "swarm.saga.xyz/tailnet-fqdn: decision-api-staging.taile0bda9.ts.net")
      && length(regexall("__[A-Z0-9_]+__", output.tailscale_egress_manifest)) == 0
    )
    error_message = "on, the manifest is rendered with the tailnet name and no placeholder left"
  }

  assert {
    condition     = strcontains(output.tailscale_egress_manifest, "clusterIP: 10.48.6.120")
    error_message = "on, the decision-api Service carries the fixed clusterIP judging pods pin the tailnet name to (local.decision_api_cluster_ip, #1380)"
  }
}

run "on_without_a_deployer_has_no_binding" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress  = true
    deployer_service_account = ""
  }

  assert {
    condition     = length(google_secret_manager_secret_iam_binding.tailscale_oauth_accessor) == 0
    error_message = "an owner applying by hand reads as project owner; no binding, never an empty authoritative one"
  }

  assert {
    condition     = output.tailscale_egress_manifest != null
    error_message = "the manifest is rendered whoever applies it"
  }
}

run "the_target_is_one_magicdns_name" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress = true
    tailscale_egress_target = "decision-api-staging.taile0bda9.ts.net 443 --anything"
  }

  expect_failures = [var.tailscale_egress_target]
}

# MUTATION: give tailscale_egress_target a default again, or drop its second
# validation, and an install that turns the egress on without naming a tailnet
# renders a forwarder with no target instead of being refused at plan.
run "the_egress_needs_a_target" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress = true
    tailscale_egress_target = null
  }

  expect_failures = [var.tailscale_egress_target]
}

# The Service's fixed clusterIP is dev's; a services range that does not hold
# it is refused rather than rendered into a Service Kubernetes would reject.
run "the_fixed_cluster_ip_must_be_in_the_services_range" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress = true
    services_cidr           = "10.68.0.0/20"
  }

  expect_failures = [output.tailscale_egress_manifest]
}
