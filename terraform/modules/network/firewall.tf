# Firewall.
#
# Default-deny ingress at the lowest priority, then narrowly scoped allows. No
# rule anywhere in this file sources from 0.0.0.0/0, and no rule opens 22 or
# 3389 from the internet.

resource "google_compute_firewall" "deny_all_ingress" {
  project = var.project_id
  name    = "${var.name_prefix}-fw-deny-all-ingress"
  network = google_compute_network.this.name

  direction = "INGRESS"
  priority  = 65534

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; default-deny ingress for the swarm VPC"
}

resource "google_compute_firewall" "allow_internal" {
  project = var.project_id
  name    = "${var.name_prefix}-fw-allow-internal"
  network = google_compute_network.this.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = local.internal_ranges

  allow {
    protocol = "tcp"
    ports    = ["1-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["1-65535"]
  }

  allow {
    protocol = "icmp"
  }

  log_config {
    metadata = "EXCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; east-west traffic inside the swarm ranges only"
}

# --------------------------------------------------------------------------
# Tenant worker isolation.
#
# The rule above is what the GKE data plane needs: nodes, Pods and Services talk
# to each other freely inside the swarm ranges. Cloud Run Jobs executions attach
# to the SAME subnet via Direct VPC egress, so without this rule every tenant's
# worker instance would sit in that same unsegmented L4 broadcast domain as
# every other tenant's.
#
# That matters more here than it would anywhere else, because Cloud Run Jobs is
# the PRIMARY backend and agent workers run untrusted repositories and untrusted
# prompts by design. Any port an agent runtime happens to open on its own
# instance -- a Chromium remote-debugging port, an MCP server, a language
# server, a dev server the agent decided to start -- would be a plain TCP
# connection away from another tenant's worker. The repository's tenant network
# policy lives in Kubernetes NetworkPolicy, which does not apply to Cloud Run
# instances at all, so nothing else covers this path.
#
# A worker never accepts an inbound connection from anywhere: it clones, calls a
# provider and writes to GCS, all outbound. So the rule is a flat deny of ALL
# ingress to anything carrying the worker tag, at a priority that beats the
# internal allow. Egress is untouched -- workers still reach providers through
# Cloud NAT.
resource "google_compute_firewall" "deny_worker_ingress" {
  project = var.project_id
  name    = "${var.name_prefix}-fw-deny-worker-ingress"
  network = google_compute_network.this.name

  direction = "INGRESS"

  # Below allow_internal (1000) and below the health-check and webhook rules, so
  # it wins against all of them for tagged instances.
  priority = 900

  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.worker_network_tag]

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; agent workers accept no inbound connection, including from another tenant's worker"
}

resource "google_compute_firewall" "allow_health_checks" {
  project = var.project_id
  name    = "${var.name_prefix}-fw-allow-health-checks"
  network = google_compute_network.this.name

  direction = "INGRESS"
  priority  = 1000

  # Google's fixed health-check and load-balancer probe ranges. Not the internet.
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]

  allow {
    protocol = "tcp"
  }

  log_config {
    metadata = "EXCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; Google LB and health-check probes"
}

resource "google_compute_firewall" "allow_gke_webhooks" {
  project = var.project_id
  name    = "${var.name_prefix}-fw-allow-gke-webhooks"
  network = google_compute_network.this.name

  direction = "INGRESS"
  priority  = 1000

  # A private control plane can only reach node ports that are opened
  # explicitly; GKE auto-creates 10250/443 but not admission-webhook ports.
  source_ranges = [var.gke_master_cidr]

  allow {
    protocol = "tcp"
    ports    = ["8443", "9443", "15017"]
  }

  log_config {
    metadata = "EXCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; private GKE control plane to admission webhooks"
}

# --------------------------------------------------------------------------
# Optional default-deny egress.
# --------------------------------------------------------------------------

resource "google_compute_firewall" "deny_all_egress" {
  count = var.restrict_egress ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}-fw-deny-all-egress"
  network = google_compute_network.this.name

  direction          = "EGRESS"
  priority           = 65534
  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; default-deny egress"
}

resource "google_compute_firewall" "allow_egress_allowed_ports" {
  count = var.restrict_egress ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}-fw-allow-egress"
  network = google_compute_network.this.name

  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["0.0.0.0/0"]

  allow {
    protocol = "tcp"
    ports    = var.egress_allowed_ports
  }

  allow {
    protocol = "udp"
    ports    = ["53"]
  }

  log_config {
    metadata = "EXCLUDE_ALL_METADATA"
  }

  description = "${local.owner_marker}; permitted egress ports when restrict_egress is on"
}
