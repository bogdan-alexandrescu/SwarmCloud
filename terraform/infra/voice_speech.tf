# Voice it's own speech engine: the speech machine exists, stopped, on
# swarm-vpc (docs/design/voice-it-own-speech.md §3.2, §5, §6 and §12.3; lane
# L1 of its build plan, part of #2688).
#
# What this file makes, and only this, all behind one switch:
#
#   * the speech machine: ONE g2-standard-4 (one NVIDIA L4) in ONE zone,
#     on-demand, made STOPPED. swarm-api starts it for a Voice it session and
#     stops it after (swarm_api/voice/speech_machine.py, lane L4);
#   * its reserved internal address on the subnet, so swarm-api's
#     SWARM_SPEECH_ADDR never moves;
#   * a firewall pair on its tag: TCP 8080 from the subnet, everything else
#     refused, ahead of allow_internal;
#   * swarm-api's operator role on that one instance (row I1);
#   * the machine account's three grants: its own image, its own logs, its own
#     metrics (row I3 with the owner's answer 2);
#   * the TTL policy that deletes the telemetry's 5-second samples.
#
# ONE ZONE, ONE MACHINE (owner, 2026-11-05, answer 1). A zone with no free L4
# makes the session fall back to text mode; there is no second machine to try.
#
# ON-DEMAND (invariant 6: Spot is off platform-wide). GPU machines cannot
# live-migrate, so a host event TERMINATEs it; automatic_restart = false keeps
# it stopped after one, never running with no session to pay for.
#
# STOPPED, AND swarm-api OWNS ITS STATE. desired_status = "TERMINATED" makes
# Terraform create it and stop it. From then on swarm-api starts and stops it,
# and at each start applies Settings' machine type, the release's speech
# server digest and the models (§4.2 step 1, §5.3, §11.4); ignore_changes keeps
# the next plan from undoing any of that or stopping a machine mid-session.
#
# THE IDENTITY CHECK IS THE REAL GATE (§3.2). Tenant Cloud Run Jobs on Direct
# VPC egress also take addresses in the subnet, so the firewall's source range
# proves nothing about the caller. swarm-api presents a Google ID token for
# audience `swarm-speech` on every socket; the speech server checks it with
# Google's public certificates and admits only the email in the
# `swarm-speech-caller` metadata, swarm-api's account. No secret anywhere.
#
# THE ACCOUNT. `swarm-speech` is made by terraform/bootstrap
# (platform_service_accounts.tf) with its operator role and the two actAs
# grants on it (the release deployer's, to attach it here; swarm-api's, row
# I2). This root reads it and never makes it. Both roots take the account id
# from modules/service_account_ids (speech_id) and the role id from
# modules/custom_role_ids (speech_operator).
#
# OFF until both `voice_speech.enabled` and `voice_it = "on"`. Every
# environment's tfvars leaves it off, so merging this makes nothing. Turning it
# on is an IAM change, so the release that carries it waits at the IAM gate for
# the owner.

locals {
  voice_speech_on = var.voice_speech.enabled && var.voice_it == "on"

  # The machine's identity, derived, not read, so the plan knows it: the id is
  # modules/service_account_ids' speech_id, the one terraform/bootstrap gives
  # the account.
  speech_email  = "${module.service_account_ids.speech_id}@${var.project_id}.iam.gserviceaccount.com"
  speech_member = "serviceAccount:${local.speech_email}"

  # The firewall pair's target, and the machine's only network tag.
  speech_tag = "${var.name_prefix}-speech"

  # "us-central1-a" -> "a": the machine is `<prefix>-speech-a`, one per zone.
  speech_zone_suffix = element(split("-", var.voice_speech.zone), 2)

  # The machine's name up to its zone letter, and so its boot disk's, which
  # takes the instance's name. terraform/bootstrap/deployer_conditions.tf
  # grants the release deployer the instance and disk verbs on names that start
  # with exactly this (deployer_speech_machine_prefix): rename one, rename both
  # (tests/unit/terraform/test_voice_speech.py holds them equal).
  speech_machine_prefix = "${var.name_prefix}-speech-"

  speech_labels = merge(local.labels, { "component" = "voice-speech" })

  # The custom role terraform/bootstrap defines (platform_service_accounts.tf,
  # google_project_iam_custom_role.speech_operator), by the name both roots
  # read from modules/custom_role_ids. A plain string: this root may not read a
  # role (#79).
  speech_operator_role = module.speech_role_ids.names["speech_operator"]
}

# The custom role ids both roots spell from one place. No resource and no data
# source: plain strings, known at plan.
module "speech_role_ids" {
  source = "../modules/custom_role_ids"

  project_id = var.project_id
}

# The account terraform/bootstrap made. A plan against a project whose
# bootstrap apply has not made it fails here, by the account's name: apply
# terraform/bootstrap first.
data "google_service_account" "speech" {
  count = local.voice_speech_on ? 1 : 0

  project    = var.project_id
  account_id = local.speech_email
}

# swarm-api's path to the machine (§3.2): Direct VPC egress already routes
# all of swarm-api's traffic into the subnet, so a reserved address is all it
# needs, as voice.tf reserves swarm-voice's.
resource "google_compute_address" "speech" {
  count = local.voice_speech_on ? 1 : 0

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-speech"
  address_type = "INTERNAL"
  subnetwork   = module.network.subnetwork_id
  description  = "Internal address of Voice it's speech machine (docs/design/voice-it-own-speech.md section 3.2)."

  labels = local.speech_labels
}

# TCP 8080 from the subnet, where swarm-api's Direct VPC egress takes its
# addresses. Priority 940, ahead of the deny below.
resource "google_compute_firewall" "allow_speech_from_subnet" {
  count = local.voice_speech_on ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}-fw-allow-speech"
  network = module.network.network_name

  direction     = "INGRESS"
  priority      = 940
  source_ranges = [var.subnet_cidr]
  target_tags   = [local.speech_tag]

  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }

  log_config {
    metadata = "EXCLUDE_ALL_METADATA"
  }

  description = "managed-by=swarm-terraform; swarm-api to the speech machine, TCP 8080 from the subnet only"
}

# Everything else to the machine, refused at 950: ahead of allow_internal
# (1000), which would otherwise admit the GKE pod ranges and every other
# internal range on every port.
resource "google_compute_firewall" "deny_speech_other" {
  count = local.voice_speech_on ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}-fw-deny-speech"
  network = module.network.network_name

  direction     = "INGRESS"
  priority      = 950
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [local.speech_tag]

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }

  description = "managed-by=swarm-terraform; refuses all other ingress to the speech machine, ahead of allow-internal"
}

# The machine (§5.1).
resource "google_compute_instance" "speech" {
  # checkov:skip=CKV_GCP_38:A customer-supplied key is raw key material in the Terraform config and state, and no secret goes into Terraform (CLAUDE.md). The disk holds the OS, the speech server image and public model weights, never audio or tenant data; it is encrypted with Google-managed keys.
  count = local.voice_speech_on ? 1 : 0

  project      = var.project_id
  zone         = var.voice_speech.zone
  name         = "${local.speech_machine_prefix}${local.speech_zone_suffix}"
  machine_type = var.voice_speech.machine_type
  description  = "Voice it's speech machine: speech-to-text and text-to-speech on one L4. Kept stopped; swarm-api starts it for a session."

  # A g2 type brings its L4 with it: no guest_accelerator block.
  tags   = [local.speech_tag]
  labels = local.speech_labels

  # Created, then stopped. swarm-api starts and stops it from here on.
  desired_status            = "TERMINATED"
  allow_stopping_for_update = true
  deletion_protection       = var.deletion_protection

  boot_disk {
    initialize_params {
      image  = var.voice_speech.image
      size   = var.voice_speech.disk_gb
      type   = "pd-balanced"
      labels = local.speech_labels
    }
  }

  # On swarm-vpc's subnet at the reserved address, with NO external IP (no
  # access_config): it pulls its image and the models' public weights through
  # the subnet's Cloud NAT.
  network_interface {
    subnetwork = module.network.subnetwork_id
    network_ip = google_compute_address.speech[0].address
  }

  scheduling {
    provisioning_model  = "STANDARD"
    preemptible         = false
    on_host_maintenance = "TERMINATE"
    automatic_restart   = false
  }

  # Secure boot off: the NVIDIA kernel module on the Deep Learning VM image is
  # not signed for it (to be measured on the machine, M1). vTPM and integrity
  # monitoring stay on.
  shielded_instance_config {
    enable_secure_boot          = false
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  # Its own account; what it may do is exactly its three grants below.
  service_account {
    email  = local.speech_email
    scopes = ["cloud-platform"]
  }

  # Names and numbers only, never a secret. The image and the models are
  # empty here: swarm-api writes them at the first start and rewrites them at
  # any start where they changed (§4.2 step 1, §5.3) -- the release's
  # swarm-speech digest reaches it as SWARM_SPEECH_IMAGE (lanes L3 and L4),
  # never through this root, so a new digest never re-plans a stopped machine.
  metadata = {
    "swarm-speech-image"        = ""
    "swarm-speech-models"       = jsonencode({})
    "swarm-speech-caller"       = module.iam.service_account_emails["swarm-api"]
    "swarm-speech-idle-minutes" = tostring(var.voice_speech.idle_minutes)
    "block-project-ssh-keys"    = "TRUE"
    "serial-port-enable"        = "FALSE"
  }

  lifecycle {
    ignore_changes = [
      desired_status,
      machine_type,
      metadata["swarm-speech-image"],
      metadata["swarm-speech-models"],
      metadata["swarm-speech-idle-minutes"],
    ]
  }

  depends_on = [
    data.google_service_account.speech,
    google_compute_firewall.deny_speech_other,
  ]
}

# --- IAM (§6; the owner's answer 2) ------------------------------------------
#
# I1. swarm-api starts, stops, reads and applies Settings to THIS instance,
# and to no other: an instance-level member, never a project role. The role is
# swarmSpeechOperator (get, start, stop, setMachineType, setMetadata,
# getSerialPortOutput), defined by terraform/bootstrap.
resource "google_compute_instance_iam_member" "api_speech_operator" {
  count = local.voice_speech_on ? 1 : 0

  project       = var.project_id
  zone          = var.voice_speech.zone
  instance_name = google_compute_instance.speech[0].name
  role          = local.speech_operator_role
  member        = module.iam.service_account_members["swarm-api"]
}

# I3. The machine pulls its own speech server image: reader on the platform's
# repository only.
resource "google_artifact_registry_repository_iam_member" "speech_reader" {
  count = local.voice_speech_on ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = module.artifact_registry.repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.speech_member

  depends_on = [data.google_service_account.speech]
}

# I5 (answer 2). The machine writes its own logs: the speech server's and its
# systemd unit's, so a failed boot or a crash is in Cloud Logging and not only
# on the serial port. Write only.
resource "google_project_iam_member" "speech_log_writer" {
  count = local.voice_speech_on ? 1 : 0

  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = local.speech_member

  depends_on = [data.google_service_account.speech]
}

# I6 (answer 2). Its own metrics, beside the telemetry it sends over the
# socket (§12.1): the GPU's and the host's readings while it runs. Write only.
resource "google_project_iam_member" "speech_metric_writer" {
  count = local.voice_speech_on ? 1 : 0

  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = local.speech_member

  depends_on = [data.google_service_account.speech]
}

# --- Telemetry's samples (§12.3) ----------------------------------------------
#
# voice_speech_sessions/{session}/samples/{seq}: one every 5 s while a session
# runs, each with `expire_at` 30 days on. Firestore deletes them at expiry; the
# per-session summaries carry no expire_at and are kept. A TTL policy is per
# collection group, so it covers every `samples` subcollection; no other
# feature writes one.
resource "google_firestore_field" "voice_speech_samples_ttl" {
  count = local.voice_speech_on ? 1 : 0

  project    = var.project_id
  database   = module.firestore.database_name
  collection = "samples"
  field      = "expire_at"

  ttl_config {}

  # Nothing queries by expiry; an index on it is write amplification on every
  # sample.
  index_config {}
}

check "voice_speech_needs_voice_it" {
  assert {
    condition     = !var.voice_speech.enabled || var.voice_it == "on"
    error_message = "voice_speech.enabled is set but voice_it is not \"on\": the speech machine serves Voice it's sessions, so nothing was made."
  }
}

check "voice_speech_zone_in_region" {
  assert {
    condition     = !local.voice_speech_on || startswith(var.voice_speech.zone, "${var.region}-")
    error_message = "voice_speech.zone is not in var.region: the machine must sit on the region's subnet."
  }
}

output "voice_speech" {
  description = "Voice it's speech machine: what swarm-api needs to run it (SWARM_SPEECH_*). Names only; never a value."
  value = {
    enabled         = local.voice_speech_on
    instance        = local.voice_speech_on ? google_compute_instance.speech[0].name : null
    zone            = local.voice_speech_on ? var.voice_speech.zone : null
    address         = local.voice_speech_on ? google_compute_address.speech[0].address : null
    port            = 8080
    audience        = "swarm-speech"
    service_account = local.voice_speech_on ? local.speech_email : null
    idle_minutes    = var.voice_speech.idle_minutes
  }
}
