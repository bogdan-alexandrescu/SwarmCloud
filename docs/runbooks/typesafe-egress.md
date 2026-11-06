# The TypeSafe egress allowlist: which pods may name api.typesafe.ai, and how it is applied

**When:** nobody applies these by hand any more. Read this to know where the
two policies come from, to check one cluster's state, or to roll one back.
Since 2026-11-02 (the owner's pick "Wire behind a cluster check") the code that
applies each policy's neighbours applies it too, and only on a cluster whose
API serves `fqdnnetworkpolicies.networking.gke.io` -- today only dev
(`gke_enable_fqdn_network_policy`, on since 2026-11-01).

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

Closing the gap was put to the owner -- a hostname sinkhole for
`api.typesafe.ai` on every pod not labelled direct (the dispatcher's
`hostAliases`, which stops resolution through the ordinary resolver but not a
client that dials an address), or moving rule 4's general internet behind an
egress proxy that refuses the host by name -- and on 2026-11-02 the owner
picked **"Leave it named, not denied"**: no deny for unlabelled pods is built.
The gap above stands, stated, and the credential is what holds it.

## How they are applied: no hand command is needed

The hand commands this section used to hold are no longer needed. Each policy
rides with what it belongs to, behind a check that the cluster serves the
kind; a cluster that does not is left exactly as it was before the policies
existed, and says so.

**The forwarder's policy** (both objects in
`kubernetes/network-policies/swarm-system-typesafe-fwd.yaml`) is part of
terraform/infra's `swarm_typesafe_fwd_manifest` (`terraform/infra/typesafe_fwd.tf`),
as swarm-voice's policy rides in `swarm_voice_manifest`, so the release's
`scripts/roll-typesafe-fwd.sh` -- `kubernetes/apply.sh --swarm-typesafe-fwd` --
applies the forwarder and its egress policy in one apply. Only where the
environment's tfvars set `gke_enable_fqdn_network_policy = true`, and then
both objects or neither: the NetworkPolicy without the FQDNNetworkPolicy would
isolate the forwarder with no host to reach. Where the variable is off the
manifest carries neither and the forwarder keeps the egress it had. If the
manifest carries the policy and the cluster's API does not serve
`fqdnnetworkpolicies.networking.gke.io` (a tfvars that says on for a cluster
that is off), `apply.sh --swarm-typesafe-fwd` refuses the whole manifest and
applies nothing.

**The tenant's policy** (`kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml`,
`swarm-allow-typesafe-direct`) is rendered by `kubernetes/render.py tenant`
only with `--fqdn-network-policy`, after every other document, and
`kubernetes/apply.sh --tenant` passes that flag only when the cluster's API
serves the kind (`kubectl api-resources --api-group=networking.gke.io -o
name`, a discovery read every authenticated caller may make). The flag is read
from the cluster and refused on apply.sh's command line. Otherwise apply.sh
prints one line --

```
==> hostname policy  skipped: the cluster does not serve fqdnnetworkpolicies.networking.gke.io (FQDN network policy is off); swarm-allow-typesafe-direct is not rendered
```

-- and the tenant's onboarding goes on with everything else. A cluster that
cannot answer discovery gets a warning and the same skip. So every tenant
namespace the release or an operator applies on dev carries the policy;
`scripts/apply-tenant-network-policies.sh` renders NetworkPolicies only and
does not.

**The workspace deployer** (`swarm-workspace-deployer`, the identity that
makes a person's, group's or service's namespace) is granted the kind for the
one policy name: `get` and `patch` with `resourceNames:
[swarm-allow-typesafe-direct]` in `kubernetes/rbac/provisioner-rbac.yaml`, and
`create`, which RBAC cannot narrow by name, bounded by
`kubernetes/policies/workspace-provisioner-scope.yaml`, which admits that one
name and refuses every other. Both are applied by `kubernetes/apply.sh
--policies`. **What the workspace job still does not do:** its call guard
(`scripts/lib/workspace-calls.json`) admits neither the discovery read nor the
kind, and a refused call stops the job, so `apply.sh --tenant` under the guard
(`SWARM_CALL_GUARD` set) skips the policy with a line saying so. A namespace
the job made carries no `swarm-allow-typesafe-direct` until the guard admits
both; that change is the owner's question in this lane's pull request.

Neither policy is applied to `agents-staging`: every path above goes through
apply.sh's cluster check (CLAUDE.md rule 2).

### Checking one cluster by hand

Use the context of `swarm-autopilot`, never `agents-staging`, and a current
`kubectl` (`kubectl_bin` in `scripts/lib/common.sh`: an old client silently
drops manifest fields).

```bash
CTX=gke_saga-agents-staging_us-central1_swarm-autopilot

# Does the cluster serve the kind? No output means it does not, and neither
# policy is applied there.
kubectl --context "$CTX" api-resources --api-group=networking.gke.io -o name \
  | grep -x fqdnnetworkpolicies.networking.gke.io

kubectl --context "$CTX" -n swarm-system get networkpolicy,fqdnnetworkpolicy | grep swarm-typesafe-fwd
kubectl --context "$CTX" get fqdnnetworkpolicy -A | grep swarm-allow-typesafe-direct
```

What the forwarder's policy changes: `swarm-typesafe-fwd`'s pods go from
unrestricted egress to DNS (the kube-dns pods, and `169.254.20.10` on 53), the
metadata server (`169.254.169.254` on 80 and 8080) and TCP 443 to
`api.typesafe.ai` and `secretmanager.googleapis.com`. Nothing else in
`swarm-system` is selected.

What the tenant policy changes today: nothing a packet notices (rule 4 above).
It names the host for pods labelled `swarm-jev: typesafe-direct`, and is the
rule that keeps them working on the day rule 4 stops covering
`api.typesafe.ai`.

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

Turn `gke_enable_fqdn_network_policy` off only with a cluster rebuild planned
(turning it off replaces the cluster). To take the policies out of one cluster
without that, delete them; the next release's apply puts them back while the
cluster serves the kind, so a lasting roll-back is a change to
`terraform/infra/typesafe_fwd.tf` or `kubernetes/render.py`, reviewed.

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
