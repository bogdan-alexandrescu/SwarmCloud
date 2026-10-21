# swarm-images keeps what a release, a rollback or a CI reuse can read, and
# deletes every other tagged image after 14 days -- in DRY RUN in dev until the
# owner has read the would-delete list (#1886, REGISTRY-CLEANUP, owner
# approval 2026-10-21; docs/runbooks/registry-cleanup.md).
#
# Every build is tagged with its commit SHA and the only DELETE policy used to
# be UNTAGGED, so tagged images were never deleted: 2,096 GB on 2026-10-21,
# about 125 GB more a day. Artifact Registry applies KEEP over DELETE whatever
# the order, so the assertions below hold the KEEP set exactly -- a KEEP that
# goes missing turns the DELETE onto the live `:dev` image.
#
# That the KEEP prefixes are the promotion mark's and the channels' is held in
# tests/unit/scripts/test_registry_cleanup_prefixes.py, against
# promotion_mark_prefix (scripts/lib/common.sh).

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"
  labels     = { "managed-by" = "swarm-terraform" }
  tf_root    = "../../terraform"
}

run "the_registry_keeps_channels_promotions_and_the_pin_and_deletes_stale_tags" {
  command = plan

  module {
    source = "../../terraform/modules/artifact_registry"
  }

  # MUTATION: add a seventh policy (or drop one) and this fails; Artifact
  # Registry allows 10, and the six are the whole design.
  assert {
    condition = toset([for p in google_artifact_registry_repository.this.cleanup_policies : p.id]) == toset([
      "delete-untagged", "keep-recent", "keep-channel-tags", "keep-promoted-30d", "keep-owner-pinned", "delete-tagged-stale",
    ]) && length(google_artifact_registry_repository.this.cleanup_policies) == 6
    error_message = "swarm-images carries exactly the six cleanup policies of #1886"
  }

  # MUTATION: set keep-channel-tags' action to "DELETE" and this fails.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies :
      p.action == (startswith(p.id, "keep-") ? "KEEP" : "DELETE")
    ])
    error_message = "every keep-* policy is KEEP and every delete-* policy is DELETE"
  }

  # MUTATION: give keep-channel-tags `newer_than = "2592000s"` and this fails:
  # a channel tag that has not moved for 30 days is still the live image.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies : (
        one(p.condition).tag_state == "TAGGED" &&
        toset(one(p.condition).tag_prefixes) == toset(["dev", "prod"]) &&
        one(p.condition).newer_than == null && one(p.condition).older_than == null &&
        one(p.condition).package_name_prefixes == null
      ) if p.id == "keep-channel-tags"
    ]) && length([for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "keep-channel-tags"]) == 1
    error_message = "keep-channel-tags keeps every TAGGED version whose tag starts dev or prod, with no age and in every package"
  }

  # MUTATION: change keep-promoted-30d's newer_than to "1209600s" and this
  # fails: a release manifest is kept 30 days for rollback, so its digests are.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies : (
        one(p.condition).tag_state == "TAGGED" &&
        toset(one(p.condition).tag_prefixes) == toset(["promoted-"]) &&
        one(p.condition).newer_than == "2592000s" && one(p.condition).older_than == null &&
        one(p.condition).package_name_prefixes == null
      ) if p.id == "keep-promoted-30d"
    ]) && length([for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "keep-promoted-30d"]) == 1
    error_message = "keep-promoted-30d keeps TAGGED promoted-* versions newer than 2592000s (30 d), in every package"
  }

  # MUTATION: drop keep-owner-pinned's package_name_prefixes and this fails:
  # the policy would keep every promoted image of every package forever.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies : (
        one(p.condition).tag_state == "TAGGED" &&
        toset(one(p.condition).tag_prefixes) == toset(["promoted-"]) &&
        toset(one(p.condition).package_name_prefixes) == toset(["workspace-apply"]) &&
        one(p.condition).newer_than == null && one(p.condition).older_than == null
      ) if p.id == "keep-owner-pinned"
    ]) && length([for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "keep-owner-pinned"]) == 1
    error_message = "keep-owner-pinned keeps promoted workspace-apply versions with no age: terraform/bootstrap/terraform.tfvars pins one by digest"
  }

  # MUTATION: change delete-tagged-stale's older_than to "604800s" and this
  # fails: 7 days is the CI image-reuse window, and a reused digest would be
  # deleted under the release that is about to promote it.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies : (
        one(p.condition).tag_state == "TAGGED" &&
        one(p.condition).older_than == "1209600s" && one(p.condition).newer_than == null &&
        one(p.condition).tag_prefixes == null && one(p.condition).package_name_prefixes == null
      ) if p.id == "delete-tagged-stale"
    ]) && length([for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "delete-tagged-stale"]) == 1
    error_message = "delete-tagged-stale deletes every TAGGED version older than 1209600s (14 d), with no prefix"
  }

  # MUTATION: change the module's untagged_ttl default and this fails.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies : (
        one(p.condition).tag_state == "UNTAGGED" && one(p.condition).older_than == "604800s"
      ) if p.id == "delete-untagged"
    ]) && length([for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "delete-untagged"]) == 1
    error_message = "delete-untagged still deletes UNTAGGED versions older than 604800s (7 d)"
  }

  # MUTATION: change the module's keep_recent_versions default and this fails.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies :
      one(p.most_recent_versions).keep_count == 20
      if p.id == "keep-recent"
    ]) && length([for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "keep-recent"]) == 1
    error_message = "keep-recent still keeps the newest 20 versions of every package"
  }
}

run "the_root_passes_dry_run_ttl_and_keep_count_and_dev_is_a_dry_run" {
  command = plan

  module {
    source = "../../terraform/modules/artifact_registry"
  }

  # dev's values, as the root passes them. A terraform test cannot load a
  # tfvars file into a run, so the text assertions below hold dev.tfvars and
  # the root's module block to them.
  variables {
    cleanup_dry_run      = true
    untagged_ttl         = "259200s"
    keep_recent_versions = 5
  }

  # MUTATION: hard-code `cleanup_policy_dry_run = false` in the module and
  # this fails.
  assert {
    condition     = google_artifact_registry_repository.this.cleanup_policy_dry_run == true
    error_message = "cleanup_dry_run reaches the repository's cleanup_policy_dry_run"
  }

  # MUTATION: hard-code delete-untagged's older_than and this fails.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies :
      one(p.condition).older_than == "259200s" if p.id == "delete-untagged"
    ])
    error_message = "untagged_ttl reaches delete-untagged"
  }

  # MUTATION: hard-code keep-recent's keep_count and this fails.
  assert {
    condition = alltrue([
      for p in google_artifact_registry_repository.this.cleanup_policies :
      one(p.most_recent_versions).keep_count == 5 if p.id == "keep-recent"
    ])
    error_message = "keep_recent_versions reaches keep-recent"
  }

  # MUTATION: delete any one of the three lines from `module
  # "artifact_registry"` in terraform/infra/main.tf and this fails: the module
  # default would apply silently (cleanup_dry_run defaults to false there).
  assert {
    condition = alltrue([
      for line in [
        "cleanup_dry_run\\s*=\\s*var\\.cleanup_dry_run\\b",
        "untagged_ttl\\s*=\\s*var\\.untagged_ttl\\b",
        "keep_recent_versions\\s*=\\s*var\\.keep_recent_versions\\b",
      ] :
      length(regexall("(?m)^\\s*${line}", one(regexall("(?ms)^module \"artifact_registry\" \\{.*?^\\}", file("${var.tf_root}/infra/main.tf"))))) == 1
    ])
    error_message = "terraform/infra/main.tf passes cleanup_dry_run, untagged_ttl and keep_recent_versions to module \"artifact_registry\""
  }

  # MUTATION: set `cleanup_dry_run = false` in dev.tfvars, or remove the line,
  # and this fails. Turning it off is the owner's call, after the would-delete
  # list (docs/runbooks/registry-cleanup.md), and it is made by editing this.
  assert {
    condition     = length(regexall("(?m)^cleanup_dry_run\\s*=\\s*true\\b", file("${var.tf_root}/environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets cleanup_dry_run = true (owner 2026-10-21: dry run first)"
  }

  # MUTATION: change the root's cleanup_dry_run default to false and this
  # fails: an environment whose tfvars never mentions it would delete for real.
  assert {
    condition     = length(regexall("(?ms)^variable \"cleanup_dry_run\" \\{[^}]*?default\\s*=\\s*true\\b", file("${var.tf_root}/infra/variables.tf"))) == 1
    error_message = "terraform/infra/variables.tf defaults cleanup_dry_run to true, so no environment deletes until it says so"
  }
}
