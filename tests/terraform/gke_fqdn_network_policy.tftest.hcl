# Hostname-based (FQDN) network policy on SwarmCloud's own Autopilot cluster,
# terraform/modules/gke_autopilot (`google_container_cluster.this`), never the
# other team's agents-staging cluster.
#
# kubernetes/network-policies/ holds swarm-voice's FQDNNetworkPolicy, which
# fails closed: until the cluster admits hostname-based policy, swarm-voice
# reaches only DNS and the metadata server. The owner chose (2026-11-01) to turn
# it on, on two conditions that both hold:
#
#   * no charge: GKE's release note of September 2, 2025 lists "Fully Qualified
#     Domain Name (FQDN) Network Policy" among the features "included in the
#     GKE offering at no additional cost"; GKE has no editions or tiers since;
#   * in place: the google provider sends DesiredEnableFqdnNetworkPolicy in a
#     cluster update. Its ForceNewIfChange (isBeenEnabled) replaces the cluster
#     only when the setting goes from true BACK to false -- which is why
#     dev.tfvars says never to turn it off again.
#
# What is held here:
#
#   * the module's variable defaults to today's value, off, so no other
#     environment changes;
#   * the variable reaches the cluster, and terraform/infra passes
#     gke_enable_fqdn_network_policy through to it;
#   * dev turns it on, held against dev.tfvars' text (a terraform test cannot
#     load a tfvars file into a run; live.tftest.hcl does the same).
#
# The infra root's setup is live.tftest.hcl's.

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

  # The module runs' inputs.
  network             = "projects/saga-agents-staging/global/networks/swarm-vpc"
  subnetwork          = "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1"
  pods_range_name     = "swarm-pods"
  services_range_name = "swarm-services"
  labels              = { "managed-by" = "swarm-terraform" }

  # The infra runs' inputs.
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

  # Read as text by the dev assertion.
  tf_root = "../../terraform/infra"
}

run "off_by_default_the_cluster_is_unchanged" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  assert {
    condition     = google_container_cluster.this.enable_fqdn_network_policy == false
    error_message = "enable_fqdn_network_policy defaults to today's value, off: an environment that does not ask for it plans no change"
  }
}

run "on_the_cluster_admits_hostname_based_policy" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  variables {
    enable_fqdn_network_policy = true
  }

  assert {
    condition     = google_container_cluster.this.enable_fqdn_network_policy == true
    error_message = "enable_fqdn_network_policy = true sets the cluster's enable_fqdn_network_policy, which swarm-voice's FQDNNetworkPolicy needs"
  }

  assert {
    condition     = google_container_cluster.this.datapath_provider == "ADVANCED_DATAPATH"
    error_message = "FQDN network policy requires GKE Dataplane V2"
  }

  assert {
    condition     = output.fqdn_network_policy_enabled == true
    error_message = "the module reports the setting it applied"
  }
}

run "infra_passes_the_setting_through_and_dev_turns_it_on" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # dev's two switches, as committed.
  variables {
    enable_gke_autopilot           = true
    gke_enable_fqdn_network_policy = true
  }

  assert {
    condition     = length(regexall("(?m)^gke_enable_fqdn_network_policy\\s*=\\s*true\\b", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets gke_enable_fqdn_network_policy = true (owner decision 2026-11-01)"
  }

  assert {
    condition     = length(regexall("(?m)^enable_gke_autopilot\\s*=\\s*true\\b", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets enable_gke_autopilot = true, without which there is no cluster to set"
  }

  assert {
    condition     = module.gke_autopilot[0].fqdn_network_policy_enabled == true
    error_message = "terraform/infra passes gke_enable_fqdn_network_policy to the gke_autopilot module"
  }
}

run "infra_leaves_it_off_by_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_gke_autopilot = true
  }

  assert {
    condition     = module.gke_autopilot[0].fqdn_network_policy_enabled == false
    error_message = "gke_enable_fqdn_network_policy defaults to off in terraform/infra: prod plans no change"
  }
}
