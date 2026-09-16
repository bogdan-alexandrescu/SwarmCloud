variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "cluster_name" {
  description = "Must not be `agents-staging`: that cluster exists and belongs to another team."
  type        = string
  default     = "swarm-autopilot"

  validation {
    condition     = startswith(var.cluster_name, "swarm-")
    error_message = "cluster_name must start with 'swarm-' so it can never collide with agents-staging."
  }
}

variable "network" {
  description = "Self link or name of the swarm VPC."
  type        = string
}

variable "subnetwork" {
  type = string
}

variable "pods_range_name" {
  type = string
}

variable "services_range_name" {
  type = string
}

variable "master_ipv4_cidr_block" {
  type    = string
  default = "172.16.32.0/28"
}

variable "release_channel" {
  type    = string
  default = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.release_channel)
    error_message = "release_channel must be RAPID, REGULAR or STABLE. UNSPECIFIED pins the cluster to manual upgrades."
  }
}

variable "enable_private_endpoint" {
  description = <<-EOT
    True hides the control-plane endpoint from the internet entirely. That also
    means terraform and kubectl must run from inside the VPC, so it defaults to
    false and is turned on per environment once bastion access exists.
  EOT
  type        = bool
  default     = false
}

variable "master_authorized_cidrs" {
  description = "CIDRs allowed to reach the control plane. Empty means Google-internal access only."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []

  validation {
    condition     = alltrue([for c in var.master_authorized_cidrs : c.cidr_block != "0.0.0.0/0"])
    error_message = "0.0.0.0/0 is not an authorized network; that is the same as having none."
  }
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "maintenance_start_time" {
  description = "RFC3339 start of the weekly maintenance window."
  type        = string
  default     = "2026-01-04T08:00:00Z"
}

variable "maintenance_end_time" {
  type    = string
  default = "2026-01-04T16:00:00Z"
}

variable "maintenance_recurrence" {
  type    = string
  default = "FREQ=WEEKLY;BYDAY=SA,SU"
}

variable "logging_components" {
  type    = list(string)
  default = ["SYSTEM_COMPONENTS", "WORKLOADS"]
}

variable "monitoring_components" {
  type    = list(string)
  default = ["SYSTEM_COMPONENTS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]
}

variable "enable_managed_prometheus" {
  type    = bool
  default = true
}

variable "enable_cilium_clusterwide_network_policy" {
  description = "Cluster-scoped Cilium policies on top of Dataplane V2's namespaced NetworkPolicy."
  type        = bool
  default     = false
}

variable "labels" {
  type = map(string)
}
