# The tenants block of terraform/environments/dev/dev.tfvars as it stood
# before lane W9 (#847) took u-bogdan out of it, with u-bogdan replaced by a
# stand-in of the same shape, for u_bogdan_removed.tftest.hcl.
# terraform/bootstrap reads tenant ids from a tfvars file as text
# (deployer_service_accounts.tf), so the "before" plan needs a file, not a
# variable. Only the keys matter to that parse.
#
# Why a stand-in and not u-bogdan itself: with terraform/bootstrap/removed.tf
# in the configuration, a plan that still declares u-bogdan's instances fails
# ("Moved object still exists"). The test maps the stand-in's id to u-bogdan.

tenants = {
  eng = {
    kind      = "group"
    principal = "eng@saga.xyz"
    providers = ["anthropic", "openai"]
  }

  smoke = {
    kind      = "group"
    principal = "smoke@saga.xyz"
    providers = ["anthropic"]
  }

  u-sw-c90291 = {
    kind      = "user"
    principal = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    providers = []
  }

  w9-stand-in = {
    kind      = "user"
    principal = "w9-stand-in@saga.xyz"
    providers = ["anthropic"]
  }
}
