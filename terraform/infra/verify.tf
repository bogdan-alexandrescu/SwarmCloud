# ---------------------------------------------------------------------------
# swarm-verify -- the verification gate, running INSIDE the VPC
# ---------------------------------------------------------------------------
#
# >>> TRACK OWNERSHIP: this file was written by TRACK D (operations), not by
# >>> Track C, who own terraform/. It is flagged here rather than buried
# >>> because CLAUDE.md says to change your own side and REPORT the conflict.
# >>> Track C should take ownership of it or tell Track D to move it.
# >>> Rationale and evidence: docs/audits/2026-09-20/verification-targets-cannot-run.md
#
# WHY THIS EXISTS
#
# CLAUDE.md requires `make smoke concurrency-test race-test` against a deployed
# environment before claiming a change to admission, dispatch or reconciliation
# is done. On 2026-09-20 all three were found unable to reach the API at all.
#
# Nothing was broken. swarm-api runs with ingress
# `internal-and-cloud-load-balancing`, which is correct and deliberate -- it is
# what puts the service behind the external ALB and IAP -- so the `*.run.app`
# address the scripts resolve to refuses a laptop by design. The gate had
# quietly stopped being a gate, and it failed in a way that reads as an API
# problem, which is why nobody noticed.
#
# The alternative was to give the scripts an identity that can walk through
# IAP: an OAuth client id plus a service account holding
# roles/iap.httpsResourceAccessor. That works from a laptop, does not work from
# CI, and mints a credential whose only purpose is to bypass the front door.
# Running the tests from inside the VPC needs no client, no secret, and behaves
# identically in CI -- the ingress setting already permits internal traffic.

resource "google_service_account" "verify" {
  project      = var.project_id
  account_id   = "swarm-verify"
  display_name = "SwarmCloud verification job"
  description  = "Runs the smoke, concurrency and race targets from inside the VPC. Holds run.invoker on swarm-api and nothing else."
}

# The ONLY grant this identity gets. It submits and reads tasks through the
# public API surface exactly as a user would; it has no Firestore access, no
# secret access and no admin group.
resource "google_cloud_run_v2_service_iam_member" "verify_invokes_api" {
  project  = var.project_id
  location = var.region
  # `service_names` is a sorted LIST of keys, not a map -- indexing it by
  # string is the error terraform test caught here. The service name is the
  # map key itself, so take it from the map-shaped output.
  name   = [for k, _ in module.cloud_run.service_ids : k if k == "swarm-api"][0]
  role   = "roles/run.invoker"
  member = "serviceAccount:${google_service_account.verify.email}"
}

resource "google_cloud_run_v2_job" "verify" {
  project  = var.project_id
  location = var.region
  name     = "swarm-verify"

  labels = merge(local.labels, { component = "verify" })

  template {
    template {
      service_account = google_service_account.verify.email
      max_retries     = 0 # A flaky gate that retries is a gate that lies.
      timeout         = "1800s"

      vpc_access {
        # PRIVATE_RANGES_ONLY, not ALL_TRAFFIC: this job talks to swarm-api and
        # to the metadata server, both of which are reachable without routing
        # its whole egress through Cloud NAT. The worker jobs use ALL_TRAFFIC
        # because provider calls must carry the NAT's reserved addresses; this
        # one calls no provider.
        egress = "PRIVATE_RANGES_ONLY"

        network_interfaces {
          network    = module.network.network_id
          subnetwork = module.network.subnetwork_id
        }
      }

      containers {
        image = "${local.image_base}/swarm-verify:${var.image_tag}"

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        # The service's own URL, for both the request and the ID token
        # audience -- a Cloud Run ID token's audience IS the service URL.
        # Without these the scripts fall back to `gcloud run services
        # describe`, and there is deliberately no gcloud in this image.
        env {
          name  = "API_URL"
          value = module.cloud_run.service_urls["swarm-api"]
        }
        env {
          name  = "API_AUDIENCE"
          value = module.cloud_run.service_urls["swarm-api"]
        }
        env {
          name  = "PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "REGION"
          value = var.region
        }
        env {
          name  = "ENVIRONMENT"
          value = var.environment
        }
      }
    }
  }

  # The image tag moves on every deploy and the job is executed on demand, so
  # an in-flight execution must not be interrupted by an apply.
  lifecycle {
    ignore_changes = [client, client_version]
  }
}

output "verify_job_name" {
  description = "Cloud Run job that runs the verification gate from inside the VPC."
  value       = google_cloud_run_v2_job.verify.name
}
