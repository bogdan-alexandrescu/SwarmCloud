# The scoped probe admin list reaches swarm-api (owner decision 2026-11-19:
# the acceptance probes' admin identity is an admin only inside named probe
# workspaces, never a full platform admin).
#
# var.probe_admin_users renders PROBE_ADMIN_USERS, a JSON list of
# {principal, tenants}, into swarm-api's environment, which
# swarm_api.settings._probe_admins parses. These runs hold the rendering ("[]"
# by default, the entries lower-cased and sorted when set) and the plan-time
# refusals: an IAM member, a `*` principal, a personal workspace, an empty
# tenant list, and a principal that is also a full admin, a secret admin or the
# rollup sweeper.


mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "example-project"
  environment = "dev"

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

run "the_default_renders_an_empty_list" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].PROBE_ADMIN_USERS == "[]"
    error_message = "with probe_admin_users unset swarm-api is given an empty list, which changes nothing"
  }
}

run "an_entry_renders_lower_cased_and_sorted" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    probe_admin_users = [{ principal = "Probe@example.com", tenants = ["smoke", "roles-probe"] }]
  }

  assert {
    condition     = jsondecode(local.service_env["swarm-api"].PROBE_ADMIN_USERS) == [{ principal = "probe@example.com", tenants = ["roles-probe", "smoke"] }]
    error_message = "an entry renders as {principal, tenants}, the principal lower-cased and the tenants sorted"
  }
}

run "an_iam_member_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    probe_admin_users = [{ principal = "serviceAccount:probe@example.com", tenants = ["smoke"] }]
  }

  expect_failures = [var.probe_admin_users]
}

run "a_personal_workspace_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    probe_admin_users = [{ principal = "probe@example.com", tenants = ["u-someone"] }]
  }

  expect_failures = [var.probe_admin_users]
}

run "an_empty_tenant_list_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    probe_admin_users = [{ principal = "probe@example.com", tenants = [] }]
  }

  expect_failures = [var.probe_admin_users]
}

run "a_full_admin_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    admin_users       = ["probe@example.com"]
    probe_admin_users = [{ principal = "Probe@example.com", tenants = ["smoke"] }]
  }

  expect_failures = [var.probe_admin_users]
}

run "a_wildcard_principal_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    probe_admin_users = [{ principal = "*@example.com", tenants = ["smoke"] }]
  }

  expect_failures = [var.probe_admin_users]
}

run "the_rollup_sweeper_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    probe_admin_users = [{ principal = "swarm-rollup-sweeper@example-project.iam.gserviceaccount.com", tenants = ["smoke"] }]
  }

  expect_failures = [var.probe_admin_users]
}

run "a_secret_admin_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    secret_admin_members = ["user:probe@example.com"]
    probe_admin_users    = [{ principal = "probe@example.com", tenants = ["smoke"] }]
  }

  expect_failures = [var.probe_admin_users]
}

# variables.tf restates the sweeper's and the tick's account ids, which a
# variable validation cannot read from the module; this holds them together.
run "the_restated_identities_are_the_modules" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = module.service_account_ids.rollup_sweeper_id == "swarm-rollup-sweeper" && module.service_account_ids.schedule_tick_id == "swarm-schedule-tick"
    error_message = "modules/service_account_ids renamed the rollup sweeper or the schedule tick: update the probe_admin_users validation in terraform/infra/variables.tf to match"
  }
}
