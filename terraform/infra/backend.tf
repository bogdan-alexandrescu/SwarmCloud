# Remote state.
#
# Bucket is supplied at init time so this root stays environment-agnostic:
#
#   terraform init \
#     -backend-config="bucket=swarm-tfstate-saga-agents-staging" \
#     -backend-config="prefix=infra/dev"
#
# The bucket itself is created by terraform/bootstrap, which is a SEPARATE root
# whose own state is at prefix "bootstrap" in this bucket (#827) -- created with
# gcloud by scripts/bootstrap.sh first. A root cannot create the bucket its own
# backend lives in --
# the backend is initialised before any resource is planned, so the first apply
# would fail looking for a bucket that does not exist yet.
terraform {
  backend "gcs" {}
}
