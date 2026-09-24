# Every image this root deploys is pinned by DIGEST, and nothing else plans.
#
# THE DEFECT. terraform/infra took one `image_tag` and stamped it on every
# service and every job. Cloud Run resolves a tag to a digest once, when a
# revision is created, so a tag rebuilt to new content planned NO change: the
# service kept serving the old digest, reported healthy, and every check passed
# (docs/audits/2026-09-20/tag-vs-digest.md, measured on swarm-ui). The worker
# jobs and the scheduler's WORKER_IMAGE_TAG were the same tag, resolved at pull
# time instead -- so what an agent ran was whatever the tag pointed at then.
#
# `image_refs` is now the only image input: one `<registry>/<name>@sha256:<64
# hex>` per image, built from the promotion manifest by
# scripts/lib/image-refs.sh. These runs hold the root to it:
#
#   * every Cloud Run service, every worker job, the verification job and the
#     scheduler's worker-image map carry exactly the digest they were given;
#   * an image with NO digest fails the plan. It does not fall back to a tag,
#     because a fallback is how a tag gets deployed without anybody choosing it;
#   * a tag, a truncated digest, another image's digest under this name, and an
#     image from another registry are each refused before anything is planned.

mock_provider "google" {}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  tenants = {
    eng = { kind = "group", principal = "eng@saga.xyz", providers = ["anthropic", "openai"] }
  }

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}

run "every_service_and_job_runs_the_digest_it_was_given" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = length(output.service_images) == 5 && alltrue([for name, image in output.service_images : image == var.image_refs[name]])
    error_message = "every Cloud Run service must run exactly the digest image_refs names for it"
  }

  assert {
    condition     = length(output.job_images) > 0 && alltrue([for job, image in output.job_images : contains(values(var.image_refs), image)])
    error_message = "every worker job must run a digest from image_refs"
  }

  assert {
    condition     = output.job_images["swarm-job-eng-claude-code"] == var.image_refs["agent-runtime-base"]
    error_message = "a worker job runs the digest of its runner profile's image"
  }

  assert {
    condition     = google_cloud_run_v2_job.verify.template[0].template[0].containers[0].image == var.image_refs["swarm-verify"]
    error_message = "the verification job runs the swarm-verify digest"
  }

  # The GKE path has no terraform-managed Job to carry an image, so the
  # scheduler is handed the runner digests and names them in every Job it
  # creates. This is what reaches the browser profile.
  # Compared entry by entry rather than with `==` on the whole value: the
  # output is decoded JSON (an object) and the variable is a map, and `==`
  # across those two types is false even when every entry agrees.
  assert {
    condition = (
      length(output.worker_image_refs) == 2
      && output.worker_image_refs["agent-runtime-base"] == var.image_refs["agent-runtime-base"]
      && output.worker_image_refs["agent-runtime-browser"] == var.image_refs["agent-runtime-browser"]
    )
    error_message = "the scheduler must be given exactly the runner images' digests"
  }

  # The property, stated once over everything this root deploys: no image
  # reference anywhere carries a tag.
  assert {
    condition = alltrue([
      for image in concat(
        values(output.service_images),
        values(output.job_images),
        values(output.worker_image_refs),
        [google_cloud_run_v2_job.verify.template[0].template[0].containers[0].image],
      ) : can(regex("^[^@:]+@sha256:[0-9a-f]{64}$", image))
    ])
    error_message = "an image reference carries a tag or no digest; nothing this root deploys may name a mutable tag"
  }

  assert {
    condition = (
      length(output.image_refs) == length(var.image_refs)
      && alltrue([for name, ref in var.image_refs : output.image_refs[name] == ref])
    )
    error_message = "output.image_refs is what scripts/plan.sh replans against; it must be exactly what was applied"
  }
}

run "an_image_with_no_digest_fails_the_plan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  expect_failures = [google_cloud_run_v2_job.verify]
}

run "no_digests_at_all_fails_the_plan" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    image_refs = {}
  }

  expect_failures = [google_cloud_run_v2_job.verify]
}

run "a_tag_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }
  variables {
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api:7c5276212251"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  expect_failures = [var.image_refs]
}

run "a_tag_beside_a_digest_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # The runtime ignores the tag when a digest is present, so it is only ever a
  # label that can disagree with what runs.
  variables {
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api:dev@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  expect_failures = [var.image_refs]
}

run "a_truncated_digest_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }
  variables {
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  expect_failures = [var.image_refs]
}

run "another_images_digest_under_this_name_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }
  variables {
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  expect_failures = [var.image_refs]
}

run "an_image_from_another_registry_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # Pinned, but not built, scanned or promoted by this pipeline.
  variables {
    image_refs = {
      "swarm-api"             = "docker.io/library/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }

  expect_failures = [var.image_refs]
}
