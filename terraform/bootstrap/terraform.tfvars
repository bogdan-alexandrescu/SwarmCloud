# Bootstrap inputs for saga-agents-staging.
#
# LOCAL STATE, on purpose (versions.tf): this root creates the bucket the other
# roots keep their state in, so it cannot keep its own state there.

project_id = "saga-agents-staging"
region     = "us-central1"
location   = "US-CENTRAL1"

# Keyless CI. Turned on 2026-09-24 so testing, building and deploying run in
# GitHub Actions rather than on a laptop.
enable_github_wif = true
github_repository = "bogdan-alexandrescu/SwarmCloud"

# MAIN ONLY, and this is the security boundary rather than a convenience.
# A branch anyone can push -- or a pull request from a fork -- must not be able
# to mint a token holding projectIamAdmin on a SHARED project. The cost is that
# the terraform PLAN job on a pull request cannot authenticate; that is the
# correct trade and terraform.yml handles it rather than widening this.
github_allowed_refs = ["refs/heads/main"]
