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
