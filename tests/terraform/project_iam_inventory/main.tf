# Every declaration CI applies that writes the PROJECT's IAM policy, read as text.
#
# deployer_iam.tftest.hcl holds each project-level grant terraform/infra makes
# to deployer_grantable_project_roles -- the roles the scoped
# roles/resourcemanager.projectIamAdmin still lets CI modify -- by planning the
# module that declares the grant and reading the resource BY NAME. That catches
# a named grant that changes role. It cannot catch a grant nobody named: a ninth
# google_project_iam_member, or a google_project_iam_binding, added to any module
# would be checked by nothing, and its first sign would be the release that
# applies it failing with a 403 halfway through.
#
# So this module reads the files rather than a plan. It lists every
# `resource "<type>" "<name>"` whose type writes a project's IAM policy, in
# terraform/infra and in every module under terraform/modules -- including one
# terraform/infra does not call today, because calling it is a one-line change
# nobody would think to test -- and the caller compares the list with the grants
# its runs read.
#
# terraform/bootstrap is deliberately not read. The owner applies it, never CI,
# so its grants never pass through the scoped projectIamAdmin.
#
# A text scan, with catalogue_mirror's trade: it matches the literal form
# `resource "type" "name"` at the start of a line, which `terraform fmt` (checked
# in CI) guarantees, and would miss a declaration written any other way. The
# caller asserts that every grant it expects is FOUND, so a scan that stops
# matching fails rather than passing on an empty list.

locals {
  # `path.module` works when the module is loaded from its own directory; the
  # plain relative path covers a run whose working directory is tests/terraform.
  candidates = [
    "${path.module}/../../../terraform",
    "../../terraform",
  ]

  found = [for d in local.candidates : d if fileexists("${d}/infra/main.tf")]

  terraform_dir = length(local.found) > 0 ? local.found[0] : ""

  # Relative to terraform/, so `dirname` reads `infra` or `modules/<name>`.
  files = local.terraform_dir == "" ? [] : sort(concat(
    [for f in fileset("${local.terraform_dir}/infra", "**/*.tf") : "infra/${f}"],
    [for f in fileset("${local.terraform_dir}/modules", "**/*.tf") : "modules/${f}"],
  ))

  # Every resource type whose create, update or destroy is a setIamPolicy on the
  # project: the IAM member, binding, policy and audit-config resources, the one
  # that removes a member, and google_project_default_service_accounts, whose
  # DEPRIVILEGE action revokes roles/editor from the default service accounts.
  policy_writers = "google_project_iam_(?:member|binding|policy|audit_config|member_remove)|google_project_default_service_accounts"

  declared = sort(flatten([
    for f in local.files : [
      for m in regexall("(?m)^resource\\s+\"(${local.policy_writers})\"\\s+\"([^\"]+)\"", file("${local.terraform_dir}/${f}")) :
      "${dirname(f)} ${m[0]}.${m[1]}"
    ]
  ]))
}
