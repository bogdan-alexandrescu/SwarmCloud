# IAP accepts a developer's OWN sign-in only from an allowlisted OAuth client,
# and the allowlist is set on OUR backend services and nowhere wider.
#
# OWNER DECISION 2026-09-25: the `sc` plugin lets any developer act as
# themselves through IAP with ONE Desktop OAuth client per deployment. This
# deployment's IAP uses a Google-managed OAuth client, and Google's docs say so
# plainly: "Google-managed OAuth clients cannot programmatically access
# IAP-protected applications. However, IAP-protected applications that use the
# Google-managed OAuth client can still be accessed programmatically using a
# separate OAuth client configured through the programmatic_clients setting"
# (cloud.google.com/iap/docs/custom-oauth-configuration). The setting can be
# made per backend service -- `gcloud iap settings set --resource-type=compute
# --service=BACKEND_SERVICE_NAME` in cloud.google.com/iap/docs/deprecations/
# migrate-oauth-client -- which is the only scope acceptable in
# saga-agents-staging, where nine of the ten IAP backends are another team's
# (their Keycloak and ArgoCD among them).
#
# What these runs hold terraform/bootstrap/iap_programmatic_clients.tf to:
#
#   * with no client configured -- the default -- NOTHING is created, so
#     merging this changes no live setting until the owner opts in;
#   * with one, there is exactly one settings resource per backend this root
#     already manages accessors for, each named at the SERVICE level
#     (`.../iap_web/compute/services/<swarm backend>`), never at the project,
#     `iap_web` or `compute` level that would reach the other team's backends;
#   * the value is a Desktop OAuth client ID, never a secret or a URL.
#
# A mock provider proves the configuration says what was meant. It does not
# prove that IAP accepts an allowlisted client's ID token -- that is the
# runbook's verification step, docs/runbooks/iap-desktop-client.md.
#
# ORDER MATTERS FOR PROVING THESE RED (see verify_logs.tftest.hcl): runs that
# only assert come first, expect_failures runs last.

mock_provider "google" {}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]
}

run "no_client_configured_changes_nothing" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = length(google_iap_settings.frontend_programmatic_clients) == 0
    error_message = "with no programmatic client configured, no IAP setting may be written"
  }
}

run "a_desktop_client_is_allowlisted_on_our_backends_and_nowhere_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.iap_programmatic
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    frontend_iap_programmatic_clients = ["209012342332-abcdef0123.apps.googleusercontent.com"]
  }

  assert {
    condition     = toset(keys(google_iap_settings.frontend_programmatic_clients)) == toset(var.frontend_iap_backends)
    error_message = "one settings resource per backend this root manages accessors for -- no more, no fewer"
  }

  assert {
    condition = alltrue([
      for backend, settings in google_iap_settings.frontend_programmatic_clients :
      settings.name == "projects/209012342332/iap_web/compute/services/${backend}"
    ])
    error_message = "each setting must be named at the backend-SERVICE level; a project, iap_web or compute level name would reach the other team's backends"
  }

  assert {
    condition = alltrue([
      for backend, settings in google_iap_settings.frontend_programmatic_clients :
      startswith(backend, "swarm")
    ])
    error_message = "only the platform's own backends (prefix swarm) may be touched"
  }

  assert {
    condition = alltrue([
      for settings in google_iap_settings.frontend_programmatic_clients :
      settings.access_settings[0].oauth_settings[0].programmatic_clients == ["209012342332-abcdef0123.apps.googleusercontent.com"]
    ])
    error_message = "the allowlist must be exactly the configured Desktop client ids"
  }
}

run "a_client_secret_is_refused_where_a_client_id_belongs" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    frontend_iap_programmatic_clients = ["GOCSPX-this-is-a-secret"]
  }

  expect_failures = [var.frontend_iap_programmatic_clients]
}

run "a_url_is_refused_where_a_client_id_belongs" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    frontend_iap_programmatic_clients = ["https://swarm.saga.xyz"]
  }

  expect_failures = [var.frontend_iap_programmatic_clients]
}
