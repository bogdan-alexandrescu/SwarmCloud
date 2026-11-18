# Voice it's speech machine, the infrastructure side, since the owner's zone
# fallback (2026-11-09: "deploys in a if capacity available, if not checks B
# and then C"; docs/design/voice-it-own-speech.md §5 and §6):
# terraform/infra/voice_speech.tf and swarm-api's SWARM_SPEECH_* in locals.tf.
#
# What is held here:
#
#   * Terraform makes the machine's regional instance TEMPLATE, never the
#     machine: a release can no longer fail on a zone with no free L4;
#   * the template is on-demand (invariant 6), with no external address, on
#     the reserved address, as swarm-speech, with the speech tag;
#   * swarm-api is handed the zones in order, the machine's name prefix and
#     the template;
#   * voice_speech.zones defaults to the region's a, b and c; the one-zone
#     `zone` still plans as a one-zone list for one release, with a warning
#     that points to `zones`; a malformed or repeated zone is refused, and a
#     zone outside the region is the check block's.
#   * voice_speech.template = false holds the template back: none is planned,
#     swarm-api's SWARM_SPEECH_TEMPLATE is "" and the output's template null.
#
# The infra root's setup is voice.tftest.hcl's, with the speech account's read.

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


run "a_release_makes_the_template_and_never_the_machine" {
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
      length(google_compute_region_instance_template.speech) == 1 &&
      google_compute_region_instance_template.speech[0].region == "us-central1" &&
      google_compute_region_instance_template.speech[0].name_prefix == "swarm-speech-" &&
      google_compute_region_instance_template.speech[0].machine_type == "g2-standard-4"
    )
    error_message = "voice_speech.tf makes one regional template, named from the machine's prefix"
  }

  # Invariant 6: Spot is off platform-wide.
  assert {
    condition = (
      google_compute_region_instance_template.speech[0].scheduling[0].provisioning_model == "STANDARD" &&
      google_compute_region_instance_template.speech[0].scheduling[0].preemptible == false &&
      google_compute_region_instance_template.speech[0].scheduling[0].automatic_restart == false
    )
    error_message = "the speech machine's template is not on-demand, or restarts itself after a host event"
  }

  assert {
    condition = (
      length(google_compute_region_instance_template.speech[0].network_interface) == 1 &&
      length(google_compute_region_instance_template.speech[0].network_interface[0].access_config) == 0 &&
      google_compute_region_instance_template.speech[0].network_interface[0].network_ip == "10.10.0.43" &&
      google_compute_region_instance_template.speech[0].service_account[0].email == "swarm-speech@example-project.iam.gserviceaccount.com" &&
      toset(google_compute_region_instance_template.speech[0].tags) == toset(["swarm-speech"]) &&
      google_compute_region_instance_template.speech[0].metadata["swarm-speech-caller"] == "swarm-api@example-project.iam.gserviceaccount.com" &&
      google_compute_region_instance_template.speech[0].metadata["swarm-speech-image"] == ""
    )
    error_message = "the template is not on the reserved address with no external address, as swarm-speech, with its tag and its caller"
  }

  assert {
    condition = (
      local.service_env["swarm-api"].SWARM_SPEECH_ZONES == "us-central1-a,us-central1-b,us-central1-c" &&
      local.service_env["swarm-api"].SWARM_SPEECH_INSTANCE == "swarm-speech-" &&
      local.service_env["swarm-api"].SWARM_SPEECH_TEMPLATE == "projects/example-project/regions/us-central1/instanceTemplates/swarm-speech-20261109000000000000000001" &&
      local.service_env["swarm-api"].SWARM_SPEECH_ADDR == "10.10.0.43"
    )
    error_message = "swarm-api is not handed the region's a, b and c in order, the machine's name prefix and the template"
  }

  assert {
    condition     = output.voice_speech.zones == tolist(["us-central1-a", "us-central1-b", "us-central1-c"]) && output.voice_speech.machine_prefix == "swarm-speech-"
    error_message = "the voice_speech output does not name the zones in order and the machine's prefix"
  }
}

# voice_speech.template = false: the release holds the template back, so
# swarm-api is handed no template and the output names none.
run "a_deployment_can_hold_the_template_back" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled  = true
      template = false
      image    = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  assert {
    condition     = length(google_compute_region_instance_template.speech) == 0
    error_message = "voice_speech.template = false still plans the speech machine's template"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SPEECH_TEMPLATE == ""
    error_message = "swarm-api is handed a template the release held back"
  }

  assert {
    condition     = output.voice_speech.template == null
    error_message = "the voice_speech output names a template the release held back"
  }
}

run "the_zones_are_tried_in_the_configured_order" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      zones   = ["us-central1-c", "us-central1-a"]
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SPEECH_ZONES == "us-central1-c,us-central1-a"
    error_message = "swarm-api is not handed voice_speech.zones in their order"
  }
}

# The one-zone form, for one release: still a plan, as a one-zone list, with
# the check's warning pointing to `zones`.
run "the_one_zone_form_plans_as_a_one_zone_list" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      zone    = "us-central1-b"
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SPEECH_ZONES == "us-central1-b"
    error_message = "voice_speech.zone is not read as a one-zone list"
  }

  expect_failures = [check.voice_speech_zone_is_replaced_by_zones]
}

run "a_malformed_zone_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      zones   = ["us-central1"]
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  expect_failures = [var.voice_speech]
}

run "a_zone_named_twice_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      zones   = ["us-central1-a", "us-central1-a"]
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  expect_failures = [var.voice_speech]
}

run "zone_and_zones_together_are_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      zone    = "us-central1-a"
      zones   = ["us-central1-b"]
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  expect_failures = [var.voice_speech]
}

run "a_zone_outside_the_region_is_the_checks" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_speech = {
      enabled = true
      zones   = ["us-central1-a", "europe-west4-b"]
      image   = "projects/deeplearning-platform-release/global/images/common-cu129-ubuntu-2204-nvidia-580-v20261029"
    }
  }

  expect_failures = [check.voice_speech_zone_in_region]
}
