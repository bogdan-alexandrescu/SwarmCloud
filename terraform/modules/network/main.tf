# Custom-mode VPC for the swarm.
#
# This is deliberately a NEW network. The project already holds
# `agents-staging-vpc`, owned by another team, and the default VPC. Neither is
# referenced, read or modified anywhere in this module.
#
# Several Compute resources (network, subnetwork, router, NAT, firewall) have no
# `labels` field in the GCP API at all. For those the ownership marker is carried
# in `description` in the same `managed-by=swarm-terraform` form, so the
# label-scoped destroy has something to match on. Resources that do support
# labels (addresses) get the real label map.

locals {
  owner_marker = "managed-by=${lookup(var.labels, "managed-by", "swarm-terraform")}"

  # Ranges considered "inside the swarm" for the internal-allow rule.
  internal_ranges = [var.subnet_cidr, var.pods_cidr, var.services_cidr]
}

resource "google_compute_network" "this" {
  project = var.project_id
  name    = "${var.name_prefix}-vpc"

  # Custom mode. auto_create_subnetworks would silently create a subnet in every
  # region, which is both wasteful and impossible to reason about for egress.
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  mtu                     = 1460

  # Keep the default internet route: Cloud NAT is what actually gates egress,
  # and deleting the route here would strand the NAT gateway too.
  delete_default_routes_on_create = false

  description = "${local.owner_marker}; swarm execution network (not agents-staging-vpc)"
}

resource "google_compute_subnetwork" "this" {
  project = var.project_id
  name    = "${var.name_prefix}-subnet-${var.region}"
  region  = var.region
  network = google_compute_network.this.id

  ip_cidr_range = var.subnet_cidr

  # Required so workloads with no external address can still reach
  # storage.googleapis.com, firestore.googleapis.com and Artifact Registry.
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = var.pods_range_name
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = var.services_range_name
    ip_cidr_range = var.services_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = var.flow_log_sampling
    metadata             = "INCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; swarm regional subnet"
}

# --------------------------------------------------------------------------
# Egress: Cloud Router + Cloud NAT with reserved addresses.
# --------------------------------------------------------------------------

resource "google_compute_address" "nat" {
  count = var.nat_static_ip_count

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-nat-ip-${count.index}"
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"

  labels      = var.labels
  description = "${local.owner_marker}; stable egress address for model-provider allow-lists"
}

resource "google_compute_router" "this" {
  project = var.project_id
  name    = "${var.name_prefix}-router"
  region  = var.region
  network = google_compute_network.this.id

  description = "${local.owner_marker}; swarm NAT router"
}

resource "google_compute_router_nat" "this" {
  project = var.project_id
  name    = "${var.name_prefix}-nat"
  region  = var.region
  router  = google_compute_router.this.name

  nat_ip_allocate_option = "MANUAL_ONLY"
  nat_ips                = google_compute_address.nat[*].self_link

  # Only the swarm subnet, including its Pod and Service ranges. Listing the
  # subnet explicitly rather than ALL_SUBNETWORKS_ALL_IP_RANGES is what stops
  # this NAT from ever carrying another team's traffic.
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.this.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  min_ports_per_vm = var.nat_min_ports_per_vm

  # Dynamic port allocation lets a busy worker burst past min_ports_per_vm
  # instead of silently failing to open new provider connections.
  enable_dynamic_port_allocation      = true
  max_ports_per_vm                    = 8192
  enable_endpoint_independent_mapping = false

  udp_idle_timeout_sec             = 30
  tcp_established_idle_timeout_sec = 1200
  tcp_transitory_idle_timeout_sec  = 30

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
