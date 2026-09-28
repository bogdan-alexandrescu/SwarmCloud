# secretmanager.versions.add for the quota broker, taken out of the project-wide
# swarmSecretLister role and granted per secret NAME instead.
#
# In saga-agents-staging, 63 of the 77 secrets belong to another team. With
# versions.add project-wide, a bug in the broker -- or anything running as it --
# could append a version to any of them, and a version is what a reader gets
# as `latest`.
#
# The change is two releases on purpose (terraform/modules/iam/custom_roles.tf
# has the measurement): the scoped grant first, the removal after it has
# propagated. These runs hold both halves.

mock_provider "google" {}

variables {
  project_id       = "saga-agents-staging"
  artifact_bucket  = "swarm-artifacts-saga-agents-staging"
  labels           = { "managed-by" = "swarm-terraform" }
  gke_cluster_name = "swarm-autopilot"
  gke_location     = "us-central1"
}

run "the_broker_may_add_versions_only_to_the_platforms_secrets" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  override_data {
    target          = data.google_project.this
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  # secretVersionAdder is {versions.add, secrets.rotate} plus project get/list:
  # nothing in it reads a payload.
  assert {
    condition     = google_project_iam_member.broker_version_adder.role == "roles/secretmanager.secretVersionAdder"
    error_message = "the scoped grant must be secretVersionAdder and nothing wider"
  }

  # The two families quota_broker.secretstore.owned_by_this_platform accepts,
  # by project NUMBER -- the only form Secret Manager uses in a condition.
  assert {
    condition = google_project_iam_member.broker_version_adder.condition[0].expression == join(" || ", [
      "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\")",
      "resource.name.startsWith(\"projects/209012342332/secrets/swarm-account-\")",
    ])
    error_message = "the broker's adder grant must be scoped to swarm-tenant- and swarm-account- secrets by project number"
  }

  # Replayed against all 77 secrets in the project (2026-09-24).
  assert {
    condition = alltrue([
      for n in [
        "projects/209012342332/secrets/swarm-account-eng--devops-main",
        "projects/209012342332/secrets/swarm-account-eng--devops-main-refresh",
        "projects/209012342332/secrets/swarm-account-eng--devops-team",
        "projects/209012342332/secrets/swarm-account-eng--devops-team-refresh",
        "projects/209012342332/secrets/swarm-account-eng--saga-personal",
        "projects/209012342332/secrets/swarm-account-eng--saga-personal-refresh",
        "projects/209012342332/secrets/swarm-account-eng--team",
        "projects/209012342332/secrets/swarm-account-eng--team-refresh",
        "projects/209012342332/secrets/swarm-tenant-eng-anthropic",
        "projects/209012342332/secrets/swarm-tenant-eng-anthropic-refresh",
        "projects/209012342332/secrets/swarm-tenant-eng-openai",
        "projects/209012342332/secrets/swarm-tenant-eng-openai-refresh",
        "projects/209012342332/secrets/swarm-tenant-u-bogdan-anthropic",
        "projects/209012342332/secrets/swarm-tenant-u-bogdan-anthropic-refresh",
      ] : anytrue([for p in local.broker_secret_prefixes : startswith(n, p)])
    ])
    error_message = "a secret the broker writes to is refused; an account's rotated refresh token would be lost"
  }

  assert {
    condition = !anytrue([
      for n in [
        "projects/209012342332/secrets/agents-aipipeline-db-password",
        "projects/209012342332/secrets/agents-aipipeline-sentry-dsn",
        "projects/209012342332/secrets/agents-alertmanager-slack-webhook-url",
        "projects/209012342332/secrets/agents-api-service-db-password",
        "projects/209012342332/secrets/agents-api-service-keycloak-admin-client-secret",
        "projects/209012342332/secrets/agents-api-service-secrets-key",
        "projects/209012342332/secrets/agents-api-service-sentry-dsn",
        "projects/209012342332/secrets/agents-api-service-typesafe-api-key",
        "projects/209012342332/secrets/agents-argocd-github-repo-ssh-private-key",
        "projects/209012342332/secrets/agents-argocd-github-webhook-secret",
        "projects/209012342332/secrets/agents-argocd-slack-bot-token",
        "projects/209012342332/secrets/agents-backend-packages-pat",
        "projects/209012342332/secrets/agents-browser-engine-cap-token-secret",
        "projects/209012342332/secrets/agents-browser-engine-login-proxy",
        "projects/209012342332/secrets/agents-browser-engine-sentry-dsn",
        "projects/209012342332/secrets/agents-browser-engine-service-db-password",
        "projects/209012342332/secrets/agents-browser-engine-service-token",
        "projects/209012342332/secrets/agents-crawling-service-apify-token",
        "projects/209012342332/secrets/agents-crawling-service-bright-data-customer-id",
        "projects/209012342332/secrets/agents-crawling-service-bright-data-password",
        "projects/209012342332/secrets/agents-crawling-service-credential-encryption-key",
        "projects/209012342332/secrets/agents-crawling-service-db-password",
        "projects/209012342332/secrets/agents-crawling-service-sentry-dsn",
        "projects/209012342332/secrets/agents-discord-gateway-db-password",
        "projects/209012342332/secrets/agents-discord-gateway-sentry-dsn",
        "projects/209012342332/secrets/agents-discord-ingest-token",
        "projects/209012342332/secrets/agents-discord-publisher-db-password",
        "projects/209012342332/secrets/agents-discord-publisher-sentry-dsn",
        "projects/209012342332/secrets/agents-dispatcher-service-db-password",
        "projects/209012342332/secrets/agents-dispatcher-service-sentry-dsn",
        "projects/209012342332/secrets/agents-grafana-admin-password",
        "projects/209012342332/secrets/agents-homepage-argocd-token",
        "projects/209012342332/secrets/agents-instagram-app-secret",
        "projects/209012342332/secrets/agents-instagram-publisher-db-password",
        "projects/209012342332/secrets/agents-instagram-publisher-sentry-dsn",
        "projects/209012342332/secrets/agents-instagram-publisher-session-encryption-key",
        "projects/209012342332/secrets/agents-instagram-webhook-verify-token",
        "projects/209012342332/secrets/agents-internal-google-oauth-client-id",
        "projects/209012342332/secrets/agents-internal-google-oauth-client-secret",
        "projects/209012342332/secrets/agents-keycloak-admin-password",
        "projects/209012342332/secrets/agents-keycloak-db-password",
        "projects/209012342332/secrets/agents-keycloak-e2e-customer-admin-password",
        "projects/209012342332/secrets/agents-keycloak-e2e-customer-manager-password",
        "projects/209012342332/secrets/agents-keycloak-e2e-platform-admin-password",
        "projects/209012342332/secrets/agents-keycloak-e2e-saga-admin-password",
        "projects/209012342332/secrets/agents-keycloak-smtp-password",
        "projects/209012342332/secrets/agents-keycloak-smtp-user",
        "projects/209012342332/secrets/agents-langsmith-api-key",
        "projects/209012342332/secrets/agents-metrics-exporter-db-password",
        "projects/209012342332/secrets/agents-migration-manager-db-password",
        "projects/209012342332/secrets/agents-sentry-dsn",
        "projects/209012342332/secrets/agents-support-slack-bot-token",
        "projects/209012342332/secrets/agents-tailscale-operator-client-id",
        "projects/209012342332/secrets/agents-tailscale-operator-client-secret",
        "projects/209012342332/secrets/agents-tournament-digest-bonoxs-api-key",
        "projects/209012342332/secrets/agents-user-engagement-crawler-db-password",
        "projects/209012342332/secrets/agents-user-engagement-crawler-sentry-dsn",
        "projects/209012342332/secrets/promptlab-db-dsn",
        "projects/209012342332/secrets/promptlab-fernet-key",
        "projects/209012342332/secrets/promptlab-galatia-ro-dsn",
        "projects/209012342332/secrets/promptlab-galatia-ro-dsn-psycopg",
        "projects/209012342332/secrets/promptlab-ghcr-dockerconfigjson",
        "projects/209012342332/secrets/promptlab-langsmith-shared-key",
      ] : anytrue([for p in local.broker_secret_prefixes : startswith(n, p)])
    ])
    error_message = "the broker's adder grant reaches one of the other team's 63 secrets"
  }

  # RELEASE 1: the project-wide permission stays until the scoped grant has
  # propagated, because removing it first can strand an account whose refresh
  # token has just rotated.
  assert {
    condition     = contains(google_project_iam_custom_role.secret_lister.permissions, "secretmanager.versions.add")
    error_message = "versions.add must stay in swarmSecretLister for the release that creates the scoped grant"
  }
}

run "release_two_removes_the_project_wide_permission" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    secret_lister_project_wide_versions_add = false
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.secret_lister.permissions, "secretmanager.versions.add")
    error_message = "with the switch off, swarmSecretLister must no longer carry versions.add project-wide"
  }

  # Everything else the role was argued for stays, including the owner's
  # 2026-09-22 retention decision (versions.list and versions.destroy).
  assert {
    condition = google_project_iam_custom_role.secret_lister.permissions == toset([
      "secretmanager.secrets.list",
      "secretmanager.secrets.create",
      "secretmanager.secrets.get",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
      "secretmanager.versions.list",
      "secretmanager.versions.destroy",
    ])
    error_message = "removing versions.add must remove nothing else from swarmSecretLister"
  }

  # And the replacement is still there to write through.
  assert {
    condition     = google_project_iam_member.broker_version_adder.role == "roles/secretmanager.secretVersionAdder"
    error_message = "the scoped grant must exist whenever the project-wide permission does not"
  }
}
