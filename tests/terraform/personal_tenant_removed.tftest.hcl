# The owner's personal tenant leaves Terraform with a plan that destroys nothing (lane W9 of
# #847, 2026-10-09; docs/workspaces.md §3.3).
#
# W9 takes the owner's personal tenant out of terraform/environments/dev/
# dev.tfvars and adds a `moved` + `removed { lifecycle { destroy = false } }`
# pair for each of its instances, in terraform/infra/removed.tf and
# terraform/bootstrap/removed.tf. These runs hold that to four things:
#
#   1. the tenants dev.tfvars names no longer include the personal tenant, and the
#      bootstrap plan over that file makes no grant for it -- while eng's are
#      still planned (the control: a parse that read nothing would also make
#      no grant for the personal tenant);
#   2. terraform/infra over the same tenants plans no instance of the personal tenant in any
#      per-tenant resource, while eng's are planned;
#   3. the instances each root makes for a tenant shaped like the personal tenant are
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
# add, 0 to change and 0 to destroy for the personal tenant (docs/workspaces.md §3.3, the
# operator steps). What these runs prove is that the set of forgets is
# complete and that none of them destroys.

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
  project_id  = "example-project"
  environment = "dev"

  # terraform/infra refuses to plan an image with no digest; these only have to
  # pass its validations (personal_workspaces_absent.tftest.hcl has the same).
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

  frontend_iap_members = ["domain:example.com"]

  # The personal tenant's id, which the `moved` and `removed` addresses in
  # both removed.tf files still carry: state holds it until the operator
  # drops those blocks, and the runs below read it only from here.
  removed_tenant = "u-bogdan"
}

# 1. dev.tfvars as committed, read by bootstrap's own parse (the default
# infra_tenants_tfvars), so this is the plan the owner's apply would make.
run "dev_tfvars_names_no_personal_tenant_and_bootstrap_grants_it_nothing" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    # deployer_admin is made only with the release deployer's WIF on, as the
    # owner's bootstrap apply has it (deployer_sa_admin.tftest.hcl).
    enable_github_wif       = true
    github_repository       = "example-org/example-repo"
    enable_forge_user_slots = true
  }

  assert {
    condition     = !contains(local.infra_tenant_ids, var.removed_tenant)
    error_message = "terraform/environments/dev/dev.tfvars still names the personal tenant; W9 moved it out of Terraform (docs/workspaces.md §3.3)"
  }

  assert {
    condition     = toset(local.infra_tenant_ids) == toset(["eng", "smoke", "u-sw-c90291", "u-or-9a0792"])
    error_message = "dev.tfvars' tenants are not eng, smoke, u-sw-c90291 and u-or-9a0792: update this run and the infra run below together, so the infra plan is still the one dev.tfvars makes (less u-or-9a0792, whose id the stand-in takes)"
  }

  assert {
    condition = length(flatten([
      [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${k}\"]" if strcontains(k, var.removed_tenant)],
      [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${k}\"]" if strcontains(k, var.removed_tenant)],
      [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${k}\"]" if strcontains(k, var.removed_tenant)],
      [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${k}\"]" if strcontains(k, var.removed_tenant)],
    ])) == 0
    error_message = "the bootstrap plans a grant for the personal tenant; its grants are forgotten by terraform/bootstrap/removed.tf, never made again"
  }

  # The control: the same resources still plan eng's.
  assert {
    condition = alltrue([
      contains(keys(google_service_account_iam_member.deployer_admin), "swarm-agent-worker-eng"),
      contains(keys(google_project_iam_member.forge_slot_version_adder), "eng"),
      contains(keys(google_project_iam_member.forge_slot_version_manager), "eng"),
      contains(keys(google_project_iam_member.forge_refresh_reader), "eng"),
    ])
    error_message = "the bootstrap no longer plans eng's grants, so the personal-tenant assertion above proves nothing"
  }
}

# 3 and 4, bootstrap: the tenants block as it was, with a stand-in for the personal tenant.
run "bootstrap_forgets_every_grant_it_made_for_the_personal_tenant" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    # deployer_admin is made only with the release deployer's WIF on, as the
    # owner's bootstrap apply has it (deployer_sa_admin.tftest.hcl).
    enable_github_wif       = true
    github_repository       = "example-org/example-repo"
    enable_forge_user_slots = true
    infra_tenants_tfvars    = "../../tests/terraform/personal_tenant_removed_fixture/dev_before_w9.tfvars"
  }

  assert {
    condition = length(flatten([
      [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
    ])) == 5
    error_message = "a tenant shaped like the personal tenant no longer gets 5 instances in bootstrap; a per-tenant resource was added or removed, so re-read the set and terraform/bootstrap/removed.tf together"
  }

  # Exactly: a missing `moved` leaves an instance to be DESTROYED; an extra one
  # forgets a resource that is not the personal tenant's.
  assert {
    condition = toset([for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/bootstrap/removed.tf")) : m[0]]) == toset(flatten([
      [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
    ]))
    error_message = "the `moved` blocks of terraform/bootstrap/removed.tf are not exactly the instances bootstrap makes for a tenant shaped like the personal tenant: a missing one would be DESTROYED, an extra one forgets another resource"
  }

  # Each `moved` lands on an address a `removed` forgets, and every `removed`
  # is `destroy = false`. A target no `removed` names is planned for destroy.
  # The only `removed` blocks no `moved` lands on are the five platform
  # service-account addresses terraform/bootstrap took over (DAYONE-BOOT-2):
  # 33 for the personal tenant plus those 5.
  assert {
    condition = (
      toset([for m in regexall("(?m)^\\s*to\\s*=\\s*(\\S+)\\s*$", file("../../terraform/bootstrap/removed.tf")) : m[0]]) == toset([for m in regexall("(?ms)^removed \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/bootstrap/removed.tf")) : m[0]]) &&
      length(regexall("(?m)^removed \\{", file("../../terraform/bootstrap/removed.tf"))) == length(flatten([
        [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      ])) &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*false\\s*$", file("../../terraform/bootstrap/removed.tf"))) == length(flatten([
        [for k in keys(google_service_account_iam_member.deployer_admin) : "google_service_account_iam_member.deployer_admin[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_adder) : "google_project_iam_member.forge_slot_version_adder[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_version_manager) : "google_project_iam_member.forge_slot_version_manager[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_refresh_reader) : "google_project_iam_member.forge_refresh_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
        [for k in keys(google_project_iam_member.forge_slot_reader) : "google_project_iam_member.forge_slot_reader[\"${replace(k, "w9-stand-in", var.removed_tenant)}\"]" if strcontains(k, "w9-stand-in")],
      ])) &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*true", file("../../terraform/bootstrap/removed.tf"))) == 0
    )
    error_message = "terraform/bootstrap/removed.tf holds a `moved` whose target no `removed` forgets, or a `removed` that is not `destroy = false`: the personal tenant's resources are forgotten, never destroyed"
  }
}

# 2. terraform/infra over dev.tfvars' tenants. The map is written out because
# terraform cannot decode a tfvars file; the first run holds its keys to the
# file's.
run "infra_plans_nothing_for_the_personal_tenant_and_still_plans_eng" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind             = "group"
        principal        = "eng@example.com"
        directory_group  = true
        display_name     = "Engineering"
        providers        = ["anthropic", "openai"]
        max_active       = 45
        capacity_units   = 45
        service_accounts = ["swarm-ci-fix@example-project.iam.gserviceaccount.com"]
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
        principal       = "swarm-verify@example-project.iam.gserviceaccount.com"
        display_name    = "Verification gate"
        directory_group = false
        providers       = []
        max_active      = 4
        capacity_units  = 8
      }
      u-or-9a0792 = {
        kind            = "user"
        principal       = "oracle-api@example-project.iam.gserviceaccount.com"
        display_name    = "Oracle API"
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
      [for t in keys(output.tenant_service_accounts) : strcontains(t, var.removed_tenant)],
      [for t in keys(output.tenant_namespaces) : strcontains(t, var.removed_tenant)],
      [for t, ids in output.tenant_secret_ids : strcontains(t, var.removed_tenant) || anytrue([for id in values(ids) : strcontains(id, var.removed_tenant)])],
      [for j in output.job_names : strcontains(j, var.removed_tenant)],
      [for p in output.pool_names : strcontains(p, var.removed_tenant)],
      [for n in output.scheduler_jobs : strcontains(n, var.removed_tenant)],
    ))
    error_message = "terraform/infra plans a resource for the personal tenant; it left var.tenants in W9 and its resources are forgotten by terraform/infra/removed.tf"
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
      contains(output.scheduler_jobs, "swarm-issue-run-advance-eng"),
    ])
    error_message = "terraform/infra no longer plans eng's resources, so the personal-tenant assertion above proves nothing"
  }

  assert {
    condition     = toset(keys(output.tenant_namespaces)) == toset(["eng", "smoke", "u-sw-c90291", "u-or-9a0792"])
    error_message = "the tenants planned are not dev.tfvars' four"
  }
}

# 3 and 4, infra: dev's tenants, with a stand-in for the personal tenant under u-or-9a0792's id.
run "infra_forgets_every_instance_it_made_for_the_personal_tenant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind             = "group"
        principal        = "eng@example.com"
        directory_group  = true
        display_name     = "Engineering"
        providers        = ["anthropic", "openai"]
        max_active       = 45
        capacity_units   = 45
        service_accounts = ["swarm-ci-fix@example-project.iam.gserviceaccount.com"]
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
        principal       = "swarm-verify@example-project.iam.gserviceaccount.com"
        display_name    = "Verification gate"
        directory_group = false
        providers       = []
        max_active      = 4
        capacity_units  = 8
      }
      # the personal tenant as it was in dev.tfvars, under another id and with
      # kind = "group": terraform/infra refuses a person in this map
      # (variables.tf; docs/workspaces.md §3.2 rule 3), and a plan that
      # declared the personal tenant's instances while removed.tf moves them fails
      # ("Moved object still exists"). Every address the map makes is keyed by
      # the tenant id, so the stand-in's addresses with its id replaced are
      # the personal tenant's.
      #
      # The id is u-or-9a0792's, and that tenant is left out of this run:
      # var.tenants is frozen to dev's four keys (tenants_frozen.tftest.hcl),
      # so a fifth id is refused. u-or-9a0792 is a service tenant with no
      # provider, shaped exactly like u-sw-c90291, which stays.
      u-or-9a0792 = {
        kind           = "group"
        principal      = "w9-stand-in@example.com"
        display_name   = "Personal tenant stand-in"
        providers      = ["anthropic"]
        max_active     = 80
        capacity_units = 80
      }
    }
  }

  # A per-tenant instance added AFTER the personal tenant left (W9, 2026-10-09) never
  # existed for it, so there is nothing to forget and it is not counted:
  # stranded_pr_sweep (part of #295, 2026-10-10). Add such a job to the
  # `contains([...], n)` lists below, never a `moved` for an instance that was
  # never created.
  # A per-tenant instance DELETED after the personal tenant left is not counted either,
  # but its `moved` and `removed` stay in removed.tf: the instance did exist
  # for the personal tenant, and state may still hold it. issue_sweep (the issue
  # sweeper's job, deleted 2026-10-12, Schedules S9 second change) is the one
  # so far, which is why 13 of the 14 instances W9 counted are still made.
  # A test cannot address a child module's resources, so the instances are
  # read through the root's outputs: the account, the tenant document, the
  # pools, the secrets, the jobs and the scheduler jobs -- 13 of the 33. Each
  # must be the `from` of a `moved`, or the release would DESTROY it. The other
  # 19 (grants and bindings, keyed the same way) were read from a verbose mock
  # plan of dev.tfvars with and without the personal tenant on 2026-10-09; the release
  # plan reading 0 to destroy is their proof (docs/workspaces.md §3.3).
  assert {
    condition = length(concat(
      [for t in keys(output.tenant_service_accounts) : "module.tenancy.google_service_account.worker[\"${var.removed_tenant}\"]" if t == "u-or-9a0792"],
      [for t in keys(output.tenant_namespaces) : "module.firestore.google_firestore_document.tenant[\"${var.removed_tenant}\"]" if t == "u-or-9a0792"],
      [for p in output.pool_names : "module.firestore.google_firestore_document.pool[\"${replace(p, "u-or-9a0792", var.removed_tenant)}\"]" if strcontains(p, "u-or-9a0792")],
      [for id in values(output.tenant_secret_ids["u-or-9a0792"]) : "module.secret_manager.google_secret_manager_secret.this[\"${replace(id, "u-or-9a0792", var.removed_tenant)}\"]"],
      [for j in output.job_names : "module.cloud_run_jobs.google_cloud_run_v2_job.this[\"${replace(j, "u-or-9a0792", var.removed_tenant)}\"]" if strcontains(j, "u-or-9a0792")],
      [for n in output.scheduler_jobs : "module.scheduler.google_cloud_scheduler_job.${replace(trimsuffix(trimprefix(n, "swarm-"), "-u-or-9a0792"), "-", "_")}[\"${var.removed_tenant}\"]" if strcontains(n, "u-or-9a0792") && !contains(["swarm-stranded-pr-sweep-u-or-9a0792"], n)],
    )) == 13
    error_message = "a tenant shaped like the personal tenant no longer gets the 13 output-visible instances (account, tenant document, 2 pools, 1 secret, 4 jobs, 4 scheduler jobs); a per-tenant resource changed, so re-read the set and terraform/infra/removed.tf together"
  }

  assert {
    condition = alltrue([
      for a in concat(
        [for t in keys(output.tenant_service_accounts) : "module.tenancy.google_service_account.worker[\"${var.removed_tenant}\"]" if t == "u-or-9a0792"],
        [for t in keys(output.tenant_namespaces) : "module.firestore.google_firestore_document.tenant[\"${var.removed_tenant}\"]" if t == "u-or-9a0792"],
        [for p in output.pool_names : "module.firestore.google_firestore_document.pool[\"${replace(p, "u-or-9a0792", var.removed_tenant)}\"]" if strcontains(p, "u-or-9a0792")],
        [for id in values(output.tenant_secret_ids["u-or-9a0792"]) : "module.secret_manager.google_secret_manager_secret.this[\"${replace(id, "u-or-9a0792", var.removed_tenant)}\"]"],
        [for j in output.job_names : "module.cloud_run_jobs.google_cloud_run_v2_job.this[\"${replace(j, "u-or-9a0792", var.removed_tenant)}\"]" if strcontains(j, "u-or-9a0792")],
        [for n in output.scheduler_jobs : "module.scheduler.google_cloud_scheduler_job.${replace(trimsuffix(trimprefix(n, "swarm-"), "-u-or-9a0792"), "-", "_")}[\"${var.removed_tenant}\"]" if strcontains(n, "u-or-9a0792") && !contains(["swarm-stranded-pr-sweep-u-or-9a0792"], n)],
      ) : contains([for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]], a)
    ])
    error_message = "an instance terraform/infra makes for a tenant shaped like the personal tenant is not the `from` of a `moved` in terraform/infra/removed.tf: the release would DESTROY it"
  }

  # Every `moved` is the personal tenant's, and there are as many as were planned for it.
  assert {
    condition     = length([for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]]) == 33 && alltrue([for a in [for m in regexall("(?ms)^moved \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]] : strcontains(a, var.removed_tenant)]) && contains(keys(output.tenant_namespaces), "u-or-9a0792")
    error_message = "terraform/infra/removed.tf does not move exactly the personal tenant's 33 instances: an extra one forgets another tenant's resource, a missing one is destroyed"
  }

  # Each `moved` lands on an address a `removed` forgets, and every `removed`
  # is `destroy = false`. A target no `removed` names is planned for destroy.
  # The only `removed` blocks no `moved` lands on are the five platform
  # service-account addresses terraform/bootstrap took over (DAYONE-BOOT-2):
  # 33 for the personal tenant plus those 5.
  assert {
    condition = (
      setunion(
        toset([for m in regexall("(?m)^\\s*to\\s*=\\s*(\\S+)\\s*$", file("../../terraform/infra/removed.tf")) : m[0]]),
        toset([
          "module.iam.google_service_account.platform",
          "module.iam.google_service_account.tick",
          "google_service_account.verify",
          "module.scheduler.google_service_account.schedule_tick",
          "module.scheduler.google_service_account.rollup_sweeper",
        ]),
      ) == toset([for m in regexall("(?ms)^removed \\{\\s*from\\s*=\\s*(\\S+)", file("../../terraform/infra/removed.tf")) : m[0]]) &&
      length(regexall("(?m)^removed \\{", file("../../terraform/infra/removed.tf"))) == 38 &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*false\\s*$", file("../../terraform/infra/removed.tf"))) == 38 &&
      length(regexall("(?m)^\\s*destroy\\s*=\\s*true", file("../../terraform/infra/removed.tf"))) == 0 &&
      length(output.tenant_namespaces) > 0
    )
    error_message = "terraform/infra/removed.tf holds a `moved` whose target no `removed` forgets, or a `removed` that is not `destroy = false`: the personal tenant's resources are forgotten, never destroyed"
  }
}
