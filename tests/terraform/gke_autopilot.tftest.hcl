# GKE Autopilot is the SECONDARY backend -- browser, GPU and anything over the
# Cloud Run ceiling. The cluster this module must never touch is agents-staging,
# which is live and owned by another team.

mock_provider "google" {}

variables {
  project_id          = "saga-agents-staging"
  network             = "projects/saga-agents-staging/global/networks/swarm-vpc"
  subnetwork          = "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1"
  pods_range_name     = "swarm-pods"
  services_range_name = "swarm-services"
  labels              = { "managed-by" = "swarm-terraform" }
}

run "regional_private_autopilot_with_workload_identity" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  assert {
    condition     = google_container_cluster.this.enable_autopilot == true
    error_message = "the cluster must be Autopilot: no nodes to size, patch or drain"
  }

  assert {
    condition     = google_container_cluster.this.location == "us-central1"
    error_message = "regional, not zonal: a zonal control plane is a single point of failure for every browser and GPU task"
  }

  assert {
    condition     = google_container_cluster.this.private_cluster_config[0].enable_private_nodes == true
    error_message = "execution workloads get no public IPs"
  }

  assert {
    condition     = google_container_cluster.this.workload_identity_config[0].workload_pool == "saga-agents-staging.svc.id.goog"
    error_message = "Workload Identity is how a tenant Pod assumes only its own tenant's GSA"
  }

  assert {
    condition     = google_container_cluster.this.release_channel[0].channel == "REGULAR"
    error_message = "a release channel must be set; UNSPECIFIED pins the cluster to manual upgrades"
  }

  assert {
    condition     = google_container_cluster.this.datapath_provider == "ADVANCED_DATAPATH"
    error_message = "Dataplane V2 is what enforces Kubernetes NetworkPolicy on Autopilot"
  }

  assert {
    condition     = length(google_container_cluster.this.maintenance_policy[0].recurring_window) == 1
    error_message = "a maintenance window keeps upgrades off the middle of a working day"
  }

  assert {
    condition     = google_container_cluster.this.deletion_protection == true
    error_message = "deletion protection defaults on"
  }

  assert {
    condition     = google_container_cluster.this.master_auth[0].client_certificate_config[0].issue_client_certificate == false
    error_message = "a client certificate is a static credential that bypasses IAM"
  }

  assert {
    condition     = length(google_container_cluster.this.logging_config[0].enable_components) > 0 && length(google_container_cluster.this.monitoring_config[0].enable_components) > 0
    error_message = "logging and monitoring components must be enabled"
  }
}

run "the_cluster_uses_the_swarm_network_and_its_own_secondary_ranges" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  assert {
    condition     = google_container_cluster.this.networking_mode == "VPC_NATIVE"
    error_message = "VPC-native is required for Autopilot and for the secondary ranges"
  }

  assert {
    condition     = google_container_cluster.this.ip_allocation_policy[0].cluster_secondary_range_name == "swarm-pods"
    error_message = "Pods come from the swarm subnet's own secondary range"
  }

  assert {
    condition     = google_container_cluster.this.network == var.network
    error_message = "the cluster must sit in swarm-vpc, never in agents-staging-vpc"
  }
}

run "refuses_to_manage_another_teams_cluster" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  variables {
    cluster_name = "agents-staging"
  }

  expect_failures = [var.cluster_name]
}

run "an_open_master_authorized_network_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  variables {
    master_authorized_cidrs = [{ cidr_block = "0.0.0.0/0", display_name = "everywhere" }]
  }

  expect_failures = [var.master_authorized_cidrs]
}

run "an_unknown_release_channel_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/gke_autopilot"
  }

  variables {
    release_channel = "UNSPECIFIED"
  }

  expect_failures = [var.release_channel]
}
