variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "network" {
  description = "Swarm VPC self link, for Direct VPC egress."
  type        = string
}

variable "subnetwork" {
  type = string
}

variable "vpc_egress" {
  description = "PRIVATE_RANGES_ONLY keeps googleapis traffic on Google's backbone; control-plane services need nothing else."
  type        = string
  default     = "PRIVATE_RANGES_ONLY"

  validation {
    condition     = contains(["PRIVATE_RANGES_ONLY", "ALL_TRAFFIC"], var.vpc_egress)
    error_message = "vpc_egress must be PRIVATE_RANGES_ONLY or ALL_TRAFFIC."
  }
}

variable "ingress" {
  description = <<-EOT
    INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER is Cloud Run's
    "internal and Cloud Load Balancing" setting. It also admits same-project
    Pub/Sub push and Cloud Scheduler, which is how the scheduler is woken.
  EOT
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  validation {
    condition     = var.ingress != "INGRESS_TRAFFIC_ALL"
    error_message = "INGRESS_TRAFFIC_ALL would expose the control plane to the internet."
  }
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "services" {
  description = <<-EOT
    The four control-plane services. Every one runs as its own service account:
    swarm-api authenticates callers, the scheduler admits work, the quota broker
    owns provider state, and the reconciler is the only component allowed to
    delete anything.
  EOT
  type = map(object({
    service_account_email = string
    image                 = string
    max_instances         = number
    cpu                   = optional(string, "1")
    memory                = optional(string, "512Mi")
    concurrency           = optional(number, 80)
    request_timeout       = optional(string, "300s")
    env                   = optional(map(string), {})
    invokers              = optional(list(string), [])
    health_check_path     = optional(string, "/healthz")
    container_port        = optional(number, 8080)
  }))

  validation {
    condition     = alltrue([for k, v in var.services : startswith(k, "swarm-")])
    error_message = "every Cloud Run service name must start with 'swarm-'."
  }

  validation {
    condition     = alltrue([for k, v in var.services : v.max_instances > 0 && v.max_instances <= 1000])
    error_message = "max_instances must be explicit and between 1 and 1000; an unbounded control plane can outscale its own database."
  }

  validation {
    condition     = alltrue([for k, v in var.services : !contains(v.invokers, "allUsers") && !contains(v.invokers, "allAuthenticatedUsers")])
    error_message = "allUsers/allAuthenticatedUsers may never be granted run.invoker; callers are authenticated by Google ID token."
  }
}

variable "labels" {
  type = map(string)
}
