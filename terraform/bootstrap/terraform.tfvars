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

# The deployer's roles that have traded their project-wide grant for a
# conditioned one (deployer_conditions.tf). EMPTY: the conditions are written
# and none is applied yet.
#
# ADD ONE PER RELEASE, apply bootstrap between releases, and let the next
# release's plan -- which refreshes everything terraform/infra manages -- prove
# it. A wrong condition fails that plan with a 403 on our own resource; the
# revert is deleting the line and applying again.
#
# Accepted names: roles/compute.networkAdmin, roles/compute.securityAdmin,
# roles/container.admin, roles/datastore.owner, roles/logging.configWriter,
# roles/resourcemanager.projectIamAdmin, swarmSecretProvisioner.
deployer_scoped_roles = []
