# The live browser, the infrastructure side (docs/design/live-browser.md §3.3,
# §7, §8; lane LB-L8, part of #1030).
#
# Four things, and only these:
#
#   * `swarm-live-key`, the HMAC key swarm-api mints tunnel and viewer tickets
#     with and the relay verifies them with (§3.3 step 1). The SECRET and its
#     readers are here; its VALUE never is -- a managed secret version would
#     put the plaintext in a state file several people can read. The value is
#     generated and added by `scripts/create-secrets.sh --live-key`, which
#     nobody sees.
#   * the relay's ONE grant on the artifact bucket: roles/storage.objectCreator,
#     conditioned on a tenant's `.../attempts/<a>/live/` key ending in one of
#     the two recording suffixes (§7). No read, no delete, no list. The 7-day
#     rule that deletes those objects is the storage module's
#     (modules/storage/main.tf, `live_recording_suffixes`).
#   * an internal address for the relay's internal passthrough load balancer,
#     which is swarm-api's path to it (§11: "Direct VPC egress to an internal
#     load balancer in front of swarm-live").
#   * the relay's manifest, kubernetes/swarm-system/swarm-live.yaml, rendered
#     with the values above as the `swarm_live_manifest` output.
#
# THE WIRE (owner decisions 2026-10-11, live-browser.md §3.3; lane LB-L8e).
# The relay serves plain ws on TCP 8080 inside the VPC, with no TLS and no
# certificate anywhere: browsers open wss to swarm-api, and swarm-api proxies
# them to the relay through the internal load balancer below. It reads
# swarm-live-key from Secret Manager by the version reference rendered into
# its SWARM_LIVE_KEY_SECRET. Its manifest carries a NetworkPolicy that admits
# only swarm-api's egress range and labelled tenant gates; the range is
# var.subnet_cidr, because swarm-api's Direct VPC egress is
# module.network.subnetwork_id (main.tf, module "cloud_run") and that is the
# subnet var.subnet_cidr is the primary range of (modules/network).
#
# WHO THE RELAY IS. Its Kubernetes service account, `swarm-live` in
# `swarm-system`, holds the grants DIRECTLY, as a Workload Identity Federation
# for GKE principal
# (principal://iam.googleapis.com/projects/<number>/locations/global/
#  workloadIdentityPools/<project>.svc.id.goog/subject/ns/swarm-system/sa/swarm-live).
# Not through a Google service account, for three reasons:
#
#   * nothing to impersonate. A GSA is one more identity a token can be minted
#     for; a KSA principal exists only inside a pod in that namespace running
#     as that KSA. The namespace is part of the principal, so a tenant pod,
#     which runs in swarm-tenant-<t>, cannot present it;
#   * nothing for the deployer to administer. A GSA terraform/infra creates
#     must be listed in modules/service_account_ids so terraform/bootstrap can
#     grant CI serviceAccountAdmin on it (#334), an owner-applied change
#     before the first release could set its IAM;
#   * Cloud Storage and Secret Manager are both products that accept the
#     principal identifier in an allow policy, and those are the only two the
#     relay touches.
#
# WHAT THE RELAY DOES NOT HOLD, and why that differs from §3.3. The design
# allows the relay "no Firestore access except the session document and the
# audit collection". Firestore IAM cannot name a document or a collection:
# the narrowest resource a binding can name is the database
# (modules/tenancy/main.tf records the measurement), so "the session document"
# is not expressible and the closest faithful grant is NONE. The relay works
# from what the ticket carries -- session, tenant, task, attempt, generation,
# role and expiry, all under swarm-live-key's MAC -- and the audit is written
# by swarm-api in the same transaction as the change it records (§5.8). A
# stale generation's session is closed by swarm-api over the same internal
# path the viewer's websocket takes (§5.11).
#
# OFF UNTIL `enable_live_browser` IS SET. Off, the secret container exists (so
# `create-secrets.sh --live-key` can add a version first, as swarm-child-key's
# does) and swarm-api may read it; the relay holds nothing, no address is
# reserved and there is no manifest to apply. The order:
#
#   1. `make apply` with it false: swarm-live-key exists, nothing reads it yet;
#   2. `scripts/create-secrets.sh --live-key`: the first version exists;
#   3. the swarm-live image is built and its digest is in image_refs (the
#      release builds it: scripts/build-images.sh ALL_TARGETS);
#   4. `enable_live_browser = true`, apply: the relay's grants and address,
#      swarm-api's SWARM_LIVE_URL and SWARM_LIVE_KEY, and the scheduler's
#      LIVE_BROWSER_SIDECAR (locals.tf derives it from local.live_relay_enabled,
#      so a browser pod gets the display sidecar exactly when there is a
#      relay; LB-Lon);
#   5. `kubernetes/apply.sh --swarm-system --confirm`, which applies
#      `terraform output -raw swarm_live_manifest` to swarm-autopilot and then
#      swarm-system's policies, and refuses the shared cluster agents-staging.
#
# GKE ONLY (§8.1). The relay is a StatefulSet on the Autopilot cluster, and
# its principal lives in that cluster's workload pool, so the switch does
# nothing without enable_gke_autopilot (`live_browser_needs_gke` says so).

variable "enable_live_browser" {
  description = <<-EOT
    Give the live-browser relay (swarm-live, a one-replica StatefulSet in
    swarm-system) its grants: create-only on the artifact bucket under an
    attempt's live/ key, and read on swarm-live-key. Reserves its internal
    load-balancer address and renders its manifest as `swarm_live_manifest`.
    Set it only after `scripts/create-secrets.sh --live-key` has added a
    version and the swarm-live image has a digest in image_refs. See live.tf.
  EOT
  type        = bool
  default     = false
}

locals {
  # The relay's identity, spelled once. kubernetes/swarm-system/swarm-live.yaml
  # creates the namespace and the KSA under exactly these names, and
  # tests/terraform/live.tftest.hcl holds the rendered manifest to them.
  live_relay_namespace = "swarm-system" # namespace-prefix-exempt: the platform's own relay namespace, not a tenant's
  live_relay_ksa       = "swarm-live"
  live_relay_enabled   = var.enable_live_browser && var.enable_gke_autopilot

  live_relay_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${local.live_relay_namespace}/sa/${local.live_relay_ksa}"

  # Must equal modules/storage `live_recording_suffixes`: a suffix the grant
  # admits and the rule does not is a recording kept forever, and the other
  # way round one the relay cannot write. Not read across the module boundary
  # because that would mean another output on the storage module; the live
  # suite holds both to one list instead.
  live_recording_suffixes = [".live.webm", ".live.jsonl"]

  # The grant's condition. Three clauses, ANDed:
  #   1. an object in this bucket under tenants/ -- never the bucket itself, a
  #      prefix marker outside tenants/, or another bucket;
  #   2. under an attempt's live/ directory, `.../attempts/<a>/live/...`;
  #   3. ending in one of the two recording suffixes.
  # `extract` returns "" when the template does not match, which is how IAM
  # Conditions say "contains" (there is no contains() on resource.name).
  live_relay_object_condition = join(" && ", [
    "resource.name.startsWith(\"projects/_/buckets/${module.storage.artifact_bucket_name}/objects/tenants/\")",
    "resource.name.extract(\"/attempts/{attempt}/live/\") != \"\"",
    "(${join(" || ", [for s in local.live_recording_suffixes : "resource.name.endsWith(\"${s}\")"])})",
  ])

  live_relay_image = lookup(var.image_refs, "swarm-live", "")

  # What the relay asks Secret Manager for at runtime; a reference, never a value.
  live_key_version_name = "projects/${var.project_id}/secrets/${google_secret_manager_secret.live_key.secret_id}/versions/latest"

  # swarm-api's Direct VPC egress range, admitted to the relay on 8080 by the
  # manifest's NetworkPolicy. Every Cloud Run service here egresses from
  # module.network.subnetwork_id, whose primary range is var.subnet_cidr; the
  # network module has no output for the range, so it is read at its source.
  # kubernetes/apply.sh compares it with the range it reads from swarm-api's
  # own service and refuses to apply when they differ.
  live_swarm_api_egress_cidr = var.subnet_cidr
}

resource "google_secret_manager_secret" "live_key" {
  project   = var.project_id
  secret_id = "swarm-live-key"

  # Pinned to the workload region, as every platform key is: automatic
  # replication would copy it into regions nothing here runs in.
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "live-key"
  })

  annotations = {
    "swarm-populated-by" = "scripts/create-secrets.sh --live-key"
  }
}

# swarm-api and the relay, and NOBODY ELSE -- no tenant service account, no
# human. Authoritative, so a grant made out of band is removed on the next
# apply. An identity that could read this could mint a tunnel ticket for any
# attempt, or a viewer ticket for any tenant's session (§3.3, §5.11).
resource "google_secret_manager_secret_iam_binding" "live_key_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.live_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = concat(
    [module.iam.service_account_members["swarm-api"]],
    local.live_relay_enabled ? [local.live_relay_principal] : [],
  )
}

# The relay records (§7: "a recording written by a platform identity cannot be
# forged or padded by the agent"). roles/storage.objectCreator is create, plus
# the multipart steps of a create; it has no get, list or delete, so the relay
# cannot read a recording back, enumerate a tenant's objects, or overwrite a
# segment it already wrote (an overwrite needs delete).
#
# Bucket-scoped, never project-scoped: this project is SHARED with another
# team, and a project-level storage role would reach their buckets.
resource "google_storage_bucket_iam_member" "live_relay_records" {
  count = local.live_relay_enabled ? 1 : 0

  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectCreator"
  member = local.live_relay_principal

  condition {
    title       = "live-recordings-only"
    description = "Only tenants/.../attempts/<a>/live/ objects ending .live.webm or .live.jsonl (docs/design/live-browser.md section 7)."
    expression  = local.live_relay_object_condition
  }
}

# swarm-api's address for the relay. swarm-api is Cloud Run: it reaches the
# VPC by Direct VPC egress on the cluster's subnet and cannot resolve a
# cluster Service, so the relay is fronted by an internal passthrough load
# balancer on this reserved address (the Service in swarm-live.yaml names it).
# Reserved rather than ephemeral so a relay redeploy cannot move it from under
# swarm-api's setting.
#
# NOT MEASURED (§11 asks L8 to): this lane has no cluster to stand the relay
# up on. What the path relies on, for whoever measures it: the VPC firewall's
# allow_internal rule (modules/network/firewall.tf) admits the subnet to the
# nodes on every port, and deny_worker_ingress targets only the Cloud Run
# worker tag, not the nodes; the Service uses externalTrafficPolicy Cluster, so
# the load balancer health-checks the node, not the pod, and swarm-system's
# ingress policy needs no health-check range (swarm-system-live.yaml).
resource "google_compute_address" "live_relay" {
  count = local.live_relay_enabled ? 1 : 0

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-live-relay"
  address_type = "INTERNAL"
  subnetwork   = module.network.subnetwork_id
  description  = "Internal load balancer address of the swarm-live relay (docs/design/live-browser.md section 3.3)."

  labels = merge(local.labels, {
    "component" = "live-relay"
  })
}

check "live_browser_needs_gke" {
  assert {
    condition     = !var.enable_live_browser || var.enable_gke_autopilot
    error_message = "enable_live_browser is set but enable_gke_autopilot is not: the relay is a StatefulSet on the Autopilot cluster and its principal is that cluster's (docs/design/live-browser.md §8.1). Nothing was granted."
  }
}

check "live_relay_has_an_image" {
  assert {
    condition     = !local.live_relay_enabled || local.live_relay_image != ""
    error_message = "enable_live_browser is set but image_refs has no swarm-live digest, so there is no relay manifest to apply. Build the image and add its digest first."
  }
}

output "live_relay" {
  description = "The live relay's wiring: its identity, the key's name, the bucket and suffixes it may write, and swarm-api's internal address for it. Names only; never a value."
  value = {
    enabled            = local.live_relay_enabled
    namespace          = local.live_relay_namespace
    service_account    = local.live_relay_ksa
    principal          = local.live_relay_principal
    key_secret         = google_secret_manager_secret.live_key.secret_id
    artifact_bucket    = module.storage.artifact_bucket_name
    recording_suffixes = local.live_recording_suffixes
    internal_address   = local.live_relay_enabled ? google_compute_address.live_relay[0].address : null
    # Plain ws (owner decision 2026-10-11 (b)); swarm_live.main.PORT.
    port = 8080
    # What the manifest's NetworkPolicy admits; apply.sh holds it to the range
    # swarm-api's service actually egresses from.
    swarm_api_egress_cidr = local.live_swarm_api_egress_cidr
  }
}

# kubernetes/swarm-system/swarm-live.yaml with its placeholders filled. Null
# while the relay is off or has no image, so there is never a half-rendered
# manifest to apply. Rendered here, not by kubernetes/render.py, because every
# value it needs is this root's: the bucket, the key's name, the reserved
# address, the image digest and swarm-api's subnet range.
output "swarm_live_manifest" {
  description = "The relay's Kubernetes objects, rendered. Apply to swarm-autopilot only (never agents-staging). Null while enable_live_browser is off."
  value = local.live_relay_enabled && local.live_relay_image != "" ? replace(replace(replace(replace(replace(replace(
    file("${path.module}/../../kubernetes/swarm-system/swarm-live.yaml"),
    "__SWARM_LIVE_IMAGE__", local.live_relay_image),
    "__PROJECT_ID__", var.project_id),
    "__ARTIFACT_BUCKET__", module.storage.artifact_bucket_name),
    "__LIVE_KEY_VERSION__", local.live_key_version_name),
    "__SWARM_LIVE_ILB_IP__", google_compute_address.live_relay[0].address),
  "__SWARM_API_EGRESS_CIDR__", local.live_swarm_api_egress_cidr) : null
}
