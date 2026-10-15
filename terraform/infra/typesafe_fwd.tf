# The TypeSafe forwarder, the platform side (docs/design/workspace-secrets.md
# §3.5, the `inject` delivery mode; owner decision §10 Q8, 2026-10-14; lane
# SEC-INJECT-A, part of #1347). The pattern is egress.tf's.
#
# Three things, and only these:
#
#   * `swarm-typesafe-fwd-key`, the HMAC key swarm-api mints attempt TypeSafe
#     tickets with and the forwarder verifies them with. Its own key, not
#     swarm-egress-key or swarm-live-key: a service that can verify one kind of
#     ticket must not be able to mint another. Its container and its readers
#     are here; its VALUE never is (scripts/create-secrets.sh owns values);
#   * swarm-api's side: SWARM_TYPESAFE_FWD_KEY by reference, once served;
#   * the forwarder's manifest, kubernetes/swarm-system/swarm-typesafe-fwd.yaml,
#     rendered as `swarm_typesafe_fwd_manifest` with the image, the key's
#     version reference, the project and the setting (the kill switch).
#
# WHO READS WHAT.
#
#   swarm-typesafe-fwd-key     swarm-api (mints) and the forwarder's KSA
#                              principal (verifies); nobody else. An identity
#                              that could read it could mint a ticket naming
#                              any tenant's TypeSafe key.
#   swarm-tenant-*-typesafe    the forwarder reads their VERSIONS, under a
#                              condition, through a custom role the OWNER
#                              applies: terraform/bootstrap/typesafe_forwarder.tf.
#                              A versions.access role cannot be defined or
#                              granted from this root (#79).
#
# THE IDENTITY is the Kubernetes service account `swarm-typesafe-fwd` in
# `swarm-system`, as a Workload Identity Federation for GKE principal, exactly
# as swarm-egress's is: no Google service account, so nothing for the CI
# deployer to create or administer, nothing to impersonate, and a tenant pod
# in swarm-tenant-<t> cannot present it. The brief named "a GSA and the KSA
# binding"; the direct principal IS the KSA binding without the GSA, and it is
# what the bootstrap grant names.
#
# THREE STEPS, ALL OFF BY DEFAULT, because a Cloud Run secret reference to a
# secret with no version stops swarm-api starting:
#
#   1. enable_typesafe_forwarder = true, apply: the key's container and its
#      two readers exist. Nothing references it yet.
#   2. add the key's first version (generated, never shown), build the image
#      (lane SEC-INJECT-B) so image_refs has its digest, owner-apply
#      bootstrap's enable_typesafe_forwarder_read, then
#      typesafe_forwarder.serve = true, apply: SWARM_TYPESAFE_FWD_KEY on
#      swarm-api and the rendered manifest.
#   3. typesafe_forwarder.enabled = true and name tenants: the forwarder then
#      serves them. Steps 1-2 forward nothing.
#
# Nothing here labels a pod: no agent pod reaches the forwarder until
# SEC-MIGRATE-TYPESAFE adds the dispatcher label and the tenant-side policy.
#
# GKE ONLY: the forwarder runs on the Autopilot cluster and its principal lives
# in that cluster's workload pool.

variable "enable_typesafe_forwarder" {
  description = <<-EOT
    Create swarm-typesafe-fwd-key (no version) and let swarm-api and the
    TypeSafe forwarder's KSA read it (docs/design/workspace-secrets.md §3.5).
    Off by default: nothing is created until the owner flips it. Serving is
    var.typesafe_forwarder.serve; forwarding is var.typesafe_forwarder.enabled.
  EOT
  type        = bool
  default     = false
}

variable "typesafe_forwarder" {
  description = <<-EOT
    The TypeSafe forwarder's setting (docs/design/workspace-secrets.md §3.5).
    `serve` puts SWARM_TYPESAFE_FWD_KEY on swarm-api and renders the manifest:
    set it only once swarm-typesafe-fwd-key has a version and the image has a
    digest. `enabled` and `tenants` are the kill switch the forwarder re-reads
    from its ConfigMap: it forwards only when enabled, and only for a ticket
    whose tenant is listed. All OFF by default.
  EOT
  type = object({
    serve   = optional(bool, false)
    enabled = optional(bool, false)
    tenants = optional(list(string), [])
    # Requests per attempt per minute. A jev step asks once or twice; this
    # stops a loop spending a person's key, not a browser. Not measured.
    requests_per_minute = optional(number, 60)
    # A judgment request carries page state; 2 MiB is well over what
    # jev-browser 0.2.0 sends and well under what would make the forwarder a
    # general upload path. Not measured.
    max_request_bytes = optional(number, 2097152)
    # How long the forwarder waits on TypeSafe before answering 504.
    max_response_seconds = optional(number, 120)
  })
  default = {}

  validation {
    condition     = var.typesafe_forwarder.requests_per_minute > 0 && var.typesafe_forwarder.max_request_bytes > 0 && var.typesafe_forwarder.max_response_seconds > 0
    error_message = "typesafe_forwarder.requests_per_minute, max_request_bytes and max_response_seconds are positive: the forwarder reads a non-positive limit as an invalid setting and fails closed."
  }
}

locals {
  typesafe_fwd_namespace = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  typesafe_fwd_ksa       = "swarm-typesafe-fwd"
  typesafe_fwd_on        = var.enable_typesafe_forwarder && var.enable_gke_autopilot
  typesafe_fwd_serving   = local.typesafe_fwd_on && var.typesafe_forwarder.serve
  typesafe_fwd_image     = lookup(var.image_refs, "swarm-typesafe-fwd", "")

  typesafe_fwd_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${local.typesafe_fwd_namespace}/sa/${local.typesafe_fwd_ksa}"

  typesafe_fwd_key_version_name = local.typesafe_fwd_on ? "projects/${var.project_id}/secrets/${google_secret_manager_secret.typesafe_fwd_key[0].secret_id}/versions/latest" : ""

  # The forwarder's setting. Exactly the keys design §3.5 specifies: an
  # unknown key there is an invalid setting, which fails closed.
  # tests/unit/scripts/test_typesafe_fwd_manifest.py holds the two equal.
  typesafe_fwd_config = {
    enabled              = var.typesafe_forwarder.enabled
    tenants              = var.typesafe_forwarder.tenants
    requests_per_minute  = floor(var.typesafe_forwarder.requests_per_minute)
    max_request_bytes    = floor(var.typesafe_forwarder.max_request_bytes)
    max_response_seconds = floor(var.typesafe_forwarder.max_response_seconds)
  }
  typesafe_fwd_config_json = jsonencode(local.typesafe_fwd_config)

  # swarm-typesafe-fwd-key for swarm-api, BY REFERENCE, once served (a
  # reference to a secret with no version would stop swarm-api starting).
  typesafe_fwd_key_secret_env = local.typesafe_fwd_serving ? {
    SWARM_TYPESAFE_FWD_KEY = {
      secret  = google_secret_manager_secret.typesafe_fwd_key[0].secret_id
      version = "latest"
    }
  } : {}
}

resource "google_secret_manager_secret" "typesafe_fwd_key" {
  count = local.typesafe_fwd_on ? 1 : 0

  project   = var.project_id
  secret_id = "swarm-typesafe-fwd-key"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "typesafe-fwd-key"
  })

  annotations = {
    "swarm-populated-by" = "generated, never shown (docs/design/workspace-secrets.md section 3.5)"
  }
}

# swarm-api mints, the forwarder verifies; nobody else. Authoritative, so a
# grant made out of band is removed on the next apply.
resource "google_secret_manager_secret_iam_binding" "typesafe_fwd_key_accessor" {
  count = local.typesafe_fwd_on ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.typesafe_fwd_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = [
    module.iam.service_account_members["swarm-api"],
    local.typesafe_fwd_principal,
  ]
}

check "typesafe_forwarder_needs_gke" {
  assert {
    condition     = !var.enable_typesafe_forwarder || var.enable_gke_autopilot
    error_message = "enable_typesafe_forwarder is set but enable_gke_autopilot is not: the forwarder is a Deployment on the Autopilot cluster and its principal is that cluster's. Nothing was created."
  }
}

check "typesafe_forwarder_serves_only_when_created" {
  assert {
    condition     = !var.typesafe_forwarder.serve || local.typesafe_fwd_on
    error_message = "typesafe_forwarder.serve is set but enable_typesafe_forwarder (with enable_gke_autopilot) is not: there is no key to reference and nothing to serve."
  }
}

check "typesafe_forwarder_has_an_image" {
  assert {
    condition     = !local.typesafe_fwd_serving || local.typesafe_fwd_image != ""
    error_message = "typesafe_forwarder.serve is set but image_refs has no swarm-typesafe-fwd digest, so there is no manifest to apply. Build the image (lane SEC-INJECT-B) and add its digest first."
  }
}

check "typesafe_forwarder_switch_needs_the_service" {
  assert {
    condition     = !var.typesafe_forwarder.enabled || local.typesafe_fwd_serving
    error_message = "typesafe_forwarder.enabled is set but the forwarder is not served (enable_typesafe_forwarder and typesafe_forwarder.serve): swarm-api mints no ticket and nothing forwards."
  }
}

output "typesafe_forwarder" {
  description = "TypeSafe forwarder wiring: identity, the key's secret name and the rendered setting. Names only; never a value."
  value = {
    created         = local.typesafe_fwd_on
    serving         = local.typesafe_fwd_serving
    namespace       = local.typesafe_fwd_namespace
    service_account = local.typesafe_fwd_ksa
    principal       = local.typesafe_fwd_principal
    key_secret      = local.typesafe_fwd_on ? google_secret_manager_secret.typesafe_fwd_key[0].secret_id : ""
    config          = local.typesafe_fwd_config
    # What an agent pod dials (design §3.5).
    url = "http://swarm-typesafe-fwd.swarm-system.svc.cluster.local:8080"
  }
}

# kubernetes/swarm-system/swarm-typesafe-fwd.yaml with its placeholders filled.
# Null until served with an image, so there is never a half-rendered manifest
# to apply.
output "swarm_typesafe_fwd_manifest" {
  description = "The TypeSafe forwarder's Kubernetes objects, rendered. Apply to swarm-autopilot only (never agents-staging). Null until typesafe_forwarder.serve is set and the image has a digest."
  value = local.typesafe_fwd_serving && local.typesafe_fwd_image != "" ? replace(replace(replace(replace(replace(
    file("${path.module}/../../kubernetes/swarm-system/swarm-typesafe-fwd.yaml"),
    "__SWARM_TYPESAFE_FWD_IMAGE__", local.typesafe_fwd_image),
    "__TYPESAFE_FWD_KEY_VERSION__", local.typesafe_fwd_key_version_name),
    "__PROJECT_ID__", var.project_id),
    "__TYPESAFE_FWD_CONFIG_SHA256__", sha256(local.typesafe_fwd_config_json)),
  "__TYPESAFE_FWD_CONFIG_JSON__", local.typesafe_fwd_config_json) : null
}
