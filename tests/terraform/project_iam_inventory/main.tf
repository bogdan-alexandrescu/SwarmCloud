# Every declaration CI applies that writes the PROJECT's IAM policy, read as
# text, against the parity assertions that hold each one to the grantable list.
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
# So this module reads two things as text and compares them:
#
#   DECLARED -- every `resource "<type>" "<name>"` whose type writes a project's
#   IAM policy, in terraform/infra and in every module under terraform/modules,
#   including one terraform/infra does not call today, because calling it is a
#   one-line change nobody would think to test.
#
#   READ BY A PARITY ASSERTION -- every such resource that an `assert` in the
#   caller's .tftest.hcl passes, by its `.role`, to
#   contains(<...>deployer_grantable_project_roles, ...), in a run that plans
#   the directory declaring it. Nothing here is a hand-kept list: removing a
#   grant from a parity assertion un-reads it, and this module sees that.
#
# terraform/bootstrap is deliberately not read. The owner applies it, never CI,
# so its grants never pass through the scoped projectIamAdmin. Nor is a module
# CI would fetch from a registry or a git URL -- none is called today, and the
# caller asserts that every module source is a path this scan also reads.
#
# A text scan, with catalogue_mirror's trade: it matches the literal forms that
# `terraform fmt` (checked in CI over terraform/ and tests/terraform) produces,
# and would miss a declaration or an assertion written any other way. Both
# directions are asserted by the caller, so a scan that stops matching fails
# rather than passing on an empty list: a declared grant no assertion is found
# to read is `uncovered`, and a grant an assertion reads that the declaration
# scan did not find is `stale`.

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

  # Every `module` block's source, as "<dir under terraform/> <source>".
  module_sources = sort(flatten([
    for f in local.files : [
      for b in regexall("(?ms)^module\\s+\"[^\"]+\"\\s+\\{\\n(.*?)^\\}", file("${local.terraform_dir}/${f}")) : [
        for s in regexall("(?m)^  source\\s*=\\s*\"([^\"]+)\"", b[0]) : "${dirname(f)} ${s[0]}"
      ]
    ]
  ]))

  # A module the scan above does not also read: a registry or git source, or a
  # path that leaves terraform/infra and terraform/modules. From infra, a path
  # into ../modules/ or below itself; from a module, a sibling or below itself.
  unscanned_module_sources = [
    for m in local.module_sources : m
    if length(regexall("^(infra (\\./|\\.\\./modules/[^./])|modules/[^ /]+ (\\./|\\.\\./[^./]))", m)) == 0
  ]

  # The caller's test file, found the same two ways.
  test_candidates = [
    "${path.module}/..",
    ".",
  ]

  test_found = [for d in local.test_candidates : d if fileexists("${d}/${var.parity_test_file}")]

  test_source = length(local.test_found) > 0 ? file("${local.test_found[0]}/${var.parity_test_file}") : ""

  # [name, body] for every top-level run block.
  runs = regexall("(?ms)^run \"([^\"]+)\" \\{\\n(.*?)^\\}", local.test_source)

  # "<dir under terraform/> <type>.<name>" for every grant a parity assertion
  # reads, in the two forms the runs use:
  #
  #   [for k, m in <type>.<name> : contains(<...>deployer_grantable_project_roles, m.role)]
  #   contains(<...>deployer_grantable_project_roles, <type>.<name>.role)
  #
  # The loop form is counted only when the value tested is the loop's own
  # element (RE2 has no backreferences, so that is compared below). Only runs
  # that plan terraform/infra or a module under terraform/modules count, since
  # only those directories are scanned for declarations.
  parity_reads = sort(distinct(flatten([
    for r in local.runs : [
      for dir in [for s in regexall("(?m)^    source\\s*=\\s*\"\\.\\./\\.\\./terraform/([^\"]+)\"", r[1]) : s[0]] : [
        for a in regexall("(?ms)^  assert \\{\\n(.*?)^  \\}", r[1]) : concat(
          [
            for m in regexall("for \\w+, (\\w+) in (${local.policy_writers})\\.([A-Za-z0-9_-]+) : contains\\([^,()]*deployer_grantable_project_roles, (\\w+)\\.role\\)", a[0]) :
            "${dir} ${m[1]}.${m[2]}" if m[0] == m[3]
          ],
          [
            for m in regexall("contains\\([^,()]*deployer_grantable_project_roles, (${local.policy_writers})\\.([A-Za-z0-9_-]+)\\.role\\)", a[0]) :
            "${dir} ${m[0]}.${m[1]}"
          ],
        )
      ] if dir == "infra" || startswith(dir, "modules/")
    ]
  ])))
}
