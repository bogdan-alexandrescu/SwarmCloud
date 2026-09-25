# terraform/bootstrap/terraform.tfvars as committed, read as text.
#
# terraform test sets a run's variables from its own `variables` block, never
# from a root's terraform.tfvars, so a run that says it plans "what tfvars
# names" and writes the value out by hand is planning a copy. When the file
# moves -- the next role scoped, or a revert -- the copy does not, and the run
# goes on describing a plan nobody will apply. deployer_iam.tftest.hcl feeds
# `deployer_scoped_roles` from this module's output instead, so the plan it
# asserts on is the one the owner's apply would make from the file.
#
# A text parser, not HCL: terraform has no function that decodes a .tfvars
# file. It reads the one top-level assignment
#
#     deployer_scoped_roles = ["...", "..."]
#
# after removing `#` and `//` comments (none of this variable's values can
# contain either), so a role named in a comment is never read as scoped. The
# caller asserts `assignments == 1` and `path != ""`, so a file that moved or a
# form this cannot read fails there rather than yielding an empty list, which
# would plan "nothing scoped" and pass.

locals {
  candidates = [
    "${path.module}/../../../terraform/bootstrap/terraform.tfvars",
    "../../terraform/bootstrap/terraform.tfvars",
  ]

  found = [for p in local.candidates : p if fileexists(p)]

  path = length(local.found) > 0 ? local.found[0] : ""

  text = local.path == "" ? "" : replace(file(local.path), "/(?m)(#|//).*$/", "")

  assignments = regexall("(?ms)^deployer_scoped_roles\\s*=\\s*\\[(.*?)\\]", local.text)

  deployer_scoped_roles = length(local.assignments) == 1 ? [
    for m in regexall("\"([^\"]*)\"", local.assignments[0][0]) : m[0]
  ] : []
}
