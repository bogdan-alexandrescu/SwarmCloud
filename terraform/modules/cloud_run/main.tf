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
        # requests == limits (CONTRACT.md invariant 7).
        limits = {
          cpu    = each.value.cpu
          memory = each.value.memory
        }

        # CPU is only billed while a request is in flight.
        cpu_idle          = true
        startup_cpu_boost = true
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
      # Images are rolled by the deploy pipeline, not by terraform. Without this
      # every `terraform apply` would silently roll production back to whatever
      # tag was in the tfvars.
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

# Invoker IAM. There is no allUsers binding anywhere: every caller presents a
# Google ID token, and service-to-service calls present their own SA identity.
resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = {
    for pair in flatten([
      for svc_name, svc in var.services : [
        for member in svc.invokers : {
          key     = "${svc_name}:${member}"
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
