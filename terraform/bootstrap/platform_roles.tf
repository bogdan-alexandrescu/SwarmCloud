# ---------------------------------------------------------------------------
# THE PLATFORM'S CUSTOM ROLES, AND THE QUOTA BROKER'S swarmSecretLister GRANT
# ---------------------------------------------------------------------------
#
# Owner decisions, 2026-09-25 (#79, #69):
#
#   * roles/iam.roleAdmin comes OFF the CI deployer (variables.tf,
#     deployer_roles). roleAdmin's iam.roles.update cannot be conditioned -- IAM
#     names no role in a condition, and a role carries no allow policy of its
#     own -- so while CI held it, one update could add
#     resourcemanager.projects.setIamPolicy to a custom role CI already held
#     unconditioned (swarmSecretProvisioner, swarmDeployerProjectBuckets), and
#     CI's next project setIamPolicy, roles/owner included, was authorised by
#     THAT binding. The scoped projectIamAdmin (#68) would never be evaluated.
#   * so every custom role terraform/infra and its modules defined is defined
#     HERE, in the root the owner applies. Changing one of their permissions is
#     an owner bootstrap apply, not a release; the owner accepted that cost.
#   * the broker's swarmSecretLister grant is made here too, and the role is off
#     deployer_grantable_project_roles (deployer_conditions.tf), so the scoped
#     projectIamAdmin cannot admit a grant of it. The role carries project-wide
#     secrets.setIamPolicy, which reaches the other team's 63 secrets; while it
#     was grantable, CI could grant it to itself, unconditioned.
#
# THE EIGHT ROLES ARE BYTE FOR BYTE WHAT terraform/infra DEFINED -- ids, titles,
# descriptions, stage and permission sets. tests/terraform/platform_roles.tftest.hcl
# holds them to platform_custom_roles_before_the_move.json, read from the live
# project on 2026-09-25 and held to main's module definitions in CI before the
# move (by a test file the move deleted: `git show
# 15856da:tests/terraform/platform_roles_fixture.tftest.hcl`, terraform run
# 36118571454, four of four passed). The comments on each permission moved with
# it, unchanged.
#
# terraform/infra still GRANTS seven of them (the bindings stayed where they
# were), naming each by the id in terraform/modules/custom_role_ids -- the one
# spelling both roots read, so the role made here and the role bound there
# cannot drift apart.
#
# ---------------------------------------------------------------------------
# ADOPTING THE LIVE ROLES, AND THE ORDER THAT MAKES IT SAFE
# ---------------------------------------------------------------------------
#
# The eight roles and the grant exist today, in terraform/infra's remote state.
# The move is three steps, and docs/runbooks/custom-roles-to-bootstrap.md has
# the commands:
#
#   1. the merge's release applies terraform/infra, whose `removed` blocks
#      (custom_roles_moved_to_bootstrap.tf there) make it FORGET the nine
#      objects without touching them, and write the output custom_roles_owner;
#   2. the owner applies this root: the `import` blocks below ADOPT the nine,
#      and the deployer's roleAdmin binding is destroyed;
#   3. the next release plans and applies with no custom role in its plan.
#
# STEP 2 BEFORE STEP 1 IS REFUSED, not documented. Imported while infra still
# holds them, the roles would be managed from two states -- and once roleAdmin
# is gone, infra's next plan 403s refreshing roles it still thinks it owns,
# which blocks every release until someone edits a state file by hand. So each
# resource below carries a precondition: for every infra state named in
# adopt_from_infra_states, that state's custom_roles_owner output must read
# "terraform/bootstrap". An output reaches a state only through an APPLY, and
# the apply that writes this one is the one that runs the `removed` blocks. A
# plan (terraform.yml's `plan (dev)`) writes nothing, so it cannot satisfy it.
#
# ON A FRESH PROJECT nothing exists to adopt: leave adopt_from_infra_states
# empty (its default), so there are no imports and no state to read, and set
# grant_broker_secret_lister = false for the first apply, before terraform/infra
# has created swarm-quota-broker; set it back once it has. The roles themselves
# must exist before terraform/infra's first apply, which grants them.

module "custom_role_ids" {
  source = "../modules/custom_role_ids"

  project_id = var.project_id
}

locals {
  # The predefined alternatives are all too wide for this platform:
  #
  #   roles/run.developer also grants run.services.* -- the scheduler could
  #   redeploy the API it is called by.
  #   roles/run.admin adds setIamPolicy, so the dispatcher could grant itself
  #   anything on any Cloud Run resource.
  #   roles/container.developer covers every workload type in every namespace,
  #   including Secrets.
  #
  # Each role below is the exact permission set one component needs and nothing
  # beyond it. Note what is absent from every one of them but swarmSecretLister:
  # setIamPolicy -- and that role's is on secrets only, argued for below. And no
  # delete permission outside the reconciler's roles, except the broker's
  # versions.destroy, also argued for below.
  #
  # key -> the role. The key is the one terraform/modules/custom_role_ids names
  # the role's id by.
  platform_roles = {

    # modules/iam grants this to swarm-scheduler, unconditioned.
    job_dispatcher = {
      title       = "Swarm Job Dispatcher"
      description = "Create and run Cloud Run Job resources. No delete, no IAM, no services."

      permissions = [
        # Per-tenant-per-profile Job resources: Cloud Run pins the service account
        # on the Job, not the execution, so the dispatcher must be able to create
        # them (CONTRACT.md, platform decisions).
        "run.jobs.create",
        "run.jobs.get",
        "run.jobs.list",
        "run.jobs.update",
        "run.jobs.run",
        "run.jobs.runWithOverrides",
        "run.executions.get",
        "run.executions.list",
        "run.tasks.get",
        "run.tasks.list",
        "run.operations.get",
        "run.operations.list",
      ]
    }

    # modules/iam grants this to swarm-reconciler, unconditioned.
    job_reaper = {
      title       = "Swarm Job Reaper"
      description = "Cancel and delete executions, garbage-collect unused Job resources. Reconciler only."

      permissions = [
        "run.jobs.get",
        "run.jobs.list",
        "run.jobs.delete",
        "run.executions.get",
        "run.executions.list",
        "run.executions.cancel",
        "run.executions.delete",
        "run.tasks.get",
        "run.tasks.list",
        "run.operations.get",
        "run.operations.list",
      ]
    }

    # modules/iam grants this to swarm-scheduler, conditioned to the swarm
    # cluster. It used to exist only when terraform/infra enabled GKE; this
    # root cannot see that setting, so it is defined either way -- a role
    # nobody is bound to grants nothing.
    gke_dispatcher = {
      title       = "Swarm GKE Dispatcher"
      description = "Create and observe Jobs on the swarm Autopilot cluster. No Secret access, no cluster mutation."

      permissions = [
        # clusters.get is what `gcloud container clusters get-credentials` needs;
        # it returns the endpoint and CA, not any workload data.
        "container.clusters.get",
        "container.namespaces.get",
        "container.namespaces.list",
        "container.jobs.create",
        "container.jobs.get",
        "container.jobs.list",
        "container.jobs.update",
        "container.pods.get",
        "container.pods.list",
        "container.pods.getLogs",
        "container.events.get",
        "container.events.list",
      ]
    }

    # modules/iam grants this to swarm-reconciler, conditioned to the swarm
    # cluster. Defined whether or not GKE is on, as above.
    gke_reaper = {
      title       = "Swarm GKE Reaper"
      description = "Delete finished Jobs and stuck Pods on the swarm Autopilot cluster. Reconciler only."

      permissions = [
        "container.clusters.get",
        "container.namespaces.get",
        "container.jobs.get",
        "container.jobs.list",
        "container.jobs.delete",
        "container.pods.get",
        "container.pods.list",
        "container.pods.delete",
        "container.events.list",
      ]
    }

    # Granted to swarm-quota-broker by THIS root (broker_secret_lister, below),
    # not by terraform/infra (#69).
    secret_lister = {
      title       = "Swarm Secret Lister and Account Provisioner"
      description = "List secret metadata project-wide, and create the two secrets an account pool entry needs. Cannot read any payload."

      # The quota broker must discover which tenants hold a subscription
      # credential, and discovery is a list. roles/secretmanager.viewer would do it,
      # but it also grants versions.list and versions.get across every secret in the
      # project, including secrets belonging to teams that have nothing to do with
      # this platform. This is the single permission discovery actually needs.
      #
      # Absent, and deliberately: secretmanager.versions.access. Payload access
      # stays per-secret, granted by the secret_manager module on the specific
      # refresh secrets, so a bug here cannot widen into reading tenant keys.
      permissions = concat([
        "secretmanager.secrets.list",

        # PROVISIONING an account's secrets, added when account management moved
        # into the Settings page and stopped being a shell script.
        #
        # A secret for a pool account cannot be created ahead of time: its name
        # contains a LABEL the operator chooses at registration, so terraform
        # cannot declare it and only the component handling the registration can
        # make it. That component is this one, because it is already the single
        # writer for subscription credentials -- see quota_broker.credentials for
        # why a second writer bricks a rotating credential.
        #
        # `setIamPolicy` is here for the same reason `create` is. A secret created
        # without an accessor binding is one the tenant's pod cannot read, and the
        # failure surfaces much later as an unexplained auth error inside a job.
        # Creating it and binding it are one operation or the secret is useless.
        "secretmanager.secrets.create",
        "secretmanager.secrets.get",
        "secretmanager.secrets.getIamPolicy",
        "secretmanager.secrets.setIamPolicy",

        # RETENTION, added 2026-09-22 on the owner's explicit decision.
        #
        # Without these the broker cannot expire what it supersedes, and it never
        # could: swarm-tenant-u-bogdan-anthropic reached 1,816 versions, ALL
        # ENABLED, ZERO destroyed, because nothing in this platform had ever
        # expired one. Only `latest` is ever read, so 1,815 of those were dead
        # credentials that stayed retrievable by anything holding accessor. A
        # credential that rotates but leaves its predecessor enabled has not
        # rotated -- the same point create-secrets.sh makes beside
        # `--disable-previous`.
        #
        # `list` is needed before `destroy`: retention recomputes the retained set
        # from a live listing on every publish rather than recording state.
        #
        # THE SCOPE IS PROJECT-WIDE AND THAT IS A DELIBERATE, INFORMED CHOICE, not
        # an oversight. saga-agents-staging is SHARED, so this permits the broker
        # to destroy a version of any secret in it, including another team's. The
        # owner chose this over a per-secret binding on 2026-09-22 having been shown
        # that trade-off explicitly.
        #
        # WHAT ACTUALLY STOPS IT is therefore no longer IAM but
        # `quota_broker.secretstore.owned_by_this_platform`, which matches
        # \Aswarm-(?:tenant|account)-[A-Za-z0-9_-]+\Z -- anchored with \A/\Z
        # rather than ^/$ so a trailing newline cannot smuggle a second name past
        # it, and admitting no `/` so a name cannot re-point the resource path at
        # another secret or project. It was attacked with thirteen hostile inputs
        # on 2026-09-22 -- newline injection, traversal, full resource paths,
        # lookalike prefixes -- and refused all of them.
        #
        # If that guard is ever weakened, this grant becomes the hole. Do not widen
        # one without re-reading the other.
        "secretmanager.versions.list",
        "secretmanager.versions.destroy",
        ],
        # versions.add, while it is still project-wide. The scoped replacement is
        # terraform/infra's broker_version_adder (modules/iam/custom_roles.tf),
        # already live; see broker_secret_lister_project_wide_versions_add.
        var.broker_secret_lister_project_wide_versions_add ? ["secretmanager.versions.add"] : [],
      )
    }

    # modules/tenancy grants this to every tenant worker, unconditioned.
    #
    # Firestore IAM has NO collection- or document-level granularity: the
    # smallest resource a binding can name is the database, and a worker running
    # attacker-controlled code -- the normal case, not the exceptional one -- is
    # inside that scope. What IS available at this layer is the shape of the
    # access, so the role is built rather than borrowed. roles/datastore.user
    # grants entities.delete and entities.list on top of what a worker needs.
    # Removing them removes the two capabilities that turn "can touch the shared
    # database" into the worst version of itself:
    #
    #   entities.delete -- a hostile worker could delete another tenant's tasks,
    #   leases and attempts, and the pool documents the whole platform admits
    #   against. Nothing in the worker path deletes a document: agent_worker.control
    #   and swarm_common.admission are get/set/update only.
    #
    #   entities.list -- queries. Without it, a document can only be fetched by an
    #   id already known, and task, attempt and lease ids are `<prefix>_<20 hex>`.
    #   So a hostile worker cannot ENUMERATE other tenants' work: no dumping every
    #   prompt, repo URL and artifact path in the database. The worker path runs no
    #   queries at all; every read is a document ref built from an id the dispatcher
    #   handed this attempt (agent_worker.control, agent_worker.secrets.load_tenant).
    #
    # modules/tenancy/main.tf records the residual this leaves.
    worker_firestore = {
      title       = "Swarm Tenant Worker Firestore"
      description = "Read and write control-plane documents by id. No deletes, no queries."

      permissions = [
        # The client library resolves the named database before its first call.
        "datastore.databases.get",
        "resourcemanager.projects.get",
        # Document get / set / update, which is the entire worker data path.
        "datastore.entities.get",
        "datastore.entities.create",
        "datastore.entities.update",
      ]
    }

    # modules/tenancy grants this to every tenant worker, on the artifact bucket.
    #
    # Cloud Storage FUSE and the client libraries both need the bucket's own
    # metadata, and a prefix condition can never match the bucket resource name.
    # Granting legacyBucketReader instead would hand over objects.list across the
    # WHOLE bucket, which is exactly the isolation that module exists to keep.
    bucket_metadata_reader = {
      title       = "Swarm Bucket Metadata Reader"
      description = "storage.buckets.get only. Enough to mount, not enough to enumerate."

      permissions = ["storage.buckets.get"]
    }

    # modules/artifact_registry grants this to every tenant worker, on the
    # swarm-images repository. Pull without enumerate: image_puller_permissions
    # (variables.tf) says what is in it and refuses what may never be. It used to
    # exist only while a tenant existed; this root cannot see tenants, so it is
    # defined either way, and a role nobody is bound to grants nothing.
    image_puller = {
      title       = "Swarm Image Puller"
      description = "Pull an image by a name already held. Cannot enumerate the repository, cannot push."

      permissions = var.image_puller_permissions
    }
  }
}

resource "google_project_iam_custom_role" "platform" {
  for_each = local.platform_roles

  project = var.project_id
  role_id = module.custom_role_ids.ids[each.key]
  title   = each.value.title

  description = each.value.description
  stage       = "GA"

  permissions = each.value.permissions

  lifecycle {
    precondition {
      condition     = length(local.infra_states_holding_platform_roles) == 0
      error_message = "terraform/infra state ${join(", ", local.infra_states_holding_platform_roles)} has not released the platform's custom roles: its custom_roles_owner output does not read terraform/bootstrap. That output is written by the release that applies terraform/infra/custom_roles_moved_to_bootstrap.tf, whose `removed` blocks make infra forget these roles. Adopting them first would leave every role managed from two states, and infra's next plan would 403 refreshing them once roleAdmin is gone. Wait for that release's `terraform apply (dev)` to succeed, then plan again (docs/runbooks/custom-roles-to-bootstrap.md, step 2)."
    }
  }
}

# ---------------------------------------------------------------------------
# swarm-quota-broker's swarmSecretLister grant (#69).
#
# The account is terraform/infra's (modules/iam, google_service_account.platform
# ["swarm-quota-broker"]), so it is named here, not read: this root cannot
# depend on that one. tests/terraform/platform_roles.tftest.hcl holds this
# account id to modules/iam's output quota_broker_account_id.
#
# Unconditioned, as it was in terraform/infra: the broker lists secrets
# project-wide and creates secrets whose names it chooses at registration, so no
# resource.name condition could admit its calls. What bounds it is
# quota_broker.secretstore.owned_by_this_platform, as the role's comments say.
# ---------------------------------------------------------------------------
locals {
  broker_account_id = "swarm-quota-broker"

  broker_secret_lister_grants = var.grant_broker_secret_lister ? tomap({
    (local.broker_account_id) = "serviceAccount:${local.broker_account_id}@${var.project_id}.iam.gserviceaccount.com"
  }) : tomap({})
}

resource "google_project_iam_member" "broker_secret_lister" {
  for_each = local.broker_secret_lister_grants

  project = var.project_id

  # Through the resource, so the role is made before it is granted; role_id is
  # configuration, so the name is still known at plan.
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.platform["secret_lister"].role_id}"
  member = each.value

  lifecycle {
    precondition {
      condition     = length(local.infra_states_holding_platform_roles) == 0
      error_message = "terraform/infra state ${join(", ", local.infra_states_holding_platform_roles)} has not released the broker's swarmSecretLister grant (its custom_roles_owner output does not read terraform/bootstrap). Wait for the release that applies terraform/infra/custom_roles_moved_to_bootstrap.tf, then plan again (docs/runbooks/custom-roles-to-bootstrap.md, step 2)."
    }
  }
}

# ---------------------------------------------------------------------------
# The adoption: import blocks, and the state they wait for.
# ---------------------------------------------------------------------------

# Every infra state the roles are adopted from -- terraform/infra's remote state
# in the bucket this root creates, at the prefix scripts/bootstrap.sh gives it
# (infra/<environment>). Only its outputs are read; the data source stores them
# in this root's local state, and none of terraform/infra's outputs is sensitive.
data "terraform_remote_state" "infra" {
  for_each = toset(var.adopt_from_infra_states)

  backend = "gcs"
  config = {
    bucket = local.state_bucket_name
    prefix = each.value
  }
}

locals {
  # Named infra states that still manage the roles, as far as their outputs
  # say. Empty when adopt_from_infra_states is: nothing to wait for.
  infra_states_holding_platform_roles = [
    for prefix, s in data.terraform_remote_state.infra : prefix
    if try(s.outputs.custom_roles_owner, "") != "terraform/bootstrap"
  ]
}

# Import ids, from the provider's documentation (hashicorp/google 6.50.0, the
# version .terraform.lock.hcl pins): a custom role is
# `projects/{{project}}/roles/{{role_id}}`; a project IAM member is
# `"{{project_id}} {{role}} {{member}}"`, space-separated, with a custom role
# named in full and -- only for a conditioned member -- the condition's title
# appended. The broker's grant has no condition (read-only
# `gcloud projects get-iam-policy`, 2026-09-25 08:52 UTC).
#
# Once the objects are in this root's state, Terraform skips an import block
# whose target it already manages, so these stay harmless after step 2.
import {
  for_each = length(var.adopt_from_infra_states) > 0 ? toset(keys(local.platform_roles)) : toset([])

  to = google_project_iam_custom_role.platform[each.key]
  id = "projects/${var.project_id}/roles/${module.custom_role_ids.ids[each.key]}"
}

import {
  for_each = length(var.adopt_from_infra_states) > 0 ? local.broker_secret_lister_grants : tomap({})

  to = google_project_iam_member.broker_secret_lister[each.key]
  id = "${var.project_id} ${module.custom_role_ids.names.secret_lister} ${each.value}"
}
