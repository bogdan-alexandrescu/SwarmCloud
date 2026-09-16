# ---------------------------------------------------------------------------
# Project and environment
# ---------------------------------------------------------------------------

variable "project_id" {
  description = "SHARED project. It holds a live GKE cluster agents-staging, VPC agents-staging-vpc and 12 service accounts owned by other teams."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "name_prefix" {
  description = "Prefix on every resource. `make destroy` is label-scoped, and this is the second line of defence behind the label."
  type        = string
  default     = "swarm"

  validation {
    condition     = startswith(var.name_prefix, "swarm")
    error_message = "name_prefix must start with 'swarm'."
  }
}

variable "extra_labels" {
  description = "Merged on top of the mandatory managed-by label. Cannot override it."
  type        = map(string)
  default     = {}

  validation {
    condition     = !contains(keys(var.extra_labels), "managed-by")
    error_message = "managed-by is set by this configuration and may not be overridden; destroy.sh refuses to delete anything lacking managed-by=swarm-terraform."
  }
}

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

variable "deletion_protection" {
  description = "Protects the Autopilot cluster, the Firestore database and the Cloud Run services from deletion."
  type        = bool
  default     = true

  validation {
    condition     = var.environment != "prod" || var.deletion_protection || var.allow_unprotected_prod
    error_message = "prod requires deletion_protection = true. Set allow_unprotected_prod = true to override deliberately."
  }
}

variable "allow_unprotected_prod" {
  description = "The explicit override for running prod without deletion protection. Nothing sets this implicitly."
  type        = bool
  default     = false
}

variable "bucket_force_destroy" {
  description = "True lets terraform destroy delete artifacts and checkpoints. Refused in prod."
  type        = bool
  default     = false

  validation {
    condition     = var.environment != "prod" || var.bucket_force_destroy == false
    error_message = "bucket_force_destroy must be false in prod: it would let a destroy take every checkpoint with it."
  }
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

variable "subnet_cidr" {
  type    = string
  default = "10.40.0.0/20"
}

variable "pods_cidr" {
  type    = string
  default = "10.44.0.0/14"
}

variable "services_cidr" {
  type    = string
  default = "10.48.0.0/20"
}

variable "gke_master_cidr" {
  type    = string
  default = "172.16.32.0/28"
}

variable "nat_static_ip_count" {
  type    = number
  default = 2
}

variable "restrict_egress" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------------------
# GKE Autopilot (browser / GPU / >32 GiB only)
# ---------------------------------------------------------------------------

variable "enable_gke_autopilot" {
  type    = bool
  default = true
}

variable "gke_release_channel" {
  type    = string
  default = "REGULAR"
}

variable "gke_enable_private_endpoint" {
  type    = bool
  default = false
}

variable "gke_master_authorized_cidrs" {
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

variable "firestore_database" {
  type    = string
  default = "swarm"

  validation {
    condition     = var.firestore_database != "(default)"
    error_message = "the (default) database must stay free for other teams in this shared project."
  }
}

variable "bootstrap_firestore_documents" {
  type    = bool
  default = true
}

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

variable "image_tag" {
  description = "Tag used on the FIRST apply only; the deploy pipeline owns the image afterwards (see ignore_changes in the cloud_run modules)."
  type        = string
  default     = "bootstrap"
}

variable "artifact_registry_repository" {
  description = "Must match swarm_common.config.Settings.artifact_registry."
  type        = string
  default     = "swarm-images"
}

variable "immutable_image_tags" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

variable "tenants" {
  description = "Tenants provisioned up front. The API creates personal fallback tenants at runtime; those are not managed here."
  type = map(object({
    kind           = string
    principal      = string
    display_name   = optional(string, "")
    providers      = optional(list(string), [])
    max_active     = optional(number, 20)
    capacity_units = optional(number, 40)
    ksa_name       = optional(string, "swarm-agent-worker")
  }))
  default = {}
}

variable "secret_admin_members" {
  description = "Identities allowed to ADD a provider-key version. They cannot read one back."
  type        = list(string)
  default     = []
}

variable "api_invokers" {
  description = "Identities allowed to call swarm-api. Google groups, not allUsers."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.api_invokers, "allUsers") && !contains(var.api_invokers, "allAuthenticatedUsers")
    error_message = "the API authenticates callers by Google ID token; a public invoker binding would bypass that entirely."
  }
}

# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

variable "pool_limits" {
  description = <<-EOT
    Hard ceilings for the slot pools. These are the `hard_limit` of each pool
    document; the adaptive target and any quota-derived cap can only lower the
    effective limit, never raise it above this.
  EOT
  type = object({
    global           = optional(number, 100)
    default_tenant   = optional(number, 20)
    provider_tenant  = optional(number, 10)
    resource_classes = optional(map(number), {})
    runner_profiles  = optional(map(number), {})
    backends         = optional(map(number), {})
    providers        = optional(map(number), {})
  })
  default = {}

  validation {
    condition     = var.pool_limits.global > 0
    error_message = "the global pool must have a positive, finite ceiling."
  }
}

variable "service_max_instances" {
  description = "Explicit Cloud Run maximum per control-plane service."
  type        = map(number)
  default = {
    "swarm-api"          = 20
    "swarm-scheduler"    = 3
    "swarm-quota-broker" = 3
    "swarm-reconciler"   = 2
  }

  validation {
    condition     = alltrue([for k, v in var.service_max_instances : v > 0])
    error_message = "every service needs an explicit positive max-instances."
  }
}

variable "settings_env" {
  description = <<-EOT
    Overrides for swarm_common.config.Settings.from_env. Only what must be known
    at process start belongs here -- concurrency limits live in Firestore
    precisely so they can change without a redeploy.
  EOT
  type        = map(string)
  default     = {}
}

variable "allowed_domains" {
  description = "Hosted domains permitted to authenticate."
  type        = list(string)
  default     = ["saga.xyz"]

  validation {
    condition     = length(var.allowed_domains) > 0
    error_message = "at least one allowed domain is required; refusing open auth."
  }
}

# ---------------------------------------------------------------------------
# Storage and observability
# ---------------------------------------------------------------------------

variable "artifact_retention_days" {
  type    = number
  default = 90
}

variable "alert_emails" {
  type    = list(string)
  default = []
}

variable "extra_notification_channels" {
  type    = list(string)
  default = []
}

variable "create_alerts" {
  type    = bool
  default = true
}

variable "manage_project_services" {
  description = "All 18 APIs are already enabled on saga-agents-staging; this holds them enabled rather than turning anything on."
  type        = bool
  default     = true
}

variable "custom_role_suffix" {
  description = "Disambiguates custom role ids when a previous one is still in its 7-day soft-delete window."
  type        = string
  default     = ""
}
