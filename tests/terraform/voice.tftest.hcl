# Voice it's swarm-voice, the infrastructure side (docs/design/voice-it.md
# §3.2, §3.6, §7, section 10 phase one; lane VOICE-STATEFULSET, part of
# #2688): terraform/infra/voice.tf, swarm-api's SWARM_VOICE_URL in locals.tf,
# and kubernetes/swarm-system/swarm-voice.yaml with
# kubernetes/network-policies/swarm-system-voice.yaml as voice.tf renders
# them.
#
# What is held here, from the phase-one row:
#
#   * every resource that can carry a label carries managed-by=swarm-terraform
#     (an IAM binding cannot), and so does every rendered Kubernetes object;
#   * swarm-voice is READ, never created: terraform/bootstrap makes it
#     (platform_service_accounts.tf, whose description carries
#     managed-by=swarm-terraform), and voice.tf binds on the account it read;
#   * each Secret Manager grant names ONE secret, the platform owner's own
#     OpenAI secret, and only the two roles credential.py needs: read, and
#     add-version for a rotated refresh token;
#   * the owner's personal tenant is computed as
#     swarm_common.identity.tenant_id_for_user computes it (the expected
#     values below were printed by that function);
#   * run.invoker on swarm-api is the only other grant;
#   * off by default nothing exists, and swarm-api has no SWARM_VOICE_URL.
#
# The infra root's setup is live.tftest.hcl's.

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

# The swarm-voice account terraform/bootstrap made, as a read returns it.
override_data {
  target          = data.google_service_account.voice
  override_during = plan
  values = {
    email = "swarm-voice@saga-agents-staging.iam.gserviceaccount.com"
    name  = "projects/saga-agents-staging/serviceAccounts/swarm-voice@saga-agents-staging.iam.gserviceaccount.com"
  }
}

# swarm-voice's internal load balancer address is computed by GCE; a fixed
# one lets the rendered manifest and SWARM_VOICE_URL be read at plan.
override_resource {
  target          = google_compute_address.voice
  override_during = plan
  values = {
    address = "10.10.0.42"
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
      providers = ["anthropic", "openai"]
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

  # Read as text by the assertion that lists voice.tf's resources.
  tf_root = "../../terraform/infra"
}

run "off_by_default_nothing_exists_and_swarm_api_has_no_address" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(data.google_service_account.voice) == 0
      && length(google_service_account_iam_member.voice_workload_identity) == 0
      && length(google_secret_manager_secret_iam_member.voice_reads_openai) == 0
      && length(google_secret_manager_secret_iam_member.voice_stores_openai) == 0
      && length(google_cloud_run_v2_service_iam_member.voice_invokes_api) == 0
      && length(google_compute_address.voice) == 0
      && length(google_storage_bucket_iam_member.api_voice_blob_writer) == 0
      && length(google_storage_bucket_iam_member.api_voice_blob_deleter) == 0
    )
    error_message = "with voice_it off swarm-voice's account is not read, and there is no grant and no address"
  }

  assert {
    condition     = output.swarm_voice_manifest == null && local.service_env["swarm-api"].SWARM_VOICE_URL == ""
    error_message = "with voice_it off there is no manifest to apply and swarm-api's SWARM_VOICE_URL is empty (every Voice it route answers 503)"
  }
}

run "account_reads_the_bootstrap_account_holding_nothing" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it = "account"
  }

  assert {
    condition = (
      length(data.google_service_account.voice) == 1
      && data.google_service_account.voice[0].account_id == "swarm-voice"
      && data.google_service_account.voice[0].project == "saga-agents-staging"
    )
    error_message = "step 1 reads swarm-voice, the account terraform/bootstrap made, by the id modules/service_account_ids spells"
  }

  assert {
    condition = (
      length(google_service_account_iam_member.voice_workload_identity) == 0
      && length(google_secret_manager_secret_iam_member.voice_reads_openai) == 0
      && length(google_secret_manager_secret_iam_member.voice_stores_openai) == 0
      && length(google_cloud_run_v2_service_iam_member.voice_invokes_api) == 0
      && output.swarm_voice_manifest == null
    )
    error_message = "step 1's account holds nothing and nothing runs as it"
  }
}

run "on_every_labelled_resource_carries_managed_by" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it = "on"
  }

  # voice.tf declares exactly these. A new resource fails here until its label
  # (or, for an IAM binding, its absence of one) is held below.
  assert {
    condition = toset([
      for m in regexall("(?m)^resource \"([a-z0-9_]+)\" \"([a-z0-9_]+)\"", file("${var.tf_root}/voice.tf")) : "${m[0]}.${m[1]}"
      ]) == toset([
      "google_service_account_iam_member.voice_workload_identity",
      "google_secret_manager_secret_iam_member.voice_reads_openai",
      "google_secret_manager_secret_iam_member.voice_stores_openai",
      "google_cloud_run_v2_service_iam_member.voice_invokes_api",
      "google_compute_address.voice",
      "google_storage_bucket_iam_member.api_voice_blob_writer",
      "google_storage_bucket_iam_member.api_voice_blob_deleter",
    ])
    error_message = "voice.tf declares the account's Workload Identity binding, two secret bindings, the invoker, the address and swarm-api's two conditioned blob grants (voice_storage.tftest.hcl holds those), and nothing else"
  }

  # The account is bootstrap's: infra reads it and never declares one.
  assert {
    condition = (
      length(regexall("(?m)^resource \"google_service_account\" ", file("${var.tf_root}/voice.tf"))) == 0
      && length(regexall("(?m)^data \"google_service_account\" \"voice\" \\{", file("${var.tf_root}/voice.tf"))) == 1
    )
    error_message = "voice.tf reads swarm-voice as data.google_service_account.voice and creates no service account; terraform/bootstrap creates it"
  }

  assert {
    condition     = google_compute_address.voice[0].labels["managed-by"] == "swarm-terraform"
    error_message = "the reserved address carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  assert {
    condition     = google_service_account_iam_member.voice_workload_identity[0].service_account_id == data.google_service_account.voice[0].name
    error_message = "the Workload Identity binding is on the account voice.tf read, the one bootstrap made"
  }

  # Every rendered object is labelled and sits in swarm-system; no RBAC.
  assert {
    condition = alltrue([
      for d in [for s in split("\n---\n", output.swarm_voice_manifest) : yamldecode(s)] :
      d.metadata.labels["managed-by"] == "swarm-terraform"
      && (d.kind == "Namespace" ? d.metadata.name : try(d.metadata.namespace, "")) == "swarm-system"
      && !contains(["ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"], d.kind)
    ])
    error_message = "every object swarm_voice_manifest renders carries managed-by=swarm-terraform, sits in swarm-system and grants no Kubernetes RBAC"
  }

  assert {
    condition     = length(regexall("__[A-Z0-9_]+__", output.swarm_voice_manifest)) == 0
    error_message = "every placeholder in swarm-voice.yaml and swarm-system-voice.yaml is rendered"
  }
}

run "on_each_secret_grant_names_only_the_owners_openai_secret" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it = "on"
  }

  # tenant_id_for_user("bgd@example.com") == "u-bgd".
  assert {
    condition     = output.voice.openai_secret == "swarm-tenant-u-bgd-openai"
    error_message = "the secret is the platform owner's own OpenAI secret, swarm-tenant-<tenant_id_for_user(owner)>-openai"
  }

  assert {
    condition = (
      google_secret_manager_secret_iam_member.voice_reads_openai[0].secret_id == "swarm-tenant-u-bgd-openai"
      && google_secret_manager_secret_iam_member.voice_reads_openai[0].role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_member.voice_reads_openai[0].member == "serviceAccount:swarm-voice@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "read is roles/secretmanager.secretAccessor for swarm-voice on swarm-tenant-u-bgd-openai, and on no other secret"
  }

  assert {
    condition = (
      google_secret_manager_secret_iam_member.voice_stores_openai[0].secret_id == "swarm-tenant-u-bgd-openai"
      && google_secret_manager_secret_iam_member.voice_stores_openai[0].role == "roles/secretmanager.secretVersionAdder"
      && google_secret_manager_secret_iam_member.voice_stores_openai[0].member == "serviceAccount:swarm-voice@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "storing a rotated refresh token is roles/secretmanager.secretVersionAdder for swarm-voice on swarm-tenant-u-bgd-openai, and on no other secret"
  }

  # Each is a binding ON the secret, never a project-level grant: voice.tf
  # makes no google_project_iam_* and no google_secret_manager_secret_iam_binding
  # (which would replace the secret's other members) or _policy.
  assert {
    condition = (
      !strcontains(file("${var.tf_root}/voice.tf"), "resource \"google_project_iam")
      && !strcontains(file("${var.tf_root}/voice.tf"), "google_secret_manager_secret_iam_binding")
      && !strcontains(file("${var.tf_root}/voice.tf"), "google_secret_manager_secret_iam_policy")
      && !strcontains(file("${var.tf_root}/voice.tf"), "google_secret_manager_secret_version")
    )
    error_message = "voice.tf grants on the one secret only, replaces no member list, and never holds a secret value"
  }

  assert {
    condition = (
      google_cloud_run_v2_service_iam_member.voice_invokes_api[0].name == "swarm-api"
      && google_cloud_run_v2_service_iam_member.voice_invokes_api[0].role == "roles/run.invoker"
      && google_cloud_run_v2_service_iam_member.voice_invokes_api[0].member == "serviceAccount:swarm-voice@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "swarm-voice's one other grant is run.invoker on swarm-api"
  }

  # Only the KSA swarm-system/swarm-voice acts as the account.
  assert {
    condition = (
      google_service_account_iam_member.voice_workload_identity[0].role == "roles/iam.workloadIdentityUser"
      && google_service_account_iam_member.voice_workload_identity[0].member == "serviceAccount:saga-agents-staging.svc.id.goog[swarm-system/swarm-voice]"
    )
    error_message = "the Workload Identity binding admits exactly the KSA swarm-system/swarm-voice"
  }
}

run "a_lossy_owner_address_names_the_digested_tenant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it       = "on"
    platform_owner = "first.last@example.com"
  }

  # tenant_id_for_user("first.last@example.com") == "u-fi-ed5404".
  assert {
    condition     = google_secret_manager_secret_iam_member.voice_reads_openai[0].secret_id == "swarm-tenant-u-fi-ed5404-openai"
    error_message = "a local part slugification changes is cut and digested exactly as tenant_id_for_user does"
  }
}

run "a_long_owner_address_names_the_digested_tenant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it       = "on"
    platform_owner = "abcdefghij@example.com"
  }

  # tenant_id_for_user("abcdefghij@example.com") == "u-ab-e1b541".
  assert {
    condition     = google_secret_manager_secret_iam_member.voice_stores_openai[0].secret_id == "swarm-tenant-u-ab-e1b541-openai"
    error_message = "a local part longer than nine characters is cut and digested exactly as tenant_id_for_user does"
  }
}

run "on_swarm_api_calls_the_internal_address_and_swarm_voice_admits_only_swarm_api" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it = "on"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_VOICE_URL == "http://10.10.0.42:8080"
    error_message = "swarm-api's SWARM_VOICE_URL is http://<swarm-voice's reserved internal address>:8080"
  }

  assert {
    condition = (
      strcontains(output.swarm_voice_manifest, "value: http://10.10.0.42:8080")
      && strcontains(output.swarm_voice_manifest, "value: swarm-api@saga-agents-staging.iam.gserviceaccount.com")
      && strcontains(output.swarm_voice_manifest, "iam.gke.io/gcp-service-account: swarm-voice@saga-agents-staging.iam.gserviceaccount.com")
      && strcontains(output.swarm_voice_manifest, "loadBalancerIP: 10.10.0.42")
    )
    error_message = "the rendered pod checks for the URL swarm-api mints its token for, admits swarm-api's account alone, and acts as swarm-voice's account"
  }
}

run "voice_it_runs_only_on_gke" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it             = "on"
    enable_gke_autopilot = false
  }

  assert {
    condition     = length(google_service_account_iam_member.voice_workload_identity) == 0 && output.swarm_voice_manifest == null
    error_message = "without the Autopilot cluster swarm-voice gets no binding and no manifest"
  }

  expect_failures = [check.voice_it_needs_gke]
}
