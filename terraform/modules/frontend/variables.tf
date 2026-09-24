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
  description = "The Cloud Run service serving the API. Receives /v1/* and the health paths."
  type        = string
}

variable "ui_service_name" {
  description = <<-EOT
    Cloud Run service serving the static web UI. Receives everything that is not
    an API path.

    Empty means no UI backend is created and the load balancer sends everything
    to the API -- which is how this module first shipped, and is still a valid
    configuration for an API-only deployment.
  EOT
  type        = string
  default     = ""
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

variable "project_number" {
  description = "Needed for the IAP audience, which is built from the project NUMBER rather than its id."
  type        = string
}

variable "labels" {
  type = map(string)
}
