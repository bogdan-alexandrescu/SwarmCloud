# Two personal tenants where one's browser prefix starts with the other's:
# swarm-tenant-u-a-browser-x-browser-... begins swarm-tenant-u-a-browser-, so a
# prefix condition for u-a would hand u-a-browser-x's sessions to u-a's worker.
# terraform/bootstrap/browser_logins.tf must refuse to plan it. Never applied.

project_id = "saga-agents-staging"

tenants = {
  u-a = {
    kind      = "user"
    principal = "a-bot@saga-agents-staging.iam.gserviceaccount.com"
  }

  u-a-browser-x = {
    kind      = "user"
    principal = "x-bot@saga-agents-staging.iam.gserviceaccount.com"
  }
}
