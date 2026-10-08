# Bootstrap root.
#
# Its state is remote, in the bucket it manages, under prefix "bootstrap"
# (backend.tf, #827). That is possible only because scripts/bootstrap.sh
# creates the bucket with gcloud BEFORE the first init: terraform initialises
# its backend before it plans a single resource, so a root cannot create the
# bucket its own backend lives in. state_bucket.tf then manages that bucket
# (on the live project it is already in this root's state), and
# `prevent_destroy` keeps a destroy of this root from deleting it.
#
# Until 2026-10 this root ran on local state, kept in a single laptop
# checkout, and every other checkout planned the live deployer as a create.
# The one-time move is scripts/bootstrap.sh --migrate-state; the script refuses
# to plan against an empty state while the deployer exists
# (docs/operations.md, "The bootstrap layer's state").
terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = local.labels
}
