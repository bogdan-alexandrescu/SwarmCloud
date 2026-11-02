# ---------------------------------------------------------------------------
# swarm-discovery: GET https://<H>/.well-known/swarmcloud, unauthenticated
# (DAYONE-TF-2; design docs/design/day-one-install.md §9.2 option (c)).
# ---------------------------------------------------------------------------
#
# The plugin needs only the deployment's URL; it fetches this document before
# it holds any credential, to learn the OAuth client id and the API's IAP
# audience. Every other backend on the front door has IAP on, so this one path
# gets its own backend without IAP (modules/frontend, discovery backend and its
# exact path rule) and its own tiny service:
#
#   * the swarm-api IMAGE, so the release adds no image, with the discovery
#     factory as its command (apps/swarm-api/swarm_api/discovery.py, whose only
#     route is that path; docs and OpenAPI off). The image's ENTRYPOINT is
#     tini plus a shell line that starts swarm_api.main, so the command keeps
#     tini as PID 1 and replaces the rest;
#   * ingress internal-and-cloud-load-balancing: reachable only through the
#     load balancer (or the VPC), never on its run.app URL from outside;
#   * the invoker IAM check DISABLED (invoker_iam_disabled), so NO allUsers
#     binding is made and Domain Restricted Sharing is not involved. The org
#     constraint run.managed.requireInvokerIam must be absent or excepted for
#     this project; if it is enforced, the apply fails on this argument and the
#     fallback is the design's option (b), asked of the owner (PR body);
#   * it runs as swarm-discovery (terraform/bootstrap/platform_service_accounts.tf),
#     which holds NO role unless discovery_serves_client_secret is set, when it
#     holds secretAccessor on swarm-iap-desktop-client-secret and nothing else.
#
# Created only once var.discovery names the OAuth client: the app builds the
# document at start-up and refuses to start without each field, so a service
# without them would fail its first revision (and the apply with it).

locals {
  discovery_on = var.enable_frontend && var.frontend_hostname != "" && var.discovery.oauth_client_id != "" && local.image["swarm-api"] != ""

  discovery_name   = "swarm-discovery"
  discovery_email  = "${module.service_account_ids.discovery_id}@${var.project_id}.iam.gserviceaccount.com"
  discovery_member = "serviceAccount:${local.discovery_email}"

  # The Desktop client's secret, created and filled outside Terraform
  # (docs/runbooks/iap-desktop-client.md); named once here.
  discovery_desktop_client_secret = "swarm-iap-desktop-client-secret"
}

# A plan against a project whose bootstrap has not made the account fails
# here, by name, rather than deploying as an account that does not exist.
data "google_service_account" "discovery" {
  count = local.discovery_on ? 1 : 0

  project    = var.project_id
  account_id = module.service_account_ids.discovery_id
}

resource "google_cloud_run_v2_service" "discovery" {
  count = local.discovery_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = local.discovery_name

  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  invoker_iam_disabled = true
  deletion_protection  = var.deletion_protection
  launch_stage         = "GA"

  labels = merge(local.labels, {
    "component" = "discovery"
  })

  template {
    service_account = "${data.google_service_account.discovery[0].account_id}@${var.project_id}.iam.gserviceaccount.com"

    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    max_instance_request_concurrency = 80
    timeout                          = "10s"

    labels = merge(local.labels, {
      "component" = "discovery"
    })

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = local.image["swarm-api"]

      command = ["/usr/bin/tini", "--"]
      args    = ["uvicorn", "--factory", "swarm_api.discovery:create_app", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "DISCOVERY_HOST"
        value = var.frontend_hostname
      }
      env {
        name  = "DISCOVERY_NAME"
        value = var.discovery.name
      }
      env {
        name  = "DISCOVERY_VERSION"
        value = var.discovery.version
      }
      env {
        name  = "DISCOVERY_OAUTH_CLIENT_ID"
        value = var.discovery.oauth_client_id
      }
      env {
        name  = "DISCOVERY_IAP_AUDIENCE"
        value = module.frontend[0].api_iap_audience
      }
      env {
        name  = "DISCOVERY_PLUGIN_MIN_VERSION"
        value = var.discovery.plugin_min_version
      }
      env {
        name  = "DISCOVERY_SERVE_CLIENT_SECRET"
        value = var.discovery_serves_client_secret ? "true" : "false"
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }

  depends_on = [module.project_services]
}

# THE ONE ROLE swarm-discovery may hold, and only when the deployment chooses to
# publish the Desktop client's secret (design §9.3). On the secret, never the
# project.
resource "google_secret_manager_secret_iam_member" "discovery_client_secret" {
  count = var.discovery_serves_client_secret ? 1 : 0

  project   = var.project_id
  secret_id = local.discovery_desktop_client_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = local.discovery_member
}

# Admin › Setup's Sign-in client step (issue #2964): the console takes the
# Desktop client's secret in a form and swarm-api adds it as a new version of
# this one secret. secretVersionAdder only: swarm-api cannot read a version.
# On the secret, never the project; under enable_console_app_setup, which the
# install engine turns on in infra pass 2, after stage 2 made the container
# (`edge.client-secret-container`). The live deployment leaves it off.
resource "google_secret_manager_secret_iam_member" "console_client_secret_adder" {
  count = var.enable_console_app_setup ? 1 : 0

  project   = var.project_id
  secret_id = local.discovery_desktop_client_secret
  role      = "roles/secretmanager.secretVersionAdder"
  member    = module.iam.service_account_members["swarm-api"]
}
