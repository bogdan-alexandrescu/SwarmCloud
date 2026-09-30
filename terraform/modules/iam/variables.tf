variable "project_id" {
  type = string
}

variable "firestore_database" {
  description = "Named database the control plane uses. IAM is conditioned to it so no swarm identity can touch `(default)`."
  type        = string
  default     = "swarm"
}

variable "scope_firestore_to_database" {
  description = <<-EOT
    Adds an IAM condition pinning datastore grants to
    projects/<project>/databases/<db>.

    DEFAULTS OFF, and it does NOT do what its name suggests. Firestore does not
    evaluate IAM Conditions on the DATA plane, only for administrative
    operations. Verified live on 2026-09-16: with this on, every control-plane
    service was denied document reads and writes and /readyz reported
    "firestore unavailable: PermissionDenied". Firestore Security Rules do not
    help either -- server SDKs with admin credentials bypass Rules, and every
    component here is a server SDK.

    Turning it on therefore breaks the platform without buying isolation. It is
    kept only for administrative-plane scoping, if you want that.

    The honest position: a swarm identity can reach ANY Firestore database in
    this project. Today `swarm` is the only one. If you create another database
    in this project, these identities can read and write it. The real fix is a
    dedicated project for Firestore, where project-level IAM IS the boundary.
  EOT
  type        = bool
  default     = false
}

variable "gke_enabled" {
  description = "Grant the GKE dispatch role. False when the Autopilot cluster is not deployed."
  type        = bool
  default     = true
}

variable "gke_cluster_name" {
  description = <<-EOT
    The swarm's own Autopilot cluster. Required when gke_enabled is true,
    because the dispatch and reap roles are conditioned to it.
  EOT
  type        = string
  default     = ""
}

variable "gke_location" {
  description = "Region of the swarm cluster; part of the resource name the IAM condition pins."
  type        = string
  default     = "us-central1"
}

variable "scope_gke_to_cluster" {
  description = <<-EOT
    Adds an IAM condition pinning the container.* grants to
    projects/<project>/locations/<region>/clusters/<swarm cluster>.

    This project is SHARED and holds a live `agents-staging` cluster owned by
    another team. A project-level container.jobs.create covers EVERY cluster in
    the project, so without this condition swarm-scheduler could create
    workloads in -- and read pod logs out of -- that other team's production
    cluster, and swarm-reconciler could delete their Jobs and Pods. That is
    precisely the blast radius this platform is required never to have.

    The switch exists for the same reason `scope_firestore_to_database` does: a
    conditioned binding is fail-closed, so if an environment ever finds GKE's
    authorizer evaluating a check this condition cannot match, an operator can
    turn it off DELIBERATELY rather than discovering it during an incident.
    Leaving it on is the supported configuration.
  EOT
  type        = bool
  default     = true
}

variable "artifact_bucket" {
  description = "Artifact bucket name; the control plane reads objects from it to serve results."
  type        = string
}

variable "labels" {
  description = "Service accounts carry no labels in GCP, so this is recorded in their description instead."
  type        = map(string)
}
