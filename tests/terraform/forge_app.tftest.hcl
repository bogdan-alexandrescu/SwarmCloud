# The SwarmCloud GitHub App, the deployment side (docs/onboarding.md §3.4, lane
# OB2 of #780; docs/runbooks/github-app.md is how the owner registers it).
#
# What these runs hold:
#
#   * the App's two platform secret SLOTS -- client secret and private key --
#     exist with no version, carry managed-by=swarm-terraform, and are readable
#     by swarm-api alone, through an authoritative per-secret binding;
#   * the user slots swarm-api creates at onboarding (D3) are reachable through
#     owner-applied grants in terraform/bootstrap, each but the create scoped to
#     one tenant's `swarm-tenant-<t>-git-u-` prefix in its full
#     `projects/<number>/secrets/` form: swarm-api creates (a custom role with
#     secrets.create and nothing else), adds versions, disables and re-enables
#     them (a custom role with versions.disable and versions.enable and
#     nothing else, so Disconnect leaves no usable token, OB3), and reads
#     both the base slots and their `-refresh` twins (owner decision
#     2026-10-08: it reuses the person's current access token instead of
#     refreshing on every call); the tenant's worker reads only the base
#     slots (D7, U1);
#   * a tenant id whose user-slot prefix would start with another tenant's is
#     refused, because a prefix condition would hand that tenant's slots over;
#   * the refresher job (D2) is `swarm-forge-refresh`, every 15 minutes, OIDC,
#     its ownership in its description, and absent until switched on;
#   * forge_grants and forge_orgs have their (tenant_id, user_hash) indexes.
#
# None of this proves what IAM does with the conditions live; see
# terraform/bootstrap/forge_user_slots.tf, "NOT VERIFIED LIVE".

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]
}

# ---------------------------------------------------------------------------
# The App's platform secrets (modules/secret_manager).
# ---------------------------------------------------------------------------

run "the_app_secrets_are_two_empty_slots_only_swarm_api_reads" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    labels         = { "managed-by" = "swarm-terraform" }
    tenant_secrets = {}

    github_app_secrets_enabled = true
    github_app_secret_readers  = ["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"]
  }

  assert {
    condition = toset(output.github_app_secret_ids) == toset([
      "swarm-github-app-client-secret",
      "swarm-github-app-private-key",
    ])
    error_message = "the App's two platform slots are swarm-github-app-client-secret and swarm-github-app-private-key; docs/runbooks/github-app.md and scripts/create-secrets.sh --github-app name exactly these"
  }

  assert {
    condition = alltrue([
      for k, s in google_secret_manager_secret.github_app : s.labels["managed-by"] == "swarm-terraform" && s.labels["component"] == "github-app"
    ])
    error_message = "every App secret carries managed-by=swarm-terraform, or make destroy can never remove it, and component=github-app"
  }

  assert {
    condition = alltrue([
      for k, s in google_secret_manager_secret.github_app : s.replication[0].user_managed[0].replicas[0].location == "us-central1"
    ])
    error_message = "the App's secrets are pinned to the workload region, as every tenant credential is"
  }

  # Authoritative and per secret: the members list IS the complete reader set,
  # and a grant made out of band is removed on the next apply.
  assert {
    condition = alltrue([
      for k, b in google_secret_manager_secret_iam_binding.github_app_accessor :
      b.role == "roles/secretmanager.secretAccessor" && b.members == toset(["serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"])
    ]) && length(google_secret_manager_secret_iam_binding.github_app_accessor) == 2
    error_message = "swarm-api, and nobody else, reads the App's client secret and private key"
  }

  # No version: a managed version would put the plaintext in a state file
  # several people can read. scripts/create-secrets.sh --github-app adds it.
  assert {
    condition = alltrue([
      for k, s in google_secret_manager_secret.github_app : s.annotations["swarm-populated-by"] == "scripts/create-secrets.sh --github-app ${k} --stdin"
    ])
    error_message = "each App secret names the one command that adds its value"
  }
}

run "the_app_secrets_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    labels         = { "managed-by" = "swarm-terraform" }
    tenant_secrets = {}
  }

  assert {
    condition     = length(google_secret_manager_secret.github_app) == 0 && length(google_secret_manager_secret_iam_binding.github_app_accessor) == 0
    error_message = "with github_app_secrets_enabled false the module declares no App secret"
  }
}

run "an_app_secret_reader_must_be_a_service_account" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    labels         = { "managed-by" = "swarm-terraform" }
    tenant_secrets = {}

    github_app_secrets_enabled = true
    github_app_secret_readers  = ["group:eng@saga.xyz"]
  }

  expect_failures = [var.github_app_secret_readers]
}

run "enabled_app_secrets_need_a_reader" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    labels         = { "managed-by" = "swarm-terraform" }
    tenant_secrets = {}

    github_app_secrets_enabled = true
  }

  expect_failures = [var.github_app_secret_readers]
}

# ---------------------------------------------------------------------------
# The user slots' grants (terraform/bootstrap, forge_user_slots.tf).
# ---------------------------------------------------------------------------

run "user_slot_grants_are_scoped_per_tenant_and_split_read_from_write" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.forge_user_slots
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_forge_user_slots = true
    infra_tenants_tfvars    = "../../tests/terraform/tenants_tfvars_fixture/fixture.tfvars"
  }

  # The role creates a secret and does nothing else: no read, no version, no
  # IAM, no delete.
  assert {
    condition     = google_project_iam_custom_role.forge_slot_creator[0].permissions == toset(["secretmanager.secrets.create"])
    error_message = "swarmForgeSlotCreator must carry secretmanager.secrets.create and nothing else; any read permission on it would be project-wide"
  }

  assert {
    condition     = google_project_iam_custom_role.forge_slot_creator[0].role_id == "swarmForgeSlotCreator"
    error_message = "the role id is the one terraform/modules/custom_role_ids spells"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.forge_slot_creator[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.forge_slot_creator[0].role == "projects/saga-agents-staging/roles/swarmForgeSlotCreator",
    ])
    error_message = "swarm-api, alone, holds the slot-creator role"
  }

  # One binding per tenant, each on that tenant's full-form prefix.
  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.forge_slot_version_adder :
      m.role == "roles/secretmanager.secretVersionAdder" &&
      m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com" &&
      m.condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-${t}-git-u-\")"
    ]) && length(google_project_iam_member.forge_slot_version_adder) > 0
    error_message = "swarm-api adds versions to user slots and their -refresh twins, one tenant's prefix per binding, by the project NUMBER"
  }

  # Disconnect disables the user's versions and reconnect re-enables them
  # (OB3). The role does exactly that: no destroy, which would lose the slot's
  # history, and no access, which would let swarm-api read a base slot.
  assert {
    condition     = google_project_iam_custom_role.forge_slot_version_manager[0].permissions == toset(["secretmanager.versions.disable", "secretmanager.versions.enable"])
    error_message = "swarmForgeSlotVersionManager must carry secretmanager.versions.disable and secretmanager.versions.enable and nothing else: no destroy, no access"
  }

  assert {
    condition     = google_project_iam_custom_role.forge_slot_version_manager[0].role_id == "swarmForgeSlotVersionManager"
    error_message = "the role id is the one terraform/modules/custom_role_ids spells"
  }

  # One binding per tenant, on exactly the prefix the version adder has.
  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.forge_slot_version_manager :
      m.role == "projects/saga-agents-staging/roles/swarmForgeSlotVersionManager" &&
      m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com" &&
      m.condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-${t}-git-u-\")" &&
      m.condition[0].expression == google_project_iam_member.forge_slot_version_adder[t].condition[0].expression
    ]) && length(google_project_iam_member.forge_slot_version_manager) > 0
    error_message = "swarm-api disables and enables versions of user slots and their -refresh twins, one tenant's prefix per binding, the version adder's own condition"
  }

  # Only the create grant is unconditioned; every per-tenant grant carries
  # exactly one condition.
  assert {
    condition = alltrue(flatten([
      for grants in [
        google_project_iam_member.forge_slot_version_adder,
        google_project_iam_member.forge_slot_version_manager,
        google_project_iam_member.forge_refresh_reader,
        google_project_iam_member.forge_slot_reader,
      ] : [for m in values(grants) : length(m.condition) == 1]
    ]))
    error_message = "a per-tenant user-slot grant without its prefix condition would reach every secret in the project, the other team's included"
  }

  # The worker reads base slots and never a -refresh twin, in either the
  # secret's or the version's resource-name form.
  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.forge_slot_reader :
      m.role == "roles/secretmanager.secretAccessor" &&
      m.member == "serviceAccount:swarm-agent-worker-${t}@saga-agents-staging.iam.gserviceaccount.com" &&
      startswith(m.condition[0].expression, "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-${t}-git-u-\") && ") &&
      strcontains(m.condition[0].expression, "!resource.name.endsWith(\"-refresh\")") &&
      strcontains(m.condition[0].expression, "!resource.name.extract(\"/secrets/{name}/versions/\").endsWith(\"-refresh\")")
    ]) && length(google_project_iam_member.forge_slot_reader) > 0
    error_message = "a tenant's worker reads only its own tenant's base user slots, never a -refresh twin (decision D7, U1)"
  }

  # swarm-api reads the base slots AND their -refresh twins, on the tenant's
  # prefix and nothing narrower (owner decision 2026-10-08). It reuses the
  # person's current access token rather than refreshing on every call --
  # each refresh made GitHub drop the token a running task held, which then
  # failed 401. It already held the refresh token, which mints access
  # tokens, so reading the access token adds no power. Exactly the version
  # adder's condition: the slots it writes, and no other secret.
  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.forge_refresh_reader :
      m.role == "roles/secretmanager.secretAccessor" &&
      m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com" &&
      m.condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-${t}-git-u-\")" &&
      m.condition[0].expression == google_project_iam_member.forge_slot_version_adder[t].condition[0].expression
    ]) && length(google_project_iam_member.forge_refresh_reader) > 0
    error_message = "swarm-api reads each tenant's user slots, base and -refresh twin alike, on that tenant's prefix and nothing wider (owner decision 2026-10-08)"
  }

  # The binding's title names what it reads now. A condition is part of an
  # IAM binding's identity, so this change replaces the binding; the resource
  # carries create_before_destroy so the sweep never loses the twins mid-apply
  # (the mock provider cannot assert that; the plan shows it).
  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.forge_refresh_reader :
      m.condition[0].title == "swarm forge user slots api ${t}"
    ])
    error_message = "swarm-api's user-slot reader binding is titled for what it reads now: the base slots and their twins"
  }

  # The same tenants on every per-tenant grant, and exactly the fixture's.
  assert {
    condition = alltrue([
      toset(keys(google_project_iam_member.forge_slot_reader)) == toset(local.infra_tenant_ids),
      toset(keys(google_project_iam_member.forge_slot_version_adder)) == toset(local.infra_tenant_ids),
      toset(keys(google_project_iam_member.forge_refresh_reader)) == toset(local.infra_tenant_ids),
      toset(keys(google_project_iam_member.forge_slot_version_manager)) == toset(local.infra_tenant_ids),
    ])
    error_message = "every tenant the release applies gets the four per-tenant user-slot grants, and no other key does"
  }

  # Nothing here names the other team's secrets: every prefix is ours.
  assert {
    condition = alltrue([
      for t, p in local.forge_user_slot_prefixes : startswith(p, "projects/209012342332/secrets/swarm-tenant-")
    ])
    error_message = "a user-slot prefix outside swarm-tenant-; the other team's secrets in this project are agents-* and promptlab-*"
  }
}

run "user_slot_grants_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_forge_user_slots = false
  }

  assert {
    condition = alltrue([
      length(google_project_iam_custom_role.forge_slot_creator) == 0,
      length(google_project_iam_member.forge_slot_creator) == 0,
      length(google_project_iam_member.forge_slot_version_adder) == 0,
      length(google_project_iam_member.forge_slot_reader) == 0,
      length(google_project_iam_member.forge_refresh_reader) == 0,
      length(google_project_iam_custom_role.forge_slot_version_manager) == 0,
      length(google_project_iam_member.forge_slot_version_manager) == 0,
    ])
    error_message = "enable_forge_user_slots = false must grant nothing"
  }
}

# `eng-git-u-x`'s slots are named swarm-tenant-eng-git-u-x-git-u-<hex>, which
# starts with `eng`'s prefix: eng's worker would read them. Refused at plan.
run "a_tenant_whose_prefix_nests_inside_anothers_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_forge_user_slots = true
    infra_tenants_tfvars    = "../../tests/terraform/forge_app_fixture/nested_tenants.tfvars"
  }

  expect_failures = [google_project_iam_member.forge_slot_reader]
}

run "bootstrap_tfvars_as_committed_switch_the_user_slots_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # Read as text, as deployer_service_accounts.tf reads dev.tfvars: the file is
  # `terraform fmt`ed, so the switch is one line of exactly this shape.
  variables {
    enable_forge_user_slots = length(regexall("(?m)^enable_forge_user_slots[ \\t]*=[ \\t]*true[ \\t]*$", file("../../terraform/bootstrap/terraform.tfvars"))) == 1
  }

  assert {
    condition     = var.enable_forge_user_slots && length(google_project_iam_custom_role.forge_slot_creator) == 1
    error_message = "terraform/bootstrap/terraform.tfvars must set enable_forge_user_slots = true: owner decisions D3 and D7, made by the owner's next bootstrap apply"
  }
}

# ---------------------------------------------------------------------------
# The refresher job (modules/scheduler jobs.tf).
# ---------------------------------------------------------------------------

run "the_refresher_job_ticks_every_fifteen_minutes_with_oidc" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    wake_topic_name         = "swarm-scheduler-wake"
    scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
    reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
    quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
    tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
    api_endpoint            = "https://swarm-api-abcdef-uc.a.run.app"
    labels                  = { "managed-by" = "swarm-terraform" }
    publisher_members       = {}
    enable_forge_refresh    = true
  }

  assert {
    condition     = google_cloud_scheduler_job.forge_refresh[0].name == "swarm-forge-refresh"
    error_message = "the refresher job is swarm-forge-refresh (docs/onboarding.md §3.4 item 6)"
  }

  assert {
    condition     = google_cloud_scheduler_job.forge_refresh[0].schedule == "*/15 * * * *"
    error_message = "the refresh sweep runs every 15 minutes"
  }

  # A Cloud Scheduler job carries no labels, so its ownership is in its
  # description, as every other job's is.
  assert {
    condition     = google_cloud_scheduler_job.forge_refresh[0].description == "managed-by=swarm-terraform; refreshes GitHub user access tokens before they expire"
    error_message = "the job's description states managed-by=swarm-terraform and what it does"
  }

  assert {
    condition = alltrue([
      google_cloud_scheduler_job.forge_refresh[0].http_target[0].http_method == "POST",
      google_cloud_scheduler_job.forge_refresh[0].http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/forge/refresh",
      google_cloud_scheduler_job.forge_refresh[0].http_target[0].oidc_token[0].service_account_email == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com",
      google_cloud_scheduler_job.forge_refresh[0].http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app",
    ])
    error_message = "the job POSTs swarm-api's refresh sweep with an OIDC token minted for the rollup-sweeper account, audience the service URL"
  }

  assert {
    condition     = google_cloud_scheduler_job.forge_refresh[0].retry_config[0].retry_count == 0
    error_message = "no retry: a refresh token works once, and the next tick is the retry"
  }
}

run "the_refresher_job_is_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    wake_topic_name         = "swarm-scheduler-wake"
    scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
    reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
    quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
    tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
    api_endpoint            = "https://swarm-api-abcdef-uc.a.run.app"
    labels                  = { "managed-by" = "swarm-terraform" }
    publisher_members       = {}
  }

  assert {
    condition     = length(google_cloud_scheduler_job.forge_refresh) == 0
    error_message = "the refresh sweep's route ships with lane OB3; until enable_forge_refresh is set the job must not exist"
  }
}

# ---------------------------------------------------------------------------
# The indexes (modules/firestore).
# ---------------------------------------------------------------------------

run "forge_grants_and_forge_orgs_have_their_tenant_user_indexes" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  assert {
    condition = alltrue([
      for name, coll in { "forge-grants-tenant-user" = "forge_grants", "forge-orgs-tenant-user" = "forge_orgs" } :
      google_firestore_index.this[name].collection == coll &&
      [for f in google_firestore_index.this[name].fields : f.field_path] == ["tenant_id", "user_hash"]
    ])
    error_message = "forge_grants and forge_orgs each need a (tenant_id, user_hash) composite index (docs/onboarding.md §3.4 item 7)"
  }
}
