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
  description = <<-EOT
    Tenants provisioned up front. The API creates personal fallback tenants at
    runtime; those are not managed here.

    `max_active` is deliberately NOT defaulted here. A default would make
    `pool_limits.default_tenant` unreachable -- coalesce() never sees a null --
    and an operator who added a tenant without stating a ceiling would silently
    get the type's default instead of the environment's. Left null, the tenant
    pool falls through to pool_limits.default_tenant, which is what both tfvars
    files are written as though it does.
  EOT
  type = map(object({
    kind           = string
    principal      = string
    display_name   = optional(string, "")
    providers      = optional(list(string), [])
    max_active     = optional(number)
    capacity_units = optional(number, 40)
    ksa_name       = optional(string, "swarm-agent-worker")
    # Identities allowed to add a version to THIS tenant's provider-key secrets.
    # Empty falls back to var.secret_admin_members, which must be a platform
    # admin group rather than any tenant's own group -- see the validation there.
    secret_admins = optional(list(string), [])
  }))
  default = {}

  validation {
    condition     = alltrue([for t, v in var.tenants : v.max_active == null || v.max_active > 0])
    error_message = "a tenant's max_active must be positive; omit it to take pool_limits.default_tenant."
  }
}

variable "secret_admin_members" {
  description = <<-EOT
    Fallback identities allowed to ADD a provider-key version for a tenant that
    declares no `secret_admins` of its own. They cannot read a key back.

    This list is applied to EVERY such tenant's secrets, so it must be a
    dedicated platform-admin group. A tenant's own group here would hand every
    member of that tenant secretVersionAdder on every other tenant's provider
    key -- and while secretVersionAdder cannot read the key in place, it can
    REPLACE it with one that points at an attacker-controlled proxy, after which
    the victim tenant's prompts, source and output all flow through it. The
    validation below refuses that shape outright.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = length([
      for m in var.secret_admin_members : m
      if contains([for t, v in var.tenants : lower("group:${v.principal}")], lower(m))
      || contains([for t, v in var.tenants : lower("user:${v.principal}")], lower(m))
    ]) == 0
    error_message = "secret_admin_members is applied to every tenant's secrets, so it may not name a tenant's own principal: that grants one tenant the ability to replace another tenant's provider key. Use a dedicated platform-admin group, or set per-tenant `secret_admins`."
  }
}

variable "api_invokers" {
  description = <<-EOT
    Identities allowed past Cloud Run's edge to reach swarm-api.

    This is NOT what authenticates a caller. swarm_api.auth is: it verifies the
    Google ID token against Google's keys, requires email_verified, enforces the
    saga.xyz hosted domain, and resolves Cloud Identity group membership to pick
    the tenant. This variable only decides who the platform lets through to be
    authenticated.

    Setting it to ["allUsers"] is the SUPPORTED configuration, because Cloud Run
    cannot do both jobs at once. When the edge enforces IAM it CONSUMES the
    caller's Authorization header and the container receives a different,
    non-JWT credential -- measured on one correlated request on 2026-09-16:
    the client sent tok:7f518e564d27 and the application saw tok:c130e289a085,
    which failed with MalformedError. So edge IAM does not add a second layer
    here; it removes the only one that can identify a tenant.

    Reachability is controlled by ingress, which the cloud_run module pins to
    internal-and-cloud-load-balancing and refuses to set to INGRESS_TRAFFIC_ALL.
    That, not this binding, is what satisfies "no required service publicly
    exposed by default".
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.api_invokers, "allAuthenticatedUsers")
    error_message = "allAuthenticatedUsers means every Google account and adds nothing: the application performs the real authentication. Use allUsers with internal ingress, or name specific groups."
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

  # swarm_common.models.pool_names_for() makes a task acquire BOTH
  # `provider:<p>` and `provider:<p>:tenant:<t>`, and admission is all-or-nothing
  # across the list. So whenever the provider-wide ceiling is lower than the sum
  # of the per-tenant ceilings underneath it, it becomes the binding constraint
  # and tenants start competing for the same slots -- one tenant parking long
  # runs directly reduces what another can admit, even though that other tenant
  # pays for and holds its own key. The per-tenant pools exist precisely so that
  # cannot happen.
  #
  # Requiring the provider pool to be at least the sum keeps it as what it is
  # meant to be: a platform-wide safety net that can never be what denies a
  # tenant its own declared capacity. The `global` pool is where a real
  # cross-tenant ceiling belongs, because it is the one every task shares.
  validation {
    condition = alltrue([
      for p in distinct(flatten([for t, cfg in var.tenants : cfg.providers])) :
      lookup(var.pool_limits.providers, p, var.pool_limits.global) >=
      length([for t, cfg in var.tenants : t if contains(cfg.providers, p)]) * var.pool_limits.provider_tenant
    ])
    error_message = "a provider's platform-wide pool must be at least (tenants holding that provider) x provider_tenant, or the shared ceiling re-couples tenants that were deliberately given separate per-tenant provider pools."
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

variable "pubsub_kms_key_name" {
  description = "Optional CMEK for the wake and dead-letter topics. Empty uses Google-managed keys."
  type        = string
  default     = ""
}

variable "enable_quota_refresh" {
  description = "Create the Cloud Scheduler tick that recomputes provider quota state and adaptive pool targets."
  type        = bool
  default     = true
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
