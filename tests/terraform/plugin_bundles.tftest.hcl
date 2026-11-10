# The managed plugins' bundle build (docs/design/managed-plugins.md §1.4.2,
# §1.4.5, §15.2; lane 15 of §17.1): terraform/infra/plugin-bundles.tf and the
# two environment variables it feeds.
#
# What is held here:
#
#   * builtins_managed is "off" by default, and off plans NOTHING in
#     plugin-bundles.tf: no account, no repository, no grant, so nothing costs
#     money or grants access before the switch; the two variables render
#     SWARM_BUILTINS_MANAGED="off" and an empty PLUGIN_BUNDLE_REPOSITORY;
#   * on, the builder account and the two repositories exist, labelled (or
#     described) managed-by=swarm-terraform; the bundle repository refuses a
#     moved tag; the npm mirror is a remote repository of the npm registry;
#   * the builder reads platform/sources/ only (the condition is evaluated
#     against admitted and refused names), reads the platform repository and
#     the mirrors, pushes to the bundle repository and nowhere else, and holds
#     one project role, logging.logWriter;
#   * nothing here grants swarm-api actAs on the builder or a Cloud Build
#     role: both are the owner's (plugin-bundles.tf header);
#   * the control plane reads the bundle repository and a tenant pulls from it
#     without enumerating, as from the platform repository;
#   * SWARM_BUILTINS_MANAGED goes to swarm-api alone, PLUGIN_BUNDLE_REPOSITORY
#     to swarm-api and the scheduler alone;
#   * any value but "on" or "off" is refused.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "example-project"
  environment = "dev"

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic"]
    }
  }

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_plans_nothing" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_service_account.plugin_bundler) == 0
      && length(google_artifact_registry_repository.plugin_bundles) == 0
      && length(google_artifact_registry_repository.mirror_npm) == 0
      && length(google_storage_bucket_iam_member.plugin_bundler_sources) == 0
      && length(google_storage_bucket_iam_member.plugin_bundler_tool_mirror) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_release_images) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_npm) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_dockerhub) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_push) == 0
      && length(google_project_iam_member.plugin_bundler_logs) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundles_readers) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundles_pullers) == 0
    )
    error_message = "builtins_managed off (the default) plans nothing in plugin-bundles.tf: nothing costs money or grants access before the switch"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].SWARM_BUILTINS_MANAGED == "off"
      && local.service_env["swarm-api"].PLUGIN_BUNDLE_REPOSITORY == ""
      && local.service_env["swarm-scheduler"].PLUGIN_BUNDLE_REPOSITORY == ""
    )
    error_message = "off renders SWARM_BUILTINS_MANAGED=\"off\" and an empty PLUGIN_BUNDLE_REPOSITORY: no bundle digest is accepted"
  }
}

run "on_makes_the_builder_and_its_repositories" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "on"
  }

  assert {
    condition = (
      google_service_account.plugin_bundler[0].account_id == "swarm-plugin-bundler"
      && startswith(google_service_account.plugin_bundler[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the builder account is swarm-plugin-bundler, described managed-by=swarm-terraform (an account takes no labels)"
  }

  assert {
    condition = (
      google_artifact_registry_repository.plugin_bundles[0].repository_id == "swarm-plugin-bundles"
      && google_artifact_registry_repository.plugin_bundles[0].format == "DOCKER"
      && google_artifact_registry_repository.plugin_bundles[0].docker_config[0].immutable_tags == true
      && google_artifact_registry_repository.plugin_bundles[0].labels["managed-by"] == "swarm-terraform"
      && length(google_artifact_registry_repository.plugin_bundles[0].cleanup_policies) == 0
    )
    error_message = "the bundle repository is swarm-plugin-bundles, Docker, tags never moved, no age cleanup (the retention sweep deletes), managed-by=swarm-terraform"
  }

  assert {
    condition = (
      google_artifact_registry_repository.mirror_npm[0].repository_id == "swarm-mirror-npm"
      && google_artifact_registry_repository.mirror_npm[0].format == "NPM"
      && google_artifact_registry_repository.mirror_npm[0].mode == "REMOTE_REPOSITORY"
      && google_artifact_registry_repository.mirror_npm[0].remote_repository_config[0].npm_repository[0].public_repository == "NPMJS"
      && google_artifact_registry_repository.mirror_npm[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the npm mirror is a remote repository of the npm registry, managed-by=swarm-terraform (Q19: dependencies only through the deployment's mirrors)"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].SWARM_BUILTINS_MANAGED == "on"
      && local.service_env["swarm-api"].PLUGIN_BUNDLE_REPOSITORY == "us-central1-docker.pkg.dev/example-project/swarm-plugin-bundles"
      && local.service_env["swarm-scheduler"].PLUGIN_BUNDLE_REPOSITORY == "us-central1-docker.pkg.dev/example-project/swarm-plugin-bundles"
    )
    error_message = "on renders SWARM_BUILTINS_MANAGED=\"on\" and the bundle repository's image path to swarm-api and the scheduler"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "SWARM_BUILTINS_MANAGED")
    ])
    error_message = "SWARM_BUILTINS_MANAGED is read at submission, by swarm-api alone"
  }

  assert {
    condition = alltrue([
      for svc in ["swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "PLUGIN_BUNDLE_REPOSITORY")
    ])
    error_message = "PLUGIN_BUNDLE_REPOSITORY goes to swarm-api and the scheduler only"
  }
}

run "the_builder_reads_sources_only_and_pushes_to_one_repository" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "on"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugin_bundler_sources[0].bucket == "example-project-swarm-plugins"
      && google_storage_bucket_iam_member.plugin_bundler_sources[0].member == "serviceAccount:swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugin_bundler_sources[0].role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.plugin_bundler_sources[0].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/platform/sources/\")"
      && startswith(google_storage_bucket_iam_member.plugin_bundler_sources[0].condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the builder reads the plugins bucket under platform/sources/ only (objectViewer, conditioned, managed-by=swarm-terraform)"
  }

  # The condition, evaluated: startsWith() emulated on admitted and refused names.
  assert {
    condition = alltrue([
      for name, admitted in {
        "projects/_/buckets/example-project-swarm-plugins/objects/platform/sources/abc.tar" = true
        "projects/_/buckets/example-project-swarm-plugins/objects/platform/abc.tar"         = false
        "projects/_/buckets/example-project-swarm-plugins/objects/tenants/eng/abc.tar"      = false
        "projects/_/buckets/example-project-swarm-plugins/objects/quarantine/i-1"           = false
        "projects/_/buckets/example-project-swarm-plugins/objects/staging/i-1/report.json"  = false
        "projects/_/buckets/other-bucket/objects/platform/sources/abc.tar"                  = false
      } : startswith(name, trimsuffix(trimprefix(google_storage_bucket_iam_member.plugin_bundler_sources[0].condition[0].expression, "resource.name.startsWith(\""), "\")")) == admitted
    ])
    error_message = "the builder's read admits platform/sources/ of the plugins bucket and refuses tenants/, quarantine/, staging/, other platform/ objects and other buckets"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugin_bundler_tool_mirror[0].bucket == "swarm-build-mirror-example-project"
      && google_storage_bucket_iam_member.plugin_bundler_tool_mirror[0].role == "roles/storage.objectViewer"
    )
    error_message = "the builder reads Bun's pinned zip from the build mirror bucket, read only"
  }

  assert {
    condition = (
      google_artifact_registry_repository_iam_member.plugin_bundler_release_images[0].repository == "swarm-images"
      && google_artifact_registry_repository_iam_member.plugin_bundler_release_images[0].role == "roles/artifactregistry.reader"
      && google_artifact_registry_repository_iam_member.plugin_bundler_npm[0].repository == "swarm-mirror-npm"
      && google_artifact_registry_repository_iam_member.plugin_bundler_npm[0].role == "roles/artifactregistry.reader"
      && google_artifact_registry_repository_iam_member.plugin_bundler_dockerhub[0].repository == "swarm-mirror-dockerhub"
      && google_artifact_registry_repository_iam_member.plugin_bundler_dockerhub[0].role == "roles/artifactregistry.reader"
    )
    error_message = "the builder reads the platform repository (the release images) and the mirrors, read only"
  }

  assert {
    condition = (
      google_artifact_registry_repository_iam_member.plugin_bundler_push[0].repository == "swarm-plugin-bundles"
      && google_artifact_registry_repository_iam_member.plugin_bundler_push[0].role == "roles/artifactregistry.writer"
    )
    error_message = "the builder pushes to swarm-plugin-bundles"
  }

  # Push only there: every grant the builder holds on a repository other than
  # the bundle repository is the reader role.
  assert {
    condition = alltrue([
      for g in [
        google_artifact_registry_repository_iam_member.plugin_bundler_release_images[0],
        google_artifact_registry_repository_iam_member.plugin_bundler_npm[0],
        google_artifact_registry_repository_iam_member.plugin_bundler_dockerhub[0],
      ] : g.role == "roles/artifactregistry.reader" && g.repository != "swarm-plugin-bundles"
    ])
    error_message = "the builder writes to no repository but swarm-plugin-bundles"
  }

  assert {
    condition = (
      google_project_iam_member.plugin_bundler_logs[0].role == "roles/logging.logWriter"
      && google_project_iam_member.plugin_bundler_logs[0].member == "serviceAccount:swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
    )
    error_message = "the builder's one project role is logging.logWriter, for its own build log"
  }
}

run "who_pulls_a_bundle_image" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "on"
  }

  assert {
    condition = (
      toset(keys(google_artifact_registry_repository_iam_member.plugin_bundles_readers)) == toset(["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"])
      && alltrue([for g in google_artifact_registry_repository_iam_member.plugin_bundles_readers : g.role == "roles/artifactregistry.reader" && g.repository == "swarm-plugin-bundles"])
    )
    error_message = "the control plane reads the bundle repository, as it reads the platform repository"
  }

  assert {
    condition = (
      keys(google_artifact_registry_repository_iam_member.plugin_bundles_pullers) == ["eng"]
      && google_artifact_registry_repository_iam_member.plugin_bundles_pullers["eng"].role == "projects/example-project/roles/swarmImagePuller"
      && google_artifact_registry_repository_iam_member.plugin_bundles_pullers["eng"].repository == "swarm-plugin-bundles"
    )
    error_message = "a tenant worker pulls a bundle image without enumerating (swarmImagePuller), as from the platform repository"
  }
}

run "an_unknown_value_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "On"
  }

  expect_failures = [var.builtins_managed]
}

run "a_boolean_word_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "true"
  }

  expect_failures = [var.builtins_managed]
}
