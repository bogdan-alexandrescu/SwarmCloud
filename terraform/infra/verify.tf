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
  # 256 characters at most -- the provider refuses a longer description at
  # plan time, which is how an earlier wording of this line failed CI. This one
  # is 245. It names what the identity CAN do (any runner ceiling, through
  # admin_pool_users) rather than what race-test does with it (mock's), and it
  # says it is not an admin: the wording before 2026-09-24's correction said it
  # was one.
  description = "Runs the verification suites inside the VPC. Reads Firestore, Cloud Run executions and artifact objects. Writes only through swarm-api: as its own tenant, and runner ceilings where admin_pool_users names it (for race-test). Not a platform admin."
}

# WHY THIS IS NO LONGER THE ONLY GRANT.
#
# The comment that stood here said this identity "has no Firestore access, no
# secret access and no admin group", and treated that as the design. It was
# true and it made the gate impossible: the suites do NOT work only through the
# public API. scripts/lib/testlib.sh reads pool and task documents with fs_get
# and fs_count, counts Cloud Run executions for invariant 1, and counts GCS
# objects to prove artifacts landed under the tenant's own prefix. With
# run.invoker alone, require_platform died on its opening guard before a single
# assertion ran -- twice in two days, for two different reasons.
#
# So the grants below are the ones the suites actually exercise, and no more.
# All three are READ-ONLY at the project level; everything the gate writes, it
# writes through swarm-api, which is the property the original comment was
# protecting and which still holds.
#
# NOT EVERY WRITE IS AS ITS OWN TENANT, by owner decision on 2026-09-24: in dev
# this identity is also on `admin_pool_users`, because race-test narrows
# runner:mock with PUT /v1/admin/limits/runner/mock rather than through a
# Firestore write role. That list reaches an allow-list of admin routes in
# swarm-api (swarm_api.auth.POOL_ADMIN_ROUTES) holding that one route; every
# other /v1/admin route, tenant disable included, answers it 403. It is NOT a
# platform admin: the first form of the decision put it in `admin_users`, and
# the owner reversed that the same day. It is an application-level list in
# tfvars, not IAM, and it is deliberately not granted here -- this file is the
# same in every environment, and the gate has no reason to hold it in prod.
# docs/audits/2026-09-22/race-test-needs-a-write.md has the comparison.
#
# Secret access is deliberately still absent. Nothing in the suites reads a
# secret, and the mock runner profile this tenant uses needs no provider key.
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

# Firestore. require_fs_database asks the Admin API whether the database
# exists, then every suite reads task, lease and pool documents directly.
# roles/datastore.viewer, not .user: the suites read documents and never write
# one -- task creation goes through swarm-api, which holds its own write role.
resource "google_project_iam_member" "verify_reads_firestore" {
  project = var.project_id
  role    = "roles/datastore.viewer"
  member  = "serviceAccount:${google_service_account.verify.email}"
}

# Cloud Run executions. CONTRACT invariant 1 -- that QUEUED, PARKED and READY
# create no infrastructure demand -- is asserted by counting executions, so a
# gate that cannot list them cannot check the invariant it exists for.
resource "google_project_iam_member" "verify_reads_run" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.verify.email}"
}

# Artifact objects. The smoke suite proves artifacts landed under the tenant's
# OWN GCS prefix, which is per-tenant isolation (invariant 9) and cannot be
# checked without listing them. Scoped to the artifact bucket, not the project:
# this project is SHARED with another team and a project-level storage role
# would reach their buckets.
resource "google_storage_bucket_iam_member" "verify_reads_artifacts" {
  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.verify.email}"
}

resource "google_cloud_run_v2_job" "verify" {
  project  = var.project_id
  location = var.region
  name     = "swarm-verify"

  labels = merge(local.labels, { component = "verify" })

  # Mirrors what the worker jobs and services do (both modules take
  # var.deletion_protection). Omitting it meant the provider's default of
  # `true` applied, and the first apply after a FAILED create could not
  # replace the half-made job: "cannot destroy job without setting
  # deletion_protection=false". A gate that cannot be redeployed after a bad
  # build is a gate that stays broken.
  deletion_protection = var.deletion_protection

  template {
    template {
      service_account = google_service_account.verify.email
      max_retries     = 0 # A flaky gate that retries is a gate that lies.
      timeout         = "1800s"

      vpc_access {
        # ALL_TRAFFIC, and the reasoning that put PRIVATE_RANGES_ONLY here was
        # wrong in a way worth recording.
        #
        # It argued that this job "talks to swarm-api and to the metadata
        # server, both reachable without routing egress through Cloud NAT".
        # The address it talks to swarm-api on is
        # module.cloud_run.service_urls["swarm-api"] -- a PUBLIC *.run.app
        # hostname (see the API_URL env below). A public hostname is not in a
        # private range, so under PRIVATE_RANGES_ONLY the request never left
        # the VPC. This repository had already measured that exact refusal from
        # the other direction and written it down in
        # docs/audits/2026-09-20/verification-targets-cannot-run.md.
        #
        # swarm-api's ingress is INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER, which
        # admits traffic arriving from within the VPC network -- but the packet
        # has to be routed there first, and that is what this setting decides.
        egress = "ALL_TRAFFIC"

        network_interfaces {
          network    = module.network.network_id
          subnetwork = module.network.subnetwork_id
        }
      }

      containers {
        image = local.image["swarm-verify"]

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

  lifecycle {
    ignore_changes = [client, client_version]

    # EVERY IMAGE THIS ROOT DEPLOYS HAS A DIGEST, OR NOTHING PLANS.
    #
    # It is checked HERE, on the one image consumer declared in this root,
    # because a module call cannot carry a precondition -- and a failed
    # precondition fails the whole plan, so no service and no worker job is
    # planned either. The alternative, a fall back to a tag for a missing
    # entry, is exactly how a tag used to get deployed without anybody
    # choosing it (see var.image_refs).
    #
    # A precondition, not a validation on var.image_refs, for one case: a
    # FRESH project has no images yet, so its first plan targets the registry
    # alone (scripts/plan.sh does this when there is nothing to pin), and a
    # targeted plan does not evaluate this resource. A variable validation would
    # refuse that plan too, leaving no way to create the registry the first
    # images are pushed to.
    precondition {
      condition     = length(local.images_without_a_digest) == 0
      error_message = "image_refs has no digest for: ${join(", ", local.images_without_a_digest)}. Nothing is deployed at a tag. Plan through scripts/plan.sh (which pins what terraform last applied) or deploy through scripts/lib/deploy.sh (which pins the promotion manifest); both write image_refs with scripts/lib/image-refs.sh."
    }
  }
}

output "verify_job_name" {
  description = "Cloud Run job that runs the verification gate from inside the VPC."
  value       = google_cloud_run_v2_job.verify.name
}
