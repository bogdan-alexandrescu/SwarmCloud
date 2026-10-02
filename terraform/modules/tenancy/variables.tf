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
    # The forge the merge and post-verdict Jobs talk to (#295; merge-step.md
    # §2.1b, open question (a), resolved 2026-09-29). Rendered into those Jobs'
    # environment and nowhere a tenant identity can write: a task document's
    # repository_url is a claim, and these are what a claim is checked
    # against. None of the five is a credential -- the App keys stay in Secret
    # Manager. `review_app_bot_id` is the review App's bot USER id, resolved
    # by registering git-review; `merge` filters reviews to it.
    forge = optional(object({
      host              = optional(string, "api.github.com")
      owner             = string
      repo              = string
      review_app_id     = optional(number)
      review_app_bot_id = optional(number)
    }))
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

  # A merge or post-verdict Job with no forge record would have to read the
  # host, owner and repo out of the task -- which is the forged-repository_url
  # attack merge-step.md §2.1b closes. So the plan refuses it.
  validation {
    condition = alltrue([
      for t, v in var.tenants :
      v.forge != null || (!contains(v.providers, "git-merge") && !contains(v.providers, "git-review"))
    ])
    error_message = "a tenant that registers git-merge or git-review must declare `forge` (host, owner, repo): the merge and post-verdict workers never take them from a task."
  }

  # post-verdict checks the key it read against the registered App id, and
  # merge filters reviews to the review App's bot user; without either, the
  # step has nothing to check against.
  validation {
    condition = alltrue([
      for t, v in var.tenants :
      !contains(v.providers, "git-review") || try(v.forge.review_app_id, null) != null
    ])
    error_message = "a tenant that registers git-review must declare forge.review_app_id."
  }

  # Merging trusts exactly one reviewer, the tenant's review App, so a merge
  # account with no review App to trust could only ever refuse.
  validation {
    condition = alltrue([
      for t, v in var.tenants :
      !contains(v.providers, "git-merge") || (contains(v.providers, "git-review") && try(v.forge.review_app_bot_id, null) != null)
    ])
    error_message = "a tenant that registers git-merge must also register git-review and declare forge.review_app_bot_id: merge accepts a verdict only from the review App's bot user."
  }

  # The pinned form: a bare lower-case hostname. No scheme, port, path or
  # userinfo, any of which would let a value point the App's token somewhere
  # the worker does not mean.
  validation {
    condition = alltrue([
      for t, v in var.tenants :
      v.forge == null || can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", v.forge.host))
    ])
    error_message = "forge.host is a bare lower-case hostname (api.github.com, or the Enterprise Server host exactly): no scheme, port, path or userinfo."
  }

  # GitHub's own shapes: an owner is 1-39 alphanumerics or single hyphens; a
  # repository name is up to 100 of [A-Za-z0-9._-] and never "." or "..".
  validation {
    condition = alltrue([
      for t, v in var.tenants :
      v.forge == null || (
        can(regex("^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$", v.forge.owner))
        && can(regex("^[A-Za-z0-9._-]{1,100}$", v.forge.repo))
        && !contains([".", ".."], v.forge.repo)
      )
    ])
    error_message = "forge.owner and forge.repo must be a GitHub owner and repository name, nothing else: they are spliced into /repos/{owner}/{repo}/... by the worker."
  }

  validation {
    condition = alltrue([
      for t, v in var.tenants :
      v.forge == null || alltrue([
        for id in [try(v.forge.review_app_id, null), try(v.forge.review_app_bot_id, null)] :
        id == null || (id > 0 && floor(id) == id)
      ])
    ])
    error_message = "forge.review_app_id and forge.review_app_bot_id are positive integers, as GitHub issues them."
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

variable "action_act_as_members" {
  description = <<-EOT
    Identities allowed to actAs the #295 merge, post-verdict and review
    accounts, keyed by a static label. The deployer only: it deploys their
    Jobs. Never a tenant identity, and not the dispatcher or the reconciler,
    which act as every worker account (main.tf, action_act_as_grants).
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
