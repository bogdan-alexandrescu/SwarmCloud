# Container images for every runner profile.
#
# Regional, in the same region as the workloads: pulling a multi-gigabyte agent
# image across regions is paid for on every single cold start.

resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"

  description = "Swarm runner images (managed-by=swarm-terraform)"

  docker_config {
    immutable_tags = var.immutable_tags
  }

  # DISABLED, not INHERITED (owner decision 2026-10-21: Google's container
  # scanning off, project-wide). It charges per image pushed: 10,833 scans in
  # October to the 21st, one per digest this repository received (11,172
  # created in that window, ~1,200 a day that week). Nothing reads its
  # results. Every gate is trivy's: `trivy (repo)` on each pull request, the
  # release scanning every digest it promotes (.github/actions/release-images,
  # --scan-only), and the weekly `trivy (published images)` scan of what is
  # deployed. The project's containerscanning API is turned off by hand
  # (terraform/infra/main.tf says why); this keeps the repository off even if
  # someone turns the API back on.
  vulnerability_scanning_config {
    enablement_config = "DISABLED"
  }

  kms_key_name = var.kms_key_name == "" ? null : var.kms_key_name

  # True in dev until the owner has read the would-delete list (owner
  # 2026-10-21, #1886; docs/runbooks/registry-cleanup.md says how to read it).
  cleanup_policy_dry_run = var.cleanup_dry_run

  # SIX POLICIES OF ARTIFACT REGISTRY'S TEN (#1886). A version matching any
  # KEEP is kept whatever the DELETEs say and whatever the order here, so the
  # KEEPs are the whole list of what survives: a release, a rollback or a CI
  # reuse reads nothing else. Tags are matched by PREFIX, which is why the
  # promotion mark is `promoted-<channel>-<tag>` (promotion_mark_prefix,
  # scripts/lib/common.sh) and starts with no channel name;
  # tests/unit/scripts/test_registry_cleanup_prefixes.py holds the prefixes
  # below to it and to the channels.

  # Untagged layers accumulate on every build and nothing ever pulls them.
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = var.untagged_ttl
    }
  }

  # KEEP wins over DELETE, so a rollback target is always present: the newest
  # versions of every package, whatever they are tagged.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"

    most_recent_versions {
      keep_count = var.keep_recent_versions
    }
  }

  # `:dev` and `:prod` are the live images, and a channel tag that has not
  # moved for months is still what the environment runs: no age. Only the
  # channel tags start this way now that marks start `promoted-`, and commit
  # SHA tags are hex, which neither prefix is.
  cleanup_policies {
    id     = "keep-channel-tags"
    action = "KEEP"

    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["dev", "prod"]
    }
  }

  # Every promoted digest for 30 days, how long a release's image manifest
  # (the `images-<env>` CI artifact, docs/ci.md) is kept for a rollback to
  # deploy from. Age is the VERSION's, from when the digest was pushed, not
  # from when it was marked: a digest CI reused is up to 7 days older than its
  # promotion, still well inside the window.
  cleanup_policies {
    id     = "keep-promoted-30d"
    action = "KEEP"

    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["promoted-"]
      newer_than   = "2592000s"
    }
  }

  # The owner pins workspace-apply by digest in terraform/bootstrap/
  # terraform.tfvars (workspace_apply_image), and only an owner's bootstrap
  # apply moves the pin, so a promoted workspace-apply digest is kept with no
  # age: the pinned one may be months old.
  cleanup_policies {
    id     = "keep-owner-pinned"
    action = "KEEP"

    condition {
      tag_state             = "TAGGED"
      package_name_prefixes = ["workspace-apply"]
      tag_prefixes          = ["promoted-"]
    }
  }

  # Everything tagged that no KEEP holds -- in practice the commit-SHA tag of
  # a build nobody promoted -- after 14 days. That is above the 7-day CI
  # image-reuse window (BUILD_REUSE_MAX_AGE_DAYS, scripts/build-images.sh)
  # plus the time a release takes to promote what it reused, so no digest a
  # build may still re-tag and promote is deleted under it.
  cleanup_policies {
    id     = "delete-tagged-stale"
    action = "DELETE"

    condition {
      tag_state  = "TAGGED"
      older_than = "1209600s"
    }
  }

  labels = var.labels
}

# Keyed by a STABLE label rather than by the member string. Members are service
# account emails that are unknown until apply, and a for_each whose KEYS are
# unknown fails the very first plan -- terraform cannot name instances it cannot
# compute. Keys static, unknown values only in the value.
#
# `readers` is the CONTROL PLANE. It gets the predefined reader role, which
# includes the *.list surface, because the platform's own components are the
# things that legitimately look at what is in the repository. Tenant workers get
# `pullers` below instead -- see the variable for why enumeration is the line.
resource "google_artifact_registry_repository_iam_member" "readers" {
  for_each = var.readers

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}

# Pull without enumerate: swarmImagePuller, a custom role with every *.list
# permission removed.
#
# The role is DEFINED in terraform/bootstrap/platform_roles.tf since #79 (owner
# decision 2026-09-25), with the validations that keep enumeration, push and
# delete out of it: CI no longer holds roles/iam.roleAdmin, so no module CI
# applies can define a role. It used to be created here only when `pullers` was
# non-empty, so a swarm with no tenants planted no project-wide name; bootstrap
# cannot see this root's tenants, so it defines the role unconditionally, and a
# role nobody is bound to grants nothing.
#
# The name is a plain string from ../custom_role_ids, the spelling this module
# and terraform/bootstrap share -- known at plan, and read from no role.
module "custom_role_ids" {
  source = "../custom_role_ids"

  project_id = var.project_id
}

resource "google_artifact_registry_repository_iam_member" "pullers" {
  for_each = var.pullers

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name

  role   = module.custom_role_ids.names.image_puller
  member = each.value
}

resource "google_artifact_registry_repository_iam_member" "writers" {
  for_each = var.writers

  project    = var.project_id
  location   = google_artifact_registry_repository.this.location
  repository = google_artifact_registry_repository.this.name
  role       = "roles/artifactregistry.writer"
  member     = each.value
}
