# The GitHub push receiver, the platform side (docs/design/graph-service.md
# §6.2; lane GS-HOOKS, part of #1384): terraform/infra/github_hooks.tf.
#
# What is held here:
#
#   * OFF BY DEFAULT nothing exists: no service, no public invoker, no secret,
#     no accessor, no Firestore role, no TTL policy. Merging the lane changes
#     nothing in an environment until the owner sets enable_github_hooks;
#   * on, the service swarm-github-hooks runs as the ROLLUP SWEEPER account
#     (owner decision 2026-10-16), ingress all, with requests == limits
#     (invariant 7), and allUsers holds run.invoker on THIS service and on no
#     other resource in the plan;
#   * swarm-github-app-webhook-secret exists with NO VERSION, read by the
#     sweeper account alone;
#   * the sweeper gets the bootstrap custom role swarmGithubHooksDedup
#     (datastore.entities.create alone; terraform/bootstrap/github_hooks_role.tf,
#     lane HOOKS-ROLE, owner decision 2026-10-16), never roles/datastore.user,
#     and github_deliveries.expires_at a TTL policy;
#   * on without a digest, the IAM rows exist, the service does not, and the
#     check says why;
#   * every labelable resource carries managed-by=swarm-terraform.
#
# The infra root's setup is graph_service_iam.tftest.hcl's, with a
# swarm-github-hooks digest added to image_refs.

mock_provider "google" {}

override_data {
  target = data.google_kms_crypto_key_versions.step_spec
  values = {
    versions = [
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 1
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
    ]
  }
}

override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  # Read by the dev run's assertions only (live.tftest.hcl does the same).
  tf_root = "../../terraform/infra"

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
  }

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-egress"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "swarm-typesafe-fwd"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-typesafe-fwd@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    "swarm-graph"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-graph@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    "swarm-github-hooks"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-github-hooks@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_nothing_exists" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_service.github_hooks) == 0
      && length(google_secret_manager_secret.github_webhook_secret) == 0
      && length(google_secret_manager_secret_iam_binding.github_webhook_secret_accessor) == 0
      && length(google_project_iam_member.github_hooks_firestore) == 0
      && length(google_firestore_field.github_deliveries_ttl) == 0
    )
    error_message = "with enable_github_hooks off (the default) no receiver, public invoker, secret, accessor, Firestore role or TTL policy exists, even with a digest in image_refs"
  }

  assert {
    condition = (
      output.github_hooks.created == false
      && output.github_hooks.serving == false
      && output.github_hooks.webhook_secret == ""
      && output.github_hooks.webhook_url == ""
    )
    error_message = "with the receiver off there is no URL to give GitHub and no secret to store into"
  }

  # Only dev turns it on, because the owner turned it on there (2026-10-16, PR 1432):
  # any other environment's tfvars naming it is a public receiver nobody decided on.
  assert {
    condition = alltrue([
      for f in fileset("${var.tf_root}/../environments", "**/*.tfvars") :
      f == "dev/dev.tfvars" || !strcontains(file("${var.tf_root}/../environments/${f}"), "enable_github_hooks")
    ])
    error_message = "only dev/dev.tfvars sets enable_github_hooks (the owner turned it on there, 2026-10-16); no other environment may"
  }
}

run "on_the_receiver_is_public_runs_as_the_sweeper_and_reads_its_secret_alone" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_github_hooks = true
  }

  assert {
    condition = (
      length(google_cloud_run_v2_service.github_hooks) == 1
      && google_cloud_run_v2_service.github_hooks[0].name == "swarm-github-hooks"
      && google_cloud_run_v2_service.github_hooks[0].ingress == "INGRESS_TRAFFIC_ALL"
      && google_cloud_run_v2_service.github_hooks[0].template[0].containers[0].image == "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-github-hooks@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    )
    error_message = "the receiver is the Cloud Run service swarm-github-hooks, ingress all, on the promoted digest"
  }

  # The owner's 2026-10-16 decision: the rollup sweeper's account, not a new one.
  assert {
    condition = (
      google_cloud_run_v2_service.github_hooks[0].template[0].service_account == module.scheduler.rollup_sweeper_email
      && startswith(module.scheduler.rollup_sweeper_email, "swarm-rollup-sweeper@")
      && output.github_hooks.service_account == module.scheduler.rollup_sweeper_email
    )
    error_message = "the receiver runs as the rollup sweeper account (owner decision 2026-10-16), and no new identity"
  }

  # Public on THIS service and nowhere else. Since DAYONE-TF-2 (Q3) the door
  # is the service's own invoker check switched off, not an allUsers
  # run.invoker binding: github_hooks.tf grants allUsers nothing.
  assert {
    condition = (
      google_cloud_run_v2_service.github_hooks[0].invoker_iam_disabled == true
      && google_cloud_run_v2_service_iam_member.rollup_sweeper_invokes_api.member != "allUsers"
      && length(regexall("\"allUsers\"", file("${var.tf_root}/github_hooks.tf"))) == 0
    )
    error_message = "swarm-github-hooks is public by invoker_iam_disabled on that service alone, and github_hooks.tf binds allUsers to nothing"
  }

  # requests == limits (invariant 7): no idle throttling, no start-up boost.
  assert {
    condition = (
      google_cloud_run_v2_service.github_hooks[0].template[0].containers[0].resources[0].cpu_idle == false
      && google_cloud_run_v2_service.github_hooks[0].template[0].containers[0].resources[0].startup_cpu_boost == false
      && google_cloud_run_v2_service.github_hooks[0].template[0].scaling[0].min_instance_count == 0
    )
    error_message = "the receiver's CPU is its limit and nothing more (invariant 7), and an idle receiver runs nothing (invariant 1)"
  }

  assert {
    condition = (
      # swarm-api's URL is unknown until apply; its name is held here and
      # its value by tests/unit/hooks/test_hooks_image.py's source check.
      toset([for e in google_cloud_run_v2_service.github_hooks[0].template[0].containers[0].env : e.name]) == toset(["FIRESTORE_DATABASE", "SWARM_API_URL", "SWARM_GITHUB_HOOKS_SECRET"])
      && one([for e in google_cloud_run_v2_service.github_hooks[0].template[0].containers[0].env : e.value if e.name == "SWARM_GITHUB_HOOKS_SECRET"]) == "projects/saga-agents-staging/secrets/swarm-github-app-webhook-secret/versions/latest"
      && one([for e in google_cloud_run_v2_service.github_hooks[0].template[0].containers[0].env : e.value if e.name == "FIRESTORE_DATABASE"]) == "swarm"
    )
    error_message = "the receiver reads the webhook secret by version name at run time (never a value), and forwards to swarm-api's URL"
  }

  assert {
    condition = (
      google_secret_manager_secret.github_webhook_secret[0].secret_id == "swarm-github-app-webhook-secret"
      && google_secret_manager_secret_iam_binding.github_webhook_secret_accessor[0].role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.github_webhook_secret_accessor[0].members == toset(["serviceAccount:${module.scheduler.rollup_sweeper_email}"])
    )
    error_message = "swarm-github-app-webhook-secret is read by the sweeper account the receiver runs as, and by nobody else"
  }

  assert {
    condition = (
      google_project_iam_member.github_hooks_firestore[0].role == "projects/saga-agents-staging/roles/swarmGithubHooksDedup"
      && google_project_iam_member.github_hooks_firestore[0].member == "serviceAccount:${module.scheduler.rollup_sweeper_email}"
      && google_firestore_field.github_deliveries_ttl[0].collection == "github_deliveries"
      && google_firestore_field.github_deliveries_ttl[0].field == "expires_at"
      && length(google_firestore_field.github_deliveries_ttl[0].ttl_config) == 1
    )
    error_message = "the sweeper can create github_deliveries documents through swarmGithubHooksDedup, the custom role terraform/bootstrap defines, and Firestore expires them on expires_at"
  }

  # The owner's 2026-10-16 decision (HOOKS-ROLE): a public-facing service's
  # identity holds create on Firestore and nothing more. roles/datastore.user
  # is read-write on every collection of the platform's database, and no IAM
  # condition narrows Firestore's data plane, so no grant in the plan may hand
  # the sweeper account any predefined datastore role.
  assert {
    condition = alltrue([
      for k, g in google_project_iam_member.github_hooks_firestore :
      !startswith(g.role, "roles/")
    ])
    error_message = "github_hooks_firestore grants a predefined role; the receiver's dedup holds the bootstrap custom role swarmGithubHooksDedup, never roles/datastore.user"
  }

  assert {
    condition = (
      google_cloud_run_v2_service.github_hooks[0].labels["managed-by"] == "swarm-terraform"
      && google_cloud_run_v2_service.github_hooks[0].template[0].labels["managed-by"] == "swarm-terraform"
      && google_secret_manager_secret.github_webhook_secret[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "every labelable resource carries managed-by=swarm-terraform"
  }

  # No version, ever, from terraform: the owner types the value.
  assert {
    condition     = length(regexall("google_secret_manager_secret_version|secret_data|secret_key_ref", file("${var.tf_root}/github_hooks.tf"))) == 0
    error_message = "github_hooks.tf declares no secret version, no secret data and no Cloud Run secret reference (a reference to a versionless secret would stop the revision)"
  }

  assert {
    condition     = output.github_hooks.created == true && output.github_hooks.serving == true && output.github_hooks.webhook_secret == "swarm-github-app-webhook-secret"
    error_message = "the output names the secret the owner stores into"
  }
}

run "on_without_a_digest_the_iam_exists_and_the_check_says_why" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_github_hooks = true
    image_refs = {
      "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
      "swarm-egress"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
      "swarm-typesafe-fwd"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-typesafe-fwd@sha256:9999999999999999999999999999999999999999999999999999999999999999"
      "swarm-graph"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-graph@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  expect_failures = [check.github_hooks_has_an_image]

  assert {
    condition = (
      length(google_cloud_run_v2_service.github_hooks) == 0
      && length(google_secret_manager_secret.github_webhook_secret) == 1
      && length(google_secret_manager_secret_iam_binding.github_webhook_secret_accessor) == 1
    )
    error_message = "without a digest no service and no public invoker exist; the secret and its reader do"
  }
}
