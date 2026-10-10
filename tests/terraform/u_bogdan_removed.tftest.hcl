# u-bogdan leaves Terraform with a plan that destroys nothing (lane W9 of
# #847, 2026-10-09; docs/workspaces.md §3.3).
#
# W9 takes the owner's personal tenant out of terraform/environments/dev/
# dev.tfvars and adds a `moved` + `removed { lifecycle { destroy = false } }`
# pair for each of its instances, in terraform/infra/removed.tf and
# terraform/bootstrap/removed.tf. These runs hold that to four things:
#
#   1. the tenants dev.tfvars names no longer include u-bogdan, and the
#      bootstrap plan over that file makes no grant for it -- while eng's are
#      still planned (the control: a parse that read nothing would also make
#      no u-bogdan grant);
#   2. terraform/infra over the same tenants plans no u-bogdan instance in any
#      per-tenant resource, while eng's are planned;
#   3. the instances each root makes for a tenant shaped like u-bogdan are
#      `from`s of the `moved` blocks in that root's removed.tf -- a missing
#      one would be destroyed by the release, or by the owner's bootstrap
#      apply. Exactly, in bootstrap; in infra, through the root's outputs,
#      because a test cannot address a child module's resources;
#   4. every `moved` there lands on an address a `removed` forgets with
#      `destroy = false`, and nothing in either file destroys.
#
# 3 and 4 read removed.tf as text, the way platform_roles.tftest.hcl reads
# custom_roles_moved_to_bootstrap.tf. A mock plan has no state, so it cannot
# show the forget itself; that is the release plan's job, which must read 0 to
# add, 0 to change and 0 to destroy for u-bogdan (docs/workspaces.md §3.3, the
# operator steps). What these runs prove is that the set of forgets is
# complete and that none of them destroys.

mock_provider "google" {
  source = "./mocks/google"
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

# 1. dev.tfvars as committed, read by bootstrap's own parse (the default
# infra_tenants_tfvars), so this is the plan the owner's apply would make.
run "dev_tfvars_names_no_u_bogdan_and_bootstrap_grants_it_nothing" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    # deployer_admin is made only with the release deployer's WIF on, as the
    # owner's bootstrap apply has it (deployer_sa_admin.tftest.hcl).
    enable_github_wif       = true
    github_repository       = "bogdan-alexandrescu/SwarmCloud"
    enable_forge_user_slots = true
  }

  assert {
    condition     = !contains(local.infra_tenant_ids, "u-bogdan")
    error_message = "terraform/environments/dev/dev.tfvars still names u-bogdan; W9 moved it out of Terraform (docs/workspaces.md §3.3)"
  }

  assert {
    condition     = toset(local.infra_tenant_ids) == toset(["eng", "smoke", "u-sw-c90291"])
    error_message = "dev.tfvars' tenants are not eng, smoke and u-sw-c90291: update this run and the infra run below together, so the infra plan is still the one dev.tfvars makes"
  }

  assert {
    condition = length(flatten([
      [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${k}\"]" if strcontains(k, "u-bogdan")],
      [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${k}\"]" if strcontains(k, "u-bogdan")],
      [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${k}\"]" if strcontains(k, "u-bogdan")],
      [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${k}\"]" if strcontains(k, "u-bogdan")],
      [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${k}\"]" if strcontains(k, "u-bogdan")],
    ])) == 0
    error_message = "the bootstrap plans a grant for u-bogdan; its grants are forgotten by terraform/bootstrap/removed.tf, never made again"
  }

  # The control: the same resources still plan eng's.
  assert {
    condition = alltrue([
      contains(keys(google_service_account_iam_member.deployer_admin), "swarm-agent-worker-eng"),
      contains(keys(google_project_iam_member.forge_slot_version_adder), "eng"),
      contains(keys(google_project_iam_member.forge_slot_version_manager), "eng"),
      contains(keys(google_project_iam_member.forge_refresh_reader), "eng"),
      contains(keys(google_project_iam_member.forge_slot_reader), "eng"),
    ])
    error_message = "the bootstrap no longer plans eng's grants, so the u-bogdan assertion above proves nothing"
  }
}

# 3 and 4, bootstrap: the tenants block as it was, with a stand-in for u-bogdan.
run "bootstrap_forgets_every_grant_it_made_for_u_bogdan" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    # deployer_admin is made only with the release deployer's WIF on, as the
    # owner's bootstrap apply has it (deployer_sa_admin.tftest.hcl).
    enable_github_wif       = true
    github_repository       = "bogdan-alexandrescu/SwarmCloud"
    enable_forge_user_slots = true
    infra_tenants_tfvars    = "../../tests/terraform/u_bogdan_removed_fixture/dev_before_w9.tfvars"
  }

  assert {
    condition = length(flatten([
      [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
    ])) == 5
    error_message = "a tenant shaped like u-bogdan no longer gets 5 instances in bootstrap; a per-tenant resource was added or removed, so re-read the set and terraform/bootstrap/removed.tf together"
  }

  # Exactly: a missing `moved` leaves an instance to be DESTROYED; an extra one
  # forgets a resource that is not u-bogdan's.
  assert {
    condition = toset([for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/bootstrap/removed.tf")) : m[0]]) == toset(flatten([
      [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
    ]))
    error_message = "the `moved` blocks of terraform/bootstrap/removed.tf are not exactly the instances bootstrap makes for a tenant shaped like u-bogdan: a missing one would be DESTROYED, an extra one forgets another resource"
  }

  # Each `moved` lands on an address a `removed` forgets, and every `removed`
  # is `destroy = false`. A target no `removed` names is planned for destroy.
  assert {
    condition = (
      toset([for m in regexall("(?m)^\\s*to\\s*=\\s*(\\S+)\\s*$", file("../../terraform/bootstrap/removed.tf")) : m[0]]) == toset([for m in regexall("(?ms)^removed \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/bootstrap/removed.tf")) : m[0]]) &&
      length(regexall("(?m)^removed \\{", file("../../terraform/bootstrap/removed.tf"))) == length(flatten([
        [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      ])) &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*false\\s*$", file("../../terraform/bootstrap/removed.tf"))) == length(flatten([
        [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", "u-bogdan")}\"]" if strcontains(k, "w9-stand-in")],
      ])) &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*true", file("../../terraform/bootstrap/removed.tf"))) == 0
    )
    error_message = "terraform/bootstrap/removed.tf holds a `moved` whose target no `removed` forgets, or a `removed` that is not `destroy = false`: u-bogdan's resources are forgotten, never destroyed"
  }
}

# 2. terraform/infra over dev.tfvars' tenants. The map is written out because
# terraform cannot decode a tfvars file; the first run holds its keys to the
# file's.
run "infra_plans_nothing_for_u_bogdan_and_still_plans_eng" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind             = "group"
        principal        = "eng@saga.xyz"
        directory_group  = true
        display_name     = "Engineering"
        providers        = ["anthropic", "openai"]
        max_active       = 45
        capacity_units   = 45
        service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@saga.xyz"
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
    }
  }

  # Through the root's outputs: a test cannot address a child module's
  # resources. Between them they cover the account, the namespace, the
  # secrets, the jobs, the pools and the scheduler jobs.
  assert {
    condition = !anytrue(concat(
      [for t in keys(output.tenant_service_accounts) : strcontains(t, "u-bogdan")],
      [for t in keys(output.tenant_namespaces) : strcontains(t, "u-bogdan")],
      [for t, ids in output.tenant_secret_ids : strcontains(t, "u-bogdan") || anytrue([for id in values(ids) : strcontains(id, "u-bogdan")])],
      [for j in output.job_names : strcontains(j, "u-bogdan")],
      [for p in output.pool_names : strcontains(p, "u-bogdan")],
      [for n in output.scheduler_jobs : strcontains(n, "u-bogdan")],
    ))
    error_message = "terraform/infra plans a u-bogdan resource; it left var.tenants in W9 and its resources are forgotten by terraform/infra/removed.tf"
  }

  # The control: the same outputs still carry eng's.
  assert {
    condition = alltrue([
      contains(keys(output.tenant_service_accounts), "eng"),
      contains(keys(output.tenant_namespaces), "eng"),
      output.tenant_secret_ids["eng"]["anthropic"] == "swarm-tenant-eng-anthropic",
      contains(output.job_names, "swarm-job-eng-claude-code"),
      contains(output.pool_names, "tenant:eng"),
      contains(output.pool_names, "provider:anthropic:tenant:eng"),
      contains(output.scheduler_jobs, "swarm-workflow-rollup-eng"),
      contains(output.scheduler_jobs, "swarm-issue-sweep-eng"),
    ])
    error_message = "terraform/infra no longer plans eng's resources, so the u-bogdan assertion above proves nothing"
  }

  assert {
    condition     = toset(keys(output.tenant_namespaces)) == toset(["eng", "smoke", "u-sw-c90291"])
    error_message = "the tenants planned are not dev.tfvars' three"
  }
}

# 3 and 4, infra: dev's tenants plus a stand-in for u-bogdan.
run "infra_forgets_every_instance_it_made_for_u_bogdan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind             = "group"
        principal        = "eng@saga.xyz"
        directory_group  = true
        display_name     = "Engineering"
        providers        = ["anthropic", "openai"]
        max_active       = 45
        capacity_units   = 45
        service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@saga.xyz"
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
      # u-bogdan as it was in dev.tfvars, under another id and with
      # kind = "group": terraform/infra refuses a person in this map
      # (variables.tf; docs/workspaces.md §3.2 rule 3), and a plan that
      # declared u-bogdan's instances while removed.tf moves them fails
      # ("Moved object still exists"). Every address the map makes is keyed by
      # the tenant id, so the stand-in's addresses with its id replaced are
      # u-bogdan's.
      w9-stand-in = {
        kind           = "group"
        principal      = "w9-stand-in@saga.xyz"
        display_name   = "Bogdan (personal)"
        providers      = ["anthropic"]
        max_active     = 80
        capacity_units = 80
      }
    }
  }

  # A test cannot address a child module's resources, so the instances are
  # read through the root's outputs: the account, the tenant document, the
  # pools, the secrets, the jobs and the scheduler jobs -- 14 of the 33. Each
  # must be the `from` of a `moved`, or the release would DESTROY it. The other
  # 19 (grants and bindings, keyed the same way) were read from a verbose mock
  # plan of dev.tfvars with and without u-bogdan on 2026-10-09; the release
  # plan reading 0 to destroy is their proof (docs/workspaces.md §3.3).
  assert {
    condition = length(concat(
      [for t in keys(output.tenant_service_accounts) : "module.tenancy.google_service_account.worker[\"u-bogdan\"]" if t == "w9-stand-in"],
      [for t in keys(output.tenant_namespaces) : "module.firestore.google_firestore_document.tenant[\"u-bogdan\"]" if t == "w9-stand-in"],
      [for p in output.pool_names : "module.firestore.google_firestore_document.pool[\"${replace(p, "w9-stand-in", "u-bogdan")}\"]" if strcontains(p, "w9-stand-in")],
      [for id in values(output.tenant_secret_ids["w9-stand-in"]) : "module.secret_manager.google_secret_manager_secret.this[\"${replace(id, "w9-stand-in", "u-bogdan")}\"]"],
      [for j in output.job_names : "module.cloud_run_jobs.google_cloud_run_v2_job.this[\"${replace(j, "w9-stand-in", "u-bogdan")}\"]" if strcontains(j, "w9-stand-in")],
      [for n in output.scheduler_jobs : "module.scheduler.google_cloud_scheduler_job.${replace(trimsuffix(trimprefix(n, "swarm-"), "-w9-stand-in"), "-", "_")}[\"u-bogdan\"]" if strcontains(n, "w9-stand-in")],
    )) == 14
    error_message = "a tenant shaped like u-bogdan no longer gets the 14 output-visible instances (account, tenant document, 2 pools, 1 secret, 4 jobs, 5 scheduler jobs); a per-tenant resource changed, so re-read the set and terraform/infra/removed.tf together"
  }

  assert {
    condition = alltrue([
      for a in concat(
        [for t in keys(output.tenant_service_accounts) : "module.tenancy.google_service_account.worker[\"u-bogdan\"]" if t == "w9-stand-in"],
        [for t in keys(output.tenant_namespaces) : "module.firestore.google_firestore_document.tenant[\"u-bogdan\"]" if t == "w9-stand-in"],
        [for p in output.pool_names : "module.firestore.google_firestore_document.pool[\"${replace(p, "w9-stand-in", "u-bogdan")}\"]" if strcontains(p, "w9-stand-in")],
        [for id in values(output.tenant_secret_ids["w9-stand-in"]) : "module.secret_manager.google_secret_manager_secret.this[\"${replace(id, "w9-stand-in", "u-bogdan")}\"]"],
        [for j in output.job_names : "module.cloud_run_jobs.google_cloud_run_v2_job.this[\"${replace(j, "w9-stand-in", "u-bogdan")}\"]" if strcontains(j, "w9-stand-in")],
        [for n in output.scheduler_jobs : "module.scheduler.google_cloud_scheduler_job.${replace(trimsuffix(trimprefix(n, "swarm-"), "-w9-stand-in"), "-", "_")}[\"u-bogdan\"]" if strcontains(n, "w9-stand-in")],
      ) : contains([for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]], a)
    ])
    error_message = "an instance terraform/infra makes for a tenant shaped like u-bogdan is not the `from` of a `moved` in terraform/infra/removed.tf: the release would DESTROY it"
  }

  # Every `moved` is u-bogdan's, and there are as many as were planned for it.
  assert {
    condition     = length([for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]]) == 33 && alltrue([for a in [for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]] : strcontains(a, "u-bogdan")]) && contains(keys(output.tenant_namespaces), "w9-stand-in")
    error_message = "terraform/infra/removed.tf does not move exactly u-bogdan's 33 instances: an extra one forgets another tenant's resource, a missing one is destroyed"
  }

  # Each `moved` lands on an address a `removed` forgets, and every `removed`
  # is `destroy = false`. A target no `removed` names is planned for destroy.
  assert {
    condition = (
      toset([for m in regexall("(?m)^\\s*to\\s*=\\s*(\\S+)\\s*$", file("../../terraform/infra/removed.tf")) : m[0]]) == toset([for m in regexall("(?ms)^removed \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]]) &&
      length(regexall("(?m)^removed \\{", file("../../terraform/infra/removed.tf"))) == 33 &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*false\\s*$", file("../../terraform/infra/removed.tf"))) == 33 &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*true", file("../../terraform/infra/removed.tf"))) == 0 &&
      length(output.tenant_namespaces) > 0
    )
    error_message = "terraform/infra/removed.tf holds a `moved` whose target no `removed` forgets, or a `removed` that is not `destroy = false`: u-bogdan's resources are forgotten, never destroyed"
  }
}
