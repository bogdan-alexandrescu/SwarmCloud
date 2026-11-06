# Managed plugins' infrastructure (docs/design/managed-plugins.md §2.2, §3,
# §9.3, §15; lane 3 of §17): terraform/infra/plugins.tf.
#
# What is held here, grant by grant (member, role, condition):
#
#   * the bucket is <project>-swarm-plugins, uniform access, public access
#     prevented, labelled managed-by=swarm-terraform, in the region;
#   * swarm-api: objectUser on the bucket (the catalogue's only writer);
#   * the intake account: objectViewer under quarantine/ and objectCreator
#     under staging/, nothing else anywhere;
#   * the reconciler: objectViewer on the bucket and the delete-only role
#     under platform/ and tenants/ (garbage collection);
#   * each listed tenant's worker: objectViewer under platform/ and its own
#     tenants/<t>/ only, never another tenant's;
#   * swarm-api: secretVersionAdder on swarm-tenant-<t>-plg-* names only;
#   * the intake job: no egress (its own network, no NAT, egress denied but
#     HTTPS), no secret in its environment, no database, max one try; it is
#     planned only once its gated image has a digest, and swarm-api may start
#     it with overrides.
#
# Conditions are EVALUATED against admitted and refused object names with the
# startsWith() emulation below, so dropping or widening a clause turns a run
# red.

mock_provider "google" {}

override_data {
  target = data.google_kms_crypto_key_versions.step_spec
  values = {
    versions = [
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 1
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
    ]
  }
}

override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic"]
    }
    smoke = {
      kind      = "group"
      principal = "smoke@example.com"
      providers = []
    }
  }

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-voice"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-voice@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  platform_owner = "bgd@example.com"
}

run "the_bucket_and_its_grants" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      google_storage_bucket.plugins.name == "saga-agents-staging-swarm-plugins"
      && google_storage_bucket.plugins.uniform_bucket_level_access == true
      && google_storage_bucket.plugins.public_access_prevention == "enforced"
      && google_storage_bucket.plugins.labels["managed-by"] == "swarm-terraform"
      && google_storage_bucket.plugins.location == "us-central1"
    )
    error_message = "the plugins bucket is <project>-swarm-plugins, uniform access, public access prevented, managed-by=swarm-terraform, in the region (section 3)"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_api_objects.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_api_objects.role == "roles/storage.objectUser"
      && length(google_storage_bucket_iam_member.plugins_api_objects.condition) == 0
    )
    error_message = "swarm-api holds objectUser on the plugins bucket, the catalogue's one writer"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_intake_read.member == "serviceAccount:swarm-plugin-intake@saga-agents-staging.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_intake_read.role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.plugins_intake_read.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/saga-agents-staging-swarm-plugins/objects/quarantine/\")"
      && startswith(google_storage_bucket_iam_member.plugins_intake_read.condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the intake account reads under quarantine/ only (objectViewer, conditioned)"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_intake_stage.member == "serviceAccount:swarm-plugin-intake@saga-agents-staging.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_intake_stage.role == "roles/storage.objectCreator"
      && google_storage_bucket_iam_member.plugins_intake_stage.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/saga-agents-staging-swarm-plugins/objects/staging/\")"
      && startswith(google_storage_bucket_iam_member.plugins_intake_stage.condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the intake account creates under staging/ only (objectCreator: no overwrite, no delete)"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_reconciler_list.member == "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_reconciler_list.role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.plugins_reconciler_gc.member == "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_reconciler_gc.role == "projects/saga-agents-staging/roles/swarmHistoryPurgeDeleter"
      && google_storage_bucket_iam_member.plugins_reconciler_gc.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/saga-agents-staging-swarm-plugins/objects/platform/\") || resource.name.startsWith(\"projects/_/buckets/saga-agents-staging-swarm-plugins/objects/tenants/\")"
    )
    error_message = "the reconciler lists the bucket and deletes, with the delete-only role, under platform/ and tenants/ only (section 9.3)"
  }

  assert {
    condition = (
      toset(keys(google_storage_bucket_iam_member.plugins_tenant_read)) == toset(["eng", "smoke"])
      && alltrue([
        for t, g in google_storage_bucket_iam_member.plugins_tenant_read : (
          g.member == "serviceAccount:swarm-agent-worker-${t}@saga-agents-staging.iam.gserviceaccount.com"
          && g.role == "roles/storage.objectViewer"
          && g.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/saga-agents-staging-swarm-plugins/objects/platform/\") || resource.name.startsWith(\"projects/_/buckets/saga-agents-staging-swarm-plugins/objects/tenants/${t}/\")"
          && startswith(g.condition[0].description, "managed-by=swarm-terraform;")
        )
      ])
    )
    error_message = "every listed tenant's worker reads platform/ and its own tenants/<t>/ only"
  }

  # Evaluated: each tenant's condition admits platform/ and its own prefix and
  # refuses the other tenant's, quarantine/, staging/ and a lookalike tenant.
  assert {
    condition = alltrue([
      for case in [
        ["eng", "platform/abc.tar", true],
        ["eng", "tenants/eng/abc.tar", true],
        ["eng", "tenants/smoke/abc.tar", false],
        ["eng", "tenants/engx/abc.tar", false],
        ["eng", "quarantine/i1", false],
        ["eng", "staging/i1/report.json", false],
        ["smoke", "tenants/smoke/abc.tar", true],
        ["smoke", "tenants/eng/abc.tar", false],
        ] : (
        anytrue([
          startswith("projects/_/buckets/saga-agents-staging-swarm-plugins/objects/${case[1]}", "projects/_/buckets/saga-agents-staging-swarm-plugins/objects/platform/"),
          startswith("projects/_/buckets/saga-agents-staging-swarm-plugins/objects/${case[1]}", "projects/_/buckets/saga-agents-staging-swarm-plugins/objects/tenants/${case[0]}/"),
        ]) == case[2]
        && strcontains(google_storage_bucket_iam_member.plugins_tenant_read[case[0]].condition[0].expression, "objects/tenants/${case[0]}/\")")
      )
    ])
    error_message = "a tenant reads platform/ and its own prefix, never another tenant's"
  }

  assert {
    condition = (
      google_project_iam_member.plugins_api_secret_versions.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && google_project_iam_member.plugins_api_secret_versions.role == "roles/secretmanager.secretVersionAdder"
      && google_project_iam_member.plugins_api_secret_versions.condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-plg-\") != \"\""
      && startswith(google_project_iam_member.plugins_api_secret_versions.condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "swarm-api adds versions to swarm-tenant-<t>-plg-* secrets only"
  }

  # Nothing grants the intake account a secret, a database or a project role.
  assert {
    condition = alltrue([
      for m in [
        google_project_iam_member.plugins_api_secret_versions.member,
        google_storage_bucket_iam_member.plugins_api_objects.member,
        google_storage_bucket_iam_member.plugins_reconciler_list.member,
        google_storage_bucket_iam_member.plugins_reconciler_gc.member,
      ] : m != "serviceAccount:swarm-plugin-intake@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "the intake account holds the two conditioned bucket grants and nothing else"
  }

  # Without its image's digest the job is not planned (a gated image).
  assert {
    condition     = length(google_cloud_run_v2_job.plugin_intake) == 0 && length(google_cloud_run_v2_job_iam_member.plugin_intake_api_runs) == 0
    error_message = "the intake job is planned only once the release carries swarm-plugin-intake's digest"
  }

  assert {
    condition     = output.plugins_bucket == "saga-agents-staging-swarm-plugins"
    error_message = "the plugins_bucket output names the bucket the installer checks"
  }
}

run "the_intake_job_has_no_egress_and_no_secret" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    image_refs = merge(var.image_refs, {
      "swarm-plugin-intake" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-plugin-intake@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition = (
      google_cloud_run_v2_job.plugin_intake[0].name == "swarm-plugin-intake"
      && google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].service_account == "swarm-plugin-intake@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].max_retries == 0
      && google_cloud_run_v2_job.plugin_intake[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the intake job runs as its own account, tries once, and is labelled managed-by=swarm-terraform"
  }

  # No egress: all traffic through the intake network, which has no NAT and
  # admits HTTPS alone; the job is tagged so the allow rule is its only one.
  assert {
    condition = (
      google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC"
      && google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].vpc_access[0].network_interfaces[0].tags == tolist(["swarm-plugin-intake"])
      && google_compute_network.plugin_intake.name == "swarm-plugin-intake"
      && google_compute_network.plugin_intake.auto_create_subnetworks == false
      && google_compute_subnetwork.plugin_intake.private_ip_google_access == true
      && google_compute_firewall.plugin_intake_deny_egress.direction == "EGRESS"
      && google_compute_firewall.plugin_intake_deny_egress.destination_ranges == toset(["0.0.0.0/0"])
      && one(google_compute_firewall.plugin_intake_deny_egress.deny).protocol == "all"
      && one(google_compute_firewall.plugin_intake_allow_google_apis.allow).protocol == "tcp"
      && one(google_compute_firewall.plugin_intake_allow_google_apis.allow).ports == tolist(["443"])
      && google_compute_firewall.plugin_intake_allow_google_apis.priority < google_compute_firewall.plugin_intake_deny_egress.priority
    )
    error_message = "the intake job leaves only through its own NAT-less network, which denies all egress but HTTPS to Google's APIs"
  }

  # No secret: no env value comes from Secret Manager, and none names one.
  assert {
    condition = alltrue([
      for e in google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].env :
      length(e.value_source) == 0 && !strcontains(lower(e.name), "secret") && !strcontains(lower(e.name), "token")
    ])
    error_message = "the intake job's environment holds no secret reference"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].volumes) == 0
      && toset([for e in google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].env : e.name]) == toset(["PLUGINS_BUCKET", "PLUGIN_MAX_PACKED_BYTES", "PLUGIN_MAX_UNPACKED_BYTES", "PLUGIN_MAX_FILES"])
    )
    error_message = "the intake job mounts nothing and is told only the bucket and the caps"
  }

  assert {
    condition = (
      { for e in google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].env : e.name => e.value }["PLUGIN_MAX_PACKED_BYTES"] == "104857600"
      && { for e in google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].env : e.name => e.value }["PLUGIN_MAX_UNPACKED_BYTES"] == "524288000"
      && { for e in google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].env : e.name => e.value }["PLUGIN_MAX_FILES"] == "20000"
    )
    error_message = "the caps are the owner's: 100 MiB packed, 500 MiB unpacked, 20,000 files"
  }

  assert {
    condition = (
      google_cloud_run_v2_job_iam_member.plugin_intake_api_runs[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_run_v2_job_iam_member.plugin_intake_api_runs[0].role == "roles/run.jobsExecutorWithOverrides"
      && google_cloud_run_v2_job_iam_member.plugin_intake_api_runs[0].name == "swarm-plugin-intake"
    )
    error_message = "swarm-api starts the intake job with overrides, on that job only"
  }
}
