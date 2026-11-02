# A workflow step waits for a tenant member's answer, and swarm-api writes that
# answer into the artifact bucket (docs/design/human-steps.md §4.3, §4.7; lane
# HW-IAM, part of #1436).
#
# What is held here, from the build plan's row:
#
#   * swarm-api's write grant, `api_input_writer`, is ONE bucket-scoped
#     roles/storage.objectCreator member on the artifact bucket: create, never
#     read, list, delete or overwrite, and never project-wide;
#   * its condition, `await_input_object_condition`, ADMITS an answer's form
#     and its files, `tenants/<t>/tasks/<task>/attempts/input-<nonce>/
#     artifacts/<artifact>` and `.../artifacts/<stem>.files/<file>`, and
#     REFUSES a worker attempt's artifacts, an input attempt's checkpoints/ or
#     live/, anything outside tenants/ and any other bucket;
#   * `api_reader`, swarm-api's read grant, is unchanged: objectViewer,
#     unconditioned, on the same bucket;
#   * `await_input_expiry`, the composite index the expiry sweep reads
#     (state == PARKED, park_reason == AWAITING_INPUT, expires_at <= now).
#
# The condition is EVALUATED here, not only compared. Cloud Storage IAM
# conditions have no matches() (Google refused one with a 400, release run
# 38114832799; terraform/modules/iam/bindings.tf, history_purge_condition), so
# the condition is a startsWith and extract() comparisons. Each `&&` clause is
# read back out of the planned expression and applied to object names the way
# IAM applies it. extract("pre{x}suf") is emulated per Google's attribute
# reference, as history_purge_iam.tftest.hcl does: the text between the FIRST
# occurrence of pre and the first suf after it, "" when either is missing; the
# placeholder may span "/". A clause of any other shape fails the run.
#
# The infra root's setup is live.tftest.hcl's: the step-spec key versions are
# mocked so the root plans at all, and swarm-api's account is given an email
# so the grant's member is known at plan.

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
      providers = ["anthropic", "openai"]
    }
  }

  # Every image pinned by digest, or verify.tf refuses the plan.
  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  # Keys as swarm-api writes an answer (§4.3), a member's form and one of its
  # files, under a group tenant and a personal one.
  input_admitted = [
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.files/draft.md",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/u-ana/tasks/task_2/attempts/input-ffffffffffff/artifacts/answers.json",
  ]

  # Keys it must never create: the row's four, then a near-miss of every
  # clause, so dropping or loosening any one of them turns the run red.
  input_refused = [
    # the row's: a worker attempt, an input attempt's checkpoints/ and live/,
    # a key outside tenants/
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/att_x/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/checkpoints/ck_1/archive.tar.gz",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/live/rec-000001.live.webm",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    # another bucket, a longer bucket name, the bucket itself
    "projects/_/buckets/other-bucket/objects/tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging-x/objects/tenants/a/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging",
    # the input path written INSIDE a worker attempt's artifacts, or inside an
    # input attempt's checkpoints/: the placeholder spans "/", so only the
    # anchored comparisons refuse these
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/att_x/artifacts/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/checkpoints/attempts/input-0123456789ab/artifacts/review.json",
    # not tenants/<one segment>/tasks/<one segment>/attempts/
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/b/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/verdicts/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/repos/r/tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/sub/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants//tasks/t/attempts/input-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks//attempts/input-0123456789ab/artifacts/review.json",
    # not attempts/input-<non-empty>/artifacts/
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/xinput-0123456789ab/artifacts/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/artifactsx/review.json",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/logs/stdout.log",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/attempts/input-0123456789ab/artifacts",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/tasks/t/result.json",
  ]
}

run "swarm_api_may_create_an_answer_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # Computed, so unknown at plan; given, so the member below can only match if
  # it is swarm-api's.
  override_resource {
    target          = module.iam.google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_input_writer.role == "roles/storage.objectCreator"
    error_message = "the answer writer is roles/storage.objectCreator and nothing wider: no get, no list, no delete, so no overwrite (§4.3)"
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_input_writer.bucket == "swarm-artifacts-saga-agents-staging"
    error_message = "the grant is a member of the artifact bucket's policy, never the project's: this project holds another team's buckets"
  }

  assert {
    condition     = google_storage_bucket_iam_member.api_input_writer.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "swarm-api writes the answer (Q10); no other account holds this grant"
  }

  # objectCreator reaches no project-level policy: a project grant would let
  # swarm-api create objects in every bucket of this shared project.
  assert {
    condition = alltrue([
      for account, roles in module.iam.granted_roles : !contains(roles, "roles/storage.objectCreator")
    ])
    error_message = "roles/storage.objectCreator must never be granted project-wide"
  }

  assert {
    condition = (
      length(google_storage_bucket_iam_member.api_input_writer.condition) == 1
      && google_storage_bucket_iam_member.api_input_writer.condition[0].expression == local.await_input_object_condition
      && strcontains(google_storage_bucket_iam_member.api_input_writer.condition[0].description, "managed-by=swarm-terraform")
    )
    error_message = "the grant is conditioned on await_input_object_condition, and its condition's description says managed-by=swarm-terraform (an IAM member carries no labels)"
  }

  # Every clause is one of the three shapes the evaluation below understands,
  # joined only by `&&`: no matches() (Cloud Storage refuses it), no `||` that
  # could widen it.
  assert {
    condition = alltrue([
      for clause in split(" && ", local.await_input_object_condition) : (
        can(regex("^resource\\.name\\.startsWith\\(\"[^\"]+\"\\)$", clause))
        || can(regex("^resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\) != \"\"$", clause))
        || can(regex("^resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\) == resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\)$", clause))
      )
    ]) && !strcontains(local.await_input_object_condition, "matches(") && !strcontains(local.await_input_object_condition, "||")
    error_message = "await_input_object_condition is startsWith and extract() comparisons joined by &&; no matches() and no ||"
  }

  # EVALUATED: every name is run through the planned expression, clause by
  # clause. The map on the right says what each must come out as.
  assert {
    condition = {
      for name in concat(var.input_admitted, var.input_refused) : name => alltrue([
        for clause in split(" && ", google_storage_bucket_iam_member.api_input_writer.condition[0].expression) : (
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
    } == merge({ for name in var.input_refused : name => false }, { for name in var.input_admitted : name => true })
    error_message = "await_input_object_condition, evaluated with extract() as documented, must admit tenants/<t>/tasks/<task>/attempts/input-<nonce>/artifacts/ (the form and its <stem>.files/) of the artifact bucket and refuse worker attempts, checkpoints/, live/, nested or missing segments, anything outside tenants/ and every other bucket"
  }
}

run "swarm_api_reads_the_bucket_as_before" {
  command = plan

  override_resource {
    target          = google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    artifact_bucket  = "swarm-artifacts-saga-agents-staging"
    labels           = { "managed-by" = "swarm-terraform" }
    gke_cluster_name = "swarm-autopilot"
    gke_location     = "us-central1"
  }

  # The writer is a separate member in terraform/infra/await_input.tf; the
  # reader keeps its role, its member and no condition, so objects.py's read
  # path is exactly what it was (§4.3: "api_reader stays as it is").
  assert {
    condition = (
      google_storage_bucket_iam_member.api_reader.role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.api_reader.bucket == "swarm-artifacts-saga-agents-staging"
      && google_storage_bucket_iam_member.api_reader.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && length(google_storage_bucket_iam_member.api_reader.condition) == 0
    )
    error_message = "api_reader is unchanged: swarm-api's objectViewer on the artifact bucket, unconditioned"
  }
}

run "the_expiry_sweep_has_its_index" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  # awaitinput.expire_due (swarm-api, §4.6):
  #   tasks where state == PARKED and park_reason == AWAITING_INPUT
  #         and metadata.await_input.expires_at <= now
  #         order by metadata.await_input.expires_at
  # Two equalities then the range field; without the index Firestore fails the
  # query with FAILED_PRECONDITION and no wait ever expires.
  assert {
    condition = (
      google_firestore_index.await_input_expiry.collection == "tasks"
      && google_firestore_index.await_input_expiry.query_scope == "COLLECTION"
      && google_firestore_index.await_input_expiry.database == google_firestore_database.this.name
      && length(google_firestore_index.await_input_expiry.fields) == 3
      && google_firestore_index.await_input_expiry.fields[0].field_path == "state"
      && google_firestore_index.await_input_expiry.fields[0].order == "ASCENDING"
      && google_firestore_index.await_input_expiry.fields[1].field_path == "park_reason"
      && google_firestore_index.await_input_expiry.fields[1].order == "ASCENDING"
      && google_firestore_index.await_input_expiry.fields[2].field_path == "metadata.await_input.expires_at"
      && google_firestore_index.await_input_expiry.fields[2].order == "ASCENDING"
    )
    error_message = "the expiry sweep needs tasks (state, park_reason, metadata.await_input.expires_at ASC) at collection scope"
  }
}
