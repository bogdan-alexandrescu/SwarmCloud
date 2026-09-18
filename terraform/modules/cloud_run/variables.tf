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
    # Extra OIDC audiences this service accepts, alongside its own URL. Present
    # so a caller and a receiver can agree on an audience that is a constant
    # rather than a URL only known after apply -- without it, a service cannot
    # be told its own audience, because the URL is an attribute of the very
    # resource whose environment would carry it.
    custom_audiences = optional(list(string), [])
    # caller label -> IAM member. Keyed by label because the members are service
    # account emails that are unknown until apply.
    invokers          = optional(map(string), {})
    health_check_path = optional(string, "/healthz")
    container_port    = optional(number, 8080)
  }))

  validation {
    condition     = alltrue([for k, v in var.services : startswith(k, "swarm-")])
    error_message = "every Cloud Run service name must start with 'swarm-'."
  }

  validation {
    condition     = alltrue([for k, v in var.services : v.max_instances > 0 && v.max_instances <= 1000])
    error_message = "max_instances must be explicit and between 1 and 1000; an unbounded control plane can outscale its own database."
  }

  # allAuthenticatedUsers stays forbidden. It means every Google account on
  # earth, and it buys nothing here because the application performs the real
  # authentication itself.
  #
  # allUsers is PERMITTED, and only for a service whose ingress keeps it off the
  # internet. The reason is specific and was established on a live deployment on
  # 2026-09-16: when Cloud Run enforces IAM on a service, it CONSUMES the
  # caller's Authorization header and the container receives a different,
  # non-JWT credential. Measured on one correlated request --
  #
  #     sent by the client : tok:7f518e564d27
  #     seen by the app    : tok:c130e289a085   (MalformedError)
  #
  # -- so swarm_api.auth can never verify the caller's Google ID token while the
  # edge is also gating on it. Double-gating does not harden this service, it
  # breaks it.
  #
  # What actually authenticates a caller is unchanged and is all in the app:
  # signature verification against Google's keys, email_verified, the hosted
  # domain (saga.xyz), and group membership resolved through Cloud Identity to
  # pick the tenant. What keeps the service off the internet is
  # ingress = internal-and-cloud-load-balancing, which is what the spec's
  # "no required service publicly exposed by default" requirement means.
  #
  # Hence the paired validation below: allUsers is allowed ONLY together with
  # internal ingress, so this can never combine into a genuinely open endpoint.
  validation {
    condition = alltrue([
      for k, v in var.services : alltrue([
        for label, member in v.invokers : member != "allAuthenticatedUsers"
      ])
    ])
    error_message = "allAuthenticatedUsers may never be granted run.invoker: it means every Google account, and the application's own ID-token check is what authenticates callers."
  }

  validation {
    condition = alltrue([
      for k, v in var.services :
      !contains(values(v.invokers), "allUsers") || var.ingress != "INGRESS_TRAFFIC_ALL"
    ])
    error_message = "allUsers is only permitted while ingress is restricted. With INGRESS_TRAFFIC_ALL it would leave the API genuinely open to the internet."
  }
}

variable "labels" {
  type = map(string)
}
