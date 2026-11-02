# Voice it's storage: swarm-api's two grants on the artifact bucket's
# `tenants/<tenant>/voice/<project>/` objects (docs/design/voice-it.md §3.7,
# §10 phase 2; lane VOICE-STORAGE, part of #2688): terraform/infra/voice.tf,
# `api_voice_blob_writer` and `api_voice_blob_deleter`.
#
# What is held here:
#
#   * the writer is ONE bucket-scoped roles/storage.objectCreator member on the
#     artifact bucket: create, never read, list, delete or overwrite, and never
#     project-wide;
#   * the deleter is the custom role that is storage.objects.delete and nothing
#     else (swarmHistoryPurgeDeleter, defined by terraform/bootstrap), on the
#     same bucket;
#   * both are swarm-api's, both carry managed-by=swarm-terraform in their
#     condition's description, and both are conditioned on
#     `voice_blob_condition`;
#   * that condition, EVALUATED, admits tenants/<t>/voice/<project>/<object>
#     and refuses every other prefix, voice/ reached from inside another
#     prefix, empty segments and every other bucket;
#   * with voice_it off neither exists.
#
# The condition is evaluated as await_input.tftest.hcl evaluates its own:
# extract("pre{x}suf") is the text between the FIRST pre and the first suf
# after it, "" when either is missing, and the placeholder may span "/"
# (Google's attribute reference). A clause of any other shape fails the run.
#
# The infra root's setup is voice.tftest.hcl's.

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

  # Keys as store.py builds them (swarm_api/voice/store.py, blob_key): a
  # plan, a transcript part, a mock-up and a diagram, under a group tenant and
  # a personal one.
  voice_admitted = [
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/voice/vp_0123/plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/voice/vp_0123/transcripts/vs_4567/000000.jsonl",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/u-bgd/voice/vp_89ab/mockups/vc_1/2.html",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/u-bgd/voice/vp_89ab/diagrams/vc_2/1.mmd",
  ]

  # Keys neither grant may reach: a near-miss of every clause, so dropping or
  # loosening any one of them turns the run red.
  voice_refused = [
    # anything outside tenants/, another bucket, a longer bucket name, the
    # bucket itself
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/voice/vp_0123/plans/3.md",
    "projects/_/buckets/other-bucket/objects/tenants/eng/voice/vp_0123/plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging-x/objects/tenants/eng/voice/vp_0123/plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging",
    # another feature's prefix in the same tenant: a task's artifacts, a
    # checkpoint, the repository index, a verdict
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/tasks/t/attempts/a/artifacts/out.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/checkpoints/ck_1/archive.tar.gz",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/repos/r/index.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/verdicts/v.json",
    # voice/ reached from inside another prefix: the placeholder spans "/", so
    # only the anchored comparison refuses these
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/tasks/t/voice/vp_0123/plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/repos/r/voice/vp_0123/plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/b/voice/vp_0123/plans/3.md",
    # an empty tenant, an empty project, an object straight under voice/, the
    # prefix itself
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants//voice/vp_0123/plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/voice//plans/3.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/voice/notes.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/voice/",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/voicex/vp_0123/plans/3.md",
  ]
}

run "off_by_default_swarm_api_holds_no_voice_grant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_storage_bucket_iam_member.api_voice_blob_writer) == 0
      && length(google_storage_bucket_iam_member.api_voice_blob_deleter) == 0
    )
    error_message = "with voice_it off swarm-api holds no grant on tenants/<t>/voice/"
  }
}

run "on_swarm_api_may_create_and_delete_under_the_voice_prefix_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    voice_it = "on"
  }


  assert {
    condition     = google_storage_bucket_iam_member.api_voice_blob_writer[0].role == "roles/storage.objectCreator"
    error_message = "the writer is roles/storage.objectCreator and nothing wider: no get, no list, no delete (§3.7)"
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_voice_blob_deleter[0].role == "projects/saga-agents-staging/roles/swarmHistoryPurgeDeleter"
    error_message = "the deleter is the custom role that is storage.objects.delete and nothing else, never objectAdmin"
  }

  assert {
    condition = alltrue([
      for g in [google_storage_bucket_iam_member.api_voice_blob_writer[0], google_storage_bucket_iam_member.api_voice_blob_deleter[0]] : (
        g.bucket == "swarm-artifacts-saga-agents-staging"
        && g.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
        && length(g.condition) == 1
        && g.condition[0].expression == local.voice_blob_condition
        && strcontains(g.condition[0].description, "managed-by=swarm-terraform")
      )
    ])
    error_message = "both grants are swarm-api's, members of the artifact bucket's policy (never the project's), conditioned on voice_blob_condition, with managed-by=swarm-terraform in the condition's description"
  }

  assert {
    condition = alltrue([
      for account, roles in module.iam.granted_roles : !contains(roles, "roles/storage.objectCreator")
    ])
    error_message = "roles/storage.objectCreator must never be granted project-wide"
  }

  # Every clause is one of the three shapes the evaluation below understands,
  # joined only by `&&`: no matches() (Cloud Storage refuses it), no `||`.
  assert {
    condition = alltrue([
      for clause in split(" && ", local.voice_blob_condition) : (
        can(regex("^resource\\.name\\.startsWith\\(\"[^\"]+\"\\)$", clause))
        || can(regex("^resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\) != \"\"$", clause))
        || can(regex("^resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\) == resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\)$", clause))
      )
    ]) && !strcontains(local.voice_blob_condition, "matches(") && !strcontains(local.voice_blob_condition, "||")
    error_message = "voice_blob_condition is startsWith and extract() comparisons joined by &&; no matches() and no ||"
  }

  # EVALUATED: every name is run through the planned expression, clause by
  # clause, for each grant.
  assert {
    condition = alltrue([
      for g in [google_storage_bucket_iam_member.api_voice_blob_writer[0], google_storage_bucket_iam_member.api_voice_blob_deleter[0]] : {
        for name in concat(var.voice_admitted, var.voice_refused) : name => alltrue([
          for clause in split(" && ", g.condition[0].expression) : (
            can(regex("^resource\\.name\\.startsWith\\(\"[^\"]+\"\\)$", clause))
            ? startswith(name, regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\)$", clause)[0])
            : [
              for v in [[
                # extract(tpl), emulated: m = [prefix, suffix].
                for t in regexall("extract\\(\"([^\"]+)\"\\)", clause) : [
                  for m in [regex("^(.*)\\{[a-z]+\\}(.*)$", t[0])] : [
                    for rest in [length(split(m[0], name)) < 2 ? "" : join(m[0], slice(split(m[0], name), 1, length(split(m[0], name))))] :
                    length(split(m[1], rest)) < 2 ? "" : split(m[1], rest)[0]
                  ][0]
                ][0]
              ]] :
              can(regex("^resource\\.name\\.extract\\(\"[^\"]+\"\\) != \"\"$", clause)) ? v[0] != "" : (
                can(regex("^resource\\.name\\.extract\\(\"[^\"]+\"\\) == resource\\.name\\.extract\\(\"[^\"]+\"\\)$", clause)) ? v[0] == v[1] : false
              )
            ][0]
          )
        ])
      } == merge({ for name in var.voice_refused : name => false }, { for name in var.voice_admitted : name => true })
    ])
    error_message = "voice_blob_condition, evaluated with extract() as documented, must admit tenants/<t>/voice/<project>/<object> of the artifact bucket and refuse every other prefix, voice/ inside another prefix, empty segments, objects straight under voice/ and every other bucket"
  }
}
