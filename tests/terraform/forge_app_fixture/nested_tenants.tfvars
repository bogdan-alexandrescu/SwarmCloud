# A stand-in for terraform/environments/dev/dev.tfvars, read by
# tests/terraform/forge_app.tftest.hcl through terraform/bootstrap's
# infra_tenants_tfvars. Never applied.
#
# `eng-git-u-x`'s user slots are named swarm-tenant-eng-git-u-x-git-u-<hex>,
# which starts with eng's prefix, swarm-tenant-eng-git-u-: a prefix condition
# would let eng's worker read them. terraform/bootstrap/forge_user_slots.tf
# must refuse the pair.

tenants = {
  eng = {
    kind      = "group"
    principal = "eng@example.com"
  }

  eng-git-u-x = {
    kind      = "user"
    principal = "x@example.com"
  }
}
