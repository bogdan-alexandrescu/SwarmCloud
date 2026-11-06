# Browser egress, the platform side (docs/browser-egress.md §2.2, §3.1, §3.5,
# §6.1; lane EGRESS-SVC, part of #1030): terraform/infra/egress.tf and
# kubernetes/swarm-system/swarm-egress.yaml as it renders it.
#
# What is held here:
#
#   * the vendor credential's secret has ONE reader, swarm-egress's KSA
#     principal, and none at all while the service is off: never swarm-api,
#     never a tenant service account;
#   * swarm-egress-key is read by swarm-api and swarm-egress, nobody else;
#   * NO SECRET VERSION: both are containers, their values added by the owner;
#   * the switch ships OFF, in swarm-api's environment and swarm-egress's
#     setting alike, and swarm-api carries no key reference while off;
#   * the $150 pilot cap (owner decision Q3) becomes the byte cap swarm-egress
#     enforces, at the conservative per-GB price;
#   * every labelable resource carries managed-by=swarm-terraform;
#   * enable_browser_egress with a swarm-egress digest in image_refs renders
#     the manifest, and without one renders none and fails its check
#     (EGRESS-BUILD: the release now builds that digest);
#   * dev turns it on for u-bogdan's claude-code-browser only, US exit, the
#     $150 cap (owner decision 2026-10-14), held against dev.tfvars' text.
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

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  # Read by the dev run's assertions only (live.tftest.hcl does the same).
  tf_root = "../../terraform/infra"

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
    "swarm-egress"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_nothing_reads_the_credential_and_nothing_is_proxied" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      google_secret_manager_secret.browser_egress.secret_id == "swarm-egress-credential"
      && google_secret_manager_secret.egress_key.secret_id == "swarm-egress-key"
    )
    error_message = "the credential is swarm-egress-credential and the ticket key swarm-egress-key (docs/browser-egress.md §3.1)"
  }

  assert {
    condition = (
      google_secret_manager_secret.browser_egress.labels["managed-by"] == "swarm-terraform"
      && google_secret_manager_secret.egress_key.labels["managed-by"] == "swarm-terraform"
    )
    error_message = "every resource carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  assert {
    condition     = length(google_secret_manager_secret_iam_binding.browser_egress_accessor) == 0
    error_message = "with enable_browser_egress off nobody may read the vendor credential"
  }

  assert {
    # swarm-api's member comes from the iam module and is unknown at plan; the
    # count, and the principal's absence, are what this holds.
    condition = (
      length(google_secret_manager_secret_iam_binding.egress_key_accessor.members) == 1
      && !contains(google_secret_manager_secret_iam_binding.egress_key_accessor.members, output.browser_egress.principal)
    )
    error_message = "with the service off only swarm-api reads swarm-egress-key"
  }

  assert {
    condition     = output.swarm_egress_manifest == null && output.browser_egress.enabled == false
    error_message = "with enable_browser_egress off there is no swarm-egress manifest to apply"
  }

  assert {
    condition = (
      local.service_env["swarm-api"]["BROWSER_EGRESS_ENABLED"] == "false"
      && local.service_env["swarm-api"]["BROWSER_EGRESS_TENANTS"] == ""
      && !contains(keys(local.api_secret_env), "SWARM_EGRESS_KEY")
    )
    error_message = "the switch ships OFF: swarm-api mints nothing and carries no egress key reference"
  }

  assert {
    condition     = local.egress_config.enabled == false && length(local.egress_config.tenants) == 0
    error_message = "swarm-egress's setting ships OFF with no tenant named (§6.1)"
  }

  assert {
    # The dispatcher's side (lane EGRESS-DISPATCH): off, so no browser pod is
    # labelled for the proxy path or told to route through it.
    condition = (
      local.service_env["swarm-scheduler"]["BROWSER_EGRESS_ENABLED"] == "false"
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_TENANTS"] == ""
    )
    error_message = "the switch ships OFF on the scheduler too: no pod is dispatched with SWARM_BROWSER_EGRESS=proxy"
  }
}

run "on_only_swarm_egress_reads_the_credential" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_browser_egress = true
    browser_egress = {
      enabled = true
      tenants = ["eng"]
    }
  }

  assert {
    condition = (
      length(google_secret_manager_secret_iam_binding.browser_egress_accessor) == 1
      && google_secret_manager_secret_iam_binding.browser_egress_accessor[0].role == "roles/secretmanager.secretAccessor"
      && toset(google_secret_manager_secret_iam_binding.browser_egress_accessor[0].members) == toset([
        "principal://iam.googleapis.com/projects/209012342332/locations/global/workloadIdentityPools/saga-agents-staging.svc.id.goog/subject/ns/swarm-system/sa/swarm-egress",
      ])
    )
    error_message = "the vendor credential has exactly one reader, swarm-egress's KSA principal: not swarm-api, not a tenant GSA (docs/browser-egress.md §2.2, §3.1)"
  }

  assert {
    # swarm-api's member is unknown at plan under the mock, and a set holding
    # an unknown has no known length either; the binding's role and secret,
    # and the principal it adds (egress.tf's concat), are what plan can show.
    condition = (
      google_secret_manager_secret_iam_binding.egress_key_accessor.role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.egress_key_accessor.secret_id == "swarm-egress-key"
      && output.browser_egress.principal == "principal://iam.googleapis.com/projects/209012342332/locations/global/workloadIdentityPools/saga-agents-staging.svc.id.goog/subject/ns/swarm-system/sa/swarm-egress"
    )
    error_message = "swarm-egress-key is read by swarm-api (mints) and swarm-egress (verifies), nobody else"
  }

  assert {
    condition = (
      local.service_env["swarm-api"]["BROWSER_EGRESS_ENABLED"] == "true"
      && local.service_env["swarm-api"]["BROWSER_EGRESS_TENANTS"] == "eng"
      && local.service_env["swarm-api"]["BROWSER_EGRESS_PROFILES"] == "claude-code-browser"
      && local.service_env["swarm-api"]["BROWSER_EGRESS_COUNTRY"] == "us"
      && local.api_secret_env["SWARM_EGRESS_KEY"].secret == "swarm-egress-key"
    )
    error_message = "swarm-api reads the same switch swarm-egress does, and the key by reference"
  }

  assert {
    # The dispatcher reads the SAME switch, so it never routes a pod swarm-api
    # would refuse a ticket, nor leaves direct a pod swarm-api would mint for
    # (docs/browser-egress.md §10.3). The bypass list is swarm-egress's.
    condition = (
      local.service_env["swarm-scheduler"]["BROWSER_EGRESS_ENABLED"] == local.service_env["swarm-api"]["BROWSER_EGRESS_ENABLED"]
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_TENANTS"] == local.service_env["swarm-api"]["BROWSER_EGRESS_TENANTS"]
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_PROFILES"] == local.service_env["swarm-api"]["BROWSER_EGRESS_PROFILES"]
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_ENABLED"] == "true"
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_BYPASS"] == join(",", local.egress_config.bypass_hosts)
    )
    error_message = "the scheduler dispatches by the switch swarm-api mints by, with swarm-egress's bypass list"
  }

  # $150 at $8/GB is 18.75 GB: the cap swarm-egress answers with before the
  # vendor's sub-user limit does.
  assert {
    condition = (
      local.egress_config.monthly_cap_bytes == 18750000000
      && local.egress_config.tenant_monthly_cap_bytes == 18750000000
      && local.egress_config.attempt_cap_bytes == 1000000000
      && local.egress_config.country == "us"
    )
    error_message = "the $150 pilot cap (owner decision Q3) renders as 18.75 GB at the conservative $8/GB, 1 GB per attempt, US exit"
  }

  assert {
    condition = alltrue([
      for h in ["github.com", ".github.com", ".githubusercontent.com"] : contains(local.egress_config.bypass_hosts, h)
    ])
    error_message = "GitHub goes direct (docs/browser-egress.md §2.3)"
  }

  assert {
    condition = (
      output.swarm_egress_manifest != null
      && length(regexall("__[A-Z0-9_]+__", output.swarm_egress_manifest)) == 0
      && strcontains(output.swarm_egress_manifest, "swarm-egress@sha256:8888")
      && strcontains(output.swarm_egress_manifest, "projects/saga-agents-staging/secrets/swarm-egress-credential/versions/latest")
      && strcontains(output.swarm_egress_manifest, "projects/saga-agents-staging/secrets/swarm-egress-key/versions/latest")
    )
    error_message = "the rendered manifest has no placeholder left, the image digest, and the two secrets by version reference"
  }
}

run "the_switch_without_the_service_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    browser_egress = {
      enabled = true
      tenants = ["eng"]
    }
  }

  expect_failures = [check.browser_egress_switch_needs_the_service]
}

# EGRESS-BUILD. The order egress.tf states: the image is built and its digest
# is in image_refs (step 3) BEFORE enable_browser_egress (step 4). On without
# a digest renders no manifest -- there is nothing kubernetes/apply.sh could
# apply -- and fails the check that says so. The same map as the file's,
# minus swarm-egress: a run's variables replace the whole map.
run "on_without_a_swarm_egress_digest_renders_no_manifest_and_fails_its_check" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_browser_egress = true
    image_refs = {
      "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
      "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }

  expect_failures = [check.browser_egress_has_an_image]

  assert {
    condition     = output.swarm_egress_manifest == null
    error_message = "with no swarm-egress digest there is no manifest to apply, never one naming an empty image"
  }
}

# dev, as committed (owner decision 2026-10-14): residential egress through
# Bright Data, US exit, the $150/month cap, for u-bogdan's claude-code-browser
# and nothing else. A terraform test cannot load a tfvars file into a run, so
# the run sets dev's values and the first two assertions hold dev.tfvars' text
# to them (live.tftest.hcl's "dev_turns_the_live_browser_on_end_to_end" does
# the same). With the file's swarm-egress digest, the manifest renders.
run "dev_turns_browser_egress_on_for_u_bogdans_live_browser_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_gke_autopilot  = true
    enable_browser_egress = true
    browser_egress = {
      enabled  = true
      tenants  = ["u-bogdan"]
      profiles = ["claude-code-browser"]
      country  = "us"
    }
  }

  assert {
    condition     = length(regexall("(?m)^enable_browser_egress\\s*=\\s*true\\b", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets enable_browser_egress = true (EGRESS-BUILD, owner decision 2026-10-14)"
  }

  assert {
    # The block as dev.tfvars writes it, whitespace aside: exactly these four
    # attributes, so a fifth (a raised cap, another tenant) fails here first.
    condition = (
      length(regexall("(?ms)^browser_egress\\s*=\\s*\\{\\s*enabled\\s*=\\s*true\\s+tenants\\s*=\\s*\\[\\s*\"u-bogdan\"\\s*\\]\\s+profiles\\s*=\\s*\\[\\s*\"claude-code-browser\"\\s*\\]\\s+country\\s*=\\s*\"us\"\\s*\\}", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    )
    error_message = "terraform/environments/dev/dev.tfvars sets browser_egress = { enabled = true, tenants = [\"u-bogdan\"], profiles = [\"claude-code-browser\"], country = \"us\" } and nothing more"
  }

  assert {
    condition = (
      output.browser_egress.enabled && output.browser_egress.switch_on
      && output.swarm_egress_manifest != null
      && strcontains(output.swarm_egress_manifest, "swarm-egress@sha256:8888")
    )
    error_message = "dev renders swarm-egress deployed and switched on, with a manifest naming the release's digest"
  }

  assert {
    condition = (
      local.service_env["swarm-api"]["BROWSER_EGRESS_ENABLED"] == "true"
      && local.service_env["swarm-api"]["BROWSER_EGRESS_TENANTS"] == "u-bogdan"
      && local.service_env["swarm-api"]["BROWSER_EGRESS_PROFILES"] == "claude-code-browser"
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_TENANTS"] == "u-bogdan"
      && local.service_env["swarm-scheduler"]["BROWSER_EGRESS_PROFILES"] == "claude-code-browser"
      && join(",", local.egress_config.tenants) == "u-bogdan"
      && local.egress_config.default_zone == "residential"
      && local.egress_config.country == "us"
    )
    error_message = "dev proxies u-bogdan's claude-code-browser only, through the residential zone, US exit"
  }

  assert {
    # $150 at the conservative $8/GB: the default, which dev does not raise.
    condition     = local.egress_config.monthly_cap_bytes == 18750000000
    error_message = "dev's monthly cap is the owner's $150 (18.75 GB at $8/GB)"
  }
}
