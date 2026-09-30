# The step-spec signing key's names, spelled once for both roots (contract
# request 34, #342).
#
# terraform/bootstrap CREATES the key ring and the key, and the owner applies
# it. terraform/infra READS the key's versions and renders the public keys onto
# every worker, and CI applies it. The two roots share no state, so the only
# thing that keeps "the key bootstrap made" and "the key infra trusts" the same
# key is that both derive its name here -- the pattern modules/service_account_ids
# set for the service accounts (docs/mirrored-values.md, answer (a): derived,
# nothing to compare).
#
# The names: plain strings, no resource and no data source, so both roots know
# them at plan.

locals {
  key_ring_name   = "${var.name_prefix}-${var.environment}-specs"
  key_ring_id     = "projects/${var.project_id}/locations/${var.region}/keyRings/${local.key_ring_name}"
  crypto_key_name = "step-spec"
  crypto_key_id   = "${local.key_ring_id}/cryptoKeys/${local.crypto_key_name}"
}

# THE PUBLIC KEYS. Not from var.versions[*].public_key -- that field is never
# populated, on any entry (contract request 34, #354's dev apply: run
# 36655830725, local.spec_verify_keys empty although version 1 was ENABLED).
#
# EVIDENCE, provider v6.50.0, google/services/kms/data_source_google_kms_crypto_key_versions.go:
#   * flattenKMSCryptoKeyVersionsList (the function that builds each entry of
#     the `versions` list) sets id/name/crypto_key/version/state/
#     protection_level/algorithm and NEVER public_key.
#   * dataSourceGoogleKmsCryptoKeyVersionsRead sets `public_key` exactly once,
#     as the DATA SOURCE'S OWN top-level attribute -- not on any entry of
#     `versions` -- fetched for `versions.0.version` only.
# So var.versions[*].public_key is the empty list Terraform gives an unset
# computed list on every entry, whatever the filter argument returns; the
# state=ENABLED filter itself is valid (it is Google's own documented example
# at https://cloud.google.com/kms/docs/sorting-and-filtering), and this is not
# an IAM-propagation gap either -- both roots' deployer already holds
# cloudkms.viewer and publicKeyViewer on the key.
#
# THE FIX: google_kms_crypto_key_version (singular), which DOES carry its own
# public_key (data_source_google_kms_crypto_key_version.go,
# dataSourceGoogleKmsCryptoKeyVersionRead), read once per ENABLED version.
# Living here, in the module both roots share, so the fix applies to
# bootstrap's and infra's callers alike without duplicating it in both.
data "google_kms_crypto_key_version" "enabled" {
  for_each = { for v in var.versions : tostring(v.version) => v if v.state == "ENABLED" }

  crypto_key = local.crypto_key_id
  version    = each.value.version
}

locals {
  # {full version name: PEM}, the shape the worker parses
  # (agent_worker.specverify.parse_verify_keys) and checks a task's
  # `spec_key_version` against: `<crypto key>/cryptoKeyVersions/<n>`. Built
  # from the version NUMBER rather than read off a data source's `name`/`id`,
  # so the prefix the worker compares is exactly SPEC_SIGNING_KEY whatever
  # form the provider gives those two in.
  #
  # ENABLED ONLY, already guaranteed by the for_each above -- disabling a
  # version is how a leaked or retired version is revoked, and from the next
  # release on it must be absent here.
  verify_keys = {
    for k, v in data.google_kms_crypto_key_version.enabled :
    "${local.crypto_key_id}/cryptoKeyVersions/${v.version}" => v.public_key[0].pem
    if length(v.public_key) > 0
  }
}
