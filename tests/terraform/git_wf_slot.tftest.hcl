# No -git-wf slot (#2029, WF-APP-TOKEN; docs/design/agent-autonomy.md §3.3).
#
# BGD, 2026-10-22: the workflow-scoped `swarm-tenant-<tenant>-git-wf` slot and
# swarm-api's accessor on it (IAM row L-2) are removed. A workflow-file push
# uses a token the SwarmCloud GitHub App mints per push, for one repository
# whose registration has `workflow_files` switched on; nothing is stored.
#
# One run holds it: no google_secret_manager_secret is named `*-git-wf`, and
# swarm-api holds no accessor on such a secret -- in the plan (every tenant's
# secret module.secret_manager declares) and in the source of every root and
# module file, so a resource added outside the module is caught too.
#
# Two more (WF-APP-FOLLOW): var.tenants refuses a provider named git-wf in
# every environment -- the validation git_wf_tenant carried went with it --
# and still accepts an ordinary provider, so the refusal is not a refusal of
# everything.

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

  # Two tenants, both holding a -git slot, so "no tenant has a -git-wf
  # slot" is asked of more than one.
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

  # Every image pinned by digest, or verify.tf refuses the plan.
  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "no_git_wf_secret_and_no_swarm_api_accessor_on_one" {
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

  # MUTATION: list git-wf among a tenant's providers and module.secret_manager
  # declares swarm-tenant-<t>-git-wf with the tenant's worker as its reader.
  assert {
    condition = alltrue(flatten([
      for t, ids in output.tenant_secret_ids : [for p, id in ids : !endswith(id, "-git-wf")]
    ]))
    error_message = "no tenant secret is named *-git-wf: the slot was removed (#2029)"
  }

  # MUTATION: restore google_secret_manager_secret.git_wf (or any secret whose
  # id ends -git-wf) in terraform/infra or a module and this is red.
  assert {
    # (`output.tenant_secret_ids != null`: terraform test refuses a condition
    # that names no object of the configuration.)
    condition = output.tenant_secret_ids != null && alltrue([
      for f in concat(
        [for n in fileset("../../terraform/infra", "*.tf") : "../../terraform/infra/${n}"],
        [for n in fileset("../../terraform/modules", "**/*.tf") : "../../terraform/modules/${n}"],
      ) : length(regexall("(?m)^[^#\\n]*(secret_id\\s*=\\s*\"[^\"]*-git-wf\"|resource\\s+\"google_secret_manager_secret\"\\s+\"git_wf\")", file(f))) == 0
    ])
    error_message = "no google_secret_manager_secret is named *-git-wf in terraform/infra or terraform/modules (#2029)"
  }

  # MUTATION: restore google_secret_manager_secret_iam_binding.git_wf_accessor
  # (IAM row L-2) and swarm-api reads a -git-wf secret again.
  assert {
    # (`output.tenant_secret_ids != null`: terraform test refuses a condition
    # that names no object of the configuration.)
    condition = output.tenant_secret_ids != null && alltrue([
      for f in concat(
        [for n in fileset("../../terraform/infra", "*.tf") : "../../terraform/infra/${n}"],
        [for n in fileset("../../terraform/modules", "**/*.tf") : "../../terraform/modules/${n}"],
      ) : length(regexall("(?m)^[^#\\n]*(google_secret_manager_secret\\.git_wf|\"git_wf_accessor\"|-git-wf\")", file(f))) == 0
    ])
    error_message = "swarm-api holds no secretAccessor on a -git-wf secret: IAM row L-2 was removed (#2029)"
  }

  # MUTATION: leave dev.tfvars' git_wf_tenant line (or a tenant listing
  # git-wf) and the removed slot's input is still set.
  assert {
    condition     = output.tenant_secret_ids != null && length(regexall("(?m)^\\s*git_wf_tenant\\s*=|\"git-wf\"", file("../../terraform/environments/dev/dev.tfvars"))) == 0
    error_message = "dev.tfvars no longer sets git_wf_tenant nor lists git-wf as a provider (#2029)"
  }
}

run "a_tenant_listing_git_wf_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    # prod, not dev: the refusal holds in every environment, not only where
    # the slot once lived.
    environment = "prod"
    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic", "git", "git-wf"] }
    }
  }

  # MUTATION: delete the git-wf validation on var.tenants in
  # terraform/infra/variables.tf and the plan goes through, giving the tenant
  # a swarm-tenant-eng-git-wf secret its worker can read.
  expect_failures = [var.tenants]
}

run "a_tenant_listing_ordinary_providers_is_accepted" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "prod"
    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic", "git"] }
    }
  }

  override_resource {
    target          = module.iam.google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # MUTATION: write the validation as a refusal of every provider starting
  # "git" (or of any non-empty list) and this plan fails on var.tenants.
  assert {
    condition     = output.tenant_secret_ids["eng"]["git"] == "swarm-tenant-eng-git"
    error_message = "a tenant listing anthropic and git plans, with its -git slot"
  }
}
