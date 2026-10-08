# Everything in terraform/ that could put a PERSON'S workspace into Terraform,
# read as text (docs/workspaces.md §3.2, held by
# personal_workspaces_absent.tftest.hcl).
#
# Personal workspaces live outside Terraform state (WD3). A plan can never
# change or destroy one only while three things hold, and each is a property of
# the CODE, not of one plan, so it is read from every file -- including a module
# no root calls today, because calling it is a one-line change:
#
#   1. nothing iterates over people: no variable of workspaces or people, no
#      data source reading Firestore (where the list of people is), and no
#      workspace id (`w-` and six hex) written anywhere;
#   2. nothing is authoritative over a policy a personal worker is a member
#      of: the artifact bucket's and the project's. An `_iam_binding` or
#      `_iam_policy` there would remove every person the job added at the next
#      release;
#   3. (the tenants validation in terraform/infra/variables.tf; planned by the
#      caller, not read here).
#
# The same trade as project_iam_inventory: `terraform fmt` writes
# `resource "<type>" "<name>"` at the start of a line, and that is what is
# matched. The caller asserts files_read > 0, so a scan that stops matching
# fails rather than passing on empty lists.

locals {
  candidates = [
    "${path.module}/../../../terraform",
    "../../terraform",
  ]

  found = [for d in local.candidates : d if fileexists("${d}/infra/main.tf")]

  terraform_dir = length(local.found) > 0 ? local.found[0] : ""

  # Both roots and every module: the bootstrap holds grants that touch personal
  # workers (workspace_deployer.tf), so it is read like the rest.
  files = local.terraform_dir == "" ? [] : sort(concat(
    [for f in fileset("${local.terraform_dir}/infra", "**/*.tf") : "infra/${f}"],
    [for f in fileset("${local.terraform_dir}/bootstrap", "**/*.tf") : "bootstrap/${f}"],
    [for f in fileset("${local.terraform_dir}/modules", "**/*.tf") : "modules/${f}"],
  ))

  tfvars = local.terraform_dir == "" ? [] : sort([for f in fileset("${local.terraform_dir}/environments", "**/*.tfvars") : "environments/${f}"])

  authoritative_types = "google_storage_bucket_iam_(?:binding|policy)|google_project_iam_(?:binding|policy)"

  authoritative = sort(flatten([
    for f in local.files : [
      for m in regexall("(?m)^resource\\s+\"(${local.authoritative_types})\"\\s+\"([^\"]+)\"", file("${local.terraform_dir}/${f}")) :
      "${f} ${m[0]}.${m[1]}"
    ]
  ]))

  people_inputs = sort(flatten([
    for f in local.files : concat(
      [
        for m in regexall("(?m)^variable\\s+\"([a-z_]*(?:workspace_ids|workspaces|people|persons|personal_tenants)[a-z_]*)\"", file("${local.terraform_dir}/${f}")) :
        "${f} variable.${m[0]}"
      ],
      [
        for m in regexall("(?m)^data\\s+\"(google_firestore_[a-z_]+)\"\\s+\"([^\"]+)\"", file("${local.terraform_dir}/${f}")) :
        "${f} data.${m[0]}.${m[1]}"
      ],
    )
  ]))

  # A workspace id in quotes, in any .tf or .tfvars file. Comments that quote
  # the design's example (`w-3f9a2c`) are not code, so only a quoted literal
  # outside a `#` comment line counts.
  workspace_ids = sort(flatten([
    for f in concat(local.files, local.tfvars) : [
      for m in regexall("(?m)^[^#\\n]*\"(w-[0-9a-f]{6})\"", file("${local.terraform_dir}/${f}")) : "${f} ${m[0]}"
    ]
  ]))
}

output "files_read" {
  description = "How many .tf files were scanned, so an empty scan is visible rather than silent."
  value       = length(local.files)
}

output "tfvars_read" {
  description = "How many environment tfvars files were scanned for workspace ids."
  value       = length(local.tfvars)
}

output "authoritative" {
  description = "Every authoritative bucket or project IAM resource declared under terraform/: must be empty."
  value       = local.authoritative
}

output "people_inputs" {
  description = "Every variable named for workspaces or people, and every Firestore data source, declared under terraform/: must be empty."
  value       = local.people_inputs
}

output "workspace_ids" {
  description = "Every quoted workspace id (w-<6 hex>) outside a comment in terraform/: must be empty."
  value       = local.workspace_ids
}
