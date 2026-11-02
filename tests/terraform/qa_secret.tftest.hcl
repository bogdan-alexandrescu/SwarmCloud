# The per-tenant QA target credential secret and its two IAM rows
# (docs/design/visual-qa-any-app.md §2.1 and §7; lane VQ-3 QA-SECRET of
# docs/design/agent-autonomy.md §6; owner answer VQ-Q2 (a); part of #1933):
# terraform/infra/qa_targets.tf.
#
# What is held here:
#
#   * one `swarm-tenant-<t>-qa` per TENANT -- not per provider, so a tenant
#     with no provider key gets one too -- with NO VERSION, labelled
#     managed-by=swarm-terraform and with the tenant label
#     scripts/offboard-tenant.sh finds a tenant's secrets by;
#   * V-1: swarm-api's account, and nobody else, is secretVersionAdder on each
#     one, by an authoritative binding on that secret alone;
#   * V-2: swarm-api's account, and nobody else, is secretAccessor on each one,
#     the same way;
#   * no tenant's worker account is bound on any of them, its own tenant's
#     included: the worker asks swarm-api for its target's sign-in (§2.2) and
#     never reads the secret;
#   * neither role is granted to swarm-api project-wide, which would reach
#     every tenant's provider keys;
#   * the secret is not a `tenant-credential`, so the quota broker's refresh
#     sweep and create-secrets.sh's provider listing never treat it as a
#     provider key.
#
# The infra root's setup is workspace_grant_key.tftest.hcl's, with a second
# tenant and the account emails pinned so the members can be compared.

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

  # Two tenants, one holding no provider key at all: the QA secret is per
  # TENANT, not per provider, so `smoke` gets one even though
  # modules/secret_manager makes it nothing.
  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
    smoke = {
      kind      = "group"
      principal = "smoke@example.com"
      providers = []
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
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "one_empty_qa_secret_per_tenant_read_and_written_by_swarm_api_alone" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_resource {
    target          = module.iam.google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = module.tenancy.google_service_account.worker["eng"]
    override_during = plan
    values = {
      email = "swarm-w-eng@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-w-eng@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = module.tenancy.google_service_account.worker["smoke"]
    override_during = plan
    values = {
      email = "swarm-w-smoke@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-w-smoke@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # MUTATION: key the secrets by provider (modules/secret_manager's
  # secret_inputs, which skips a tenant with none), or drop a tenant, and the
  # set differs.
  assert {
    condition = (
      toset([for s in google_secret_manager_secret.qa_target : s.secret_id])
      == toset(["swarm-tenant-eng-qa", "swarm-tenant-smoke-qa"])
    )
    error_message = "every tenant, a tenant with no provider key included, has exactly one QA secret, swarm-tenant-<t>-qa"
  }

  # MUTATION: replace local.labels with a literal map that omits managed-by
  # and scripts/destroy.sh can never delete the secret.
  assert {
    condition = alltrue([
      for t, s in google_secret_manager_secret.qa_target :
      s.labels["managed-by"] == "swarm-terraform" && s.labels["tenant"] == t
    ])
    error_message = "every QA secret is labelled managed-by=swarm-terraform and tenant=<its tenant> (scripts/offboard-tenant.sh finds a tenant's secrets by labels.tenant)"
  }

  # MUTATION: label it component=tenant-credential and the broker's refresh
  # sweep and create-secrets.sh --list take it for a provider key.
  assert {
    condition = alltrue([
      for s in google_secret_manager_secret.qa_target :
      s.labels["component"] == "qa-target-credential" && !contains(keys(s.labels), "provider")
    ])
    error_message = "a QA secret is component=qa-target-credential and names no provider: it is not a provider key"
  }

  # MUTATION: replicate automatically and the credential is copied into
  # regions nothing here runs in.
  assert {
    condition = alltrue([
      for s in google_secret_manager_secret.qa_target :
      s.replication[0].user_managed[0].replicas[0].location == "us-central1"
      && length(s.replication[0].user_managed[0].replicas) == 1
    ])
    error_message = "every QA secret is pinned to the workload region"
  }

  # Empty, always: swarm-api adds its versions from the console form
  # (visual-qa-any-app.md §2.1); a version here would put a credential in
  # state. MUTATION: add a google_secret_manager_secret_version (or any
  # secret_data) to qa_targets.tf.
  assert {
    condition     = length(regexall("google_secret_manager_secret_version|secret_data", file("${var.tf_root}/qa_targets.tf"))) == 0
    error_message = "terraform/infra/qa_targets.tf declares no secret version and no secret data: swarm-api adds every version from the console's credential form"
  }

  # V-1. MUTATION: make the role secretVersionManager (or admin) and
  # swarm-api's grant reaches beyond adding a version.
  assert {
    condition = alltrue([
      for t, b in google_secret_manager_secret_iam_binding.qa_target_adder :
      b.role == "roles/secretmanager.secretVersionAdder" && b.secret_id == "swarm-tenant-${t}-qa"
    ])
    error_message = "V-1 is roles/secretmanager.secretVersionAdder on each tenant's own swarm-tenant-<t>-qa"
  }

  # MUTATION: add a tenant's worker, an admin or var.secret_admin_members to
  # the members and a second identity can replace a tenant's credential.
  assert {
    condition = alltrue([
      for b in google_secret_manager_secret_iam_binding.qa_target_adder :
      b.members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    ])
    error_message = "V-1's only member is swarm-api's account"
  }

  # V-2. MUTATION: make the role secretVersionAdder or viewer and swarm-api
  # cannot read the sign-in it hands the gate (§2.2).
  assert {
    condition = alltrue([
      for t, b in google_secret_manager_secret_iam_binding.qa_target_accessor :
      b.role == "roles/secretmanager.secretAccessor" && b.secret_id == "swarm-tenant-${t}-qa"
    ])
    error_message = "V-2 is roles/secretmanager.secretAccessor on each tenant's own swarm-tenant-<t>-qa"
  }

  # MUTATION: add module.tenancy.worker_members[t] to the members and the
  # tenant's agent can read the test account's password.
  assert {
    condition = alltrue([
      for b in google_secret_manager_secret_iam_binding.qa_target_accessor :
      b.members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    ])
    error_message = "V-2's only member is swarm-api's account"
  }

  # One binding of each per secret, and on no other secret. MUTATION: key
  # the bindings by anything other than the secrets' tenants.
  assert {
    condition = (
      toset(keys(google_secret_manager_secret_iam_binding.qa_target_adder)) == toset(keys(google_secret_manager_secret.qa_target))
      && toset(keys(google_secret_manager_secret_iam_binding.qa_target_accessor)) == toset(keys(google_secret_manager_secret.qa_target))
    )
    error_message = "each QA secret has exactly one V-1 and one V-2 binding, and no binding names another secret"
  }

  # No worker account, its own tenant's included, on either binding. The
  # assertions above already pin the members; this one names the accounts,
  # so the failure says which. MUTATION: bind a worker on any QA secret.
  assert {
    condition = alltrue(flatten([
      for b in concat(values(google_secret_manager_secret_iam_binding.qa_target_adder), values(google_secret_manager_secret_iam_binding.qa_target_accessor)) : [
        for w in values(module.tenancy.worker_members) : !contains(b.members, w)
      ]
    ]))
    error_message = "no tenant's worker account is bound on any swarm-tenant-<t>-qa: the worker asks swarm-api for its target's sign-in and never reads the secret"
  }

  # The control: the worker members are the pinned emails, so the check
  # above is not passing over an empty or unknown list.
  assert {
    condition = module.tenancy.worker_members == {
      eng   = "serviceAccount:swarm-w-eng@saga-agents-staging.iam.gserviceaccount.com"
      smoke = "serviceAccount:swarm-w-smoke@saga-agents-staging.iam.gserviceaccount.com"
    }
    error_message = "the control: both tenants' worker members are the pinned accounts"
  }

  # "On each one only": never project-wide. MUTATION: grant swarm-api either
  # role in module.iam's project roles and it reaches every tenant's
  # provider keys.
  assert {
    condition = (
      !contains(module.iam.granted_roles["swarm-api"], "roles/secretmanager.secretAccessor")
      && !contains(module.iam.granted_roles["swarm-api"], "roles/secretmanager.secretVersionAdder")
    )
    error_message = "swarm-api holds neither secretAccessor nor secretVersionAdder project-wide: V-1 and V-2 are per-secret bindings"
  }
}

run "a_tenant_listing_qa_as_a_provider_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
        providers = ["anthropic", "qa"]
      }
    }
  }

  # modules/secret_manager would make a provider secret of the same name,
  # swarm-tenant-eng-qa, read by the worker. MUTATION: drop the precondition
  # and the plan succeeds with two resources claiming one secret.
  expect_failures = [
    google_secret_manager_secret.qa_target["eng"],
  ]
}
