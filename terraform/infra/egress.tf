# Browser egress, the platform side (docs/browser-egress.md §2.2, §3.1, §3.5,
# §6.1, §7.1; lane EGRESS-SVC, part of #1030).
#
# Four things, and only these:
#
#   * `swarm-egress-credential`, the ONE secret that holds the vendor credential
#     (Bright Data, owner decision Q1). Its container and its single accessor
#     are here; its VALUE never is. The owner adds it on stdin (§7.1), so it is
#     in no tfvars file, no plan, no state, no environment and no log;
#   * `swarm-egress-key`, the HMAC key swarm-api mints attempt egress tickets
#     with and swarm-egress verifies them with. Its own key, not
#     swarm-live-key: the forwarder that can verify an egress ticket must not
#     be able to mint a live viewer ticket, nor the reverse;
#   * swarm-api's side of the switch: SWARM_EGRESS_KEY by reference, and the
#     BROWSER_EGRESS_* settings it mints by (swarm_api.egress);
#   * swarm-egress's manifest, kubernetes/swarm-system/swarm-egress.yaml,
#     rendered as `swarm_egress_manifest` with the image, the two secrets'
#     version references and the platform setting.
#
# WHO READS WHAT.
#
#   swarm-egress-credential   swarm-egress's KSA principal, and NOBODY ELSE: not
#                          swarm-api, not the scheduler, not a tenant GSA. A
#                          tenant GSA is the agent's identity (every process in
#                          the pod can mint its token), so a grant there would
#                          hand the credential to the agent (§2.2).
#   swarm-egress-key       swarm-api (mints) and swarm-egress (verifies).
#
# swarm-egress's identity is its Kubernetes service account `swarm-egress` in
# `swarm-system`, as a Workload Identity Federation for GKE principal: nothing
# to impersonate, nothing for the deployer to administer, and a tenant pod in
# swarm-tenant-<t> cannot present it (live.tf says why at more length).
#
# THE OWNER APPLIES THIS. It creates IAM bindings on platform secrets; per the
# lane's brief nothing here is applied by an agent. The order:
#
#   1. `make apply` with enable_browser_egress false: both secret containers
#      exist, nobody reads the credential yet, swarm-api reads the key;
#   2. add the key's first version (generated, never shown) and the vendor
#      credential's (on stdin) -- docs/browser-egress.md §9 has the commands;
#   3. the swarm-egress image is built and its digest is in image_refs;
#   4. `enable_browser_egress = true`, apply: the credential's accessor, the
#      key reference on swarm-api, and the rendered manifest;
#   5. `kubernetes/apply.sh --swarm-system --confirm`, which applies it to
#      swarm-autopilot and refuses the shared cluster agents-staging;
#   6. only then name a tenant in var.browser_egress.tenants and set
#      enabled = true. Steps 1-5 proxy nothing: the setting ships OFF.
#
# GKE ONLY: swarm-egress runs on the Autopilot cluster and its principal lives
# in that cluster's workload pool.

variable "enable_browser_egress" {
  description = <<-EOT
    Deploy swarm-egress (docs/browser-egress.md): give its Kubernetes service
    account read on swarm-egress-credential and swarm-egress-key, put
    SWARM_EGRESS_KEY on swarm-api, and render `swarm_egress_manifest`. Set it
    only after both secrets have a version and the swarm-egress image has a
    digest in image_refs. Routing nothing by itself: var.browser_egress is the
    switch that proxies a tenant. See egress.tf.
  EOT
  type        = bool
  default     = false
}

variable "browser_egress" {
  description = <<-EOT
    The browser egress setting (docs/browser-egress.md §6.1), rendered into
    swarm-api and swarm-egress so the two never disagree. OFF by default: a
    browser attempt is proxied only when `enabled`, its tenant is in
    `tenants` and its profile is in `profiles`. `enabled = false` is the kill
    switch: swarm-egress refuses every CONNECT and closes open tunnels (fail
    closed, owner decision Q7).
  EOT
  type = object({
    enabled  = optional(bool, false)
    country  = optional(string, "us")
    tenants  = optional(list(string), [])
    profiles = optional(list(string), ["claude-code-browser"])
    # Which Bright Data zone in swarm-egress-credential a tenant uses. Phase 1
    # trials both (owner decision Q2), so a tenant can be moved between them.
    default_zone = optional(string, "residential")
    tenant_zones = optional(map(string), {})
    # The owner's pilot budget (decision Q3), per month. Also set as the
    # vendor sub-user's traffic limit, which is the hard stop.
    monthly_cap_usd = optional(number, 150)
    # What one GB costs, to turn the budget into bytes. $8.00 is Bright
    # Data's residential LIST price (§1.2, read 2026-10-14) -- the highest
    # figure in the design -- so swarm-egress stops at or before the budget
    # whatever the plan actually costs. Lower it to the contracted price
    # once the invoice shows it.
    price_per_gb_usd = optional(number, 8)
    # One tenant's share; null means the whole platform cap.
    tenant_monthly_cap_usd = optional(number)
    # §3.5: 1 GB per attempt, ten times the 0.10 GB planning figure.
    attempt_cap_gb = optional(number, 1)
    # New CONNECTs per attempt per minute, and open tunnels per attempt. A page
    # load opens a few dozen; these stop a loop, not a browser.
    connects_per_minute     = optional(number, 600)
    max_tunnels_per_attempt = optional(number, 128)
    # Hosts that go DIRECT (§2.3) in addition to GitHub and the console.
    extra_bypass_hosts = optional(list(string), [])
  })
  default = {}

  validation {
    condition     = can(regex("^[a-z]{2}$", var.browser_egress.country))
    error_message = "browser_egress.country is a two-letter lower-case country code (owner decision Q6: us)."
  }

  validation {
    condition     = var.browser_egress.monthly_cap_usd > 0 && var.browser_egress.price_per_gb_usd > 0 && var.browser_egress.attempt_cap_gb > 0
    error_message = "browser_egress.monthly_cap_usd, price_per_gb_usd and attempt_cap_gb are positive: a zero cap would refuse everything, and swarm-egress reads a non-positive cap as an invalid setting and fails closed."
  }

  validation {
    condition     = var.browser_egress.tenant_monthly_cap_usd == null || coalesce(var.browser_egress.tenant_monthly_cap_usd, 1) > 0
    error_message = "browser_egress.tenant_monthly_cap_usd is positive, or null for the whole platform cap."
  }
}

locals {
  egress_namespace = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  egress_ksa       = "swarm-egress"
  egress_enabled   = var.enable_browser_egress && var.enable_gke_autopilot
  egress_image     = lookup(var.image_refs, "swarm-egress", "")

  egress_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${local.egress_namespace}/sa/${local.egress_ksa}"

  egress_key_version_name        = "projects/${var.project_id}/secrets/${google_secret_manager_secret.egress_key.secret_id}/versions/latest"
  egress_credential_version_name = "projects/${var.project_id}/secrets/${google_secret_manager_secret.browser_egress.secret_id}/versions/latest"

  # §2.3: these go direct, through swarm-nat. GitHub, because a person's
  # sign-in there should come from the address the agent's git uses; the
  # console, because IAP and our own logs should see our NAT address and we
  # should not pay per GB for our own UI.
  egress_bypass_hosts = distinct(concat(
    ["github.com", ".github.com", ".githubusercontent.com"],
    var.frontend_hostname != "" ? [var.frontend_hostname] : [],
    var.browser_egress.extra_bypass_hosts,
  ))

  egress_monthly_cap_bytes = floor(var.browser_egress.monthly_cap_usd / var.browser_egress.price_per_gb_usd * 1000000000)
  egress_tenant_cap_bytes = var.browser_egress.tenant_monthly_cap_usd == null ? local.egress_monthly_cap_bytes : floor(
    coalesce(var.browser_egress.tenant_monthly_cap_usd, 0) / var.browser_egress.price_per_gb_usd * 1000000000
  )

  # swarm-egress's setting (apps/swarm-egress/swarm_egress/config.py). Exactly
  # config.CONFIG_KEYS: an unknown key there is an invalid setting, which
  # fails closed. tests/unit/egress/test_egress_manifest.py holds the keys of
  # this block to that tuple.
  egress_config = {
    enabled                  = var.browser_egress.enabled
    country                  = var.browser_egress.country
    tenants                  = var.browser_egress.tenants
    default_zone             = var.browser_egress.default_zone
    tenant_zones             = var.browser_egress.tenant_zones
    monthly_cap_bytes        = local.egress_monthly_cap_bytes
    tenant_monthly_cap_bytes = local.egress_tenant_cap_bytes
    attempt_cap_bytes        = floor(var.browser_egress.attempt_cap_gb * 1000000000)
    connects_per_minute      = floor(var.browser_egress.connects_per_minute)
    max_tunnels_per_attempt  = floor(var.browser_egress.max_tunnels_per_attempt)
    bypass_hosts             = local.egress_bypass_hosts
  }
  egress_config_json = jsonencode(local.egress_config)

  # swarm-api's side of the same setting (swarm_api.settings). Off unless the
  # service is deployed: a ticket nobody can present is not a route.
  api_browser_egress_env = {
    BROWSER_EGRESS_ENABLED  = local.egress_enabled && var.browser_egress.enabled ? "true" : "false"
    BROWSER_EGRESS_TENANTS  = join(",", var.browser_egress.tenants)
    BROWSER_EGRESS_PROFILES = join(",", var.browser_egress.profiles)
    BROWSER_EGRESS_COUNTRY  = var.browser_egress.country
  }

  # swarm-egress-key for swarm-api, BY REFERENCE, once the service is on (a
  # reference to a secret with no version would stop swarm-api starting).
  egress_key_secret_env = local.egress_enabled ? {
    SWARM_EGRESS_KEY = {
      secret  = google_secret_manager_secret.egress_key.secret_id
      version = "latest"
    }
  } : {}
}

resource "google_secret_manager_secret" "browser_egress" {
  project   = var.project_id
  secret_id = "swarm-egress-credential"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "browser-egress"
  })

  annotations = {
    "swarm-populated-by" = "the owner, on stdin only (docs/browser-egress.md section 9)"
  }
}

# swarm-egress, and NOBODY ELSE. Authoritative, so a grant made out of band is
# removed on the next apply. Absent while the service is off, when nothing may
# read the credential at all.
resource "google_secret_manager_secret_iam_binding" "browser_egress_accessor" {
  count = local.egress_enabled ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.browser_egress.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = [local.egress_principal]
}

resource "google_secret_manager_secret" "egress_key" {
  project   = var.project_id
  secret_id = "swarm-egress-key"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "egress-key"
  })

  annotations = {
    "swarm-populated-by" = "generated, never shown (docs/browser-egress.md section 9)"
  }
}

# swarm-api mints, swarm-egress verifies; nobody else. An identity that could
# read this could mint egress for any attempt.
resource "google_secret_manager_secret_iam_binding" "egress_key_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.egress_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = concat(
    [module.iam.service_account_members["swarm-api"]],
    local.egress_enabled ? [local.egress_principal] : [],
  )
}

check "browser_egress_needs_gke" {
  assert {
    condition     = !var.enable_browser_egress || var.enable_gke_autopilot
    error_message = "enable_browser_egress is set but enable_gke_autopilot is not: swarm-egress is a StatefulSet on the Autopilot cluster and its principal is that cluster's. Nothing was granted."
  }
}

check "browser_egress_has_an_image" {
  assert {
    condition     = !local.egress_enabled || local.egress_image != ""
    error_message = "enable_browser_egress is set but image_refs has no swarm-egress digest, so there is no manifest to apply. Build the image and add its digest first."
  }
}

check "browser_egress_switch_needs_the_service" {
  assert {
    condition     = !var.browser_egress.enabled || local.egress_enabled
    error_message = "browser_egress.enabled is set but swarm-egress is not deployed (enable_browser_egress and enable_gke_autopilot): swarm-api will not mint, and every browser attempt runs direct."
  }
}

output "browser_egress" {
  description = "Browser egress wiring: identity, secret names, the rendered setting and its byte caps. Names and numbers only; never a value."
  value = {
    enabled           = local.egress_enabled
    switch_on         = local.egress_enabled && var.browser_egress.enabled
    namespace         = local.egress_namespace
    service_account   = local.egress_ksa
    principal         = local.egress_principal
    credential_secret = google_secret_manager_secret.browser_egress.secret_id
    key_secret        = google_secret_manager_secret.egress_key.secret_id
    config            = local.egress_config
    # swarm_egress.main.PORT; the gate's upstream is
    # swarm-egress.swarm-system.svc.cluster.local on it.
    port = 3128
  }
}

# kubernetes/swarm-system/swarm-egress.yaml with its placeholders filled. Null
# while the service is off or has no image, so there is never a half-rendered
# manifest to apply.
output "swarm_egress_manifest" {
  description = "swarm-egress's Kubernetes objects, rendered. Apply to swarm-autopilot only (never agents-staging), with kubernetes/apply.sh --swarm-system. Null while enable_browser_egress is off."
  value = local.egress_enabled && local.egress_image != "" ? replace(replace(replace(replace(replace(
    file("${path.module}/../../kubernetes/swarm-system/swarm-egress.yaml"),
    "__SWARM_EGRESS_IMAGE__", local.egress_image),
    "__EGRESS_KEY_VERSION__", local.egress_key_version_name),
    "__EGRESS_CREDENTIAL_VERSION__", local.egress_credential_version_name),
    "__EGRESS_CONFIG_SHA256__", sha256(local.egress_config_json)),
  "__EGRESS_CONFIG_JSON__", local.egress_config_json) : null
}
