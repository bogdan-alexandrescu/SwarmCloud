# The TypeSafe egress allowlist: which pods may name api.typesafe.ai, and how to apply it

**When:** the owner, once per cluster that has FQDN network policy
(`gke_enable_fqdn_network_policy`, on in dev since 2026-11-01), after the
release carrying `kubernetes/network-policies/swarm-system-typesafe-fwd.yaml`;
again for a tenant whose TypeSafe-backed pods call TypeSafe without the
forwarder; and to roll either back.

Built 2026-11-02 because the owner chose to build every improvement proposal
in CLD's Jev report, this one included: "no host allowlist exists for
TypeSafe" (`docs/jev-data-flow.md`, Which egress path).

## What it is

Two GKE FQDNNetworkPolicies name `api.typesafe.ai`, and nothing else in
`kubernetes/` does:

| Policy | Namespace | Selects | Admits |
|---|---|---|---|
| `swarm-typesafe-fwd-egress-hosts` (`kubernetes/network-policies/swarm-system-typesafe-fwd.yaml`) | `swarm-system` | the forwarder's pods, `app.kubernetes.io/name: swarm-typesafe-fwd` | TCP 443 to `api.typesafe.ai` and `secretmanager.googleapis.com`, the two hosts its code dials |
| `swarm-allow-typesafe-direct` (`kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml`) | each tenant namespace that needs it | a pod labelled `swarm-jev: typesafe-direct` | TCP 443 to `api.typesafe.ai` |

The same file as the first adds `swarm-typesafe-fwd-egress`, a NetworkPolicy
that puts the forwarder's pods under **egress isolation** for the first time:
cluster DNS and the metadata server by address, the two hosts above by name,
and nothing else. The forwarder is the one process that holds workspaces'
TypeSafe keys; before this, it could open a socket anywhere.

**Who carries which label**, set by the dispatcher from configuration and the
stored settings, never by a caller (invariant 10):

* `swarm-typesafe: inject` (existing, `scheduler.dispatch.TYPESAFE_POD_LABEL`):
  the forwarder serves the tenant. The pod reaches TypeSafe **only through the
  forwarder**, on TCP 8080 (`allow-typesafe-fwd.yaml`); no policy names
  `api.typesafe.ai` for it.
* `swarm-jev: typesafe-direct` (new, `scheduler.dispatch.TYPESAFE_DIRECT_POD_LABEL`,
  set by `with_typesafe_direct`): a pod whose worker calls TypeSafe itself --
  its `SWARM_JEV_BACKEND` is `typesafe` (a live browser on the TypeSafe
  judgment backend) or its quality gates' verdict `SWARM_QUALITY_GATES_JEV` is
  `true` or `on_request` (the lane hooks always call TypeSafe), or its signed
  workflow switch says `jev_judgments: on` on the TypeSafe backend with a
  runner that carries the Jev client (`_with_judgments_forwarder`; the worker
  resolves the workspace's key, `jevkey.resolve_workspace`) -- and that is not
  labelled `inject`. A different key from `swarm-typesafe`, so the
  forwarder's ingress never admits a direct pod and no pod carries both.
* Neither: every other worker pod. No policy names `api.typesafe.ai` for it.

`tests/unit/scripts/test_typesafe_egress_allowlist.py` judges every
FQDNNetworkPolicy under `kubernetes/` against those three pods and the
forwarder; `tests/unit/control_plane/test_dispatch_typesafe_direct_label.py`
holds the dispatcher's label.

## What it does not do: the gap, stated plainly

**An unlabelled tenant pod can still reach api.typesafe.ai.**
`allow-egress.yaml` rule 4 admits every worker pod to the public internet,
minus private ranges, on TCP 443, because agents need the general internet for
git and package installs. NetworkPolicies and FQDNNetworkPolicies are
allow-only and add up as a union: no rule in either kind can take one host back
out of rule 4. The only subtraction a NetworkPolicy has is rule 4's `except`
list, and `api.typesafe.ai` answers from shared Cloudflare addresses
(`104.18.24.46`, `104.18.25.46`, read with `getent ahosts` on 2026-11-02):
excepting them would cut every other site that shares them, and the next
address change would quietly reopen the path. So the tenant-side policy
**names** the host for the pods meant to use it; it does not yet **deny** it to
the rest. What keeps an unlabelled pod's call from doing anything today is the
credential: such a pod holds no workspace TypeSafe key (the worker writes one
only for a TypeSafe-backed live browser, quality gates or a `jev_judgments`
step, which are exactly the labelled pods), and a forwarded pod's worker holds only a loopback-shim token.

Closing the gap is an owner decision, asked in this lane's `questions.json`:
a hostname sinkhole for `api.typesafe.ai` on every pod not labelled direct
(the dispatcher's `hostAliases`, which stops resolution through the ordinary
resolver but not a client that dials an address), or moving rule 4's general
internet behind an egress proxy that refuses the host by name.

## Apply it

Both files are applied by hand today: neither is in `kubernetes/render.py`,
so neither `apply.sh --swarm-system` nor `scripts/apply-tenant-network-policies.sh`
applies it. They render NetworkPolicies only, into every cluster, and an
FQDNNetworkPolicy is refused where FQDN network policy is off; the workspace
deployer's RBAC and scope policy admit `networkpolicies` only. Wiring them in
behind a cluster check is the second question in this lane's
`questions.json`.

Use the context of `swarm-autopilot`, never `agents-staging` (CLAUDE.md
rule 2), and a current `kubectl` (`kubectl_bin` in `scripts/lib/common.sh`:
an old client silently drops manifest fields).

```bash
CTX=gke_saga-agents-staging_us-central1_swarm-autopilot

# 0. The cluster admits FQDNNetworkPolicy. No output means it does not:
#    stop, and do not apply step 1 (the forwarder would reach only DNS and
#    the metadata server, and every forwarded Jev call would fail).
kubectl --context "$CTX" get crd fqdnnetworkpolicies.networking.gke.io -o name

# 1. The forwarder: egress isolation and its two hosts, in ONE apply.
kubectl --context "$CTX" apply -f kubernetes/network-policies/swarm-system-typesafe-fwd.yaml
```

What step 1 changes: `swarm-typesafe-fwd`'s pods go from unrestricted egress
to DNS (the kube-dns pods, and `169.254.20.10` on 53), the metadata server
(`169.254.169.254` on 80 and 8080) and TCP 443 to `api.typesafe.ai` and
`secretmanager.googleapis.com`. Nothing else in `swarm-system` is selected.

```bash
# 2. Per tenant whose pods call TypeSafe directly (a TypeSafe-backed workspace
#    the forwarder does not serve, or quality gates on without it):
TENANT=eng
sed -e "s/__NAMESPACE__/swarm-tenant-${TENANT}/" -e "s/__TENANT_ID__/${TENANT}/" \
  kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml \
  | kubectl --context "$CTX" apply -f -
```

What step 2 changes today: nothing a packet notices (rule 4 above). It names
the host for pods labelled `swarm-jev: typesafe-direct`, and is the rule that
keeps them working on the day rule 4 stops covering `api.typesafe.ai`.

## Verify

The forwarder resolves and reaches its upstream, and reaches nothing else
(its image carries Python; nothing is installed):

```bash
kubectl --context "$CTX" -n swarm-system exec deploy/swarm-typesafe-fwd -- python -c '
import socket
for host in ("api.typesafe.ai", "secretmanager.googleapis.com", "example.com"):
    try:
        socket.create_connection((host, 443), timeout=5).close(); print(host, "open")
    except OSError as exc:
        print(host, "closed", type(exc).__name__)'
# expect: api.typesafe.ai open, secretmanager.googleapis.com open,
#         example.com closed (a timeout: Dataplane V2 drops, never refuses)
```

`example.com closed` is the control that shows the first two could have come
out the other way. Then one forwarded Jev call from a labelled pod (a live
browser in a tenant the forwarder serves) succeeds, and the forwarder's log
shows the upstream status.

## Roll back

```bash
kubectl --context "$CTX" -n swarm-system delete \
  networkpolicy/swarm-typesafe-fwd-egress fqdnnetworkpolicy/swarm-typesafe-fwd-egress-hosts
kubectl --context "$CTX" -n "swarm-tenant-${TENANT}" delete fqdnnetworkpolicy/swarm-allow-typesafe-direct
```

Delete both forwarder objects together: the FQDN policy alone, with the
NetworkPolicy gone, adds to swarm-system's unrestricted egress and restricts
nothing.

## How CONTRACT.md's invariants hold

Nothing here creates demand, reserves capacity, counts concurrency, parks,
fences, chooses Spot or sizes a pod (invariants 1-8). Invariant 9: the direct
policy is per tenant namespace and selects only that namespace's pods.
Invariant 10: both labels come from configuration and the stored settings, as
the pod's environment already did; no caller field reaches them.
