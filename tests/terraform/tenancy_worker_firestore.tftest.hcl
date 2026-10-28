# #1457, design B's last step, built inert: var.worker_firestore_access.
#
# A tenant's worker holding swarmTenantWorkerFirestore can read and write any
# document whose id it can guess, another tenant's workspace_settings among
# them, and no IAM condition can scope that by tenant. The fix moves every
# worker onto swarm-api's attempt-signed routes and then removes the grant.
# These runs hold both ends of the switch: by default the binding is exactly
# today's (one per tenant, same role, same member, same keys, so no environment
# plans a change); switched off, no tenant has one.

mock_provider "google" {}

variables {
  project_id             = "saga-agents-staging"
  artifact_bucket        = "swarm-artifacts-saga-agents-staging"
  workload_identity_pool = "saga-agents-staging.svc.id.goog"
  labels                 = { "managed-by" = "swarm-terraform" }

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@saga.xyz"
      providers = ["anthropic"]
    }
    "u-alice" = {
      kind      = "user"
      principal = "alice@example.com"
    }
  }

  dispatcher_members = {
    scheduler = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
  }
}

run "by_default_every_tenant_keeps_todays_firestore_binding" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  assert {
    condition     = toset(keys(google_project_iam_member.worker_firestore)) == toset(["eng", "u-alice"])
    error_message = "the default must keep one worker_firestore binding per tenant, keyed by tenant id as before, or every environment plans a destroy-and-create"
  }

  assert {
    condition     = google_project_iam_member.worker_firestore["eng"].role == "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
    error_message = "the default binding must grant the same custom role it always has"
  }
}

run "switched_off_no_tenant_holds_a_firestore_binding" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    worker_firestore_access = false
  }

  assert {
    condition     = length(google_project_iam_member.worker_firestore) == 0
    error_message = "with worker_firestore_access = false no tenant's worker may hold swarmTenantWorkerFirestore: it would still reach another tenant's workspace_settings (#1457)"
  }

  # Only the Firestore grant goes. The worker still writes its own objects and
  # telemetry, so a switch that removed more would break every attempt.
  assert {
    condition     = toset(keys(google_storage_bucket_iam_member.worker_objects_write)) == toset(["eng", "u-alice"])
    error_message = "switching Firestore access off must leave each tenant's object grant in place"
  }

  assert {
    condition     = length(google_project_iam_member.worker_telemetry) > 0
    error_message = "switching Firestore access off must leave the worker's telemetry grants in place"
  }
}
