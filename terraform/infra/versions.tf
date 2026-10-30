terraform {
  # The version scripts/prerequisites.sh pins and the install engine enforces
  # (docs/design/day-one-install.md §14 Q8): patch releases of 1.16 only.
  required_version = "~> 1.16.2"

  required_providers {
    google = {
      source = "hashicorp/google"
      # Pinned to a major line here; the exact build is pinned by
      # .terraform.lock.hcl, which is committed.
      version = "~> 6.0"
    }
  }
}
