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
  value       = length(var.pullers) > 0 ? module.custom_role_ids.ids.image_puller : ""
}
