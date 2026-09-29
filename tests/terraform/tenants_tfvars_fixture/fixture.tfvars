# A stand-in for terraform/environments/dev/dev.tfvars, read by
# tests/terraform/deployer_sa_admin.tftest.hcl through terraform/bootstrap's
# infra_tenants_tfvars. Never applied. Its shape exercises the parse
# terraform/bootstrap/deployer_service_accounts.tf does on the real file: a
# block before tenants that also has two-space `x = {` lines, a tenant with
# nested objects whose keys sit at four spaces, a quoted tenant key, a comment
# that looks like a tenant, and a block after tenants.
#
# The parse must find exactly alpha, b-2 and quoted.

project_id = "saga-agents-staging"

service_max_instances = {
  notatenant = {
    before = 1
  }
}

tenants = {
  # A comment that is not a tenant:
  # ghost = {
  alpha = {
    kind      = "group"
    principal = "alpha@example.com"

    nested = {
      deeper = {
        x = 1
      }
    }
  }

  b-2 = {
    kind      = "user"
    principal = "b@example.com"
  }

  "quoted" = {
    kind      = "user"
    principal = "q@example.com"
  }
}

after = {
  alsonotatenant = {
    x = 1
  }
}
