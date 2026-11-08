# The tenants block of terraform/environments/dev/dev.tfvars as it stood
# before lane W9 (#847) took the personal tenant out of it, with the personal tenant replaced by a
# stand-in of the same shape, for the removal test beside this directory.
# terraform/bootstrap reads tenant ids from a tfvars file as text
# (deployer_service_accounts.tf), so the "before" plan needs a file, not a
# variable. Only the keys matter to that parse.
#
# Why a stand-in and not the personal tenant itself: with terraform/bootstrap/removed.tf
# in the configuration, a plan that still declares the personal tenant's instances fails
# ("Moved object still exists"). The test maps the stand-in's id to the personal tenant.

tenants = {
  eng = {
    kind      = "group"
    principal = "eng@example.com"
    providers = ["anthropic", "openai"]
  }

  smoke = {
    kind      = "group"
    principal = "smoke@example.com"
    providers = ["anthropic"]
  }

  u-sw-c90291 = {
    kind      = "user"
    principal = "swarm-verify@example-project.iam.gserviceaccount.com"
    providers = []
  }

  w9-stand-in = {
    kind      = "user"
    principal = "w9-stand-in@example.com"
    providers = ["anthropic"]
  }
}
