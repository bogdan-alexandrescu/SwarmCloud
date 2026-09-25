# The fixture the custom-role move is held to, held first to the code it was
# taken from.
#
# platform_custom_roles_before_the_move.json records the eight custom roles
# terraform/infra and its modules defined on origin/main (89f2e09). It was read
# from the live project, not from the code, so these runs are what show the two
# agree: they plan each module as it stands on main and compare every role's id,
# title, description, stage and permission set with the file. Once that is
# green, the same file is what terraform/bootstrap is held to after the move
# (#79), so "the move changes nothing live" is an assertion rather than a claim.
#
# This file plans the modules' own role resources. The commit that moves the
# roles out of the modules replaces it with the bootstrap-side comparison in
# platform_roles.tftest.hcl.

mock_provider "google" {}

variables {
  project_id      = "saga-agents-staging"
  artifact_bucket = "swarm-artifacts-saga-agents-staging"
  labels          = { "managed-by" = "swarm-terraform" }

  gke_cluster_name = "swarm-autopilot"
  gke_location     = "us-central1"
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

run "the_fixture_is_what_modules_iam_defined" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.job_dispatcher.role_id == run.fixture.roles.job_dispatcher.role_id,
      google_project_iam_custom_role.job_dispatcher.title == run.fixture.roles.job_dispatcher.title,
      google_project_iam_custom_role.job_dispatcher.description == run.fixture.roles.job_dispatcher.description,
      google_project_iam_custom_role.job_dispatcher.stage == run.fixture.roles.job_dispatcher.stage,
      google_project_iam_custom_role.job_dispatcher.permissions == run.fixture.roles.job_dispatcher.permissions,
    ])
    error_message = "the fixture differs from swarmJobDispatcher as modules/iam defines it on main"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.job_reaper.role_id == run.fixture.roles.job_reaper.role_id,
      google_project_iam_custom_role.job_reaper.title == run.fixture.roles.job_reaper.title,
      google_project_iam_custom_role.job_reaper.description == run.fixture.roles.job_reaper.description,
      google_project_iam_custom_role.job_reaper.stage == run.fixture.roles.job_reaper.stage,
      google_project_iam_custom_role.job_reaper.permissions == run.fixture.roles.job_reaper.permissions,
    ])
    error_message = "the fixture differs from swarmJobReaper as modules/iam defines it on main"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.gke_dispatcher[0].role_id == run.fixture.roles.gke_dispatcher.role_id,
      google_project_iam_custom_role.gke_dispatcher[0].title == run.fixture.roles.gke_dispatcher.title,
      google_project_iam_custom_role.gke_dispatcher[0].description == run.fixture.roles.gke_dispatcher.description,
      google_project_iam_custom_role.gke_dispatcher[0].stage == run.fixture.roles.gke_dispatcher.stage,
      google_project_iam_custom_role.gke_dispatcher[0].permissions == run.fixture.roles.gke_dispatcher.permissions,
    ])
    error_message = "the fixture differs from swarmGkeDispatcher as modules/iam defines it on main"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.gke_reaper[0].role_id == run.fixture.roles.gke_reaper.role_id,
      google_project_iam_custom_role.gke_reaper[0].title == run.fixture.roles.gke_reaper.title,
      google_project_iam_custom_role.gke_reaper[0].description == run.fixture.roles.gke_reaper.description,
      google_project_iam_custom_role.gke_reaper[0].stage == run.fixture.roles.gke_reaper.stage,
      google_project_iam_custom_role.gke_reaper[0].permissions == run.fixture.roles.gke_reaper.permissions,
    ])
    error_message = "the fixture differs from swarmGkeReaper as modules/iam defines it on main"
  }

  # With secret_lister_project_wide_versions_add at its default, true: the
  # value main's terraform/infra applies (it never sets the variable), and the
  # live role carries versions.add.
  assert {
    condition = alltrue([
      google_project_iam_custom_role.secret_lister.role_id == run.fixture.roles.secret_lister.role_id,
      google_project_iam_custom_role.secret_lister.title == run.fixture.roles.secret_lister.title,
      google_project_iam_custom_role.secret_lister.description == run.fixture.roles.secret_lister.description,
      google_project_iam_custom_role.secret_lister.stage == run.fixture.roles.secret_lister.stage,
      google_project_iam_custom_role.secret_lister.permissions == run.fixture.roles.secret_lister.permissions,
    ])
    error_message = "the fixture differs from swarmSecretLister as modules/iam defines it on main"
  }
}

run "the_fixture_is_what_modules_tenancy_defined" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = ["anthropic"]
      }
    }
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.worker_firestore.role_id == run.fixture.roles.worker_firestore.role_id,
      google_project_iam_custom_role.worker_firestore.title == run.fixture.roles.worker_firestore.title,
      google_project_iam_custom_role.worker_firestore.description == run.fixture.roles.worker_firestore.description,
      google_project_iam_custom_role.worker_firestore.stage == run.fixture.roles.worker_firestore.stage,
      google_project_iam_custom_role.worker_firestore.permissions == run.fixture.roles.worker_firestore.permissions,
    ])
    error_message = "the fixture differs from swarmTenantWorkerFirestore as modules/tenancy defines it on main"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.bucket_metadata_reader.role_id == run.fixture.roles.bucket_metadata_reader.role_id,
      google_project_iam_custom_role.bucket_metadata_reader.title == run.fixture.roles.bucket_metadata_reader.title,
      google_project_iam_custom_role.bucket_metadata_reader.description == run.fixture.roles.bucket_metadata_reader.description,
      google_project_iam_custom_role.bucket_metadata_reader.stage == run.fixture.roles.bucket_metadata_reader.stage,
      google_project_iam_custom_role.bucket_metadata_reader.permissions == run.fixture.roles.bucket_metadata_reader.permissions,
    ])
    error_message = "the fixture differs from swarmBucketMetadataReader as modules/tenancy defines it on main"
  }
}

run "the_fixture_is_what_modules_artifact_registry_defined" {
  command = plan

  module {
    source = "../../terraform/modules/artifact_registry"
  }

  variables {
    # The role exists only when something pulls.
    pullers = {
      "tenant-eng" = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.image_puller[0].role_id == run.fixture.roles.image_puller.role_id,
      google_project_iam_custom_role.image_puller[0].title == run.fixture.roles.image_puller.title,
      google_project_iam_custom_role.image_puller[0].description == run.fixture.roles.image_puller.description,
      google_project_iam_custom_role.image_puller[0].stage == run.fixture.roles.image_puller.stage,
      google_project_iam_custom_role.image_puller[0].permissions == run.fixture.roles.image_puller.permissions,
    ])
    error_message = "the fixture differs from swarmImagePuller as modules/artifact_registry defines it on main"
  }
}
