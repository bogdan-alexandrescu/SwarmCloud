# Cloud Run services: the control plane.
#
# min-instances is 0 on every one of them. An idle swarm costs nothing, which is
# the whole premise -- a backlog of ten thousand QUEUED tasks must produce zero
# running containers (CONTRACT.md invariant 1). Cold start on the scheduler is
# paid for by the Pub/Sub wake plus a one-minute safety tick, not by keeping a
# warm instance burning money.

resource "google_cloud_run_v2_service" "this" {
  for_each = var.services

  project  = var.project_id
  location = var.region
  name     = each.key

  ingress             = var.ingress
  deletion_protection = var.deletion_protection
  launch_stage        = "GA"

  # Accepted IN ADDITION to the service URL, never instead of it: an existing
  # caller that presents a URL-audience token keeps working across this change.
  custom_audiences = each.value.custom_audiences

  labels = var.labels

  template {
    service_account = each.value.service_account_email

    # Second generation: full Linux compatibility and a real filesystem.
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    max_instance_request_concurrency = each.value.concurrency
    timeout                          = each.value.request_timeout

    labels = var.labels

    scaling {
      # Idle costs nothing. Explicit maximum so a wedged caller cannot turn the
      # control plane into an unbounded bill.
      min_instance_count = 0
      max_instance_count = each.value.max_instances
    }

    vpc_access {
      egress = var.vpc_egress

      # Direct VPC egress rather than a Serverless VPC Access connector: no
      # connector instances to size, patch or pay for while idle.
      network_interfaces {
        network    = var.network
        subnetwork = var.subnetwork
      }
    }

    containers {
      image = each.value.image

      ports {
        container_port = each.value.container_port
      }

      resources {
        # requests == limits, no bursting (CONTRACT.md invariant 7). Both flags
        # below are the ways Cloud Run breaks that equality, and both are off:
        #
        #   startup_cpu_boost allocates MORE CPU than the declared limit while an
        #   instance starts. That is bursting by definition.
        #
        #   cpu_idle = true throttles CPU to near zero between requests, so the
        #   guaranteed allocation is not the limit -- it is the limit only while
        #   a request happens to be in flight. The scheduler's drain loop and the
        #   reconciler's sweep both run for minutes inside one request with
        #   concurrency 1; "CPU always allocated" is what makes their declared
        #   2 vCPU an actual 2 vCPU for that whole time.
        #
        # This costs more than request-billed CPU on an idle service. It is paid
        # deliberately: min_instance_count is 0, so an idle swarm still runs no
        # instances at all and the bill is still zero (invariant 1).
        limits = {
          cpu    = each.value.cpu
          memory = each.value.memory
        }

        cpu_idle          = false
        startup_cpu_boost = false
      }

      dynamic "env" {
        for_each = each.value.env
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        # TCP rather than HTTP: it is true the moment the server binds, and it
        # does not depend on a handler that a future refactor might rename.
        tcp_socket {
          port = each.value.container_port
        }

        initial_delay_seconds = 5
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 12
      }

      dynamic "liveness_probe" {
        for_each = each.value.health_check_path == "" ? [] : [each.value.health_check_path]
        content {
          http_get {
            path = liveness_probe.value
            port = each.value.container_port
          }

          initial_delay_seconds = 30
          period_seconds        = 30
          timeout_seconds       = 5
          failure_threshold     = 3
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [
      # `client` and `client_version` are stamped by whatever last touched the
      # service (gcloud, the console, Cloud Deploy). They are metadata about the
      # writer, not about the workload, and a permanent diff on them is noise.
      #
      # The image is deliberately NOT ignored. scripts/lib/deploy.sh detects the
      # `image_tag` variable in terraform/infra and deploys by running
      # `terraform apply -var image_tag=<promoted tag>`, so terraform IS the
      # deploy mechanism here: ignoring the image would make every deploy a
      # no-op. It also means an image changed out of band -- by anything holding
      # run.services.update or run.jobs.update -- shows up as drift on the next
      # plan instead of running unnoticed.
      client,
      client_version,
    ]
  }
}

# Invoker IAM. There is no allUsers binding anywhere: every caller presents a
# Google ID token, and service-to-service calls present their own SA identity.
#
# `invokers` is a map keyed by a caller LABEL. Keying it by the member string
# would put a service account email -- unknown until apply -- in a for_each key,
# which terraform cannot plan: the first plan on a fresh project fails with
# "the for_each map includes keys derived from resource attributes that cannot
# be determined until apply".
resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = {
    for pair in flatten([
      for svc_name, svc in var.services : [
        for label, member in svc.invokers : {
          key     = "${svc_name}:${label}"
          service = svc_name
          member  = member
        }
      ]
    ]) : pair.key => pair
  }

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.this[each.value.service].name
  role     = "roles/run.invoker"
  member   = each.value.member
}
