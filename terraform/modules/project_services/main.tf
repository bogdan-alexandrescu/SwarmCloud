# Service API enablement.
#
# All 18 APIs this platform needs are already enabled on saga-agents-staging.
# These resources exist so the dependency graph is explicit and so a fresh
# project can be stood up from zero -- not to flip anything off. Both disable
# flags are pinned false by variable validation because this project is shared.

resource "google_project_service" "this" {
  for_each = toset(var.services)

  project = var.project_id
  service = each.value

  disable_on_destroy         = var.disable_on_destroy
  disable_dependent_services = var.disable_dependent_services
}
