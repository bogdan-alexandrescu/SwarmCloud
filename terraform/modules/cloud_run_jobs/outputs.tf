output "job_names" {
  value = sort(keys(google_cloud_run_v2_job.this))
}

output "job_ids" {
  value = { for k, j in google_cloud_run_v2_job.this : k => j.id }
}

# Read off the resource rather than echoed from var.jobs, so a test of the root
# asserts what the job will actually run: terraform/infra deploys by digest,
# and tests/terraform/image_refs.tftest.hcl holds it to that.
output "images" {
  value = { for k, j in google_cloud_run_v2_job.this : k => j.template[0].template[0].containers[0].image }
}

output "workspace_size_gib" {
  description = <<-EOT
    Effective workspace size per job. This is a FRACTION OF CONTAINER MEMORY,
    not the profile's disk_gib: Cloud Run's disk-backed ephemeral storage is
    still Preview and the provider exposes only memory-medium volumes. Surface
    it so the sizing gap is visible in `terraform output` rather than discovered
    by a worker running out of space mid-run.
  EOT
  value       = { for k, v in var.jobs : k => local.workspace_gib[v.resource_class] }
}

output "workspace_size_gib_by_class" {
  description = "resource class -> workspace GiB, so a test can assert the workspace never claims a container's whole memory limit."
  value       = local.workspace_gib
}
