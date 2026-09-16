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

variable "artifact_bucket" {
  description = "Artifact bucket name; the control plane reads objects from it to serve results."
  type        = string
}

variable "labels" {
  description = "Service accounts carry no labels in GCP, so this is recorded in their description instead."
  type        = map(string)
}
