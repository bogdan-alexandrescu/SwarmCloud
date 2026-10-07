# Remote state, in the swarm's own state bucket under its own prefix (#827).
#
# Until 2026-10 this root had only this file as `backend.tf.example`, so its one
# state was a local terraform.tfstate in a single laptop checkout. Any other
# checkout -- a git worktree of main -- planned against an EMPTY state: on
# 2026-10-07 that was "9 to import, 91 to add", the live deployer service
# account, every WIF binding and every deployer role offered for re-creation.
#
# The bucket is supplied at init time, the same way terraform/infra receives it
# (scripts/bootstrap.sh passes -backend-config="bucket=${TF_STATE_BUCKET}"), so
# the root names no project. The prefix is fixed here because there is exactly
# one bootstrap state per project; TF_BOOTSTRAP_STATE_PREFIX in
# scripts/lib/common.sh is the same value for the scripts that read the object
# directly, and tests/unit/scripts/test_bootstrap_remote_state.py holds the two
# equal.
#
# The bucket this state lives in is also managed BY this root
# (state_bucket.tf). That is safe only because scripts/bootstrap.sh creates the
# bucket with gcloud before the first init, and `prevent_destroy` keeps a
# destroy of this root from deleting it. A root still cannot CREATE the bucket
# its own backend lives in.
#
# Moving the old local state here is a one-time, typed step:
# scripts/bootstrap.sh --migrate-state (docs/operations.md says when and why).
terraform {
  backend "gcs" {
    prefix = "bootstrap"
  }
}
