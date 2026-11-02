# Voice it's swarm-voice, the infrastructure side (docs/design/voice-it.md
# §3.2, §3.4, §3.6, §7, section 10 phase one; lane VOICE-STATEFULSET, part of
# #2688).
#
# Seven things, and only these (the last, lane VOICE-STORAGE):
#
#   * a read of `swarm-voice`, the Google service account the pod acts as,
#     which terraform/bootstrap creates (platform_service_accounts.tf), and
#     the Workload Identity binding that lets exactly the KSA swarm-system/
#     swarm-voice act as it;
#   * read (secretAccessor) on the platform owner's own OpenAI secret,
#     `swarm-tenant-<personal tenant>-openai`, and NOTHING ELSE in Secret
#     Manager (§3.6);
#   * add-version (secretVersionAdder) on that same secret, and nothing else:
#     swarm_voice/credential.py stores each rotated refresh token as a new
#     version of the secret it read (`SecretManagerStore._add`, a POST to
#     `<secret>:addVersion`). It never creates, deletes or disables one;
#   * run.invoker on swarm-api, the one service it calls (§3.6: every read and
#     write goes through swarm-api, presented with swarm-voice's own ID token);
#   * an internal address for its internal load balancer, swarm-api's path to
#     it, and so swarm-api's SWARM_VOICE_URL (locals.tf, local.voice_url);
#   * its manifest, kubernetes/swarm-system/swarm-voice.yaml followed by
#     kubernetes/network-policies/swarm-system-voice.yaml, rendered with the
#     values above as the `swarm_voice_manifest` output;
#   * swarm-api's conditioned create and delete on the artifact bucket's
#     `tenants/<tenant>/voice/<project>/` objects (below, "Voice it's blobs").
#
# WHY A GOOGLE SERVICE ACCOUNT, when swarm-live holds its grants as a bare
# Workload Identity principal (live.tf says why it can). swarm-voice must call
# swarm-api with a Google ID TOKEN (the caller swarm-api checks is "swarm-voice's
# own service account", §3.6), and GKE's metadata server mints an ID token
# only for a KSA that impersonates a GSA; a principal:// identity can hold a
# grant but cannot present itself to a Cloud Run service.
#
# WHY BINDINGS ON THE SECRET, NOT CONDITIONED PROJECT GRANTS. Each of the two
# Secret Manager grants is a binding on the one secret, which reaches that
# secret's versions and no other secret: the narrowest scope IAM has, and so
# the condition is the binding's resource. A project-level grant conditioned
# on the name would put roles/secretmanager.secretAccessor on the CI
# deployer's grantable list (terraform/bootstrap deployer_conditions.tf), and
# that list limits WHICH roles CI grants, never to whom -- CI could grant
# itself read of every secret in this shared project (github_app.tf records
# the same reasoning). CI may set IAM on a `swarm-` secret
# (swarmSecretProvisioner), so this root can make the binding.
#
# WHOSE SECRET. Version one of Voice it is BGD's only: swarm-api admits
# VOICE_IT_PEOPLE, which is unset, so PLATFORM_OWNER (voice-it.md §1). The
# secret is that person's personal-scope OpenAI secret, named as
# swarm_voice.credential.personal_secret_id names it:
# `swarm-tenant-` + swarm_common.identity.tenant_id_for_user(person) + `-openai`.
# local.voice_owner_tenant is that function in HCL; tests/terraform/voice.tftest.hcl
# holds it to values the Python function gives. credential.py's check_scope
# refuses any other secret before it asks Secret Manager, so the IAM grant and
# the code agree on the one secret twice over.
#
# OFF UNTIL `voice_it` SAYS SO, in THREE STEPS. terraform/bootstrap -- which
# the owner applies, and which a fresh install runs before infra -- creates
# swarm-voice and grants the CI deployer roles/iam.serviceAccountAdmin on it
# (its id is spelled once, in terraform/modules/service_account_ids, which
# both roots read). This root never creates the account; it reads it and
# binds on it (docs/ci.md, "The deployer's service-account grants"):
#
#   1. voice_it = "account": this root reads the swarm-voice account
#      bootstrap made, and grants nothing. If the owner's bootstrap apply has
#      not made it yet, the plan fails at `data.google_service_account.voice`
#      by the account's name; apply terraform/bootstrap first.
#   2. BGD stores the OpenAI credential in Config › Secrets (personal scope,
#      provider `openai`), so the secret the bindings name exists.
#   3. voice_it = "on" with the swarm-voice image's digest in image_refs: the
#      Workload Identity binding, the two secret bindings, the invoker, the
#      address, SWARM_VOICE_URL and the manifest; then
#      `terraform output -raw swarm_voice_manifest` is applied to
#      swarm-autopilot (never agents-staging).
#
# Step 1 changes no IAM. Step 3 does, so a release making it waits at the IAM
# gate for the owner.
#
# GKE ONLY. swarm-voice is a StatefulSet on the Autopilot cluster and its KSA
# lives in that cluster's workload pool (`voice_it_needs_gke` says so).

variable "voice_it" {
  description = <<-EOT
    Voice it's swarm-voice (voice.tf): "off" (default) creates nothing;
    "account" reads its service account, which terraform/bootstrap creates,
    and grants nothing (the plan fails if bootstrap has not made it); "on"
    binds it to
    the KSA swarm-system/swarm-voice, grants it read and add-version on the
    platform owner's own OpenAI secret and run.invoker on swarm-api, reserves
    its internal address, sets swarm-api's SWARM_VOICE_URL and renders
    `swarm_voice_manifest`. "on" needs the owner's bootstrap apply (the
    account and the deployer's grant on it), the secret stored, and the
    swarm-voice image's digest.
  EOT
  type        = string
  default     = "off"

  validation {
    condition     = contains(["off", "account", "on"], var.voice_it)
    error_message = "voice_it is \"off\", \"account\" or \"on\"."
  }
}

locals {
  # swarm-voice's identity, spelled once. kubernetes/swarm-system/swarm-voice.yaml
  # creates the KSA under exactly these names; the Google account's id is
  # modules/service_account_ids' voice_id, the one bootstrap creates it with.
  voice_namespace = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  voice_ksa       = "swarm-voice"
  # Spelled, not read from the account, so the plan knows it.
  voice_email  = "${module.service_account_ids.voice_id}@${var.project_id}.iam.gserviceaccount.com"
  voice_member = "serviceAccount:${local.voice_email}"

  voice_account = var.voice_it != "off"
  voice_on      = var.voice_it == "on" && var.enable_gke_autopilot

  voice_image = lookup(var.image_refs, "swarm-voice", "")

  # The one person Voice it serves in version one (PLATFORM_OWNER), and their
  # personal tenant: swarm_common.identity.tenant_id_for_user, in HCL. The
  # email's local part, lower-cased, with every run of characters outside
  # [a-z0-9-] made one "-" and "-" trimmed from both ends. When that changed
  # anything, or is longer than the 9 characters a `u-` id has room for
  # (identity._MAX_TENANT_ID 11, less the prefix), it is cut to 2 characters
  # (trailing "-" dropped) and the first 6 hex digits of sha256(lower(email))
  # are appended.
  voice_owner       = lower(var.platform_owner)
  voice_owner_local = local.voice_owner == "" ? "" : split("@", local.voice_owner)[0]
  voice_owner_slug  = trim(replace(local.voice_owner_local, "/[^a-z0-9-]+/", "-"), "-")
  voice_owner_tenant = local.voice_owner_slug == "" ? "" : (
    local.voice_owner_slug != local.voice_owner_local || length(local.voice_owner_slug) > 9
    ? "u-${replace(substr(local.voice_owner_slug, 0, 2), "/-+$/", "")}-${substr(sha256(local.voice_owner), 0, 6)}"
    : "u-${local.voice_owner_slug}"
  )
  voice_openai_secret = local.voice_owner_tenant == "" ? "" : "swarm-tenant-${local.voice_owner_tenant}-openai"

  # The internal load balancer's address, and swarm-api's URL for swarm-voice.
  # Plain HTTP on 8080 inside the VPC; swarm-api presents a Google ID token for
  # this URL (it sets no SWARM_VOICE_AUDIENCE), and swarm-voice checks it.
  voice_url = local.voice_on ? "http://${google_compute_address.voice[0].address}:8080" : ""

  # swarm-api's Direct VPC egress range, admitted to swarm-voice on 8080 (as
  # live.tf's local.live_swarm_api_egress_cidr, and for the same reason).
  voice_swarm_api_egress_cidr = var.subnet_cidr

  # swarm-api's own host, the one Google service swarm-voice's egress names
  # besides Secret Manager and the certificate endpoint (main.tf
  # local.swarm_api_url, the deterministic URL of the service).
  voice_swarm_api_host = trimprefix(local.swarm_api_url, "https://")
}

# The account terraform/bootstrap made (platform_service_accounts.tf). A plan
# against a project whose bootstrap apply has not made it fails here, by the
# account's name, rather than binding on an account that does not exist:
# apply terraform/bootstrap first.
data "google_service_account" "voice" {
  count = local.voice_account ? 1 : 0

  project    = var.project_id
  account_id = module.service_account_ids.voice_id
}

# Exactly the KSA swarm-system/swarm-voice may act as the account. The
# namespace is part of the member, so a tenant pod (swarm-tenant-<t>) cannot.
resource "google_service_account_iam_member" "voice_workload_identity" {
  count = local.voice_on ? 1 : 0

  service_account_id = data.google_service_account.voice[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${local.voice_namespace}/${local.voice_ksa}]"
}

# Read the person's own OpenAI credential, by version reference (§3.6).
resource "google_secret_manager_secret_iam_member" "voice_reads_openai" {
  count = local.voice_on ? 1 : 0

  project   = var.project_id
  secret_id = local.voice_openai_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = local.voice_member
}

# Store each rotated refresh token as a new version of that same secret
# (credential.py). secretVersionAdder is versions.add and secrets.rotate on
# the secret it is bound on; no read, no delete, no create.
resource "google_secret_manager_secret_iam_member" "voice_stores_openai" {
  count = local.voice_on ? 1 : 0

  project   = var.project_id
  secret_id = local.voice_openai_secret
  role      = "roles/secretmanager.secretVersionAdder"
  member    = local.voice_member
}

# Call swarm-api, on the service, nothing else (doctor.tf's shape).
resource "google_cloud_run_v2_service_iam_member" "voice_invokes_api" {
  count = local.voice_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = "swarm-api"
  role     = "roles/run.invoker"
  member   = local.voice_member

  depends_on = [module.cloud_run]
}

# swarm-api's address for swarm-voice: an internal passthrough load balancer
# on a reserved address (swarm-api is Cloud Run and cannot resolve a cluster
# Service), as live.tf reserves swarm-live's. Reserved so a redeploy cannot
# move it from under SWARM_VOICE_URL.
resource "google_compute_address" "voice" {
  count = local.voice_on ? 1 : 0

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-voice"
  address_type = "INTERNAL"
  subnetwork   = module.network.subnetwork_id
  description  = "Internal load balancer address of swarm-voice (docs/design/voice-it.md section 3.2)."

  labels = merge(local.labels, {
    "component" = "voice"
  })
}

# --- Voice it's blobs: swarm-api's two storage grants -----------------------
#
# docs/design/voice-it.md §3.7 and §10 phase 2 (lane VOICE-STORAGE, part of
# #2688). swarm-api keeps a project's redacted transcript and the card bodies
# too large for a document under the tenant's prefix in the artifact bucket:
#
#   tenants/<t>/voice/<project>/transcripts/<session>/<part>.jsonl
#   tenants/<t>/voice/<project>/mockups/<card>/<version>.html
#   tenants/<t>/voice/<project>/diagrams/<card>/<version>.mmd | .json
#   tenants/<t>/voice/<project>/plans/<version>.md
#
# and removes all of it when the person deletes the project. Two grants, both
# bucket-scoped members of the artifact bucket's policy (never the project's:
# this shared project holds another team's buckets), both conditioned on the
# same shape, and nothing else:
#
#   * api_voice_blob_writer: roles/storage.objectCreator, create and nothing
#     else -- no get, no list, no delete. swarm-api's read and list stay
#     `api_reader` in modules/iam/bindings.tf, unchanged;
#   * api_voice_blob_deleter: swarmHistoryPurgeDeleter, the custom role that is
#     storage.objects.delete and nothing else (terraform/bootstrap
#     history_purge.tf defines it; this root names it through
#     modules/custom_role_ids and may not read it, #79).
#
# CREATE-ONLY IN THE CODE, NOT ONLY IN IAM. Create plus delete on one prefix
# would admit a replace, so the writer (swarm_api/voice/store.py, `create`)
# uploads with if_generation_match=0: an existing name is a refusal, and a new
# transcript part or card version is a new name.
#
# THE TENANT IS FIXED BY THE CODE, NOT BY THIS CONDITION. One swarm-api serves
# every tenant, so no condition can tie the tenant segment to the caller.
# store.py builds every key from the tenant the caller resolves to and refuses
# any key outside tenants/<that tenant>/voice/<project>/ before it calls GCS
# (`key_refusal`: no "..", no absolute path, no other tenant). The condition
# pins the SHAPE, so neither grant reaches a task's artifacts, a checkpoint, a
# verdict, the repository index or another feature's prefix.
#
# THE CONDITION, four clauses ANDed. Cloud Storage IAM conditions have no
# matches() (Google refused one with a 400, release run 38114832799;
# modules/iam bindings.tf `history_purge_condition` records it), and a
# startsWith per tenant would miss the personal tenants created at runtime, so
# the shape is pinned with startsWith and extract() comparisons:
#
#   1. an object in this bucket under tenants/ -- never another bucket, a
#      longer bucket name, the bucket itself or a key outside tenants/;
#   2. T, the text after /objects/tenants/ up to the next "/", is not empty;
#   3. T is also the text before the FIRST "/voice/" after it, so the name is
#      tenants/T/voice/... with T one segment (not tenants/a/b/voice/,
#      tenants/a/tasks/t/voice/ or tenants/a/repos/r/voice/);
#   4. P, the text after the first "/voice/" up to the next "/", is not empty,
#      so the name is tenants/T/voice/P/<object>: never an object directly
#      under voice/, and never tenants/T/voice//<object>.
#
# extract("pre{x}suf") returns the text between the FIRST pre and the first
# suf after it, "" when either is missing, and {x} may span "/" (Google's
# attribute reference). The first "/voice/" is the one after T because the
# bucket name and T hold no "/" and clause 3 puts one there. What it does not
# pin: P's `vp_` form and the kinds below it; store.py refuses any other shape
# before GCS is called, and neither widens the reach past one tenant's voice/.
#
# tests/terraform/voice_storage.tftest.hcl evaluates this expression against
# admitted and refused names, clause by clause.
#
# Behind the same switch as everything else here (voice_it = "on"). Both are
# IAM changes, so the release that carries them waits at the IAM gate for the
# owner.

# The delete role's full name, a plain string: terraform/bootstrap defines the
# role, and this root may not so much as read one (#79).
module "voice_role_ids" {
  source = "../modules/custom_role_ids"

  project_id = var.project_id
}

locals {
  voice_blob_condition = join(" && ", [
    "resource.name.startsWith(\"projects/_/buckets/${module.storage.artifact_bucket_name}/objects/tenants/\")",
    "resource.name.extract(\"/objects/tenants/{t}/\") != \"\"",
    "resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/voice/\")",
    "resource.name.extract(\"/voice/{p}/\") != \"\"",
  ])
}

# An IAM member takes no labels; destroy-guard.jq treats it as a policy edge,
# and each condition's description carries managed-by=swarm-terraform.
resource "google_storage_bucket_iam_member" "api_voice_blob_writer" {
  count = local.voice_on ? 1 : 0

  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectCreator"
  member = module.iam.service_account_members["swarm-api"]

  condition {
    title       = "voice-it-blobs-create"
    description = "managed-by=swarm-terraform; swarm-api creates Voice it transcripts and card bodies under tenants/<t>/voice/<project>/ only (docs/design/voice-it.md section 3.7)."
    expression  = local.voice_blob_condition
  }
}

resource "google_storage_bucket_iam_member" "api_voice_blob_deleter" {
  count = local.voice_on ? 1 : 0

  bucket = module.storage.artifact_bucket_name
  role   = module.voice_role_ids.names.history_purge_deleter
  member = module.iam.service_account_members["swarm-api"]

  condition {
    title       = "voice-it-blobs-delete"
    description = "managed-by=swarm-terraform; swarm-api deletes a removed Voice it project's objects under tenants/<t>/voice/<project>/ only (docs/design/voice-it.md section 3.7)."
    expression  = local.voice_blob_condition
  }
}

check "voice_it_needs_gke" {
  assert {
    condition     = var.voice_it != "on" || var.enable_gke_autopilot
    error_message = "voice_it is \"on\" but enable_gke_autopilot is not: swarm-voice is a StatefulSet on the Autopilot cluster (docs/design/voice-it.md section 3.4). Nothing was granted."
  }
}

check "voice_it_has_an_image" {
  assert {
    condition     = !local.voice_on || local.voice_image != ""
    error_message = "voice_it is \"on\" but image_refs has no swarm-voice digest, so there is no swarm-voice manifest to apply. Build the image and add its digest first."
  }
}

check "voice_it_has_an_owner" {
  assert {
    condition     = !local.voice_on || local.voice_openai_secret != ""
    error_message = "voice_it is \"on\" but platform_owner is empty: Voice it serves the platform owner alone in version one, and its secret grants name that person's OpenAI secret."
  }
}

output "voice" {
  description = "swarm-voice's wiring: its identity, the one secret it may read and add versions to, and swarm-api's internal address for it. Names only; never a value."
  value = {
    mode             = var.voice_it
    enabled          = local.voice_on
    namespace        = local.voice_namespace
    service_account  = local.voice_ksa
    google_account   = local.voice_account ? local.voice_email : null
    openai_secret    = local.voice_openai_secret
    internal_address = local.voice_on ? google_compute_address.voice[0].address : null
    url              = local.voice_url
    port             = 8080
    # What the manifest's ingress policy admits.
    swarm_api_egress_cidr = local.voice_swarm_api_egress_cidr
  }
}

# The two templates with their placeholders filled, as one manifest. Null
# while swarm-voice is off or has no image, so there is never a half-rendered
# manifest to apply.
output "swarm_voice_manifest" {
  description = "swarm-voice's Kubernetes objects and NetworkPolicies, rendered. Apply to swarm-autopilot only (never agents-staging). Null until voice_it is on with an image."
  value = local.voice_on && local.voice_image != "" ? replace(replace(replace(replace(replace(replace(replace(replace(
    join("---\n", [
      file("${path.module}/../../kubernetes/swarm-system/swarm-voice.yaml"),
      file("${path.module}/../../kubernetes/network-policies/swarm-system-voice.yaml"),
    ]),
    "__SWARM_VOICE_IMAGE__", local.voice_image),
    "__PROJECT_ID__", var.project_id),
    "__SWARM_VOICE_GSA__", local.voice_email),
    "__SWARM_VOICE_ILB_IP__", google_compute_address.voice[0].address),
    "__SWARM_VOICE_AUDIENCE__", local.voice_url),
    "__SWARM_VOICE_CALLERS__", module.iam.service_account_emails["swarm-api"]),
    "__SWARM_API_EGRESS_CIDR__", local.voice_swarm_api_egress_cidr),
  "__SWARM_API_HOST__", local.voice_swarm_api_host) : null
}
