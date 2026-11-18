# The managed plugins' bundle build identity (docs/design/managed-plugins.md
# §1.4.2, §15.2; terraform/infra/plugin-bundles.tf). A platform account, so
# this root creates it and terraform/infra reads it as a data source (plan §8).
# It holds NO grant here: its whole authority is plugin-bundles.tf's rows,
# planned only with builtins_managed "on". Until then the account exists and
# can do nothing, which costs nothing.
resource "google_service_account" "plugin_bundler" {
  project      = var.project_id
  account_id   = "swarm-plugin-bundler"
  display_name = "Swarm plugin bundle builder"
  description  = "managed-by=swarm-terraform; the bundle build of the managed plugins gstack and security-audit (managed-plugins.md section 1.4.2). Reads platform/sources/, the release images and the mirrors; pushes to the plugin-bundles repository only."
}
