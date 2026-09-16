# GKE Autopilot -- the SECONDARY backend.
#
# Cloud Run Jobs runs everything it can hold. This cluster exists only for the
# work Cloud Run cannot take: Chromium (which needs a large /dev/shm we control),
# GPUs, and anything over 32 GiB of memory.
#
# Autopilot specifics worth knowing before editing:
#   * Node-level settings (node_config, node pools, Spot) are not settable and
#     must not appear here. Spot is banned platform-wide anyway -- Spot Pods
#     cannot use Autopilot extended run time, so Spot and "no preemption" are
#     mutually exclusive (CONTRACT.md invariant 6).
#   * Autopilot is always Dataplane V2, so Kubernetes NetworkPolicy is enforced
#     without the legacy `network_policy` addon. Setting that addon on an
#     Autopilot cluster is rejected by the API, which is why it is absent.
#   * Workload Identity is mandatory on Autopilot; it is declared explicitly so
#     the pool name is visible to the tenancy module rather than implied.

resource "google_container_cluster" "this" {
  project = var.project_id

  # Regional, not zonal: a zonal control plane is a single point of failure for
  # every browser and GPU task in the swarm.
  location = var.region
  name     = var.cluster_name

  enable_autopilot = true

  network    = var.network
  subnetwork = var.subnetwork

  # Dataplane V2. Explicit rather than implicit so the network-policy story is
  # readable from the configuration.
  datapath_provider                        = "ADVANCED_DATAPATH"
  enable_cilium_clusterwide_network_policy = var.enable_cilium_clusterwide_network_policy

  networking_mode = "VPC_NATIVE"

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  private_cluster_config {
    # Nodes never receive a public address. Egress is via Cloud NAT.
    enable_private_nodes    = true
    enable_private_endpoint = var.enable_private_endpoint
    master_ipv4_cidr_block  = var.master_ipv4_cidr_block

    master_global_access_config {
      enabled = true
    }
  }

  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled = false

    dynamic "cidr_blocks" {
      for_each = var.master_authorized_cidrs
      content {
        cidr_block   = cidr_blocks.value.cidr_block
        display_name = cidr_blocks.value.display_name
      }
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = var.release_channel
  }

  logging_config {
    enable_components = var.logging_components
  }

  monitoring_config {
    enable_components = var.monitoring_components

    managed_prometheus {
      enabled = var.enable_managed_prometheus
    }
  }

  maintenance_policy {
    recurring_window {
      start_time = var.maintenance_start_time
      end_time   = var.maintenance_end_time
      recurrence = var.maintenance_recurrence
    }
  }

  security_posture_config {
    mode               = "BASIC"
    vulnerability_mode = "VULNERABILITY_BASIC"
  }

  cost_management_config {
    enabled = true
  }

  # Secrets reach Pods through the CSI driver, never through a baked image.
  secret_manager_config {
    enabled = true
  }

  # Kubernetes API access is via IAM + Workload Identity only.
  enable_legacy_abac = false

  deletion_protection = var.deletion_protection

  resource_labels = var.labels

  lifecycle {
    precondition {
      condition     = var.cluster_name != "agents-staging"
      error_message = "Refusing to manage `agents-staging`: it is a live cluster owned by another team."
    }
  }
}
