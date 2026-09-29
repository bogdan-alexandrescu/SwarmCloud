# A tenants block holding a heredoc. The heredoc's body has a line shaped like a
# tenant, which the pattern parse in terraform/bootstrap/deployer_service_accounts.tf
# would count as one: an account the release deployer would be granted
# serviceAccountAdmin on without any tenant of that name. So bootstrap refuses
# to plan against a tenants block containing `<<` at all.

project_id = "saga-agents-staging"

tenants = {
  alpha = {
    kind      = "group"
    principal = "alpha@example.com"
    notes     = <<-EOT
  smuggled = {
    EOT
  }
}
