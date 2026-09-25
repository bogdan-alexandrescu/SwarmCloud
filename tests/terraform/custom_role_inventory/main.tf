# Every declaration that DEFINES or READS a custom IAM role, read as text, in
# the files CI applies and in the root the owner applies.
#
# WHY TEXT. Once roles/iam.roleAdmin is off the CI deployer (#79), CI holds no
# iam.roles.* permission at all: measured 2026-09-25 with read-only
# `gcloud iam roles describe` over every role the deployer holds or will hold,
# roleAdmin was the only one carrying any. So a single
# google_project_iam_custom_role anywhere under terraform/infra or
# terraform/modules is a release that 403s -- on create, update, delete and on
# the refresh every plan does -- and so is a data source that reads a role
# (google_iam_role, google_project_iam_custom_role[s]), because a read is
# iam.roles.get. A plan-level assertion can only look at resources it knows the
# name of; this reads every file, so a ninth role added to a new module is
# found too.
#
# terraform/bootstrap is scanned separately, as the CONTROL: it declares custom
# roles (swarmSecretProvisioner, swarmDeployerProjectBuckets, and after the move
# the platform's eight), so a scan that stopped matching would report zero
# there as well as in CI's files, and the caller fails on that instead of
# passing on an empty list.
#
# Literal forms only, the trade catalogue_mirror and project_iam_inventory make:
# `terraform fmt`, which CI checks over terraform/, writes
# `resource "<type>" "<name>"` at the start of a line, and that is what is
# matched.

locals {
  # `path.module` works when the module is loaded from its own directory; the
  # plain relative path covers a run whose working directory is tests/terraform.
  candidates = [
    "${path.module}/../../../terraform",
    "../../terraform",
  ]

  found = [for d in local.candidates : d if fileexists("${d}/infra/main.tf")]

  terraform_dir = length(local.found) > 0 ? local.found[0] : ""

  # Relative to terraform/, so every hit names the directory it was found in.
  ci_files = local.terraform_dir == "" ? [] : sort(concat(
    [for f in fileset("${local.terraform_dir}/infra", "**/*.tf") : "infra/${f}"],
    [for f in fileset("${local.terraform_dir}/modules", "**/*.tf") : "modules/${f}"],
  ))

  bootstrap_files = local.terraform_dir == "" ? [] : sort([for f in fileset("${local.terraform_dir}/bootstrap", "**/*.tf") : "bootstrap/${f}"])

  # Defining a role, and reading one: google_iam_role reads a predefined or
  # custom role's permissions, and the custom-role data sources read ours.
  # Every one of them is an iam.roles.* call.
  role_types = "google_project_iam_custom_roles?|google_organization_iam_custom_roles?|google_iam_role"

  pattern = "(?m)^(resource|data)\\s+\"(${local.role_types})\"\\s+\"([^\"]+)\""

  ci_declarations = sort(flatten([
    for f in local.ci_files : [
      for m in regexall(local.pattern, file("${local.terraform_dir}/${f}")) : "${f} ${m[0]} ${m[1]}.${m[2]}"
    ]
  ]))

  bootstrap_declarations = sort(flatten([
    for f in local.bootstrap_files : [
      for m in regexall(local.pattern, file("${local.terraform_dir}/${f}")) : "${f} ${m[0]} ${m[1]}.${m[2]}"
    ]
  ]))
}

output "terraform_dir" {
  description = "The terraform/ directory actually read. Empty means no candidate path held terraform/infra."
  value       = local.terraform_dir
}

output "ci_files_read" {
  description = "How many .tf files under terraform/infra and terraform/modules were scanned."
  value       = length(local.ci_files)
}

output "ci_declarations" {
  description = "\"<file> <resource|data> <type>.<name>\" for every custom-role definition or role read in the files CI applies. Must be empty."
  value       = local.ci_declarations
}

output "bootstrap_declarations" {
  description = "The same scan over terraform/bootstrap, the control: it must find the roles bootstrap defines."
  value       = local.bootstrap_declarations
}
