variable "project_id" {
  type = string
}

variable "region" {
  description = "Where the key ring lives: the platform's region. A key ring's location is fixed at creation."
  type        = string
}

variable "name_prefix" {
  type = string
}

variable "environment" {
  description = "The environment whose key this is. One ring per environment: the canonical form carries no environment, so a shared key would let dev's specs verify in prod."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}$", var.environment))
    error_message = "environment must be a short lowercase name: letters, digits and hyphens, starting with a letter."
  }
}

variable "versions" {
  description = <<-EOT
    The key's versions as the google_kms_crypto_key_versions data source
    returns them. Empty when the caller only needs the names. The filtering
    lives here, once, so every root that renders public keys trusts the same
    set: ENABLED versions that carry a public key, and nothing else.
  EOT
  type = list(object({
    version    = number
    state      = string
    public_key = list(object({ pem = string }))
  }))
  default = []
}
