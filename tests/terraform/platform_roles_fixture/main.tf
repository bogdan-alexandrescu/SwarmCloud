# platform_custom_roles_before_the_move.json, read once and normalised.
#
# The file records the eight custom roles terraform/infra defined before they
# moved to terraform/bootstrap (#79). Runs that compare a planned role with it
# read it through this module, as `run.<name>.roles`, so the comparison is
# written once per role instead of re-parsing the file in every assertion.
#
# Permissions come out as a SET, the type google_project_iam_custom_role gives
# them, so `==` compares membership and not order. The caller asserts `path`
# and the role count, so a moved or empty file fails there rather than passing
# as "nothing to compare".

locals {
  candidates = [
    "${path.module}/../platform_custom_roles_before_the_move.json",
    "platform_custom_roles_before_the_move.json",
  ]

  found = [for p in local.candidates : p if fileexists(p)]

  path = length(local.found) > 0 ? local.found[0] : ""

  roles = local.path == "" ? {} : {
    for key, r in jsondecode(file(local.path)).roles : key => {
      role_id     = r.role_id
      title       = r.title
      description = r.description
      stage       = r.stage
      permissions = toset(r.permissions)
    }
  }
}

output "path" {
  description = "The fixture actually read. Empty means no candidate path held it."
  value       = local.path
}

output "roles" {
  description = "key -> {role_id, title, description, stage, permissions (a set)} for each of the eight roles."
  value       = local.roles
}
