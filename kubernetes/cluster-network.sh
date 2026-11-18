# shellcheck shell=bash
# The swarm cluster's network, read from the cluster. SOURCED, never executed.
#
#   resolve_cluster_network KUBECTL CLUSTER LOCATION PROJECT [KUBECTL_ARGS...]
#
# Sourced by kubernetes/apply.sh, which renders the tenant egress policy with
# these values, and by scripts/lib/check-cluster-network-parity.sh, which holds
# every applied copy of that policy to them. ONE implementation for both, so the
# thing that renders and the thing that checks cannot read different fields.
# Source scripts/lib/common.sh first: this uses its err, dim, redact and
# die_if_auth_failure.
#
# WHY THIS EXISTS. Until 2026-09-24 the egress policy was rendered from
# render.py's defaults -- pods 10.0.0.0/8, services 34.118.224.0/20 -- and had
# no DNS address at all, only a rule to the kube-dns pods. swarm-autopilot's
# pods are 10.44.0.0/14, its services 10.48.0.0/20, and its DNS is Cloud DNS
# behind NodeLocal DNSCache, which never sends a pod's query to those pods. A
# browser worker ran for 390 s with every lookup silently dropped.
#
# WHERE EACH VALUE COMES FROM, measured against swarm-autopilot on 2026-09-24.
# Two of the four are NOT in `gcloud container clusters describe` at all -- its
# output was searched for both addresses and contains neither -- so they are
# read from the objects that actually serve DNS:
#
#   CLUSTER_POD_CIDR           describe .clusterIpv4Cidr, which must equal
#                              .ipAllocationPolicy.clusterIpv4CidrBlock
#                              (measured: both 10.44.0.0/14)
#   CLUSTER_SERVICE_CIDR       describe .servicesIpv4Cidr, which must equal
#                              .ipAllocationPolicy.servicesIpv4CidrBlock
#                              (measured: both 10.48.0.0/20)
#   CLUSTER_DNS_IP             `kubectl -n kube-system get service kube-dns`
#                              .spec.clusterIP (measured: 10.48.0.10). The
#                              "tenth address of the service range" convention
#                              holds on this cluster, but a convention is not a
#                              reading; GKE's own NodeLocal DNSCache guidance
#                              reads it from the Service exactly this way.
#   CLUSTER_NODE_LOCAL_DNS_IP  the first entry of `-localip` on the node-cache
#                              container of `kubectl -n kube-system get
#                              daemonset node-local-dns` (measured:
#                              `-localip 169.254.20.10,10.48.0.10`, so
#                              169.254.20.10; the second entry is the kube-dns
#                              IP, which the agent also answers on). describe
#                              says only WHETHER the cache runs:
#                              .addonsConfig.dnsCacheConfig.enabled, required
#                              true here (GKE: always on for Autopilot).
#   CLUSTER_NETWORK_SOURCE     gke/<project>/<location>/<cluster>, recorded on
#                              the rendered policy.
#
# NEVER GUESSES. Any value that cannot be read is a return 1 with the reason,
# not a fallback: a fallback is how the defaults above reached a cluster.
#
# Read-only: one `describe`, two `kubectl get`. The kubectl calls go through
# KUBECTL_ARGS (normally `--context <ctx>`), so the caller decides -- and has
# already checked -- which cluster is read.

resolve_cluster_network() {
  local kubectl="$1" cluster="$2" location="$3" project="$4"
  shift 4
  local kargs=("$@")
  local work
  work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-cluster-network.XXXXXX")"

  CLUSTER_POD_CIDR=""
  CLUSTER_SERVICE_CIDR=""
  CLUSTER_DNS_IP=""
  CLUSTER_NODE_LOCAL_DNS_IP=""
  CLUSTER_NETWORK_SOURCE=""

  # --- the ranges, and whether NodeLocal DNSCache runs ------------------------
  if ! gcloud container clusters describe "${cluster}" \
        --project "${project}" --location "${location}" --format=json \
        >"${work}/describe.json" 2>"${work}/describe.err"; then
    # die_if_auth_failure exits on an expired session, which reads the same as a
    # missing cluster everywhere except stderr.
    die_if_auth_failure "$(cat "${work}/describe.err")"
    err "could not describe cluster ${cluster} in ${location} (project ${project}):"
    redact <"${work}/describe.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    rm -rf "${work}"
    return 1
  fi

  local pods pods_alt services services_alt cache
  pods="$(jq -r '.clusterIpv4Cidr // empty' "${work}/describe.json")"
  pods_alt="$(jq -r '.ipAllocationPolicy.clusterIpv4CidrBlock // empty' "${work}/describe.json")"
  services="$(jq -r '.servicesIpv4Cidr // empty' "${work}/describe.json")"
  services_alt="$(jq -r '.ipAllocationPolicy.servicesIpv4CidrBlock // empty' "${work}/describe.json")"
  # `== true`, never `// false`: jq's alternative operator treats false as
  # absent (CLAUDE.md), so compare the value itself.
  cache="$(jq -r 'if .addonsConfig.dnsCacheConfig.enabled == true then "on" else "off" end' \
    "${work}/describe.json")"
  rm -rf "${work}/describe.json" "${work}/describe.err"

  [[ -n "${pods}" ]] || pods="${pods_alt}"
  [[ -n "${services}" ]] || services="${services_alt}"
  if [[ -n "${pods_alt}" && "${pods}" != "${pods_alt}" ]]; then
    err "${cluster} reports two pod ranges: clusterIpv4Cidr=${pods}, ipAllocationPolicy.clusterIpv4CidrBlock=${pods_alt}"
    rm -rf "${work}"; return 1
  fi
  if [[ -n "${services_alt}" && "${services}" != "${services_alt}" ]]; then
    err "${cluster} reports two service ranges: servicesIpv4Cidr=${services}, ipAllocationPolicy.servicesIpv4CidrBlock=${services_alt}"
    rm -rf "${work}"; return 1
  fi
  local cidr_re='^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$'
  local ip_re='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
  if [[ ! "${pods}" =~ ${cidr_re} ]]; then
    err "${cluster}: no pod range in describe (.clusterIpv4Cidr); got '${pods}'"
    rm -rf "${work}"; return 1
  fi
  if [[ ! "${services}" =~ ${cidr_re} ]]; then
    err "${cluster}: no service range in describe (.servicesIpv4Cidr); got '${services}'"
    rm -rf "${work}"; return 1
  fi
  if [[ "${cache}" != "on" ]]; then
    err "${cluster}: NodeLocal DNSCache is off (addonsConfig.dnsCacheConfig.enabled is not true)."
    err "The egress policy's DNS rule is written for it -- GKE runs it on every Autopilot"
    err "cluster and does not allow turning it off -- so this is not the cluster that policy"
    err "was written for. Refusing rather than rendering DNS to an address nothing answers on."
    rm -rf "${work}"; return 1
  fi

  # --- the kube-dns Service IP -------------------------------------------------
  if ! "${kubectl}" ${kargs[@]+"${kargs[@]}"} -n kube-system get service kube-dns \
        -o 'jsonpath={.spec.clusterIP}' >"${work}/kube-dns" 2>"${work}/kube-dns.err"; then
    err "could not read the kube-dns Service in kube-system:"
    redact <"${work}/kube-dns.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    rm -rf "${work}"; return 1
  fi
  local dns
  dns="$(tr -d '[:space:]' <"${work}/kube-dns")"
  if [[ ! "${dns}" =~ ${ip_re} ]]; then
    err "the kube-dns Service has no usable clusterIP (got '${dns}')"
    rm -rf "${work}"; return 1
  fi

  # --- the NodeLocal DNSCache address ----------------------------------------
  if ! "${kubectl}" ${kargs[@]+"${kargs[@]}"} -n kube-system get daemonset node-local-dns \
        -o json >"${work}/node-local-dns.json" 2>"${work}/node-local-dns.err"; then
    err "could not read the node-local-dns DaemonSet in kube-system, although describe"
    err "reports NodeLocal DNSCache on:"
    redact <"${work}/node-local-dns.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    rm -rf "${work}"; return 1
  fi
  # `-localip A,B` or `-localip=A,B` on the node-cache container; A is the
  # node-local address, B (when present) the kube-dns IP it also answers on.
  local node_local
  node_local="$(jq -r '
    [ .spec.template.spec.containers[]? | select(.name == "node-cache") | (.args // []) as $a
      | range(0; $a | length) as $i
      | if $a[$i] == "-localip" then ($a[$i + 1] // "")
        elif ($a[$i] | startswith("-localip=")) then ($a[$i] | ltrimstr("-localip="))
        else empty end ]
    | (.[0] // "") | split(",") | (.[0] // "")' "${work}/node-local-dns.json" 2>/dev/null || true)"
  rm -rf "${work}"
  if [[ ! "${node_local}" =~ ${ip_re} ]]; then
    err "the node-local-dns DaemonSet's node-cache container has no usable -localip (got '${node_local}')"
    return 1
  fi

  CLUSTER_POD_CIDR="${pods}"
  CLUSTER_SERVICE_CIDR="${services}"
  CLUSTER_DNS_IP="${dns}"
  CLUSTER_NODE_LOCAL_DNS_IP="${node_local}"
  CLUSTER_NETWORK_SOURCE="gke/${project}/${location}/${cluster}"
  return 0
}
