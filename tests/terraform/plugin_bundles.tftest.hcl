# The managed plugins' bundle build (docs/design/managed-plugins.md §1.4.2,
# §1.4.5, §15.2; lane 15 of §17.1): terraform/infra/plugin-bundles.tf. The
# builder account is terraform/bootstrap's (bootstrap.tftest.hcl holds it).
#
# What is held here:
#
#   * builtins_managed is "off" by default, and off plans NOTHING in
#     plugin-bundles.tf: no account, no repository, no grant, so nothing costs
#     money or grants access before the switch; the bundle image path is "";
#   * on, this root reads the builder account and the two repositories exist,
#     labelled managed-by=swarm-terraform; the bundle repository refuses a
#     moved tag; the npm mirror is a remote repository of the npm registry;
#   * the builder holds exactly five grants (§1.4.2, the owner's change of 11
#     November 2026): platform/sources/ by condition (evaluated against
#     admitted and refused names), the platform repository, the ONE pinned
#     Bun object of the build mirror by condition, the npm mirror, and a push
#     to the bundle repository alone; no Docker Hub mirror read and no project
#     role (tests/unit/terraform/test_plugin_bundle_job_iam.py counts them in
#     the file and holds that nothing there names the old build service);
#   * the job swarm-plugin-bundle plans only with the switch on and the
#     bundler image's digest; it runs as the builder, the bundler image by
#     digest, one task, no retries, 3600 seconds, its whole configuration in
#     its fixed command, ending `--`;
#   * the job reaches Google's APIs and nothing else (the security review of
#     11 November 2026): every packet leaves through its own network,
#     swarm-plugin-bundle, which is neither the swarm network nor the intake
#     network and has no router and no Cloud NAT; its subnetwork has Private
#     Google Access and flow logs; its egress firewall admits TCP 443 from the
#     job's tag and denies everything else; all of it only with the switch on;
#   * swarm-api's one binding on the job is swarmPluginBundleRunner (the role
#     terraform/bootstrap defines, bootstrap's plugin_bundle_runner.tftest.hcl);
#   * the job's logs go to the platform's log bucket, filtered to that job;
#   * the control plane reads the bundle repository and a tenant pulls from it
#     without enumerating, as from the platform repository;
#   * no service is handed SWARM_BUILTINS_MANAGED or PLUGIN_BUNDLE_REPOSITORY
#     yet: lanes 17 and 18 set them with the code that reads them;
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
      length(data.google_service_account.plugin_bundler) == 0
      && length(google_artifact_registry_repository.plugin_bundles) == 0
      && length(google_artifact_registry_repository.mirror_npm) == 0
      && length(google_storage_bucket_iam_member.plugin_bundler_sources) == 0
      && length(google_storage_bucket_iam_member.plugin_bundler_tool_mirror) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_release_images) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_npm) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundler_push) == 0
      && length(google_cloud_run_v2_job.plugin_bundle) == 0
      && length(google_cloud_run_v2_job_iam_member.plugin_bundle_api_runs) == 0
      && length(google_logging_project_sink.plugin_bundle_logs) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundles_readers) == 0
      && length(google_artifact_registry_repository_iam_member.plugin_bundles_pullers) == 0
      && length(google_compute_network.plugin_bundle) == 0
      && length(google_compute_subnetwork.plugin_bundle) == 0
      && length(google_compute_firewall.plugin_bundle_deny_egress) == 0
      && length(google_compute_firewall.plugin_bundle_allow_google_apis) == 0
    )
    error_message = "builtins_managed off (the default) plans nothing in plugin-bundles.tf: nothing costs money or grants access before the switch"
  }

  assert {
    condition     = local.plugin_bundle_repository == ""
    error_message = "off spells no bundle image path: no bundle digest is accepted"
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
    condition     = data.google_service_account.plugin_bundler[0].account_id == "swarm-plugin-bundler"
    error_message = "on reads the builder account swarm-plugin-bundler, which terraform/bootstrap creates (plan §8)"
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
    condition     = local.plugin_bundle_repository == "us-central1-docker.pkg.dev/example-project/swarm-plugin-bundles"
    error_message = "on spells the bundle repository's image path, the one a bundle digest may name"
  }

  assert {
    condition = alltrue([
      for svc in keys(local.service_env) :
      !contains(keys(local.service_env[svc]), "SWARM_BUILTINS_MANAGED")
      && !contains(keys(local.service_env[svc]), "PLUGIN_BUNDLE_REPOSITORY")
    ])
    error_message = "no service is handed SWARM_BUILTINS_MANAGED or PLUGIN_BUNDLE_REPOSITORY before the code that reads it (scripts/lib/check-env-parity.sh)"
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
      && google_storage_bucket_iam_member.plugin_bundler_tool_mirror[0].member == "serviceAccount:swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugin_bundler_tool_mirror[0].condition[0].expression == "resource.name == \"projects/_/buckets/swarm-build-mirror-example-project/objects/github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64.zip\""
      && startswith(google_storage_bucket_iam_member.plugin_bundler_tool_mirror[0].condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the builder reads the ONE pinned Bun object of the build mirror bucket, by a condition on its name, never the whole bucket (managed-plugins.md section 1.4.2, grant 3)"
  }

  assert {
    condition = (
      google_artifact_registry_repository_iam_member.plugin_bundler_release_images[0].repository == "swarm-images"
      && google_artifact_registry_repository_iam_member.plugin_bundler_release_images[0].role == "roles/artifactregistry.reader"
      && google_artifact_registry_repository_iam_member.plugin_bundler_npm[0].repository == "swarm-mirror-npm"
      && google_artifact_registry_repository_iam_member.plugin_bundler_npm[0].role == "roles/artifactregistry.reader"
    )
    error_message = "the builder reads the platform repository (the release images) and the npm mirror, read only"
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
      ] : g.role == "roles/artifactregistry.reader" && g.repository != "swarm-plugin-bundles"
    ])
    error_message = "the builder writes to no repository but swarm-plugin-bundles"
  }

  # Every builder grant, by member: five, and they are these five.
  assert {
    condition = length([
      for g in [
        google_storage_bucket_iam_member.plugin_bundler_sources[0],
        google_storage_bucket_iam_member.plugin_bundler_tool_mirror[0],
        google_artifact_registry_repository_iam_member.plugin_bundler_release_images[0],
        google_artifact_registry_repository_iam_member.plugin_bundler_npm[0],
        google_artifact_registry_repository_iam_member.plugin_bundler_push[0],
      ] : g if g.member == "serviceAccount:swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
    ]) == 5
    error_message = "the builder holds exactly the design's five grants"
  }
}

run "off_plans_no_job_even_with_the_bundler_image" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    swarm_workers_log_view_enabled = true
    image_refs = merge(var.image_refs, {
      "swarm-plugin-bundler" = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-plugin-bundler@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition = (
      length(google_cloud_run_v2_job.plugin_bundle) == 0
      && length(google_cloud_run_v2_job_iam_member.plugin_bundle_api_runs) == 0
      && length(google_logging_project_sink.plugin_bundle_logs) == 0
      && length(google_compute_network.plugin_bundle) == 0
      && length(google_compute_subnetwork.plugin_bundle) == 0
      && length(google_compute_firewall.plugin_bundle_deny_egress) == 0
      && length(google_compute_firewall.plugin_bundle_allow_google_apis) == 0
    )
    error_message = "with builtins_managed off neither the job nor its network is planned, whatever image the release carries"
  }
}

run "the_job_waits_for_the_switch_and_the_bundler_image" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # The switch on, but the release does not carry the bundler image yet: the
  # builder's grants plan, the job and the rows that exist only for it do not.
  variables {
    builtins_managed = "on"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_job.plugin_bundle) == 0
      && length(google_cloud_run_v2_job_iam_member.plugin_bundle_api_runs) == 0
      && length(google_logging_project_sink.plugin_bundle_logs) == 0
    )
    error_message = "the job, swarm-api's run of it and its log sink wait for the bundler image's digest (images.json gates swarm-plugin-bundler)"
  }
}

run "the_job_runs_as_the_builder_and_swarm_api_may_only_run_it" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed               = "on"
    swarm_workers_log_view_enabled = true
    image_refs = merge(var.image_refs, {
      "swarm-plugin-bundler" = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-plugin-bundler@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition = (
      google_cloud_run_v2_job.plugin_bundle[0].name == "swarm-plugin-bundle"
      && google_cloud_run_v2_job.plugin_bundle[0].location == "us-central1"
      && google_cloud_run_v2_job.plugin_bundle[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the job is swarm-plugin-bundle, in the deployment's region, managed-by=swarm-terraform"
  }

  assert {
    condition = (
      google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].service_account == "swarm-plugin-bundler@example-project.iam.gserviceaccount.com"
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].image == "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-plugin-bundler@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    )
    error_message = "the job's identity is the builder account and its image the bundler image by digest, from the release's image_refs"
  }

  assert {
    condition = (
      google_cloud_run_v2_job.plugin_bundle[0].template[0].task_count == 1
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].parallelism == 1
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].max_retries == 0
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].timeout == "3600s"
      && length(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].env) == 0
    )
    error_message = "one task, parallelism 1, no retries, a 3600-second agent timeout and no environment (managed-plugins.md section 1.4.2)"
  }

  assert {
    condition = (
      join(" ", slice(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command, 0, 4)) == "python3 -I /opt/swarm-bundler/swarm-bundle job"
      && element(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command, length(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command) - 1) == "--"
      && contains(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command, "--bundle-repository=us-central1-docker.pkg.dev/example-project/swarm-plugin-bundles")
      && contains(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command, "--base-repository=us-central1-docker.pkg.dev/example-project/swarm-images")
      && contains(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command, "--runner-image=agent-runtime-browser:browser")
      && contains(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].command, "--runner-image=agent-runtime-base:base")
      && try(length(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].containers[0].args), 0) == 0
    )
    error_message = "the job's whole configuration is its fixed command, which an execution cannot override, ending with -- before the execution's arguments"
  }

  assert {
    condition = (
      google_cloud_run_v2_job_iam_member.plugin_bundle_api_runs[0].name == "swarm-plugin-bundle"
      && google_cloud_run_v2_job_iam_member.plugin_bundle_api_runs[0].role == "projects/example-project/roles/swarmPluginBundleRunner"
      && google_cloud_run_v2_job_iam_member.plugin_bundle_api_runs[0].member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com"
    )
    error_message = "swarm-api's one grant for the bundle build is swarmPluginBundleRunner on the job swarm-plugin-bundle's own IAM policy"
  }

  assert {
    condition = (
      google_logging_project_sink.plugin_bundle_logs[0].filter == "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"swarm-plugin-bundle\""
      && google_logging_project_sink.plugin_bundle_logs[0].destination == "logging.googleapis.com/projects/example-project/locations/us-central1/buckets/swarm-worker-logs"
      && startswith(google_logging_project_sink.plugin_bundle_logs[0].description, "managed-by=swarm-terraform")
    )
    error_message = "the job's executions' logs, and only those, go to the platform's log bucket"
  }
}

# The job runs upstream code (gstack's ./setup, bun install, security-audit's
# tests): every packet it sends leaves through its own network, which reaches
# Google's APIs through Private Google Access and nothing else.
run "the_job_reaches_googles_apis_through_its_own_network_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "on"
    image_refs = merge(var.image_refs, {
      "swarm-plugin-bundler" = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-plugin-bundler@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition = (
      length(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].vpc_access) == 1
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC"
      && length(google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].vpc_access[0].network_interfaces) == 1
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].vpc_access[0].network_interfaces[0].tags == tolist(["swarm-plugin-bundle"])
      && google_cloud_run_v2_job.plugin_bundle[0].template[0].template[0].vpc_access[0].connector == null
    )
    error_message = "the job sends ALL its traffic through Direct VPC egress, one interface, tagged swarm-plugin-bundle, no connector"
  }

  assert {
    condition = (
      google_compute_network.plugin_bundle[0].name == "swarm-plugin-bundle"
      && google_compute_network.plugin_bundle[0].auto_create_subnetworks == false
      && google_compute_subnetwork.plugin_bundle[0].name == "swarm-plugin-bundle"
      && google_compute_subnetwork.plugin_bundle[0].region == "us-central1"
      && google_compute_subnetwork.plugin_bundle[0].ip_cidr_range == "10.250.0.64/26"
      && google_compute_network.plugin_bundle[0].name != module.network.network_name
      && google_compute_network.plugin_bundle[0].name != google_compute_network.plugin_intake.name
      && google_compute_subnetwork.plugin_bundle[0].name != module.network.subnetwork_name
      && google_compute_subnetwork.plugin_bundle[0].name != google_compute_subnetwork.plugin_intake.name
    )
    error_message = "the job's network is its own, swarm-plugin-bundle, custom-mode, one /26 in the region; never the swarm network nor the intake network"
  }

  # No router and no Cloud NAT on it: the root declares no router, NAT or route
  # at all, and the one router of any module is the swarm network's.
  assert {
    condition = (
      length(google_compute_network.plugin_bundle) == 1
      && length(regexall("resource\\s+\"google_compute_(router|router_nat|route)\"", join("\n", [for f in fileset("../../terraform/infra", "*.tf") : file("../../terraform/infra/${f}")]))) == 0
      && length(regexall("resource\\s+\"google_compute_router\"", join("\n", [for f in fileset("../../terraform/modules", "**/*.tf") : file("../../terraform/modules/${f}")]))) == 1
      && length(regexall("resource\\s+\"google_compute_router\"\\s+\"this\"\\s+\\{[^}]*network\\s+=\\s+google_compute_network\\.this\\.id", file("../../terraform/modules/network/main.tf"))) == 1
    )
    error_message = "nothing gives the bundle network a router, a Cloud NAT or a route out: the one router is the swarm network's own"
  }

  assert {
    condition = (
      google_compute_subnetwork.plugin_bundle[0].private_ip_google_access == true
      && google_compute_subnetwork.plugin_bundle[0].log_config[0].aggregation_interval == "INTERVAL_10_MIN"
      && google_compute_subnetwork.plugin_bundle[0].log_config[0].flow_sampling == 1
      && google_compute_subnetwork.plugin_bundle[0].log_config[0].metadata == "INCLUDE_ALL_METADATA"
    )
    error_message = "the subnetwork has Private Google Access (its one way to Artifact Registry and Cloud Storage) and flow logs, as the intake subnetwork has"
  }

  assert {
    condition = (
      google_compute_firewall.plugin_bundle_allow_google_apis[0].network == "swarm-plugin-bundle"
      && google_compute_firewall.plugin_bundle_allow_google_apis[0].direction == "EGRESS"
      && google_compute_firewall.plugin_bundle_allow_google_apis[0].target_tags == toset(["swarm-plugin-bundle"])
      && length(google_compute_firewall.plugin_bundle_allow_google_apis[0].allow) == 1
      && one(google_compute_firewall.plugin_bundle_allow_google_apis[0].allow).protocol == "tcp"
      && one(google_compute_firewall.plugin_bundle_allow_google_apis[0].allow).ports == tolist(["443"])
      && length(google_compute_firewall.plugin_bundle_allow_google_apis[0].deny) == 0
    )
    error_message = "the one allow is TCP 443 from the job's tag (Google's APIs: with no NAT, nothing else answers)"
  }

  assert {
    condition = (
      google_compute_firewall.plugin_bundle_deny_egress[0].network == "swarm-plugin-bundle"
      && google_compute_firewall.plugin_bundle_deny_egress[0].direction == "EGRESS"
      && google_compute_firewall.plugin_bundle_deny_egress[0].destination_ranges == toset(["0.0.0.0/0"])
      && length(google_compute_firewall.plugin_bundle_deny_egress[0].target_tags) == 0
      && length(google_compute_firewall.plugin_bundle_deny_egress[0].allow) == 0
      && one(google_compute_firewall.plugin_bundle_deny_egress[0].deny).protocol == "all"
      && google_compute_firewall.plugin_bundle_allow_google_apis[0].priority < google_compute_firewall.plugin_bundle_deny_egress[0].priority
    )
    error_message = "everything else leaving the bundle network is denied, below the HTTPS allow"
  }

  # The compute network types take no labels (scripts/lib/unlabelable-types.json):
  # each carries managed-by=swarm-terraform in its description.
  assert {
    condition = alltrue([
      for d in [
        google_compute_network.plugin_bundle[0].description,
        google_compute_subnetwork.plugin_bundle[0].description,
        google_compute_firewall.plugin_bundle_deny_egress[0].description,
        google_compute_firewall.plugin_bundle_allow_google_apis[0].description,
      ] : startswith(d, "managed-by=swarm-terraform;")
    ]) && google_cloud_run_v2_job.plugin_bundle[0].labels["managed-by"] == "swarm-terraform"
    error_message = "every resource of the bundle network carries managed-by=swarm-terraform"
  }
}

# With the switch on and no bundler image yet, the network is made, so it is in
# place before the job's first deploy, as the intake network is.
run "the_network_waits_only_for_the_switch" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    builtins_managed = "on"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_job.plugin_bundle) == 0
      && length(google_compute_network.plugin_bundle) == 1
      && length(google_compute_subnetwork.plugin_bundle) == 1
      && length(google_compute_firewall.plugin_bundle_deny_egress) == 1
      && length(google_compute_firewall.plugin_bundle_allow_google_apis) == 1
    )
    error_message = "with the switch on, the bundle network, its subnetwork and its two firewalls are planned before the bundler image exists"
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
