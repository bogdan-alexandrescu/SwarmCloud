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
# Plain strings, no resource and no data source, so both roots know them at plan.

locals {
  key_ring_name   = "${var.name_prefix}-${var.environment}-specs"
  key_ring_id     = "projects/${var.project_id}/locations/${var.region}/keyRings/${local.key_ring_name}"
  crypto_key_name = "step-spec"
  crypto_key_id   = "${local.key_ring_id}/cryptoKeys/${local.crypto_key_name}"

  # {full version name: PEM}, the shape the worker parses
  # (agent_worker.specverify.parse_verify_keys) and checks a task's
  # `spec_key_version` against: `<crypto key>/cryptoKeyVersions/<n>`. Built
  # here from the version NUMBER rather than read off the data source's
  # `name`/`id`, so the prefix the worker compares is exactly SPEC_SIGNING_KEY
  # whatever form the provider gives those two in.
  #
  # ENABLED ONLY. Disabling a version is how a leaked or retired version is
  # revoked (the entry's "Revocation"): from the next release on it is absent
  # here, and every task signed by it is refused. terraform/infra also asks the
  # API for ENABLED versions only; this filter is what holds if that changes.
  verify_keys = {
    for v in var.versions :
    "${local.crypto_key_id}/cryptoKeyVersions/${v.version}" => v.public_key[0].pem
    if v.state == "ENABLED" && length(v.public_key) > 0
  }
}
