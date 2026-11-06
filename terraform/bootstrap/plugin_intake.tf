# The managed plugins' intake job's identity (docs/design/managed-plugins.md
# §2.2; terraform/infra/plugins.tf). A platform account, so this root creates
# it and terraform/infra reads it as a data source (plan §8). It holds NO
# project role: its whole authority is plugins.tf's two conditioned grants on
# the plugins bucket -- read quarantine/, create under staging/ -- and it is
# given no secret, no database and no route out.
resource "google_service_account" "plugin_intake" {
  project      = var.project_id
  account_id   = "swarm-plugin-intake"
  display_name = "Swarm plugin intake"
  description  = "managed-by=swarm-terraform; the plugin intake job unpacks and checks one quarantined archive. Reads quarantine/, creates under staging/ of the plugins bucket; no project role, no secret, no database, no egress."
}
