provider "google" {
  project = var.project_id
  region  = var.region

  # Applied by the provider to every resource that supports labels, on top of
  # whatever the resource sets itself. Belt and braces for the label-scoped
  # destroy: a resource added later without an explicit `labels` argument is
  # still marked managed-by=swarm-terraform.
  default_labels = local.labels
}
