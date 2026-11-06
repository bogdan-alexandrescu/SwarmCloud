# One Terraform tenant's worker loses swarmTenantWorkerFirestore on its own
# (lane ISO-TENANT-OFF, part of #1457; docs/runbooks/isolation-cutover.md b2).
#
# The cutover order is owner decision Q7, confirmed 2026-10-20: u-bogdan, then
# eng, then every tenant. worker_firestore_access is one switch for every
# Terraform tenant, so before this lane eng could not lose its role alone, and
# a hand removal was re-created by the next release. worker_firestore_off_tenants_iam
# is the per-tenant list: a tenant in it gets no module.tenancy worker_firestore
# binding while the global switch is still true.
#
# These runs hold, against the mock provider:
#   * the default [] renders today's bindings exactly, every key, role and
#     member, so adding the variable plans no change anywhere;
#   * ["eng"] removes eng's binding and leaves every other tenant's under the
#     same key, role and member, so the destroy touches eng alone and the
#     others are not re-keyed (a re-key is a destroy-and-create of a live
#     grant);
#   * the plan refuses an entry that is not in state_via_api_tenants (eng's
#     worker would still be dispatched onto direct Firestore and fail its first
#     read), an entry that is not a tenant id (`*`), and, in the module, an
#     entry that names no Terraform tenant (a typo would otherwise plan nothing
#     and look like a removal that happened).
#
# A test cannot address a child module's resources from the root, so the root's
# half is read as text (the call passes the variable) plus the module's runs.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  # dev.tfvars' two Terraform tenants. u-bogdan is not one (removed.tf, lane
  # W9): its worker is a job-made workspace's, switched by swarm-api's list.
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

run "the_root_default_lists_no_tenant_and_reaches_the_module" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    # MUTATION: default terraform/infra's worker_firestore_off_tenants_iam to
    # anything but [] and this is red: the next release destroys a live grant
    # nobody cut over.
    condition     = length(var.worker_firestore_off_tenants_iam) == 0
    error_message = "terraform/infra's worker_firestore_off_tenants_iam must default to []: no tenant loses its grant until the owner lists it"
  }

  assert {
    # MUTATION: delete `worker_firestore_off_tenants_iam = var.worker_firestore_off_tenants_iam`
    # from the `module "tenancy"` call in terraform/infra/main.tf, pass it a
    # literal, or pass it swarm-api's worker_firestore_off_tenants, and this
    # is red: the tfvars line the runbook's step b2 writes would remove nothing,
    # or would remove a Terraform binding on swarm-api's job-made list.
    # The module's own output is the control: the call is planned at all.
    condition = contains(keys(module.tenancy.namespaces), "eng") && length(regexall(
      "(?m)^\\s*worker_firestore_off_tenants_iam\\s*=\\s*var\\.worker_firestore_off_tenants_iam\\s*$",
      one(regexall("(?ms)^module \"tenancy\" \\{.*?^\\}", file("../../terraform/infra/main.tf")))
    )) == 1
    error_message = "terraform/infra's module \"tenancy\" call must pass worker_firestore_off_tenants_iam = var.worker_firestore_off_tenants_iam, once"
  }
}

# The module with the default: today's bindings exactly. (Plan, as every run
# here: an apply under the mock provider fails the provider's own id checks.)
run "the_default_keeps_every_tenants_binding" {
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
  }

  assert {
    # MUTATION: default the module's worker_firestore_off_tenants_iam to any
    # tenant, or key worker_firestore by anything but the tenant id, and this is
    # red: every environment plans a destroy (or a destroy-and-create) of a
    # live grant.
    condition = {
      for k, b in google_project_iam_member.worker_firestore : k => [b.role, length(b.condition)]
      } == {
      eng   = ["projects/saga-agents-staging/roles/swarmTenantWorkerFirestore", 0]
      smoke = ["projects/saga-agents-staging/roles/swarmTenantWorkerFirestore", 0]
    }
    error_message = "worker_firestore_off_tenants_iam = [] must render today's grant exactly: one unconditioned swarmTenantWorkerFirestore binding per tenant, keyed by tenant id"
  }
}

# eng alone: its binding goes, smoke's stays under the same key, role and
# member, and nothing else the module makes moves.
run "eng_listed_loses_its_binding_and_nobody_else_does" {
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
    worker_firestore_access          = true
    worker_firestore_off_tenants_iam = ["eng"]
  }

  assert {
    # MUTATION: ignore worker_firestore_off_tenants_iam in the for_each and
    # eng keeps the role: its worker can still rewrite another tenant's
    # workspace_settings (#1457) after the step that was meant to end that.
    condition     = !contains(keys(google_project_iam_member.worker_firestore), "eng")
    error_message = "a tenant in worker_firestore_off_tenants_iam must have no worker_firestore binding while worker_firestore_access is true"
  }

  assert {
    # MUTATION: key the remaining bindings by position (a list's index, or
    # count) instead of tenant id, filter the wrong way round (keep only the
    # listed tenants), or point the member at the listed tenant's account, and
    # this is red: smoke's grant is re-keyed (destroy-and-create of a live
    # grant), removed, or moved onto another tenant's worker.
    condition = {
      for k, b in google_project_iam_member.worker_firestore : k => [b.role, length(b.condition)]
      } == {
      smoke = ["projects/saga-agents-staging/roles/swarmTenantWorkerFirestore", 0]
    }
    error_message = "every tenant not in worker_firestore_off_tenants_iam keeps its binding under its own tenant-id key, with the same role and no condition"
  }

  # The member is the worker's email, which a mock plan does not know; what it
  # is built from is: the binding's own key, on that tenant's own account.
  assert {
    # MUTATION: build the member from anything but each.key (the listed
    # tenant, the first tenant, one shared account) and this is red: the
    # remaining binding lands on another tenant's worker.
    condition = alltrue([
      for k, _ in google_project_iam_member.worker_firestore :
      google_service_account.worker[k].account_id == "swarm-agent-worker-${k}"
    ]) && length(regexall("member\\s*=\\s*\"serviceAccount:\\$\\{google_service_account\\.worker\\[each\\.key\\]\\.email\\}\"", one(regexall("(?ms)^resource \"google_project_iam_member\" \"worker_firestore\" \\{.*?^\\}", file("../../terraform/modules/tenancy/main.tf"))))) == 1
    error_message = "each remaining worker_firestore binding must name that tenant's own worker, swarm-agent-worker-<tenant>"
  }

  assert {
    # MUTATION: filter var.tenants itself (or anything but the worker_firestore
    # for_each) by the list and eng's worker account, object grants and
    # telemetry go too: every eng attempt fails, not just its Firestore reads.
    condition = (
      toset(keys(google_service_account.worker)) == toset(["eng", "smoke"]) &&
      toset(keys(google_storage_bucket_iam_member.worker_objects_write)) == toset(["eng", "smoke"]) &&
      toset([for k in keys(google_project_iam_member.worker_telemetry) : split(":", k)[0]]) == toset(["eng", "smoke"]) &&
      length(google_project_iam_member.worker_telemetry) == 6
    )
    error_message = "the list removes the Firestore binding alone: both tenants' worker accounts, object grants and telemetry grants stay"
  }
}

# The module refuses an entry that names no Terraform tenant: it would plan
# nothing, and a cutover step whose plan is empty reads like one that worked.
run "the_module_refuses_an_entry_that_is_not_one_of_its_tenants" {
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
    worker_firestore_off_tenants_iam = ["egn"]
  }

  # MUTATION: drop the module's tenants-key validation and a typo plans no
  # change: eng keeps the role while the runbook's step b2 reads as done.
  expect_failures = [var.worker_firestore_off_tenants_iam]
}

# The root's positive control: eng switched first, then listed, plans.
run "eng_listed_after_its_state_switch_plans" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks               = true
    state_via_api_tenants            = ["u-bogdan", "eng"]
    worker_firestore_off_tenants_iam = ["eng"]
  }

  assert {
    # MUTATION: render the IAM list onto swarm-api's WORKER_FIRESTORE_OFF_TENANTS
    # and this is red: the two lists are distinct, and a Terraform tenant
    # listed for its binding must not change a job-made workspace record.
    condition     = local.service_env["swarm-api"].WORKER_FIRESTORE_OFF_TENANTS == ""
    error_message = "worker_firestore_off_tenants_iam is the tenancy module's list alone; swarm-api's WORKER_FIRESTORE_OFF_TENANTS stays \"\""
  }
}

run "eng_listed_before_its_state_switch_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks               = true
    state_via_api_tenants            = ["u-bogdan"]
    worker_firestore_off_tenants_iam = ["eng"]
  }

  # MUTATION: drop the state_via_api_tenants subset validation on
  # worker_firestore_off_tenants_iam and this plans: eng's binding is destroyed
  # while the scheduler still dispatches eng onto direct Firestore, and every
  # eng attempt fails PermissionDenied on its first read.
  expect_failures = [var.worker_firestore_off_tenants_iam]
}

run "a_star_entry_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks               = true
    state_via_api_tenants            = ["eng", "smoke"]
    worker_firestore_off_tenants_iam = ["*"]
  }

  # `*` is refused twice over at the root: it is not a tenant id, and
  # state_via_api_tenants (whose own validation refuses `*`) can never list it.
  # MUTATION: drop BOTH validations on worker_firestore_off_tenants_iam and
  # this is red: `*` reaches the module (whose tenants-key check is then the
  # last refusal) and, without that, plans as "nobody" today and as whatever a
  # later reader decides `*` means tomorrow. Every tenant is cut over by name.
  # Dropping the tenant-id validation alone stays green, by design.
  expect_failures = [var.worker_firestore_off_tenants_iam]
}
