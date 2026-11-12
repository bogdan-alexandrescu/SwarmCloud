# Voice it's swarm-voice, the infrastructure side (docs/design/voice-it.md
# §3.2, §3.4, §3.6, §7, section 10 phase one; lane VOICE-STATEFULSET, part of
# #2688).
#
# Six things, and only these (the last, lane VOICE-STORAGE):
#
#   * a read of `swarm-voice`, the Google service account the pod acts as,
#     which terraform/bootstrap creates (platform_service_accounts.tf), and
#     the Workload Identity binding that lets exactly the KSA swarm-system/
#     swarm-voice act as it;
#   * run.invoker on swarm-api (§3.6: every read and write goes through
#     swarm-api, presented with swarm-voice's own ID token);
#   * run.invoker on swarm-quota-broker, so the request for a Voice it pool
#     account reaches the broker (below, "THE VOICE IT POOL");
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
# THE VOICE IT POOL, NO SECRET (owner, 2026-11-02). The ChatGPT plan account
# is reserved for Voice it through a separate "Voice it pool" in the quota
# broker, and swarm-voice leases an account from it for each session
# (swarm_voice/accountlease.py: POST /v1/voice/lease, .../renew, .../release),
# authenticated with the ID token GKE's metadata server mints for the broker's
# audience. The broker returns the account's access token with each lease and
# renewal, so swarm-voice reads NO secret: this file grants nothing in Secret
# Manager. The person's own ChatGPT sign-in -- the secretAccessor and
# secretVersionAdder bindings on `swarm-tenant-<owner>-openai` this file used
# to make -- is gone, code and grant both.
#
# Two things admit swarm-voice to the broker, the way the broker's other
# callers are admitted: Cloud Run's run.invoker on the service (below,
# voice_invokes_broker), so the request arrives at all, and the broker's own
# check of the caller's ID token. The broker's voice routes accept
# `swarm-voice@<PROJECT_ID>.iam.gserviceaccount.com` -- local.voice_email, the
# account terraform/bootstrap creates -- unless its VOICE_SERVICE_ACCOUNTS
# names others, so the broker's environment (locals.tf) needs no change.
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
#   2. The owner puts a ChatGPT plan account in the quota broker's Voice it
#      pool (Settings › Accounts), so a session has an account to lease.
#   3. voice_it = "on" with the swarm-voice image's digest in image_refs: the
#      Workload Identity binding, the two invokers (swarm-api and the quota
#      broker), the address, SWARM_VOICE_URL and the manifest; then
#      `terraform output -raw swarm_voice_manifest` is applied to
#      swarm-autopilot (never peer-cluster).
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
    the KSA swarm-system/swarm-voice, grants it run.invoker on swarm-api and
    on swarm-quota-broker (whose Voice it pool leases each session's
    account; no Secret Manager grant), reserves its internal address, sets
    swarm-api's SWARM_VOICE_URL and renders `swarm_voice_manifest`. "on"
    needs the owner's bootstrap apply (the account and the deployer's grant
    on it) and the swarm-voice image's digest.
  EOT
  type        = string
  default     = "off"

  validation {
    condition     = contains(["off", "account", "on"], var.voice_it)
    error_message = "voice_it is \"off\", \"account\" or \"on\"."
  }
}

# THE PAUSE'S SWITCH. swarm-voice closes a GPT-Live session after about 15 s
# of silence while delegated work runs, and reopens it on the result
# (docs/design/voice-it.md §3.12.7), only with VOICE_PAUSE=on
# (swarm_voice/main.py, PAUSE_SWITCH). This renders it into the StatefulSet.
variable "voice_pause" {
  description = <<-EOT
    swarm-voice's automatic pause (VOICE_PAUSE, swarm_voice/main.py): "on"
    (default) closes the paid GPT-Live voice after about 15 s of silence
    while delegated work runs and reopens it on the result; "off" keeps the
    voice open, and billed, until the session ends.
  EOT
  type        = string
  # "on": the owner's answer of 11 November 2026 kept the pause off until
  # swarm-api keeps a paused session live and the session lives above the
  # page; both merged on 12 November 2026, so the pause is on.
  default = "on"

  validation {
    condition     = contains(["on", "off"], var.voice_pause)
    error_message = "voice_pause is \"on\" or \"off\"."
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

  # The quota broker, whose Voice it pool leases each session's account
  # (swarm_voice/accountlease.py): its deterministic URL and the audience its
  # ID-token check pins, the two values every other broker caller is handed
  # (locals.tf QUOTA_BROKER_URL / QUOTA_BROKER_AUDIENCE). The URL's host is
  # also the one the egress policy admits for it.
  voice_broker_url      = local.quota_broker_url
  voice_broker_audience = local.push_audiences["swarm-quota-broker"]
  voice_broker_host     = trimprefix(local.quota_broker_url, "https://")

  # The internal load balancer's address, and swarm-api's URL for swarm-voice.
  # Plain HTTP on 8080 inside the VPC; swarm-api presents a Google ID token for
  # this URL (it sets no SWARM_VOICE_AUDIENCE), and swarm-voice checks it.
  voice_url = local.voice_on ? "http://${google_compute_address.voice[0].address}:8080" : ""

  # swarm-api's Direct VPC egress range, admitted to swarm-voice on 8080 (as
  # live.tf's local.live_swarm_api_egress_cidr, and for the same reason).
  voice_swarm_api_egress_cidr = var.subnet_cidr

  # swarm-api's own host, one of the Google services swarm-voice's egress
  # names (main.tf local.swarm_api_url, the deterministic URL of the service).
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

# Reach the quota broker's Voice it pool routes: run.invoker on the broker
# service, nothing else, as the workers' invokers (main.tf, the broker's
# `invokers`) are admitted. Which routes swarm-voice may use is the broker's
# decision, by the caller's email in VOICE_SERVICE_ACCOUNTS: the voice lease,
# renew and release, and nothing a worker or swarm-api calls. The planner's
# Claude account (docs/design/voice-it.md §3.6, owner 2026-11-02) rides the
# same invoker and adds no binding: one `POST /v1/accounts/assign` per turn as
# a service holder for the session person's own workspace, renewed and released
# through the voice hold routes. The broker admitting that caller on the assign
# route is its own code (apps/quota-broker), held for the owner's approval.
resource "google_cloud_run_v2_service_iam_member" "voice_invokes_broker" {
  count = local.voice_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = "swarm-quota-broker"
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

output "voice" {
  description = "swarm-voice's wiring: its identity, the quota broker it leases Voice it pool accounts from, and swarm-api's internal address for it. Names only; never a value."
  value = {
    mode             = var.voice_it
    enabled          = local.voice_on
    namespace        = local.voice_namespace
    service_account  = local.voice_ksa
    google_account   = local.voice_account ? local.voice_email : null
    broker_url       = local.voice_on ? local.voice_broker_url : null
    internal_address = local.voice_on ? google_compute_address.voice[0].address : null
    url              = local.voice_url
    port             = 8080
    # What the manifest's ingress policy admits.
    swarm_api_egress_cidr = local.voice_swarm_api_egress_cidr
  }
}

# VOICE_PAUSE (`voice_pause`, above) is the container's first variable,
# written in after the voice container's one `env:` line rather than as a
# placeholder, so the template and its other renderers stay as they are. The
# pause tests in tests/terraform/voice.tftest.hcl fail if that line moves.
locals {
  voice_pause_anchor = "\n          env:\n"
  voice_pause_env    = "\n          env:\n            - name: VOICE_PAUSE\n              value: \"${var.voice_pause}\"\n"
}

# The two templates with their placeholders filled, as one manifest. Null
# while swarm-voice is off or has no image, so there is never a half-rendered
# manifest to apply.
#
# The last two placeholders are swarm-voice's model pins, by class from
# local.runner_models (voice-it-own-speech.md §14.1): VOICE_FRONT_MODEL, own
# speech's fast front, is the haiku class (first-reply latency); and
# VOICE_PLANNER_MODEL, the planner on both engines, is the planning class,
# fable. Nothing to ask at install: the pins are Terraform's.
output "swarm_voice_manifest" {
  description = "swarm-voice's Kubernetes objects and NetworkPolicies, rendered. Apply to swarm-autopilot only (never peer-cluster). Null until voice_it is on with an image."
  value = local.voice_on && local.voice_image != "" ? replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
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
    "__SWARM_API_HOST__", local.voice_swarm_api_host),
    "__QUOTA_BROKER_URL__", local.voice_broker_url),
    "__QUOTA_BROKER_AUDIENCE__", local.voice_broker_audience),
    "__QUOTA_BROKER_HOST__", local.voice_broker_host),
    "__VOICE_FRONT_MODEL__", local.runner_models["haiku"]),
  "__VOICE_PLANNER_MODEL__", local.runner_models["fable"]), local.voice_pause_anchor, local.voice_pause_env) : null
}
