# Voice it's own speech engine: everything the speech machine needs on
# swarm-vpc, and the template swarm-api makes it from
# (docs/design/voice-it-own-speech.md §3.2, §5, §6 and §12.3; lane L1 of its
# build plan, part of #2688; zone fallback, owner, 2026-11-09).
#
# What this file makes, and only this, all behind one switch:
#
#   * the speech machine's regional instance TEMPLATE: one g2-standard-4 (one
#     NVIDIA L4), on-demand, with the pinned image, the speech account, the
#     network tag, the reserved address and the metadata the boot reads.
#     NOT the machine itself: swarm-api creates it from the template, or
#     starts it, for a Voice it session's Start and stops it after
#     (swarm_api/voice/speech_machine.py);
#   * its reserved internal address on the subnet, so swarm-api's
#     SWARM_SPEECH_ADDR never moves, whichever zone the machine is in;
#   * a firewall pair on its tag: TCP 8080 from the subnet, everything else
#     refused, ahead of allow_internal;
#   * NOT swarm-api's operator role (row I1): terraform/bootstrap grants it,
#     on the project, conditioned to the speech machine (see I1 below);
#   * the machine account's three grants: its own image, its own logs, its own
#     metrics (row I3 with the owner's answer 2);
#   * the TTL policy that deletes the telemetry's 5-second samples.
#
# GOOGLE PICKS THE ZONE, ONE MACHINE (owner, 2026-11-09: "let Google pick the
# zone. Create the speech machine through ONE regional bulk insert whose
# location policy lists the configured zones", revising the same day's zones
# in order and 2026-11-05's one zone). voice_speech.zones lists the region's
# L4 zones the bulk insert allows. A Start with no machine creates it through
# that one insert, and Google places it in a zone with a free L4; a Start of a
# stopped machine whose zone has none deletes it and inserts it the same way;
# an insert no zone could place falls back to text mode. Never two machines:
# it has one name in every zone, `<name_prefix>-speech-machine`, and
# swarm-api keeps one.
#
# A RELEASE NEVER CREATES THE MACHINE. Until 2026-11-09 this file made the
# instance, and a zone with no free L4 failed the release's whole apply. The
# instance left Terraform through the `removed` block below, with
# destroy = false: a machine a release made before stays where it is, and
# swarm-api finds it by its name.
#
# ON-DEMAND (invariant 6: Spot is off platform-wide). GPU machines cannot
# live-migrate, so a host event TERMINATEs it; automatic_restart = false keeps
# it stopped after one, never running with no session to pay for.
#
# swarm-api OWNS ITS STATE. At each start it applies Settings' machine type,
# the release's speech server digest and the models (§4.2 step 1, §5.3,
# §11.4): on a stopped machine with setMachineType and setMetadata, on a new
# one in the create itself. The template's metadata leaves the image and the
# models empty, so a new digest never re-plans anything here.
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
# grants on it (the release deployer's, to attach it to the template;
# swarm-api's, row I2, to create the machine as it). This root reads it and
# never makes it. Both roots take the account id from
# modules/service_account_ids (speech_id).
#
# OFF until both `voice_speech.enabled` and `voice_it = "on"`. Turning it on is
# an IAM change, so the release that carries it waits at the IAM gate for the
# owner.

locals {
  voice_speech_on = var.voice_speech.enabled && var.voice_it == "on"

  # The machine's identity, derived, not read, so the plan knows it: the id is
  # modules/service_account_ids' speech_id, the one terraform/bootstrap gives
  # the account.
  speech_email  = "${module.service_account_ids.speech_id}@${var.project_id}.iam.gserviceaccount.com"
  speech_member = "serviceAccount:${local.speech_email}"

  # The firewall pair's target, and the machine's only network tag.
  speech_tag = "${var.name_prefix}-speech"

  # The zones the bulk insert allows (owner, 2026-11-09). `zone`, the
  # one-zone form, is read as a one-zone list for one release; neither set is
  # the region's a, b and c.
  speech_zones = (
    length(var.voice_speech.zones) > 0 ? var.voice_speech.zones :
    var.voice_speech.zone != null ? [var.voice_speech.zone] :
    ["${var.region}-a", "${var.region}-b", "${var.region}-c"]
  )

  # The machine's name prefix (swarm-api names it `<prefix>-speech-machine`
  # in whichever zone Google placed it), and so its boot
  # disk's, which takes the instance's name; the template's name starts with
  # it too. terraform/bootstrap/deployer_conditions.tf grants the release
  # deployer its verbs on names that start with exactly this
  # (deployer_speech_machine_prefix): rename one, rename both
  # (tests/unit/terraform/test_voice_speech.py holds them equal).
  speech_machine_prefix = "${var.name_prefix}-speech-"

  speech_labels = merge(local.labels, { "component" = "voice-speech" })
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

# The machine (§5.1), as a regional template: everything a machine created
# from it needs, in whichever zone of the region it is created. Templates are
# immutable, so a change makes a new one under a new name (name_prefix) before
# the old is deleted; swarm-api reads the current one's name from
# SWARM_SPEECH_TEMPLATE, which the same apply rewrites.
resource "google_compute_region_instance_template" "speech" {
  # checkov:skip=CKV_GCP_38:A customer-supplied key is raw key material in the Terraform config and state, and no secret goes into Terraform (CLAUDE.md). The disk holds the OS, the speech server image and public model weights, never audio or tenant data; it is encrypted with Google-managed keys.
  count = local.voice_speech_on ? 1 : 0

  project      = var.project_id
  region       = var.region
  name_prefix  = local.speech_machine_prefix
  machine_type = var.voice_speech.machine_type
  description  = "Voice it's speech machine: speech-to-text and text-to-speech on one L4. swarm-api creates it from this template for a session."

  instance_description = "Voice it's speech machine: speech-to-text and text-to-speech on one L4. Kept stopped; swarm-api starts it for a session."

  # A g2 type brings its L4 with it: no guest_accelerator block.
  tags   = [local.speech_tag]
  labels = local.speech_labels

  disk {
    source_image = var.voice_speech.image
    disk_size_gb = var.voice_speech.disk_gb
    disk_type    = "pd-balanced"
    boot         = true
    auto_delete  = true
    labels       = local.speech_labels
  }

  # On swarm-vpc's subnet at the reserved address, with NO external IP (no
  # access_config): it pulls its image and the models' public weights through
  # the subnet's Cloud NAT. The address is regional, so a machine in any
  # listed zone takes it; there is one machine at a time to take it.
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
  # empty here: swarm-api writes them into the create, and rewrites them at
  # any start where they changed (§4.2 step 1, §5.3) -- the release's
  # swarm-speech digest reaches it as SWARM_SPEECH_IMAGE, never through this
  # root, so a new digest never re-plans the template.
  metadata = {
    "swarm-speech-image"        = ""
    "swarm-speech-models"       = jsonencode({})
    "swarm-speech-caller"       = module.iam.service_account_emails["swarm-api"]
    "swarm-speech-idle-minutes" = tostring(var.voice_speech.idle_minutes)
    "block-project-ssh-keys"    = "TRUE"
    "serial-port-enable"        = "FALSE"
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    data.google_service_account.speech,
    google_compute_firewall.deny_speech_other,
  ]
}

# The machine a release made before 2026-11-09 leaves Terraform's state and
# stays where it is: swarm-api finds it by its name in its zone and starts it,
# moves it or keeps it. destroy = false, so no plan ever deletes a machine.
removed {
  from = google_compute_instance.speech

  lifecycle {
    destroy = false
  }
}

# --- IAM (§6; the owner's answer 2) ------------------------------------------
#
# I1. swarm-api creates, starts, stops, reads, deletes and applies Settings to
# the speech machine, and to no other VM: swarmSpeechOperator, on the project
# but conditioned to the machine's instance and disk names in a zone of the
# region, its template, its address and its subnet. That grant is made by
# terraform/bootstrap (speech_operator_grant.tf), which the owner applies,
# NOT here: a project-level grant from this root would put the role on the
# release deployer's grantable list, and hasOnly() limits which roles CI may
# grant, never to whom or under what condition -- CI could then grant it to
# anyone unconditioned, over every VM in a shared project. Until 2026-11-09
# this root granted it on the one instance it made; that instance-level grant
# is gone with the instance (the `removed` block above).

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
    condition     = !local.voice_speech_on || alltrue([for z in local.speech_zones : startswith(z, "${var.region}-")])
    error_message = "voice_speech.zones names a zone that is not in var.region: the machine must sit on the region's subnet."
  }
}

# The one-zone form still plans, for one release (owner, 2026-11-09); this says
# what to write instead.
check "voice_speech_zone_is_replaced_by_zones" {
  assert {
    condition     = var.voice_speech.zone == null
    error_message = "voice_speech.zone is replaced by voice_speech.zones, the ordered list of the region's L4 zones; it is read as a one-zone list for this release only."
  }
}

output "voice_speech" {
  description = "Voice it's speech machine: what swarm-api needs to run it (SWARM_SPEECH_*). Names only; never a value."
  value = {
    enabled         = local.voice_speech_on
    machine_prefix  = local.voice_speech_on ? local.speech_machine_prefix : null
    template        = local.voice_speech_on ? google_compute_region_instance_template.speech[0].id : null
    zones           = local.voice_speech_on ? local.speech_zones : null
    address         = local.voice_speech_on ? google_compute_address.speech[0].address : null
    port            = 8080
    audience        = "swarm-speech"
    service_account = local.voice_speech_on ? local.speech_email : null
    idle_minutes    = var.voice_speech.idle_minutes
  }
}
