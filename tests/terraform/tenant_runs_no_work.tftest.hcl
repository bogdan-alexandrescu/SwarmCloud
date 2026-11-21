# A tenant that runs no work may go without swarmTenantWorkerFirestore before
# its state switch (owner decision 2026-11-21: reuse the existing
# worker-Firestore switch rather than add one).
#
# The roles and join acceptance probes' workspaces exist only to test
# workspace roles and join requests; they never run agents, so their workers
# never read or write control-plane state and need no project-wide Firestore
# role. var.tenants' `runs_no_work` marks such a tenant, and the three
# worker-Firestore switches (worker_firestore_access, worker_firestore_off_tenants_iam,
# worker_firestore_off_tenants) accept it without state_via_api_tenants.
#
# These runs hold, against the mock provider:
#   * the marker defaults false, so every existing tenant is unchanged;
#   * the marker alone removes nothing: a marked tenant keeps its binding
#     until a switch says otherwise;
#   * a marked tenant in worker_firestore_off_tenants_iam plans no binding for
#     its worker, validates without being in state_via_api_tenants, and the
#     unmarked default tenant beside it keeps its binding;
#   * an unmarked tenant that is not in state_via_api_tenants is still refused,
#     by each of the three switches.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "example-project"
  environment = "dev"

  # dev.tfvars' two Terraform tenants. u-pat is not one (forgotten by lane
  # W9): its worker is a job-made workspace's, switched by swarm-api's list.
  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
    }
    smoke = {
      kind            = "group"
      principal       = "smoke@example.com"
      directory_group = false
    }
  }

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

run "the_marker_defaults_false_for_every_tenant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    # MUTATION: default runs_no_work to true in terraform/infra's var.tenants
    # and this is red: every tenant could lose its grant without its state
    # switch, and a worker still on direct Firestore fails its first read.
    condition     = alltrue([for t, cfg in var.tenants : cfg.runs_no_work == false])
    error_message = "runs_no_work must default to false: no existing tenant is a no-work tenant until the owner marks it"
  }
}

run "the_marker_alone_removes_no_binding" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    artifact_bucket        = "swarm-artifacts-example-project"
    workload_identity_pool = "example-project.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }
    dispatcher_members = {
      scheduler = "serviceAccount:swarm-scheduler@example-project.iam.gserviceaccount.com"
    }
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind         = "group"
        principal    = "smoke@example.com"
        runs_no_work = true
      }
    }
  }

  assert {
    # MUTATION: filter worker_firestore's for_each on runs_no_work and this is
    # red: marking a tenant would destroy a live grant with no switch flipped.
    condition = {
      for k, b in google_project_iam_member.worker_firestore : k => [b.role, length(b.condition)]
      } == {
      eng   = ["projects/example-project/roles/swarmTenantWorkerFirestore", 0]
      smoke = ["projects/example-project/roles/swarmTenantWorkerFirestore", 0]
    }
    error_message = "runs_no_work alone must remove nothing: both tenants keep their unconditioned swarmTenantWorkerFirestore binding, keyed by tenant id"
  }
}

run "a_no_work_tenant_with_firestore_off_has_no_binding_and_the_default_tenant_keeps_its" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    artifact_bucket        = "swarm-artifacts-example-project"
    workload_identity_pool = "example-project.svc.id.goog"
    labels                 = { "managed-by" = "swarm-terraform" }
    dispatcher_members = {
      scheduler = "serviceAccount:swarm-scheduler@example-project.iam.gserviceaccount.com"
    }
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind         = "group"
        principal    = "smoke@example.com"
        runs_no_work = true
      }
    }
    worker_firestore_access          = true
    worker_firestore_off_tenants_iam = ["smoke"]
  }

  assert {
    # MUTATION: keep smoke's binding in the for_each, or drop eng's with it,
    # and this is red.
    condition = {
      for k, b in google_project_iam_member.worker_firestore : k => [b.role, length(b.condition)]
      } == {
      eng = ["projects/example-project/roles/swarmTenantWorkerFirestore", 0]
    }
    error_message = "the no-work tenant (smoke) must have no worker_firestore binding, and the default tenant (eng) must keep its own"
  }

  assert {
    # The switch removes the Firestore binding alone: the no-work tenant's
    # worker account and its object grant stay.
    condition = (
      toset(keys(google_service_account.worker)) == toset(["eng", "smoke"]) &&
      toset(keys(google_storage_bucket_iam_member.worker_objects_write)) == toset(["eng", "smoke"])
    )
    error_message = "worker_firestore_off_tenants_iam must remove the Firestore binding alone: both worker accounts and object grants stay"
  }
}

run "a_no_work_tenant_validates_off_without_its_state_switch" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@example.com"
        directory_group = false
        runs_no_work    = true
      }
    }
    worker_firestore_off_tenants_iam = ["smoke"]
    worker_firestore_off_tenants     = ["smoke"]
  }

  assert {
    # MUTATION: keep the state_via_api_tenants-only subset validation on
    # worker_firestore_off_tenants_iam or worker_firestore_off_tenants, and
    # this run fails at plan instead of reaching here.
    condition     = length(var.state_via_api_tenants) == 0 && var.tenants["smoke"].runs_no_work
    error_message = "a tenant marked runs_no_work must plan with worker Firestore off while state_via_api_tenants is empty"
  }

  assert {
    # The switch reaches swarm-api's job-made list as typed.
    condition     = local.service_env["swarm-api"].WORKER_FIRESTORE_OFF_TENANTS == "smoke"
    error_message = "worker_firestore_off_tenants = [\"smoke\"] must render WORKER_FIRESTORE_OFF_TENANTS = \"smoke\""
  }
}

run "the_global_switch_off_plans_when_every_tenant_is_switched_or_runs_no_work" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_child_tasks    = true
    state_via_api_tenants = ["eng"]
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@example.com"
        directory_group = false
        runs_no_work    = true
      }
    }
    worker_firestore_access = false
  }

  assert {
    condition     = var.worker_firestore_access == false
    error_message = "worker_firestore_access = false must plan when eng is in state_via_api_tenants and smoke runs no work"
  }
}

# The refusals hold for a tenant that runs work: the marker on one tenant
# relaxes nothing for another.
run "a_work_running_tenant_without_its_state_switch_is_still_refused_by_the_iam_list" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@example.com"
        directory_group = false
        runs_no_work    = true
      }
    }
    worker_firestore_off_tenants_iam = ["smoke", "eng"]
  }

  # MUTATION: accept any entry once some tenant is marked runs_no_work, or
  # drop the subset validation, and this plans: eng's binding is destroyed
  # while eng's worker is still dispatched onto direct Firestore.
  expect_failures = [var.worker_firestore_off_tenants_iam]
}

run "a_work_running_tenant_without_its_state_switch_is_still_refused_by_the_job_made_list" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@example.com"
        directory_group = false
        runs_no_work    = true
      }
    }
    # u-pat is no Terraform tenant, so it has no marker to read: still refused.
    worker_firestore_off_tenants = ["smoke", "u-pat"]
  }

  expect_failures = [var.worker_firestore_off_tenants]
}

run "the_global_switch_off_is_still_refused_while_a_work_running_tenant_is_unswitched" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@example.com"
      }
      smoke = {
        kind            = "group"
        principal       = "smoke@example.com"
        directory_group = false
        runs_no_work    = true
      }
    }
    worker_firestore_access = false
  }

  # MUTATION: accept the global switch off once any tenant runs no work and
  # this plans: eng's worker loses the role it still reads its state with.
  expect_failures = [var.worker_firestore_access]
}
