variable "project_id" {
  type = string
}

variable "firestore_database" {
  type    = string
  default = "swarm"
}

# DEFAULTS OFF. Firestore does not evaluate IAM Conditions on the data plane, so
# this condition does not scope document access -- it DENIES it. Verified live:
# every identity carrying it reported "firestore unavailable: PermissionDenied".
# See terraform/modules/iam/variables.tf and docs/security.md.
variable "scope_firestore_to_database" {
  type    = bool
  default = false
}

# DEFAULTS TRUE: every tenant's worker keeps swarmTenantWorkerFirestore on the
# database, exactly as before this switch existed, so adding it changes no plan.
# False creates no worker_firestore binding for any tenant -- the last step of
# #1457's design B, taken by the owner after every tenant's worker reaches its
# state through swarm-api (main.tf, "Control-plane state", says why it is safe
# only then). Never set false in a tfvars ahead of that cutover.
variable "worker_firestore_access" {
  description = "Grant each tenant's worker the swarmTenantWorkerFirestore role on the database. True is today's grant; false removes it for every tenant (after the design-B cutover)."
  type        = bool
  default     = true
  nullable    = false
}

# DEFAULTS EMPTY: every tenant keeps its worker_firestore binding, so adding
# this changes no plan. The per-tenant twin of worker_firestore_access above,
# because the cutover goes one tenant at a time (owner decision Q7: u-bogdan,
# then eng, then all) and the global switch cannot take eng's role alone. A
# listed tenant's binding is filtered out of the for_each; every other tenant
# keeps its tenant-id key, so the plan destroys the listed binding and touches
# no other. Every entry must be a key of var.tenants: a name that is not one
# would plan nothing, and an empty plan for a cutover step reads like one that
# worked. terraform/infra refuses an entry not yet in state_via_api_tenants.
variable "worker_firestore_off_tenants_iam" {
  description = "Tenant ids (keys of var.tenants) whose worker gets NO swarmTenantWorkerFirestore binding even while worker_firestore_access is true. Default [] = every tenant keeps today's grant."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for t in var.worker_firestore_off_tenants_iam : contains(keys(var.tenants), t)])
    error_message = "every worker_firestore_off_tenants_iam entry must be a tenant in var.tenants, by its exact id: an entry that names none removes nothing, and \"*\" is not supported."
  }
}

variable "artifact_bucket" {
  type = string
}

variable "workload_identity_pool" {
  description = "<project>.svc.id.goog. Empty when the Autopilot cluster is not deployed."
  type        = string
  default     = ""
}

variable "tenants" {
  description = <<-EOT
    Tenants known at provisioning time.

    A tenant is a Google group (`eng@saga.xyz` -> `eng`) or a personal fallback
    (`u-<user>`), exactly as swarm_common.identity derives them. `providers` is
    the set this tenant will register keys for; a runner whose provider is
    absent parks as CREDENTIAL_MISSING rather than failing mid-run.
  EOT
  type = map(object({
    kind         = string # "group" | "user"
    principal    = string # eng@saga.xyz
    display_name = optional(string, "")
    providers    = optional(list(string), [])
    # Nullable, matching terraform/infra: a default here would shadow the
    # environment's pool_limits.default_tenant rather than fall through to it.
    max_active     = optional(number)
    capacity_units = optional(number, 40)
    # Kubernetes service account that assumes this tenant's GSA on Autopilot.
    ksa_name = optional(string, "swarm-agent-worker")
  }))

  validation {
    condition     = alltrue([for t, v in var.tenants : contains(["group", "user"], v.kind)])
    error_message = "tenant kind must be 'group' or 'user'."
  }

  validation {
    condition     = alltrue([for t, v in var.tenants : can(regex("^[a-z0-9-]+$", t))])
    error_message = "tenant ids are lowercase alphanumeric with dashes, matching swarm_common.identity's slugging."
  }

  validation {
    condition     = alltrue([for t, v in var.tenants : length(t) <= 11])
    error_message = "tenant id must be at most 11 characters: the service account id swarm-agent-worker-<tenant> has a 30-character GCP limit."
  }

  validation {
    condition     = alltrue([for t, v in var.tenants : v.kind != "user" || startswith(t, "u-")])
    error_message = "a personal tenant id must be prefixed u- so it cannot collide with a group tenant."
  }
}

variable "dispatcher_members" {
  description = <<-EOT
    Identities allowed to actAs a tenant worker SA, keyed by component name.
    Cloud Run pins the service account on the Job resource, so whoever creates
    that Job needs iam.serviceAccountUser on the SA it names -- granted on the
    SA itself, never project-wide.

    Keyed by component because the members are service account emails that only
    exist after apply; terraform cannot plan a for_each over unknown keys.
  EOT
  type        = map(string)
  default     = {}
}

variable "namespace_prefix" {
  description = "Kubernetes namespace per tenant on the Autopilot cluster."
  type        = string
  default     = "swarm-tenant-"
}

variable "labels" {
  type = map(string)
}
