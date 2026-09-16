# CONTRACT.md invariant 9 in one file: own GSA, own secrets, own GCS prefix, own
# namespace. And invariant 10: a worker identity can create no infrastructure,
# which is enforced by the absence of any run.*, container.* or iam.* grant.

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
      providers = ["anthropic", "openai"]
    }
    "u-alice" = {
      kind      = "user"
      principal = "alice@saga.xyz"
      providers = []
    }
  }

  dispatcher_members = {
    scheduler  = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    reconciler = "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
  }
}

run "each_tenant_gets_its_own_identity_prefix_and_namespace" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  assert {
    condition     = google_service_account.worker["eng"].account_id == "swarm-agent-worker-eng"
    error_message = "the worker SA id must match swarm_common's tenant slug"
  }

  assert {
    condition     = output.gcs_prefixes["eng"] == "tenants/eng/"
    error_message = "the object prefix must match agent_worker.config.WorkerConfig.gcs_prefix"
  }

  assert {
    condition     = output.namespaces["eng"] == "swarm-tenant-eng"
    error_message = "each tenant gets its own Kubernetes namespace on Autopilot"
  }

  assert {
    condition = length(distinct([
      for t, sa in google_service_account.worker : sa.account_id
    ])) == 2
    error_message = "no two tenants may share an identity"
  }
}

run "a_tenant_can_only_reach_its_own_objects" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # Two clauses, both needed. The first scopes get/create/delete on an object
  # path; the second scopes LIST, which carries no object name -- without the
  # objectListPrefix clause a worker could enumerate every tenant's object keys.
  assert {
    condition     = strcontains(google_storage_bucket_iam_member.worker_objects["eng"].condition[0].expression, "objects/tenants/eng/")
    error_message = "object access must be scoped to the tenant's own prefix"
  }

  assert {
    condition     = strcontains(google_storage_bucket_iam_member.worker_objects["eng"].condition[0].expression, "objectListPrefix")
    error_message = "without an objectListPrefix clause a worker can list every tenant's object names"
  }

  assert {
    condition     = google_storage_bucket_iam_member.worker_objects["eng"].role == "roles/storage.objectUser"
    error_message = "objectUser, not a bucket-wide role"
  }

  # legacyBucketReader would hand over objects.list across the WHOLE bucket.
  assert {
    condition     = google_project_iam_custom_role.bucket_metadata_reader.permissions == toset(["storage.buckets.get"])
    error_message = "mounting needs bucket metadata and nothing else"
  }
}

run "a_worker_identity_can_create_no_infrastructure" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # The member's `role` interpolates the custom role's computed `.id`, so it is
  # unknown during plan. Pinning `.id` to the value the provider will produce
  # makes the grant assertable without applying anything.
  override_resource {
    target          = google_project_iam_custom_role.worker_firestore
    override_during = plan
    values = {
      id      = "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
      name    = "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
      role_id = "swarmTenantWorkerFirestore"
    }
  }

  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.worker_telemetry :
      startswith(b.role, "roles/logging.") || startswith(b.role, "roles/monitoring.") || startswith(b.role, "roles/cloudtrace.")
    ])
    error_message = "a worker's only project roles are telemetry: no run.*, container.*, compute.* or iam.*"
  }

  # Firestore IAM cannot scope below a database, so this grant is the one place
  # a worker's authority is wider than one tenant. Two things are asserted, and
  # the error messages say only what is actually true of each.
  #
  # First: the grant reaches no OTHER database in this shared project.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.worker_firestore :
      length(b.condition) == 1 && strcontains(b.condition[0].expression, "databases/swarm")
    ])
    error_message = "a worker's Firestore grant must be conditioned to the swarm database; (default) and every other team's database are out of reach"
  }

  # Second: the SHAPE of the access inside that database. The predefined
  # roles/datastore.user would add entities.delete and entities.list, which is
  # the difference between "can read and write documents it was handed ids for"
  # and "can enumerate and destroy every other tenant's tasks, leases and the
  # pool documents the whole platform admits against". Nothing in the worker
  # path deletes a document or runs a query.
  assert {
    # .id is computed and unknown during plan, so compare against role_id,
    # which comes from configuration and IS known. Same guarantee, plan-safe.
    condition = alltrue([
      for k, b in google_project_iam_member.worker_firestore :
      b.role == "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
    ])
    error_message = "the worker Firestore grant must be the narrowed custom role, not predefined roles/datastore.user"
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.worker_firestore.permissions, "datastore.entities.delete")
    error_message = "entities.delete would let a hostile worker delete another tenant's tasks and leases, and the pool documents admission depends on"
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.worker_firestore.permissions, "datastore.entities.list")
    error_message = "entities.list is queries: without it a document can only be fetched by an id already known, so another tenant's work cannot be enumerated"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.worker_firestore.permissions :
      startswith(p, "datastore.") || p == "resourcemanager.projects.get"
    ])
    error_message = "the worker's Firestore role covers Firestore and nothing else"
  }

  # actAs is granted ON the individual service account, never project-wide, and
  # only to the two identities that create Job resources.
  assert {
    condition     = length(google_service_account_iam_member.act_as) == 4
    error_message = "two dispatchers x two tenants, each granted on that tenant's SA alone"
  }

  assert {
    condition = alltrue([
      for k, b in google_service_account_iam_member.act_as :
      b.role == "roles/iam.serviceAccountUser"
    ])
    error_message = "dispatch needs serviceAccountUser on the SA it names and nothing more"
  }

  # The namespace is part of the Workload Identity principal, so a Pod in
  # another tenant's namespace cannot impersonate this GSA even if it guesses
  # the KSA name.
  assert {
    condition     = google_service_account_iam_member.workload_identity["eng"].member == "serviceAccount:saga-agents-staging.svc.id.goog[swarm-tenant-eng/swarm-agent-worker]"
    error_message = "the Workload Identity binding must pin both the namespace and the KSA"
  }
}

run "secret_inputs_only_cover_tenants_that_registered_a_provider" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # A tenant with no provider key gets no secret. A runner that needs one parks
  # as CREDENTIAL_MISSING, which costs nothing, rather than failing at start.
  assert {
    condition     = contains(keys(output.secret_inputs), "eng") && !contains(keys(output.secret_inputs), "u-alice")
    error_message = "only tenants with declared providers get secrets"
  }

  assert {
    condition     = output.secret_inputs["eng"].providers == tolist(["anthropic", "openai"])
    error_message = "the tenant's declared providers must reach the secret module"
  }
}

run "a_tenant_id_too_long_for_a_service_account_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    tenants = {
      # swarm-agent-worker-<tenant> must fit in 30 characters.
      "engineering-platform" = { kind = "group", principal = "engineering-platform@saga.xyz" }
    }
  }

  expect_failures = [var.tenants]
}

run "a_personal_tenant_must_be_namespaced" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    tenants = {
      # Without the u- prefix a personal tenant could collide with a group.
      alice = { kind = "user", principal = "alice@saga.xyz" }
    }
  }

  expect_failures = [var.tenants]
}
