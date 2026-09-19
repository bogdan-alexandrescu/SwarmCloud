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

  # 0.0.0.0/0 is refused BY DEFAULT, and opening it is an explicit, per-
  # environment opt-in rather than an edit to this rule. The rule itself stays,
  # because prod runs gke_enable_private_endpoint = false and depends on an
  # empty allowlist -- deleting the check to unblock dev would have quietly
  # removed prod's backstop too.
  #
  # WHAT THE ALLOWLIST BUYS, so the opt-in can be judged rather than copied:
  # it is a NETWORK control and nothing more. The GKE API server still requires
  # a Google identity and still enforces RBAC, so opening it does not make the
  # cluster unauthenticated -- it makes it reachable, and so discoverable and
  # probe-able, from the internet.
  #
  # WHY DEV OPTS IN (2026-09-18, operator decision): the allowlist rots. It held
  # one operator's home /32, which changes without warning, and the symptom when
  # it changes is kubectl hanging rather than saying why. That is what happened
  # here -- the cluster carried a stale operator /32 for an address that person
  # no longer had, so the control was denying the one person it existed to admit
  # while protecting nothing.
  #
  # The alternative that keeps both properties is a private endpoint reached
  # through IAP TCP forwarding or a bastion: no public exposure, no IP to rot.
  # Not chosen because it is setup work rather than a flag. If GKE access
  # becomes routine here rather than occasional, it is the right answer.
  validation {
    condition = alltrue([
      for c in var.master_authorized_cidrs :
      c.cidr_block != "0.0.0.0/0" || var.allow_open_master_authorized_network
    ])
    error_message = "0.0.0.0/0 is not an authorized network; that is the same as having none. Opening the control plane to the internet requires allow_open_master_authorized_network = true, set per environment and never in prod."
  }
}

variable "allow_open_master_authorized_network" {
  description = <<-EOT
    Permits 0.0.0.0/0 in master_authorized_cidrs.

    Exists so that opening a control plane is a decision recorded in an
    environment's tfvars, reviewable in a diff, rather than an edit to the
    validation that protects every environment at once.

    Dev sets it. Prod must not: a prod cluster keeps the private endpoint and
    reaches the control plane from inside the VPC.
  EOT
  type        = bool
  default     = false
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

variable "authenticator_groups_security_group" {
  description = <<-EOT
    Google group whose members' RBAC is managed through group membership, in the
    form gke-security-groups@<domain>. The group must ALREADY EXIST: naming one
    that does not makes cluster creation fail, which is why this is empty by
    default rather than set optimistically.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.authenticator_groups_security_group == "" || startswith(var.authenticator_groups_security_group, "gke-security-groups@")
    error_message = "GKE requires this group to be named gke-security-groups@<domain>."
  }
}

variable "binary_authorization_mode" {
  description = <<-EOT
    Binary Authorization evaluation mode. DISABLED by default because the policy
    it evaluates is a PROJECT singleton and saga-agents-staging is shared:
    enabling enforcement here subjects swarm workloads to whatever attestation
    policy another team has configured project-wide.
  EOT
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["DISABLED", "PROJECT_SINGLETON_POLICY_ENFORCE"], var.binary_authorization_mode)
    error_message = "binary_authorization_mode must be DISABLED or PROJECT_SINGLETON_POLICY_ENFORCE."
  }
}

variable "enable_cilium_clusterwide_network_policy" {
  description = "Cluster-scoped Cilium policies on top of Dataplane V2's namespaced NetworkPolicy."
  type        = bool
  default     = false
}

variable "labels" {
  type = map(string)
}
