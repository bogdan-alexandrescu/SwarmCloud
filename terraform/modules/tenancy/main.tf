# Per-tenant identity and isolation.
#
# CONTRACT.md invariant 9 in one module: own GSA, own secrets, own GCS prefix,
# own namespace. Everything a tenant can reach is reachable only by that
# tenant's service account.
#
# What a tenant worker CANNOT do is the more important list, and it is enforced
# by absence -- there is no run.*, container.*, compute.* or iam.* grant for a
# worker identity anywhere in this repository. A worker that is talked into
# running attacker-controlled code still cannot create infrastructure, because
# the identity it runs under has never been able to (invariant 10).

locals {
  owner_marker = "managed-by=${lookup(var.labels, "managed-by", "swarm-terraform")}"

  tenant_ids = keys(var.tenants)

  # The prefix is spelled in modules/service_account_ids, which
  # terraform/bootstrap reads to grant the release deployer
  # roles/iam.serviceAccountAdmin on each worker account (#334).
  sa_account_id = { for t, _ in var.tenants : t => "${module.service_account_ids.tenant_worker_prefix}${t}" }

  namespace = { for t, _ in var.tenants : t => "${var.namespace_prefix}${t}" }

  gcs_prefix = { for t, _ in var.tenants : t => "tenants/${t}/" }

  # (tenant, dispatcher) pairs for the actAs grant.
  #
  # Keyed by the dispatcher's LABEL, never by its member string: the member is a
  # service account email that is unknown until apply, and a for_each whose keys
  # are unknown cannot be planned -- the first plan on a fresh project fails
  # outright. Keys stay static; only the value is unknown.
  act_as_grants = {
    for pair in flatten([
      for t in local.tenant_ids : [
        for label, m in var.dispatcher_members : {
          key    = "${t}:${label}"
          tenant = t
          member = m
        }
      ]
    ]) : pair.key => pair
  }

  telemetry_roles = [
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ]

  telemetry_grants = {
    for pair in flatten([
      for t in local.tenant_ids : [
        for role in local.telemetry_roles : {
          key    = "${t}:${role}"
          tenant = t
          role   = role
        }
      ]
    ]) : pair.key => pair
  }

  # Workload Identity bindings only make sense once the cluster exists.
  wi_tenants = var.workload_identity_pool == "" ? {} : var.tenants

  # --- #295: the merge, post-verdict and review accounts -------------------
  #
  # "<tenant>:<profile>" -> {tenant, profile, account_id}, one per profile in
  # modules/service_account_ids' action_accounts whose gating provider the
  # tenant registers. Keyed by strings known at plan, like everything above.
  action_profiles = module.service_account_ids.action_accounts

  action = {
    for k, id in module.service_account_ids.action_ids : k => {
      tenant     = split(":", k)[0]
      profile    = split(":", k)[1]
      account_id = id
    }
  }

  # The one of the three that runs an agent, and so writes artifacts and
  # checkpoints of its own -- and the only identity with create under
  # tenants/<tenant>/verdicts/ (merge-step.md §4.3).
  review = { for k, a in local.action : k => a if a.profile == "claude-code-review" }

  action_telemetry_grants = {
    for pair in flatten([
      for k, a in local.action : [
        for role in local.telemetry_roles : {
          key    = "${k}:${role}"
          action = k
          role   = role
        }
      ]
    ]) : pair.key => pair
  }

  # Only the deployer may act as these accounts: it deploys their Jobs. NOT the
  # dispatcher or the reconciler, which act as every worker account. The
  # dispatcher holds run.jobs.update on every Job, and actAs on the merge
  # account would let a compromised dispatcher repoint the merge Job's image
  # and run it as the merge account -- the one identity that can read the
  # merge App's key.
  action_act_as_grants = {
    for pair in flatten([
      for k, a in local.action : [
        for label, m in var.action_act_as_members : {
          key    = "${k}:${label}"
          action = k
          member = m
        }
      ]
    ]) : pair.key => pair
  }

  # tenant -> provider -> who reads that provider's secret, BY NAME: "worker",
  # or a "<tenant>:<profile>" key of local.action. Only providers whose reader
  # list differs from the worker alone appear; outputs.tf turns the names into
  # members for the secret_manager module. Known at plan, which the emails are
  # not, so tests/terraform/merge_step_iam.tftest.hcl reads this.
  #
  # The merge account is the ONLY reader of -git-merge and the post-verdict
  # account the ONLY reader of -git-review (`sole_accessor`; contract request
  # 35's MAJOR 1 and its git-merge twin). The review account runs an agent on
  # its profile's provider, so it reads that key beside the worker
  # (`also_reads`) -- and never an App key.
  secret_readers = {
    for t, v in var.tenants : t => merge(
      {
        for k, a in local.action :
        local.action_profiles[a.profile].provider => [k]
        if a.tenant == t && local.action_profiles[a.profile].sole_accessor
      },
      {
        for p in distinct(flatten([
          for k, a in local.action : local.action_profiles[a.profile].also_reads if a.tenant == t
        ])) :
        p => concat(["worker"], [
          for k, a in local.action : k
          if a.tenant == t && contains(local.action_profiles[a.profile].also_reads, p)
        ])
        if contains(v.providers, p)
      },
    )
  }

  # --- the object conditions, spelled once ---------------------------------
  #
  # The object-path clause covers get/create/delete on an object. LIST carries
  # no object name, so the only way to scope it is the objectListPrefix
  # attribute -- without it an identity could enumerate every tenant's object
  # names. Every tenant identity reads and lists its own tenant's prefix;
  # writing is split, so that tenants/<tenant>/verdicts/ is writable by the
  # review account alone (merge-step.md §4.3, §10 item 4).
  object_prefix = {
    for t, _ in var.tenants :
    t => "resource.name.startsWith(\"projects/_/buckets/${var.artifact_bucket}/objects/tenants/${t}/\")"
  }
  list_prefix = {
    for t, _ in var.tenants :
    t => "api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"tenants/${t}/\")"
  }
  verdicts_prefix = {
    for t, _ in var.tenants :
    t => "resource.name.startsWith(\"projects/_/buckets/${var.artifact_bucket}/objects/tenants/${t}/verdicts/\")"
  }

  read_expression     = { for t, _ in var.tenants : t => join(" || ", [local.object_prefix[t], local.list_prefix[t]]) }
  write_expression    = { for t, _ in var.tenants : t => "${local.object_prefix[t]} && !${local.verdicts_prefix[t]}" }
  verdicts_expression = { for t, _ in var.tenants : t => local.verdicts_prefix[t] }
}

module "service_account_ids" {
  source = "../service_account_ids"

  tenant_providers = { for t, v in var.tenants : t => v.providers }
}

# A NEW TENANT'S ACCOUNT MUST EXIST BEFORE THE RELEASE THAT ADDS IT (#334).
# The release sets this account's IAM policy in the same apply that creates it
# (act_as and workload_identity below), and the deployer may do that only
# through a per-account roles/iam.serviceAccountAdmin grant terraform/bootstrap
# makes -- which cannot be made on an account that does not exist yet. So
# scripts/register-tenant.sh creates the account first, the owner applies
# bootstrap's grant on it (from main), and only then does the release run;
# create_ignore_already_exists adopts that account here instead of failing on a
# 409. This and the #295 accounts below are the ONLY accounts infra adopts
# (#334 security review): adopting is also how an account somebody else made
# first -- their IAM policy, their keys -- would become a tenant's identity, so
# register-tenant.sh refuses an existing
# account carrying any IAM member the platform does not make, or any
# user-managed key. docs/ci.md, "A new account exists before the release that
# adds it", has the order.
resource "google_service_account" "worker" {
  for_each = var.tenants

  project      = var.project_id
  account_id   = local.sa_account_id[each.key]
  display_name = "Swarm agent worker (${each.key})"
  description  = "${local.owner_marker}; tenant ${each.key} (${each.value.principal}). No infrastructure-creation permissions."

  create_ignore_already_exists = true
}

# THE MERGE, POST-VERDICT AND REVIEW ACCOUNTS (#295). One each per tenant that
# registers the provider that needs it: `swarm-<tenant>-merge` for git-merge,
# `swarm-<tenant>-post-verdict` and `swarm-<tenant>-review` for git-review
# (docs/merge-step.md §1.3, §10 item 4). Cloud Run sets the service account on
# the Job, so the profile is the one place a step's identity can differ from
# its tenant's other steps, and these are those identities.
#
# What isolates them is what they are NOT given: nothing grants the tenant's
# worker account actAs or tokenCreator on any of them, no tenant identity
# holds run.jobs.* on their Jobs (tests/terraform/merge_step_iam.tftest.hcl),
# and the merge and post-verdict accounts are each the SOLE accessor of their
# App key's secret (outputs.tf, secret_inputs).
#
# ADOPTED, LIKE THE WORKER ACCOUNT AND FOR THE SAME REASON (#334): the
# deployer's actAs below is a setIamPolicy on the account, which it may make
# only through terraform/bootstrap's per-account serviceAccountAdmin grant, and
# that grant needs the account to exist first. So whatever registers the
# provider creates the account before the release, under the same refusal the
# worker account has -- no IAM member the platform does not make, no
# user-managed key -- and this adopts it.
resource "google_service_account" "action" {
  for_each = local.action

  project      = var.project_id
  account_id   = each.value.account_id
  display_name = "Swarm ${each.value.profile} (${each.value.tenant})"
  description  = "${local.owner_marker}; tenant ${each.value.tenant}, runner profile ${each.value.profile} only. No infrastructure-creation permissions."

  create_ignore_already_exists = true
}

# --------------------------------------------------------------------------
# Control-plane state.
#
# Read this block together with its limits, because they are load-bearing.
#
# Firestore IAM has NO collection- or document-level granularity: the smallest
# resource a binding or a condition can name is the database. Every swarm
# identity therefore shares one authorization scope over the `swarm` database,
# and a worker running attacker-controlled code -- which is the normal case, not
# the exceptional one -- is inside that scope. The condition below keeps it out
# of `(default)` and every other database in this shared project.
#
# CORRECTION, verified live 2026-09-16: that is FALSE. Firestore does not
# evaluate IAM Conditions on the data plane -- only for administrative
# operations -- so the condition neither restricted anything nor was silently
# ignored: it DENIED document access outright, and every control-plane service
# came up with /readyz reporting "firestore unavailable: PermissionDenied".
# The condition now defaults off (see modules/iam/variables.tf), which means a
# swarm identity can reach any Firestore database in this project. Today `swarm`
# is the only one. The real boundary is a separate project.
#
# It also cannot keep tenant A's worker out of tenant B's documents, and no
# condition can -- which is why the SHAPE of the grant below carries the weight.
#
# What IS available at this layer is the shape of the access, so the role is
# built rather than borrowed: swarmTenantWorkerFirestore, which drops
# entities.delete and entities.list from what roles/datastore.user grants.
# terraform/bootstrap/platform_roles.tf defines it, with the reasoning for each
# of its five permissions, since #79 (owner decision 2026-09-25): CI no longer
# holds roles/iam.roleAdmin, so no role can be defined in a module CI applies.
#
# The residual is real and is written down here rather than implied: a worker
# still holds create/get/update across the database, so it can read a document
# whose id it can guess (`pools/global`, `tenants/<id>`) and write one. Closing
# that needs the worker off direct Firestore access entirely -- reaching state
# through a control-plane service that scopes by caller identity -- or one
# database per tenant, and neither is expressible in IAM.
#
# The role names below are plain strings from ../custom_role_ids, the one
# spelling this module and terraform/bootstrap share. A string, not a resource
# reference, so it is known at plan and nothing here reads a role: CI holds no
# iam.roles.* permission.
module "custom_role_ids" {
  source = "../custom_role_ids"

  project_id = var.project_id
}

resource "google_project_iam_member" "worker_firestore" {
  for_each = var.tenants

  project = var.project_id
  role    = module.custom_role_ids.names.worker_firestore
  member  = "serviceAccount:${google_service_account.worker[each.key].email}"

  dynamic "condition" {
    for_each = var.scope_firestore_to_database ? [1] : []
    content {
      title       = "swarm-database-only"
      description = "Restricts this grant to the swarm database."
      expression  = "resource.type == \"datastore.googleapis.com/Database\" && resource.name == \"projects/${var.project_id}/databases/${var.firestore_database}\""
    }
  }
}

resource "google_project_iam_member" "worker_telemetry" {
  for_each = local.telemetry_grants

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.worker[each.value.tenant].email}"
}

# The SAME Firestore grant as the worker's, and not a narrower one: Firestore
# has no document- or field-level IAM for a server client, so "a narrowed
# Firestore role" for the merge account is not buildable (merge-step.md §1.3,
# corrected in the joint review with CR 34). Its isolation is the storage and
# secret grants, not this.
resource "google_project_iam_member" "action_firestore" {
  for_each = local.action

  project = var.project_id
  role    = module.custom_role_ids.names.worker_firestore
  member  = "serviceAccount:${google_service_account.action[each.key].email}"

  dynamic "condition" {
    for_each = var.scope_firestore_to_database ? [1] : []
    content {
      title       = "swarm-database-only"
      description = "Restricts this grant to the swarm database."
      expression  = "resource.type == \"datastore.googleapis.com/Database\" && resource.name == \"projects/${var.project_id}/databases/${var.firestore_database}\""
    }
  }
}

resource "google_project_iam_member" "action_telemetry" {
  for_each = local.action_telemetry_grants

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.action[each.value.action].email}"
}

# --------------------------------------------------------------------------
# Object storage: one prefix per tenant, enforced by IAM rather than by habit.
# --------------------------------------------------------------------------

# Cloud Storage FUSE and the client libraries both need the bucket's own
# metadata, and a prefix condition can never match the bucket resource name.
# Granting legacyBucketReader instead would hand over objects.list across the
# WHOLE bucket, which is exactly the isolation this module exists to keep. So
# the grant is swarmBucketMetadataReader, storage.buckets.get alone, defined in
# terraform/bootstrap/platform_roles.tf (#79).
resource "google_storage_bucket_iam_member" "worker_bucket_metadata" {
  for_each = var.tenants

  bucket = var.artifact_bucket
  role   = module.custom_role_ids.names.bucket_metadata_reader
  member = "serviceAccount:${google_service_account.worker[each.key].email}"
}

resource "google_storage_bucket_iam_member" "action_bucket_metadata" {
  for_each = local.action

  bucket = var.artifact_bucket
  role   = module.custom_role_ids.names.bucket_metadata_reader
  member = "serviceAccount:${google_service_account.action[each.key].email}"
}

# THE TENANT PREFIX, SPLIT IN TWO (#295; merge-step.md §4.3, §10 item 4).
#
# This was one binding, `worker_objects`: roles/storage.objectUser on
# tenants/<tenant>/, read, list and write. Its write half now excludes
# tenants/<tenant>/verdicts/, which only the review account may create in.
# GCS IAM is allow-only, so the exclusion is not a deny rule: it is the
# write binding's condition, and the read binding beside it keeps both of the
# old clauses so read and list stay exactly as tenant-scoped as before.
#
# objectViewer, not objectUser, carries the list clause. objectUser has no
# list clause of its own, so it can list nothing: a list request names the
# bucket, which its object-path clause never matches.
resource "google_storage_bucket_iam_member" "worker_objects_read" {
  for_each = var.tenants

  bucket = var.artifact_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker[each.key].email}"

  condition {
    title       = "swarm-tenant-prefix-read-${each.key}"
    description = "Read and list objects under tenants/${each.key}/ only."
    expression  = local.read_expression[each.key]
  }
}

resource "google_storage_bucket_iam_member" "worker_objects_write" {
  for_each = var.tenants

  bucket = var.artifact_bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.worker[each.key].email}"

  condition {
    title       = "swarm-tenant-prefix-write-${each.key}"
    description = "Write objects under tenants/${each.key}/, except tenants/${each.key}/verdicts/."
    expression  = local.write_expression[each.key]
  }
}

# THE OLD BINDING IS REMOVED, AND SAID SO. A new resource does nothing for a
# tenant applied under the old one: without this the release would add the
# split beside the old objectUser grant, and the old one -- unconditioned on
# verdicts/ -- would keep every worker able to write a verdict. Destroying the
# resource removes exactly its (member, role, condition) triple from the
# bucket policy, for every tenant in state. A tenant registered by
# scripts/register-tenant.sh alone is not in this state; the script's own
# binding needs the same split (merge-step.md §10 item 5).
#
# The block also refuses a `worker_objects` resource coming back: Terraform
# rejects a removed block whose address is still declared.
removed {
  from = google_storage_bucket_iam_member.worker_objects

  lifecycle {
    destroy = true
  }
}

# Every #295 account reads its tenant's prefix. Only the review account
# writes: merge and post-verdict run no agent and write neither artifacts nor
# checkpoints (contract request 35: "no objectUser grant for its own artifacts
# or checkpoints").
resource "google_storage_bucket_iam_member" "action_objects_read" {
  for_each = local.action

  bucket = var.artifact_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.action[each.key].email}"

  condition {
    title       = "swarm-tenant-prefix-read-${each.value.tenant}"
    description = "Read and list objects under tenants/${each.value.tenant}/ only."
    expression  = local.read_expression[each.value.tenant]
  }
}

resource "google_storage_bucket_iam_member" "review_objects_write" {
  for_each = local.review

  bucket = var.artifact_bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.action[each.key].email}"

  condition {
    title       = "swarm-tenant-prefix-write-${each.value.tenant}"
    description = "Write objects under tenants/${each.value.tenant}/, except tenants/${each.value.tenant}/verdicts/."
    expression  = local.write_expression[each.value.tenant]
  }
}

# CREATE ONLY: objectCreator has no update, no delete and no get (owner
# decision, round 5). A verdict, once written, is not rewritten by the account
# that wrote it; the read above is what lets it see one. The residual is R8
# (merge-step.md §7): the agent can mint this account's token while it runs and
# create a verdict at another workflow's path inside the same tenant's
# subtree. Recorded, not closed here.
resource "google_storage_bucket_iam_member" "review_verdicts" {
  for_each = local.review

  bucket = var.artifact_bucket
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.action[each.key].email}"

  condition {
    title       = "swarm-tenant-verdicts-${each.value.tenant}"
    description = "Create objects under tenants/${each.value.tenant}/verdicts/ only."
    expression  = local.verdicts_expression[each.value.tenant]
  }
}

# --------------------------------------------------------------------------
# Dispatch: actAs, scoped to the individual service account.
# --------------------------------------------------------------------------

resource "google_service_account_iam_member" "act_as" {
  for_each = local.act_as_grants

  service_account_id = google_service_account.worker[each.value.tenant].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value.member
}

resource "google_service_account_iam_member" "action_act_as" {
  for_each = local.action_act_as_grants

  service_account_id = google_service_account.action[each.value.action].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value.member
}

# --------------------------------------------------------------------------
# Workload Identity: the tenant's KSA on Autopilot assumes the tenant's GSA.
# --------------------------------------------------------------------------

resource "google_service_account_iam_member" "workload_identity" {
  for_each = local.wi_tenants

  service_account_id = google_service_account.worker[each.key].name
  role               = "roles/iam.workloadIdentityUser"

  # Namespace is part of the principal, so a Pod in another tenant's namespace
  # cannot impersonate this GSA even if it guesses the KSA name.
  member = "serviceAccount:${var.workload_identity_pool}[${local.namespace[each.key]}/${each.value.ksa_name}]"
}
