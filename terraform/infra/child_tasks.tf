# Child tasks, the deployment side (docs/design/child-tasks.md §3.2, §9 item 2).
#
# Three things make a running agent's child submission reach swarm-api:
#
#   * `swarm-child-key`, the platform HMAC key the scheduler mints each
#     attempt's one-use registration nonce with and swarm-api verifies,
#     derives registration ids and attests with. The SECRET and its accessor
#     binding are here; its VALUE never is -- a managed secret version would put
#     the plaintext in a state file several people can read. The value is
#     generated and added by `scripts/create-secrets.sh --child-key`, which
#     nobody sees.
#   * the two services reading it as SWARM_CHILD_KEY, by Secret Manager
#     reference (modules/cloud_run `secret_env`), and the scheduler handing each
#     worker SWARM_API_URL and SWARM_API_AUDIENCE (locals.tf), the address and
#     the token audience of the worker-only child routes.
#   * the network and IAM path from a tenant's worker to swarm-api: the workers
#     already egress ALL_TRAFFIC through the VPC (modules/cloud_run_jobs, and
#     the GKE pods' node network), which is how they reach the internal-ingress
#     quota broker today, so swarm-api is reached the same way; what was missing
#     is `run.invoker` for each tenant's worker account on swarm-api (main.tf)
#     and an audience the worker can mint a token for without knowing a URL
#     (`local.push_audiences["swarm-api"]`, a custom audience on the service).
#
# OFF UNTIL `enable_child_tasks` IS SET, and it must not be set before two
# things are done, in this order:
#
#   1. `make apply` with it false: the secret and its two accessor bindings
#      exist, nothing reads them yet;
#   2. `scripts/create-secrets.sh --child-key`: the first version exists. A
#      Cloud Run revision that references a secret with no version FAILS TO
#      START, so flipping the switch before this would take swarm-api and the
#      scheduler down, not merely leave child tasks off;
#   3. `enable_child_tasks = true`, apply. swarm-api's address is computed
#      from the project number (main.tf `local.swarm_api_url`), not copied.
#
# AND NOT ANYWHERE BEFORE the measurement design §9 item 2 asks for: that a
# tenant service account cannot read a Cloud Run execution's overrides
# (`run.executions.get`) or a GKE pod spec in its namespace. If another
# container of the tenant can read SWARM_CHILD_NONCE, it can race the worker's
# registration. Deferred to #476 with the other security items; the switch is
# off by default so that measurement comes first.
#
# THE FORGE BROKER (#1041) needs the same key: a person's `git-u-` slot is
# released by swarm-api only to an attempt that registered one. But child
# tasks being on does NOT turn the broker on: dev has `enable_child_tasks =
# true` (since 2026-10-11, for the live browser), so the broker has its own
# switch, `forge_broker` below, off by default and off in dev. Off, workers
# read a person's slot with their own account through the bootstrap root's
# `forge_slot_reader` binding, exactly as before the broker; on, swarm-api's
# broker is the only path. That binding is removed only after `forge_broker`
# has been on in dev and a live run passed -- a later, separate owner step
# (docs/design/user-scoped-secrets.md §5), never the same change.
#
# Off, nothing about a deployment changes: no service reads the secret, the
# scheduler mints no nonce (no SWARM_CHILD_KEY), every worker runs without a
# child path, and both child routes answer 503 child_submit_unavailable.

variable "enable_child_tasks" {
  description = <<-EOT
    Give swarm-scheduler and swarm-api SWARM_CHILD_KEY (from the
    swarm-child-key secret) and let every tenant's worker invoke swarm-api, so
    a running agent can submit child tasks (docs/design/child-tasks.md). Set it
    only after `scripts/create-secrets.sh --child-key` has added a version: a
    revision that references a secret with no version fails to start. See
    child_tasks.tf for the order and the measurement that comes first.
  EOT
  type        = bool
  default     = false
}

variable "forge_broker" {
  description = <<-EOT
    Turn on the forge-credential broker (#1041, docs/design/user-scoped-secrets.md):
    rendered as FORGE_BROKER into swarm-api (which then answers
    POST /v1/attempts/forge-credential) and the scheduler (which passes it to
    every worker and keys a user-slot child for it). On, a worker obtains a
    person's git-u- slot ONLY from swarm-api, proven with its attempt key, and
    fails closed without it. Off -- the default, and dev's setting although dev
    has enable_child_tasks on -- workers read the slot with their own account
    through forge_slot_reader, exactly as before the broker. Needs
    enable_child_tasks: the proof is the child path's attempt key. The
    forge_slot_reader binding is removed only after this has been on in dev
    and a live run passed, as a separate owner step.
  EOT
  type        = bool
  default     = false

  validation {
    condition     = !var.forge_broker || var.enable_child_tasks
    error_message = "forge_broker needs enable_child_tasks: the broker releases a person's slot only to an attempt proven with the child path's attempt key, so without it every user-slot task would fail closed."
  }
}

variable "state_via_api_tenants" {
  description = <<-EOT
    Tenant ids whose workers reach their attempt's state ONLY through
    swarm-api's state routes, never direct Firestore (#1457,
    docs/design/tenant-settings-integrity.md §4.6, ISO-SCHED phase 2).
    Rendered into swarm-scheduler as SWARM_STATE_VIA_API_TENANTS; the
    scheduler dispatches every attempt of a listed tenant, children included,
    with SWARM_STATE_VIA_API=true, SWARM_API_URL and the attempt's
    registration nonce. Default [] = nobody switched, and an unlisted tenant
    is dispatched exactly as before. Dev's tenants first, then every tenant;
    only then is the worker's Firestore role removed (phase 3). Needs
    enable_child_tasks: a switched worker opens its attempt by spending the
    child path's nonce at state/open, and without SWARM_CHILD_KEY it cannot
    start.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.state_via_api_tenants) == 0 || var.enable_child_tasks
    error_message = "state_via_api_tenants needs enable_child_tasks: a switched worker spends the child path's registration nonce at state/open, so without SWARM_CHILD_KEY every attempt of a listed tenant would refuse to start."
  }

  validation {
    condition     = alltrue([for t in var.state_via_api_tenants : can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", t))])
    error_message = "every state_via_api_tenants entry must be a tenant id (lowercase letters, digits and inner hyphens). \"*\" is not supported: list the tenants."
  }
}

variable "child_key_previous_version" {
  description = <<-EOT
    During a swarm-child-key rotation only: the version number the new one
    replaced, read by swarm-api as SWARM_CHILD_KEY_PREVIOUS so a worker
    dispatched under the old key can still register (design §5 F13). Set it in
    the apply that follows `create-secrets.sh --child-key`, keep it for one
    dispatch window (the lease's 300 s dispatch deadline), then clear it and
    disable the old version. Empty outside a rotation.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^([0-9]+)?$", var.child_key_previous_version))
    error_message = "child_key_previous_version must be a version NUMBER, or empty; never a value."
  }
}

resource "google_secret_manager_secret" "child_key" {
  project   = var.project_id
  secret_id = "swarm-child-key"

  # Pinned to the workload region, as every tenant credential is: automatic
  # replication would copy a platform key into regions nothing here runs in.
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "child-key"
  })

  annotations = {
    "swarm-populated-by" = "scripts/create-secrets.sh --child-key"
  }
}

# The scheduler and swarm-api, and NOBODY ELSE -- no tenant service account,
# no human. Authoritative, so a grant made out of band is removed on the next
# apply. A tenant identity that could read this could mint a nonce for any
# attempt and forge a registration's attestation (design §3.2).
resource "google_secret_manager_secret_iam_binding" "child_key_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.child_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = [
    module.iam.service_account_members["swarm-scheduler"],
    module.iam.service_account_members["swarm-api"],
  ]
}

locals {
  # The Secret Manager references both services read the key through. Empty
  # while child tasks are off, so a deployment that has not added a version
  # references nothing and starts as it always has.
  child_key_secret_env = var.enable_child_tasks ? merge(
    {
      SWARM_CHILD_KEY = {
        secret  = google_secret_manager_secret.child_key.secret_id
        version = "latest"
      }
    },
    var.child_key_previous_version == "" ? {} : {
      SWARM_CHILD_KEY_PREVIOUS = {
        secret  = google_secret_manager_secret.child_key.secret_id
        version = var.child_key_previous_version
      }
    },
  ) : {}

  # The scheduler mints nonces; it never verifies one, so the previous version
  # is swarm-api's alone.
  scheduler_child_key_secret_env = {
    for k, v in local.child_key_secret_env : k => v if k == "SWARM_CHILD_KEY"
  }

  # Every tenant's worker account may invoke swarm-api once child tasks are
  # on: the child routes are the only ones a worker calls, and swarm-api's own
  # check (`ChildService.authenticate_worker` against the body's tenant, then
  # the attempt proof) decides WHICH tenant and attempt it is. Keyed by tenant
  # id, as the broker's grants are: an email is unknown until apply.
  child_route_invokers = var.enable_child_tasks ? {
    for tenant_id, member in module.tenancy.worker_members :
    "worker-${tenant_id}" => member
  } : {}
}

output "swarm_api_url" {
  description = "swarm-api's own uri, as Cloud Run reports it. Workers are handed main.tf's local.swarm_api_url, the deterministic URL of the same service."
  value       = module.cloud_run.service_urls["swarm-api"]
}

output "child_tasks_service_wiring" {
  description = "What child tasks put on the services: the Secret Manager env each reads, the tenants whose worker may invoke swarm-api, the address and audience handed to workers, and each service's FORGE_BROKER switch. Names only; never a secret value."
  value = {
    secret_env = {
      "swarm-api"       = sort(keys(local.child_key_secret_env))
      "swarm-scheduler" = sort(keys(local.scheduler_child_key_secret_env))
    }
    worker_invokers    = sort(keys(local.child_route_invokers))
    api_audience       = local.service_env["swarm-api"].SWARM_API_AUDIENCE
    scheduler_audience = local.service_env["swarm-scheduler"].SWARM_API_AUDIENCE
    scheduler_api_url  = local.service_env["swarm-scheduler"].SWARM_API_URL
    forge_broker = {
      "swarm-api"       = local.service_env["swarm-api"].FORGE_BROKER
      "swarm-scheduler" = local.service_env["swarm-scheduler"].FORGE_BROKER
    }
  }
}
