variable "project_id" {
  type = string
}

variable "region" {
  description = "Region of the Cloud Run service this fronts. The serverless NEG must live in the same one."
  type        = string
}

variable "name_prefix" {
  type = string
}

variable "service_name" {
  description = "The Cloud Run service to put behind the load balancer."
  type        = string
}

variable "hostname" {
  description = <<-EOT
    The name the certificate is issued for and the browser connects to.

    DNS for it is NOT managed here -- saga.xyz lives at an external registrar --
    so terraform reserves the address and outputs it, and an operator adds the A
    record by hand. Until that record resolves, the managed certificate stays in
    PROVISIONING and the site serves a TLS error. That is expected and can take
    up to an hour; see the module header.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.hostname))
    error_message = "hostname must be a fully qualified domain name, lowercase, e.g. swarm.saga.xyz."
  }
}

variable "iap_members" {
  description = <<-EOT
    Identities allowed past IAP. This is the OUTER gate only.

    It is deliberately a coarse grant. The real tenant boundary is swarm-api:
    it verifies the Google ID token, enforces ALLOWED_DOMAINS, resolves Cloud
    Identity group membership and scopes every read to the caller's own tenant
    in the store (CONTRACT.md invariant 9). IAP decides who may knock.

    A list of individual users is discouraged for the reason this repository
    learned the hard way on 2026-09-19: a hand-maintained allowlist of
    principals rots, and the failure mode is locking out the person it exists to
    admit, silently.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.iap_members) > 0
    error_message = "iap_members may not be empty: an IAP-protected backend with no members is unreachable by everyone, which reads as an outage rather than a configuration mistake."
  }

  validation {
    condition     = alltrue([for m in var.iap_members : can(regex("^(user|group|domain|serviceAccount):", m))])
    error_message = "every IAP member must be a fully qualified IAM member, e.g. domain:saga.xyz or group:eng@saga.xyz."
  }

  validation {
    condition     = !contains(var.iap_members, "allUsers") && !contains(var.iap_members, "allAuthenticatedUsers")
    error_message = "allUsers and allAuthenticatedUsers defeat IAP entirely: allAuthenticatedUsers means ANY Google account on the internet, not any account in your organisation."
  }
}

variable "labels" {
  type = map(string)
}
