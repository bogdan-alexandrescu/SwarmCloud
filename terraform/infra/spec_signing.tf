# Step-spec signing, the platform side (contract request 34, #342).
#
# The key is terraform/bootstrap's (spec_signing.tf there): created, and its
# one `signer` grant to swarm-api made, by the owner, so CI can never grant
# itself a signature. This root only READS the key -- its versions and their
# public keys, which the deployer may read and nothing more -- and hands:
#
#   * every worker it creates, and the scheduler (for the Jobs the scheduler
#     creates itself), the four settings the worker verifies with:
#       SPEC_VERIFY_KEYS     {full version name: PEM}, every ENABLED version
#       SPEC_SIGNING_KEY     the crypto key whose versions are trusted
#       SPEC_SIGNATURE_MODE  enforce | legacy (the rollout, below)
#       SPEC_LEGACY_CUTOVER  RFC 3339; only with legacy
#     The Job's environment IS the cache: fixed for an execution, refreshed by
#     every release, no network call, no quota and no IAM at attempt start.
#   * swarm-api the one version it signs with, SPEC_SIGNING_KEY_VERSION. An
#     asymmetric key has no primary version, so it is named in full. A
#     hardened swarm-api without it refuses to start (#353).
#   * GKE, through output.spec_verify_keys_configmap: kubernetes/render.py
#     turns it into each tenant namespace's `swarm-spec-verify-keys` ConfigMap
#     and kubernetes/apply.sh applies it in the release (owner decision
#     2026-09-29: no Kubernetes provider here). A browser pod's manifest is
#     rendered per task by the scheduler, so the map is mounted, never inlined
#     into a template a task shapes.
#
# `scheduler.dispatch.worker_env`, which a task shapes, never sets any of the
# four; a test in #353 holds both dispatchers to that.
#
# THE ROLLOUT, docs/runbooks/spec-signing-rollout.md: legacy first (unsigned
# tasks created before the cutover are admitted and logged), then enforce by
# 2026-10-20 at the latest -- the worker enforces after that date whatever
# this says (SPEC_LEGACY_UNTIL, in the code).

variable "spec_signature_mode" {
  description = <<-EOT
    SPEC_SIGNATURE_MODE on every worker: `enforce` (the default, and the
    worker's own) refuses an unsigned task; `legacy` admits an unsigned task
    whose Firestore create_time is before spec_legacy_cutover, with a WARNING
    and a RUNNING event noting it. The first release that verifies must ship
    `legacy`: every task parked at that moment is unsigned.
  EOT
  type        = string
  default     = "enforce"

  validation {
    condition     = contains(["enforce", "legacy"], var.spec_signature_mode)
    error_message = "spec_signature_mode must be enforce or legacy (agent_worker.specverify.MODES)."
  }
}

variable "spec_legacy_cutover" {
  description = <<-EOT
    SPEC_LEGACY_CUTOVER: the moment the signing swarm-api revision took all
    traffic, RFC 3339 WITH a zone. In legacy mode an unsigned task is admitted
    only if Firestore created it before this. Required with legacy -- a legacy
    worker with no cutover admits no unsigned task at all -- and omitted from
    the workers' environment in enforce.
  EOT
  type        = string
  default     = ""

  validation {
    condition = var.spec_legacy_cutover == "" || can(regex(
      "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$",
      var.spec_legacy_cutover,
    ))
    error_message = "spec_legacy_cutover must be an RFC 3339 time with a zone, e.g. 2026-10-01T12:00:00Z; the worker refuses one without."
  }

  validation {
    condition     = var.spec_signature_mode != "legacy" || var.spec_legacy_cutover != ""
    error_message = "spec_signature_mode = \"legacy\" needs spec_legacy_cutover: without it the worker admits no unsigned task, and every task parked before the signing release fails spec_signature_invalid."
  }
}

variable "spec_signing_key_version" {
  description = <<-EOT
    The step-spec key version swarm-api signs with, by NUMBER; rendered as the
    full version name SPEC_SIGNING_KEY_VERSION. 1 is the version Cloud KMS
    creates with the key (terraform/bootstrap). Rotation moves it only after a
    release has put the new version in every worker's SPEC_VERIFY_KEYS
    (docs/runbooks/spec-signing-rollout.md, "Rotation").
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.spec_signing_key_version >= 1 && floor(var.spec_signing_key_version) == var.spec_signing_key_version
    error_message = "spec_signing_key_version is a key version number: a whole number from 1."
  }
}

module "spec_signing_key" {
  source = "../modules/spec_signing_key"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  environment = var.environment
  versions    = data.google_kms_crypto_key_versions.step_spec.versions
}

# Read at plan, by the deployer, with the two reader roles bootstrap grants it
# on this key (publicKeyViewer, viewer). THE KEY MUST EXIST FIRST: a plan of
# this root before the owner has applied bootstrap's spec_signing.tf fails here.
#
# The module's `crypto_key_id` depends only on the names, not on `versions`,
# so reading it here and feeding the result back in is not a cycle.
data "google_kms_crypto_key_versions" "step_spec" {
  crypto_key = module.spec_signing_key.crypto_key_id
  filter     = "state=ENABLED"
}

locals {
  spec_verify_keys = module.spec_signing_key.verify_keys
  spec_signing_key = module.spec_signing_key.crypto_key_id

  spec_signing_key_version = "${local.spec_signing_key}/cryptoKeyVersions/${var.spec_signing_key_version}"

  # What every worker Job and the scheduler carry. SPEC_LEGACY_CUTOVER only
  # when set: a worker reads an absent one and an empty one alike, but a Job
  # with no such variable says so.
  spec_worker_env = merge(
    {
      SPEC_VERIFY_KEYS    = jsonencode(local.spec_verify_keys)
      SPEC_SIGNING_KEY    = local.spec_signing_key
      SPEC_SIGNATURE_MODE = var.spec_signature_mode
    },
    var.spec_signature_mode == "legacy" && var.spec_legacy_cutover != "" ? { SPEC_LEGACY_CUTOVER = var.spec_legacy_cutover } : {},
  )
}

# swarm-api signs with a version every worker trusts, or every submission is a
# task no worker will run. A check, not a precondition: it warns on the plan
# rather than blocking a release whose other changes are urgent.
check "spec_signing_version_is_trusted" {
  assert {
    condition     = contains(keys(local.spec_verify_keys), local.spec_signing_key_version)
    error_message = "spec_signing_key_version names a version that is not ENABLED on the step-spec key, so no worker would verify what swarm-api signs. Enable it, or point spec_signing_key_version at an enabled one."
  }
}

output "spec_verify_keys" {
  description = "{full version name: PEM} over the step-spec key's ENABLED versions: SPEC_VERIFY_KEYS on every worker."
  value       = local.spec_verify_keys
}

output "spec_verify_keys_configmap" {
  description = "The data of each tenant namespace's swarm-spec-verify-keys ConfigMap: `terraform output -json spec_verify_keys_configmap` is what kubernetes/render.py --spec-verify-keys-file reads. Public keys, not secrets."
  value       = local.spec_worker_env
}

output "spec_signing_key_version" {
  description = "The full key version swarm-api signs with (SPEC_SIGNING_KEY_VERSION)."
  value       = local.spec_signing_key_version
}

output "job_spec_env" {
  description = "Worker job -> the four step-spec settings its environment carries (null where absent), as its module entry carries them."
  value       = module.cloud_run_jobs.spec_env
}

output "spec_service_env" {
  description = "swarm-api and swarm-scheduler -> the SPEC_* variables their environment carries."
  value = {
    for svc in ["swarm-api", "swarm-scheduler"] :
    svc => { for k, v in local.service_env[svc] : k => v if startswith(k, "SPEC_") }
  }
}
