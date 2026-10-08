# ---------------------------------------------------------------------------
# swarm-workspace-deployer: the identity that makes a person's workspace, the
# one Cloud Build trigger that may use it, and where its logs go
# (docs/workspaces.md §2.1-2.6, lane W4 of #847)
# ---------------------------------------------------------------------------
#
# WHAT THIS IS FOR. An admin's approval publishes {"workspace_id", "mode"} to
# the topic swarm-workspace-apply; the Cloud Build trigger of the same name
# builds main and runs scripts/cloudbuild/workspace-apply.yaml as
# swarm-workspace-deployer; the build runs `scripts/register-tenant.sh
# --workspace <w-id>` under the call guard (scripts/lib/workspace-guard.sh),
# which makes that one person's worker account, its bindings, its bucket
# prefix grants, its forge slot and its namespace (owner decisions WD2 and WD3,
# 2026-10-08).
#
# THE ACCEPTED RISK (§2.4, owner, 2026-10-08). A worker account's own IAM
# policy can only be written by an identity holding
# iam.serviceAccounts.setIamPolicy on it, and IAM cannot condition that grant
# on the account's name ("IAM resources don't provide the resource name",
# #334). So swarmWorkspaceAccountAdmin below is PROJECT-WIDE, and could set IAM
# on the other team's accounts if misused. What bounds it:
#
#   1. Nothing but the trigger can use the identity. It has no key (nothing
#      here makes one) and no WIF binding, and its own IAM policy is written
#      AUTHORITATIVELY EMPTY below -- no actAs, no token creator, no
#      workloadIdentityUser, for anyone, at the account level. Using it from a
#      build needs actAs on it, which the owner's bootstrap apply exercises to
#      create the trigger; W0 lists who else holds actAs at the project level.
#   2. The trigger builds refs/heads/main only, and reads its build file from
#      refs/heads/main. .github/CODEOWNERS names the owner on the build file,
#      the guard and the script, so what this identity does is what the owner
#      approved into main.
#   3. The builder image is pinned BY DIGEST here (workspace_apply_builder_image),
#      so a rebuilt image reaches the identity only through this root's apply.
#   4. Three alerts (terraform/modules/monitoring/workspace_alerts.tf): a change
#      by this identity to an account not named swarm-agent-worker-u-*, or a
#      grant to such a member anywhere; a build as this identity that did not
#      come from this trigger; and any change to the trigger, the topic's IAM
#      or this identity's own IAM.
#
# WD9 TOOK THE FALLBACK. W0 (2026-10-08) found that an IAM allow policy accepts
# no principal set naming only the swarm-agent-worker-u-* accounts -- the sets
# on offer are wider, and the widest would hand the database role to the other
# team's twelve accounts. So there is NO one-time principal-set grant here: the
# job grants each person's worker the database and telemetry roles itself,
# through the CONDITIONED projectIamAdmin below, whose hasOnly() admits those
# three roles and no other, and the first alert watches the members.
#
# WHY HERE AND NOT IN terraform/infra. Every grant TO a deployer comes from the
# root the owner applies, never the root CI applies to itself; and creating a
# trigger that runs as an account needs actAs on that account, which CI must
# never hold on this one.
#
# NOT VERIFIED LIVE -- each is settled by the first approved workspace, and each
# fails closed (a failed build, or an alert that pages), never open:
#
#   * that a Pub/Sub trigger maps `$(body.message.data.<field>)` of a JSON
#     message to a substitution and evaluates `filter` over it, as Google's
#     Pub/Sub-trigger page shows for Artifact Registry's messages. If it does
#     not, step 1 of the build file refuses the empty values and nothing runs;
#   * that a build's log entries carry resource.labels.build_trigger_id, which
#     the sink and the exclusion select on. If they do not, the log stays in
#     _Default (the §2.6 residual) and the restricted bucket stays empty;
#   * that Cloud Build pulls a step's image with the BUILD's identity, which is
#     why swarmImagePuller is granted on the image's repository;
#   * that IAM evaluates every condition below in the form written. The
#     secret, bucket-object and cluster forms are the ones forge_user_slots.tf,
#     modules/tenancy and modules/iam already use; hasOnly() is
#     deployer_conditions.tf's.

locals {
  workspace_deployer_on = var.enable_workspace_deployer ? 1 : 0

  # One name for the topic, the trigger, the log bucket, the sink and the
  # exclusion: what an operator searches for finds all five. The build file
  # checks $TRIGGER_NAME against this literal.
  workspace_apply_name = "swarm-workspace-apply"

  workspace_deployer_account_id = "swarm-workspace-deployer"
  workspace_deployer_email      = "${local.workspace_deployer_account_id}@${var.project_id}.iam.gserviceaccount.com"
  workspace_deployer_member     = "serviceAccount:${local.workspace_deployer_email}"

  # What the trigger builds: main, and the build file on main. Literals, and
  # held by tests/terraform/workspace_deployer.tftest.hcl: a variable here
  # would be one tfvars edit from building an unreviewed branch as this
  # identity.
  workspace_apply_ref        = "refs/heads/main"
  workspace_apply_build_file = "scripts/cloudbuild/workspace-apply.yaml"

  # The second-generation connection is regional; the trigger lives beside it.
  workspace_apply_location = var.workspace_apply_repository == "" ? var.region : split("/", var.workspace_apply_repository)[3]

  # The builder image's repository, read from its own reference, so the pull
  # grant is on exactly the repository the trigger names.
  workspace_builder_parts = regex("^(?P<location>[a-z0-9-]+)-docker\\.pkg\\.dev/(?P<project>[^/]+)/(?P<repository>[^/]+)/", var.workspace_apply_builder_image == "" ? "${var.region}-docker.pkg.dev/${var.project_id}/swarm-images/" : var.workspace_apply_builder_image)

  # Shared names the job's grants are made on, spelled as the rest of the
  # platform spells them: the artifact bucket as terraform/modules/storage
  # names it (scripts/lib/common.sh's ARTIFACT_BUCKET), the cluster as
  # terraform/infra names it, the project number for Secret Manager's
  # resource names.
  workspace_artifact_bucket = "${var.name_prefix}-artifacts-${var.project_id}"
  workspace_cluster_name    = "projects/${var.project_id}/locations/${var.region}/clusters/${var.name_prefix}-autopilot"
  workspace_project_number  = var.enable_workspace_deployer ? data.google_project.workspace_deployer[0].number : ""

  # Personal tenants' secrets, and only theirs: `swarm-tenant-u-` is
  # Tenant.secret_name's spelling for a tenant id the frozen contract mints
  # for a person (identity.tenant_id_for_user). The other team's secrets are
  # agents-* and promptlab-*, which this cannot match.
  workspace_personal_secret_prefix = "projects/${local.workspace_project_number}/secrets/swarm-tenant-u-"

  # The roles each person's worker holds on the project, and so the only roles
  # the conditioned projectIamAdmin may add a member to (the WD9 fallback):
  # what modules/tenancy grants a Terraform-managed worker (worker_firestore,
  # worker_telemetry), so a personal worker is not a different kind of worker.
  workspace_worker_project_roles = [
    module.custom_role_ids.names.worker_firestore,
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]

  workspace_deployer_conditions = {
    # hasOnly() over the roles a setIamPolicy call MODIFIES: a call that adds
    # a member to any other role -- owner, editor, another team's -- is
    # refused. It limits WHICH roles, never to WHOM; the alert watches whom.
    project_iam_admin = "api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([${join(", ", [for r in local.workspace_worker_project_roles : "\"${r}\""])}])"

    # The forge slots and their twins, by full name (forge_user_slots.tf
    # explains the project-number form).
    secret_binder = "resource.name.startsWith(\"${local.workspace_personal_secret_prefix}\")"

    # The `.tenant` marker objects, under tenants/ and nowhere else.
    object_creator = "resource.name.startsWith(\"projects/_/buckets/${local.workspace_artifact_bucket}/objects/tenants/\")"

    # get-credentials on the swarm cluster; nothing on the other team's
    # agents-staging. The expression modules/iam builds for the dispatchers.
    cluster_viewer = "resource.name.startsWith(\"${local.workspace_cluster_name}\")"
  }

  # Where the job's log goes (§2.6), and the one filter that selects it.
  workspace_log_bucket      = "projects/${var.project_id}/locations/global/buckets/${local.workspace_apply_name}"
  workspace_log_view        = "${local.workspace_log_bucket}/views/_AllLogs"
  workspace_log_destination = "logging.googleapis.com/${local.workspace_log_bucket}"
  workspace_log_filter      = var.enable_workspace_deployer ? "resource.type=\"build\" AND resource.labels.build_trigger_id=\"${google_cloudbuild_trigger.workspace_apply[0].trigger_id}\"" : ""

  workspace_role_on = { for r in var.workspace_deployer_roles : r => true }
}

data "google_project" "workspace_deployer" {
  count      = local.workspace_deployer_on
  project_id = var.project_id
}

# ---------------------------------------------------------------------------
# The identity
# ---------------------------------------------------------------------------

# No labels: google_service_account has none (scripts/lib/unlabelable-types.json);
# the description says who manages it. No create_ignore_already_exists: an
# account someone made first under this name is a squat to investigate, and the
# create's ALREADY_EXISTS stops the apply.
resource "google_service_account" "workspace_deployer" {
  count = local.workspace_deployer_on

  project      = var.project_id
  account_id   = local.workspace_deployer_account_id
  display_name = "Swarm workspace deployer"
  description  = "managed-by=swarm-terraform; makes personal workspaces, only as the swarm-workspace-apply Cloud Build trigger, under the call guard (docs/workspaces.md §2.3). No key, no WIF."
}

# AUTHORITATIVE AND EMPTY, on purpose: nobody holds actAs, token creator or
# workloadIdentityUser on this account at the account level, and anything added
# by hand is removed at the next bootstrap apply. The trigger needs no binding
# here -- Cloud Build's service agent mints the build's token through its own
# project-level role -- and creating or editing the trigger needs actAs, which
# only the owner exercises (safeguard 1).
data "google_iam_policy" "workspace_deployer_nobody" {}

resource "google_service_account_iam_policy" "workspace_deployer" {
  count = local.workspace_deployer_on

  service_account_id = google_service_account.workspace_deployer[0].name
  policy_data        = data.google_iam_policy.workspace_deployer_nobody.policy_data
}

# ---------------------------------------------------------------------------
# Its custom roles (§2.3). Defined here, like every custom role (#79).
# ---------------------------------------------------------------------------

locals {
  workspace_custom_roles = {
    workspace_account_admin = {
      title       = "Swarm Workspace Account Admin"
      description = "managed-by=swarm-terraform; create a person's worker account and write its own Workload Identity and act-as bindings. No delete, disable, key, token, signBlob or actAs. Project-wide because IAM cannot condition it (docs/workspaces.md §2.4); bounded by the call guard and the alert."
      # Exactly §2.3's five. getIamPolicy and setIamPolicy are what
      # `gcloud iam service-accounts add-iam-policy-binding` reads and writes.
      permissions = [
        "iam.serviceAccounts.create",
        "iam.serviceAccounts.get",
        "iam.serviceAccounts.getIamPolicy",
        "iam.serviceAccounts.list",
        "iam.serviceAccounts.setIamPolicy",
      ]
    }

    workspace_project_reader = {
      title       = "Swarm Workspace Project Reader"
      description = "managed-by=swarm-terraform; read the project's IAM policy and the custom roles the workspace job checks. Read only."
      permissions = [
        "iam.roles.get",
        "resourcemanager.projects.getIamPolicy",
      ]
    }

    workspace_bucket_iam = {
      title       = "Swarm Workspace Bucket IAM"
      description = "managed-by=swarm-terraform; add a person's worker to the artifact bucket's policy with its tenants/<tenant>/ conditions. Granted on that bucket only. No object read: not legacyBucketReader, which lists every object."
      permissions = [
        "storage.buckets.get",
        "storage.buckets.getIamPolicy",
        "storage.buckets.setIamPolicy",
      ]
    }

    workspace_secret_binder = {
      title       = "Swarm Workspace Secret Binder"
      description = "managed-by=swarm-terraform; bind a person's worker to its own forge slot. Granted conditioned on the swarm-tenant-u- prefix. No secret read, no version."
      permissions = [
        "secretmanager.secrets.get",
        "secretmanager.secrets.getIamPolicy",
        "secretmanager.secrets.setIamPolicy",
      ]
    }

    workspace_firestore = {
      title       = "Swarm Workspace Firestore"
      description = "managed-by=swarm-terraform; read the workspace record and admin_roles, write progress and the tenant and pool documents. No delete. Project-wide: Firestore ignores conditions on the data plane."
      permissions = [
        "datastore.databases.getMetadata",
        "datastore.entities.create",
        "datastore.entities.get",
        "datastore.entities.list",
        "datastore.entities.update",
      ]
    }
  }

}

resource "google_project_iam_custom_role" "workspace" {
  for_each = var.enable_workspace_deployer ? local.workspace_custom_roles : {}

  project     = var.project_id
  role_id     = module.custom_role_ids.ids[each.key]
  title       = each.value.title
  description = each.value.description
  stage       = "GA"
  permissions = each.value.permissions
}

# ---------------------------------------------------------------------------
# Its grants (§2.3), each only while its role is on var.workspace_deployer_roles
# ---------------------------------------------------------------------------

locals {
  # Project-level grants. `role` is what the binding names; `conditions` is
  # empty for the five §2.3 leaves unconditioned (the account admin, which IAM
  # cannot narrow; the reader, which only reads; Firestore, whose data plane
  # ignores conditions; the slot creator, whose create is checked on the
  # project; and the log writer Cloud Build writes the job's log with).
  workspace_project_grants = {
    "swarmWorkspaceAccountAdmin" = {
      role       = module.custom_role_ids.names.workspace_account_admin
      conditions = []
    }
    "swarmWorkspaceProjectReader" = {
      role       = module.custom_role_ids.names.workspace_project_reader
      conditions = []
    }
    "swarmWorkspaceFirestore" = {
      role       = module.custom_role_ids.names.workspace_firestore
      conditions = []
    }
    "swarmForgeSlotCreator" = {
      role       = module.custom_role_ids.names.forge_slot_creator
      conditions = []
    }
    "roles/logging.logWriter" = {
      role       = "roles/logging.logWriter"
      conditions = []
    }
    "roles/resourcemanager.projectIamAdmin" = {
      role = "roles/resourcemanager.projectIamAdmin"
      conditions = [{
        title       = "a personal worker's three project roles only"
        description = "managed-by=swarm-terraform; WD9 fallback: adds a person's worker to the worker Firestore role, logWriter or metricWriter and to no other role (docs/workspaces.md §2.3). Members are watched by the alert."
        expression  = local.workspace_deployer_conditions.project_iam_admin
      }]
    }
    "swarmWorkspaceSecretBinder" = {
      role = module.custom_role_ids.names.workspace_secret_binder
      conditions = [{
        title       = "personal forge slots only"
        description = "managed-by=swarm-terraform; secrets named swarm-tenant-u-*, the personal tenants' (docs/workspaces.md §2.3)."
        expression  = local.workspace_deployer_conditions.secret_binder
      }]
    }
    "roles/container.clusterViewer" = {
      role = "roles/container.clusterViewer"
      conditions = [{
        title       = "swarm-cluster-only"
        description = "managed-by=swarm-terraform; get-credentials on the swarm Autopilot cluster for the namespace step; agents-staging belongs to another team."
        expression  = local.workspace_deployer_conditions.cluster_viewer
      }]
    }
  }
}

resource "google_project_iam_member" "workspace_deployer" {
  for_each = var.enable_workspace_deployer ? { for r, g in local.workspace_project_grants : r => g if lookup(local.workspace_role_on, r, false) } : {}

  project = var.project_id
  role    = each.value.role
  member  = local.workspace_deployer_member

  dynamic "condition" {
    for_each = each.value.conditions
    content {
      title       = condition.value.title
      description = condition.value.description
      expression  = condition.value.expression
    }
  }

  lifecycle {
    # The slot creator is forge_user_slots.tf's role, defined only while the
    # user slots are on; granting a role that does not exist fails the apply
    # halfway, so it is refused at plan instead.
    precondition {
      condition     = each.key != "swarmForgeSlotCreator" || var.enable_forge_user_slots
      error_message = "swarm-workspace-deployer is granted swarmForgeSlotCreator, which exists only while enable_forge_user_slots is true. Turn the user slots on in the same apply, or take swarmForgeSlotCreator off workspace_deployer_roles."
    }
  }

  depends_on = [
    google_project_iam_custom_role.workspace,
    google_project_iam_custom_role.forge_slot_creator,
    google_service_account.workspace_deployer,
  ]
}

# The artifact bucket: §2.3's two bucket rows. Additive members, never a
# bucket policy or binding, which would remove every personal worker the job
# has added (§3.2, rule 2).
resource "google_storage_bucket_iam_member" "workspace_deployer_bucket_iam" {
  count = var.enable_workspace_deployer && lookup(local.workspace_role_on, "swarmWorkspaceBucketIam", false) ? 1 : 0

  bucket = local.workspace_artifact_bucket
  role   = module.custom_role_ids.names.workspace_bucket_iam
  member = local.workspace_deployer_member

  depends_on = [google_project_iam_custom_role.workspace, google_service_account.workspace_deployer]
}

resource "google_storage_bucket_iam_member" "workspace_deployer_marker" {
  count = var.enable_workspace_deployer && lookup(local.workspace_role_on, "roles/storage.objectCreator", false) ? 1 : 0

  bucket = local.workspace_artifact_bucket
  role   = "roles/storage.objectCreator"
  member = local.workspace_deployer_member

  condition {
    title       = "tenant marker objects only"
    description = "managed-by=swarm-terraform; the .tenant marker under tenants/<tenant>/. Create only: no read, no delete."
    expression  = local.workspace_deployer_conditions.object_creator
  }

  depends_on = [google_service_account.workspace_deployer]
}

# The builder image's repository: pull, never list or push.
resource "google_artifact_registry_repository_iam_member" "workspace_deployer_pull" {
  count = var.enable_workspace_deployer && lookup(local.workspace_role_on, "swarmImagePuller", false) ? 1 : 0

  project    = local.workspace_builder_parts.project
  location   = local.workspace_builder_parts.location
  repository = local.workspace_builder_parts.repository
  role       = module.custom_role_ids.names.image_puller
  member     = local.workspace_deployer_member

  depends_on = [google_project_iam_custom_role.platform, google_service_account.workspace_deployer]
}

# ---------------------------------------------------------------------------
# The topic: swarm-api publishes, and nobody else is granted to
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "workspace_apply" {
  count = local.workspace_deployer_on

  project = var.project_id
  name    = local.workspace_apply_name
  labels  = local.labels

  # A message nobody built for in a day is stale: the sweep (§2.2)
  # republishes an approved record that was never claimed.
  message_retention_duration = "86400s"
}

# AUTHORITATIVE FOR THE ROLE: the topic's publishers are swarm-api and no one
# else. Project-level publishers (owners, editors, pubsub.admin holders) can
# still publish; a forged message does nothing on its own, because A1 refuses a
# record no admin approved (§2.4 R3). The account is terraform/infra's, named
# here as forge_user_slots.tf names it.
resource "google_pubsub_topic_iam_binding" "workspace_apply_publisher" {
  count = local.workspace_deployer_on

  project = var.project_id
  topic   = google_pubsub_topic.workspace_apply[0].name
  role    = "roles/pubsub.publisher"
  members = [local.forge_api_member]
}

# ---------------------------------------------------------------------------
# The trigger: Pub/Sub, main only, as the deployer
# ---------------------------------------------------------------------------

# No labels: google_cloudbuild_trigger has none in the pinned provider
# (6.50.0); its description and its name say whose it is
# (scripts/lib/unlabelable-types.json).
resource "google_cloudbuild_trigger" "workspace_apply" {
  count = local.workspace_deployer_on

  project     = var.project_id
  location    = local.workspace_apply_location
  name        = local.workspace_apply_name
  description = "managed-by=swarm-terraform; makes a personal workspace from an approved record: main's scripts/cloudbuild/workspace-apply.yaml as swarm-workspace-deployer (docs/workspaces.md §2.1)."

  service_account = google_service_account.workspace_deployer[0].id

  pubsub_config {
    topic = google_pubsub_topic.workspace_apply[0].id
  }

  # What is built, and the file that says how: main, both.
  source_to_build {
    repository = var.workspace_apply_repository
    ref        = local.workspace_apply_ref
    repo_type  = "GITHUB"
  }

  git_file_source {
    path       = local.workspace_apply_build_file
    repository = var.workspace_apply_repository
    revision   = local.workspace_apply_ref
    repo_type  = "GITHUB"
  }

  # The message's two fields and nothing else; the image is this root's.
  substitutions = {
    _WORKSPACE_ID  = "$(body.message.data.workspace_id)"
    _MODE          = "$(body.message.data.mode)"
    _BUILDER_IMAGE = var.workspace_apply_builder_image
  }

  # A message of any other shape starts no build. Step 1 of the build file
  # checks the same again, so neither alone is load-bearing.
  filter = "_WORKSPACE_ID.matches('^w-[0-9a-f]{6}$') && _MODE.matches('^(create|limits)$')"

  lifecycle {
    precondition {
      condition     = var.workspace_apply_repository != ""
      error_message = "enable_workspace_deployer needs workspace_apply_repository: connect this repository to Cloud Build first (docs/workspaces.md §10, the owner's one-time steps)."
    }
    precondition {
      condition     = var.workspace_apply_builder_image != ""
      error_message = "enable_workspace_deployer needs workspace_apply_builder_image, the workspace-apply image by digest from the release's promotion manifest."
    }
    precondition {
      condition     = local.workspace_builder_parts.project == var.project_id
      error_message = "workspace_apply_builder_image must be in this project's Artifact Registry: only images this pipeline built, scanned and promoted run as swarm-workspace-deployer."
    }
  }

  depends_on = [google_service_account_iam_policy.workspace_deployer]
}

# ---------------------------------------------------------------------------
# Its logs: a restricted bucket, and out of _Default (§2.6)
# ---------------------------------------------------------------------------
#
# The build log names a person's tenant id and email. _Default is readable by
# every holder of roles/logging.viewer in this shared project, the other team
# included. A user-defined bucket is not: reading one needs logging.views.access
# on its view, which logging.viewer does not carry (measured 2026-09-24,
# log-reading-roles.json), so its readers are the project's owners and logging
# admins and the people named in var.workspace_log_readers.
#
# What still reaches _Required, and so every log viewer, is the Admin Activity
# audit trail of the job's calls, which names the accounts it creates. Cloud
# Logging routes those entries to _Required whatever any sink says.

# No labels: google_logging_project_bucket_config has none
# (scripts/lib/unlabelable-types.json). Its id carries the platform prefix.
resource "google_logging_project_bucket_config" "workspace_apply" {
  count = local.workspace_deployer_on

  project        = var.project_id
  location       = "global"
  bucket_id      = local.workspace_apply_name
  retention_days = var.workspace_log_retention_days
  description    = "managed-by=swarm-terraform; the swarm-workspace-apply job's build logs, which name people (docs/workspaces.md §2.6)."
}

resource "google_logging_project_sink" "workspace_apply" {
  count = local.workspace_deployer_on

  project     = var.project_id
  name        = local.workspace_apply_name
  destination = local.workspace_log_destination
  filter      = local.workspace_log_filter
  description = "managed-by=swarm-terraform; the swarm-workspace-apply trigger's build logs, to the restricted bucket."

  # A log bucket in the same project needs no grant to the writer.
  unique_writer_identity = true

  depends_on = [google_logging_project_bucket_config.workspace_apply]
}

# A project exclusion applies to the _Default sink (Logging API,
# projects.exclusions.create: "Creates a new exclusion in the _Default sink").
# Created after the sink, so no entry is ever in neither place.
resource "google_logging_project_exclusion" "workspace_apply" {
  count = local.workspace_deployer_on

  project     = var.project_id
  name        = local.workspace_apply_name
  filter      = local.workspace_log_filter
  description = "managed-by=swarm-terraform; keeps the swarm-workspace-apply job's build logs out of _Default, which the other team can read."

  depends_on = [google_logging_project_sink.workspace_apply]
}

resource "google_project_iam_member" "workspace_log_readers" {
  for_each = var.enable_workspace_deployer ? toset(var.workspace_log_readers) : toset([])

  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = each.value

  condition {
    title       = "swarm-workspace-apply logs only"
    description = "managed-by=swarm-terraform; the restricted bucket of the workspace job's build logs (docs/workspaces.md §2.6)."
    expression  = "resource.name == \"${local.workspace_log_view}\""
  }

  depends_on = [google_logging_project_bucket_config.workspace_apply]
}

output "workspace_deployer" {
  description = "What the owner reads a plan against: the workspace job's identity, its trigger, the image it runs, the log bucket and filter. Null while enable_workspace_deployer is false."
  value = var.enable_workspace_deployer ? {
    service_account = local.workspace_deployer_email
    trigger         = local.workspace_apply_name
    location        = local.workspace_apply_location
    ref             = local.workspace_apply_ref
    build_file      = local.workspace_apply_build_file
    builder_image   = var.workspace_apply_builder_image
    log_bucket      = local.workspace_log_bucket
    roles           = var.workspace_deployer_roles
  } : null
}
