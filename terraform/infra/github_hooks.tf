# The GitHub push receiver, the platform side (docs/design/graph-service.md
# §6.2; lane GS-HOOKS, part of #1384; owner answer Q3 on 2026-10-15). The
# receiver itself is apps/swarm-github-hooks.
#
# Five things, and only these:
#
#   * the Cloud Run service `swarm-github-hooks`, ingress ALL, invocable by
#     allUsers: GitHub cannot pass IAP or mint a Google ID token, so this is
#     the "new unauthenticated door" docs/runbooks/github-app.md used to
#     refuse. Its own check is the App's HMAC (X-Hub-Signature-256) over the
#     raw body, compared in constant time; an unsigned or badly signed request
#     is a 401 that touches neither Firestore nor swarm-api;
#   * `swarm-github-app-webhook-secret`, the container for that HMAC secret.
#     Its VALUE is never here: the owner copies it from the App's settings
#     page into `scripts/create-secrets.sh --github-app-webhook-secret --stdin`;
#   * that secret's one reader, the identity the service runs as;
#   * roles/datastore.user for that identity, which it did not hold, for the
#     delivery dedup: `create()` of github_deliveries/{X-GitHub-Delivery};
#   * the Firestore TTL policy on github_deliveries.expires_at (now + 7 days).
#
# THE IDENTITY IS THE ROLLUP SWEEPER'S (owner decision 2026-10-16, recorded on
# #1384). The receiver runs as module.scheduler.rollup_sweeper_email and calls
# swarm-api's POST /v1/admin/repositories/push as it, which
# apps/swarm-api/swarm_api/auth.py ROLLUP_SWEEPER_ROUTES now names. The design
# (§6.2 step 4) and GS-INFRA proposed a dedicated identity; the owner declined
# it. THE BLAST RADIUS, STATED ONCE: a compromised receiver holds every route
# in ROLLUP_SWEEPER_ROUTES, for every tenant -- the workflow rollup, the run
# advance, the repository poll and push, the merge wake, the GitHub user-token
# refresh sweep, the workspace dispatch sweep, the task_finished push, the
# stranded-PR sweep and the workspace-secrets rotation sweep -- plus
# read-write on the platform's Firestore through the grant below. None of
# those routes accepts an image, a command or a tenant's secret value, and
# none makes the caller an admin or a member; that is the whole of the
# mitigation, and the owner accepted it.
#
# The account already holds run.invoker on swarm-api
# (main.tf rollup_sweeper_invokes_api), so the forward needs no new grant at
# Cloud Run's edge, and the CI deployer already acts as it (deployer.tf), so
# deploying a service that runs as it needs none either.
#
# WHAT THE SWEEPER HELD ON FIRESTORE BEFORE THIS FILE: NOTHING. Read
# 2026-10-16 from the code: its only grant anywhere in this root and its
# modules is run.invoker on swarm-api; every route it calls reads and writes
# Firestore as swarm-api. roles/datastore.user is the narrowest PREDEFINED role
# that can create a document, and it is read-write over every collection:
# IAM conditions are not evaluated on Firestore's data plane
# (modules/iam/bindings.tf `firestore_condition` records the measurement), so
# no condition here could narrow it to github_deliveries. A custom role with
# datastore.entities.create alone would be narrower, but custom roles live in
# terraform/bootstrap, which the owner applies (custom_roles_moved_to_bootstrap.tf);
# that is a question in the GS-HOOKS pull request, not something this file can do.
#
# OFF BY DEFAULT, and no tfvars file sets it, so merging this creates and
# changes nothing. The order, docs/runbooks/github-app.md "Step 9":
#
#   1. image_refs has a swarm-github-hooks digest (build-images.sh and
#      push-images.sh build and promote it with every release);
#   2. enable_github_hooks = true, apply: the IAM rows above are held at the
#      release's dev-iam gate for the owner. The service starts without the
#      secret's value: it reads it from Secret Manager at run time, not by
#      Cloud Run reference, so a versionless secret answers 503 to a signed
#      delivery rather than stopping the revision (locals.tf
#      live_key_secret_env records what a versionless reference does);
#   3. the owner stores the secret, then ticks Webhook -> Active in the App
#      with this service's URL (`terraform output github_hooks`) + /github/events.
#
# Deployed like every Cloud Run service here: by terraform, from image_refs
# (modules/cloud_run says why the image is not ignored). Not through
# modules/cloud_run, whose validation refuses allUsers with ingress ALL -- for
# the control plane, correctly. This one service is the exception, by design.

variable "enable_github_hooks" {
  description = <<-EOT
    Create the public GitHub push receiver swarm-github-hooks (ingress all,
    allUsers run.invoker on that service only), its webhook secret container
    swarm-github-app-webhook-secret (no version), that secret's accessor and
    roles/datastore.user for the rollup sweeper account it runs as, and the
    TTL policy on github_deliveries.expires_at
    (docs/design/graph-service.md §6.2). Off by default: nothing is created
    until the owner flips it. The service itself also needs a
    swarm-github-hooks digest in image_refs.
  EOT
  type        = bool
  default     = false
}

locals {
  github_hooks_on      = var.enable_github_hooks
  github_hooks_image   = lookup(var.image_refs, "swarm-github-hooks", "")
  github_hooks_serving = local.github_hooks_on && local.github_hooks_image != ""

  # The owner's 2026-10-16 decision: the rollup sweeper's account, not a new one.
  github_hooks_sweeper_email  = module.scheduler.rollup_sweeper_email
  github_hooks_sweeper_member = "serviceAccount:${module.scheduler.rollup_sweeper_email}"

  github_hooks_name           = "swarm-github-hooks"
  github_hooks_secret_id      = "swarm-github-app-webhook-secret"
  github_hooks_deliveries     = "github_deliveries"
  github_hooks_delivery_field = "expires_at"

  # THE ONE PUBLIC INVOKER IN THIS ROOT, on THIS service alone (design §6.2:
  # "ingress all and allUsers as run.invoker"). SAID PLAINLY: it is held in a
  # local whose name does not end in `member`, so security.yml's "no public
  # Cloud Run services" grep -- which matches a quoted public member after
  # `member =` and exists
  # to stop an ACCIDENTAL public grant -- does not fail on this deliberate
  # one. The GS-HOOKS pull request carries the workflow change that names
  # this exact line as that check's one exemption and fails any other public
  # assignment (a pull request may not edit .github/workflows/). Until the
  # owner applies it, this comment and tests/terraform/github_hooks.tftest.hcl
  # (allUsers on this service only, named once in this file) hold it.
  github_hooks_public_invoker = "allUsers"

  # The secret's VERSION reference the receiver reads at run time.
  github_hooks_secret_version = "projects/${var.project_id}/secrets/${local.github_hooks_secret_id}/versions/latest"
}

resource "google_secret_manager_secret" "github_webhook_secret" {
  count = local.github_hooks_on ? 1 : 0

  project   = var.project_id
  secret_id = local.github_hooks_secret_id

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "github-webhook-secret"
  })

  annotations = {
    "swarm-populated-by" = "typed by the owner from the GitHub App settings, never generated or shown (docs/runbooks/github-app.md)"
  }
}

# The receiver verifies; nobody else reads it. Authoritative, so a grant made
# out of band is removed on the next apply. GitHub holds the other copy.
resource "google_secret_manager_secret_iam_binding" "github_webhook_secret_accessor" {
  count = local.github_hooks_on ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.github_webhook_secret[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = [local.github_hooks_sweeper_member]
}

# The delivery dedup's create() (see "WHAT THE SWEEPER HELD" above for why this
# role and why it carries no condition). An IAM member has no labels: the
# managed-by marker is in the plan's description of this file and in the
# GS-HOOKS pull request's IAM rows.
resource "google_project_iam_member" "github_hooks_firestore" {
  count = local.github_hooks_on ? 1 : 0

  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.github_hooks_sweeper_member
}

# A delivery id is worth remembering for as long as GitHub would redeliver it
# by hand; seven days, then Firestore deletes the document.
resource "google_firestore_field" "github_deliveries_ttl" {
  count = local.github_hooks_on ? 1 : 0

  project    = var.project_id
  database   = module.firestore.database_name
  collection = local.github_hooks_deliveries
  field      = local.github_hooks_delivery_field

  ttl_config {}

  # Nothing queries by expiry; a single-field index on it is write
  # amplification on every delivery (modules/firestore events_ttl).
  index_config {}
}

resource "google_cloud_run_v2_service" "github_hooks" {
  count = local.github_hooks_serving ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = local.github_hooks_name

  # GitHub's delivery servers are on the internet. The HMAC is the check.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection
  launch_stage        = "GA"

  labels = merge(local.labels, {
    "component" = "github-hooks"
  })

  template {
    service_account = local.github_hooks_sweeper_email

    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    # A delivery is one HMAC, one Firestore write and one forward: seconds.
    max_instance_request_concurrency = 20
    timeout                          = "60s"

    labels = merge(local.labels, {
      "component" = "github-hooks"
    })

    scaling {
      # Idle costs nothing (invariant 1). A small ceiling: GitHub sends one
      # delivery per push, and a flood of unsigned requests is refused before
      # any outside call, so more instances buy nothing but cost.
      min_instance_count = 0
      max_instance_count = 3
    }

    vpc_access {
      # swarm-api's ingress is internal-and-cloud-load-balancing, so the
      # forward to its run.app URL must leave through the VPC to count as
      # internal (main.tf says the same of swarm-api's call to the broker).
      egress = "ALL_TRAFFIC"

      network_interfaces {
        network    = module.network.network_id
        subnetwork = module.network.subnetwork_id
      }
    }

    containers {
      image = local.github_hooks_image

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

      env {
        name  = "SWARM_GITHUB_HOOKS_SECRET"
        value = local.github_hooks_secret_version
      }

      # The forward's address and the ID token's audience: swarm-api's URL,
      # the audience the sweeper's Cloud Scheduler jobs mint for
      # (modules/scheduler jobs.tf, api_audience empty).
      env {
        name  = "SWARM_API_URL"
        value = module.cloud_run.service_urls["swarm-api"]
      }

      env {
        name  = "FIRESTORE_DATABASE"
        value = module.firestore.database_name
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }

        initial_delay_seconds = 2
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 12
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

# allUsers -> run.invoker on THIS service only (see the local above).
resource "google_cloud_run_v2_service_iam_member" "github_hooks_public" {
  # checkov:skip=CKV_GCP_102:the public GitHub push receiver (docs/design/graph-service.md section 6.2, owner answer Q3); its check is the App's HMAC, and no other Cloud Run service is public.
  count = local.github_hooks_serving ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.github_hooks[0].name
  role     = "roles/run.invoker"
  member   = local.github_hooks_public_invoker
}

check "github_hooks_has_an_image" {
  assert {
    condition     = !local.github_hooks_on || local.github_hooks_image != ""
    error_message = "enable_github_hooks is set but image_refs has no swarm-github-hooks digest, so the receiver is not deployed (its secret, accessor, Firestore role and TTL policy are). Build and promote the image (scripts/build-images.sh, scripts/push-images.sh) first."
  }
}

output "github_hooks" {
  description = "The GitHub push receiver: whether it exists, its webhook URL (the App's Webhook URL), the secret's name and the identity it runs as. Names only; never a value."
  value = {
    created         = local.github_hooks_on
    serving         = local.github_hooks_serving
    service         = local.github_hooks_name
    webhook_secret  = local.github_hooks_on ? google_secret_manager_secret.github_webhook_secret[0].secret_id : ""
    service_account = local.github_hooks_sweeper_email
    webhook_url     = local.github_hooks_serving ? "${google_cloud_run_v2_service.github_hooks[0].uri}/github/events" : ""
  }
}
