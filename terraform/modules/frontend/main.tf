# The front door. An external Application Load Balancer, with IAP, in front of
# swarm-api.
#
# WHY THIS DID NOT EXIST, AND WHY IT IS NOT A BIG CHANGE
# -----------------------------------------------------
# It was widely believed in this repository that swarm-api could not be reached
# from a browser because its ingress setting forbade it. That is not so.
# `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` is precisely the setting an external
# Application Load Balancer in front of Cloud Run requires, and the module's
# validation only refuses `INGRESS_TRAFFIC_ALL`. `allUsers` already holds
# run.invoker on swarm-api, deliberately and on measured evidence (see
# `api_invokers` in infra/variables.tf: Cloud Run's edge IAM CONSUMES the
# caller's Authorization header, which destroys the only credential that can
# identify a tenant).
#
# So nothing about the existing services changes here. The front door was never
# forbidden. It was simply never built.
#
# WHAT PROTECTS WHAT
# ------------------
# IAP is the outer gate: it decides who may reach the backend at all. It is NOT
# the tenant boundary. swarm-api verifies the Google ID token itself, enforces
# ALLOWED_DOMAINS, resolves Cloud Identity group membership and scopes every
# read to the caller's own tenant inside the store. That ordering matters: a
# design where the UI holds a privileged identity and filters by tenant in
# application code would move invariant 9 into new code, and an audit on
# 2026-09-18 found the existing API already gets that wrong on its read paths.
#
# IAP forwards the caller's identity in `x-goog-iap-jwt-assertion` and, because
# the backend is Cloud Run, ALSO passes the original Authorization header
# through. swarm-api reads both (deps.py prefers X-Serverless-Authorization),
# so no application change is required.
#
# THE IAP BRAND IS NOT MANAGED HERE, DELIBERATELY
# -----------------------------------------------
# IAP needs an OAuth brand (the consent screen) to exist on the project. It is
# not created here because `google_iap_brand` CANNOT BE DELETED -- terraform can
# create one and then has no way to remove it, so `make destroy` would leave a
# resource behind forever and every subsequent destroy rehearsal would have to
# special-case it. A module that cannot be cleanly destroyed in a project
# holding another team's production is not worth the convenience.
#
# It is a once-per-project, one-line manual step, and the first apply fails
# loudly without it rather than doing something subtle:
#
#   gcloud iap oauth-brands create \
#     --application_title="SwarmCloud" \
#     --support_email=<an owner of this project, or a group they own> \
#     --project=<project>
#
# Google requires the support email to be the address of the caller or a group
# they own, and rejects anything else with an error that says neither.
#
# DNS IS NOT MANAGED HERE
# -----------------------
# saga.xyz is at an external registrar. Terraform reserves a global static IP
# and outputs it; the A record is added by hand. Until it resolves, the managed
# certificate sits in PROVISIONING and the host serves a TLS error. That is
# normal, takes up to an hour, and looks exactly like a broken deploy -- which
# is why it is written here and in the module's outputs rather than left to be
# rediscovered at the worst moment.

locals {
  # Compute LB resources are not labelable (see scripts/lib/unlabelable-types.json),
  # so the name prefix is the only thing tying them to this platform. Keep it.
  name = "${var.name_prefix}-ui"
}

# --------------------------------------------------------------------------
# Address
# --------------------------------------------------------------------------

# Reserved, not ephemeral: the A record an operator adds by hand must not change
# under them on a later apply.
resource "google_compute_global_address" "this" {
  project = var.project_id
  name    = "${local.name}-ip"

  labels = var.labels
}

# --------------------------------------------------------------------------
# Backend: a serverless NEG pointing at the Cloud Run service
# --------------------------------------------------------------------------

resource "google_compute_region_network_endpoint_group" "this" {
  project               = var.project_id
  name                  = "${local.name}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = var.service_name
  }
}

resource "google_compute_backend_service" "this" {
  project     = var.project_id
  name        = "${local.name}-backend"
  description = "managed-by=swarm-terraform; external ALB in front of ${var.service_name}"

  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"

  # No health check. A serverless NEG has no health checking -- Cloud Run
  # manages that itself -- and terraform errors if one is attached.

  backend {
    group = google_compute_region_network_endpoint_group.this.id
  }

  # NO timeout_sec. A backend service fronting a SERVERLESS NEG rejects it
  # outright -- "Timeout sec is not supported for a backend service with
  # Serverless network endpoint groups" -- because the request timeout belongs to
  # the Cloud Run service, which already has its own (`request_timeout` in
  # modules/cloud_run, 300s for swarm-api). Setting it here would have been a
  # second, quieter copy of a value that already exists somewhere authoritative.

  log_config {
    enable      = true
    sample_rate = 1.0
  }

  iap {
    enabled = true
    # No oauth2_client_id/secret: with provider 6.x and an internal brand, IAP
    # uses a Google-managed OAuth client. Supplying one means owning a client
    # secret in state, which this repository refuses to do for the same reason
    # it refuses managed secret versions.
  }
}

# --------------------------------------------------------------------------
# The UI backend
# --------------------------------------------------------------------------
# A second Cloud Run service rather than static files inside swarm-api: the UI
# ships far more often than the API, and coupling them means a button colour
# redeploys the service that holds the tenant boundary.
#
# It sits behind the SAME IAP gate. A static bundle is not secret, but an
# unauthenticated UI that then fails every API call is a worse experience than
# one sign-in, and it keeps a single answer to "who may reach this host".

locals {
  ui_enabled = var.ui_service_name != ""
}

resource "google_compute_region_network_endpoint_group" "ui" {
  count = local.ui_enabled ? 1 : 0

  project               = var.project_id
  name                  = "${local.name}-ui-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = var.ui_service_name
  }
}

resource "google_compute_backend_service" "ui" {
  count = local.ui_enabled ? 1 : 0

  project     = var.project_id
  name        = "${local.name}-ui-backend"
  description = "managed-by=swarm-terraform; static web UI behind the same IAP gate as the API"

  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"

  backend {
    group = google_compute_region_network_endpoint_group.ui[0].id
  }

  log_config {
    enable = true
    # The UI is chatty by comparison and carries no tenant data, so it is
    # sampled rather than fully logged. The API keeps sample_rate 1.0.
    sample_rate = 0.1
  }

  iap {
    enabled = true
  }
}

resource "google_iap_web_backend_service_iam_member" "ui_members" {
  for_each = local.ui_enabled ? toset(var.iap_members) : toset([])

  project             = var.project_id
  web_backend_service = google_compute_backend_service.ui[0].name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

# --------------------------------------------------------------------------
# Who may pass IAP
# --------------------------------------------------------------------------

resource "google_iap_web_backend_service_iam_member" "members" {
  for_each = toset(var.iap_members)

  project             = var.project_id
  web_backend_service = google_compute_backend_service.this.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

# --------------------------------------------------------------------------
# Routing and TLS
# --------------------------------------------------------------------------

# The DEFAULT is the UI and the API is matched explicitly, not the other way
# round. A path this map does not know is a UI route -- React owns the client
# side -- whereas defaulting to the API would answer an unknown page with a JSON
# 404 from a service that never meant to serve it.
#
# With no UI backend the default is the API, which is how an API-only
# deployment behaves.
resource "google_compute_url_map" "this" {
  project         = var.project_id
  name            = "${local.name}-urlmap"
  default_service = local.ui_enabled ? google_compute_backend_service.ui[0].id : google_compute_backend_service.this.id

  dynamic "host_rule" {
    for_each = local.ui_enabled ? [1] : []
    content {
      hosts        = ["*"]
      path_matcher = "main"
    }
  }

  dynamic "path_matcher" {
    for_each = local.ui_enabled ? [1] : []
    content {
      name            = "main"
      default_service = google_compute_backend_service.ui[0].id

      # Everything the API owns. /healthz and /readyz are the API's, not the
      # UI's -- the UI has its own at the same path inside nginx, and a health
      # check that silently answers from the wrong service is worthless.
      path_rule {
        paths   = ["/v1", "/v1/*", "/healthz", "/readyz", "/metrics", "/docs", "/openapi.json"]
        service = google_compute_backend_service.this.id
      }
    }
  }
}

resource "google_compute_managed_ssl_certificate" "this" {
  project = var.project_id
  name    = "${local.name}-cert"

  managed {
    domains = [var.hostname]
  }

  # A managed certificate cannot be updated in place: changing the domain
  # replaces it, and the replacement is PROVISIONING for up to an hour. Create
  # the new one before destroying the one currently serving traffic.
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_target_https_proxy" "this" {
  project          = var.project_id
  name             = "${local.name}-https-proxy"
  url_map          = google_compute_url_map.this.id
  ssl_certificates = [google_compute_managed_ssl_certificate.this.id]
}

resource "google_compute_global_forwarding_rule" "https" {
  project    = var.project_id
  name       = "${local.name}-https"
  target     = google_compute_target_https_proxy.this.id
  port_range = "443"
  ip_address = google_compute_global_address.this.id

  load_balancing_scheme = "EXTERNAL_MANAGED"

  labels = var.labels
}

# --------------------------------------------------------------------------
# Port 80: redirect only
# --------------------------------------------------------------------------
# IAP requires HTTPS. Without this, http:// is a connection refused rather than
# a redirect, which reads as "the site is down" to anyone who omits the scheme --
# which is everyone.

resource "google_compute_url_map" "redirect" {
  project = var.project_id
  name    = "${local.name}-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  project = var.project_id
  name    = "${local.name}-http-proxy"
  url_map = google_compute_url_map.redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  project    = var.project_id
  name       = "${local.name}-http"
  target     = google_compute_target_http_proxy.redirect.id
  port_range = "80"
  ip_address = google_compute_global_address.this.id

  load_balancing_scheme = "EXTERNAL_MANAGED"

  labels = var.labels
}
