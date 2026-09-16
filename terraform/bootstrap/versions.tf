# Bootstrap root.
#
# There is deliberately NO `backend` block in this configuration, and adding one
# here would be a mistake rather than an improvement. Terraform initialises its
# backend BEFORE it plans a single resource, so a root that configures a GCS
# backend pointing at the bucket it is itself about to create cannot run: the
# first `init` fails looking for a bucket that does not exist yet.
#
# So bootstrap runs on local state and creates the bucket. Everything else --
# terraform/infra -- uses that bucket. If you later want bootstrap's own state
# in GCS, that is a SECOND apply: copy backend.tf.example to backend.tf and run
# `terraform init -migrate-state`, after the bucket exists.
#
# Keep terraform.tfstate for this root in version control or somewhere durable.
# Losing it means terraform no longer knows it owns the state bucket, and the
# next apply will try to create a bucket that is already there.
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
