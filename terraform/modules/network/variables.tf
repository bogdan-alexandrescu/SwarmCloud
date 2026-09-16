variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name_prefix" {
  description = "Every resource carries this prefix so the label-scoped destroy can never reach another team's network."
  type        = string
  default     = "swarm"

  validation {
    condition     = startswith(var.name_prefix, "swarm")
    error_message = "name_prefix must start with 'swarm'."
  }
}

variable "subnet_cidr" {
  description = "Primary range. Sized for Direct VPC egress from Cloud Run Jobs, which consumes one address per running instance."
  type        = string
  default     = "10.40.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary range for GKE Autopilot Pods."
  type        = string
  default     = "10.44.0.0/14"
}

variable "services_cidr" {
  description = "Secondary range for GKE Autopilot Services."
  type        = string
  default     = "10.48.0.0/20"
}

variable "pods_range_name" {
  type    = string
  default = "swarm-pods"
}

variable "services_range_name" {
  type    = string
  default = "swarm-services"
}

variable "gke_master_cidr" {
  description = "GKE private control-plane range. Used for the webhook firewall rule."
  type        = string
  default     = "172.16.32.0/28"
}

variable "nat_static_ip_count" {
  description = <<-EOT
    Reserved static egress addresses for Cloud NAT. Agent workers call model
    providers, and providers allow-list source addresses, so egress must be
    stable rather than whatever Cloud NAT hands out today.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.nat_static_ip_count >= 1 && var.nat_static_ip_count <= 8
    error_message = "nat_static_ip_count must be between 1 and 8."
  }
}

variable "nat_min_ports_per_vm" {
  type    = number
  default = 128
}

variable "flow_log_sampling" {
  description = "Fraction of flows logged. 0.5 keeps forensics usable without paying for every packet."
  type        = number
  default     = 0.5

  validation {
    condition     = var.flow_log_sampling > 0 && var.flow_log_sampling <= 1
    error_message = "flow_log_sampling must be greater than 0 (flow logs are required) and at most 1."
  }
}

variable "worker_network_tag" {
  description = <<-EOT
    Network tag carried by every agent-worker instance, on Cloud Run Jobs and on
    GKE alike. It is the handle the worker-ingress deny rule targets, which is
    what stops one tenant's worker reaching another's over the shared subnet.
  EOT
  type        = string
  default     = "swarm-worker"

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.worker_network_tag))
    error_message = "a network tag is 1-63 lowercase alphanumerics or dashes, starting with a letter."
  }
}

variable "restrict_egress" {
  description = <<-EOT
    When true, egress is default-deny with explicit allows for HTTPS, DNS and
    the Google restricted VIP. Off by default because agent workers clone over
    arbitrary git transports; turn it on per environment once the real egress
    set is known.
  EOT
  type        = bool
  default     = false
}

variable "egress_allowed_ports" {
  description = "TCP ports permitted out when restrict_egress is true."
  type        = list(string)
  default     = ["443", "22"]
}

variable "labels" {
  description = "Applied to every resource in this module that supports labels."
  type        = map(string)
}
