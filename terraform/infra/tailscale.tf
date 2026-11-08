# Tailscale egress: the one path from swarm-autopilot onto the organisation's tailnet, to
# the internal decision API Jev's "Internal Jev" and "Internal Laya" judgment
# backends call (owner decisions 2026-10-14; lane JUDGE-NET;
# docs/runbooks/tailscale-egress.md).
#
# Three things, and only these:
#
#   * `swarm-tailscale-oauth`, the secret that holds the Tailscale OAuth
#     client's secret (tag:swarmcloud, created by the tailnet admin). Its
#     container and its one accessor are here; its VALUE never is. The owner
#     adds it on stdin with `scripts/create-secrets.sh --tailscale-oauth
#     --stdin`, so it is in no tfvars file, no plan, no state, no environment
#     and no log;
#   * that accessor: the identity that copies it into the cluster;
#   * the manifest, kubernetes/swarm-system/tailscale-egress.yaml with the
#     tailnet name filled in, as `tailscale_egress_manifest`, for
#     `kubernetes/apply.sh --swarm-system` to apply.
#
# WHO READS THE SECRET, AND WHY THAT ONE. The pod runs containerboot, which
# reads the OAuth secret from a FILE, so the value has to reach a Kubernetes
# Secret (`swarm-system/operator-oauth`) one way or another. Three ways were
# weighed:
#
#   1. the pod reads Secret Manager itself, as swarm-egress does. The image
#      has no Google client; its only HTTPS client is busybox wget, whose TLS
#      does not verify the server's certificate. The credential would cross
#      an unverified connection. Refused;
#   2. a tiny init Job running as a dedicated GSA. It would need its own
#      image with gcloud, its own Workload Identity binding, and Kubernetes
#      RBAC to write Secrets in swarm-system -- and `create` cannot be scoped
#      to one Secret name, so that Role could create any Secret there. More
#      identities, wider Kubernetes rights;
#   3. the sync step in `kubernetes/apply.sh --swarm-system --confirm`, which
#      already runs through the shared-cluster refusals and already writes
#      swarm-system. It reads the version with gcloud (verified TLS) into a
#      0600 file and applies the Secret, never printing either. Its identity
#      is the one that runs that apply: the CI deployer
#      (var.deployer_service_account, in the release's roll of the relay) or
#      the owner by hand (a project owner reads every secret already).
#
# (3) is the narrowest: it adds ONE reader, an identity that already
# administers this secret's IAM policy -- terraform applies this binding as
# the deployer -- so it gains no reach it could not grant itself, and it adds
# no Kubernetes right to anything. The binding is authoritative, so a grant
# made out of band is removed on the next apply; absent while the switch is
# off, when nothing may read the secret.
#
# THE OWNER APPLIES THIS. The order (the runbook has the commands):
#
#   1. `make apply` with enable_tailscale_egress false: the container exists,
#      nobody reads it, nothing is rendered;
#   2. the tailnet admin creates the OAuth client (tag:swarmcloud, auth keys
#      write) and the ACL that limits tag:swarmcloud to the decision API on
#      tcp:443; the owner stores its secret with create-secrets.sh;
#   3. `enable_tailscale_egress = true`, apply: the accessor and the manifest;
#   4. `kubernetes/apply.sh --policies --confirm` (the workspace deployer may
#      then write swarm-allow-judge-egress), then `kubernetes/apply.sh
#      --swarm-system --confirm`, which applies the manifest to
#      swarm-autopilot, refuses peer-cluster, and syncs the Secret.
#
# GKE ONLY: the forwarder is a pod on the Autopilot cluster.

variable "enable_tailscale_egress" {
  description = <<-EOT
    Deploy the decision-api tailnet egress (terraform/infra/tailscale.tf,
    kubernetes/swarm-system/tailscale-egress.yaml): give the deployer read on
    swarm-tailscale-oauth and render `tailscale_egress_manifest` for
    kubernetes/apply.sh --swarm-system. Set it only after the tailnet admin has
    created the tag:swarmcloud OAuth client and its ACL and the owner has
    stored the client secret (docs/runbooks/tailscale-egress.md).
  EOT
  type        = bool
  default     = false
}

variable "tailscale_egress_target" {
  description = <<-EOT
    The tailnet name the forwarder dials, on 443, and the only one: the
    decision API (owner decision 2026-10-14). Its /jev and /laya paths, and
    the root, are all behind this one host, so one forwarder serves both
    internal judgment backends.

    NO DEFAULT (null): it names one tailnet, and a default would carry that
    tailnet into every install (DAYONE-TF-1). The Tailscale add-on sets it;
    without the add-on nothing reads it. Required once enable_tailscale_egress
    or internal_decision_api_enabled is on.
  EOT
  type        = string
  default     = null

  validation {
    # A MagicDNS name: <host>.<tailnet>.ts.net. It is written into a command
    # line and an annotation, so nothing else is accepted.
    condition     = var.tailscale_egress_target == null || can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.[a-z0-9-]+\\.ts\\.net$", var.tailscale_egress_target))
    error_message = "tailscale_egress_target is one MagicDNS name, <host>.<tailnet>.ts.net, in lower case."
  }

  validation {
    # Refused at plan rather than rendered as "https://" or an empty forwarder
    # target, which would fail only when a judging pod first dials it.
    condition     = var.tailscale_egress_target != null || (!var.enable_tailscale_egress && !var.internal_decision_api_enabled)
    error_message = "tailscale_egress_target is unset, but enable_tailscale_egress or internal_decision_api_enabled is on: set the decision API's MagicDNS name, <host>.<tailnet>.ts.net."
  }
}

locals {
  tailscale_namespace       = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  tailscale_k8s_object      = "operator-oauth"
  tailscale_egress_enabled  = var.enable_tailscale_egress && var.enable_gke_autopilot
  tailscale_secret_accessor = local.tailscale_egress_enabled ? compact([local.deployer_member]) : []

  # The `decision-api` Service's clusterIP, FIXED, and stated here ONCE. A
  # judging pod reaches the decision API by its tailnet name, which the
  # cluster's DNS does not know: the scheduler gives exactly those pods a
  # hostAliases entry pinning var.tailscale_egress_target to this address
  # (owner decision 2026-10-15, #1380), so TLS verifies the API's own
  # certificate end to end. A Service that came back with another address
  # would leave every such pod dialling nothing, so it may not get one.
  # 10.48.6.120 is the address Kubernetes gave it on swarm-autopilot, read
  # 2026-10-15, so pinning it changed nothing live; it is inside dev's
  # services_cidr (10.48.0.0/20) and the precondition below refuses a
  # render whose range does not hold it. scheduler.judgment
  # DECISION_API_CLUSTER_IP restates it; tests/unit/control_plane/
  # test_dispatch_judgment.py holds the two equal.
  decision_api_cluster_ip = "10.48.6.120"

  # SWARM-API'S OWN ROUTE to the decision API (docs/design/agent-quality/
  # jev-contract.md section 4.1; the owner's answer to Q1, 3 November 2026).
  # The contract review runs inside swarm-api's submission request, and
  # swarm-api is Cloud Run: it cannot resolve the `decision-api` Service, and
  # it has no /etc/hosts to pin the tailnet name in. It already reaches the VPC
  # by Direct VPC egress on module.network.subnetwork_id (ALL_TRAFFIC, main.tf),
  # so the manifest puts an internal passthrough load balancer,
  # `decision-api-internal`, in front of the same forwarder pods, on the
  # reserved address below. TLS still passes through untouched: swarm-api
  # dials this address and sends SNI and Host for the tailnet name
  # (swarm_api.judgment `pinned_transport`), exactly what a judging pod's
  # hostAliases entry does with the clusterIP.
  #
  # The range the forwarder's ingress policy admits on 8443 beside the judging
  # pods: swarm-api's Direct VPC egress range, var.subnet_cidr, the same local
  # live.tf and graph_service.tf admit for the relay and the stats port.
  decision_api_swarm_api_cidr = var.subnet_cidr

  # What swarm-api is given: the address, only while the internal backends are
  # offered at all (internal_decision_api_enabled, which sets
  # INTERNAL_DECISION_API_URL) and the load balancer is rendered. Empty, a
  # contract review asked of an internal backend reads `no_route`
  # (swarm_api.judgment.resolve_call_backend) -- never TypeSafe in its place.
  api_decision_route_env = {
    INTERNAL_DECISION_API_ADDRESS = local.tailscale_egress_enabled && var.internal_decision_api_enabled ? google_compute_address.decision_api_route[0].address : ""
  }
}

# The internal load balancer's address (see local.decision_api_swarm_api_cidr
# above), as graph_service.tf's graph_stats is the stats port's: reserved, not
# ephemeral, so a re-applied Service cannot move it from under
# INTERNAL_DECISION_API_ADDRESS. Exists exactly when the manifest that names it
# renders. INTERNAL, in the platform's own subnet: nothing outside the VPC can
# reach it, and nothing of the shared project's (CLAUDE.md rule 2) is named.
#
# NOT MEASURED here, as live_relay's and graph_stats' paths were not: this lane
# has no cluster. The path relies on what live.tf records for the relay's:
# allow_internal (modules/network/firewall.tf) admits the subnet to the nodes,
# and the Service's externalTrafficPolicy Cluster keeps the health check on
# the node.
resource "google_compute_address" "decision_api_route" {
  count = local.tailscale_egress_enabled ? 1 : 0

  project      = var.project_id
  region       = var.region
  name         = "${var.name_prefix}-decision-api-route"
  address_type = "INTERNAL"
  subnetwork   = module.network.subnetwork_id
  description  = "Internal passthrough load balancer address of the decision-api tailnet egress, swarm-api's route to the Jev backend (docs/design/agent-quality/jev-contract.md section 4.1)."

  labels = merge(local.labels, {
    "component" = "tailscale-egress"
  })
}

resource "google_secret_manager_secret" "tailscale_oauth" {
  project   = var.project_id
  secret_id = "swarm-tailscale-oauth"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "tailscale-egress"
  })

  annotations = {
    "swarm-populated-by" = "the owner, on stdin only (scripts/create-secrets.sh --tailscale-oauth --stdin)"
  }
}

# The identity that syncs it into the cluster, and NOBODY ELSE (see the
# header). None while the switch is off, or when no deployer is named -- an
# owner applying by hand reads it as project owner and needs no binding.
resource "google_secret_manager_secret_iam_binding" "tailscale_oauth_accessor" {
  count = length(local.tailscale_secret_accessor) > 0 ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.tailscale_oauth.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = local.tailscale_secret_accessor
}

check "tailscale_egress_needs_gke" {
  assert {
    condition     = !var.enable_tailscale_egress || var.enable_gke_autopilot
    error_message = "enable_tailscale_egress is set but enable_gke_autopilot is not: the decision-api forwarder is a pod on the Autopilot cluster. Nothing was granted or rendered."
  }
}

output "tailscale_egress" {
  description = "Tailscale egress wiring: names only, never a value."
  value = {
    enabled           = local.tailscale_egress_enabled
    namespace         = local.tailscale_namespace
    service           = "decision-api"
    port              = 443
    target            = var.tailscale_egress_target
    tag               = "tag:swarmcloud"
    oauth_secret      = google_secret_manager_secret.tailscale_oauth.secret_id
    kubernetes_secret = local.tailscale_k8s_object
    pod_label         = { "swarm-judge" = "internal" }
    # swarm-api's route (jev-contract.md section 4.1): the internal load
    # balancer's Service, its address ("" while off) and the range its
    # ingress policy admits.
    internal_service      = "decision-api-internal"
    swarm_api_address     = local.tailscale_egress_enabled ? google_compute_address.decision_api_route[0].address : ""
    swarm_api_egress_cidr = local.decision_api_swarm_api_cidr
  }
}

# kubernetes/swarm-system/tailscale-egress.yaml with the tailnet name, the
# Service's fixed clusterIP, the internal load balancer's reserved address and
# swarm-api's egress range filled in. Null while the switch is off, so
# apply.sh applies nothing about the tailnet and syncs no secret.
output "tailscale_egress_manifest" {
  description = "The decision-api tailnet egress, rendered. Apply to swarm-autopilot only (never peer-cluster), with kubernetes/apply.sh --swarm-system. Null while enable_tailscale_egress is off."
  value = local.tailscale_egress_enabled ? replace(replace(replace(replace(
    file("${path.module}/../../kubernetes/swarm-system/tailscale-egress.yaml"),
    "__TAILNET_FQDN__", var.tailscale_egress_target),
    "__DECISION_API_CLUSTER_IP__", local.decision_api_cluster_ip),
    "__DECISION_API_ROUTE_ADDRESS__", google_compute_address.decision_api_route[0].address),
    "__SWARM_API_EGRESS_CIDR__", local.decision_api_swarm_api_cidr
  ) : null

  precondition {
    # cidrsubnet(x, 0, 0) is the network x's address lies in, at the
    # services range's prefix length.
    condition     = !local.tailscale_egress_enabled || cidrsubnet("${local.decision_api_cluster_ip}/${split("/", var.services_cidr)[1]}", 0, 0) == var.services_cidr
    error_message = "local.decision_api_cluster_ip is not inside var.services_cidr, so Kubernetes would refuse the decision-api Service's fixed clusterIP. Pick a free address in this cluster's services range and change it, and scheduler.judgment DECISION_API_CLUSTER_IP with it."
  }
}
