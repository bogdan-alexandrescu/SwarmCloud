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
#   3. `enable_child_tasks = true`, `swarm_api_url` from `terraform output
#      swarm_api_url`, apply.
#
# AND NOT ANYWHERE BEFORE the measurement design §9 item 2 asks for: that a
# tenant service account cannot read a Cloud Run execution's overrides
# (`run.executions.get`) or a GKE pod spec in its namespace. If another
# container of the tenant can read SWARM_CHILD_NONCE, it can race the worker's
# registration. Deferred to #476 with the other security items; the switch is
# off by default so that measurement comes first.
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

variable "swarm_api_url" {
  description = <<-EOT
    Base URL of the swarm-api Cloud Run service, for the SCHEDULER's
    environment (SWARM_API_URL). The scheduler never calls swarm-api; it hands
    this to every worker with a child path (scheduler.dispatch.worker_env), and
    a worker with no SWARM_API_URL offers its agent no child path.

    Declared rather than derived for quota_broker_url's reason: the scheduler's
    environment is an input to the Cloud Run module and swarm-api's URL is an
    output of it, a cycle terraform refuses to plan. Read it from
    `terraform output swarm_api_url` after the first apply. The
    `swarm_api_url_is_wired` check fails while it is stale.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.swarm_api_url == "" || startswith(var.swarm_api_url, "https://")
    error_message = "swarm_api_url must be an https:// base URL, or empty."
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

check "swarm_api_url_is_wired" {
  assert {
    condition     = var.swarm_api_url == "" || var.swarm_api_url == module.cloud_run.service_urls["swarm-api"]
    error_message = "swarm_api_url does not match the deployed swarm-api URL. A worker handed a stale one cannot submit children: every request answers api_unavailable. Run `terraform output swarm_api_url` and update tfvars."
  }
}

check "child_tasks_have_an_address" {
  assert {
    condition     = !var.enable_child_tasks || var.swarm_api_url != ""
    error_message = "enable_child_tasks is set but swarm_api_url is empty: the scheduler mints a nonce only for a worker it can also give swarm-api's address, so no attempt gets a child path. Set swarm_api_url from `terraform output swarm_api_url`."
  }
}

output "swarm_api_url" {
  description = "Paste into swarm_api_url in tfvars. See that variable for why this is a two-step."
  value       = module.cloud_run.service_urls["swarm-api"]
}

output "child_tasks_service_wiring" {
  description = "What child tasks put on the services: the Secret Manager env each reads, the tenants whose worker may invoke swarm-api, and the address and audience handed to workers. Names only; never a value."
  value = {
    secret_env = {
      "swarm-api"       = sort(keys(local.child_key_secret_env))
      "swarm-scheduler" = sort(keys(local.scheduler_child_key_secret_env))
    }
    worker_invokers    = sort(keys(local.child_route_invokers))
    api_audience       = local.service_env["swarm-api"].SWARM_API_AUDIENCE
    scheduler_audience = local.service_env["swarm-scheduler"].SWARM_API_AUDIENCE
    scheduler_api_url  = local.service_env["swarm-scheduler"].SWARM_API_URL
  }
}
