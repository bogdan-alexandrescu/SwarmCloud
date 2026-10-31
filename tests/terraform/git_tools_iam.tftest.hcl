# A git_fetch bundle over the inline cap: swarm-api's one write grant for it
# (#1815, lane GIT-FETCH-BUNDLES; terraform/infra/git_tools.tf).
#
# What is held here, from the owner's option A of 2026-10-21:
#
#   * `api_fetch_bundle_writer` is ONE bucket-scoped roles/storage.objectCreator
#     member on the artifact bucket, for swarm-api's account: create, never
#     read, list, delete or overwrite, and never project-wide;
#   * its condition is titled `git-fetch-bundles-only`, carries
#     managed-by=swarm-terraform in its description, and is EXACTLY the
#     expression the owner approved;
#   * evaluated, that expression ADMITS tenants/<t>/attempts/<a>/git/<call>.fetch.bundle
#     and REFUSES a push bundle, a task's artifacts, verdicts, checkpoints,
#     the repository index, nested or empty segments, a name that does not end
#     .fetch.bundle, anything outside tenants/ and every other bucket.
#
# The evaluation is await_input.tftest.hcl's: each `&&` clause is read back out
# of the planned expression and applied to object names the way IAM applies
# it, with extract("pre{x}suf") emulated per Google's attribute reference (the
# text between the FIRST pre and the first suf after it, "" when either is
# missing; the placeholder may span "/"). This one adds endsWith. A clause of
# any other shape fails the run.

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

  # Keys swarm-api creates for a large fetch (gittools.FETCH_OBJECT), under a
  # group tenant and a personal one.
  fetch_admitted = [
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/u-ana/attempts/att_x/git/7f3e.fetch.bundle",
  ]

  # Keys it must never create: a near-miss of every clause, so dropping or
  # loosening any one of them turns the run red.
  fetch_refused = [
    # a push bundle (the worker's own, swarm-api only reads it), a task's
    # artifacts, a verdict, a checkpoint, the repository index, outside tenants/
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/git/req-1.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/tasks/t/attempts/att_0123/artifacts/review.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/verdicts/v.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/checkpoints/att_0123/git/c.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/repos/r/attempts/att_0123/git/c.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/attempts/att_0123/git/req-1.fetch.bundle",
    # another bucket, a longer bucket name, the bucket itself
    "projects/_/buckets/other-bucket/objects/tenants/eng/attempts/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging-x/objects/tenants/eng/attempts/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging",
    # not tenants/<one segment>/attempts/<one segment>/git/: the placeholder
    # spans "/", so only the anchored comparisons refuse these
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/a/b/attempts/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants//attempts/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts//git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/sub/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/live/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/tasks/t/attempts/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/artifacts/attempts/x/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attemptsx/att_0123/git/req-1.fetch.bundle",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/gitx/req-1.fetch.bundle",
    # not ending .fetch.bundle
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/git/req-1.fetch.bundle.tmp",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/git/req-1.fetch",
    "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/attempts/att_0123/git/req-1.txt",
  ]
}

run "swarm_api_may_create_a_fetch_bundle_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  override_resource {
    target          = module.iam.google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # MUTATION: objectAdmin (or any role with get or delete) here and swarm-api
  # could read or overwrite what a worker stored.
  assert {
    condition     = google_storage_bucket_iam_member.api_fetch_bundle_writer.role == "roles/storage.objectCreator"
    error_message = "the fetch bundle writer is roles/storage.objectCreator and nothing wider: no get, no list, no delete, so no overwrite"
  }

  # MUTATION: point it at another bucket (or the project) and it reaches the
  # other team's buckets in this shared project.
  assert {
    condition     = google_storage_bucket_iam_member.api_fetch_bundle_writer.bucket == "swarm-artifacts-saga-agents-staging"
    error_message = "the grant is a member of the artifact bucket's policy, never the project's: this project holds another team's buckets"
  }

  # MUTATION: grant it to a worker's or the scheduler's account and swarm-api's
  # upload is still refused while another identity gains the write.
  assert {
    condition     = google_storage_bucket_iam_member.api_fetch_bundle_writer.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "swarm-api uploads a large fetch bundle (gittools.GcsBundles.upload); no other account holds this grant"
  }

  # MUTATION: retitle the condition and the dev-iam row the owner approved no
  # longer matches the plan.
  assert {
    condition = (
      length(google_storage_bucket_iam_member.api_fetch_bundle_writer.condition) == 1
      && google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].title == "git-fetch-bundles-only"
      && strcontains(google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].description, "managed-by=swarm-terraform")
    )
    error_message = "the grant carries one condition, titled git-fetch-bundles-only, whose description says managed-by=swarm-terraform (an IAM member carries no labels)"
  }

  # The approved text, verbatim (#1815, proposal of 2026-10-21, option A).
  # MUTATION: change one character of the expression and this fails.
  assert {
    condition = google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].expression == join(" && ", [
      "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/\")",
      "resource.name.extract(\"/objects/tenants/{t}/\") != \"\"",
      "resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/attempts/\")",
      "resource.name.extract(\"/attempts/{a}/\") != \"\"",
      "resource.name.extract(\"/attempts/{a}/\") == resource.name.extract(\"/attempts/{a}/git/\")",
      "resource.name.endsWith(\".fetch.bundle\")",
    ])
    error_message = "the condition is exactly the git-fetch-bundles-only expression the owner approved for dev-iam"
  }

  # MUTATION: grant objectCreator project-wide and swarm-api creates objects
  # in every bucket of this shared project.
  assert {
    condition = alltrue([
      for account, roles in module.iam.granted_roles : !contains(roles, "roles/storage.objectCreator")
    ])
    error_message = "roles/storage.objectCreator must never be granted project-wide"
  }

  # MUTATION: add matches() or an || and this fails.
  assert {
    condition = alltrue([
      for clause in split(" && ", google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].expression) : (
        can(regex("^resource\\.name\\.(startsWith|endsWith)\\(\"[^\"]+\"\\)$", clause))
        || can(regex("^resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\) != \"\"$", clause))
        || can(regex("^resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\) == resource\\.name\\.extract\\(\"[^\"{}]*\\{[a-z]+\\}[^\"{}]+\"\\)$", clause))
      )
    ]) && !strcontains(google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].expression, "matches(") && !strcontains(google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].expression, "||")
    error_message = "git-fetch-bundles-only is startsWith, endsWith and extract() comparisons joined by &&; no matches() and no ||"
  }

  # EVALUATED: every name is run through the planned expression, clause by
  # clause. The map on the right says what each must come out as.
  # MUTATION: drop or loosen any one clause and a refused name comes out true.
  assert {
    condition = {
      for name in concat(var.fetch_admitted, var.fetch_refused) : name => alltrue([
        for clause in split(" && ", google_storage_bucket_iam_member.api_fetch_bundle_writer.condition[0].expression) : (
          can(regex("^resource\\.name\\.startsWith\\(\"[^\"]+\"\\)$", clause))
          ? startswith(name, regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\)$", clause)[0])
          : can(regex("^resource\\.name\\.endsWith\\(\"[^\"]+\"\\)$", clause))
          ? endswith(name, regex("^resource\\.name\\.endsWith\\(\"([^\"]+)\"\\)$", clause)[0])
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
    } == merge({ for name in var.fetch_refused : name => false }, { for name in var.fetch_admitted : name => true })
    error_message = "git-fetch-bundles-only, evaluated with extract() as documented, must admit tenants/<t>/attempts/<a>/git/<call>.fetch.bundle of the artifact bucket and refuse push bundles, tasks/, verdicts/, checkpoints/, repos/, nested or empty segments, other suffixes, anything outside tenants/ and every other bucket"
  }
}
