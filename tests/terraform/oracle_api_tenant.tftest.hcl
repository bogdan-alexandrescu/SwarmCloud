# oracle-api submits work as a tenant of its own, u-or-9a0792 (#1568; owner
# decision 2026-10-18), modelled on swarm-verify's u-sw-c90291 and never a
# member of eng@example.com.
#
# The id is what swarm_common.identity.tenant_id_for_user gives
# oracle-api@saga-agents-staging.iam.gserviceaccount.com. Terraform cannot run
# the frozen module, so tests/unit/scripts/test_oracle_api_tenant.py recomputes
# it and holds dev.tfvars' key to it; these runs hold the PLANS to that key:
#
#   1. bootstrap, over dev.tfvars as committed (its own parse, the default
#      infra_tenants_tfvars): the tenant is one of the release's tenants, the
#      release deployer gets its per-account grant on the tenant's worker, the
#      worker reads its own secrets prefix (its forge slots), and the account is admitted at the
#      front door with roles/iap.httpsResourceAccessor on both backends --
#      the rows the owner's bootstrap apply makes;
#   2. terraform/infra, over dev.tfvars' tenants written out: the tenant gets
#      its own worker account, namespace, GCS prefix and pool
#      (invariant 9), no provider pool (no providers, as u-sw-c90291), and
#      swarm-api's ALLOWED_USERS admits the account beside swarm-verify.

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
  project_id  = "saga-agents-staging"
  environment = "dev"

  # terraform/infra refuses to plan an image with no digest; these only have to
  # pass its validations (personal_workspaces_absent.tftest.hcl has the same).
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

  frontend_iap_members = ["domain:example.com"]
}

# 1. The bootstrap plan the owner's apply makes.
run "bootstrap_admits_oracle_api_and_grants_on_its_worker" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    frontend_iap_backends = ["swarm-ui-backend", "swarm-ui-ui-backend"]
    # deployer_admin is made only with the release deployer's WIF on, as the
    # owner's bootstrap apply has it (deployer_sa_admin.tftest.hcl).
    enable_github_wif       = true
    github_repository       = "bogdan-alexandrescu/SwarmCloud"
    enable_forge_user_slots = true
    # terraform test cannot load terraform/bootstrap/terraform.tfvars, so the
    # member is passed here and the file's line is held by the text assertion
    # below (and by test_oracle_api_tenant.py).
    frontend_iap_members = [
      "domain:example.com",
      "serviceAccount:oracle-api@saga-agents-staging.iam.gserviceaccount.com",
    ]
  }

  assert {
    condition     = contains(local.infra_tenant_ids, "u-or-9a0792")
    error_message = "terraform/environments/dev/dev.tfvars declares no tenant u-or-9a0792 (oracle-api's, owner decision 2026-10-18)"
  }

  assert {
    condition     = contains(keys(google_service_account_iam_member.deployer_admin), "swarm-agent-worker-u-or-9a0792")
    error_message = "the release deployer gets no serviceAccountAdmin on swarm-agent-worker-u-or-9a0792, so the release that adds the tenant 403s on its setIamPolicy"
  }

  # Its secrets prefix (invariant 9): the worker reads its own forge slots,
  # swarm-tenant-u-or-9a0792-git-u-*, and no other tenant's.
  assert {
    condition = (
      contains(keys(google_project_iam_member.forge_slot_reader), "u-or-9a0792") &&
      endswith(local.forge_user_slot_prefixes["u-or-9a0792"], "/secrets/swarm-tenant-u-or-9a0792-git-u-")
    )
    error_message = "u-or-9a0792's worker gets no read on its own forge slots, swarm-tenant-u-or-9a0792-git-u-*"
  }

  assert {
    condition = alltrue([
      for b in ["swarm-ui-backend", "swarm-ui-ui-backend"] :
      google_iap_web_backend_service_iam_member.frontend_accessors["${b} serviceAccount:oracle-api@saga-agents-staging.iam.gserviceaccount.com"].role == "roles/iap.httpsResourceAccessor"
    ])
    error_message = "oracle-api is not admitted with roles/iap.httpsResourceAccessor on both front-door backends"
  }

  # The committed record of the owner's apply: the member is in the file,
  # for the project this plan grants in.
  assert {
    condition     = var.project_id == "saga-agents-staging" && length(regexall("(?ms)^frontend_iap_members\\s*=\\s*\\[[^\\]]*\"serviceAccount:oracle-api@saga-agents-staging\\.iam\\.gserviceaccount\\.com\"", file("../../terraform/bootstrap/terraform.tfvars"))) == 1
    error_message = "terraform/bootstrap/terraform.tfvars' frontend_iap_members does not list serviceAccount:oracle-api@saga-agents-staging.iam.gserviceaccount.com; a later bootstrap plan would remove the owner's grant"
  }
}

# 2. terraform/infra over dev.tfvars' tenants. The map is written out because
# terraform cannot decode a tfvars file; u_bogdan_removed.tftest.hcl holds the
# file's keys to the same four.
run "infra_gives_u_or_9a0792_its_own_boundaries_and_admits_the_account" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # The verify account's email is computed, so unknown in a mock plan;
  # ALLOWED_USERS joins it, and an unknown join cannot be compared.
  override_resource {
    target          = google_service_account.verify
    override_during = plan
    values = {
      email = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  variables {
    allowed_users = ["oracle-api@saga-agents-staging.iam.gserviceaccount.com"]
    tenants = {
      eng = {
        kind             = "group"
        principal        = "eng@example.com"
        directory_group  = true
        display_name     = "Engineering"
        providers        = ["anthropic", "openai"]
        max_active       = 45
        capacity_units   = 45
        service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@example.com"
        directory_group = true
        display_name    = "Smoke tests"
        providers       = ["anthropic"]
        max_active      = 20
        capacity_units  = 20
      }
      u-sw-c90291 = {
        kind            = "user"
        principal       = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
        display_name    = "Verification gate"
        directory_group = false
        providers       = []
        max_active      = 4
        capacity_units  = 8
      }
      u-or-9a0792 = {
        kind            = "user"
        principal       = "oracle-api@saga-agents-staging.iam.gserviceaccount.com"
        display_name    = "Oracle API"
        directory_group = false
        providers       = []
        max_active      = 4
        capacity_units  = 8
      }
    }
  }

  # Invariant 9: its own account, namespace and prefix. Its secrets prefix is
  # run 1's forge slot condition, and below.
  assert {
    condition = alltrue([
      contains(keys(output.tenant_service_accounts), "u-or-9a0792"),
      output.tenant_namespaces["u-or-9a0792"] == "swarm-tenant-u-or-9a0792",
      module.tenancy.gcs_prefixes["u-or-9a0792"] == "tenants/u-or-9a0792/",
    ])
    error_message = "u-or-9a0792 does not get its own worker account, namespace swarm-tenant-u-or-9a0792 and prefix tenants/u-or-9a0792/"
  }

  # Its own, not eng's: no boundary of it is shared with another tenant.
  assert {
    condition = alltrue([
      for t in ["eng", "smoke", "u-sw-c90291"] :
      output.tenant_namespaces[t] != output.tenant_namespaces["u-or-9a0792"] &&
      module.tenancy.gcs_prefixes[t] != module.tenancy.gcs_prefixes["u-or-9a0792"]
    ])
    error_message = "u-or-9a0792 shares a namespace or a GCS prefix with another tenant"
  }

  # No providers, as u-sw-c90291: no Terraform-managed key (secret_inputs
  # carries only tenants that declare a provider; its git token is stored by
  # scripts/create-secrets.sh as swarm-tenant-u-or-9a0792-git) and no provider
  # pool. A Claude account is lent to it from the pool instead.
  assert {
    condition = (
      !contains(keys(output.tenant_secret_ids), "u-or-9a0792") &&
      contains(output.pool_names, "tenant:u-or-9a0792") &&
      length([for p in output.pool_names : p if startswith(p, "provider:") && endswith(p, ":tenant:u-or-9a0792")]) == 0
    )
    error_message = "u-or-9a0792 should have its tenant pool and no provider key or provider pool (providers = [], swarm-verify's pattern)"
  }

  assert {
    condition = toset(split(",", local.service_env["swarm-api"].ALLOWED_USERS)) == toset([
      "swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
      "oracle-api@saga-agents-staging.iam.gserviceaccount.com",
    ])
    error_message = "swarm-api's ALLOWED_USERS is not exactly swarm-verify and oracle-api: ${local.service_env["swarm-api"].ALLOWED_USERS}"
  }
}
