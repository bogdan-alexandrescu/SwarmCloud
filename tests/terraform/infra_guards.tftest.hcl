# Whole-root composition, and the guards that exist because this project is
# SHARED: it holds a live GKE cluster, another team's VPC and 12 service
# accounts that must never be touched.
#
# Every run here is a full plan of terraform/infra against a mock provider, in
# which every computed attribute is unknown -- the same state as a FIRST apply
# into an empty project. A `for_each` or `count` derived from a value that does
# not exist yet fails here rather than on day one.

mock_provider "google" {}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
}

run "a_fresh_environment_with_no_tenants_still_plans" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {}
  }

  assert {
    condition     = length(output.job_names) == 0
    error_message = "no tenants means no Job resources, and no failure either"
  }

  assert {
    condition     = contains(output.pool_names, "global")
    error_message = "the global pool exists even before the first tenant does"
  }

  assert {
    condition     = output.firestore_database == "swarm"
    error_message = "the control plane uses the named swarm database"
  }
}

run "the_gke_backend_can_be_switched_off_entirely" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_gke_autopilot = false
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = ["anthropic"] }
    }
  }

  # Cloud Run Jobs is the primary backend, so an environment that needs no
  # browser or GPU work should not have to pay for a cluster to stand up.
  assert {
    condition     = output.gke_cluster_name == ""
    error_message = "with Autopilot disabled there must be no cluster"
  }

  assert {
    condition     = contains(output.job_names, "swarm-job-eng-claude-code")
    error_message = "the Cloud Run path must work with no cluster at all"
  }
}

# A tenant that states no ceiling must take the ENVIRONMENT's default, not a
# number hidden in the type constraint.
#
# `max_active` is declared `optional(number)` with no default precisely so that
# the coalesce in locals.tf has a null to fall through to. Give the optional a
# default and the fallback becomes unreachable: `pool_limits.default_tenant`
# turns into dead configuration that both tfvars files set as though it worked,
# and an operator adding a tenant without a ceiling silently gets the type's
# number instead of the environment's. In dev that is the difference between a
# tenant capped at 10 and one tenant able to consume the entire global budget of
# 20 -- in the environment whose own comment says a runaway loop spends real
# money. Nothing else in this suite exercises a tenant that omits max_active.
run "a_tenant_that_states_no_ceiling_takes_the_environments_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"

    tenants = {
      declared = { kind = "group", principal = "eng@saga.xyz", providers = [], max_active = 7 }
      silent   = { kind = "group", principal = "research@saga.xyz", providers = [] }
    }

    pool_limits = {
      global          = 20
      default_tenant  = 10
      provider_tenant = 5
    }
  }

  assert {
    condition     = output.pool_limits["tenant:silent"] == 10
    error_message = "a tenant with no max_active must take pool_limits.default_tenant; a number from the variable's type constraint means the environment's ceiling is dead configuration"
  }

  assert {
    condition     = output.pool_limits["tenant:declared"] == 7
    error_message = "an explicit max_active must still win over the default"
  }

  # The Firestore tenant document and the slot pool are resolved from one local
  # so the ceiling admission enforces and the ceiling Firestore records cannot
  # disagree. If they ever did, the pool would be the real limit and the
  # document would be a lie an operator reads.
  assert {
    condition     = output.pool_limits["tenant:silent"] <= output.pool_limits["global"]
    error_message = "a single undeclared tenant must not be able to consume the whole environment's budget"
  }
}

run "a_tenant_ceiling_of_zero_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = [], max_active = 0 }
    }
  }

  # Zero is not "unlimited" and it is not "paused" either: it is a tenant that
  # can never admit anything, which is a ceiling nobody chose. Omitting the
  # field is how you ask for the default.
  expect_failures = [var.tenants]
}

run "the_prod_shaped_configuration_composes" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # The same shape as terraform/environments/prod/prod.tfvars: protection on,
  # immutable tags, and three tenants of which one holds no provider key at all.
  variables {
    environment          = "prod"
    deletion_protection  = true
    bucket_force_destroy = false
    immutable_image_tags = true
    gke_release_channel  = "STABLE"

    tenants = {
      eng      = { kind = "group", principal = "eng@saga.xyz", providers = ["anthropic", "openai"], max_active = 40 }
      research = { kind = "group", principal = "research@saga.xyz", providers = ["anthropic"], max_active = 20 }
      smoke    = { kind = "group", principal = "swarm-smoke@saga.xyz", providers = [], max_active = 2 }
    }

    pool_limits = {
      global          = 100
      default_tenant  = 20
      provider_tenant = 10
    }
  }

  assert {
    condition     = output.pool_limits["tenant:eng"] == 40 && output.pool_limits["tenant:smoke"] == 2
    error_message = "a tenant's max_active must become its own pool ceiling, not the default"
  }

  assert {
    condition     = output.pool_limits["global"] == 100
    error_message = "the global ceiling comes from pool_limits"
  }

  # research holds an anthropic key but no openai key, so it gets claude-code
  # and not codex. A codex Job for research would fail at start on a missing
  # secret; the task should PARK as CREDENTIAL_MISSING instead and cost nothing.
  assert {
    condition     = contains(output.job_names, "swarm-job-research-claude-code") && !contains(output.job_names, "swarm-job-research-codex")
    error_message = "a Job resource must exist only where the tenant actually holds that provider's credential"
  }

  assert {
    condition     = contains(output.pool_names, "provider:anthropic:tenant:research") && !contains(output.pool_names, "provider:openai:tenant:research")
    error_message = "per-tenant provider pools follow the credentials the tenant actually has"
  }

  assert {
    condition     = output.labels["swarm-env"] == "prod"
    error_message = "prod resources are labelled prod"
  }
}

run "prod_without_deletion_protection_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment         = "prod"
    deletion_protection = false
  }

  expect_failures = [var.deletion_protection]
}

run "prod_deletion_protection_can_only_be_waived_deliberately" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment            = "prod"
    deletion_protection    = false
    allow_unprotected_prod = true
    tenants                = {}
  }

  # The override exists so the refusal above is a guard rather than a wall. It
  # is never set implicitly by anything.
  assert {
    condition     = output.environment == "prod"
    error_message = "the explicit override must let a deliberate prod teardown plan"
  }
}

run "prod_may_not_let_a_destroy_take_the_checkpoints" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment          = "prod"
    bucket_force_destroy = true
  }

  expect_failures = [var.bucket_force_destroy]
}

run "the_default_firestore_database_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    firestore_database = "(default)"
  }

  expect_failures = [var.firestore_database]
}

run "a_public_api_invoker_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    api_invokers = ["allUsers"]
  }

  expect_failures = [var.api_invokers]
}

run "an_unbounded_control_plane_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    service_max_instances = {
      "swarm-api"          = 0
      "swarm-scheduler"    = 2
      "swarm-quota-broker" = 2
      "swarm-reconciler"   = 1
    }
  }

  expect_failures = [var.service_max_instances]
}

run "auth_cannot_be_opened_to_every_domain" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    allowed_domains = []
  }

  expect_failures = [var.allowed_domains]
}

run "an_unknown_environment_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "staging-2"
  }

  expect_failures = [var.environment]
}
