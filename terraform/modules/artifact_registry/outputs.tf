output "repository_id" {
  value = google_artifact_registry_repository.this.repository_id
}

output "repository_name" {
  value = google_artifact_registry_repository.this.name
}

output "registry_host" {
  value = "${var.region}-docker.pkg.dev"
}

output "image_base" {
  description = "Prefix for every runner image: <base>/<image name>:<tag>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.this.repository_id}"
}

output "image_puller_role_id" {
  description = "The pull-without-enumerate role granted to tenant workers. Empty when no puller was declared."
  value       = length(google_project_iam_custom_role.image_puller) > 0 ? google_project_iam_custom_role.image_puller[0].role_id : ""
}
