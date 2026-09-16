# Custom roles.
#
# The predefined alternatives are all too wide for this platform:
#
#   roles/run.developer also grants run.services.* -- the scheduler could
#   redeploy the API it is called by.
#   roles/run.admin adds setIamPolicy, so the dispatcher could grant itself
#   anything on any Cloud Run resource.
#   roles/container.developer covers every workload type in every namespace,
#   including Secrets.
#
# Each role below is the exact permission set one component needs and nothing
# beyond it. Note what is absent from every one of them: setIamPolicy, and any
# delete permission outside the reconciler's role.

locals {
  role_suffix = var.custom_role_suffix == "" ? "" : "_${var.custom_role_suffix}"
}

resource "google_project_iam_custom_role" "job_dispatcher" {
  project = var.project_id
  role_id = "swarmJobDispatcher${local.role_suffix}"
  title   = "Swarm Job Dispatcher"

  description = "Create and run Cloud Run Job resources. No delete, no IAM, no services."
  stage       = "GA"

  permissions = [
    # Per-tenant-per-profile Job resources: Cloud Run pins the service account
    # on the Job, not the execution, so the dispatcher must be able to create
    # them (CONTRACT.md, platform decisions).
    "run.jobs.create",
    "run.jobs.get",
    "run.jobs.list",
    "run.jobs.update",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.executions.get",
    "run.executions.list",
    "run.tasks.get",
    "run.tasks.list",
    "run.operations.get",
    "run.operations.list",
  ]
}

resource "google_project_iam_custom_role" "job_reaper" {
  project = var.project_id
  role_id = "swarmJobReaper${local.role_suffix}"
  title   = "Swarm Job Reaper"

  description = "Cancel and delete executions, garbage-collect unused Job resources. Reconciler only."
  stage       = "GA"

  permissions = [
    "run.jobs.get",
    "run.jobs.list",
    "run.jobs.delete",
    "run.executions.get",
    "run.executions.list",
    "run.executions.cancel",
    "run.executions.delete",
    "run.tasks.get",
    "run.tasks.list",
    "run.operations.get",
    "run.operations.list",
  ]
}

resource "google_project_iam_custom_role" "gke_dispatcher" {
  count = var.gke_enabled ? 1 : 0

  project = var.project_id
  role_id = "swarmGkeDispatcher${local.role_suffix}"
  title   = "Swarm GKE Dispatcher"

  description = "Create and observe Jobs on the swarm Autopilot cluster. No Secret access, no cluster mutation."
  stage       = "GA"

  permissions = [
    # clusters.get is what `gcloud container clusters get-credentials` needs;
    # it returns the endpoint and CA, not any workload data.
    "container.clusters.get",
    "container.namespaces.get",
    "container.namespaces.list",
    "container.jobs.create",
    "container.jobs.get",
    "container.jobs.list",
    "container.jobs.update",
    "container.pods.get",
    "container.pods.list",
    "container.pods.getLogs",
    "container.events.get",
    "container.events.list",
  ]
}

resource "google_project_iam_custom_role" "gke_reaper" {
  count = var.gke_enabled ? 1 : 0

  project = var.project_id
  role_id = "swarmGkeReaper${local.role_suffix}"
  title   = "Swarm GKE Reaper"

  description = "Delete finished Jobs and stuck Pods on the swarm Autopilot cluster. Reconciler only."
  stage       = "GA"

  permissions = [
    "container.clusters.get",
    "container.namespaces.get",
    "container.jobs.get",
    "container.jobs.list",
    "container.jobs.delete",
    "container.pods.get",
    "container.pods.list",
    "container.pods.delete",
    "container.events.list",
  ]
}
