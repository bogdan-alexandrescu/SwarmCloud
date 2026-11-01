# Voice it's swarm-voice, the infrastructure side (docs/design/voice-it.md
# §3.2, §3.4, §3.6, §7, section 10 phase one; lane VOICE-STATEFULSET, part of
# #2688).
#
# Six things, and only these:
#
#   * `swarm-voice`, the Google service account the pod acts as, and the
#     Workload Identity binding that lets exactly the KSA swarm-system/
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
#     values above as the `swarm_voice_manifest` output.
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
# OFF UNTIL `voice_it` SAYS SO, in THREE STEPS, because CI can create a
# service account but can bind on one only once terraform/bootstrap -- which
# the owner applies -- has granted it roles/iam.serviceAccountAdmin on that
# account, and bootstrap can grant on an account only after it exists
# (mcp_test_server.tf has the same three steps; docs/ci.md, "The deployer's
# service-account grants"):
#
#   1. voice_it = "account": swarm-voice exists, holding nothing.
#   2. the owner adds swarm-voice to the accounts bootstrap grants the
#      deployer admin on (terraform/modules/service_account_ids; this lane's
#      pull request asks for it) and applies bootstrap. BGD stores the OpenAI
#      credential in Config › Secrets (personal scope, provider `openai`), so
#      the secret the bindings name exists.
#   3. voice_it = "on" with the swarm-voice image's digest in image_refs: the
#      Workload Identity binding, the two secret bindings, the invoker, the
#      address, SWARM_VOICE_URL and the manifest; then
#      `terraform output -raw swarm_voice_manifest` is applied to
#      swarm-autopilot (never agents-staging).
#
# Steps 1 and 3 change IAM, so a release making either waits at the IAM gate
# for the owner.
#
# GKE ONLY. swarm-voice is a StatefulSet on the Autopilot cluster and its KSA
# lives in that cluster's workload pool (`voice_it_needs_gke` says so).

variable "voice_it" {
  description = <<-EOT
    Voice it's swarm-voice (voice.tf): "off" (default) creates nothing;
    "account" creates its service account, holding nothing; "on" binds it to
    the KSA swarm-system/swarm-voice, grants it read and add-version on the
    platform owner's own OpenAI secret and run.invoker on swarm-api, reserves
    its internal address, sets swarm-api's SWARM_VOICE_URL and renders
    `swarm_voice_manifest`. "on" needs the owner's bootstrap grant on the
    account, the secret stored, and the swarm-voice image's digest.
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
  # creates the KSA under exactly these names.
  voice_namespace  = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  voice_ksa        = "swarm-voice"
  voice_account_id = "swarm-voice"
  # Spelled, not read from the account, so the plan knows it.
  voice_email  = "${local.voice_account_id}@${var.project_id}.iam.gserviceaccount.com"
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

resource "google_service_account" "voice" {
  count = local.voice_account ? 1 : 0

  project      = var.project_id
  account_id   = local.voice_account_id
  display_name = "Swarm Voice it"
  description  = "swarm-voice (Voice it) runs as this account from the KSA swarm-system/swarm-voice. Read and add-version on the platform owner's OpenAI secret, run.invoker on swarm-api, nothing else. managed-by=swarm-terraform"
}

# Exactly the KSA swarm-system/swarm-voice may act as the account. The
# namespace is part of the member, so a tenant pod (swarm-tenant-<t>) cannot.
resource "google_service_account_iam_member" "voice_workload_identity" {
  count = local.voice_on ? 1 : 0

  service_account_id = google_service_account.voice[0].name
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
