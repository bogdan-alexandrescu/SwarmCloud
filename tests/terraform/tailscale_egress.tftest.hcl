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
#   * the tailnet name is one MagicDNS name, or the plan is refused;
#   * swarm-api's own route (docs/design/agent-quality/jev-contract.md
#     section 4.1, the owner's answer to Q1): ON, an INTERNAL address is
#     reserved in the platform's subnet, labelled, the manifest puts the
#     internal load balancer `decision-api-internal` on it and admits
#     swarm-api's egress range to the forwarder, and swarm-api is given the
#     address as INTERNAL_DECISION_API_ADDRESS once internal_decision_api_enabled
#     offers the internal backends; OFF, there is no address and swarm-api's
#     value is "", which judgment.resolve_call_backend reads as `no_route`.
#
# The infra root's setup is egress.tftest.hcl's.

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

# A mock provider leaves a computed address unknown at plan; the route's
# address is fixed here so the rendered manifest and swarm-api's environment
# can be read.
override_resource {
  target          = google_compute_address.decision_api_route
  override_during = plan
  values = {
    address = "10.10.0.77"
  }
}

# The platform's subnet id, known at plan, so the route's address and
# swarm-api's Direct VPC egress can be compared as one subnet.
override_resource {
  target          = module.network.google_compute_subnetwork.this
  override_during = plan
  values = {
    id = "projects/example-project/regions/us-central1/subnetworks/swarm-subnet-us-central1"
  }
}

variables {
  project_id  = "example-project"
  environment = "dev"

  # No default since DAYONE-TF-1: the tailnet is the deployment's, so a test
  # that renders the egress names it.
  tailscale_egress_target = "decision-api.example-tailnet.ts.net"

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
      length(google_compute_address.decision_api_route) == 0
      && local.service_env["swarm-api"].INTERNAL_DECISION_API_ADDRESS == ""
      && output.tailscale_egress.swarm_api_address == ""
    )
    error_message = "with the egress off no route address is reserved and swarm-api's INTERNAL_DECISION_API_ADDRESS is empty (read as no_route)"
  }

  assert {
    condition = (
      output.tailscale_egress.service == "decision-api"
      && output.tailscale_egress.port == 443
      && output.tailscale_egress.target == "decision-api.example-tailnet.ts.net"
      && output.tailscale_egress.kubernetes_secret == "operator-oauth"
    )
    error_message = "one egress Service, decision-api on 443, to decision-api.example-tailnet.ts.net, mounting operator-oauth"
  }
}

run "on_one_reader_the_deployer_and_a_rendered_manifest" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress  = true
    deployer_service_account = "swarm-tf-deployer@example-project.iam.gserviceaccount.com"
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
      "serviceAccount:swarm-tf-deployer@example-project.iam.gserviceaccount.com",
    ])
    error_message = "the deployer that runs the sync is the secret's ONE reader: no service, no tenant, no pod"
  }

  assert {
    condition = (
      output.tailscale_egress.enabled
      && strcontains(output.tailscale_egress_manifest, "- decision-api.example-tailnet.ts.net")
      && length(regexall("(?m)__LABEL_PREFIX__/tailnet-fqdn: decision-api\\.example-tailnet\\.ts\\.net$", output.tailscale_egress_manifest)) > 0
      && length(regexall("__[A-Z0-9_]+__", replace(output.tailscale_egress_manifest, "__LABEL_PREFIX__", ""))) == 0
    )
    error_message = "on, the manifest is rendered with the tailnet name and no placeholder left"
  }

  assert {
    condition     = strcontains(output.tailscale_egress_manifest, "clusterIP: 10.48.6.120")
    error_message = "on, the decision-api Service carries the fixed clusterIP judging pods pin the tailnet name to (local.decision_api_cluster_ip, #1380)"
  }
}

# MUTATION: drop google_compute_address.decision_api_route, make it EXTERNAL,
# move it out of the platform's subnet, drop its label, or stop rendering it
# into the manifest or swarm-api's environment, and a run below goes red.
run "on_swarm_api_reaches_the_egress_through_an_internal_load_balancer" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress       = true
    internal_decision_api_enabled = true
    deployer_service_account      = "swarm-tf-deployer@example-project.iam.gserviceaccount.com"
  }

  assert {
    condition = (
      length(google_compute_address.decision_api_route) == 1
      && google_compute_address.decision_api_route[0].address_type == "INTERNAL"
      && google_compute_address.decision_api_route[0].region == var.region
    )
    error_message = "on, swarm-api's route is ONE reserved INTERNAL address in the platform's own subnet, which its Direct VPC egress reaches"
  }

  # swarm-api's own Direct VPC egress leaves on module.network.subnetwork_id
  # (main.tf module.cloud_run_api, vpc_egress ALL_TRAFFIC; the module's
  # vpc_access is held by cloud_run.tftest.hcl), so the address must be in
  # that very subnet for the route to exist.
  assert {
    condition     = google_compute_address.decision_api_route[0].subnetwork == module.network.subnetwork_id
    error_message = "the route: the load balancer's address is in the subnet swarm-api's Direct VPC egress leaves on"
  }

  assert {
    condition     = google_compute_address.decision_api_route[0].labels["managed-by"] == "swarm-terraform"
    error_message = "every resource carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  assert {
    condition = (
      strcontains(output.tailscale_egress_manifest, "name: decision-api-internal")
      && strcontains(output.tailscale_egress_manifest, "networking.gke.io/load-balancer-type: \"Internal\"")
      && strcontains(output.tailscale_egress_manifest, "loadBalancerIP: 10.10.0.77")
      && length(regexall("__[A-Z0-9_]+__", replace(output.tailscale_egress_manifest, "__LABEL_PREFIX__", ""))) == 0
    )
    error_message = "on, the manifest puts the internal passthrough load balancer decision-api-internal on the reserved address, with no placeholder left"
  }

  assert {
    condition     = strcontains(output.tailscale_egress_manifest, "cidr: ${var.subnet_cidr}")
    error_message = "on, the forwarder's ingress policy admits swarm-api's Direct VPC egress range (var.subnet_cidr), as swarm-live-ingress and swarm-graph-ingress do"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].INTERNAL_DECISION_API_ADDRESS == "10.10.0.77"
      && local.service_env["swarm-api"].INTERNAL_DECISION_API_URL == "https://decision-api.example-tailnet.ts.net"
      && output.tailscale_egress.swarm_api_address == "10.10.0.77"
      && output.tailscale_egress.internal_service == "decision-api-internal"
    )
    error_message = "on, swarm-api is given the load balancer's address beside the decision API's tailnet origin"
  }

  assert {
    condition     = !contains(keys(local.service_env["swarm-scheduler"]), "INTERNAL_DECISION_API_ADDRESS")
    error_message = "only swarm-api dials the load balancer; the scheduler's pods keep their hostAliases route"
  }
}

# The egress rendered but the internal backends not offered: the load balancer
# exists (the manifest names it) but swarm-api is given no address, so its
# contract review reads no_route rather than calling a backend the platform
# does not offer.
run "egress_on_backends_off_gives_swarm_api_no_address" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_tailscale_egress       = true
    internal_decision_api_enabled = false
  }

  assert {
    condition = (
      length(google_compute_address.decision_api_route) == 1
      && local.service_env["swarm-api"].INTERNAL_DECISION_API_ADDRESS == ""
    )
    error_message = "with internal_decision_api_enabled off swarm-api's INTERNAL_DECISION_API_ADDRESS is empty"
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
    tailscale_egress_target = "decision-api.example-tailnet.ts.net 443 --anything"
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
