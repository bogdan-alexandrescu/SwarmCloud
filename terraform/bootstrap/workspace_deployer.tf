# ---------------------------------------------------------------------------
# swarm-workspace-deployer: the identity that makes a person's workspace, the
# one Cloud Run job that may use it, who may start that job, and where its logs
# go (docs/workspaces.md §2.1-2.6; lane W4 of #847, rebuilt by W4b)
# ---------------------------------------------------------------------------
#
# WHAT THIS IS FOR. An admin's approval publishes {"workspace_id", "mode"} to
# the topic swarm-workspace-apply. Under option (ii) of §2.1 (owner,
# 2026-10-10) an Eventarc trigger hands the message to the workflow of the same
# name (workspace_dispatch.tf), which checks both values and starts ONE
# execution of the Cloud Run job swarm-workspace-apply with them as its two
# arguments, as swarm-workspace-dispatch. The job runs the pinned
# images/workspace-apply image as swarm-workspace-deployer: its fixed command,
# `python3 -I /opt/swarm/entry.py`, scrubs every override (§2.2 step 0) and
# runs `scripts/register-tenant.sh --workspace <w-id>` under the call guard
# (scripts/lib/workspace-guard.sh), which makes that one person's worker
# account, its bindings, its bucket prefix grants, its forge slot and its
# namespace (owner decisions WD2, re-decided 2026-10-10, and WD3).
#
# The Cloud Build trigger this file used to make is gone with WD2's
# re-decision, and so, since the job reached A9 on 2026-10-13 (lane W11), are
# the two grants to Cloud Build's service agent that let the repository
# connection store its token.
#
# THE ACCEPTED RISK (§2.4, owner, 2026-10-08). A worker account's own IAM
# policy can only be written by an identity holding
# iam.serviceAccounts.setIamPolicy on it, and IAM cannot condition that grant
# on the account's name ("IAM resources don't provide the resource name",
# #334). So swarmWorkspaceAccountAdmin below is PROJECT-WIDE, and could set IAM
# on the other team's accounts if misused. What bounds it:
#
#   1. Nothing but the job runs as the identity. It has no key (nothing here
#      makes one) and no WIF binding, and its own IAM policy is written
#      AUTHORITATIVELY EMPTY below -- no actAs, no token creator, no
#      workloadIdentityUser, for anyone, at the account level. Making anything
#      run as it, or changing what the job runs, needs actAs on it: Cloud Run
#      checks actAs on every job create and update (W0b (1), documentation
#      half; the operator's live refusal is still pending, §0).
#   2. The job runs only the image pinned BY DIGEST here (workspace_apply_image),
#      with a command no caller can override, so a merge to main reaches the
#      identity only through this root's apply. .github/CODEOWNERS names the
#      owner on what that image carries.
#   3. Starting the job is NOT gated by actAs, and Cloud Run grants cannot be
#      conditioned, so starting it is not the boundary: what a start can choose
#      is (two arguments, each re-validated, every overridable variable
#      discarded, and A1 refusing a record no admin approved). The job's own
#      policy names the dispatcher only, and Cloud Run's DATA_WRITE audit log,
#      enabled below, is what lets an alert see any other caller.
#   4. Five alerts (terraform/modules/monitoring/workspace_alerts.tf): a change
#      by this identity to an account not named swarm-agent-worker-u-*, or a
#      grant to such a member anywhere; anything else made to run as this
#      identity; any change to the job, its IAM, the dispatch path or the
#      identities; and a jobs.run of the job by any caller but the dispatcher.
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
# job that runs as an account needs actAs on that account, which CI must never
# hold on this one.
#
# NOT VERIFIED LIVE -- each is settled by the first approved workspace, and each
# fails closed (a failed execution, or an alert that pages), never open:
#
#   * that Cloud Run pulls the job's image as the project's Cloud Run service
#     agent, so the identity needs no pull grant (§2.3; W0b (4)-(6) read it on
#     the first execution). If it does not, the execution fails to start;
#   * that the job's own stdout reaches Cloud Logging without
#     roles/logging.logWriter on the identity. Until W0b says so, the grant
#     stays (workspace_deployer_roles);
#   * that the job's log entries carry resource.labels.job_name, which the sink
#     and the exclusion select on (a documented label of cloud_run_job, unlike
#     the build's trigger id). If they do not, the log stays in _Default (the
#     §2.6 residual) and the restricted bucket stays empty;
#   * that IAM evaluates every condition below in the form written. The
#     secret, bucket-object and cluster forms are the ones forge_user_slots.tf,
#     modules/tenancy and modules/iam already use; hasOnly() is
#     deployer_conditions.tf's.

locals {
  workspace_deployer_on = var.enable_workspace_deployer ? 1 : 0

  # One name for the topic, the job, the workflow, the Eventarc trigger, the
  # log bucket, the sink and the exclusion: what an operator searches for finds
  # all of them. The alerts (modules/monitoring) spell the same literal.
  workspace_apply_name = "swarm-workspace-apply"

  workspace_deployer_account_id = "swarm-workspace-deployer"
  workspace_deployer_email      = "${local.workspace_deployer_account_id}@${var.project_id}.iam.gserviceaccount.com"
  workspace_deployer_member     = "serviceAccount:${local.workspace_deployer_email}"

  # The public keys the job verifies approval signatures with (lane
  # WK-APPROVAL-KEYS of #847; owner decision 2026-10-19: no new IAM for
  # approval verification, so the job never reads Cloud KMS). SPEC_VERIFY_KEYS
  # in the shape workers get, {full version name: PEM}, over the ENABLED
  # versions of the key swarm-api signs approvals with: dev's step-spec key,
  # the one the command below names to images/workspace-apply/entry.py. Read
  # below, at the owner's plan, with the owner's credentials. Public keys, not
  # secrets; nothing private is ever read or written here.
  workspace_spec_environment = "dev"
  workspace_spec_verify_keys = jsonencode(var.enable_workspace_deployer ? module.workspace_spec_keys[0].verify_keys : {})

  # The key whose signatures the job trusts (DAYONE-BOOT-1, part of #2688):
  # THIS project's step-spec key, the same module's the verify keys come from,
  # so the trusted key and the public keys pinned beside it are one key's.
  # entry.py used to trust one hard-coded project's key, so on any other
  # project every group or service workspace stopped at APPROVAL_UNSIGNED.
  workspace_spec_signing_key = var.enable_workspace_deployer ? module.workspace_spec_keys[0].crypto_key_id : ""

  # The job's command: isolated-mode Python running the scrub (§2.2 step 0),
  # which the image puts root-owned under /opt/swarm (images/workspace-apply,
  # W6b). Held by tests/terraform/workspace_deployer.tftest.hcl: the command
  # is the one part of an execution no caller can override, so it is what
  # makes the scrub run first.
  #
  # ITS LAST TWO ELEMENTS ARE THE TRUST. A caller holding runWithOverrides may
  # set any environment variable on an execution, SPEC_VERIFY_KEYS included,
  # and would then choose which key "verifies" a record it wrote. It cannot
  # touch the command, so the command carries the SHA-256 of the value set
  # below, and entry.py refuses an execution whose SPEC_VERIFY_KEYS hashes to
  # anything else; and it names the one key whose versions count, which
  # entry.py holds to a step-spec key of the metadata server's project.
  workspace_apply_command = [
    "python3", "-I", "/opt/swarm/entry.py",
    "--spec-verify-keys-sha256=${sha256(local.workspace_spec_verify_keys)}",
    "--spec-signing-key=${local.workspace_spec_signing_key}",
  ]

  # The swarm subnet and the network it is in, as terraform/modules/network
  # names them (this root cannot read infra's state). The worker tag is that
  # module's worker_network_tag default, which modules/network's
  # deny_worker_ingress targets, so nothing in the VPC can open a connection to
  # an execution; the job's own tag names it in a flow log or a firewall rule.
  workspace_apply_network    = "projects/${var.project_id}/global/networks/${var.name_prefix}-vpc"
  workspace_apply_subnetwork = var.workspace_apply_subnetwork != "" ? var.workspace_apply_subnetwork : "projects/${var.project_id}/regions/${var.region}/subnetworks/${var.name_prefix}-subnet-${var.region}"
  workspace_apply_tags       = ["swarm-worker", local.workspace_apply_name]

  # The image's repository, read from its own reference: the precondition on
  # the job holds it to this project.
  workspace_image_parts = regex("^(?P<location>[a-z0-9-]+)-docker\\.pkg\\.dev/(?P<project>[^/]+)/(?P<repository>[^/]+)/", var.workspace_apply_image == "" ? "${var.region}-docker.pkg.dev/${var.project_id}/swarm-images/" : var.workspace_apply_image)

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
  #
  # The job's own entries, by its resource type and documented job_name label,
  # a literal known before the job exists. Its AUDIT entries are kept out of
  # the filter: Admin Activity goes to _Required whatever any sink says, and
  # the DATA_WRITE entry of a jobs.run (the caller and the workspace id) stays
  # in _Default, where §2.6 item 4 says it is and where the foreign-run alert
  # reads it -- routing it into this bucket would hide the one record of who
  # started an execution from everyone outside the bucket's readers.
  workspace_log_filter = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${local.workspace_apply_name}\" AND NOT logName:\"cloudaudit.googleapis.com\""

  workspace_role_on = { for r in var.workspace_deployer_roles : r => true }
}

data "google_project" "workspace_deployer" {
  count      = local.workspace_deployer_on
  project_id = var.project_id
}

# ---------------------------------------------------------------------------
# The job's verify keys: read at the owner's plan, never at run time
# ---------------------------------------------------------------------------
#
# The step-spec key's ENABLED versions and each one's public key, read by
# NAME -- bootstrap's own module.spec_signing_key["dev"] spells it -- with the
# owner's credentials, exactly as terraform/infra reads them for workers. So
# the job's identity holds no role on the key (owner decision 2026-10-19).
#
# WHY THIS DOES NOT HIT #360. spec_signing.tf explains why this root reads no
# versions for the key it creates: a list that depends on the key RESOURCE is
# unknown on the plan that creates it, and the module's for_each over it
# fails. This data source depends on no resource -- the name is a string the
# module derives from variables -- so it is read at plan. What that costs: the
# key must already exist when enable_workspace_deployer is switched on. It has
# since #342's bootstrap apply, and the job needs the platform's network and
# image from infra's release anyway, so the deployer is never part of a
# project's first apply. With the switch off, nothing here is read.
#
# THE KEYS ARE AS FRESH AS THE LAST BOOTSTRAP APPLY. Workers' keys refresh on
# every release; these only when the owner applies this root. A version
# enabled since stops every group and service run at APPROVAL_UNSIGNED (fail
# closed) until it is applied: re-apply bootstrap after enabling a version and
# before swarm-api signs with it (docs/runbooks/spec-signing-rollout.md,
# "Rotation").
data "google_kms_crypto_key_versions" "workspace_spec_versions" {
  count = local.workspace_deployer_on

  crypto_key = module.spec_signing_key[local.workspace_spec_environment].crypto_key_id
  filter     = "state=ENABLED"
}

module "workspace_spec_keys" {
  source = "../modules/spec_signing_key"
  count  = local.workspace_deployer_on

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  environment = local.workspace_spec_environment
  versions    = data.google_kms_crypto_key_versions.workspace_spec_versions[0].versions
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
  description  = "managed-by=swarm-terraform; makes personal workspaces, only as the swarm-workspace-apply Cloud Run job, under the call guard (docs/workspaces.md §2.3). No key, no WIF."
}

# AUTHORITATIVE AND EMPTY, on purpose: nobody holds actAs, token creator or
# workloadIdentityUser on this account at the account level, and anything added
# by hand is removed at the next bootstrap apply. The job needs no binding here
# -- Cloud Run's service agent mints an execution's token through its own
# project-level role -- and creating or updating the job needs actAs, which
# only the owner's bootstrap apply exercises (safeguard 1).
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
      title = "Swarm Workspace Account Admin"
      # It also lists the account's key ids (iam.serviceAccountKeys.list, A2/A9's
      # squat check, #334). The description below is unchanged: GCP caps a role
      # description at 300 characters and this one is at the limit.
      description = "managed-by=swarm-terraform; create a person's worker account and write its own Workload Identity and act-as bindings. No delete, disable, key, token, signBlob or actAs. Project-wide because IAM cannot condition it (docs/workspaces.md §2.4); bounded by the call guard and the alert."
      # §2.3's six. getIamPolicy and setIamPolicy are what
      # `gcloud iam service-accounts add-iam-policy-binding` reads and writes.
      # tests/unit/scripts/test_workspace_deployer_walk.py holds every call of
      # the workspace job to a grant here or below.
      permissions = [
        "iam.serviceAccounts.create",
        "iam.serviceAccounts.get",
        "iam.serviceAccounts.getIamPolicy",
        "iam.serviceAccounts.list",
        "iam.serviceAccounts.setIamPolicy",
        # `gcloud iam service-accounts keys list --managed-by=user`: the squat
        # inspection (#334) that refuses to adopt an account carrying a
        # user-managed key, in A2 on an account that already exists and in A9
        # on every run. Key ids and validity dates only -- never key material,
        # and no create, upload, disable or delete. Missing until WS-WALK
        # (2026-10-13), so no create could have reached `ready`.
        "iam.serviceAccountKeys.list",
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
      # One permission per Firestore call of register-tenant.sh --workspace,
      # from Google's table (firestore/docs/security/iam).
      # tests/unit/scripts/test_workspace_firestore_permissions.py walks the
      # script's calls and the guard's (workspace-calls.json) and holds this
      # list to exactly what they need.
      #
      # databases.get was missing until 2026-10-13, so the A1 claim's
      # beginTransaction 403'd on every execution for w-7f9285.
      # databases.getMetadata is projects.databases.get, which the job never
      # calls (its guard refuses it). It STAYS by owner decision 2026-10-12
      # (adds-only IAM change; removing it is a separate change), and
      # tests/unit/scripts/test_workspace_firestore_permissions.py allows
      # exactly that one unused extra.
      permissions = [
        # Unused (see above); kept by owner decision 2026-10-12.
        "datastore.databases.getMetadata",
        # beginTransaction (A1 claim) and rollback (a claim that re-admits
        # nothing). The transaction permission, not the metadata one.
        "datastore.databases.get",
        # fs_patch on tenants/<t> and pools/tenant:<t> with no precondition
        # (A8) is an upsert that creates a missing document; a commit update
        # with no exists precondition needs it too.
        "datastore.entities.create",
        # fs_get of workspaces/, tenants/, pools/, admin_roles/<email>; the
        # claim's transactional GET; runQuery on workspaces (A1) reads docs.
        "datastore.entities.get",
        # runQuery on workspaces by workspace_id (A1), and on admin_roles.
        "datastore.entities.list",
        # fs_patch of progress on workspaces/<t>, of tenants/ and pools/
        # (A8), and the claim's commit (an update, currentDocument.exists).
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
  # project; and the log writer, which a Cloud Run job's own output may not
  # need: W0b reads that on the first execution, and only then does it go).
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

# No pull grant on the image's repository. Cloud Run pulls a job's image as
# the project's Cloud Run service agent, not as the job's account, for an image
# in the same project's Artifact Registry (§2.3); the build this replaced
# needed swarmImagePuller, and workspace_deployer_pull went with it (W4b).
# No removed block: the grant was count-gated on enable_workspace_deployer,
# which the committed terraform.tfvars leaves at its default, false, so a
# bootstrap applied from main never created it.

# ---------------------------------------------------------------------------
# The topic: swarm-api publishes, and nobody else is granted to. Kept by
# option (ii): the Eventarc trigger in workspace_dispatch.tf reads it.
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "workspace_apply" {
  # checkov:skip=CKV_GCP_83:A message names one approved workspace to provision (workspace id, request id) -- no tenant data, no credential -- and is retained a day. CMEK would need a key ring this platform does not own in a shared project; same reasoning as the scheduler's wake topic.
  count = local.workspace_deployer_on

  project = var.project_id
  name    = local.workspace_apply_name
  labels  = local.labels

  # A message nobody dispatched in a day is stale: the sweep (§2.2)
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
# The job: the pinned image, a fixed command, one task, as the deployer
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "workspace_apply" {
  count = local.workspace_deployer_on

  project  = var.project_id
  location = var.region
  name     = local.workspace_apply_name
  labels   = local.labels

  # Switching enable_workspace_deployer off must remove the job, not fail the
  # owner's apply halfway. Losing it costs availability only: re-creating it
  # needs actAs on the deployer, which is the owner's apply again.
  deletion_protection = false

  template {
    # One task, no parallelism: A1's claim admits one run per workspace, and
    # step 0 refuses a task count other than one whatever an override says.
    task_count  = 1
    parallelism = 1
    labels      = local.labels

    template {
      service_account = local.workspace_deployer_email

      # A retry is an admin's action (§1.3), never Cloud Run's.
      max_retries = 0

      # As the build had; entry.py keeps its own 1800-second deadline whatever
      # timeout an override sets.
      timeout = "1800s"

      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      containers {
        image = var.workspace_apply_image

        # Fixed here, and not overridable by jobs.run (W0b (2)). No args: the
        # two arguments arrive with each execution, from the workflow.
        command = local.workspace_apply_command

        # The job's one variable: the public keys the command pins (above).
        # entry.py reads it in step 0 and passes it to nothing.
        env {
          name  = "SPEC_VERIFY_KEYS"
          value = local.workspace_spec_verify_keys
        }

        resources {
          # Invariant 7: a Cloud Run job states limits only, and they are what
          # it gets. 2 GiB holds gcloud, kubectl, the render and the guard's
          # expectation file in the execution's in-memory filesystem.
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }
      }

      vpc_access {
        # ALL_TRAFFIC, as the worker jobs do (modules/cloud_run_jobs): the GKE
        # control plane is reached from inside the VPC, through the private
        # endpoint where it is private and through the swarm Cloud NAT where
        # it is public under master_authorized_cidrs (§2.1).
        egress = "ALL_TRAFFIC"

        network_interfaces {
          network    = local.workspace_apply_network
          subnetwork = local.workspace_apply_subnetwork
          tags       = local.workspace_apply_tags
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.workspace_apply_image != ""
      error_message = "enable_workspace_deployer needs workspace_apply_image, the workspace-apply image by digest from the release's promotion manifest."
    }
    precondition {
      condition     = local.workspace_image_parts.project == var.project_id
      error_message = "workspace_apply_image must be in this project's Artifact Registry: only images this pipeline built, scanned and promoted run as swarm-workspace-deployer."
    }
  }

  depends_on = [google_service_account_iam_policy.workspace_deployer]
}

# WHO MAY START IT: the dispatcher of option (ii), and nobody else, on the job.
#
# AUTHORITATIVE: a member added to the job's policy outside this file is
# removed at the next bootstrap apply, and the job_changed alert pages on the
# change itself. roles/run.jobsExecutorWithOverrides because the workflow
# passes the two arguments as an override (run.jobs.runWithOverrides); bound
# here, on the job, because Cloud Run exposes no resource name to IAM
# Conditions (#965), so a project-level grant could not be narrowed to it.
#
# This bounds who holds the role ON THE JOB. It does not empty the list of who
# may start it: swarm-scheduler, swarm-accept, the release deployer and the
# project's owners hold run.jobs.runWithOverrides project-wide (§2.1), which
# is why the image's scrub is load-bearing and why the foreign_run alert
# exists. Changing what the job runs needs actAs on the deployer as well, which
# Cloud Run checks on every update (W0b (1)).
#
# RESIDUAL RISK, NOT TOUCHED HERE: the project's default compute account
# (<number>-compute@developer.gserviceaccount.com) holds roles/editor on the
# whole shared project (W0b (3), live, 2026-10-10; #1020). Editor carries
# actAs and Cloud Run job update and run, so a workload running as that
# account steps around this policy and the deployer's empty one. It is
# Google's legacy default grant, the other team's workloads may rely on it,
# and it is removed together with that team, never from this file.
data "google_iam_policy" "workspace_apply_job" {
  binding {
    role    = "roles/run.jobsExecutorWithOverrides"
    members = [local.workspace_dispatch_member]
  }
}

resource "google_cloud_run_v2_job_iam_policy" "workspace_apply" {
  count = local.workspace_deployer_on

  project     = var.project_id
  location    = google_cloud_run_v2_job.workspace_apply[0].location
  name        = google_cloud_run_v2_job.workspace_apply[0].name
  policy_data = data.google_iam_policy.workspace_apply_job.policy_data

  depends_on = [google_service_account.workspace_dispatch]
}

# ---------------------------------------------------------------------------
# Cloud Run's DATA_WRITE audit log: the only record of who started an execution
# ---------------------------------------------------------------------------
#
# A jobs.run is a Data Access entry (run.jobs.run and runWithOverrides are
# DATA_WRITE), and Data Access logs are off by default (W0b (2), §0). Without
# this, nothing records who started the job, and the foreign_run alert could
# never fire.
#
# AUTHORITATIVE FOR run.googleapis.com: google_project_iam_audit_config owns
# the project's whole audit config for that one service, so a type or an
# exempted member set elsewhere for Cloud Run would be replaced at the next
# bootstrap apply. The live project had NO audit config at all when read on
# 2026-10-10, so there was nothing to carry over; the job_changed alert pages
# on any later change to it. Other services' configs are untouched.
#
# DATA_WRITE ONLY. DATA_READ would log every get and list of every Cloud Run
# resource in the shared project -- the scheduler's and reconciler's polls and
# the other team's tooling -- for no question this platform asks.
#
# WHAT IT COSTS. It is project-wide for Cloud Run, so it logs the other team's
# Cloud Run DATA_WRITE calls too (executions started, services invoked through
# the Admin API), into _Default. Data Access entries are billed as ingested
# logs above the free allotment; at this platform's rate (a run per dispatch,
# the scheduler's job starts) that is a few MB a day, well inside it.
resource "google_project_iam_audit_config" "run_data_write" {
  count = local.workspace_deployer_on

  project = var.project_id
  service = "run.googleapis.com"

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# ---------------------------------------------------------------------------
# Its logs: a restricted bucket, and out of _Default (§2.6)
# ---------------------------------------------------------------------------
#
# The job's log names a person's tenant id and email. _Default is readable by
# every holder of roles/logging.viewer in this shared project, the other team
# included. A user-defined bucket is not: reading one needs logging.views.access
# on its view, which logging.viewer does not carry (measured 2026-09-24,
# log-reading-roles.json), so its readers are the project's owners and logging
# admins and the people named in var.workspace_log_readers.
#
# What still reaches every log viewer: the Admin Activity audit trail of the
# job's calls, which names the accounts it creates (Cloud Logging routes those
# entries to _Required whatever any sink says), and the DATA_WRITE entry of
# each jobs.run, which carries the caller and the workspace id (§2.6 item 4).

# No labels: google_logging_project_bucket_config has none
# (scripts/lib/unlabelable-types.json). Its id carries the platform prefix.
resource "google_logging_project_bucket_config" "workspace_apply" {
  count = local.workspace_deployer_on

  project        = var.project_id
  location       = "global"
  bucket_id      = local.workspace_apply_name
  retention_days = var.workspace_log_retention_days
  description    = "managed-by=swarm-terraform; the swarm-workspace-apply Cloud Run job's logs, which name people (docs/workspaces.md §2.6)."
}

resource "google_logging_project_sink" "workspace_apply" {
  count = local.workspace_deployer_on

  project     = var.project_id
  name        = local.workspace_apply_name
  destination = local.workspace_log_destination
  filter      = local.workspace_log_filter
  description = "managed-by=swarm-terraform; the swarm-workspace-apply Cloud Run job's logs, to the restricted bucket."

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
  description = "managed-by=swarm-terraform; keeps the swarm-workspace-apply Cloud Run job's logs out of _Default, which the other team can read."

  depends_on = [google_logging_project_sink.workspace_apply]
}

resource "google_project_iam_member" "workspace_log_readers" {
  for_each = var.enable_workspace_deployer ? toset(var.workspace_log_readers) : toset([])

  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = each.value

  condition {
    title       = "swarm-workspace-apply logs only"
    description = "managed-by=swarm-terraform; the restricted bucket of the workspace job's logs (docs/workspaces.md §2.6)."
    expression  = "resource.name == \"${local.workspace_log_view}\""
  }

  depends_on = [google_logging_project_bucket_config.workspace_apply]
}

output "workspace_deployer" {
  description = "What the owner reads a plan against: the workspace job's identity, the job, the image and command it runs, its dispatcher, the log bucket and filter. Null while enable_workspace_deployer is false."
  value = var.enable_workspace_deployer ? {
    service_account = local.workspace_deployer_email
    job             = local.workspace_apply_name
    location        = var.region
    image           = var.workspace_apply_image
    command         = local.workspace_apply_command
    dispatcher      = local.workspace_dispatch_email
    workflow        = local.workspace_apply_name
    log_bucket      = local.workspace_log_bucket
    log_filter      = local.workspace_log_filter
    roles           = var.workspace_deployer_roles
  } : null
}

# ---------------------------------------------------------------------------
# WORKSPACE KINDS: K1 and K2 (docs/design/workspace-kinds.md §4.5, lane WK-IAM
# of #847). K3 is swarm-api's, in forge_user_slots.tf; K4 is the role list in
# variables.tf. All behind enable_workspace_kinds, off by default.
# ---------------------------------------------------------------------------
#
# The same job, swarm-workspace-apply, builds group and service workspaces as
# well as personal ones (owner decision 2026-10-19, design A, Q3 (a)). Two of
# its steps need a grant the personal path never did:
#
#   K1, step S2: a service workspace's account is admitted at the front door,
#       as one member of roles/iap.httpsResourceAccessor on the API backend.
#       swarmFrontDoorAdmitter is iap.webServices.getIamPolicy and
#       setIamPolicy and nothing else -- no iap.web.* or iap.webTypes.*, which
#       reach every backend, and not roles/iap.admin (refused in variables.tf).
#       Its condition is EQUALITY on one backend's full name, by project
#       NUMBER and the backend's generated id, never a prefix: the console's
#       -ui-backend, and the other team's eight IAP backends in this shared
#       project, cannot match. A service calls the API; the console is for
#       people, so the console's backend is never named.
#
#   K2, step A6 for a group: the group's team slot, swarm-tenant-<g>-git and
#       its -git-refresh twin, is bound to the group's worker. The existing
#       secret-binder grant stops at swarm-tenant-u-, and a job-made group's
#       id has no u-, so a second grant of the same role reaches the team-slot
#       suffixes. It does not reach a tenant's provider key, workspace
#       secrets or members' -git-u- slots (Q6 (a): those are read through the
#       broker, never bound to a group worker).
#
# WHAT IAM CANNOT BOUND, and what does (§4.5): IAM cannot say WHICH member a
# setIamPolicy adds. The job's guard rule C10 admits exactly the approved
# service account and C9 refuses every removal; A1 refuses a service record
# not approved by the platform owner (Q2 (a)); swarm-api admits the account
# only once its record is `ready`; and the alert on every SetIamPolicy by this
# identity on an IAP resource (terraform/modules/monitoring/workspace_alerts.tf)
# pages a person on each one. K2 reaches the Terraform tenants' team slots
# (eng, smoke) as well; the same guard and the secret-IAM alert bound that.
#
# RESIDUAL RISK R7 (§4.5), stated so it is not mistaken for zero: a stolen
# deployer token can rewrite the API backend's accessor list for the token's
# life, widening it (which admits nobody to swarm-api, whose domain,
# ALLOWED_USERS and registry checks are unchanged) or narrowing it (an outage
# until the owner's bootstrap apply re-adds frontend_accessors).
#
# NOT VERIFIED LIVE (owner step O1, before the switch is turned on): that IAP
# accepts these two permission names in a custom role; that a project binding
# conditioned on `projects/<number>/iap_web/compute/services/<id>` matches
# the API backend and not -ui-backend; and whether IAP evaluates
# modifiedGrantsByRole (front_door_admit_role_bound). If the name form does not
# match, K1 narrows nothing usable and S2 stays an owner step
# (FRONT_DOOR_OWNER). So K1 has two keys: this switch, and
# swarmFrontDoorAdmitter on workspace_deployer_roles, which is not in that
# list's default and is added by the owner once O1 has passed.

locals {
  workspace_kinds_on = var.enable_workspace_deployer && var.enable_workspace_kinds

  # modules/frontend/main.tf's names for name_prefix: `${name_prefix}-ui` is
  # its local.name, the API backend is `<local.name>-backend`, the console's
  # `<local.name>-ui-backend`. Read through wif.tf's data source, so a rename
  # there fails this plan at the lookup instead of admitting at a stale name.
  front_door_api_backend = "${var.name_prefix}-ui-backend"

  # K1's resource, in the form IAP's IAM evaluates (§4.5): the project NUMBER
  # and the backend's numeric id, both read rather than spelled. "" when the
  # API backend is not in frontend_iap_backends; the grant's precondition
  # refuses that rather than make a binding that names nothing.
  front_door_api_resource = local.workspace_kinds_on && contains(var.frontend_iap_backends, local.front_door_api_backend) ? "projects/${local.workspace_project_number}/iap_web/compute/services/${data.google_compute_backend_service.frontend_iap[local.front_door_api_backend].generated_id}" : ""

  workspace_kinds_conditions = {
    front_door_admit = join(" && ", concat(
      ["resource.name == \"${local.front_door_api_resource}\""],
      var.front_door_admit_role_bound ? ["api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([\"roles/iap.httpsResourceAccessor\"])"] : [],
    ))

    # Every tenant's team slot and its twin, by full name. A secret's IAM is
    # evaluated on the secret, `projects/<n>/secrets/<id>`, so the suffix is
    # the secret id's own end: `-git-u-<hash>`, `-anthropic`, `-wsec-<hex>`
    # all end otherwise.
    team_slot_bind = "resource.name.startsWith(\"projects/${local.workspace_project_number}/secrets/swarm-tenant-\") && (resource.name.endsWith(\"-git\") || resource.name.endsWith(\"-git-refresh\"))"
  }

  # K1 is held only with the switch AND its role on workspace_deployer_roles:
  # the admitter's own count, below, spelled the same way.
  front_door_admitter_on = local.workspace_kinds_on && lookup(local.workspace_role_on, "swarmFrontDoorAdmitter", false)

  # The API backend's name as IAP's resource-level IAM takes it. "" when the
  # backend is not in frontend_iap_backends, so the lookup cannot fail first:
  # the reader's precondition refuses that, in words.
  front_door_api_backend_name = contains(var.frontend_iap_backends, local.front_door_api_backend) ? data.google_compute_backend_service.frontend_iap[local.front_door_api_backend].name : ""

  # Not in workspace_custom_roles: those are module.custom_role_ids' ids,
  # which terraform/infra shares. This role is defined and granted here alone,
  # and terraform/infra never names it, so its id is spelled here as
  # mcp_credentials.tf spells its own.
  front_door_admitter_role_id = "swarmFrontDoorAdmitter"
}

resource "google_project_iam_custom_role" "front_door_admitter" {
  count = local.workspace_kinds_on && lookup(local.workspace_role_on, "swarmFrontDoorAdmitter", false) ? 1 : 0

  project     = var.project_id
  role_id     = local.front_door_admitter_role_id
  title       = "Swarm Front Door Admitter"
  description = "managed-by=swarm-terraform; read and set the IAP policy of the swarm API backend to admit a service workspace's account (docs/design/workspace-kinds.md §4.5 K1). Granted on that one backend by exact name. No iap.web.*, no tunnel, no admin."
  stage       = "GA"
  permissions = [
    "iap.webServices.getIamPolicy",
    "iap.webServices.setIamPolicy",
  ]
}

resource "google_project_iam_member" "front_door_admitter" {
  count = length(google_project_iam_custom_role.front_door_admitter)

  project = var.project_id
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.front_door_admitter[0].role_id}"
  member = local.workspace_deployer_member

  condition {
    title       = "swarm API backend only"
    description = "managed-by=swarm-terraform; K1: the API backend's IAP policy, by exact name; never the console's -ui-backend or another team's backend (docs/design/workspace-kinds.md §4.5)."
    expression  = local.workspace_kinds_conditions.front_door_admit
  }

  lifecycle {
    precondition {
      condition     = contains(var.frontend_iap_backends, local.front_door_api_backend)
      error_message = "K1 is made on the API backend (${local.front_door_api_backend}), which frontend_iap_backends does not list. On a fresh project leave enable_workspace_kinds off until the release has created the backends and frontend_iap_backends names them."
    }
  }

  depends_on = [google_service_account.workspace_deployer]
}

# ---------------------------------------------------------------------------
# FRONT-DOOR-READ (part of #847, owner decision 2026-10-20): the READ of the
# API backend's accessor list, never a change to it.
# ---------------------------------------------------------------------------
#
# O1 (2): failed (docs/workspaces.md §0), so K1 is not granted and S2 cannot
# admit an account. But S2 begins with `gcloud iap web get-iam-policy` on the
# API backend (scripts/register-tenant.sh ws_s2), and A9 reads it back, and
# without even the read every service workspace stopped at FRONT_DOOR_OWNER
# -- the QA console workspace w-267d01 included, on 2026-10-20, though its
# account qa-console@ was already admitted through frontend_iap_members. With
# the read, an account the owner has admitted passes S2 and the run finishes;
# one not yet admitted still stops for the owner, because the change
# (setIamPolicy) is K1's and this role does not carry it.
#
# WHY IT IS SAFE: it shows who may pass IAP on one backend -- the accessor
# list frontend_iap_members already writes in this repository -- and changes
# nothing. No setIamPolicy, no settings, no other backend.
#
# WHERE IT IS BOUND: on that backend's OWN IAP policy, a resource-level
# binding, never the project. A project grant is what O1 found Google would
# not limit to one backend, and in this shared project it would reach the
# other team's IAP backends too. Never the console's -ui-ui-backend: no
# service is admitted there, so the job never reads it. No condition: the
# binding names the one resource itself.
#
# OFF WHILE K1 IS ON: swarmFrontDoorAdmitter already carries
# iap.webServices.getIamPolicy, so a second grant of the read would only be a
# row to keep in step.
#
# VERIFIED LIVE 2026-10-20: applied at 06:09Z, and the retried QA console
# workspace w-267d01 passed S2 at 06:11Z ("front door: swarm-ui-backend
# already admits the account"). modules/frontend/main.tf records that
# roles/iap.admin bound on our two backends was still 403 on getIamPolicy
# nine and a half minutes later (2026-09-24), and O1 noted the same; why that
# was refused and this custom role is not is not known. If a later apply is
# refused that way, nothing is lost: S2 stops for the owner as before. The
# check is one read as the deployer:
# `gcloud iap web get-iam-policy --resource-type=backend-services
# --service=<API backend>`.

resource "google_project_iam_custom_role" "front_door_reader" {
  count = local.workspace_kinds_on && !local.front_door_admitter_on ? 1 : 0

  project     = var.project_id
  role_id     = module.custom_role_ids.ids["front_door_reader"]
  title       = "Swarm Front Door Reader"
  description = "managed-by=swarm-terraform; read the IAP accessor list of the swarm API backend, so a service workspace the owner admitted passes S2 (docs/workspaces.md §0). Bound on that backend only. No setIamPolicy, no settings."
  stage       = "GA"
  permissions = [
    "iap.webServices.getIamPolicy",
  ]
}

resource "google_iap_web_backend_service_iam_member" "front_door_reader" {
  count = length(google_project_iam_custom_role.front_door_reader)

  project             = var.project_id
  web_backend_service = local.front_door_api_backend_name
  # Through the resource, so the role exists before it is granted.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.front_door_reader[0].role_id}"
  member = local.workspace_deployer_member

  lifecycle {
    precondition {
      condition     = contains(var.frontend_iap_backends, local.front_door_api_backend)
      error_message = "The front-door read is bound on the API backend (${local.front_door_api_backend}), which frontend_iap_backends does not list. On a fresh project leave enable_workspace_kinds off until the release has created the backends and frontend_iap_backends names them."
    }
  }

  depends_on = [google_service_account.workspace_deployer]
}

resource "google_project_iam_member" "workspace_team_slot_binder" {
  count = local.workspace_kinds_on && lookup(local.workspace_role_on, "swarmWorkspaceSecretBinder", false) ? 1 : 0

  project = var.project_id
  role    = module.custom_role_ids.names.workspace_secret_binder
  member  = local.workspace_deployer_member

  condition {
    title       = "team forge slots only"
    description = "managed-by=swarm-terraform; K2: a group's team slot swarm-tenant-<g>-git and its -git-refresh twin (docs/design/workspace-kinds.md §4.5, §4.6)."
    expression  = local.workspace_kinds_conditions.team_slot_bind
  }

  depends_on = [
    google_project_iam_custom_role.workspace,
    google_service_account.workspace_deployer,
  ]
}

output "workspace_kinds_grants" {
  description = "What the owner reads a plan against for the workspace-kinds rows (docs/design/workspace-kinds.md §4.5): K1's backend and condition, the backend the front-door read is bound on (null while K1 is held), K2's and K3's conditions. Null while enable_workspace_kinds is off."
  value = local.workspace_kinds_on ? {
    front_door_backend = local.front_door_api_backend
    front_door_admit   = local.workspace_kinds_conditions.front_door_admit
    front_door_reader  = length(google_iap_web_backend_service_iam_member.front_door_reader) > 0 ? google_iap_web_backend_service_iam_member.front_door_reader[0].web_backend_service : null
    team_slot_bind     = local.workspace_kinds_conditions.team_slot_bind
    member_slots       = local.forge_member_slots_any
  } : null
}
