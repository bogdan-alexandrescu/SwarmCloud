# The CI deployer's project-level roles, scoped one at a time.
#
# terraform/bootstrap/deployer_conditions.tf writes a conditioned grant for
# every deployer role IAM can scope, each switched on by naming the role in
# `deployer_scoped_roles`. These runs assert the properties that make that safe
# to apply one role per release:
#
#   * switching nothing changes nothing -- merging the conditions is inert;
#   * switching a role REPLACES its project-wide grant rather than adding a
#     conditioned one beside it, which would grant exactly what the
#     unconditioned one already does;
#   * every expression stays inside IAM's 12-operator limit, which is otherwise
#     discovered at apply;
#   * replayed against the live inventory of saga-agents-staging (read-only
#     gcloud, 2026-09-24), every name of ours is admitted and every name of the
#     other team's is refused;
#   * the scoping terraform/bootstrap/terraform.tfvars names, read from the
#     file, moves that role's grants and nothing else;
#   * every project-level role terraform/infra grants is one the scoped
#     projectIamAdmin may still grant -- the check that turns a mid-release 403
#     into a failed pull request -- and every project-level grant declared in
#     the files CI applies is one a parity assertion here reads.
#
# The replay compares names against the prefix lists the expressions are
# rendered from, and a separate assertion holds each rendered expression to
# exactly those prefixes, so a prefix cannot be tested here and missing there.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap since IAP membership moved into it (main,
  # #23). It has no default on purpose, so every bootstrap run here needs one;
  # the value only has to pass the variable's validations.
  frontend_iap_members = ["domain:example.com"]
}

run "merging_the_conditions_changes_no_live_grant" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  assert {
    condition     = length(google_project_iam_member.deployer_roles) == length(var.deployer_roles)
    error_message = "with nothing in deployer_scoped_roles every deployer role must still be granted exactly as it is today"
  }

  assert {
    condition     = length(google_project_iam_member.deployer_secrets) == 1
    error_message = "the unconditioned swarmSecretProvisioner grant must stay until it is switched"
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.deployer_network_admin) == 0,
      length(google_project_iam_member.deployer_security_admin) == 0,
      length(google_project_iam_member.deployer_container_admin) == 0,
      length(google_project_iam_member.deployer_datastore_owner) == 0,
      length(google_project_iam_member.deployer_logging_config_writer) == 0,
      length(google_project_iam_member.deployer_project_iam_admin) == 0,
      length(google_project_iam_member.deployer_secrets_scoped) == 0,
    ])
    error_message = "no conditioned grant may exist before its role is switched; merging the conditions must change nothing live"
  }
}

run "every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.this
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_scoped_roles = [
      "roles/compute.networkAdmin",
      "roles/compute.securityAdmin",
      "roles/container.admin",
      "roles/datastore.owner",
      "roles/logging.configWriter",
      "roles/resourcemanager.projectIamAdmin",
      "swarmSecretProvisioner",
    ]
  }

  # A conditioned binding next to an unconditioned one for the same role grants
  # what the unconditioned one grants. The switch is only a narrowing if the
  # project-wide grant is gone in the same plan.
  assert {
    condition = !anytrue([
      for r in var.deployer_scoped_roles : contains(keys(google_project_iam_member.deployer_roles), r)
    ])
    error_message = "a scoped role is still granted project-wide; its condition would narrow nothing"
  }

  assert {
    condition     = length(google_project_iam_member.deployer_roles) == length(var.deployer_roles) - 6
    error_message = "exactly the six scoped predefined roles leave the project-wide grant; the ten unscopable ones stay"
  }

  assert {
    condition     = length(google_project_iam_member.deployer_secrets) == 0
    error_message = "the unconditioned swarmSecretProvisioner grant must go when its scoped grant arrives"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.deployer_network_admin[0].condition[0].expression == local.deployer_conditions["roles/compute.networkAdmin"],
      google_project_iam_member.deployer_security_admin[0].condition[0].expression == local.deployer_conditions["roles/compute.securityAdmin"],
      google_project_iam_member.deployer_container_admin[0].condition[0].expression == local.deployer_conditions["roles/container.admin"],
      google_project_iam_member.deployer_datastore_owner[0].condition[0].expression == local.deployer_conditions["roles/datastore.owner"],
      google_project_iam_member.deployer_logging_config_writer[0].condition[0].expression == local.deployer_conditions["roles/logging.configWriter"],
      google_project_iam_member.deployer_project_iam_admin[0].condition[0].expression == local.deployer_conditions["roles/resourcemanager.projectIamAdmin"],
      google_project_iam_member.deployer_secrets_scoped[0].condition[0].expression == local.deployer_conditions["swarmSecretProvisioner"],
    ])
    error_message = "every scoped grant must carry its own condition, not a neighbour's"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.deployer_network_admin[0].role == "roles/compute.networkAdmin",
      google_project_iam_member.deployer_security_admin[0].role == "roles/compute.securityAdmin",
      google_project_iam_member.deployer_container_admin[0].role == "roles/container.admin",
      google_project_iam_member.deployer_datastore_owner[0].role == "roles/datastore.owner",
      google_project_iam_member.deployer_logging_config_writer[0].role == "roles/logging.configWriter",
      google_project_iam_member.deployer_project_iam_admin[0].role == "roles/resourcemanager.projectIamAdmin",
      google_project_iam_member.deployer_secrets_scoped[0].role == "projects/saga-agents-staging/roles/swarmSecretProvisioner",
    ])
    error_message = "a scoped grant must be for the role it replaces"
  }

  # docs.cloud.google.com/iam/quotas: 12 logic operators per condition. Over
  # that the apply fails, and nothing before the apply notices.
  assert {
    condition = alltrue([
      for r, e in local.deployer_conditions :
      length(regexall("&&", e)) + length(regexall("\\|\\|", e)) + length(regexall("![^=]", e)) <= 12
    ])
    error_message = "a condition exceeds IAM's 12 logical operators; split it into a second binding rather than lengthening it"
  }

  # The type guard is what keeps a name test from refusing every permission in
  # the role that IAM cannot name (a list on the project, an operation).
  assert {
    condition = alltrue([
      for r, s in local.deployer_type_scoped : startswith(local.deployer_conditions[r], "(resource.type != ")
    ])
    error_message = "every name-scoped condition must open with its resource-type guard"
  }

  # The rendered expression is exactly the lists the replays below test.
  assert {
    condition = alltrue([
      for r, s in local.deployer_type_scoped : alltrue(concat(
        [for t in s.types : strcontains(local.deployer_conditions[r], "resource.type != \"${t}\"")],
        [for p in s.prefixes : strcontains(local.deployer_conditions[r], "resource.name.startsWith(\"${p}\")")],
        [length(regexall("startsWith", local.deployer_conditions[r])) == length(s.prefixes)],
      ))
    ])
    error_message = "a rendered condition differs from the type and prefix lists it is built from"
  }
}

run "the_network_condition_refuses_their_keycloak_and_argocd_load_balancer" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/compute.networkAdmin"]
  }

  # Ours, per type IAM names, from terraform/infra's state.
  assert {
    condition = alltrue([
      for n in [
        "projects/saga-agents-staging/global/backendServices/swarm-ui-backend",
        "projects/saga-agents-staging/global/backendServices/swarm-ui-ui-backend",
        "projects/saga-agents-staging/global/forwardingRules/swarm-ui-http",
        "projects/saga-agents-staging/global/forwardingRules/swarm-ui-https",
        "projects/saga-agents-staging/global/targetHttpProxies/swarm-ui-http-proxy",
        "projects/saga-agents-staging/global/targetHttpsProxies/swarm-ui-https-proxy",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.networkAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "a load-balancer resource terraform/infra manages is refused; the next release would 403 on it"
  }

  # Theirs: the GKE Gateway in front of their services, and their nodes.
  assert {
    condition = !anytrue([
      for n in [
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-api-service-api-service-8080-5ieure64p6a2",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-argocd-argocd-server-80-zblgmjdai6zf",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-browser-engine-browser-engine-7101-eamagczv4ba6",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-crawling-serv-crawling-service-we-8088-wurq2kzjfbve",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-gateway-system-gw-serve404-80-m5di3rlf0xh3",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-gateway-system-gw-serve500-80-7wyoqghzztp7",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-keycloak-keycloak-8080-ryv2aw1g3cv9",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-promptlab-promptlab-api-8080-ohq3no6fp8sg",
        "projects/saga-agents-staging/global/backendServices/gkegw1-7hi6-promptlab-promptlab-web-3000-nfgf0nb3q201",
        "projects/saga-agents-staging/global/forwardingRules/gkegw1-7hi6-gateway-system-external-https-vcwkifae8uuy",
        "projects/saga-agents-staging/global/targetHttpsProxies/gkegw1-7hi6-gateway-system-external-https-u7vmggfcx5ue",
        "projects/saga-agents-staging/zones/us-central1-a/instances/gke-agents-staging-nap-e2-standard-2--b59db5d4-bnof",
        "projects/saga-agents-staging/zones/us-central1-a/instances/gke-agents-staging-nap-e2-standard-2--b59db5d4-h2ug",
        "projects/saga-agents-staging/zones/us-central1-a/instances/gke-agents-staging-nap-e2-standard-2--b59db5d4-xstq",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.networkAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "the network condition admits a load balancer or VM belonging to another team"
  }

  assert {
    condition = alltrue([
      for t in [
        "compute.googleapis.com/BackendService",
        "compute.googleapis.com/GlobalForwardingRule",
        "compute.googleapis.com/TargetHttpsProxy",
        "compute.googleapis.com/Instance",
      ] : contains(local.deployer_type_scoped["roles/compute.networkAdmin"].types, t)
    ])
    error_message = "every type their listed resources have must be a governed type, or the guard lets them through regardless of name"
  }
}

run "the_firewall_condition_refuses_their_gke_rules" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/compute.securityAdmin"]
  }

  assert {
    condition = alltrue([
      for n in [
        "projects/saga-agents-staging/global/firewalls/swarm-fw-allow-gke-webhooks",
        "projects/saga-agents-staging/global/firewalls/swarm-fw-allow-health-checks",
        "projects/saga-agents-staging/global/firewalls/swarm-fw-allow-internal",
        "projects/saga-agents-staging/global/firewalls/swarm-fw-deny-all-ingress",
        "projects/saga-agents-staging/global/firewalls/swarm-fw-deny-worker-ingress",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.securityAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "a firewall rule terraform/infra manages is refused"
  }

  # Theirs, and the two GKE reconciles for our own cluster: CI has no business
  # editing either.
  assert {
    condition = !anytrue([
      for n in [
        "projects/saga-agents-staging/global/firewalls/default-allow-icmp",
        "projects/saga-agents-staging/global/firewalls/default-allow-internal",
        "projects/saga-agents-staging/global/firewalls/default-allow-rdp",
        "projects/saga-agents-staging/global/firewalls/default-allow-ssh",
        "projects/saga-agents-staging/global/firewalls/gke-agents-staging-a2455d7c-all",
        "projects/saga-agents-staging/global/firewalls/gke-agents-staging-a2455d7c-exkubelet",
        "projects/saga-agents-staging/global/firewalls/gke-agents-staging-a2455d7c-inkubelet",
        "projects/saga-agents-staging/global/firewalls/gke-agents-staging-a2455d7c-vms",
        "projects/saga-agents-staging/global/firewalls/gkegw1-7hi6-l7-agents-staging-vpc-global",
        "projects/saga-agents-staging/global/firewalls/gke-swarm-autopilot-4b0b02c7-all",
        "projects/saga-agents-staging/global/firewalls/gke-swarm-autopilot-4b0b02c7-vms",
        "projects/saga-agents-staging/zones/us-central1-a/instances/gke-agents-staging-nap-e2-standard-2--b59db5d4-bnof",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.securityAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "the firewall condition admits a rule or VM terraform/infra does not manage"
  }
}

run "the_cluster_condition_refuses_agents_staging_under_either_name" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/container.admin"]
  }

  assert {
    condition = alltrue([
      for n in [
        "projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot",
        "projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot/nodePools/default-pool",
      ] : anytrue([for p in local.deployer_type_scoped["roles/container.admin"].prefixes : startswith(n, p)])
    ])
    error_message = "the swarm cluster is refused; terraform/infra could not refresh it"
  }

  # agents-staging is zonal (us-central1-a), and GKE accepts both spellings.
  assert {
    condition = !anytrue([
      for n in [
        "projects/saga-agents-staging/zones/us-central1-a/clusters/agents-staging",
        "projects/saga-agents-staging/locations/us-central1-a/clusters/agents-staging",
      ] : anytrue([for p in local.deployer_type_scoped["roles/container.admin"].prefixes : startswith(n, p)])
    ])
    error_message = "the cluster condition admits agents-staging, another team's live cluster"
  }
}

run "the_firestore_condition_admits_swarm_and_nothing_named_otherwise" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/datastore.owner"]
  }

  assert {
    condition = anytrue([
      for p in local.deployer_type_scoped["roles/datastore.owner"].prefixes :
      startswith("projects/saga-agents-staging/databases/swarm", p)
    ])
    error_message = "the swarm database is refused; terraform/infra manages it and its indexes"
  }

  # No other database exists today. `(default)` is the one a second team would
  # most plausibly create, and bindings.tf warns about exactly that.
  assert {
    condition = !anytrue([
      for p in local.deployer_type_scoped["roles/datastore.owner"].prefixes :
      startswith("projects/saga-agents-staging/databases/(default)", p)
    ])
    error_message = "the Firestore condition admits a database that is not the platform's"
  }

  # The documented type. `datastore.googleapis.com/Database`, which the
  # 2026-09-16 condition tested, is not one IAM lists.
  assert {
    condition     = local.deployer_type_scoped["roles/datastore.owner"].types == ["firestore.googleapis.com/Database"]
    error_message = "the Firestore type must be the one IAM documents, firestore.googleapis.com/Database"
  }
}

run "the_logging_condition_leaves_the_shared_buckets_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/logging.configWriter"]
  }

  # _Default and _Required are one per project and terraform manages neither,
  # so there is nothing of ours to admit.
  assert {
    condition     = length(local.deployer_type_scoped["roles/logging.configWriter"].prefixes) == 0
    error_message = "no log bucket or view is the platform's; the logging condition must admit none"
  }

  assert {
    condition = (
      contains(local.deployer_type_scoped["roles/logging.configWriter"].types, "logging.googleapis.com/LogBucket") &&
      contains(local.deployer_type_scoped["roles/logging.configWriter"].types, "logging.googleapis.com/LogView")
    )
    error_message = "both bucket and view administration must be refused"
  }
}

run "the_secret_condition_refuses_all_63_of_their_secrets" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.this
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["swarmSecretProvisioner"]
  }

  # Secret Manager names by project NUMBER; the project id never matches.
  assert {
    condition = alltrue([
      for p in local.deployer_type_scoped["swarmSecretProvisioner"].prefixes :
      startswith(p, "projects/209012342332/secrets/")
    ])
    error_message = "Secret Manager resource names use the project number; a prefix on the project id matches nothing"
  }

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
      ] : anytrue([for p in local.deployer_type_scoped["swarmSecretProvisioner"].prefixes : startswith(n, p)])
    ])
    error_message = "a secret of the platform's is refused"
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
      ] : anytrue([for p in local.deployer_type_scoped["swarmSecretProvisioner"].prefixes : startswith(n, p)])
    ])
    error_message = "the secret condition admits one of the other team's 63 secrets -- including setIamPolicy, which is a read"
  }
}

run "the_iam_admin_condition_refuses_every_role_ci_does_not_hand_out" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
  }

  # The attribute IAM provides for limiting role grants, on the allowlist.
  assert {
    condition = (
      startswith(local.deployer_conditions["roles/resourcemanager.projectIamAdmin"], "api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([") &&
      alltrue([
        for r in local.deployer_grantable_project_roles :
        strcontains(local.deployer_conditions["roles/resourcemanager.projectIamAdmin"], "\"${r}\"")
      ])
    )
    error_message = "projectIamAdmin must be limited by modifiedGrantsByRole to exactly the grantable roles"
  }

  # The plan terraform.tfvars produces is asserted in its own run below, from
  # the file itself; this run's scoping is written out because it tests the
  # condition, not the file.

  # Every role in the live project policy that terraform/infra does not grant
  # (52 of 67, re-read 2026-09-24 with `gcloud projects get-iam-policy`; the
  # first reading, 51 of 65, predates roles/logging.serviceAgent). The
  # deployer's own roles are here too: they are granted by bootstrap, which the
  # owner applies, never by CI.
  assert {
    condition = !anytrue([
      for r in [
        "projects/saga-agents-staging/roles/swarmSecretProvisioner",
        "roles/artifactregistry.admin",
        "roles/artifactregistry.reader",
        "roles/artifactregistry.serviceAgent",
        "roles/certificatemanager.admin",
        "roles/cloudbuild.builds.builder",
        "roles/cloudbuild.builds.editor",
        "roles/cloudbuild.serviceAgent",
        "roles/cloudscheduler.admin",
        "roles/cloudscheduler.serviceAgent",
        "roles/cloudsql.admin",
        "roles/compute.admin",
        "roles/compute.instanceGroupManagerServiceAgent",
        "roles/compute.loadBalancerAdmin",
        "roles/compute.networkAdmin",
        "roles/compute.securityAdmin",
        "roles/compute.serviceAgent",
        "roles/container.admin",
        "roles/container.defaultNodeServiceAgent",
        "roles/container.developer",
        "roles/container.serviceAgent",
        "roles/containeranalysis.ServiceAgent",
        "roles/containerregistry.ServiceAgent",
        "roles/containerscanning.ServiceAgent",
        "roles/datastore.owner",
        "roles/editor",
        "roles/file.serviceAgent",
        "roles/firebaserules.system",
        "roles/firestore.serviceAgent",
        "roles/iam.roleAdmin",
        "roles/iam.securityReviewer",
        "roles/iam.serviceAccountAdmin",
        "roles/iam.serviceAccountUser",
        "roles/iam.workloadIdentityPoolAdmin",
        "roles/iap.admin",
        "roles/logging.admin",
        "roles/logging.configWriter",
        "roles/logging.serviceAgent",
        "roles/logging.viewer",
        "roles/monitoring.admin",
        "roles/monitoring.editor",
        "roles/owner",
        "roles/pubsub.admin",
        "roles/pubsub.serviceAgent",
        "roles/resourcemanager.projectIamAdmin",
        "roles/run.admin",
        "roles/run.serviceAgent",
        "roles/secretmanager.admin",
        "roles/secretmanager.secretAccessor",
        "roles/servicenetworking.serviceAgent",
        "roles/serviceusage.serviceUsageAdmin",
        "roles/storage.admin",
      ] : contains(local.deployer_grantable_project_roles, r)
    ])
    error_message = "CI may grant a role terraform/infra never grants -- owner, editor, another team's, or its own"
  }
}

# ---------------------------------------------------------------------------
# THE PLAN terraform/bootstrap/terraform.tfvars PRODUCES, from the file.
#
# terraform test never reads a root's terraform.tfvars, so every run above
# writes its scoping out by hand -- right for testing a condition, wrong for
# claiming "this is what the owner's apply does". These two runs read the file
# (./bootstrap_tfvars) and plan bootstrap with exactly what it names.
# ---------------------------------------------------------------------------

run "terraform_tfvars_as_committed" {
  command = plan

  module {
    source = "./bootstrap_tfvars"
  }

  assert {
    condition     = output.path != "" && output.assignments == 1
    error_message = "terraform/bootstrap/terraform.tfvars was not found, or does not hold exactly one top-level `deployer_scoped_roles = [...]`; the run below would plan an empty list and describe a file nobody applies"
  }
}

run "the_scoping_terraform_tfvars_names_moves_its_own_grants_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = run.terraform_tfvars_as_committed.deployer_scoped_roles
  }

  # For whatever the file names: each named role's project-wide grant is gone
  # and its conditioned grant exists, and no other deployer grant moves. The
  # first run in this file is the control -- with nothing switched, no
  # conditioned grant exists.
  assert {
    condition = alltrue([
      toset(keys(google_project_iam_member.deployer_roles)) == setsubtract(toset(var.deployer_roles), var.deployer_scoped_roles),
      length(google_project_iam_member.deployer_secrets) == (contains(var.deployer_scoped_roles, "swarmSecretProvisioner") ? 0 : 1),
      length(google_project_iam_member.deployer_secrets_scoped) == (contains(var.deployer_scoped_roles, "swarmSecretProvisioner") ? 1 : 0),
      length(google_project_iam_member.deployer_network_admin) == (contains(var.deployer_scoped_roles, "roles/compute.networkAdmin") ? 1 : 0),
      length(google_project_iam_member.deployer_security_admin) == (contains(var.deployer_scoped_roles, "roles/compute.securityAdmin") ? 1 : 0),
      length(google_project_iam_member.deployer_container_admin) == (contains(var.deployer_scoped_roles, "roles/container.admin") ? 1 : 0),
      length(google_project_iam_member.deployer_datastore_owner) == (contains(var.deployer_scoped_roles, "roles/datastore.owner") ? 1 : 0),
      length(google_project_iam_member.deployer_logging_config_writer) == (contains(var.deployer_scoped_roles, "roles/logging.configWriter") ? 1 : 0),
      length(google_project_iam_member.deployer_project_iam_admin) == (contains(var.deployer_scoped_roles, "roles/resourcemanager.projectIamAdmin") ? 1 : 0),
    ])
    error_message = "the scoping terraform.tfvars names moves a deployer grant other than its own roles' two halves; the owner's apply would do more than the tfvars line says"
  }

  # What the file names TODAY, and so what the targeted apply command in its
  # comment, and the plan PR #73 derived (1 to add, 0 to change, 1 to destroy
  # against a live policy with nothing scoped), describe.
  assert {
    condition     = var.deployer_scoped_roles == toset([])
    error_message = "terraform/bootstrap/terraform.tfvars no longer scopes exactly roles/resourcemanager.projectIamAdmin, so the targeted apply command in its comment and the plan derived for it describe a different change. Update the command, that plan and this assertion together."
  }
}

run "a_role_with_no_conditioned_grant_cannot_be_switched" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "saga/agent-swarm-infra"
    deployer_scoped_roles = ["roles/pubsub.admin"]
  }

  # It would leave deployer_roles' for_each with nothing created in its place:
  # a role silently revoked, discovered by the next release.
  expect_failures = [var.deployer_scoped_roles]
}

run "a_role_the_deployer_does_not_hold_cannot_be_switched" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
    deployer_roles = [
      "roles/artifactregistry.admin",
      "roles/cloudbuild.builds.editor",
    ]
    deployer_scoped_roles = ["roles/container.admin"]
  }

  # Scoping is a narrowing. Naming a role the deployer does not hold would
  # GRANT it.
  expect_failures = [var.deployer_scoped_roles]
}

# ---------------------------------------------------------------------------
# PARITY: every project-level role terraform/infra grants must be grantable.
#
# The allowlist lives in bootstrap and the grants live in three places in
# terraform/infra. Without these runs, a new project-level grant in any of them
# is discovered as a 403 halfway through a release, after projectIamAdmin is
# scoped. They plan each place and read the list from the bootstrap run above.
# ---------------------------------------------------------------------------

run "every_project_role_the_iam_module_grants_is_grantable" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    artifact_bucket  = "swarm-artifacts-saga-agents-staging"
    labels           = { "managed-by" = "swarm-terraform" }
    gke_cluster_name = "swarm-autopilot"
    gke_location     = "us-central1"
  }

  assert {
    condition = alltrue(concat(
      [for k, m in google_project_iam_member.plain : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, m.role)],
      [for k, m in google_project_iam_member.gke : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, m.role)],
      [for k, m in google_project_iam_member.firestore : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, m.role)],
      [contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, google_project_iam_member.broker_version_adder.role)],
    ))
    error_message = "modules/iam grants a project-level role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles in terraform/bootstrap/deployer_conditions.tf"
  }
}

run "every_project_role_the_tenancy_module_grants_is_grantable" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    artifact_bucket        = "swarm-artifacts-saga-agents-staging"
    workload_identity_pool = "saga-agents-staging.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = ["anthropic"]
      }
    }
    dispatcher_members = {
      scheduler  = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
      reconciler = "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # The worker grant names the custom role by its computed id.
  override_resource {
    target          = google_project_iam_custom_role.worker_firestore
    override_during = plan
    values = {
      id      = "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
      name    = "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
      role_id = "swarmTenantWorkerFirestore"
    }
  }

  assert {
    condition = alltrue(concat(
      [for k, m in google_project_iam_member.worker_firestore : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, m.role)],
      [for k, m in google_project_iam_member.worker_telemetry : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, m.role)],
    ))
    error_message = "modules/tenancy grants a project-level role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }
}

run "every_project_role_the_verify_identity_holds_is_grantable" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"
    tenants     = {}

    # terraform/infra refuses to plan without a digest for every image it
    # deploys (main, #24: the precondition on google_cloud_run_v2_job.verify).
    # The same fixture infra_guards.tftest.hcl uses.
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  assert {
    condition = alltrue([
      contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, google_project_iam_member.verify_reads_firestore.role),
    ])
    error_message = "terraform/infra/verify.tf grants a project-level role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }
}

# The three runs above read grants by name, and a grant nobody named is checked
# by none of them. This one reads the files CI applies and this file, as text
# (./project_iam_inventory), and holds every declaration that writes the
# project's IAM policy to the grants an assertion above actually passes, by its
# role, to contains(...deployer_grantable_project_roles, ...). No list is kept
# by hand: a grant dropped from a parity assertion is un-read here as surely as
# a new grant nobody asserts on. Either fails this run -- the difference between
# a red pull request and a release that 403s halfway through its apply once
# projectIamAdmin is scoped.
run "every_project_level_grant_ci_applies_is_read_by_a_parity_run" {
  command = plan

  module {
    source = "./project_iam_inventory"
  }

  variables {
    parity_test_file = "deployer_iam.tftest.hcl"
  }

  assert {
    condition     = output.terraform_dir != "" && output.files_read > 0 && output.test_file_read
    error_message = "the inventory read no terraform/infra or terraform/modules file, or not deployer_iam.tftest.hcl; the assertions below would be comparing against nothing"
  }

  # The defect: a grant CI applies that no parity assertion holds to the
  # grantable list.
  assert {
    condition     = length(output.uncovered) == 0
    error_message = "terraform/infra or a module under terraform/modules declares a project-level IAM grant that no assertion above passes, by its role, to contains(...deployer_grantable_project_roles, ...). Add that to the run planning its directory; unread, it is found by the release that applies it, as a 403, once projectIamAdmin is scoped"
  }

  # The control: every grant an assertion reads is found by the declaration
  # scan, so the scan is matching real declarations and not passing on an empty
  # list.
  assert {
    condition     = length(output.stale) == 0
    error_message = "an assertion above reads a project-level grant the declaration scan did not find: the scan stopped matching, or the grant lives in a directory it does not read"
  }

  # A module fetched from a registry or a git URL, or from a path out of
  # terraform/infra and terraform/modules, is one this scan never reads.
  assert {
    condition     = length(output.module_sources) > 0 && length(output.unscanned_module_sources) == 0
    error_message = "terraform/infra or a module calls a module whose files the inventory does not scan (or no module block was found at all), so a project-level grant declared there would be checked by nothing"
  }
}

# ---------------------------------------------------------------------------
# storage.admin's condition (wif.tf, deployer_storage) -- the two defects that
# would have failed the first build after it was applied.
# ---------------------------------------------------------------------------

run "the_build_can_upload_its_source_and_list_its_bucket" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "saga/agent-swarm-infra"
  }

  # The source tarball is an OBJECT in the Cloud Build bucket. An equality test
  # on the bucket admits the bucket and refuses every object in it.
  assert {
    condition     = strcontains(google_project_iam_member.deployer_storage[0].condition[0].expression, "resource.name.startsWith(\"projects/_/buckets/saga-agents-staging_cloudbuild/objects/\")")
    error_message = "the storage condition admits the Cloud Build bucket but not the objects in it; gcloud builds submit could not upload its source"
  }

  # Replayed against the eight live buckets (2026-09-24), mirroring the three
  # clause shapes the expression is rendered from.
  assert {
    condition = alltrue([
      for n in [
        "projects/_/buckets/swarm-access-logs-saga-agents-staging",
        "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/x",
        "projects/_/buckets/swarm-tfstate-logs-saga-agents-staging",
        "projects/_/buckets/swarm-tfstate-saga-agents-staging/objects/infra/dev/default.tfstate",
        "projects/_/buckets/saga-agents-staging_cloudbuild",
        "projects/_/buckets/saga-agents-staging_cloudbuild/objects/source/1727000000.0-0123456789abcdef.tgz",
      ] :
      anytrue(concat(
        [for b in var.deployer_storage_bucket_prefixes : startswith(n, "projects/_/buckets/${b}")],
        [for b in var.deployer_storage_buckets_exact : n == "projects/_/buckets/${b}" || startswith(n, "projects/_/buckets/${b}/objects/")],
      ))
    ])
    error_message = "a bucket or object the release writes to is refused by the storage condition"
  }

  assert {
    condition = !anytrue([
      for n in [
        "projects/_/buckets/saga-agents-crawled-media-staging",
        "projects/_/buckets/saga-agents-files-staging/objects/a",
        "projects/_/buckets/saga-agents-terraform-state-staging",
        "projects/_/buckets/saga-agents-terraform-state-staging/objects/default.tfstate",
        "projects/_/buckets/saga-agents-staging_cloudbuild-lookalike",
      ] :
      anytrue(concat(
        [for b in var.deployer_storage_bucket_prefixes : startswith(n, "projects/_/buckets/${b}")],
        [for b in var.deployer_storage_buckets_exact : n == "projects/_/buckets/${b}" || startswith(n, "projects/_/buckets/${b}/objects/")],
      ))
    ])
    error_message = "the storage condition admits one of the other team's buckets, or a bucket that only starts with the Cloud Build bucket's name"
  }

  # buckets.list is checked on the PROJECT, which no resource.name condition
  # can admit, and `gcloud builds submit` lists buckets to check ownership of
  # its staging bucket. It needs a grant of its own, with no condition.
  assert {
    condition     = google_project_iam_custom_role.deployer_project_buckets[0].permissions == toset(["storage.buckets.create", "storage.buckets.list"])
    error_message = "the project-level bucket role must be exactly list and create; anything else checked at the project level (hmacKeys above all) reaches other teams' data"
  }

  assert {
    condition     = length(google_project_iam_member.deployer_project_buckets[0].condition) == 0
    error_message = "a parent-only permission cannot be admitted by a resource.name condition; this grant must be unconditioned"
  }
}
