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
  # The grant is the custom swarmBucketMetadataReader, whose one permission
  # (storage.buckets.get) platform_roles.tftest.hcl asserts: terraform/bootstrap
  # defines it since #79.
  assert {
    condition = alltrue([
      for k, b in google_storage_bucket_iam_member.worker_bucket_metadata :
      b.role == "projects/saga-agents-staging/roles/swarmBucketMetadataReader"
    ])
    error_message = "mounting needs bucket metadata and nothing else: the worker's bucket-level grant must be swarmBucketMetadataReader, not a predefined bucket role"
  }
}

run "a_worker_identity_can_create_no_infrastructure" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # The member's `role` is a plain string from terraform/modules/custom_role_ids
  # since #79, known at plan -- no override of a computed role id is needed.

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
  # First: the grant carries NO IAM condition, and that is the correct shape.
  #
  # REVERSED on evidence, 2026-09-16, exactly as the same assertion in
  # iam.tftest.hcl was. This used to demand a condition pinning the grant to
  # projects/<p>/databases/swarm. Firestore does not evaluate IAM Conditions on
  # the DATA plane -- only for administrative operations -- so the condition
  # never scoped document access: it DENIED it, and every identity carrying it
  # reported "firestore unavailable: PermissionDenied". A worker with that
  # condition cannot read the task it was dispatched for, so the platform does
  # not run at all. Demanding it back here would demand an outage, which is why
  # this asserts the absence rather than the presence.
  #
  # The database boundary this used to claim does not exist at the IAM layer at
  # all: a swarm identity can reach any Firestore database in this project.
  # Today `swarm` is the only one, and the real fix is a separate project. See
  # terraform/modules/iam/variables.tf and docs/security.md.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.worker_firestore : length(b.condition) == 0
    ])
    error_message = "a worker's Firestore grant must be UNCONDITIONED: an IAM condition is not evaluated on the data plane, so it denies document access outright instead of scoping it, and the worker cannot read its own task"
  }

  # Second: the SHAPE of the access inside that database. The predefined
  # roles/datastore.user would add entities.delete and entities.list, which is
  # the difference between "can read and write documents it was handed ids for"
  # and "can enumerate and destroy every other tenant's tasks, leases and the
  # pool documents the whole platform admits against". Nothing in the worker
  # path deletes a document or runs a query.
  # The role this grant names is swarmTenantWorkerFirestore. What it CONTAINS
  # -- no entities.delete, no entities.list, nothing outside Firestore -- is
  # asserted in platform_roles.tftest.hcl, against terraform/bootstrap, which
  # defines it since #79. This run holds that the worker is granted that role
  # and not the predefined one.
  assert {
    condition = alltrue([
      for k, b in google_project_iam_member.worker_firestore :
      b.role == "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore"
    ])
    error_message = "the worker Firestore grant must be the narrowed custom role, not predefined roles/datastore.user"
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
