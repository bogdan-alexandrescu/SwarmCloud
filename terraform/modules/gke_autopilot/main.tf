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
  # checkov:skip=CKV_GCP_12:Autopilot is always Dataplane V2, which enforces Kubernetes NetworkPolicy natively. The legacy `network_policy` addon this check looks for is REJECTED by the API on an Autopilot cluster.
  # checkov:skip=CKV_GCP_69:The GKE Metadata Server is mandatory and always on under Autopilot. The `node_config.workload_metadata_config` this check looks for is a node-level setting Autopilot does not accept.
  # checkov:skip=CKV_GCP_61:Intranode visibility is always on with Dataplane V2, and VPC flow logs are enabled on the subnet itself (see modules/network/main.tf `log_config`), which is where flows are actually captured.
  # checkov:skip=CKV_GCP_65:Google-group RBAC requires a gke-security-groups@<domain> group to already exist; pointing at one that does not FAILS cluster creation. Opt in with `authenticator_groups_security_group` once the group is created.
  # checkov:skip=CKV_GCP_66:Binary Authorization enforces a PROJECT-singleton policy, and saga-agents-staging is shared. Turning it on here subjects swarm pods to another team's attestation policy. Opt in with `binary_authorization_mode`.

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

  # Client certificates are a static credential with no expiry that bypasses
  # IAM entirely. Autopilot already defaults to not issuing one; this states it
  # so a future provider default cannot quietly change it.
  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  # Off by default: see the CKV_GCP_65 note above. The group must exist first.
  dynamic "authenticator_groups_config" {
    for_each = var.authenticator_groups_security_group == "" ? [] : [var.authenticator_groups_security_group]
    content {
      security_group = authenticator_groups_config.value
    }
  }

  # Off by default: see the CKV_GCP_66 note above. The policy is project-wide in
  # a project this platform shares with other teams.
  dynamic "binary_authorization" {
    for_each = var.binary_authorization_mode == "DISABLED" ? [] : [var.binary_authorization_mode]
    content {
      evaluation_mode = binary_authorization.value
    }
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
