# The swarm VPC is a NEW, custom-mode network.
#
# The project already holds agents-staging-vpc and a default VPC, both owned by
# other teams. Nothing in this module may reference either, and the destroy
# guard's name prefix is the second line of defence behind the label.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"
  labels = {
    "managed-by" = "swarm-terraform"
    "swarm-env"  = "test"
  }
}

run "custom_mode_vpc_with_private_google_access" {
  command = plan

  module {
    source = "../../terraform/modules/network"
  }

  assert {
    condition     = google_compute_network.this.name == "swarm-vpc"
    error_message = "the VPC must be swarm-vpc, never the default network and never agents-staging-vpc"
  }

  assert {
    condition     = google_compute_network.this.auto_create_subnetworks == false
    error_message = "auto_create_subnetworks would silently create a subnet in every region"
  }

  assert {
    condition     = google_compute_subnetwork.this.private_ip_google_access == true
    error_message = "workloads have no public IP; without Private Google Access they cannot reach Firestore, GCS or Artifact Registry"
  }

  assert {
    condition     = google_compute_subnetwork.this.region == "us-central1"
    error_message = "the subnet must be regional in us-central1"
  }

  assert {
    condition = length([
      for r in google_compute_subnetwork.this.secondary_ip_range : r
      if r.range_name == "swarm-pods" || r.range_name == "swarm-services"
    ]) == 2
    error_message = "both GKE secondary ranges must exist on the swarm subnet"
  }

  assert {
    condition     = length(google_compute_subnetwork.this.log_config) == 1
    error_message = "VPC flow logs must be enabled on the swarm subnet"
  }
}

run "nat_provides_stable_egress_without_public_ips" {
  command = plan

  module {
    source = "../../terraform/modules/network"
  }

  variables {
    nat_static_ip_count = 2
  }

  assert {
    condition     = google_compute_router_nat.this.nat_ip_allocate_option == "MANUAL_ONLY"
    error_message = "providers allow-list source addresses, so egress addresses must be reserved, not auto-allocated"
  }

  assert {
    condition     = length(google_compute_address.nat) == 2
    error_message = "one reserved address per nat_static_ip_count"
  }

  assert {
    condition     = google_compute_router_nat.this.source_subnetwork_ip_ranges_to_nat == "LIST_OF_SUBNETWORKS"
    error_message = "ALL_SUBNETWORKS_ALL_IP_RANGES would put another team's traffic behind the swarm NAT"
  }
}

run "firewall_is_default_deny_and_opens_nothing_to_the_internet" {
  command = plan

  module {
    source = "../../terraform/modules/network"
  }

  assert {
    condition     = google_compute_firewall.deny_all_ingress.direction == "INGRESS" && length(google_compute_firewall.deny_all_ingress.deny) == 1
    error_message = "the lowest-priority ingress rule must be a deny-all"
  }

  assert {
    condition     = google_compute_firewall.deny_all_ingress.priority > google_compute_firewall.allow_internal.priority
    error_message = "the deny-all must sit BELOW the narrow allows, or it would shadow them"
  }

  assert {
    condition     = !contains(google_compute_firewall.allow_internal.source_ranges, "0.0.0.0/0")
    error_message = "no allow rule may source from the internet"
  }

  assert {
    condition     = !contains(google_compute_firewall.allow_health_checks.source_ranges, "0.0.0.0/0")
    error_message = "health-check allows are limited to Google's probe ranges"
  }
}

run "name_prefix_cannot_wander_outside_the_swarm" {
  command = plan

  module {
    source = "../../terraform/modules/network"
  }

  variables {
    name_prefix = "agents-staging"
  }

  # destroy.sh is label-scoped and the prefix is the backstop. A prefix that is
  # not "swarm..." could name a resource another team owns.
  expect_failures = [var.name_prefix]
}
