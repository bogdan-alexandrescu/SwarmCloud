# The browser acceptance fixture page, served by us (#1628).
#
# WHY IT IS HERE. scripts/prove-gke-dispatch.sh and the smoke suite's browser
# check (t_check_browser_fixture in scripts/lib/testlib.sh) have a browser task
# load a page whose colour and text the check chose, and both gate a release.
# Until 2026-10-19 that page came from httpbin.org's /base64/ route (#358), and
# acceptance run 39249979130 failed its GKE proof on an httpbin.org 502. Owner
# decision on #1628: serve the fixture ourselves, so the check fails only when
# SwarmCloud does.
#
# WHAT IS PUBLIC, AND WHY THAT IS SAFE. One bucket holding one object,
# scripts/acceptance/fixture.html, readable by anyone. The runner opens it from
# the Cloud Run worker's egress and from the GKE Autopilot browser pod's, with
# no credential, so it cannot sit behind IAP or on swarm.saga.xyz's cookie
# origin; storage.googleapis.com is neither. The page's markup is FIXED: its
# only input is `?run=<id>`, accepted when it matches ^[a-z0-9-]{1,80}$ and set
# as textContent, so the most anyone can make it show is
# "swarm-fixture-<lowercase letters, digits and hyphens>" on a magenta block.
# Nothing here renders HTML taken from a URL; a route that did would make us a
# phishing and XSS host.
#
# A BUCKET OF ITS OWN, because the bucket uses uniform bucket-level access
# (CKV_GCP_29), so a read grant is on the bucket, not the object -- and an IAM
# condition cannot narrow a grant to allUsers. The bucket holds nothing but
# this object, so "public read on the bucket" and "public read on fixture.html"
# are the same set. legacyObjectReader reads an object by name and cannot list
# the bucket (owner decision on #1628, 2026-10-19): objectViewer would also
# admit storage.objects.list.
#
# THE OWNER APPLIES THE GRANT at the dev-iam hold. Until it is applied the
# object answers 403, browser_fixture_preflight reads that as "unavailable",
# and the check is a SKIP naming storage.googleapis.com -- never a pass.
# .github/workflows/security.yml's "no public Cloud Run services" step refuses
# every allUsers assignment outside github_hooks.tf; the exception for the one
# line below is a workflow change an operator pushes by hand (the PR that
# added this file quotes it).

locals {
  acceptance_fixture_bucket = "${var.name_prefix}-acceptance-fixture-${var.project_id}"
  # The one public member in this file, named once.
  acceptance_fixture_public_reader = "allUsers"
}

resource "google_storage_bucket" "acceptance_fixture" {
  count = var.enable_acceptance_fixture ? 1 : 0

  # checkov:skip=CKV_GCP_114:the acceptance fixture is public by design (#1628): one fixed page, no input rendered as markup; public access prevention would refuse the read grant below.
  project  = var.project_id
  name     = local.acceptance_fixture_bucket
  location = upper(var.region)

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  # `inherited`, not `enforced`: enforced refuses an allUsers grant outright.
  # An organisation policy enforcing public access prevention would refuse it
  # too; nothing in this repository measured one (the agent that wrote this
  # could not read the project's org policy), so the first apply is the
  # measurement.
  public_access_prevention = "inherited"

  versioning {
    enabled = true
  }

  logging {
    log_bucket        = module.storage.access_log_bucket_name
    log_object_prefix = "${var.name_prefix}-acceptance-fixture"
  }

  force_destroy = var.bucket_force_destroy

  labels = merge(local.labels, { "swarm-purpose" = "acceptance-fixture" })

  depends_on = [module.project_services]
}

resource "google_storage_bucket_object" "acceptance_fixture" {
  count = var.enable_acceptance_fixture ? 1 : 0

  bucket = google_storage_bucket.acceptance_fixture[0].name
  name   = "fixture.html"
  # `content`, not `source`: an edited page is then a diff in the plan, where
  # a `source` path whose file changed is not.
  content = file("${path.module}/../../scripts/acceptance/fixture.html")

  # text/html, or the runner's browser renders the source as text and the
  # page's colour never draws. no-store, so a page changed here is the page the
  # next run loads, not an hour-old copy from an edge cache.
  content_type  = "text/html; charset=utf-8"
  cache_control = "no-store"

  # An object carries metadata, not labels; the same key says whose it is.
  metadata = {
    "managed-by" = "swarm-terraform"
  }
}

# Trivy GCP-0001 ("bucket allows public access") is this grant, and it is the
# point: the owner approved allUsers read-by-name on this one-object bucket on
# #1628 (2026-10-19).
#trivy:ignore:AVD-GCP-0001
resource "google_storage_bucket_iam_member" "acceptance_fixture_public_read" {
  # checkov:skip=CKV_GCP_28:the acceptance fixture is public by design (#1628); the bucket holds only fixture.html, whose markup is fixed.
  count = var.enable_acceptance_fixture ? 1 : 0

  bucket = google_storage_bucket.acceptance_fixture[0].name
  role   = "roles/storage.legacyObjectReader"
  member = local.acceptance_fixture_public_reader
}

output "acceptance_fixture_url" {
  description = "The browser acceptance fixture page, without its ?run= id. scripts/lib/testlib.sh builds the same URL from ACCEPTANCE_FIXTURE_BUCKET in scripts/lib/common.sh; SWARM_BROWSER_FIXTURE_BASE overrides it."
  value       = "https://storage.googleapis.com/${google_storage_bucket.acceptance_fixture.name}/${google_storage_bucket_object.acceptance_fixture.name}"
}

# AN addon:ci PIECE, OFF BY DEFAULT (DAYONE-TF-2, review finding 4). An install
# makes no public bucket: enable_acceptance_fixture gates the bucket, its object
# and its allUsers reader, and dev.tfvars and prod.tfvars set it true. The
# moved blocks carry the live objects to index [0], so the live plan shows no
# change at all; a state that never held them plans nothing for them.
moved {
  from = google_storage_bucket.acceptance_fixture
  to   = google_storage_bucket.acceptance_fixture[0]
}

moved {
  from = google_storage_bucket_object.acceptance_fixture
  to   = google_storage_bucket_object.acceptance_fixture[0]
}

moved {
  from = google_storage_bucket_iam_member.acceptance_fixture_public_read
  to   = google_storage_bucket_iam_member.acceptance_fixture_public_read[0]
}
