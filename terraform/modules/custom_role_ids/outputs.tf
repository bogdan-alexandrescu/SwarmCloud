output "ids" {
  description = "key -> custom role id, e.g. job_dispatcher -> swarmJobDispatcher."
  value       = local.ids

  # IAM's rule for a custom role id. The provider adds "cannot contain -", which
  # the character class already refuses.
  precondition {
    condition     = alltrue([for id in values(local.ids) : can(regex("^[a-zA-Z0-9_.]{3,64}$", id))])
    error_message = "a custom role id may hold only letters, digits, underscores and dots, 3 to 64 of them; check custom_role_suffix."
  }
}

output "names" {
  description = "key -> the role's full name, projects/<project>/roles/<id>: what an IAM binding names."
  value       = { for key, id in local.ids : key => "projects/${var.project_id}/roles/${id}" }
}
