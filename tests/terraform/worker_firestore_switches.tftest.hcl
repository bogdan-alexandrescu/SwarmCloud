# The two worker-Firestore switches reach what they switch (lane ISO-IAM-WIRE,
# part of #1457; docs/runbooks/isolation-cutover.md).
#
# #1457 design B takes every tenant worker off direct Firestore. Two switches
# then remove the worker's swarmTenantWorkerFirestore grant, and before this
# lane neither could be used from an environment's tfvars:
#
#   * worker_firestore_access, the tenancy module's switch for a Terraform
#     tenant (eng, smoke): terraform/infra had no such variable and its
#     `module "tenancy"` call never passed one, so the module's default held
#     whatever a tfvars said;
#   * WORKER_FIRESTORE_OFF_TENANTS, swarm-api's list for a job-made workspace
#     (u-bogdan, docs/workspaces.md §2.7): nothing rendered it, so swarm-api
#     always read an empty list.
#
# These runs hold four things. The defaults render today exactly: one binding
# per tenant, the same role and member and no condition, and an empty list,
# so adding the variables plans no change anywhere. Each switch reaches what it
# switches: false removes every module.tenancy worker_firestore binding, and
# the list renders onto swarm-api alone. A switch flipped ahead of the state
# switch (state_via_api_tenants) is refused at plan, because a worker that
# loses the role while still on the direct path fails its first read. And no
# list entry is `*` or a pattern: every tenant is cut over by name.
#
# A test cannot address a child module's resources from the root, so the
# root's half is read twice: the call's text (the argument exists and names the
# variable) and a module run per value (what the module then makes).

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  # dev.tfvars' two Terraform tenants. u-bogdan is not one (removed.tf, lane
  # W9): its worker is a job-made workspace's, switched by the list.
  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@saga.xyz"
    }
    smoke = {
      kind            = "group"
      principal       = "smoke@saga.xyz"
      directory_group = false
    }
  }

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

run "the_root_defaults_switch_nothing" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    # MUTATION: default terraform/infra's worker_firestore_access to false
    # (or delete the variable) and this is red: every tenant's grant goes in
    # the next release, ahead of the cutover.
    condition     = var.worker_firestore_access == true
    error_message = "terraform/infra's worker_firestore_access must default to true: today's grant, so adding it plans no change"
  }

  assert {
    # MUTATION: delete `worker_firestore_access = var.worker_firestore_access`
    # from the `module "tenancy"` call in terraform/infra/main.tf, or pass it a
    # literal, and this is red: the module's own default would hold whatever
    # the tfvars said.
    # The module's own output is the control: the call is planned at all.
    condition = contains(keys(module.tenancy.namespaces), "eng") && length(regexall(
      "(?m)^\\s*worker_firestore_access\\s*=\\s*var\\.worker_firestore_access\\s*$",
      one(regexall("(?ms)^module \"tenancy\" \\{.*?^\\}", file("../../terraform/infra/main.tf")))
    )) == 1
    error_message = "terraform/infra's module \"tenancy\" call must pass worker_firestore_access = var.worker_firestore_access, once"
  }

  assert {
    # MUTATION: default worker_firestore_off_tenants to anything but [] and
    # this names a job-made tenant nobody cut over.
    condition     = local.service_env["swarm-api"].WORKER_FIRESTORE_OFF_TENANTS == ""
    error_message = "worker_firestore_off_tenants ships empty: WORKER_FIRESTORE_OFF_TENANTS renders \"\" on swarm-api, so every workspace keeps its grant"
  }
}

# What the module makes for the default the root now passes: today's grants
# exactly -- every key, role and member, unconditioned.
run "the_default_renders_todays_grants_exactly" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    artifact_bucket        = "swarm-artifacts-saga-agents-staging"
    workload_identity_pool = "saga-agents-staging.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }
    dispatcher_members = {
      scheduler = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    }
    worker_firestore_access = true
  }

  assert {
    # MUTATION: key the module's worker_firestore by anything but the tenant
    # id, or drop a tenant from its for_each, and every environment plans a
    # destroy-and-create of a live grant.
    condition = {
      for k, b in google_project_iam_member.worker_firestore : k => [b.role, length(b.condition)]
      } == {
      eng   = ["projects/saga-agents-staging/roles/swarmTenantWorkerFirestore", 0]
      smoke = ["projects/saga-agents-staging/roles/swarmTenantWorkerFirestore", 0]
    }
    error_message = "worker_firestore_access = true must render today's grant exactly: one unconditioned swarmTenantWorkerFirestore binding per tenant, keyed by tenant id"
  }

  # The member is the worker's email, which a mock plan does not know; what it
  # is built from is. Each tenant's binding is on that tenant's own account.
  assert {
    # MUTATION: point the binding's member at any other tenant's account (or
    # one shared account) and a tenant's grant lands on another's worker.
    condition = alltrue([
      for k, _ in google_project_iam_member.worker_firestore :
      google_service_account.worker[k].account_id == "swarm-agent-worker-${k}"
    ]) && length(regexall("member\\s*=\\s*\"serviceAccount:\\$\\{google_service_account\\.worker\\[each\\.key\\]\\.email\\}\"", one(regexall("(?ms)^resource \"google_project_iam_member\" \"worker_firestore\" \\{.*?^\\}", file("../../terraform/modules/tenancy/main.tf"))))) == 1
    error_message = "each tenant's worker_firestore binding must name that tenant's own worker, swarm-agent-worker-<tenant>"
  }
}

run "false_removes_every_tenancy_worker_firestore_binding" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    artifact_bucket        = "swarm-artifacts-saga-agents-staging"
    workload_identity_pool = "saga-agents-staging.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }
    dispatcher_members = {
      scheduler = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    }
    worker_firestore_access = false
  }

  assert {
    # MUTATION: gate the module's for_each on anything but
    # var.worker_firestore_access and a tenant keeps the role: its worker can
    # still rewrite another tenant's workspace_settings (#1457).
    condition     = length(google_project_iam_member.worker_firestore) == 0
    error_message = "worker_firestore_access = false must leave no module.tenancy worker_firestore binding for any tenant"
  }

  assert {
    condition     = toset(keys(google_service_account.worker)) == toset(["eng", "smoke"])
    error_message = "the control: both tenants' workers are still planned, so an empty binding set is the switch and not an empty tenant map"
  }
}

run "every_tenant_switched_lets_the_root_take_the_grant_away" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks           = true
    state_via_api_tenants        = ["eng", "smoke", "u-bogdan"]
    worker_firestore_off_tenants = ["u-bogdan", "eng"]
    worker_firestore_access      = false
  }

  assert {
    # MUTATION: render the list with any separator but "," and
    # swarm_api.settings reads one tenant named "u-bogdan eng".
    condition     = local.service_env["swarm-api"].WORKER_FIRESTORE_OFF_TENANTS == "u-bogdan,eng"
    error_message = "worker_firestore_off_tenants renders onto swarm-api as the comma-separated WORKER_FIRESTORE_OFF_TENANTS swarm_api.settings reads"
  }

  assert {
    # MUTATION: put the list in local.common_env and every service carries a
    # cutover list only swarm-api reads.
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "WORKER_FIRESTORE_OFF_TENANTS")
    ])
    error_message = "WORKER_FIRESTORE_OFF_TENANTS is swarm-api's alone: it writes the workspace record the job grants by"
  }
}

run "a_job_made_tenant_cut_off_before_its_state_switch_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks           = true
    state_via_api_tenants        = ["eng"]
    worker_firestore_off_tenants = ["u-bogdan"]
  }

  # MUTATION: drop the subset validation on worker_firestore_off_tenants and
  # this plans, and u-bogdan's next workspace run withholds the role from a
  # worker still dispatched onto the direct path: PermissionDenied on its
  # first read.
  expect_failures = [var.worker_firestore_off_tenants]
}

run "an_off_list_entry_that_is_not_a_tenant_id_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks           = true
    state_via_api_tenants        = ["eng"]
    worker_firestore_off_tenants = ["*"]
  }

  # MUTATION: drop the tenant-id validation and `*` plans: swarm-api reads it
  # as every tenant, the state switch cannot list `*`, and every job-made
  # workspace's next run withholds a role its unswitched worker still needs.
  expect_failures = [var.worker_firestore_off_tenants]
}

run "the_grant_cannot_go_while_a_terraform_tenant_is_unswitched" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks      = true
    state_via_api_tenants   = ["eng", "u-bogdan"]
    worker_firestore_access = false
  }

  # MUTATION: drop the validation on worker_firestore_access and this plans:
  # smoke's worker, still dispatched onto the direct path, loses the role and
  # every acceptance task fails on its first read.
  expect_failures = [var.worker_firestore_access]
}
