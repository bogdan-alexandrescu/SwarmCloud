# ---------------------------------------------------------------------------
# swarm-api operates Voice it's speech machine -- and no other VM
# (docs/design/voice-it-own-speech.md §5 and §6 row I1; owner, 2026-11-09).
# ---------------------------------------------------------------------------
#
# WHY A PROJECT GRANT. The owner's revision of 2026-11-09: "let Google pick
# the zone". swarm-api creates the speech machine from its regional template
# through one regional bulk insert (regionInstances.bulkInsert, checked as
# instances.create) whose location policy lists voice_speech.zones, moves a
# stopped one out of a zone that has none (delete, then the same insert), and
# keeps one machine.
# Until then terraform/infra made the one instance and granted swarm-api
# swarmSpeechOperator on it; an instance that does not exist yet has no IAM
# policy to grant on, so the grant is on the project, CONDITIONED to what the
# machine is.
#
# WHY HERE AND NOT IN terraform/infra (the verify_cluster_read.tf rule). THE
# OWNER APPLIES THIS (`make bootstrap`); CI never does. terraform/infra is
# applied by CI's release deployer, whose scoped projectIamAdmin may modify
# only the roles on deployer_grantable_project_roles (deployer_conditions.tf).
# hasOnly() limits WHICH roles a policy change touches, never to whom or under
# what condition, so a project grant from terraform/infra would put
# swarmSpeechOperator on that list -- and any CI run could then grant it to
# itself or anyone, unconditioned, with instances.create, .delete and .stop on
# every VM in a shared project. Granted from here, it stays off CI's list:
# tests/terraform/deployer_iam.tftest.hcl holds that it is not on it.
#
# THE CONDITION. The release deployer's speech-machine condition, the same
# extract() per kind over deployer_speech_machine_paths (linted 2026-11-05,
# deployer_conditions.tf says how), so an instance or a disk must sit in a zone
# of var.region and be named `<name_prefix>-speech-...`; plus the three other
# resources a create from the template is checked on:
#
#   the template          startsWith(.../regions/<region>/instanceTemplates/<prefix>)
#                         (deployer_speech_template_prefix: the deployer's)
#   the reserved address  == .../regions/<region>/addresses/<name_prefix>-speech
#   the subnet            == .../regions/<region>/subnetworks/<name_prefix>-subnet-<region>
#
# The address and the subnet are named exactly (terraform/infra's
# google_compute_address.speech and modules/network's subnetwork;
# tests/unit/terraform/test_voice_speech.py holds the spellings equal). NO TYPE
# GUARD: every permission in swarmSpeechOperator is checked on one of these
# five kinds, so a request on any other resource is meant to be refused. Their
# GKE nodes, the shared project's other VMs, disks, templates, addresses and
# subnets are all refused; tests/terraform/deployer_iam.tftest.hcl replays
# each. 4 operators, inside IAM's 12.
#
# WHAT IS NOT MEASURED, the same as the deployer's: that Compute evaluates
# resource.name on instances.create against the instance being created. If it
# is wrong, the first Start after this is applied falls back to text with
# `not_ready`, and the session's Compute error names the 403.
#
# NOT GATED ON voice_speech. This root does not know it, as it does not for the
# account, the role or row I2 above them. Off, there is no template and no
# machine, so the grant reaches nothing.
locals {
  speech_operator_condition = join(" || ", concat(
    [
      for p in local.deployer_speech_machine_paths :
      "resource.name.extract(\"${p.zone_prefix}{zone}${p.name_prefix}\") != \"\""
    ],
    [
      "resource.name.startsWith(\"${local.deployer_speech_template_prefix}\")",
      "resource.name == \"projects/${var.project_id}/regions/${var.region}/addresses/${var.name_prefix}-speech\"",
      "resource.name == \"projects/${var.project_id}/regions/${var.region}/subnetworks/${var.name_prefix}-subnet-${var.region}\"",
    ],
  ))
}

resource "google_project_iam_member" "api_speech_operator" {
  project = var.project_id
  role    = google_project_iam_custom_role.speech_operator.name
  member  = "serviceAccount:${local.platform_sa_email["swarm-api"]}"

  condition {
    title       = "the speech machine only"
    description = "Voice it's speech machine, <name_prefix>-speech-machine in a zone of the platform's region, its boot disk, its template, its address and the subnet it is created on. Every other VM and disk is refused."
    expression  = local.speech_operator_condition
  }

  depends_on = [google_service_account.platform]
}

# The expression to lint with iamPolicies:lintPolicy before the bootstrap apply
# that adds it (docs/design/day-one/lessons.md, lesson 10). A string, never a
# value of anyone's.
output "speech_operator_condition" {
  description = "swarm-api's condition on the speech operator role: lint it with iamPolicies:lintPolicy before applying."
  value       = local.speech_operator_condition
}
