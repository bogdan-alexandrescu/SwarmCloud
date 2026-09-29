output "key_ring_name" {
  value = local.key_ring_name
}

output "key_ring_id" {
  value = local.key_ring_id
}

output "crypto_key_name" {
  value = local.crypto_key_name
}

output "crypto_key_id" {
  description = "projects/<p>/locations/<r>/keyRings/<ring>/cryptoKeys/step-spec: the worker's SPEC_SIGNING_KEY."
  value       = local.crypto_key_id
}

output "verify_keys" {
  description = "{full version name: PEM} over the ENABLED versions in var.versions: the worker's SPEC_VERIFY_KEYS."
  value       = local.verify_keys
}
