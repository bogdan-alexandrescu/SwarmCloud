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
    Adds an IAM condition pinning every datastore.user grant to
    projects/<project>/databases/<db>. The project is shared, so an unconditioned
    datastore.user is a grant over every other team's Firestore data too.
  EOT
  type        = bool
  default     = true
}

variable "custom_role_suffix" {
  description = "Appended to custom role ids so a re-create after a soft delete does not collide with the 7-day tombstone."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-zA-Z0-9_]*$", var.custom_role_suffix))
    error_message = "custom role ids accept only letters, digits and underscores."
  }
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
