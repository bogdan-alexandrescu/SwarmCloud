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
# re-decision. The repository connection in cloudbuild_connection.tf stays
# until the job has shipped (lane W11), and nothing here reads it.
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

  # The job's command: isolated-mode Python running the scrub (§2.2 step 0),
  # which the image puts root-owned under /opt/swarm (images/workspace-apply,
  # W6b). A literal held by tests/terraform/workspace_deployer.tftest.hcl: the
  # command is the one part of an execution no caller can override, so it is
  # what makes the scrub run first.
  workspace_apply_command = ["python3", "-I", "/opt/swarm/entry.py"]

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
