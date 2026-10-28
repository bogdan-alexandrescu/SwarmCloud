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
  description = <<-EOT
    Ports Cloud NAT reserves to every VM (GKE node or Cloud Run instance) up
    front. Each one is held whether it is used or not, so this times the number
    of VMs must fit in nat_static_ip_count x 64,512.
  EOT
  type        = number
  default     = 128

  # Cloud NAT refuses anything else with dynamic port allocation on, and only
  # at apply. A plan should refuse it first.
  validation {
    condition     = contains([32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768], var.nat_min_ports_per_vm)
    error_message = "nat_min_ports_per_vm must be a power of two from 32 to 32768 (Cloud NAT dynamic port allocation)."
  }
}

variable "nat_max_ports_per_vm" {
  description = <<-EOT
    The most ports dynamic allocation will double a busy VM up to. A cap, not a
    reservation: it costs nothing until one VM really needs it.
  EOT
  type        = number
  default     = 8192

  validation {
    condition     = contains([64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536], var.nat_max_ports_per_vm)
    error_message = "nat_max_ports_per_vm must be a power of two from 64 to 65536 (Cloud NAT dynamic port allocation)."
  }

  validation {
    condition     = var.nat_max_ports_per_vm > var.nat_min_ports_per_vm
    error_message = "nat_max_ports_per_vm must be greater than nat_min_ports_per_vm."
  }
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
    When true, egress is default-deny, with one allow for TCP
    egress_allowed_ports (443 and 22 by default) and UDP 53 to any destination
    (0.0.0.0/0). There is no restricted-VIP rule: traffic to the VIP already
    passes the 443 allow, and narrowing the allow to the VIP would cut provider
    and git egress. Off by default because agent workers clone over
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
