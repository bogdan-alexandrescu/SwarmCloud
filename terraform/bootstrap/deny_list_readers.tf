# The never-touch list, read as ONE object by the two identities that run
# acceptance (scripts/lib/common.sh config_fetch_one).
#
# Sourcing scripts/lib/common.sh with a project loads config/<env>/deny-list.json
# from the state bucket and refuses everything when it cannot. accept.yml's
# swarm-accept identity ("what dev runs") and the swarm-verify job's identity
# (every suite inside the VPC) both source it, and neither could read the
# object: acceptance_reads_release_records covers releases/ alone, and the
# verify identity held nothing on this bucket. The loader reads the one object
# by its exact name -- `storage objects describe` for its generation, then a
# copy of that generation -- and never lists config/<env>/, so objects.get on
# that one object is all either identity needs.
#
# WHY legacyObjectReader: it is storage.objects.get and nothing else, where
# objectViewer adds storage.objects.list. A listing under a resource.name
# condition is checked against the bucket, so the narrow role is the honest one.
#
# WHY A CONDITION ON THE EXACT NAME, BUILT FROM THE BUCKET RESOURCE: the bucket
# also holds every root's Terraform state, which carries sensitive values, and
# config/<env>/ holds the deployment's tfvars and its private values. `==`, not
# startsWith: a prefix would reach the folder. The bucket is versioned and an
# object's resource.name carries no generation, so the grant covers every
# noncurrent generation of this one object as well -- older copies of the same
# list, nothing more.
#
# DEV ONLY. Acceptance runs against dev; a stage or prod reader is a new row,
# weighed then.
#
# Owner-applied after merge, by a targeted bootstrap apply of these two
# addresses (expected plan: 2 to add, 0 to change, 0 to destroy). Rollback:
# remove the rows and the same targeted apply shows 2 to destroy.

resource "google_storage_bucket_iam_member" "acceptance_reads_deny_list" {
  count = local.wif_enabled

  bucket = google_storage_bucket.state.name
  role   = "roles/storage.legacyObjectReader"
  member = "serviceAccount:${google_service_account.acceptance[0].email}"

  condition {
    title       = "config/dev/deny-list.json only"
    description = "managed-by=swarm-terraform; the dev never-touch list alone, every noncurrent generation of that one object included (the bucket is versioned). Dev only; no other config object, no Terraform state."
    expression  = "resource.name == \"projects/_/buckets/${google_storage_bucket.state.name}/objects/config/dev/deny-list.json\""
  }
}

resource "google_storage_bucket_iam_member" "verify_reads_deny_list" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.legacyObjectReader"
  member = "serviceAccount:${google_service_account.verify.email}"

  condition {
    title       = "config/dev/deny-list.json only"
    description = "managed-by=swarm-terraform; the dev never-touch list alone, every noncurrent generation of that one object included (the bucket is versioned). Dev only; no other config object, no Terraform state."
    expression  = "resource.name == \"projects/_/buckets/${google_storage_bucket.state.name}/objects/config/dev/deny-list.json\""
  }
}
