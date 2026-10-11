# A stand-in for terraform/environments/dev/dev.tfvars, read by
# tests/terraform/browser_logins.tftest.hcl through terraform/bootstrap's
# infra_tenants_tfvars. Never applied.
#
# Two personal tenants (kind = "user" and a `u-` id, the shape
# identity.tenant_id_for_user mints), and three that must get NO browser grant:
# a group, a group whose id happens to start `u-`, and a `kind = "user"`
# service tenant whose id does not.

project_id = "saga-agents-staging"

tenants = {
  eng = {
    kind      = "group"
    principal = "eng@example.com"
    providers = ["anthropic"]
  }

  u-grp = {
    kind      = "group"
    principal = "u-grp@example.com"
  }

  svc-bot = {
    kind      = "user"
    principal = "svc-bot@saga-agents-staging.iam.gserviceaccount.com"
  }

  u-alice = {
    kind      = "user"
    principal = "alice-bot@saga-agents-staging.iam.gserviceaccount.com"
  }

  "u-verify" = {
    kind            = "user"
    principal       = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    directory_group = false
  }
}
