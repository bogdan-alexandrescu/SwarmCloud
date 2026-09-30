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
    returns them: version number and state. Empty when the caller only needs
    the names. Deliberately NOT the data source's public_key: that field is
    never populated per entry (main.tf explains, with the provider source
    lines) -- the module reads each ENABLED version's own public key itself,
    from google_kms_crypto_key_version, once per version.
  EOT
  type = list(object({
    version = number
    state   = string
  }))
  default = []
}
