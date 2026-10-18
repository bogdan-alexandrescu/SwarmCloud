# The acceptance group's echo MCP server (lane MCP-ACCEPT, part of #1433;
# docs/design/registered-mcp-servers.md section 6, owner decision Q10 of
# 2026-10-17): a DEV-ONLY Cloud Run service that scales to zero, reached over
# public HTTPS through a task's relay exactly as a caller's own server is.
# scripts/acceptance/groups/mcp.sh dispatches a claude-code task that names it.
#
# Four things, and only these:
#
#   * the service `swarm-mcp-echo`, ingress ALL, min 0 instances: idle costs
#     nothing (invariant 1). It runs tests/acceptance/mcp_echo_server/server.py
#     from the swarm-verify image, which carries it, on that image's promoted
#     digest -- no new image, no new build. The server holds nothing: no
#     store, no secret, and it answers any bearer with only that bearer's own
#     hash (and the bearer itself, on purpose, for the relay to scrub);
#   * allUsers -> run.invoker on THIS service: the relay is a worker in a
#     tenant pod, and a caller's server is reached without a Google identity,
#     so this one must be too. The relay's checks (public address, 443, no
#     redirect) are what the acceptance group exercises;
#   * its own service account, swarm-mcp-echo, holding NO role anywhere. A
#     public service never runs as an account that can read anything: not
#     swarm-verify's (it reads Firestore), and never the default compute
#     account;
#   * the CI deployer's actAs on that account, which deploying a service that
#     runs as it requires (deployer.tf says why it is per account).
#
# OFF BY DEFAULT, AND ONLY EVER IN DEV. `mcp_test_server` is "off" by
# default, and the variable refuses anything else outside dev. No tfvars file
# sets it, so merging this creates and changes nothing. THREE STEPS, because
# the deployer can create an account (serviceAccountCreator) but can act as
# one only once terraform/bootstrap -- which the owner applies -- has granted
# it roles/iam.serviceAccountAdmin on that account, and bootstrap can grant on
# an account only after it exists (docs/ci.md, "The deployer's
# service-account grants"):
#
#   1. mcp_test_server = "account" in dev.tfvars: swarm-mcp-echo exists,
#      holding nothing; nothing runs as it.
#   2. the owner adds swarm-mcp-echo to the accounts bootstrap grants the
#      deployer admin on (terraform/modules/service_account_ids; this lane's
#      PR asks for it) and applies bootstrap.
#   3. mcp_test_server = "on": the actAs binding, the service and its public
#      invoker. Before step 2 the binding is a 403 at apply.
#
# Steps 1 and 3 change IAM, so a dev release making either waits at the
# dev-iam gate for the owner (docs/ci.md).
#
# THEN, ONCE: register it in the smoke tenant (an admin's act, owner decision
# Q1) from `terraform output mcp_test_server`:
#
#   POST /v1/mcp-servers {"name": "acc-echo", "url": "<url>",
#        "auth": {"mode": "dispatch"}, "allowed_profiles": ["claude-code"]}

variable "mcp_test_server" {
  description = <<-EOT
    The acceptance group's echo MCP server, swarm-mcp-echo
    (terraform/infra/mcp_test_server.tf; owner decision Q10 of #1433's design).
    "off" (the default): nothing. "account": its service account only, so
    terraform/bootstrap can grant the deployer admin on it. "on": the account,
    the deployer's actAs on it, the service and its public invoker -- only
    after bootstrap has granted that admin. Dev only.
  EOT
  type        = string
  default     = "off"

  validation {
    condition     = contains(["off", "account", "on"], var.mcp_test_server)
    error_message = "mcp_test_server is \"off\", \"account\" or \"on\"."
  }

  validation {
    condition     = var.mcp_test_server == "off" || var.environment == "dev"
    error_message = "mcp_test_server is dev only (owner decision Q10): the echo MCP server is a public test endpoint, and staging and prod never run one."
  }
}

locals {
  mcp_test_server_name    = "swarm-mcp-echo"
  mcp_test_server_account = var.mcp_test_server != "off" && var.environment == "dev"
  mcp_test_server_on      = var.mcp_test_server == "on" && var.environment == "dev"
  # The swarm-verify image carries the server (images/swarm-verify/Dockerfile),
  # so the service is on that image's promoted digest and nothing new is built.
  # Never empty in a plan that succeeds: the swarm-verify job's precondition
  # (verify.tf) refuses any plan without that digest, so no check of its own.
  mcp_test_server_image = local.image["swarm-verify"]
  # Where the image puts it, and the one path the server answers MCP on.
  mcp_test_server_entry = "/swarm/tests/acceptance/mcp_echo_server/server.py"
  mcp_test_server_path  = "/mcp"
  # Spelled, not read from the account, so the plan knows it.
  mcp_test_server_email = "${local.mcp_test_server_name}@${var.project_id}.iam.gserviceaccount.com"
}

resource "google_service_account" "mcp_test_server" {
  count = local.mcp_test_server_account ? 1 : 0

  project      = var.project_id
  account_id   = local.mcp_test_server_name
  display_name = "Swarm MCP echo (acceptance, dev only)"
  description  = "The acceptance group's public echo MCP server runs as this account. It holds no role anywhere. managed-by=swarm-terraform"
}

# Deploying a service that runs as the account needs actAs on it (deployer.tf).
resource "google_service_account_iam_member" "mcp_test_server_deployer_acts_as" {
  count = local.mcp_test_server_on && local.deployer_member != "" ? 1 : 0

  service_account_id = google_service_account.mcp_test_server[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member
}

resource "google_cloud_run_v2_service" "mcp_test_server" {
  count = local.mcp_test_server_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = local.mcp_test_server_name

  # Public, as a caller's server is: the relay in a tenant pod reaches it
  # through the swarm NAT with no Google identity.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection
  launch_stage        = "GA"

  labels = merge(local.labels, {
    "component" = "mcp-test-server"
  })

  # The account exists before a revision names it.
  depends_on = [google_service_account.mcp_test_server]

  template {
    service_account = local.mcp_test_server_email

    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    # One whoami call per acceptance run; each answers in milliseconds.
    max_instance_request_concurrency = 10
    timeout                          = "30s"

    labels = merge(local.labels, {
      "component" = "mcp-test-server"
    })

    scaling {
      # Idle costs nothing (invariant 1). One instance: the group makes one
      # call, and a flood from the internet gets no more capacity than that.
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image   = local.mcp_test_server_image
      command = ["python3"]
      args    = [local.mcp_test_server_entry]

      ports {
        container_port = 8080
      }

      resources {
        # requests == limits, no bursting (CONTRACT.md invariant 7):
        # modules/cloud_run says why both flags are off.
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }

        cpu_idle          = false
        startup_cpu_boost = false
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }
        initial_delay_seconds = 1
        period_seconds        = 3
        timeout_seconds       = 2
        failure_threshold     = 10
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
}

# allUsers -> run.invoker on THIS service only.
resource "google_cloud_run_v2_service_iam_member" "mcp_test_server_public" {
  # checkov:skip=CKV_GCP_102:the dev-only acceptance echo MCP server (owner decision Q10 of #1433's design); a caller's MCP server is reached with no Google identity, and this one holds nothing.
  count = local.mcp_test_server_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp_test_server[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "mcp_test_server" {
  description = "The acceptance group's echo MCP server (dev only): whether it exists, the URL to register in the smoke tenant as a dispatch server, and the identity it runs as. Names only; never a value."
  value = {
    created         = local.mcp_test_server_on
    service         = local.mcp_test_server_on ? local.mcp_test_server_name : ""
    service_account = local.mcp_test_server_account ? local.mcp_test_server_email : ""
    url             = local.mcp_test_server_on ? "${google_cloud_run_v2_service.mcp_test_server[0].uri}${local.mcp_test_server_path}" : ""
  }
}
