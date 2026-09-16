# Cloud Run Jobs -- the PRIMARY execution backend.
#
# Chosen over GKE for the boring reason that it has no nodes: no autoscaler
# draining a node out from under a two-hour agent run, no node upgrade, no
# kubelet eviction. Far fewer ways for the platform to kill a task.
#
# Two things in here are load-bearing and easy to break:
#
#   max_retries = 0. Cloud Run's own retry would re-run the SAME container with
#   the SAME fencing generation. Retries belong to the scheduler, which mints a
#   new attempt and a new generation (CONTRACT.md invariant 5). If Cloud Run
#   retried as well, a worker would come back holding a generation the control
#   plane had already moved past.
#
#   No Spot, anywhere. Spot Pods cannot use Autopilot extended run time, so the
#   no-preemption requirement and Spot are mutually exclusive (invariant 6).
#   Cloud Run Jobs has no Spot setting to disable -- the point is that nothing
#   in this module may ever introduce one.

locals {
  # The reconciler garbage-collects Job resources the dispatcher created. It
  # must leave the terraform-managed ones alone, so they are labelled for it.
  job_labels = merge(var.labels, {
    "swarm-gc-exempt" = "true"
  })

  # Workspace volume size per resource class.
  #
  # Cloud Run's disk-backed ephemeral storage is Preview and the provider does
  # not expose it: `empty_dir.medium` accepts only "MEMORY". A memory-medium
  # volume is charged against the CONTAINER'S memory limit, so the profile's
  # disk_gib (20/40/100) is not reachable here.
  #
  # Sizing it at the full memory limit would be worse than useless: a workspace
  # that grew into the agent's own memory would OOM-kill the container, and an
  # OOM kill is a SIGKILL -- no checkpoint, no graceful park, the attempt is just
  # gone. Capping it at a fraction means a runaway workspace hits ENOSPC on a
  # write instead, which the worker can see, checkpoint through and report.
  workspace_gib = {
    for name, rc in var.resource_classes :
    name => max(1, min(rc.disk_gib, floor(rc.memory_gib * var.workspace_memory_fraction)))
  }
}

resource "google_cloud_run_v2_job" "this" {
  for_each = var.jobs

  project  = var.project_id
  location = var.region
  name     = each.key

  deletion_protection = var.deletion_protection
  launch_stage        = "GA"

  labels = merge(local.job_labels, {
    "swarm-tenant" = each.value.tenant_id
    "swarm-runner" = replace(each.value.runner_profile, "_", "-")
    "swarm-class"  = each.value.resource_class
  })

  template {
    # One task per execution. Fan-out is the scheduler's job, not Cloud Run's:
    # parallelism here would create infrastructure demand that no lease covers.
    task_count  = 1
    parallelism = 1

    labels = local.job_labels

    template {
      service_account = each.value.service_account_email

      # See the header: platform-owned retries only.
      max_retries = 0

      timeout = "${each.value.timeout_seconds}s"

      # Second generation. Required for volumes, and the only environment with
      # full Linux compatibility for the agent toolchains.
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        # ALL_TRAFFIC, not PRIVATE_RANGES_ONLY: provider calls must leave through
        # the swarm Cloud NAT so they carry the reserved addresses a provider
        # allow-lists. It is also what guarantees the execution has no public IP
        # of its own.
        egress = "ALL_TRAFFIC"

        network_interfaces {
          network    = var.network
          subnetwork = var.subnetwork

          # The handle the worker-ingress deny rule targets. Without a tag on
          # the instance, a firewall rule cannot name it, and every tenant's
          # worker shares one flat subnet with every other tenant's -- see
          # modules/network/firewall.tf.
          tags = var.network_tags
        }
      }

      volumes {
        name = "workspace"

        empty_dir {
          # See local.workspace_gib: this is a fraction of the container's
          # memory, not the profile's disk_gib, and that gap is exactly why
          # checkpointing is mandatory rather than optional (invariant 8).
          medium     = "MEMORY"
          size_limit = "${local.workspace_gib[each.value.resource_class]}Gi"
        }
      }

      dynamic "volumes" {
        for_each = var.mount_artifact_bucket ? [1] : []
        content {
          name = "artifacts"

          gcs {
            bucket    = var.artifact_bucket
            read_only = false
          }
        }
      }

      containers {
        image   = each.value.image
        command = length(each.value.command) > 0 ? each.value.command : null
        args    = length(each.value.args) > 0 ? each.value.args : null

        resources {
          # Cloud Run Jobs allocates CPU for the whole execution and sets the
          # request equal to the limit; there is no burst envelope to be
          # OOM-killed out of (CONTRACT.md invariant 7).
          limits = {
            cpu    = tostring(var.resource_classes[each.value.resource_class].cpu)
            memory = "${var.resource_classes[each.value.resource_class].memory_gib}Gi"
          }
        }

        volume_mounts {
          name       = "workspace"
          mount_path = var.workspace_mount_path
        }

        dynamic "volume_mounts" {
          for_each = var.mount_artifact_bucket ? [1] : []
          content {
            name       = "artifacts"
            mount_path = var.artifact_mount_path
          }
        }

        dynamic "env" {
          for_each = each.value.env
          content {
            name  = env.key
            value = env.value
          }
        }

        # Provider keys. Only this tenant's service account can read these
        # secrets, so a doctored reference to another tenant's secret fails at
        # start instead of leaking (invariant 9).
        dynamic "env" {
          for_each = each.value.secret_env
          content {
            name = env.key

            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # Writer metadata only -- see the note in modules/cloud_run/main.tf.
      #
      # The image is NOT ignored, and that is the whole point on this path. The
      # dispatcher holds run.jobs.update (modules/iam/custom_roles.tf) and
      # iam.serviceAccountUser on every tenant SA, so a compromised dispatcher
      # could repoint swarm-job-<tenant>-<profile> at an attacker image and run
      # it under that tenant's identity, with that tenant's provider key. An
      # ignored image is an image nothing ever compares, so that repoint would
      # survive every subsequent apply. Tracking it makes the repoint drift that
      # the next plan reverts.
      client,
      client_version,
    ]
  }
}
