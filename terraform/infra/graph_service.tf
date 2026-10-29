# The shared graph service, the platform side (docs/design/graph-service.md
# §2.1, §3.2 and §3.4; lane GS-INFRA, part of #1384). The pattern is
# typesafe_fwd.tf's.
#
# Four things, and only these:
#
#   * `swarm-graph-ticket-key`, the HMAC key swarm-api mints attempt graph
#     tickets with (apps/swarm-api/swarm_api/graph_ticket.py) and swarm-graph
#     verifies them with (apps/swarm-graph/swarm_graph/ticket.py). Its own key,
#     not swarm-typesafe-fwd-key or swarm-egress-key: a graph-service
#     compromise must not mint an egress ticket, nor the reverse. Its container
#     and its readers are here; its VALUE never is (scripts/create-secrets.sh
#     --graph-ticket-key owns values);
#   * ONE conditioned object read on the artifact bucket for the service's
#     identity: tenants/<t>/repos/<r>/graph/** and nothing else (§3.4);
#   * swarm-api's side: SWARM_GRAPH_TICKET_KEY by reference, once served;
#   * the service's manifest, kubernetes/swarm-system/swarm-graph.yaml,
#     rendered as `swarm_graph_manifest` with the image, the key's version
#     reference, the bucket and the setting (the kill switch);
#   * the stats port's way in (docs/design/graph-answer-cache.md §5.3; lane
#     CACHE-2-NETWORK), once served: the reserved INTERNAL address
#     `graph_stats` the manifest's `swarm-graph-stats` load balancer sits on,
#     swarm-api's egress range in the manifest's ingress policy (8081 only),
#     and SWARM_GRAPH_STATS_URL=http://<that address>:8081 on swarm-api.
#     No new IAM: the stats ticket is minted under swarm-graph-ticket-key,
#     which swarm-api already reads (graph_ticket_key_accessor).
#
# WHO READS WHAT.
#
#   swarm-graph-ticket-key     swarm-api (mints) and the service's KSA
#                              principal (verifies); nobody else. An identity
#                              that could read it could mint a ticket naming
#                              any tenant's graph.
#   tenants/*/repos/*/graph/** the service's KSA principal, by exact object
#                              name (no list), under the condition below.
#                              Nothing on Firestore, nothing on index/**
#                              (owner Q7: the repository document is a layer
#                              of the graph manifest), no Pub/Sub (owner Q5:
#                              warm on first sight, no new IAM).
#
# THE IDENTITY is the Kubernetes service account `swarm-graph` in
# `swarm-system`, as a Workload Identity Federation for GKE principal, exactly
# as swarm-typesafe-fwd's is: no Google service account, so nothing for the CI
# deployer to create or administer, nothing to impersonate, and a tenant pod
# in swarm-tenant-<t> cannot present it.
#
# THREE STEPS, ALL OFF BY DEFAULT, so merging this creates and changes nothing:
#
#   1. enable_graph_service = true, apply: the key's container, its two
#      readers and the bucket read exist. Nothing references the key yet.
#      These are the IAM rows the release holds at its dev-iam gate for the
#      owner.
#   2. add the key's first version (`scripts/create-secrets.sh
#      --graph-ticket-key`, generated, never shown), make sure image_refs has
#      a swarm-graph digest, then graph_service.serve = true, apply:
#      SWARM_GRAPH_TICKET_KEY on swarm-api and the rendered manifest, which is
#      applied to swarm-autopilot with
#        terraform -chdir=terraform/infra output -raw swarm_graph_manifest
#      The service then runs dark: its kill switch is still off (§8.2 step 2).
#   3. graph_service.enabled = true: the service answers. Which workspaces use
#      it is the stored setting code_intelligence.graph_delivery (§8.1), not
#      this file.
#
# DEVIATION FROM §3.4, SAID ONCE. The design gates swarm-api's
# SWARM_GRAPH_TICKET_KEY on the key's creation. A Cloud Run secret reference to
# a secret with no version stops swarm-api starting (locals.tf, live_key_secret_env),
# and step 1 creates the secret with none, so the reference waits for `serve`
# (step 2), as typesafe_fwd.tf's does.
#
# GKE ONLY: the service runs on the Autopilot cluster and its principal lives
# in that cluster's workload pool.

variable "enable_graph_service" {
  description = <<-EOT
    Create swarm-graph-ticket-key (no version), let swarm-api and the graph
    service's KSA read it, and grant that KSA object read on
    tenants/<t>/repos/<r>/graph/** of the artifact bucket
    (docs/design/graph-service.md §3.4). Off by default: nothing is created
    until the owner flips it. Serving is var.graph_service.serve; answering is
    var.graph_service.enabled.
  EOT
  type        = bool
  default     = false
}

variable "graph_service" {
  description = <<-EOT
    The graph service's setting (docs/design/graph-service.md §2.1, §8.1).
    `serve` puts SWARM_GRAPH_TICKET_KEY on swarm-api and renders the manifest:
    set it only once swarm-graph-ticket-key has a version and the image has a
    digest. `enabled` is the kill switch the service re-reads from its
    ConfigMap: off, every call is 503 `disabled` and the worker stages the
    graph as before. Both OFF by default.
  EOT
  type = object({
    serve   = optional(bool, false)
    enabled = optional(bool, false)
  })
  default = {}
}

locals {
  graph_service_namespace = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  graph_service_ksa       = "swarm-graph"
  graph_service_on        = var.enable_graph_service && var.enable_gke_autopilot
  graph_service_serving   = local.graph_service_on && var.graph_service.serve
  graph_service_image     = lookup(var.image_refs, "swarm-graph", "")

  graph_service_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${local.graph_service_namespace}/sa/${local.graph_service_ksa}"

  graph_ticket_key_version_name = local.graph_service_on ? "projects/${var.project_id}/secrets/${google_secret_manager_secret.graph_ticket_key[0].secret_id}/versions/latest" : ""

  # tenants/<one segment>/repos/<one segment>/graph/... and nothing else.
  # Cloud Storage conditions have no matches() and a per-tenant list would miss
  # tenants created at runtime, so this is the extract() boundary technique
  # terraform/modules/iam/bindings.tf documents for api_history_purge_deleter:
  # T, the text after `tenants/`, must be non-empty and equal the text before
  # the first `/repos/` (one segment); R, the text after that `/repos/`, must
  # be non-empty and equal the text before the first `/graph/` after it.
  # tests/terraform/graph_service_iam.tftest.hcl evaluates it.
  graph_read_condition = {
    title       = "swarm-graph-read-graph-only"
    description = "managed-by=swarm-terraform; swarm-graph reads tenants/<t>/repos/<r>/graph/** only."
    expression = join(" && ", [
      "resource.name.startsWith(\"projects/_/buckets/${module.storage.artifact_bucket_name}/objects/tenants/\")",
      "resource.name.extract(\"/objects/tenants/{t}/\") != \"\"",
      "resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/repos/\")",
      "resource.name.extract(\"/repos/{r}/\") != \"\"",
      "resource.name.extract(\"/repos/{r}/\") == resource.name.extract(\"/repos/{r}/graph/\")",
    ])
  }

  # The service's setting: EXACTLY swarm_graph.main.CONFIG_KEYS. Any other
  # key there is an invalid setting, which the service reads as OFF.
  # tests/unit/scripts/test_graph_service_manifest.py holds the two equal.
  graph_service_config = {
    enabled = var.graph_service.enabled
  }
  graph_service_config_json = jsonencode(local.graph_service_config)

  # The stats load balancer's address exists exactly when the manifest that
  # names it renders, so swarm-api is never given an address nothing listens
  # on.
  graph_stats_serving = local.graph_service_serving && local.graph_service_image != ""

  # swarm-api's Direct VPC egress range, admitted to 8081 by the manifest's
  # ingress policy: the same local, so the same range, as swarm-live-ingress
  # admits (live.tf; kubernetes/apply.sh holds it to swarm-api's service).
  graph_stats_swarm_api_cidr = local.live_swarm_api_egress_cidr

  # Where swarm-api's GET /v1/repositories/{repo_id}/graph-cache reads the
  # stats (apps/swarm-api/swarm_api/routes/graph_cache.py stats_url): plain
  # http to the reserved address, on swarm_graph.stats.STATS_PORT. Empty while
  # not served, which the route answers as a 200 with stats not_read.
  api_graph_stats_env = {
    SWARM_GRAPH_STATS_URL = local.graph_stats_serving ? "http://${google_compute_address.graph_stats[0].address}:8081" : ""
  }

  # swarm-graph-ticket-key for swarm-api, BY REFERENCE, once served (see the
  # deviation above).
  graph_ticket_key_secret_env = local.graph_service_serving ? {
    SWARM_GRAPH_TICKET_KEY = {
      secret  = google_secret_manager_secret.graph_ticket_key[0].secret_id
      version = "latest"
    }
  } : {}
}

resource "google_storage_bucket_iam_member" "graph_service_read" {
  count = local.graph_service_on ? 1 : 0

  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectViewer"
  member = local.graph_service_principal

  condition {
    title       = local.graph_read_condition.title
    description = local.graph_read_condition.description
    expression  = local.graph_read_condition.expression
  }
}

resource "google_secret_manager_secret" "graph_ticket_key" {
  count = local.graph_service_on ? 1 : 0

  project   = var.project_id
  secret_id = "swarm-graph-ticket-key"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "graph-ticket-key"
  })

  annotations = {
    "swarm-populated-by" = "generated, never shown (docs/design/graph-service.md section 3.2)"
  }
}

# swarm-api mints, swarm-graph verifies; nobody else. Authoritative, so a
# grant made out of band is removed on the next apply.
resource "google_secret_manager_secret_iam_binding" "graph_ticket_key_accessor" {
  count = local.graph_service_on ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.graph_ticket_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = [
    module.iam.service_account_members["swarm-api"],
    local.graph_service_principal,
  ]
}

# swarm-api's address for the stats port (graph-answer-cache.md §5.3), as
# live.tf's live_relay is the relay's: swarm-api is Cloud Run, reaches the VPC
# by Direct VPC egress on module.network.subnetwork_id and cannot resolve a
# cluster Service, so one replica's 8081 is fronted by an internal passthrough
# load balancer (`swarm-graph-stats` in the manifest) on this address, in that
# subnet. Reserved rather than ephemeral so a redeploy cannot move it from
# under SWARM_GRAPH_STATS_URL.
#
# NOT MEASURED here, as live_relay's path was not: this lane has no cluster.
# The path relies on what live.tf records for the relay's: allow_internal
# (modules/network/firewall.tf) admits the subnet to the nodes, and the
# Service's externalTrafficPolicy Cluster keeps the health check on the node.
resource "google_compute_address" "graph_stats" {
  count = local.graph_stats_serving ? 1 : 0

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-graph-stats"
  address_type = "INTERNAL"
  subnetwork   = module.network.subnetwork_id
  description  = "Internal load balancer address of swarm-graph's stats port, 8081 (docs/design/graph-answer-cache.md section 5.3)."

  labels = merge(local.labels, {
    "component" = "graph-stats"
  })
}

check "graph_service_needs_gke" {
  assert {
    condition     = !var.enable_graph_service || var.enable_gke_autopilot
    error_message = "enable_graph_service is set but enable_gke_autopilot is not: the graph service is a Deployment on the Autopilot cluster and its principal is that cluster's. Nothing was created."
  }
}

check "graph_service_serves_only_when_created" {
  assert {
    condition     = !var.graph_service.serve || local.graph_service_on
    error_message = "graph_service.serve is set but enable_graph_service (with enable_gke_autopilot) is not: there is no key to reference and nothing to serve."
  }
}

check "graph_service_has_an_image" {
  assert {
    condition     = !local.graph_service_serving || local.graph_service_image != ""
    error_message = "graph_service.serve is set but image_refs has no swarm-graph digest, so there is no manifest to apply. Build and promote the image (scripts/build-images.sh, scripts/push-images.sh) first."
  }
}

check "graph_service_switch_needs_the_service" {
  assert {
    condition     = !var.graph_service.enabled || local.graph_service_serving
    error_message = "graph_service.enabled is set but the service is not served (enable_graph_service and graph_service.serve): swarm-api mints no graph ticket and nothing answers."
  }
}

output "graph_service" {
  description = "Graph service wiring: identity, the key's secret name, the bucket it reads and the rendered setting. Names only; never a value."
  value = {
    created         = local.graph_service_on
    serving         = local.graph_service_serving
    namespace       = local.graph_service_namespace
    service_account = local.graph_service_ksa
    principal       = local.graph_service_principal
    key_secret      = local.graph_service_on ? google_secret_manager_secret.graph_ticket_key[0].secret_id : ""
    bucket          = module.storage.artifact_bucket_name
    config          = local.graph_service_config
    # What an agent pod's shim dials (design §2.2): the headless Service.
    url = "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"
    # swarm-api's SWARM_GRAPH_STATS_URL (graph-answer-cache.md §5.3); "" while
    # not served.
    stats_url = local.api_graph_stats_env.SWARM_GRAPH_STATS_URL
    # What the manifest's ingress policy admits to 8081.
    swarm_api_egress_cidr = local.graph_stats_swarm_api_cidr
  }
}

# kubernetes/swarm-system/swarm-graph.yaml with its placeholders filled. Null
# until served with an image, so there is never a half-rendered manifest to
# apply.
output "swarm_graph_manifest" {
  description = "The graph service's Kubernetes objects, rendered. Apply to swarm-autopilot only (never agents-staging). Null until graph_service.serve is set and the image has a digest."
  value = local.graph_stats_serving ? replace(replace(replace(replace(replace(replace(replace(
    file("${path.module}/../../kubernetes/swarm-system/swarm-graph.yaml"),
    "__SWARM_GRAPH_IMAGE__", local.graph_service_image),
    "__GRAPH_TICKET_KEY_VERSION__", local.graph_ticket_key_version_name),
    "__GRAPH_BUCKET__", module.storage.artifact_bucket_name),
    "__GRAPH_CONFIG_SHA256__", sha256(local.graph_service_config_json)),
    "__GRAPH_CONFIG_JSON__", local.graph_service_config_json),
    "__GRAPH_STATS_ADDRESS__", google_compute_address.graph_stats[0].address),
  "__SWARM_API_EGRESS_CIDR__", local.graph_stats_swarm_api_cidr) : null
}
