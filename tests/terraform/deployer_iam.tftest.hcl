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
#   * replayed against the live inventory of example-project (read-only
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

# source: the shared defaults every suite that plans terraform/infra needs
# (mocks/google/kms.tfmock.hcl -- the step-spec key's enabled version 1).
mock_provider "google" {
  source = "./mocks/google"
}

# terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1):
# a mock provider cannot import, so the bucket's provider values are the
# override's and its configuration is still planned and asserted.
override_resource {
  target = google_storage_bucket.state
}

variables {
  project_id = "example-project"

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
    github_repository = "example-org/example-repo"
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
      number = "123456789012"
    }
  }

  variables {
    enable_github_wif = true
    github_repository = "example-org/example-repo"
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
      google_project_iam_member.deployer_secrets_scoped[0].condition[0].expression == local.deployer_conditions["swarmSecretProvisioner"],
    ])
    error_message = "every scoped grant must carry its own condition, not a neighbour's"
  }

  # projectIamAdmin is chunked (deployer_conditions.tf, #275): one instance per
  # chunk of at most 10 roles, rather than the single condition every other
  # scoped role gets, so it is asserted on its own.
  assert {
    condition = alltrue([
      for k, m in google_project_iam_member.deployer_project_iam_admin :
      m.condition[0].expression == local.deployer_project_iam_admin_conditions[k]
    ])
    error_message = "every projectIamAdmin chunk must carry its own chunk's condition, not a neighbour's"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.deployer_network_admin[0].role == "roles/compute.networkAdmin",
      google_project_iam_member.deployer_security_admin[0].role == "roles/compute.securityAdmin",
      google_project_iam_member.deployer_container_admin[0].role == "roles/container.admin",
      google_project_iam_member.deployer_datastore_owner[0].role == "roles/datastore.owner",
      google_project_iam_member.deployer_logging_config_writer[0].role == "roles/logging.configWriter",
      google_project_iam_member.deployer_secrets_scoped[0].role == "projects/example-project/roles/swarmSecretProvisioner",
    ])
    error_message = "a scoped grant must be for the role it replaces"
  }

  assert {
    condition = alltrue([
      for k, m in google_project_iam_member.deployer_project_iam_admin : m.role == "roles/resourcemanager.projectIamAdmin"
    ])
    error_message = "every projectIamAdmin chunk must be for the role it replaces"
  }

  # docs.cloud.google.com/iam/quotas: 12 logic operators per condition. Over
  # that the apply fails, and nothing before the apply notices. Every
  # projectIamAdmin chunk is one hasOnly() call and no && / || / !, so this
  # covers those too, at 0 operators each -- they are chunked because of the
  # 10-element hasOnly() limit, never this one.
  assert {
    condition = alltrue([
      for r, e in merge(local.deployer_conditions, local.deployer_project_iam_admin_conditions) :
      length(regexall("&&", e)) + length(regexall("\\|\\|", e)) + length(regexall("![^=]", e)) <= 12
    ])
    error_message = "a condition exceeds IAM's 12 logical operators; split it into a second binding rather than lengthening it"
  }

  # docs.cloud.google.com/iam/quotas also caps at 20 the role bindings in one
  # allow policy that may share a role and a principal with different
  # condition expressions -- one per projectIamAdmin chunk here, since they
  # are per-chunk bindings rather than one ORed expression (deployer_conditions.tf
  # explains why). 15 roles / 10 per chunk is 2 today (14 with enable_github_hooks_dedup_role
  # off, which this file runs, and 15 until #150 took swarmSecretLister off;
  # still 2 each time); this guards the day
  # deployer_grantable_project_roles grows past 100 roles (10 chunks), which
  # would need a different scheme, not a bigger number here. Half of 20, not
  # 20: the chunks are create_before_destroy (#227 box 26), so while a changed
  # role list replaces them the old and new bindings coexist. No documented
  # limit on total expression length was found for IAM conditions, so none is
  # asserted.
  assert {
    condition     = length(local.deployer_project_iam_admin_conditions) <= 10
    error_message = "projectIamAdmin needs more than 10 chunked bindings for the same role and principal; replaced create_before_destroy, old and new together would pass the 20 IAM's allow-policy quota allows. deployer_grantable_project_roles has outgrown per-chunk bindings and needs a different scheme"
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

  # IAM conditions have an undocumented 256-character description limit --
  # found only at apply, exactly like #275's hasOnly() list limit (see the
  # PR description; this catches its class of failure ahead of an apply).
  assert {
    condition = alltrue(concat(
      [
        for d in [
          google_project_iam_member.deployer_network_admin[0].condition[0].description,
          google_project_iam_member.deployer_security_admin[0].condition[0].description,
          google_project_iam_member.deployer_container_admin[0].condition[0].description,
          google_project_iam_member.deployer_datastore_owner[0].condition[0].description,
          google_project_iam_member.deployer_logging_config_writer[0].condition[0].description,
          google_project_iam_member.deployer_secrets_scoped[0].condition[0].description,
        ] : length(d) <= 256
      ],
      [
        for k, m in google_project_iam_member.deployer_project_iam_admin :
        length(m.condition[0].description) <= 256
      ],
    ))
    error_message = "an IAM condition description exceeds GCP's undocumented 256-character limit; apply refuses it the way #275's hasOnly() list was refused"
  }
}

run "the_network_condition_refuses_their_keycloak_and_argocd_load_balancer" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/compute.networkAdmin"]
  }

  # Ours, per type IAM names, from terraform/infra's state.
  assert {
    condition = alltrue([
      for n in [
        "projects/example-project/global/backendServices/swarm-ui-backend",
        "projects/example-project/global/backendServices/swarm-ui-ui-backend",
        "projects/example-project/global/forwardingRules/swarm-ui-http",
        "projects/example-project/global/forwardingRules/swarm-ui-https",
        "projects/example-project/global/targetHttpProxies/swarm-ui-http-proxy",
        "projects/example-project/global/targetHttpsProxies/swarm-ui-https-proxy",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.networkAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "a load-balancer resource terraform/infra manages is refused; the next release would 403 on it"
  }

  # Theirs: the GKE Gateway in front of their services, and their nodes.
  assert {
    condition = !anytrue([
      for n in [
        "projects/example-project/global/backendServices/gkegw1-7hi6-api-service-api-service-8080-5ieure64p6a2",
        "projects/example-project/global/backendServices/gkegw1-7hi6-argocd-argocd-server-80-zblgmjdai6zf",
        "projects/example-project/global/backendServices/gkegw1-7hi6-browser-engine-browser-engine-7101-eamagczv4ba6",
        "projects/example-project/global/backendServices/gkegw1-7hi6-crawling-serv-crawling-service-we-8088-wurq2kzjfbve",
        "projects/example-project/global/backendServices/gkegw1-7hi6-gateway-system-gw-serve404-80-m5di3rlf0xh3",
        "projects/example-project/global/backendServices/gkegw1-7hi6-gateway-system-gw-serve500-80-7wyoqghzztp7",
        "projects/example-project/global/backendServices/gkegw1-7hi6-keycloak-keycloak-8080-ryv2aw1g3cv9",
        "projects/example-project/global/backendServices/gkegw1-7hi6-peer-app-peer-app-api-8080-ohq3no6fp8sg",
        "projects/example-project/global/backendServices/gkegw1-7hi6-peer-app-peer-app-web-3000-nfgf0nb3q201",
        "projects/example-project/global/forwardingRules/gkegw1-7hi6-gateway-system-external-https-vcwkifae8uuy",
        "projects/example-project/global/targetHttpsProxies/gkegw1-7hi6-gateway-system-external-https-u7vmggfcx5ue",
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-bnof",
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-h2ug",
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-xstq",
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
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/compute.securityAdmin"]
  }

  assert {
    condition = alltrue([
      for n in [
        "projects/example-project/global/firewalls/swarm-fw-allow-gke-webhooks",
        "projects/example-project/global/firewalls/swarm-fw-allow-health-checks",
        "projects/example-project/global/firewalls/swarm-fw-allow-internal",
        "projects/example-project/global/firewalls/swarm-fw-deny-all-ingress",
        "projects/example-project/global/firewalls/swarm-fw-deny-worker-ingress",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.securityAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "a firewall rule terraform/infra manages is refused"
  }

  # Theirs, and the two GKE reconciles for our own cluster: CI has no business
  # editing either.
  assert {
    condition = !anytrue([
      for n in [
        "projects/example-project/global/firewalls/default-allow-icmp",
        "projects/example-project/global/firewalls/default-allow-internal",
        "projects/example-project/global/firewalls/default-allow-rdp",
        "projects/example-project/global/firewalls/default-allow-ssh",
        "projects/example-project/global/firewalls/gke-peer-cluster-a2455d7c-all",
        "projects/example-project/global/firewalls/gke-peer-cluster-a2455d7c-exkubelet",
        "projects/example-project/global/firewalls/gke-peer-cluster-a2455d7c-inkubelet",
        "projects/example-project/global/firewalls/gke-peer-cluster-a2455d7c-vms",
        "projects/example-project/global/firewalls/gkegw1-7hi6-l7-peer-vpc-global",
        "projects/example-project/global/firewalls/gke-swarm-autopilot-4b0b02c7-all",
        "projects/example-project/global/firewalls/gke-swarm-autopilot-4b0b02c7-vms",
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-bnof",
      ] : anytrue([for p in local.deployer_type_scoped["roles/compute.securityAdmin"].prefixes : startswith(n, p)])
    ])
    error_message = "the firewall condition admits a rule or VM terraform/infra does not manage"
  }
}

run "the_cluster_condition_refuses_the_peer_cluster_under_either_name" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/container.admin"]
  }

  assert {
    condition = alltrue([
      for n in [
        "projects/example-project/locations/us-central1/clusters/swarm-autopilot",
        "projects/example-project/locations/us-central1/clusters/swarm-autopilot/nodePools/default-pool",
      ] : anytrue([for p in local.deployer_type_scoped["roles/container.admin"].prefixes : startswith(n, p)])
    ])
    error_message = "the swarm cluster is refused; terraform/infra could not refresh it"
  }

  # peer-cluster is zonal (us-central1-a), and GKE accepts both spellings.
  assert {
    condition = !anytrue([
      for n in [
        "projects/example-project/zones/us-central1-a/clusters/peer-cluster",
        "projects/example-project/locations/us-central1-a/clusters/peer-cluster",
      ] : anytrue([for p in local.deployer_type_scoped["roles/container.admin"].prefixes : startswith(n, p)])
    ])
    error_message = "the cluster condition admits peer-cluster, another team's live cluster"
  }
}

run "the_firestore_condition_admits_swarm_and_nothing_named_otherwise" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/datastore.owner"]
  }

  assert {
    condition = anytrue([
      for p in local.deployer_type_scoped["roles/datastore.owner"].prefixes :
      startswith("projects/example-project/databases/swarm", p)
    ])
    error_message = "the swarm database is refused; terraform/infra manages it and its indexes"
  }

  # No other database exists today. `(default)` is the one a second team would
  # most plausibly create, and bindings.tf warns about exactly that.
  assert {
    condition = !anytrue([
      for p in local.deployer_type_scoped["roles/datastore.owner"].prefixes :
      startswith("projects/example-project/databases/(default)", p)
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
    github_repository     = "example-org/example-repo"
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
      number = "123456789012"
    }
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["swarmSecretProvisioner"]
  }

  # Secret Manager names by project NUMBER; the project id never matches.
  assert {
    condition = alltrue([
      for p in local.deployer_type_scoped["swarmSecretProvisioner"].prefixes :
      startswith(p, "projects/123456789012/secrets/")
    ])
    error_message = "Secret Manager resource names use the project number; a prefix on the project id matches nothing"
  }

  assert {
    condition = alltrue([
      for n in [
        "projects/123456789012/secrets/swarm-account-eng--devops-main",
        "projects/123456789012/secrets/swarm-account-eng--devops-main-refresh",
        "projects/123456789012/secrets/swarm-account-eng--devops-team",
        "projects/123456789012/secrets/swarm-account-eng--devops-team-refresh",
        "projects/123456789012/secrets/swarm-account-eng--example-personal",
        "projects/123456789012/secrets/swarm-account-eng--example-personal-refresh",
        "projects/123456789012/secrets/swarm-account-eng--team",
        "projects/123456789012/secrets/swarm-account-eng--team-refresh",
        "projects/123456789012/secrets/swarm-tenant-eng-anthropic",
        "projects/123456789012/secrets/swarm-tenant-eng-anthropic-refresh",
        "projects/123456789012/secrets/swarm-tenant-eng-openai",
        "projects/123456789012/secrets/swarm-tenant-eng-openai-refresh",
        "projects/123456789012/secrets/swarm-tenant-u-pat-anthropic",
        "projects/123456789012/secrets/swarm-tenant-u-pat-anthropic-refresh",
      ] : anytrue([for p in local.deployer_type_scoped["swarmSecretProvisioner"].prefixes : startswith(n, p)])
    ])
    error_message = "a secret of the platform's is refused"
  }

  assert {
    condition = !anytrue([
      for n in [
        "projects/123456789012/secrets/agents-aipipeline-db-password",
        "projects/123456789012/secrets/agents-aipipeline-sentry-dsn",
        "projects/123456789012/secrets/agents-alertmanager-slack-webhook-url",
        "projects/123456789012/secrets/agents-api-service-db-password",
        "projects/123456789012/secrets/agents-api-service-keycloak-admin-client-secret",
        "projects/123456789012/secrets/agents-api-service-secrets-key",
        "projects/123456789012/secrets/agents-api-service-sentry-dsn",
        "projects/123456789012/secrets/agents-api-service-typesafe-api-key",
        "projects/123456789012/secrets/agents-argocd-github-repo-ssh-private-key",
        "projects/123456789012/secrets/agents-argocd-github-webhook-secret",
        "projects/123456789012/secrets/agents-argocd-slack-bot-token",
        "projects/123456789012/secrets/agents-backend-packages-pat",
        "projects/123456789012/secrets/agents-browser-engine-cap-token-secret",
        "projects/123456789012/secrets/agents-browser-engine-login-proxy",
        "projects/123456789012/secrets/agents-browser-engine-sentry-dsn",
        "projects/123456789012/secrets/agents-browser-engine-service-db-password",
        "projects/123456789012/secrets/agents-browser-engine-service-token",
        "projects/123456789012/secrets/agents-crawling-service-apify-token",
        "projects/123456789012/secrets/agents-crawling-service-bright-data-customer-id",
        "projects/123456789012/secrets/agents-crawling-service-bright-data-password",
        "projects/123456789012/secrets/agents-crawling-service-credential-encryption-key",
        "projects/123456789012/secrets/agents-crawling-service-db-password",
        "projects/123456789012/secrets/agents-crawling-service-sentry-dsn",
        "projects/123456789012/secrets/agents-discord-gateway-db-password",
        "projects/123456789012/secrets/agents-discord-gateway-sentry-dsn",
        "projects/123456789012/secrets/agents-discord-ingest-token",
        "projects/123456789012/secrets/agents-discord-publisher-db-password",
        "projects/123456789012/secrets/agents-discord-publisher-sentry-dsn",
        "projects/123456789012/secrets/agents-dispatcher-service-db-password",
        "projects/123456789012/secrets/agents-dispatcher-service-sentry-dsn",
        "projects/123456789012/secrets/agents-grafana-admin-password",
        "projects/123456789012/secrets/agents-homepage-argocd-token",
        "projects/123456789012/secrets/agents-instagram-app-secret",
        "projects/123456789012/secrets/agents-instagram-publisher-db-password",
        "projects/123456789012/secrets/agents-instagram-publisher-sentry-dsn",
        "projects/123456789012/secrets/agents-instagram-publisher-session-encryption-key",
        "projects/123456789012/secrets/agents-instagram-webhook-verify-token",
        "projects/123456789012/secrets/agents-internal-google-oauth-client-id",
        "projects/123456789012/secrets/agents-internal-google-oauth-client-secret",
        "projects/123456789012/secrets/agents-keycloak-admin-password",
        "projects/123456789012/secrets/agents-keycloak-db-password",
        "projects/123456789012/secrets/agents-keycloak-e2e-customer-admin-password",
        "projects/123456789012/secrets/agents-keycloak-e2e-customer-manager-password",
        "projects/123456789012/secrets/agents-keycloak-e2e-platform-admin-password",
        "projects/123456789012/secrets/agents-keycloak-e2e-example-admin-password",
        "projects/123456789012/secrets/agents-keycloak-smtp-password",
        "projects/123456789012/secrets/agents-keycloak-smtp-user",
        "projects/123456789012/secrets/agents-langsmith-api-key",
        "projects/123456789012/secrets/agents-metrics-exporter-db-password",
        "projects/123456789012/secrets/agents-migration-manager-db-password",
        "projects/123456789012/secrets/agents-sentry-dsn",
        "projects/123456789012/secrets/agents-support-slack-bot-token",
        "projects/123456789012/secrets/agents-tailscale-operator-client-id",
        "projects/123456789012/secrets/agents-tailscale-operator-client-secret",
        "projects/123456789012/secrets/agents-tournament-digest-bonoxs-api-key",
        "projects/123456789012/secrets/agents-user-engagement-crawler-db-password",
        "projects/123456789012/secrets/agents-user-engagement-crawler-sentry-dsn",
        "projects/123456789012/secrets/peer-app-db-dsn",
        "projects/123456789012/secrets/peer-app-fernet-key",
        "projects/123456789012/secrets/peer-app-example-ro-dsn",
        "projects/123456789012/secrets/peer-app-example-ro-dsn-psycopg",
        "projects/123456789012/secrets/peer-app-ghcr-dockerconfigjson",
        "projects/123456789012/secrets/peer-app-langsmith-shared-key",
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
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
  }

  # The attribute IAM provides for limiting role grants, on the allowlist.
  # Chunked (#275): every chunk's expression must be a modifiedGrantsByRole
  # hasOnly(), and every grantable role must appear in some chunk.
  assert {
    condition = (
      alltrue([
        for e in values(local.deployer_project_iam_admin_conditions) :
        startswith(e, "api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([")
      ]) &&
      alltrue([
        for r in local.deployer_grantable_project_roles :
        anytrue([for e in values(local.deployer_project_iam_admin_conditions) : strcontains(e, "\"${r}\"")])
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
        "projects/example-project/roles/swarmSecretProvisioner",
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

run "the_iam_admin_conditions_hasonly_lists_never_exceed_gcps_ten_element_limit" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
  }

  # #275: applying this condition against the real project failed with
  # `LintValidationUnits/ListLengthCheck Error: The list argument to hasOnly()
  # cannot have more than 10 elements`. The mock provider never lints, so
  # these read the rendered expression itself, off the plan -- not a helper
  # local -- so however the roles get chunked, this is what GCP would see.
  #
  # Every hasOnly([...]) in every instance of the projectIamAdmin resource's
  # condition, split into its role list.
  assert {
    condition = alltrue([
      for chunk in [
        for w in flatten([
          for k, m in google_project_iam_member.deployer_project_iam_admin :
          regexall("hasOnly\\(\\[[^\\]]*\\]\\)", m.condition[0].expression)
        ]) : [for q in regexall("\"[^\"]+\"", w) : trim(q, "\"")]
      ] : length(chunk) <= 10
    ])
    error_message = "a hasOnly() list in the projectIamAdmin condition holds more than 10 roles; GCP's linter (LintValidationUnits/ListLengthCheck) refuses that apply (#275)"
  }

  # The chunks, combined, must grant exactly deployer_grantable_project_roles:
  # nothing left out (every grantable role is in some chunk) and nothing added
  # (#73's parity rule -- no chunk holds a role terraform/infra does not
  # grant).
  assert {
    condition = toset(flatten([
      for w in flatten([
        for k, m in google_project_iam_member.deployer_project_iam_admin :
        regexall("hasOnly\\(\\[[^\\]]*\\]\\)", m.condition[0].expression)
      ]) : [for q in regexall("\"[^\"]+\"", w) : trim(q, "\"")]
    ])) == toset(local.deployer_grantable_project_roles)
    error_message = "the projectIamAdmin condition's hasOnly() lists, combined, no longer grant exactly deployer_grantable_project_roles"
  }

  # VERIFY-GKE-READ-2 (part of #1754; owner decision 2026-10-21): swarm-verify's
  # conditioned clusterViewer is granted from this root, which the owner
  # applies, so CI may never modify a clusterViewer binding. hasOnly() limits
  # which roles CI grants, never to whom or under what condition: on this list,
  # CI could grant itself clusterViewer unconditioned, which reaches the other
  # team's peer-cluster cluster. No chunk may name any container.* role.
  # MUTATION: put "roles/container.clusterViewer" back on
  # deployer_grantable_project_roles and this is red.
  assert {
    condition = length([
      for r in local.deployer_grantable_project_roles : r if startswith(r, "roles/container.")
      ]) == 0 && alltrue([
      for k, m in google_project_iam_member.deployer_project_iam_admin : !strcontains(m.condition[0].expression, "roles/container.")
    ])
    error_message = "CI's scoped projectIamAdmin may grant a container role. swarm-verify's conditioned clusterViewer is granted from terraform/bootstrap (verify_cluster_read.tf), which the owner applies; CI's allow-list never carries it"
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

# #227 box 26 (PR #277 security review): a condition is part of an IAM
# binding's identity, so a chunk whose role list changes is REPLACED. Chunked
# from the list as written, inserting or reordering one role shifted roles
# between chunks and replaced every chunk after it -- destroy first, then
# create, so for that window CI held no projectIamAdmin chunk at all and the
# apply in progress could 403 on its own next grant.
run "a_changed_role_list_replaces_iam_admin_chunks_new_grant_first" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
    deployer_scoped_roles = ["roles/resourcemanager.projectIamAdmin"]
  }

  # Chunked from the SORTED list, so reordering the source list renders the
  # same conditions and plans no change at all.
  assert {
    condition     = jsonencode(flatten(local.deployer_project_iam_admin_chunks)) == jsonencode(sort(local.deployer_grantable_project_roles))
    error_message = "projectIamAdmin chunks must be cut from the sorted grantable list, so reordering deployer_grantable_project_roles moves no role between chunks"
  }

  # Keyed by chunk position, "0", "1", ...: a role list change edits the
  # instance at the same key rather than retiring one key and minting another.
  assert {
    condition     = toset(keys(google_project_iam_member.deployer_project_iam_admin)) == toset([for i in range(length(local.deployer_project_iam_admin_chunks)) : tostring(i)])
    error_message = "projectIamAdmin chunk instances must be keyed by chunk position, the same key across a role list change"
  }

  # The mock provider cannot show lifecycle, so the resource block is read
  # from the file: whatever replaces a chunk creates the new grant before it
  # destroys the old one.
  assert {
    condition = strcontains(
      replace(regex("(?s)resource \"google_project_iam_member\" \"deployer_project_iam_admin\" \\{.*?\n\\}", file("../../terraform/bootstrap/deployer_conditions.tf")), "/[ \t]+/", " "),
      "lifecycle {\n create_before_destroy = true\n }",
    ) && length(google_project_iam_member.deployer_project_iam_admin) > 0
    error_message = "google_project_iam_member.deployer_project_iam_admin must carry lifecycle { create_before_destroy = true }: a replaced chunk is otherwise destroyed before its successor exists, and CI holds no projectIamAdmin chunk in between"
  }
}

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
    github_repository     = "example-org/example-repo"
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
      length(google_project_iam_member.deployer_project_iam_admin) == (contains(var.deployer_scoped_roles, "roles/resourcemanager.projectIamAdmin") ? length(local.deployer_project_iam_admin_conditions) : 0),
    ])
    error_message = "the scoping terraform.tfvars names moves a deployer grant other than its own roles' two halves; the owner's apply would do more than the tfvars line says"
  }

  # What the file names TODAY, and so what the targeted apply command in its
  # comment describes. PR #73's original plan was 1 to add (one unchunked
  # condition); after #275's chunking fix a fresh apply against a live policy
  # with nothing scoped is deployer_grantable_project_roles chunked at 10 --
  # today 2 to add, 0 to change, 0 to destroy. The live project's unconditioned
  # projectIamAdmin grant is restored by hand and not in bootstrap state, so
  # this targeted apply neither imports nor destroys it -- it is removed by
  # hand afterwards (docs/ci.md, "The deployer's refusal is proven once"),
  # which is why this plan never destroys anything. The chunk count changes only if
  # deployer_grantable_project_roles crosses a multiple of 10 (deployer_conditions.tf).
  assert {
    condition     = var.deployer_scoped_roles == toset(["roles/resourcemanager.projectIamAdmin"])
    error_message = "terraform/bootstrap/terraform.tfvars no longer scopes exactly roles/resourcemanager.projectIamAdmin, so the targeted apply command in its comment and the plan derived for it describe a different change. Update the command, that plan and this assertion together."
  }

  assert {
    condition     = length(local.deployer_project_iam_admin_conditions) == 2
    error_message = "deployer_grantable_project_roles has crossed a chunk boundary (a multiple of 10); update the plan-count comment above and in docs/ci.md to the new chunk count"
  }
}

run "a_role_with_no_conditioned_grant_cannot_be_switched" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif     = true
    github_repository     = "example-org/example-repo"
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
    github_repository = "example-org/example-repo"
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
# THE SPEECH MACHINE (owner's decided follow-up, 2026-11-05; zone fallback,
# 2026-11-09). The release deployer makes Voice it's speech machine's regional
# template, and stops and deletes the instance and its boot disk, through
# swarmSpeechDeployer, conditioned from the start to those names
# (deployer_conditions.tf, its last section). swarm-api creates the machine
# from the template through swarmSpeechOperator, granted by this root on the
# project with the same extract() form (speech_operator_grant.tf).
# ---------------------------------------------------------------------------

run "the_speech_machine_grant_is_conditioned_to_its_instance_and_disk" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "example-org/example-repo"
  }

  assert {
    condition = (
      length(google_project_iam_custom_role.deployer_speech_machine) == 1 &&
      length(google_project_iam_member.deployer_speech_machine) == 1 &&
      google_project_iam_member.deployer_speech_machine[0].role == "projects/example-project/roles/swarmSpeechDeployer"
    )
    # The member is the deployer's email, unknown at plan under the mock;
    # tests/unit/terraform/test_voice_speech.py reads it from the text.
    error_message = "the release deployer holds swarmSpeechDeployer through exactly one project binding"
  }

  # The verbs voice_speech.tf's template, and the instance and instance-level
  # iam_member an earlier release made, use, on an instance, a disk and a
  # template, and nothing else.
  assert {
    condition = toset(google_project_iam_custom_role.deployer_speech_machine[0].permissions) == toset([
      "compute.disks.create",
      "compute.disks.delete",
      "compute.disks.get",
      "compute.disks.setLabels",
      "compute.instanceTemplates.create",
      "compute.instanceTemplates.delete",
      "compute.instanceTemplates.get",
      "compute.instanceTemplates.useReadOnly",
      "compute.instances.create",
      "compute.instances.delete",
      "compute.instances.get",
      "compute.instances.getIamPolicy",
      "compute.instances.setDeletionProtection",
      "compute.instances.setIamPolicy",
      "compute.instances.setLabels",
      "compute.instances.setMachineType",
      "compute.instances.setMetadata",
      "compute.instances.setScheduling",
      "compute.instances.setServiceAccount",
      "compute.instances.setTags",
      "compute.instances.start",
      "compute.instances.stop",
      "compute.instances.update",
      "compute.instances.updateShieldedVmConfig",
    ])
    error_message = "swarmSpeechDeployer carries a permission the speech machine's Terraform does not use, or lacks one it does"
  }

  assert {
    condition     = length(google_project_iam_custom_role.deployer_speech_machine[0].description) <= 300
    error_message = "IAM refuses a custom role description over 300 characters, at apply (lesson 10)"
  }

  # An unconditioned binding of the role is refused: it would let CI create,
  # delete and stop every VM and disk in the project, their GKE nodes included.
  assert {
    condition = (
      length(google_project_iam_member.deployer_speech_machine[0].condition) == 1 &&
      google_project_iam_member.deployer_speech_machine[0].condition[0].expression == local.deployer_speech_machine_condition &&
      local.deployer_speech_machine_condition != ""
    )
    error_message = "the release deployer's swarmSpeechDeployer binding carries no condition"
  }

  # The expression, rendered, exactly: one extract() per kind and the
  # template's startsWith(), nothing else.
  assert {
    condition     = local.deployer_speech_machine_condition == "resource.name.extract(\"projects/example-project/zones/us-central1-{zone}/instances/swarm-speech-\") != \"\" || resource.name.extract(\"projects/example-project/zones/us-central1-{zone}/disks/swarm-speech-\") != \"\" || resource.name.startsWith(\"projects/example-project/regions/us-central1/instanceTemplates/swarm-speech-\")"
    error_message = "the speech machine's condition is not the two extract() tests on its instance and disk names and the startsWith() on its template's"
  }

  assert {
    condition = alltrue([
      for n in ["projects/example-project/regions/us-central1/instanceTemplates/swarm-speech-20261109000000000000000001"] :
      startswith(n, local.deployer_speech_template_prefix)
      ]) && !anytrue([
      for n in [
        "projects/example-project/regions/us-central1/instanceTemplates/gke-peer-cluster-nap-e2-standard-2",
        "projects/example-project/regions/us-central1/instanceTemplates/swarm-speechx",
        "projects/example-project/regions/europe-west1/instanceTemplates/swarm-speech-1",
        "projects/example-project/global/instanceTemplates/swarm-speech-1",
        "projects/example-shared-project/regions/us-central1/instanceTemplates/swarm-speech-1",
      ] : startswith(n, local.deployer_speech_template_prefix)
    ])
    error_message = "the speech machine's condition refuses its template, or admits a template that is not its"
  }

  assert {
    condition = alltrue([
      for e in [local.deployer_speech_machine_condition] :
      length(regexall("&&", e)) + length(regexall("\\|\\|", e)) + length(regexall("![^=]", e)) <= 12 &&
      length(regexall("matches\\(", e)) == 0 && length(regexall("hasOnly\\(", e)) == 0
    ])
    error_message = "the speech machine's condition is over IAM's 12 logical operators or uses a function GCP's linter refuses"
  }

  # Replayed the way extract("A{zone}B") != "" reads: the name starts with A
  # (every Compute name starts with projects/, so the first A is at 0), B
  # follows, and the zone letter between them is not empty.
  assert {
    condition = alltrue([
      for n in [
        "projects/example-project/zones/us-central1-a/instances/swarm-speech-a",
        "projects/example-project/zones/us-central1-a/disks/swarm-speech-a",
        "projects/example-project/zones/us-central1-c/instances/swarm-speech-c",
        "projects/example-project/zones/us-central1-c/disks/swarm-speech-c",
        ] : anytrue([
          for p in local.deployer_speech_machine_paths :
          startswith(n, p.zone_prefix) && strcontains(trimprefix(n, p.zone_prefix), p.name_prefix) && split(p.name_prefix, trimprefix(n, p.zone_prefix))[0] != ""
      ])
    ])
    error_message = "the speech machine's instance or boot disk is refused"
  }

  assert {
    condition = !anytrue([
      for n in [
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-bnof",
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-h2ug",
        "projects/example-project/zones/us-central1-a/instances/gke-peer-cluster-nap-e2-standard-2--b59db5d4-xstq",
        "projects/example-project/zones/us-central1-a/disks/gke-peer-cluster-nap-e2-standard-2--b59db5d4-bnof",
        "projects/example-project/regions/us-central1/addresses/swarm-speech",
        "projects/example-project/zones/us-central1-a/instances/swarm-speechx",
        "projects/example-project/zones/us-central1-a/instances/swarm-ui-a",
        "projects/example-project/zones/us-central1-a/instanceGroups/swarm-speech-a",
        "projects/example-project/global/images/swarm-speech-a",
        "projects/example-project/zones/europe-west1-b/instances/swarm-speech-b",
        "projects/example-shared-project/zones/us-central1-a/instances/swarm-speech-a",
        ] : anytrue([
          for p in local.deployer_speech_machine_paths :
          startswith(n, p.zone_prefix) && strcontains(trimprefix(n, p.zone_prefix), p.name_prefix) && split(p.name_prefix, trimprefix(n, p.zone_prefix))[0] != ""
      ])
    ])
    error_message = "the speech machine's condition admits a VM, disk or resource that is not the speech machine"
  }

  # No project-wide compute role that holds the same verbs rides beside it.
  assert {
    condition = length(setintersection(keys(google_project_iam_member.deployer_roles), [
      "roles/compute.admin",
      "roles/compute.instanceAdmin",
      "roles/compute.instanceAdmin.v1",
    ])) == 0
    error_message = "a project-wide compute instance role on the deployer makes the speech machine's condition meaningless"
  }
}

# swarm-api creates, starts, moves and deletes the speech machine in the zones
# of voice_speech.zones (owner, 2026-11-09). Its swarmSpeechOperator is on the
# project, conditioned to the machine, and granted by this root, never by CI.
run "swarm_api_operates_the_speech_machine_only_and_ci_cannot_grant_it" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "example-org/example-repo"
  }

  assert {
    condition = toset(google_project_iam_custom_role.speech_operator.permissions) == toset([
      "compute.addresses.useInternal",
      "compute.disks.create",
      "compute.disks.delete",
      "compute.disks.setLabels",
      "compute.instanceTemplates.get",
      "compute.instanceTemplates.useReadOnly",
      "compute.instances.create",
      "compute.instances.delete",
      "compute.instances.get",
      "compute.instances.getSerialPortOutput",
      "compute.instances.setLabels",
      "compute.instances.setMachineType",
      "compute.instances.setMetadata",
      "compute.instances.setServiceAccount",
      "compute.instances.setTags",
      "compute.instances.start",
      "compute.instances.stop",
      "compute.subnetworks.use",
    ])
    error_message = "swarmSpeechOperator carries a permission swarm_api/voice/speech_machine.py does not use, or lacks one a create from the template checks"
  }

  assert {
    condition     = length(google_project_iam_custom_role.speech_operator.description) <= 300
    error_message = "IAM refuses a custom role description over 300 characters, at apply (lesson 10)"
  }

  assert {
    condition = (
      google_project_iam_member.api_speech_operator.member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com" &&
      length(google_project_iam_member.api_speech_operator.condition) == 1 &&
      google_project_iam_member.api_speech_operator.condition[0].expression == local.speech_operator_condition
    )
    error_message = "swarm-api's swarmSpeechOperator binding is not swarm-api's, or carries no condition"
  }

  # The deployer's extract() per kind, then the template, the address and the
  # subnet a create names; nothing else.
  assert {
    condition     = local.speech_operator_condition == "resource.name.extract(\"projects/example-project/zones/us-central1-{zone}/instances/swarm-speech-\") != \"\" || resource.name.extract(\"projects/example-project/zones/us-central1-{zone}/disks/swarm-speech-\") != \"\" || resource.name.startsWith(\"projects/example-project/regions/us-central1/instanceTemplates/swarm-speech-\") || resource.name == \"projects/example-project/regions/us-central1/addresses/swarm-speech\" || resource.name == \"projects/example-project/regions/us-central1/subnetworks/swarm-subnet-us-central1\""
    error_message = "swarm-api's speech condition is not the deployer's extract() form plus its template, address and subnet"
  }

  assert {
    condition = (
      length(regexall("&&", local.speech_operator_condition)) + length(regexall("\\|\\|", local.speech_operator_condition)) + length(regexall("![^=]", local.speech_operator_condition)) <= 12 &&
      length(regexall("matches\\(", local.speech_operator_condition)) == 0 &&
      length(regexall("hasOnly\\(", local.speech_operator_condition)) == 0
    )
    error_message = "swarm-api's speech condition is over IAM's 12 logical operators or uses a function GCP's linter refuses"
  }

  # hasOnly() limits which roles CI grants, never to whom: on the list, CI
  # could grant swarmSpeechOperator to anyone unconditioned.
  assert {
    condition     = !anytrue([for r in local.deployer_grantable_project_roles : strcontains(r, "swarmSpeechOperator")])
    error_message = "swarmSpeechOperator is on the release deployer's grantable list: CI could grant it unconditioned"
  }
}

run "an_unconditioned_instance_admin_role_cannot_be_given_to_ci" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "example-org/example-repo"
    deployer_roles = [
      "roles/artifactregistry.admin",
      "roles/compute.instanceAdmin.v1",
    ]
  }

  # Project-wide it reaches their GKE nodes; the speech machine's verbs come
  # only through swarmSpeechDeployer's condition.
  expect_failures = [var.deployer_roles]
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
    artifact_bucket  = "swarm-artifacts-example-project"
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
    artifact_bucket        = "swarm-artifacts-example-project"
    workload_identity_pool = "example-project.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
        providers = ["anthropic", "git"]
      }
    }
    dispatcher_members = {
      scheduler  = "serviceAccount:swarm-scheduler@example-project.iam.gserviceaccount.com"
      reconciler = "serviceAccount:swarm-reconciler@example-project.iam.gserviceaccount.com"
    }
  }

  # The worker grant names its custom role by a plain string from
  # terraform/modules/custom_role_ids since #79, known at plan, so no override
  # of a computed role id is needed here any more.

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
      "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  assert {
    condition = alltrue([
      contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, google_project_iam_member.verify_reads_firestore.role),
      contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, google_project_iam_member.verify_reads_run.role),
    ])
    error_message = "terraform/infra/verify.tf grants a project-level role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }

  # terraform/infra/plugins.tf (managed plugins, lane 3, part of #2688):
  # swarm-api adds versions to the -plg- plugin secrets through a project
  # grant conditioned on the secret's name, planned with no switch.
  assert {
    condition     = contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, google_project_iam_member.plugins_api_secret_versions.role)
    error_message = "terraform/infra/plugins.tf grants a project-level role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }
}

# terraform/infra/github_hooks.tf (lane GS-HOOKS, part of #1384) grants the
# rollup sweeper account swarmGithubHooksDedup for the push receiver's delivery
# dedup (lane HOOKS-ROLE: it was roles/datastore.user), only with
# enable_github_hooks on -- so it is planned on here, and the grant is held to
# exist, not passed over an empty count. The role is grantable only once
# terraform/bootstrap defines it (enable_github_hooks_dedup_role, the owner's
# apply that must come first), so the list it is read against is that run's.
run "the_github_hooks_dedup_role_is_grantable_once_bootstrap_defines_it" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.this
    override_during = plan
    values = {
      number = "123456789012"
    }
  }

  variables {
    enable_github_wif              = true
    github_repository              = "example-org/example-repo"
    deployer_scoped_roles          = ["roles/resourcemanager.projectIamAdmin"]
    enable_github_hooks_dedup_role = true
  }

  assert {
    condition = (
      contains(local.deployer_grantable_project_roles, "projects/example-project/roles/swarmGithubHooksDedup")
      && !contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, "projects/example-project/roles/swarmGithubHooksDedup")
    )
    error_message = "swarmGithubHooksDedup is grantable exactly when terraform/bootstrap defines it (enable_github_hooks_dedup_role), and not before"
  }
}

run "the_github_hooks_firestore_role_is_grantable" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"
    tenants     = {}

    # GS-HOOKS: the receiver and its grants exist only with this on.
    enable_github_hooks = true

    # terraform/infra refuses to plan without a digest for every image it
    # deploys (main, #24: the precondition on google_cloud_run_v2_job.verify).
    # The same fixture infra_guards.tftest.hcl uses.
    image_refs = {
      "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "swarm-github-hooks"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-github-hooks@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  assert {
    condition = length(google_project_iam_member.github_hooks_firestore) == 1 && alltrue([
      for k, g in google_project_iam_member.github_hooks_firestore : contains(run.the_github_hooks_dedup_role_is_grantable_once_bootstrap_defines_it.deployer_grantable_project_roles, g.role)
    ])
    error_message = "terraform/infra/github_hooks.tf grants a project-level role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }
}

# terraform/infra/voice_speech.tf (Voice it's speech machine, part of #2688)
# grants swarm-speech its own log and metric writes on the project, only with
# voice_speech.enabled and voice_it "on" -- so both are planned on here, and
# the grants are held to exist, not passed over an empty count.
run "the_speech_machines_project_roles_are_grantable" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # The swarm-voice and swarm-speech accounts terraform/bootstrap made, as a
  # read returns them (voice.tftest.hcl's fixture).
  override_data {
    target          = data.google_service_account.voice
    override_during = plan
    values = {
      email = "swarm-voice@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/swarm-voice@example-project.iam.gserviceaccount.com"
    }
  }

  override_data {
    target          = data.google_service_account.speech
    override_during = plan
    values = {
      email = "swarm-speech@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/swarm-speech@example-project.iam.gserviceaccount.com"
    }
  }

  variables {
    environment = "dev"
    tenants     = {}

    voice_it = "on"
    voice_speech = {
      enabled = true
      image   = "projects/deeplearning-platform-release/global/images/common-cu124-v20250101-debian-11-py310"
    }

    # terraform/infra refuses to plan without a digest for every image it
    # deploys (main, #24: the precondition on google_cloud_run_v2_job.verify).
    # The same fixture infra_guards.tftest.hcl uses, with swarm-voice's
    # digest, which voice_it "on" needs (voice.tf's voice_it_has_an_image).
    image_refs = {
      "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "swarm-voice"              = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-voice@sha256:8888888888888888888888888888888888888888888888888888888888888888"
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  assert {
    condition = length(google_project_iam_member.speech_log_writer) == 1 && alltrue([
      for k, g in google_project_iam_member.speech_log_writer : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, g.role)
    ])
    error_message = "terraform/infra/voice_speech.tf grants swarm-speech a log-write role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }

  assert {
    condition = length(google_project_iam_member.speech_metric_writer) == 1 && alltrue([
      for k, g in google_project_iam_member.speech_metric_writer : contains(run.every_switched_role_trades_its_project_wide_grant_for_a_conditioned_one.deployer_grantable_project_roles, g.role)
    ])
    error_message = "terraform/infra/voice_speech.tf grants swarm-speech a metric-write role the scoped projectIamAdmin may not grant; add it to deployer_grantable_project_roles"
  }
}

# The runs above read grants by name, and a grant nobody named is checked
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
    deployer_storage_buckets_exact = ["example-project_cloudbuild"]
    enable_github_wif              = true
    github_repository              = "example-org/example-repo"
  }

  # The source tarball is an OBJECT in the Cloud Build bucket. An equality test
  # on the bucket admits the bucket and refuses every object in it.
  assert {
    condition     = strcontains(google_project_iam_member.deployer_storage[0].condition[0].expression, "resource.name.startsWith(\"projects/_/buckets/example-project_cloudbuild/objects/\")")
    error_message = "the storage condition admits the Cloud Build bucket but not the objects in it; gcloud builds submit could not upload its source"
  }

  # Replayed against the eight live buckets (2026-09-24), mirroring the three
  # clause shapes the expression is rendered from.
  assert {
    condition = alltrue([
      for n in [
        "projects/_/buckets/swarm-access-logs-example-project",
        "projects/_/buckets/swarm-artifacts-example-project/objects/tenants/eng/x",
        "projects/_/buckets/swarm-tfstate-logs-example-project",
        "projects/_/buckets/swarm-tfstate-example-project/objects/infra/dev/default.tfstate",
        "projects/_/buckets/example-project_cloudbuild",
        "projects/_/buckets/example-project_cloudbuild/objects/source/1727000000.0-0123456789abcdef.tgz",
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
        "projects/_/buckets/peer-media-bucket",
        "projects/_/buckets/peer-bucket/objects/a",
        "projects/_/buckets/peer-state-bucket",
        "projects/_/buckets/peer-state-bucket/objects/default.tfstate",
        "projects/_/buckets/example-project_cloudbuild-lookalike",
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

# OWNER DECISION 2026-09-29, from the security review of contract request 30
# (#314): the deployer holds no roles/iam.workloadIdentityPoolAdmin.
# terraform/infra manages no workload identity pool or provider -- the only
# pool it names is GKE's "<project>.svc.id.goog", a string inside the member of
# a service-account binding (modules/tenancy) -- so the role bought CI nothing
# and let it add a provider to any pool in the project: the other team's
# github-actions pool, or swarm-github, where a provider of its own choosing
# mints tokens for swarm-ci-fix or swarm-tf-deployer outside the WIF ref pin.
run "the_deployer_holds_no_workload_identity_pool_admin" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "example-org/example-repo"
  }

  assert {
    condition     = !contains(var.deployer_roles, "roles/iam.workloadIdentityPoolAdmin")
    error_message = "roles/iam.workloadIdentityPoolAdmin is still in deployer_roles' default"
  }

  assert {
    condition     = !contains(keys(google_project_iam_member.deployer_roles), "roles/iam.workloadIdentityPoolAdmin")
    error_message = "the deployer is still granted roles/iam.workloadIdentityPoolAdmin project-wide"
  }

  # The second key: a role off the reviewed list is refused at plan.
  assert {
    condition     = !contains(local.deployer_roles_reviewed, "roles/iam.workloadIdentityPoolAdmin")
    error_message = "roles/iam.workloadIdentityPoolAdmin is still on the list of roles deployer_roles may name"
  }

  # The control: the grant loop still runs and still grants the role next to
  # it on the list, so this run is not green because WIF or the list went away.
  assert {
    condition     = contains(keys(google_project_iam_member.deployer_roles), "roles/run.admin") && length(google_project_iam_member.deployer_roles) > 10
    error_message = "the deployer's other grants went away with workloadIdentityPoolAdmin"
  }
}

# Putting it back by hand is refused at plan, by a validation that names it so
# the refusal explains itself.
run "workload_identity_pool_admin_cannot_be_put_back" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_github_wif = true
    github_repository = "example-org/example-repo"
    deployer_roles    = ["roles/run.admin", "roles/iam.workloadIdentityPoolAdmin"]
  }

  expect_failures = [var.deployer_roles]
}

# The managed plugins' bucket is named <project>-swarm-plugins by default, which
# the `swarm-` prefix does not admit; without an exact clause every release plan
# stopped on a 403 reading it. Placeholder project only.
run "the_storage_condition_admits_the_plugins_bucket_and_its_objects" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    project_id        = "example-project"
    enable_github_wif = true
    github_repository = "example-org/example-repo"
  }

  assert {
    condition     = strcontains(google_project_iam_member.deployer_storage[0].condition[0].expression, "resource.name == \"projects/_/buckets/example-project-swarm-plugins\"")
    error_message = "the storage condition does not admit the plugins bucket; every release plan stops on a 403 reading it"
  }

  assert {
    condition     = strcontains(google_project_iam_member.deployer_storage[0].condition[0].expression, "resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/\")")
    error_message = "the storage condition admits the plugins bucket but not its objects"
  }

  assert {
    condition     = !strcontains(google_project_iam_member.deployer_storage[0].condition[0].expression, "buckets/example-project-swarm-plugins-")
    error_message = "the plugins bucket must be admitted by exact name, not as a prefix another bucket could share"
  }
}
