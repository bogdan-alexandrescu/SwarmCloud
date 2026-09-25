# The platform's eight custom roles, and the quota broker's swarmSecretLister
# grant, as terraform/bootstrap defines them since #79 and #69 (owner decisions,
# 2026-09-25).
#
# THE MOVE MUST CHANGE NOTHING LIVE, so the first runs hold every role to
# platform_custom_roles_before_the_move.json: the eight as terraform/infra and
# its modules defined them on origin/main (89f2e09), read from the live project
# with `gcloud iam roles describe` on 2026-09-25. Before this change,
# platform_roles_fixture.tftest.hcl held the same file to main's module
# definitions in CI (the red-first commit on this change's pull request), so the
# comparison below is against what the roles WERE, not against a copy of what
# bootstrap now says. A different id, title, description, stage or permission
# here is a change to a live role the owner's apply would make.
#
# The permission PROPERTIES each role was argued for -- the reaper deletes and
# the dispatcher cannot, no role reads a secret payload, no worker role can
# enumerate or delete -- moved here from iam.tftest.hcl, tenancy.tftest.hcl,
# observability.tftest.hcl and broker_secret_scope.tftest.hcl with the roles.
#
# The last runs hold the adoption: the import blocks wait until terraform/infra
# has let go of the roles, and proceed once it has.

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]
}

run "fixture" {
  command = plan

  module {
    source = "./platform_roles_fixture"
  }

  # Exactly the eight, so a role left out of the file cannot pass a comparison
  # by being absent from both sides.
  assert {
    condition = output.path != "" && toset(keys(output.roles)) == toset([
      "bucket_metadata_reader",
      "gke_dispatcher",
      "gke_reaper",
      "image_puller",
      "job_dispatcher",
      "job_reaper",
      "secret_lister",
      "worker_firestore",
    ])
    error_message = "platform_custom_roles_before_the_move.json was not found, or does not name exactly the eight custom roles terraform/infra defined"
  }
}

run "bootstrap_defines_the_eight_roles_exactly_as_terraform_infra_did" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = toset(keys(google_project_iam_custom_role.platform)) == toset(keys(run.fixture.roles))
    error_message = "terraform/bootstrap must define exactly the eight custom roles terraform/infra defined: one missing is a role the release forgets and nobody manages, one extra is a role nobody decided on"
  }

  assert {
    condition = alltrue([
      for key, r in google_project_iam_custom_role.platform : alltrue([
        r.role_id == run.fixture.roles[key].role_id,
        r.title == run.fixture.roles[key].title,
        r.description == run.fixture.roles[key].description,
        r.stage == run.fixture.roles[key].stage,
        r.permissions == run.fixture.roles[key].permissions,
      ])
    ])
    error_message = "a role terraform/bootstrap defines differs from the role terraform/infra defined (id, title, description, stage or permissions): the owner's bootstrap apply would change it live, which the move must not do"
  }

  assert {
    condition = alltrue([
      for key, r in google_project_iam_custom_role.platform : r.project == "saga-agents-staging"
    ])
    error_message = "every platform role is defined in the project whose bindings name it"
  }
}

# ---------------------------------------------------------------------------
# swarm-quota-broker's swarmSecretLister grant (#69).
# ---------------------------------------------------------------------------

run "modules_iam_names_the_broker" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    artifact_bucket  = "swarm-artifacts-saga-agents-staging"
    labels           = { "managed-by" = "swarm-terraform" }
    gke_cluster_name = "swarm-autopilot"
    gke_location     = "us-central1"
  }

  assert {
    condition     = output.quota_broker_account_id != ""
    error_message = "modules/iam must say which account is the quota broker"
  }
}

run "bootstrap_grants_the_broker_swarm_secret_lister" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = toset(keys(google_project_iam_member.broker_secret_lister)) == toset(["swarm-quota-broker"])
    error_message = "terraform/bootstrap must make the broker's swarmSecretLister grant, once"
  }

  assert {
    condition     = google_project_iam_member.broker_secret_lister["swarm-quota-broker"].role == "projects/saga-agents-staging/roles/swarmSecretLister"
    error_message = "the broker's grant must be swarmSecretLister, by the name terraform/infra granted it under"
  }

  # The account modules/iam creates. Bootstrap cannot read it (the account is
  # terraform/infra's), so it names it, and this is where the two are compared.
  assert {
    condition     = google_project_iam_member.broker_secret_lister["swarm-quota-broker"].member == "serviceAccount:${run.modules_iam_names_the_broker.quota_broker_account_id}@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "terraform/bootstrap grants swarmSecretLister to an account other than the quota broker modules/iam creates"
  }

  # Unconditioned, as it was in terraform/infra. A condition added here is a
  # change to what the broker can do, not part of the move.
  assert {
    condition     = length(google_project_iam_member.broker_secret_lister["swarm-quota-broker"].condition) == 0
    error_message = "the broker's swarmSecretLister grant was unconditioned in terraform/infra; the move must not change it"
  }
}

# terraform/infra/custom_roles_moved_to_bootstrap.tf hands the broker's grant
# over with a `moved` block whose `from` is the grant's old for_each key in
# modules/iam's `plain`, spelled literally because a `moved` address cannot be
# computed. A key that matches nothing makes the release DESTROY the grant
# rather than forget it, so the literal is held here to the names both roots
# read -- `"<account>:<role name>"`, the key format bindings.tf builds.
run "the_moved_block_names_the_broker_grant_by_its_old_key" {
  command = plan

  module {
    source = "../../terraform/modules/custom_role_ids"
  }

  assert {
    condition     = strcontains(file("../../terraform/infra/custom_roles_moved_to_bootstrap.tf"), "from = module.iam.google_project_iam_member.plain[\"${run.modules_iam_names_the_broker.quota_broker_account_id}:${output.names.secret_lister}\"]")
    error_message = "the `moved` block in terraform/infra/custom_roles_moved_to_bootstrap.tf does not name the broker's swarmSecretLister grant by the key modules/iam gave it; the release would destroy that grant instead of forgetting it"
  }
}

run "a_fresh_project_grants_the_broker_nothing_until_it_exists" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    grant_broker_secret_lister = false
  }

  assert {
    condition     = length(google_project_iam_member.broker_secret_lister) == 0
    error_message = "with grant_broker_secret_lister off there is no grant, so a first apply does not name an account terraform/infra has not created"
  }

  # The roles still exist: terraform/infra's first apply grants them.
  assert {
    condition     = length(google_project_iam_custom_role.platform) == 8
    error_message = "the roles must exist before terraform/infra's first apply, which grants them"
  }
}

# ---------------------------------------------------------------------------
# The permission properties each role was argued for. Moved here with the
# roles; the fixture comparison above pins the exact sets, these say why.
# ---------------------------------------------------------------------------

run "only_the_reconciler_s_roles_can_delete" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = contains(google_project_iam_custom_role.platform["job_reaper"].permissions, "run.jobs.delete")
    error_message = "the reaper role is what makes deletion possible at all"
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.platform["job_dispatcher"].permissions, "run.jobs.delete")
    error_message = "the dispatcher may create and run Jobs; deleting them belongs to the reconciler alone"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.platform["job_dispatcher"].permissions :
      !strcontains(p, "setIamPolicy") && !strcontains(p, "delete")
    ])
    error_message = "no delete and no setIamPolicy in the dispatcher role, or it could grant itself anything"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.platform["job_reaper"].permissions :
      !strcontains(p, "setIamPolicy")
    ])
    error_message = "even the reaper may not rewrite an IAM policy"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.platform["gke_dispatcher"].permissions :
      !strcontains(p, "delete") && !strcontains(p, "secrets")
    ])
    error_message = "the GKE dispatcher may create and observe Jobs; it may neither delete nor read a Kubernetes Secret"
  }
}

# The broker enumerates tenants to find whose subscription credential is due.
# Enumeration is a list, and a list is all it gets: the ability to READ a
# credential is granted per-secret by the secret_manager module, on the refresh
# secrets only. If that separation ever collapses into one project-wide role,
# a bug in the broker stops being a failed refresh and becomes every tenant's
# provider key at once.
run "the_broker_can_provision_account_secrets_and_read_none" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # Pinned as an exact SET, not a subset check. The point of a custom role here
  # is that every permission in it was argued for; asserting "contains x" would
  # let an unrelated one be added silently, which is how a narrow role becomes
  # roles/secretmanager.admin one commit at a time.
  #
  # `create` and `setIamPolicy` arrived when account management moved into the
  # Settings page: a pool account's secret name contains a LABEL chosen at
  # registration, so terraform cannot declare it and the component handling the
  # registration has to make it -- and a secret created without an accessor
  # binding is one the tenant's pod cannot read.
  assert {
    condition = google_project_iam_custom_role.platform["secret_lister"].permissions == toset([
      "secretmanager.secrets.list",
      "secretmanager.secrets.create",
      "secretmanager.secrets.get",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
      "secretmanager.versions.add",
      # RETENTION, 2026-09-22, on the owner's explicit decision. Nothing had
      # ever expired a superseded version, so one secret reached 1,816 of them,
      # all ENABLED, while only `latest` was ever read -- 1,815 dead
      # credentials left retrievable. `list` is needed before `destroy`
      # because retention recomputes the retained set from a live listing
      # rather than recording state.
      #
      # These are METADATA and LIFECYCLE, not payload: neither reveals a
      # secret's contents, and the two assertions below still refuse
      # `versions.get` and `versions.access`. What they do carry is reach --
      # project-wide, on a SHARED project. The owner chose that over a
      # per-secret binding having been shown the trade. What stops the broker
      # touching another team's secret is therefore
      # `quota_broker.secretstore.owned_by_this_platform`, not this role.
      "secretmanager.versions.list",
      "secretmanager.versions.destroy",
    ])
    error_message = "every permission in this role was argued for; adding one silently is how it becomes secretmanager.admin"
  }

  # THE LINE THAT MUST NOT MOVE. The broker writes credentials and binds their
  # readers; it never reads a payload project-wide. `versions.add` is
  # write-only, and payload access stays per-secret.
  assert {
    condition     = !contains(google_project_iam_custom_role.platform["secret_lister"].permissions, "secretmanager.versions.get")
    error_message = "reading a version project-wide would defeat per-tenant secret isolation"
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.platform["secret_lister"].permissions, "secretmanager.versions.access")
    error_message = "a project-wide payload read would defeat per-tenant secret isolation entirely"
  }
}

# The second of broker_secret_scope.tftest.hcl's two steps, now an owner
# bootstrap apply rather than a release: the scoped replacement
# (broker_version_adder in modules/iam) is already live.
run "dropping_the_project_wide_versions_add_removes_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    broker_secret_lister_project_wide_versions_add = false
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.platform["secret_lister"].permissions, "secretmanager.versions.add")
    error_message = "with the switch off, swarmSecretLister must no longer carry versions.add project-wide"
  }

  # Everything else the role was argued for stays, including the owner's
  # 2026-09-22 retention decision (versions.list and versions.destroy).
  assert {
    condition = google_project_iam_custom_role.platform["secret_lister"].permissions == toset([
      "secretmanager.secrets.list",
      "secretmanager.secrets.create",
      "secretmanager.secrets.get",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
      "secretmanager.versions.list",
      "secretmanager.versions.destroy",
    ])
    error_message = "removing versions.add must remove nothing else from swarmSecretLister"
  }
}

run "a_tenant_worker_can_neither_enumerate_nor_destroy_control_plane_state" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # The predefined roles/datastore.user would add entities.delete and
  # entities.list, which is the difference between "can read and write
  # documents it was handed ids for" and "can enumerate and destroy every other
  # tenant's tasks, leases and the pool documents the whole platform admits
  # against". Nothing in the worker path deletes a document or runs a query.
  assert {
    condition     = !contains(google_project_iam_custom_role.platform["worker_firestore"].permissions, "datastore.entities.delete")
    error_message = "entities.delete would let a hostile worker delete another tenant's tasks and leases, and the pool documents admission depends on"
  }

  assert {
    condition     = !contains(google_project_iam_custom_role.platform["worker_firestore"].permissions, "datastore.entities.list")
    error_message = "entities.list is queries: without it a document can only be fetched by an id already known, so another tenant's work cannot be enumerated"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.platform["worker_firestore"].permissions :
      startswith(p, "datastore.") || p == "resourcemanager.projects.get"
    ])
    error_message = "the worker's Firestore role covers Firestore and nothing else"
  }

  # legacyBucketReader would hand over objects.list across the WHOLE bucket.
  assert {
    condition     = google_project_iam_custom_role.platform["bucket_metadata_reader"].permissions == toset(["storage.buckets.get"])
    error_message = "mounting needs bucket metadata and nothing else"
  }

  # Artifact Registry IAM stops at the repository, so a tenant worker's grant
  # cannot be narrowed to the images its profiles name. What CAN be narrowed is
  # the shape: a role with no *.list permission means an image is only reachable
  # under a name the caller already holds.
  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.platform["image_puller"].permissions :
      !strcontains(lower(p), "list")
    ])
    error_message = "the pull role grants no enumeration: one tenant's worker must not be able to list every image name in the shared repository"
  }

  assert {
    condition     = contains(google_project_iam_custom_role.platform["image_puller"].permissions, "artifactregistry.repositories.downloadArtifacts") && contains(google_project_iam_custom_role.platform["image_puller"].permissions, "artifactregistry.repositories.get")
    error_message = "the pull role must still be able to pull, or every worker fails at image pull instead of running"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.platform["image_puller"].permissions :
      !can(regex("(create|update|delete|upload|export)", p))
    ])
    error_message = "a worker identity that can push or delete an image can replace the runtime every other tenant runs"
  }
}

run "a_puller_role_that_can_enumerate_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    image_puller_permissions = [
      "artifactregistry.repositories.get",
      "artifactregistry.repositories.downloadArtifacts",
      "artifactregistry.packages.list",
    ]
  }

  expect_failures = [var.image_puller_permissions]
}

run "a_puller_role_that_can_push_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    image_puller_permissions = [
      "artifactregistry.repositories.get",
      "artifactregistry.repositories.downloadArtifacts",
      "artifactregistry.repositories.uploadArtifacts",
    ]
  }

  expect_failures = [var.image_puller_permissions]
}

# ---------------------------------------------------------------------------
# The adoption, and the order it waits for.
#
# Imported while terraform/infra still manages them, the roles would be managed
# from two states. So bootstrap reads each named infra state's
# custom_roles_owner output -- written only by the apply that runs infra's
# `removed` blocks -- and refuses until it reads terraform/bootstrap. The two
# runs below are the refusal and its control. The mock provider cannot import,
# so the import targets are overridden, as Terraform's own error asks.
# ---------------------------------------------------------------------------

run "adoption_waits_until_terraform_infra_has_let_go" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    adopt_from_infra_states = ["infra/dev"]

    # The grant depends on the role, so the role's refusal is the one that
    # surfaces; the grant's own precondition is covered by the run below.
    grant_broker_secret_lister = false
  }

  # infra/dev as it is before the merge's release: no custom_roles_owner.
  override_data {
    target          = data.terraform_remote_state.infra
    override_during = plan
    values = {
      outputs = {
        project_id = "saga-agents-staging"
      }
    }
  }

  override_resource {
    target          = google_project_iam_custom_role.platform
    override_during = plan
    values = {
      deleted = false
    }
  }

  expect_failures = [google_project_iam_custom_role.platform]
}

run "adoption_proceeds_once_terraform_infra_has_let_go" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    adopt_from_infra_states = ["infra/dev"]
  }

  # infra/dev after the release that ran the `removed` blocks.
  override_data {
    target          = data.terraform_remote_state.infra
    override_during = plan
    values = {
      outputs = {
        project_id         = "saga-agents-staging"
        custom_roles_owner = "terraform/bootstrap"
      }
    }
  }

  override_resource {
    target          = google_project_iam_custom_role.platform
    override_during = plan
    values = {
      deleted = false
    }
  }

  override_resource {
    target          = google_project_iam_member.broker_secret_lister
    override_during = plan
    values = {
      etag = "BwZ0"
    }
  }

  assert {
    condition     = length(local.infra_states_holding_platform_roles) == 0
    error_message = "with infra/dev's custom_roles_owner reading terraform/bootstrap, nothing may still be waiting"
  }

  assert {
    condition     = length(google_project_iam_custom_role.platform) == 8 && length(google_project_iam_member.broker_secret_lister) == 1
    error_message = "once infra has let go, bootstrap manages all eight roles and the broker's grant"
  }
}
