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
#   * the deployer's actAs on the intake account: planned only with the job
#     and a named deployer, never before the intake image ships.
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
        id               = "projects/example-project/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/example-project/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/example-project/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
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
    number = "123456789012"
  }
}

variables {
  project_id  = "example-project"
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
    "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-voice"              = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-voice@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  platform_owner = "owner@example.com"
}

run "the_bucket_and_its_grants" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      google_storage_bucket.plugins.name == "example-project-swarm-plugins"
      && google_storage_bucket.plugins.uniform_bucket_level_access == true
      && google_storage_bucket.plugins.public_access_prevention == "enforced"
      && google_storage_bucket.plugins.labels["managed-by"] == "swarm-terraform"
      && google_storage_bucket.plugins.location == "us-central1"
    )
    error_message = "the plugins bucket is <project>-swarm-plugins, uniform access, public access prevented, managed-by=swarm-terraform, in the region (section 3)"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_api_objects.member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_api_objects.role == "roles/storage.objectUser"
      && length(google_storage_bucket_iam_member.plugins_api_objects.condition) == 0
    )
    error_message = "swarm-api holds objectUser on the plugins bucket, the catalogue's one writer"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_intake_read.member == "serviceAccount:swarm-plugin-intake@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_intake_read.role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.plugins_intake_read.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/quarantine/\")"
      && startswith(google_storage_bucket_iam_member.plugins_intake_read.condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the intake account reads under quarantine/ only (objectViewer, conditioned)"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_intake_stage.member == "serviceAccount:swarm-plugin-intake@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_intake_stage.role == "roles/storage.objectCreator"
      && google_storage_bucket_iam_member.plugins_intake_stage.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/staging/\")"
      && startswith(google_storage_bucket_iam_member.plugins_intake_stage.condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "the intake account creates under staging/ only (objectCreator: no overwrite, no delete)"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.plugins_reconciler_list.member == "serviceAccount:swarm-reconciler@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_reconciler_list.role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.plugins_reconciler_gc.member == "serviceAccount:swarm-reconciler@example-project.iam.gserviceaccount.com"
      && google_storage_bucket_iam_member.plugins_reconciler_gc.role == "projects/example-project/roles/swarmHistoryPurgeDeleter"
      && google_storage_bucket_iam_member.plugins_reconciler_gc.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/platform/\") || resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/tenants/\")"
    )
    error_message = "the reconciler lists the bucket and deletes, with the delete-only role, under platform/ and tenants/ only (section 9.3)"
  }

  assert {
    condition = (
      toset(keys(google_storage_bucket_iam_member.plugins_tenant_read)) == toset(["eng", "smoke"])
      && alltrue([
        for t, g in google_storage_bucket_iam_member.plugins_tenant_read : (
          g.member == "serviceAccount:swarm-agent-worker-${t}@example-project.iam.gserviceaccount.com"
          && g.role == "roles/storage.objectViewer"
          && g.condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/platform/\") || resource.name.startsWith(\"projects/_/buckets/example-project-swarm-plugins/objects/tenants/${t}/\")"
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
          startswith("projects/_/buckets/example-project-swarm-plugins/objects/${case[1]}", "projects/_/buckets/example-project-swarm-plugins/objects/platform/"),
          startswith("projects/_/buckets/example-project-swarm-plugins/objects/${case[1]}", "projects/_/buckets/example-project-swarm-plugins/objects/tenants/${case[0]}/"),
        ]) == case[2]
        && strcontains(google_storage_bucket_iam_member.plugins_tenant_read[case[0]].condition[0].expression, "objects/tenants/${case[0]}/\")")
      )
    ])
    error_message = "a tenant reads platform/ and its own prefix, never another tenant's"
  }

  assert {
    condition = (
      google_project_iam_member.plugins_api_secret_versions.member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com"
      && google_project_iam_member.plugins_api_secret_versions.role == "roles/secretmanager.secretVersionAdder"
      && google_project_iam_member.plugins_api_secret_versions.condition[0].expression == "resource.name.startsWith(\"projects/123456789012/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-plg-\") != \"\""
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
      ] : m != "serviceAccount:swarm-plugin-intake@example-project.iam.gserviceaccount.com"
    ])
    error_message = "the intake account holds the two conditioned bucket grants and nothing else"
  }

  # Without its image's digest the job is not planned (a gated image).
  assert {
    condition     = length(google_cloud_run_v2_job.plugin_intake) == 0 && length(google_cloud_run_v2_job_iam_member.plugin_intake_api_runs) == 0
    error_message = "the intake job is planned only once the release carries swarm-plugin-intake's digest"
  }

  assert {
    condition     = output.plugins_bucket == "example-project-swarm-plugins"
    error_message = "the plugins_bucket output names the bucket the installer checks"
  }
}

run "the_bucket_keeps_a_week_of_deleted_copies_and_logs_access" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      google_storage_bucket.plugins.versioning[0].enabled == true
      && length([for r in google_storage_bucket.plugins.lifecycle_rule : r if one(r.condition).days_since_noncurrent_time == 7 && one(r.action).type == "Delete"]) == 1
      && google_storage_bucket.plugins.logging[0].log_object_prefix == "swarm-plugins"
      && google_compute_subnetwork.plugin_intake.log_config[0].flow_sampling == 1
    )
    error_message = "the plugins bucket is versioned (noncurrent copies kept 7 days) with access logs, and the intake subnetwork logs every flow"
  }
}

# The console's upload (Config > Plugins > Add plugin > Upload) is a browser
# PUT to swarm-api's signed URL on this bucket, cross-origin: one CORS rule
# admits the console's own origin, PUT only, with exactly the two headers the
# URL is signed with. A deployment with no console plans no rule at all.
run "the_console_may_put_to_its_signed_upload_url" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_frontend   = true
    frontend_hostname = "swarm.example.test"
  }

  assert {
    condition = (
      length(google_storage_bucket.plugins.cors) == 1
      && google_storage_bucket.plugins.cors[0].origin == tolist(["https://swarm.example.test"])
      && google_storage_bucket.plugins.cors[0].method == tolist(["PUT"])
      && toset(google_storage_bucket.plugins.cors[0].response_header) == toset(["Content-Type", "x-goog-content-length-range"])
      && google_storage_bucket.plugins.cors[0].max_age_seconds <= 900
    )
    error_message = "the plugins bucket admits a cross-origin PUT from the console's own origin only, with the signed Content-Type and x-goog-content-length-range headers"
  }
}

run "no_console_no_cors_rule" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = length(google_storage_bucket.plugins.cors) == 0
    error_message = "without a console (enable_frontend off or no frontend_hostname) the plugins bucket carries no CORS rule"
  }
}

# The intake's callback: the bucket announces staging/ finalizes on its own
# topic, Cloud Storage's agent may publish there and nowhere else, and the push
# reaches swarm-api's callback route with an OIDC token for the push audience
# from the rollup sweeper -- the audience and account swarm-api is told to
# admit, so the two sides cannot disagree.
run "the_intake_report_reaches_swarm_api" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      google_storage_notification.plugin_intake_reports.bucket == "example-project-swarm-plugins"
      && google_storage_notification.plugin_intake_reports.object_name_prefix == "staging/"
      && google_storage_notification.plugin_intake_reports.event_types == toset(["OBJECT_FINALIZE"])
      && google_storage_notification.plugin_intake_reports.payload_format == "JSON_API_V1"
      && google_storage_notification.plugin_intake_reports.custom_attributes["managed-by"] == "swarm-terraform"
      && google_pubsub_topic.plugin_intake_reports.name == "swarm-plugin-intake-reports"
      && google_pubsub_topic.plugin_intake_reports.labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the plugins bucket announces OBJECT_FINALIZE under staging/ only, on its own managed-by topic"
  }

  assert {
    condition = (
      google_pubsub_topic_iam_member.plugin_intake_reports_gcs_publishes.member == "serviceAccount:service-123456789012@gs-project-accounts.iam.gserviceaccount.com"
      && google_pubsub_topic_iam_member.plugin_intake_reports_gcs_publishes.role == "roles/pubsub.publisher"
      && google_pubsub_topic_iam_member.plugin_intake_reports_gcs_publishes.topic == "swarm-plugin-intake-reports"
    )
    error_message = "Cloud Storage's agent publishes to the intake reports topic, that topic only"
  }

  assert {
    condition = (
      google_pubsub_subscription.plugin_intake_reports.push_config[0].oidc_token[0].audience == "https://swarm-api.dev.swarm.internal"
      && google_pubsub_subscription.plugin_intake_reports.push_config[0].oidc_token[0].service_account_email == module.scheduler.rollup_sweeper_email
      && google_pubsub_subscription.plugin_intake_reports.labels["managed-by"] == "swarm-terraform"
      && length(google_pubsub_subscription.plugin_intake_reports.dead_letter_policy) == 0
    )
    # The push endpoint is swarm-api's URL, unknown until apply, so its path
    # is not asserted at plan.
    error_message = "the push presents an OIDC token for swarm-api's push audience from the rollup sweeper, no dead letter"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].SWARM_PLUGINS_BUCKET == "example-project-swarm-plugins"
      && local.service_env["swarm-api"].SWARM_PLUGIN_INTAKE_JOB == "swarm-plugin-intake"
      && local.service_env["swarm-api"].SWARM_PLUGIN_INTAKE_AUDIENCE == google_pubsub_subscription.plugin_intake_reports.push_config[0].oidc_token[0].audience
      && local.service_env["swarm-api"].SWARM_PLUGIN_INTAKE_CALLERS == google_pubsub_subscription.plugin_intake_reports.push_config[0].oidc_token[0].service_account_email
    )
    error_message = "swarm-api is told the bucket, the job, and exactly the audience and account the push presents"
  }
}

run "the_intake_job_has_no_egress_and_no_secret" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    image_refs = merge(var.image_refs, {
      "swarm-plugin-intake" = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-plugin-intake@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition = (
      google_cloud_run_v2_job.plugin_intake[0].name == "swarm-plugin-intake"
      && google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].service_account == "swarm-plugin-intake@example-project.iam.gserviceaccount.com"
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
      && length(google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].volume_mounts) == 0
      && toset([for e in google_cloud_run_v2_job.plugin_intake[0].template[0].template[0].containers[0].env : e.name]) == toset(["PLUGINS_BUCKET", "PLUGIN_MAX_PACKED_BYTES", "PLUGIN_MAX_UNPACKED_BYTES", "PLUGIN_MAX_FILES"])
    )
    error_message = "the intake job mounts nothing (it reads and writes the bucket through the API: a FUSE mount lists) and is told only the bucket and the caps"
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
      google_cloud_run_v2_job_iam_member.plugin_intake_api_runs[0].member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com"
      && google_cloud_run_v2_job_iam_member.plugin_intake_api_runs[0].role == "roles/run.jobsExecutorWithOverrides"
      && google_cloud_run_v2_job_iam_member.plugin_intake_api_runs[0].name == "swarm-plugin-intake"
    )
    error_message = "swarm-api starts the intake job with overrides, on that job only"
  }
}

# The deployer's actAs on the intake account exists only to deploy the intake
# job, and the deployer cannot read that account's policy until bootstrap grants
# it, so the grant is planned only with the job: the image's digest AND a named
# deployer. Either one alone plans nothing.
run "the_deployer_acts_as_the_intake_account_only_with_the_job" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    deployer_service_account = "swarm-tf-deployer@${var.project_id}.iam.gserviceaccount.com"
    image_refs = merge(var.image_refs, {
      "swarm-plugin-intake" = "us-central1-docker.pkg.dev/${var.project_id}/swarm-images/swarm-plugin-intake@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition = (
      length(google_service_account_iam_member.plugin_intake_deployer_acts_as) == 1
      && google_service_account_iam_member.plugin_intake_deployer_acts_as[0].service_account_id == "projects/${var.project_id}/serviceAccounts/swarm-plugin-intake@${var.project_id}.iam.gserviceaccount.com"
      && google_service_account_iam_member.plugin_intake_deployer_acts_as[0].role == "roles/iam.serviceAccountUser"
      && google_service_account_iam_member.plugin_intake_deployer_acts_as[0].member == "serviceAccount:swarm-tf-deployer@${var.project_id}.iam.gserviceaccount.com"
    )
    error_message = "with the intake image and a named deployer, the deployer acts as the intake account"
  }
}

run "no_intake_image_no_deployer_acts_as_grant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    deployer_service_account = "swarm-tf-deployer@${var.project_id}.iam.gserviceaccount.com"
  }

  assert {
    condition     = length(google_cloud_run_v2_job.plugin_intake) == 0 && length(google_service_account_iam_member.plugin_intake_deployer_acts_as) == 0
    error_message = "before the intake image ships, the deployer's actAs on the intake account is not planned (it would fail 403 getIamPolicy)"
  }
}

run "no_deployer_no_deployer_acts_as_grant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    image_refs = merge(var.image_refs, {
      "swarm-plugin-intake" = "us-central1-docker.pkg.dev/${var.project_id}/swarm-images/swarm-plugin-intake@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    })
  }

  assert {
    condition     = length(google_cloud_run_v2_job.plugin_intake) == 1 && length(google_service_account_iam_member.plugin_intake_deployer_acts_as) == 0
    error_message = "with no deployer named, the intake job is planned without a deployer actAs grant"
  }
}
