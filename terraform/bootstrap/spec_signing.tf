# The key that signs every step spec (contract request 34, #342).
#
# swarm-api signs each step's canonical spec with this key at submission, and
# every worker verifies the signature before it reads anything the spec
# decides. A tenant's agent can write its own task documents (it has to, to
# report progress), so without a signature a step parked behind an earlier one
# runs whatever that earlier agent rewrote its prompt, repository or inputs to.
#
# WHY HERE, AND NOT IN terraform/infra (the entry's decision 4). The key's IAM
# policy decides who can produce a valid signature. terraform/infra is applied
# by CI; if the key lived there, the CI deployer would hold setIamPolicy on it
# and the release pipeline could grant itself `signer`. Here the owner applies
# it, so CI never can. What CI holds on the key is what reading the public keys
# at plan needs (deployer_key_readers, below), and neither role can sign or
# change IAM.
#
# WHAT THIS DOES NOT PROTECT AGAINST, accepted by the owner 2026-09-29: the key
# lives in the shared saga-agents-staging project, so a project Owner or Editor
# can redeploy swarm-api's service to run code of their choosing as its account,
# and sign through it, without touching this policy. It does hold against
# tenant agents -- no tenant or worker account holds any role on this key.
#
# NEITHER A KEY RING NOR A KEY CAN BE DELETED in Cloud KMS. `terraform destroy`
# forgets the ring and schedules the key's versions for destruction (after
# destroy_scheduled_duration), and both names stay taken forever. So:
#   * prevent_destroy is set on the key: a destroyed version makes every task it
#     signed unverifiable, and there is no undo once the schedule runs out;
#   * re-creating after a destroy collides on the names. The recreate path is
#     an `import` block per object, e.g.
#         import {
#           to = google_kms_key_ring.step_spec["dev"]
#           id = "projects/<project>/locations/us-central1/keyRings/swarm-dev-specs"
#         }
#     and `gcloud kms keys versions restore` for any version still scheduled.

variable "spec_signing_environments" {
  description = <<-EOT
    The environments that get a step-spec key ring and key: one ring per
    environment, because the canonical form carries no environment and a key
    shared across environments would let a spec signed by dev's swarm-api
    verify in prod's worker. Every environment terraform/infra is planned for
    must be here, or that plan fails reading a key that does not exist.
  EOT
  type        = set(string)
  default     = ["dev", "prod"]

  validation {
    condition     = alltrue([for e in var.spec_signing_environments : can(regex("^[a-z][a-z0-9-]{0,19}$", e))])
    error_message = "each environment must be a short lowercase name: letters, digits and hyphens."
  }
}

module "spec_signing_key" {
  source   = "../modules/spec_signing_key"
  for_each = var.spec_signing_environments

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  environment = each.key
}

locals {
  # The one identity that signs, taken from the list of accounts terraform/infra
  # creates rather than typed, so a rename there fails here at plan instead of
  # granting `signer` to an account that does not exist.
  spec_signer_account_id = one([for id in keys(module.service_account_ids.platform) : id if id == "swarm-api"])
  spec_signer_member     = "serviceAccount:${local.spec_signer_account_id}@${var.project_id}.iam.gserviceaccount.com"

  # What the deployer needs to READ the key at terraform/infra's plan
  # (google_kms_crypto_key_versions there): viewer lists the versions and their
  # states, publicKeyViewer returns each version's public key. Neither can sign,
  # verify through KMS, or change the key's IAM policy.
  spec_deployer_reader_roles = ["roles/cloudkms.publicKeyViewer", "roles/cloudkms.viewer"]

  spec_deployer_reader_grants = var.enable_github_wif ? {
    for pair in setproduct(sort(tolist(var.spec_signing_environments)), local.spec_deployer_reader_roles) :
    "${pair[0]} ${pair[1]}" => { environment = pair[0], role = pair[1] }
  } : {}
}

resource "google_kms_key_ring" "step_spec" {
  for_each = var.spec_signing_environments

  project  = var.project_id
  name     = module.spec_signing_key[each.key].key_ring_name
  location = var.region

  # A key ring has no labels, in Cloud KMS or in the provider, so it cannot
  # carry managed-by=swarm-terraform; it is in scripts/lib/unlabelable-types.json.

  depends_on = [module.project_services]
}

resource "google_kms_crypto_key" "step_spec" {
  for_each = var.spec_signing_environments

  name = module.spec_signing_key[each.key].crypto_key_name
  # The same string the ring's id is, from the shared module, so the name is
  # known at plan; depends_on keeps the ring first.
  key_ring = module.spec_signing_key[each.key].key_ring_id
  purpose  = "ASYMMETRIC_SIGN"

  version_template {
    # P-256: fast to verify, and the signature is ~70 bytes of DER in every
    # task document. The worker verifies exactly this (ECDSA, SHA-256,
    # agent_worker.specverify); another algorithm is a key no worker can use.
    algorithm = "EC_SIGN_P256_SHA256"
    # SOFTWARE, not HSM (the entry's decision 10): the private key never leaves
    # Cloud KMS either way, and the threat is who may call AsymmetricSign,
    # which IAM decides.
    protection_level = "SOFTWARE"
  }

  # No rotation_period: Cloud KMS does not rotate asymmetric keys, and rotation
  # here is by hand, in the order docs/runbooks/spec-signing-rollout.md gives.

  labels = {
    "managed-by"        = "swarm-terraform"
    "swarm-root"        = "bootstrap"
    "swarm-environment" = each.key
    "swarm-purpose"     = "step-spec-signing"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_kms_key_ring.step_spec]
}

# THE ONE IDENTITY THAT CAN SIGN: swarm-api, `signer` (useToSign only), ON THE
# KEY. Not signerVerifier -- swarm-api never verifies or reads the public key.
# Not on the ring or the project -- a project grant would reach every key in a
# project shared with another team.
resource "google_kms_crypto_key_iam_member" "swarm_api_signer" {
  for_each = var.spec_signing_environments

  crypto_key_id = module.spec_signing_key[each.key].crypto_key_id
  role          = "roles/cloudkms.signer"
  member        = local.spec_signer_member

  depends_on = [google_kms_crypto_key.step_spec]
}

resource "google_kms_crypto_key_iam_member" "deployer_key_readers" {
  for_each = local.spec_deployer_reader_grants

  crypto_key_id = module.spec_signing_key[each.value.environment].crypto_key_id
  role          = each.value.role
  member        = "serviceAccount:${google_service_account.deployer[0].email}"

  depends_on = [google_kms_crypto_key.step_spec]
}

# The public keys, for the owner to read after an apply: the same
# {full version name: PEM} map terraform/infra renders onto the workers, built
# by the same module from the same data source. Public keys, not secrets.
data "google_kms_crypto_key_versions" "step_spec" {
  for_each = var.spec_signing_environments

  crypto_key = module.spec_signing_key[each.key].crypto_key_id
  filter     = "state=ENABLED"

  # Read after the key exists: on the first apply there is nothing to list yet.
  depends_on = [google_kms_crypto_key.step_spec]
}

module "spec_verify_keys" {
  source   = "../modules/spec_signing_key"
  for_each = var.spec_signing_environments

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  environment = each.key
  versions    = data.google_kms_crypto_key_versions.step_spec[each.key].versions
}

output "spec_signing_keys" {
  description = "environment -> the step-spec crypto key's full name (SPEC_SIGNING_KEY)."
  value       = { for env, m in module.spec_signing_key : env => m.crypto_key_id }
}

output "spec_verify_keys" {
  description = "environment -> {full version name: PEM} over the key's ENABLED versions: what terraform/infra renders as SPEC_VERIFY_KEYS."
  value       = { for env, m in module.spec_verify_keys : env => m.verify_keys }
}
