# ---------------------------------------------------------------------------
# The platform's service accounts (DAYONE-BOOT-2; design §5.3, Q7)
# ---------------------------------------------------------------------------
#
# WHY HERE AND NOT IN terraform/infra. Bootstrap grants roles to these
# accounts -- swarm-api's cloudkms.signer (spec_signing.tf), swarm-verify's
# clusterViewer (verify_cluster_read.tf), the broker's swarmSecretLister
# (platform_roles.tf), the deployer's per-account serviceAccountAdmin
# (deployer_service_accounts.tf). While terraform/infra created them, a fresh
# project's first bootstrap apply granted on accounts that did not exist yet
# and needed "later" switches to skip those grants until infra's first apply.
# Created here, every grant can be made in the same apply as its account.
# terraform/infra reads them as data sources (modules/iam, modules/scheduler,
# infra/verify.tf) and declares none of them.
#
# THE LIVE DEPLOYMENT MOVES WITH ZERO DESTROYS (Q7). The custom-roles move again
# (platform_roles.tf): terraform/infra/removed.tf forgets each account with
# `destroy = false`, and the `import` blocks below adopt them into this root
# while adopt_from_infra_states is set. The migration changes no account's email
# or unique id, so no grant, Workload Identity binding or RBAC subject moves
# (lesson 29). Every argument below is the value terraform/infra gave the
# account, word for word, so the adopting plan shows "import" and no update.
#
# Import id, from the provider's documentation (hashicorp/google 6.50.0, the
# version .terraform.lock.hcl pins): `projects/{{project}}/serviceAccounts/{{email}}`.
# Once an account is in this root's state, Terraform skips an import block
# whose target it already manages, so these stay harmless after the adoption.
#
# NO create_ignore_already_exists (#334 security review): an apply that meets
# a 409 on one of these must fail rather than adopt an account somebody else
# made under the name, with their IAM policy and keys. The only adoption is
# the explicit, gated import below.
#
# There are no service account KEYS anywhere in this configuration. Cloud Run
# and GKE attach identity to the workload.

locals {
  platform_sa_email = {
    for id in module.service_account_ids.bootstrap_platform : id => "${id}@${var.project_id}.iam.gserviceaccount.com"
  }
}

# One identity per component, never a shared one: the reconciler is the only
# thing allowed to DELETE infrastructure, and that can be enforced only if its
# permissions are attached to an identity nothing else runs as.
resource "google_service_account" "platform" {
  for_each = module.service_account_ids.platform

  project      = var.project_id
  account_id   = each.key
  display_name = each.value.display_name
  description  = "managed-by=swarm-terraform; ${each.value.description}"
}

# Cloud Scheduler and Pub/Sub push present this identity when they call the
# control plane. It holds NO project role: its entire authority is run.invoker
# on two specific services, granted at the service resource by terraform/infra.
resource "google_service_account" "tick" {
  project      = var.project_id
  account_id   = module.service_account_ids.tick_id
  display_name = "Swarm Tick Invoker"
  description  = "managed-by=swarm-terraform; OIDC identity for Cloud Scheduler and Pub/Sub push. No project roles."
}

# The in-VPC verification job's identity (terraform/infra/verify.tf). 256
# characters at most -- the provider refuses a longer description at plan.
resource "google_service_account" "verify" {
  project      = var.project_id
  account_id   = module.service_account_ids.verify_id
  display_name = "SwarmCloud verification job"
  description  = "Runs the verification suites inside the VPC. Reads Firestore, Cloud Run executions, GKE Jobs and artifact objects. Writes only through swarm-api: as its own tenant, and runner ceilings where admin_pool_users names it (for race-test). Not a platform admin."
}

# The identity the one schedule tick presents to swarm-api (docs/schedules.md
# SD10); run.invoker on swarm-api, granted by terraform/infra's modules/scheduler.
resource "google_service_account" "schedule_tick" {
  project      = var.project_id
  account_id   = module.service_account_ids.schedule_tick_id
  display_name = "Swarm Schedule Tick"
  description  = "managed-by=swarm-terraform; OIDC identity of the schedule tick. swarm-api admits it to POST /v1/admin/schedules/tick only. run.invoker on swarm-api; no project roles."
}

# The identity the per-tenant workflow-rollup jobs and the other sweeps present
# to swarm-api (modules/scheduler jobs.tf, D17). The description is at 252 of
# the provider's 256 characters.
resource "google_service_account" "rollup_sweeper" {
  project      = var.project_id
  account_id   = module.service_account_ids.rollup_sweeper_id
  display_name = "Swarm Workflow Rollup Sweeper"
  description  = "managed-by=swarm-terraform; OIDC id of workflow-rollup, issue-run-advance, stranded-pr-sweep, repo-index-poll, merge-wake, forge-refresh, workspace-sweep, secrets-rotation-sweep, mcp-credentials-sweep, task_finished push; 10 /v1/admin routes. No roles."
}

# NEW (no root held it before BOOT-2, so nothing imports it). swarm-discovery
# serves the unauthenticated /.well-known/swarmcloud document behind the load
# balancer (terraform/infra/discovery.tf, design §9.2 (c)). No role at all,
# unless the deployment serves the Desktop client secret through it.
resource "google_service_account" "discovery" {
  project      = var.project_id
  account_id   = module.service_account_ids.discovery_id
  display_name = "Swarm Discovery"
  description  = "managed-by=swarm-terraform; serves /.well-known/swarmcloud (non-secret discovery document). No project roles; secretAccessor on the Desktop client secret only when discovery_serves_client_secret is set."
}

# NEW. swarm-doctor runs the hourly read-only doctor (terraform/infra/doctor.tf,
# D27): read-only project roles (doctor_roles.tf), run.invoker on swarm-api to
# post its findings, nothing that writes.
resource "google_service_account" "doctor" {
  project      = var.project_id
  account_id   = module.service_account_ids.doctor_id
  display_name = "Swarm Doctor"
  description  = "managed-by=swarm-terraform; the scheduled read-only doctor. Read-only project roles (viewer, securityReviewer, secretmanager.viewer, clusterViewer on the swarm cluster); posts findings to swarm-api. Writes nothing."
}

# NEW (2026-11-01). swarm-voice runs the Voice it StatefulSet on GKE through
# Workload Identity (docs/design/voice-it.md section 3.4; terraform/infra/voice.tf).
# Made here, holding no role, so deployer_service_accounts.tf's grant on it is
# made in the same apply; voice.tf reads it as a data source and grants it its
# own roles. No import block: terraform/infra never created it.
resource "google_service_account" "voice" {
  project      = var.project_id
  account_id   = module.service_account_ids.voice_id
  display_name = "Swarm Voice"
  description  = "managed-by=swarm-terraform; the swarm-voice StatefulSet's Workload Identity account (Voice it). No project roles; terraform/infra/voice.tf grants its own."
}

# NEW (2026-11-05). swarm-speech is the account Voice it's speech machine runs
# as (docs/design/voice-it-own-speech.md §5.1 and §6; terraform/infra/
# voice_speech.tf, off until voice_speech.enabled). Made here, holding no
# role: voice_speech.tf grants its three (artifactregistry.reader on the
# platform's repository, logging.logWriter, monitoring.metricWriter) when the
# machine is on. No import block: no root ever made it.
#
# Its id is spelled here and in voice_speech.tf's local.speech_email rather
# than in modules/service_account_ids, which this lane's territory does not
# include; the pull request asks for the move. Being off that module's list,
# it gets no serviceAccountAdmin for the deployer (deployer_service_accounts.tf)
# and is not on deployer.tf's actAs list; the one grant the deployer needs on
# it, actAs to attach it to the machine, is made below instead.
resource "google_service_account" "speech" {
  project      = var.project_id
  account_id   = "swarm-speech"
  display_name = "Swarm Speech"
  description  = "managed-by=swarm-terraform; Voice it's speech machine (one stopped L4 VM). No project roles here; terraform/infra/voice_speech.tf grants its image read, log write and metric write."
}

# Row I1's role: what swarm-api may do to the speech machine, granted on that
# one instance by voice_speech.tf (google_compute_instance_iam_member), never
# on the project. Start and stop for a session; get to poll RUNNING and
# TERMINATED; setMachineType and setMetadata to apply Settings and a new
# server digest while it is stopped (§4.2 step 1, §11.4); getSerialPortOutput
# to read a failed boot. No create, no delete, no setIamPolicy, no disk or
# network permission. Defined here because terraform/infra may not define or
# read a role (#79).
resource "google_project_iam_custom_role" "speech_operator" {
  project = var.project_id
  role_id = "swarmSpeechOperator"
  title   = "Swarm Speech Operator"

  description = "managed-by=swarm-terraform; swarm-api starts, stops, reads and applies settings to Voice it's speech machine. Granted on that instance only."
  stage       = "GA"

  permissions = [
    "compute.instances.get",
    "compute.instances.getSerialPortOutput",
    "compute.instances.setMachineType",
    "compute.instances.setMetadata",
    "compute.instances.start",
    "compute.instances.stop",
  ]
}

# The release deployer attaches swarm-speech to the machine it creates, which
# needs actAs on the account (deployer.tf says why for every other account).
# Granted on this account only, never project-wide.
resource "google_service_account_iam_member" "deployer_acts_as_speech" {
  count = local.deployer_on ? 1 : 0

  service_account_id = google_service_account.speech.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer[0].email}"
}

# Row I2: swarm-api acts as swarm-speech. Compute checks actAs on the attached
# account for setMetadata on a machine that runs as one (a metadata write can
# run code as that account); swarm-api writes the image, models and idle
# minutes there at each start. The account holds only its three write-or-read
# grants, so this reaches no more than those. If the machine lane's
# measurement M3 shows start and setMetadata pass without it, the row is
# dropped.
resource "google_service_account_iam_member" "api_acts_as_speech" {
  service_account_id = google_service_account.speech.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.platform_sa_email["swarm-api"]}"

  depends_on = [google_service_account.platform]
}

# ---------------------------------------------------------------------------
# The adoption from terraform/infra (live deployment only).
# ---------------------------------------------------------------------------

import {
  for_each = length(var.adopt_from_infra_states) > 0 ? module.service_account_ids.platform : tomap({})

  to = google_service_account.platform[each.key]
  id = "projects/${var.project_id}/serviceAccounts/${local.platform_sa_email[each.key]}"
}

import {
  for_each = length(var.adopt_from_infra_states) > 0 ? toset([module.service_account_ids.tick_id]) : toset([])

  to = google_service_account.tick
  id = "projects/${var.project_id}/serviceAccounts/${local.platform_sa_email[each.key]}"
}

import {
  for_each = length(var.adopt_from_infra_states) > 0 ? toset([module.service_account_ids.verify_id]) : toset([])

  to = google_service_account.verify
  id = "projects/${var.project_id}/serviceAccounts/${local.platform_sa_email[each.key]}"
}

import {
  for_each = length(var.adopt_from_infra_states) > 0 ? toset([module.service_account_ids.schedule_tick_id]) : toset([])

  to = google_service_account.schedule_tick
  id = "projects/${var.project_id}/serviceAccounts/${local.platform_sa_email[each.key]}"
}

import {
  for_each = length(var.adopt_from_infra_states) > 0 ? toset([module.service_account_ids.rollup_sweeper_id]) : toset([])

  to = google_service_account.rollup_sweeper
  id = "projects/${var.project_id}/serviceAccounts/${local.platform_sa_email[each.key]}"
}
