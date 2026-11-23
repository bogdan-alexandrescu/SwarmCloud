# Voice it's two alerts (terraform/infra/voice_alerts.tf): a voice session
# left live with no activity, and the speech machine left running with no
# session holding it. Both read the one structured line the speech sweep
# writes every two minutes, event `voice_sweep_state`
# (swarm_api/routes/voice_speech.py).
#
# What is held here:
#
#   * with the speech engine on, both log-based metrics and both policies
#     exist, each policy labelled managed-by=swarm-terraform and each metric
#     carrying the marker in its description (a log metric takes no labels);
#   * the thresholds are variables with defaults: the idle one is in the
#     voice metric's filter, the running one is the speech condition's
#     duration, and changing either replaces nothing (no metric label moves);
#   * a threshold the sweep itself would beat is refused;
#   * with the engine off nothing is made (nothing writes the line), and
#     create_alerts = false keeps the metrics but makes no policy.
#
# The infra root's setup is voice_speech.tftest.hcl's.

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

# The swarm-voice account terraform/bootstrap made, as a read returns it.
override_data {
  target          = data.google_service_account.voice
  override_during = plan
  values = {
    email = "swarm-voice@example-project.iam.gserviceaccount.com"
    name  = "projects/example-project/serviceAccounts/swarm-voice@example-project.iam.gserviceaccount.com"
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
  project_id  = "example-project"
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

  voice_it = "on"
}

# The swarm-speech account terraform/bootstrap made, as a read returns it.
override_data {
  target          = data.google_service_account.speech
  override_during = plan
  values = {
    email = "swarm-speech@example-project.iam.gserviceaccount.com"
    name  = "projects/example-project/serviceAccounts/swarm-speech@example-project.iam.gserviceaccount.com"
  }
}

# Computed by GCE; fixed so swarm-api's variables can be read at plan.
override_resource {
  target          = google_compute_address.speech
  override_during = plan
  values = {
    address = "10.10.0.43"
  }
}

override_resource {
  target          = google_compute_region_instance_template.speech
  override_during = plan
  values = {
    id = "projects/example-project/regions/us-central1/instanceTemplates/swarm-speech-20261109000000000000000001"
  }
}


run "the_engine_on_makes_both_alerts_with_their_defaults" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  assert {
    condition = (
      length(google_logging_metric.voice_session_idle) == 1 &&
      length(google_logging_metric.speech_machine_unheld) == 1 &&
      length(google_monitoring_alert_policy.voice_session_idle) == 1 &&
      length(google_monitoring_alert_policy.speech_machine_unheld) == 1
    )
    error_message = "with the speech engine on, voice_alerts.tf makes two metrics and two policies"
  }

  assert {
    condition = (
      google_monitoring_alert_policy.voice_session_idle[0].user_labels["managed-by"] == "swarm-terraform" &&
      google_monitoring_alert_policy.speech_machine_unheld[0].user_labels["managed-by"] == "swarm-terraform" &&
      strcontains(google_logging_metric.voice_session_idle[0].description, "managed-by=swarm-terraform") &&
      strcontains(google_logging_metric.speech_machine_unheld[0].description, "managed-by=swarm-terraform")
    )
    error_message = "every new resource carries managed-by=swarm-terraform (a log metric in its description)"
  }

  assert {
    condition = (
      google_monitoring_alert_policy.voice_session_idle[0].display_name == "swarm-dev-voice-session-idle" &&
      google_monitoring_alert_policy.speech_machine_unheld[0].display_name == "swarm-dev-speech-machine-unheld" &&
      google_logging_metric.voice_session_idle[0].name == "swarm/voice-session-idle" &&
      google_logging_metric.speech_machine_unheld[0].name == "swarm/speech-machine-unheld"
    )
    error_message = "the policies and metrics are not named as the platform's others are"
  }

  # Both read swarm-api's sweep line, and only it: the project is shared.
  assert {
    condition = alltrue([
      for m in [google_logging_metric.voice_session_idle[0], google_logging_metric.speech_machine_unheld[0]] : (
        strcontains(m.filter, "resource.type=\"cloud_run_revision\"") &&
        strcontains(m.filter, "resource.labels.service_name=(\"swarm-api\")") &&
        strcontains(m.filter, "resource.labels.location=\"us-central1\"") &&
        strcontains(m.filter, "jsonPayload.event=\"voice_sweep_state\"")
      )
    ])
    error_message = "a metric does not read swarm-api's voice_sweep_state line in the region"
  }

  # The defaults: 2700 s idle (the sweep ends an idle session at 1800 s) and
  # 1200 s running unheld (the sweep stops one at its next run).
  assert {
    condition = (
      strcontains(google_logging_metric.voice_session_idle[0].filter, "jsonPayload.voice_live_idle_seconds>=2700") &&
      strcontains(google_logging_metric.speech_machine_unheld[0].filter, "jsonPayload.speech_machine_unheld=true") &&
      google_monitoring_alert_policy.voice_session_idle[0].conditions[0].condition_threshold[0].duration == "0s" &&
      google_monitoring_alert_policy.speech_machine_unheld[0].conditions[0].condition_threshold[0].duration == "1200s"
    )
    error_message = "the default thresholds are not 2700 s idle and 1200 s running unheld"
  }

  assert {
    condition = alltrue([
      for p in [google_monitoring_alert_policy.voice_session_idle[0], google_monitoring_alert_policy.speech_machine_unheld[0]] : (
        p.severity == "WARNING" &&
        p.combiner == "OR" &&
        p.conditions[0].condition_threshold[0].comparison == "COMPARISON_GT" &&
        p.conditions[0].condition_threshold[0].threshold_value == 0 &&
        strcontains(p.conditions[0].condition_threshold[0].filter, "resource.type = \"cloud_run_revision\"") &&
        strcontains(p.documentation[0].content, "docs/operations.md")
      )
    ])
    error_message = "a policy is not a WARNING on any sweep line its metric counts, with its runbook"
  }

  assert {
    condition = (
      strcontains(google_monitoring_alert_policy.voice_session_idle[0].conditions[0].condition_threshold[0].filter, "logging.googleapis.com/user/swarm/voice-session-idle") &&
      strcontains(google_monitoring_alert_policy.speech_machine_unheld[0].conditions[0].condition_threshold[0].filter, "logging.googleapis.com/user/swarm/speech-machine-unheld")
    )
    error_message = "a policy does not watch its own metric"
  }
}

run "the_thresholds_are_variables" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
    voice_session_idle_alert_seconds    = 3600
    speech_machine_unheld_alert_seconds = 900
  }

  assert {
    condition = (
      strcontains(google_logging_metric.voice_session_idle[0].filter, "jsonPayload.voice_live_idle_seconds>=3600") &&
      google_monitoring_alert_policy.speech_machine_unheld[0].conditions[0].condition_threshold[0].duration == "900s"
    )
    error_message = "the thresholds are not read from their variables"
  }
}

# One plan for both refusals: each validation fails on its own variable.
run "thresholds_the_sweep_would_beat_are_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_session_idle_alert_seconds    = 1800
    speech_machine_unheld_alert_seconds = 300
  }

  expect_failures = [
    var.voice_session_idle_alert_seconds,
    var.speech_machine_unheld_alert_seconds,
  ]
}

run "the_engine_off_makes_neither" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_logging_metric.voice_session_idle) == 0 &&
      length(google_logging_metric.speech_machine_unheld) == 0 &&
      length(google_monitoring_alert_policy.voice_session_idle) == 0 &&
      length(google_monitoring_alert_policy.speech_machine_unheld) == 0
    )
    error_message = "with the speech engine off nothing writes the sweep line, so nothing is made"
  }
}

run "create_alerts_false_keeps_the_metrics_and_makes_no_policy" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
    create_alerts = false
  }

  assert {
    condition = (
      length(google_logging_metric.voice_session_idle) == 1 &&
      length(google_logging_metric.speech_machine_unheld) == 1 &&
      length(google_monitoring_alert_policy.voice_session_idle) == 0 &&
      length(google_monitoring_alert_policy.speech_machine_unheld) == 0
    )
    error_message = "create_alerts = false must make no policy and keep the metrics"
  }
}
