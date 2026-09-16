output "job_names" {
  value = sort(keys(google_cloud_run_v2_job.this))
}

output "job_ids" {
  value = { for k, j in google_cloud_run_v2_job.this : k => j.id }
}

output "workspace_size_gib" {
  description = <<-EOT
    Effective workspace size per job. Note this is min(profile disk, container
    memory), not the profile's disk_gib: Cloud Run's disk-backed ephemeral
    storage is still Preview and the provider exposes only memory-medium
    volumes. Surface it so the sizing gap is visible in `terraform output`
    rather than discovered by a worker running out of space mid-run.
  EOT
  value = {
    for k, v in var.jobs : k => min(
      var.resource_classes[v.resource_class].disk_gib,
      var.resource_classes[v.resource_class].memory_gib
    )
  }
}
