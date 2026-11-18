# A stand-in for the deployment's config/dev/dev.tfvars (kept in the state
# bucket, never in this repository), read by
# tests/terraform/oracle_api_tenant.tftest.hcl and deployer_sa_admin.tftest.hcl
# through terraform/bootstrap's infra_tenants_tfvars. Never applied. Its
# tenants are the four oracle_api_tenant.tftest.hcl's terraform/infra run
# writes out: the two groups, the verification gate's and oracle-api's own.

project_id = "example-project"

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
  }

  u-or-9a0792 = {
    kind      = "user"
    principal = "oracle-api@example-project.iam.gserviceaccount.com"
  }
}
