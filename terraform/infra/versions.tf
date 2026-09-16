terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source = "hashicorp/google"
      # Pinned to a major line here; the exact build is pinned by
      # .terraform.lock.hcl, which is committed.
      version = "~> 6.0"
    }
  }
}
