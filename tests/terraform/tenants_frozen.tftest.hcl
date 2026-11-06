# var.tenants is frozen to the tenants each environment has today
# (docs/design/workspace-kinds.md §4.7, Q8 (a), answered by the owner
# 2026-10-19; lane WK-TF-FREEZE of #847).
#
# A new workspace -- personal, group or service -- is made by the in-cloud job
# swarm-workspace-apply from an admin's request in Admin › Tenants (§4.11), and
# lives outside Terraform state. The tenants Terraform already owns stay here
# because they carry what the job never makes (authoritative per-provider secret
# bindings, Terraform-made Cloud Run and scheduler jobs). So terraform/infra
# refuses any key outside them, and a fifth tenant cannot be added by a tfvars
# line and a release.
#
# The frozen sets are per environment, because the environments differ:
#   dev   -- eng, smoke, u-sw-c90291, u-or-9a0792 (dev.tfvars, the four of §4.7);
#   prod  -- eng, research, smoke (prod.tfvars);
#   any other environment -- none, since no tfvars file declares one.
#
# Every tenant below is otherwise valid: the same `research` the dev run refuses
# plans in prod, so the refusal is the freeze and no other validation.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  # terraform/infra refuses to plan an image with no digest; these only have to
  # pass its validations (infra_guards.tftest.hcl has the same).
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

  frontend_iap_members = ["domain:example.com"]
}

run "dev_s_four_tenants_still_plan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
        providers = ["anthropic"]
      }
      smoke = {
        kind      = "group"
        principal = "smoke@example.com"
        providers = ["anthropic"]
      }
      "u-sw-c90291" = {
        kind            = "user"
        principal       = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
        directory_group = false
        providers       = []
        max_active      = 4
      }
      "u-or-9a0792" = {
        kind            = "user"
        principal       = "oracle-api@saga-agents-staging.iam.gserviceaccount.com"
        directory_group = false
        providers       = []
        max_active      = 4
      }
    }
  }

  assert {
    condition     = toset(keys(output.tenant_namespaces)) == toset(["eng", "smoke", "u-sw-c90291", "u-or-9a0792"])
    error_message = "dev's four Terraform tenants stay in Terraform (docs/design/workspace-kinds.md §4.7, Q8 (a)) and must still plan"
  }
}

run "a_subset_of_dev_s_tenants_still_plans" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
        providers = ["anthropic"]
      }
    }
  }

  assert {
    condition     = toset(keys(output.tenant_namespaces)) == toset(["eng"])
    error_message = "the freeze bounds the keys from above; fewer tenants is not a new one"
  }
}

# The control for the refusal below: `research` is a valid tenant, and plans
# where it already exists.
run "prod_s_three_tenants_still_plan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment          = "prod"
    deletion_protection  = true
    bucket_force_destroy = false
    immutable_image_tags = true

    tenants = {
      eng      = { kind = "group", principal = "eng@example.com", providers = ["anthropic", "openai"], max_active = 40 }
      research = { kind = "group", principal = "research@example.com", providers = ["anthropic"], max_active = 20 }
      smoke    = { kind = "group", principal = "swarm-smoke@example.com", providers = [], max_active = 2 }
    }
  }

  assert {
    condition     = toset(keys(output.tenant_namespaces)) == toset(["eng", "research", "smoke"])
    error_message = "prod.tfvars' three tenants stay in Terraform and must still plan"
  }
}

# What the refusal SAYS. `expect_failures` below only proves that var.tenants
# failed, not with which message, so the message is read from variables.tf
# itself: it must send whoever meets it to the design and to the job that
# makes a workspace now, or a tfvars author is told "no" and not where to go.
run "the_refusal_names_the_design_and_the_job" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic"] }
    }
  }

  assert {
    condition = length([
      for m in regexall("error_message\\s*=\\s*\"(var\\.tenants is frozen[^\"]*)\"", file("../../terraform/infra/variables.tf")) : m[0]
      if strcontains(m[0], "docs/design/workspace-kinds.md §4.7") && strcontains(m[0], "swarm-workspace-apply")
    ]) == 1 && contains(keys(output.tenant_namespaces), "eng")
    error_message = "the freeze's error_message in terraform/infra/variables.tf must name docs/design/workspace-kinds.md §4.7 and the job swarm-workspace-apply"
  }
}

# The refusals are last: a run that errors skips every run after it.
run "a_fifth_dev_tenant_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
        providers = ["anthropic"]
      }
      research = {
        kind      = "group"
        principal = "research@example.com"
        providers = ["anthropic"]
      }
    }
  }

  expect_failures = [var.tenants]
}

run "a_new_service_tenant_in_dev_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      "u-sw-0a1b2c" = {
        kind            = "user"
        principal       = "swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"
        directory_group = false
        providers       = []
        max_active      = 4
      }
    }
  }

  expect_failures = [var.tenants]
}

run "a_fourth_prod_tenant_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment          = "prod"
    deletion_protection  = true
    bucket_force_destroy = false
    immutable_image_tags = true

    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic"], max_active = 40 }
      ops = { kind = "group", principal = "ops@example.com", providers = [], max_active = 2 }
    }
  }

  expect_failures = [var.tenants]
}

run "a_tenant_in_an_environment_with_no_frozen_set_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "staging"
    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic"] }
    }
  }

  expect_failures = [var.tenants]
}
