# The -git-wf slot: the platform tenant's workflow-scoped forge token
# (docs/design/agent-autonomy.md §3.3, owner decision Q3; lane AU-2 WF-SLOT;
# terraform/infra/git_tools.tf).
#
# What is held here:
#
#   * `swarm-tenant-<platform tenant>-git-wf` exists for the ONE tenant
#     var.git_wf_tenant names, and for no other tenant; with the variable
#     unset there is no slot at all;
#   * it is declared EMPTY: Terraform holds the container, never a version
#     (scripts/create-secrets.sh --git-wf --stdin adds the value;
#     tests/unit/scripts/test_create_secrets_git_wf.py holds that git_tools.tf
#     declares no google_secret_manager_secret_version);
#   * it carries managed-by=swarm-terraform, so make destroy can delete it;
#   * swarm-api's account is its one reader, by an AUTHORITATIVE binding, so
#     no tenant's worker account reads it and a grant made out of band is
#     removed on the next apply (IAM row L-2);
#   * no tenant may list `git-wf` as a provider, which would make
#     module.secret_manager declare the same secret with the tenant's worker
#     as its reader.

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

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  # Two tenants, both holding a -git slot: the platform tenant (eng here) and
  # smoke, which is not, so "for the platform tenant only" can come out wrong.
  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "git"]
    }
    smoke = {
      kind      = "group"
      principal = "smoke@example.com"
      providers = ["anthropic", "git"]
    }
  }

  git_wf_tenant = "eng"

  # Every image pinned by digest, or verify.tf refuses the plan.
  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}

run "the_platform_tenant_has_one_empty_slot_only_swarm_api_reads" {
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

  # Known worker emails, so "no worker account" is a comparison and not an
  # unknown.
  override_resource {
    target          = module.tenancy.google_service_account.worker
    override_during = plan
    values = {
      email = "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # MUTATION: key the slot on every tenant (for_each = var.tenants) and smoke
  # gets one too; drop the resource and the platform tenant has none.
  assert {
    condition     = [for s in google_secret_manager_secret.git_wf : s.secret_id] == ["swarm-tenant-eng-git-wf"]
    error_message = "exactly one -git-wf slot, swarm-tenant-<platform tenant>-git-wf, for the tenant git_wf_tenant names and no other"
  }

  # MUTATION: list git-wf among a tenant's providers in module.secret_manager
  # (or add it to forge_readers' path) and a second, worker-read copy appears.
  assert {
    condition = alltrue(flatten([
      for t, ids in output.tenant_secret_ids : [for p, id in ids : !endswith(id, "-git-wf")]
    ]))
    error_message = "module.secret_manager declares no -git-wf secret: that module makes the tenant's worker a reader"
  }

  # MUTATION: drop local.labels from the merge and make destroy refuses the
  # slot as unlabelled.
  assert {
    condition     = google_secret_manager_secret.git_wf[0].labels["managed-by"] == "swarm-terraform"
    error_message = "the slot carries managed-by=swarm-terraform"
  }

  # MUTATION: label it component=tenant-credential and create-secrets.sh
  # --list, purge-data.sh and the broker's discovery take it for a worker's
  # provider key.
  assert {
    condition = (
      google_secret_manager_secret.git_wf[0].labels["component"] == "forge-workflow-credential"
      && google_secret_manager_secret.git_wf[0].labels["tenant"] == "eng"
    )
    error_message = "the slot is labelled component=forge-workflow-credential and tenant=<platform tenant>, never tenant-credential"
  }

  # MUTATION: automatic replication copies the token into every region.
  assert {
    condition = (
      length(google_secret_manager_secret.git_wf[0].replication[0].user_managed) == 1
      && [for r in google_secret_manager_secret.git_wf[0].replication[0].user_managed[0].replicas : r.location] == ["us-central1"]
    )
    error_message = "the slot is replicated to the workload region only, as every tenant credential is"
  }

  # MUTATION: point the binding at the -git slot (or another secret) and
  # swarm-api still cannot read this one.
  assert {
    condition     = google_secret_manager_secret_iam_binding.git_wf_accessor[0].secret_id == "swarm-tenant-eng-git-wf"
    error_message = "the accessor binding is on the -git-wf slot"
  }

  # MUTATION: secretVersionManager or admin here and swarm-api could also
  # write, disable or destroy the owner's token.
  assert {
    condition     = google_secret_manager_secret_iam_binding.git_wf_accessor[0].role == "roles/secretmanager.secretAccessor"
    error_message = "swarm-api's grant is secretAccessor: it reads the token and does nothing else to it (IAM row L-2)"
  }

  # MUTATION: add the platform tenant's worker account (module.tenancy
  # worker_members["eng"]) and every agent of that tenant can push workflow
  # files with its own credentials, the thing §3.3 exists to prevent.
  assert {
    condition     = google_secret_manager_secret_iam_binding.git_wf_accessor[0].members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "swarm-api's account is the slot's one reader (IAM row L-2)"
  }

  # MUTATION: same as above, said the other way: any worker member reads it.
  assert {
    condition = alltrue([
      for t, m in module.tenancy.worker_members : !contains(google_secret_manager_secret_iam_binding.git_wf_accessor[0].members, m)
    ])
    error_message = "no tenant's worker account reads the -git-wf slot"
  }
}

run "no_platform_tenant_named_means_no_slot" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    git_wf_tenant = ""
  }

  # MUTATION: drop the count's empty-name guard and the plan declares
  # swarm-tenant--git-wf.
  assert {
    condition     = length(google_secret_manager_secret.git_wf) == 0 && length(google_secret_manager_secret_iam_binding.git_wf_accessor) == 0
    error_message = "with git_wf_tenant unset there is no -git-wf slot and no grant"
  }
}

run "a_tenant_may_not_list_git_wf_as_a_provider" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
        providers = ["anthropic", "git", "git-wf"]
      }
    }
  }

  # MUTATION: drop the validation and module.secret_manager declares
  # swarm-tenant-eng-git-wf with eng's worker as its reader.
  expect_failures = [var.git_wf_tenant]
}

run "a_platform_tenant_id_that_is_not_a_tenant_id_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    git_wf_tenant = "Eng_Team"
  }

  # MUTATION: drop the shape validation and the secret id is built from text
  # Secret Manager or the tenant resolver would never produce.
  expect_failures = [var.git_wf_tenant]
}

# dev, as committed (owner decision 2026-10-22 08:44Z, BGD: "Name the tenant;
# a follow-up lane adds one tfvars line"; lane AU2-TENANT, part of #1933): eng
# is the platform tenant. A terraform test cannot load a tfvars file into a
# run, so this run reads git_wf_tenant out of dev.tfvars' text and plans with
# it: the slot it asserts on is the one dev's release declares. A missing or
# commented-out line reads as "", which plans no slot and fails every
# assertion below.
run "dev_names_eng_as_the_platform_tenant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    git_wf_tenant = try(regex("(?m)^git_wf_tenant\\s*=\\s*\"([^\"]*)\"\\s*$", file("../../terraform/environments/dev/dev.tfvars"))[0], "")
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
    target          = module.tenancy.google_service_account.worker
    override_during = plan
    values = {
      email = "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # MUTATION: delete or comment out dev.tfvars' git_wf_tenant line (or set it
  # to "smoke", or set it twice) and this is red.
  assert {
    condition     = length(regexall("(?m)^git_wf_tenant\\s*=", file("../../terraform/environments/dev/dev.tfvars"))) == 1 && var.git_wf_tenant == "eng"
    error_message = "dev.tfvars sets git_wf_tenant = \"eng\", once: BGD named eng the platform tenant on 2026-10-22 (#1933)"
  }

  # MUTATION: set dev.tfvars' git_wf_tenant to "" and dev declares no slot;
  # key the slot on every tenant and smoke gets one too.
  assert {
    condition     = [for s in google_secret_manager_secret.git_wf : s.secret_id] == ["swarm-tenant-eng-git-wf"]
    error_message = "with dev's setting, exactly one -git-wf slot exists, swarm-tenant-eng-git-wf, and no other tenant has one"
  }

  # MUTATION: add module.tenancy.worker_members["eng"] to the binding's
  # members and eng's agents can push workflow files with their own identity.
  assert {
    condition = [
      for b in google_secret_manager_secret_iam_binding.git_wf_accessor : [b.secret_id, b.role, b.members]
      ] == [
      ["swarm-tenant-eng-git-wf", "roles/secretmanager.secretAccessor", toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])]
    ]
    error_message = "with dev's setting, swarm-api's account is the one reader of swarm-tenant-eng-git-wf, as secretAccessor (IAM row L-2)"
  }

  # MUTATION: add any tenant's worker member to the binding and this is red;
  # so does a dev.tfvars with no slot, which has no binding to hold to it.
  assert {
    condition = length(google_secret_manager_secret_iam_binding.git_wf_accessor) == 1 && alltrue(flatten([
      for b in google_secret_manager_secret_iam_binding.git_wf_accessor : [
        for t, m in module.tenancy.worker_members : !contains(b.members, m)
      ]
    ]))
    error_message = "with dev's setting, no tenant's worker account reads swarm-tenant-eng-git-wf"
  }
}
