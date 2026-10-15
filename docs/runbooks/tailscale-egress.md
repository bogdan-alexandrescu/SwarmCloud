# The decision-api tailnet egress: set-up, verification, rotation and rollback

Jev's two internal judgment backends, "Internal Jev" (the default) and
"Internal Laya", call Saga's decision API, which only exists on the company
Tailscale tailnet:

    https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide
    https://decision-api-staging.taile0bda9.ts.net/laya/v1/decide
    https://decision-api-staging.taile0bda9.ts.net/v1/decide        (the root answers too)

Neither the laptop nor `swarm-autopilot` is on the tailnet. On 2026-10-14 the
owner chose **Tailscale egress in our cluster**: one egress Service in
`swarm-system`, logging in with a Tailscale OAuth client tagged
`tag:swarmcloud`, whose secret is stored in Secret Manager as
`swarm-tailscale-oauth`. "TypeSafe Jev" goes to `api.typesafe.ai` over the
internet and needs none of this.

What is built (lane JUDGE-NET):

| Piece | Where | What it does |
|---|---|---|
| Secret container | `terraform/infra/tailscale.tf` | `swarm-tailscale-oauth`, no version. One reader: the deployer that runs the sync, and only while `enable_tailscale_egress` is on. |
| The switch | `enable_tailscale_egress` (default `false`) | Off: nobody reads the secret, nothing is rendered, `apply.sh` applies nothing about the tailnet. |
| The egress | `kubernetes/swarm-system/tailscale-egress.yaml` | Service `decision-api` (443, fixed clusterIP `10.48.6.120`, stated once as `local.decision_api_cluster_ip` in `terraform/infra/tailscale.tf`) in front of ONE unprivileged pod: Tailscale in userspace mode, plus a forwarder that dials `decision-api-staging.taile0bda9.ts.net:443` and nothing else. |
| Who reaches it | the manifest's `decision-api-egress-ingress`, and `kubernetes/network-policies/allow-judge-egress.yaml` in every tenant namespace | TCP 8443, only from a pod labelled `swarm-judge: internal` in a tenant namespace. |
| The sync | `kubernetes/apply.sh --swarm-system --confirm` | Copies the secret from Secret Manager into `swarm-system/operator-oauth`, never printing it. |
| Storing the secret | `scripts/create-secrets.sh --tailscale-oauth --stdin` | Stdin only; refuses anything that is not an OAuth client secret (`tskey-client-…`). |

## Why not the Tailscale Kubernetes operator

The owner asked for the operator. Read at v1.104.1, the operator **cannot run
an egress proxy on GKE Autopilot**, and that is a property of the operator,
not of our cluster:

* an egress Service (`tailscale.com/tailnet-fqdn`) makes the operator build its
  proxy from `cmd/k8s-operator/deploy/manifests/proxy.yaml`: a **privileged**
  `sysctler` init container that turns on `ip_forward`, and a **privileged**
  tailscale container that DNATs with iptables. The unprivileged userspace
  template is used only for Ingress serve proxies (`sts.go`, `reconcileSTS`);
* forcing userspace through a ProxyClass (`TS_USERSPACE=true`) does not work
  either: containerboot refuses the combination outright, *"TS_TAILNET_TARGET_FQDN
  is not supported with TS_USERSPACE"* (`cmd/containerboot/settings.go`);
* Autopilot admits no privileged container. `--workload-policies=allow-net-admin`
  admits NET_ADMIN, but the privileged init container is still refused. And
  `swarm-system` enforces the `restricted` Pod Security Standard;
* Tailscale's own documentation lists GKE Autopilot as unsupported for the
  operator for this reason.

So the same egress Service is built from the same image the operator would have
run (`ghcr.io/tailscale/tailscale` v1.104.1, pinned by digest), without the
operator. The pod runs **userspace networking**
(`tailscaled --tun=userspace-networking`), so it needs no TUN device, no
iptables, no sysctl and no capability. It joins the tailnet as an **ephemeral**
node tagged `tag:swarmcloud`, and keeps its state in memory, so it needs no
Kubernetes API access and no RBAC. A restart joins as a fresh ephemeral node,
and the tailnet removes the old one by itself. For each connection, the forwarder
(`busybox nc -lk`) runs `tailscale nc decision-api-staging.taile0bda9.ts.net 443`
against tailscaled's local socket. The destination is fixed on the command line:
a caller cannot pick a different host or port.

The Kubernetes Secret keeps the operator's name, `operator-oauth`, so a later
move to the operator (on a Standard node pool, say) can reuse it.

## Nothing else on the tailnet

Three independent limits. Any one of them alone keeps the cluster to the one
destination:

1. the forwarder dials exactly `decision-api-staging.taile0bda9.ts.net:443`;
2. the tailnet policy file grants `tag:swarmcloud` that host on `tcp:443` and
   nothing else, and grants nothing on the tailnet a path *to* `tag:swarmcloud`
   (step 2 below);
3. NetworkPolicy: only a pod the dispatcher labelled `swarm-judge: internal`, in
   a tenant namespace, opens TCP 8443 to the forwarder. Every other pod gets
   nothing. `swarm-system-default-deny-ingress` closes every other source.

TLS runs end to end, between the caller and the decision API. Nothing in the
cluster terminates it, holds a certificate or sees a request. Because the
certificate is issued for the tailnet name, a caller connects to the in-cluster
Service but verifies, and sends SNI and `Host` for, the tailnet name. curl
spells that `--connect-to` (see "Verify" below).

## Set-up, in order

### 1. Terraform with the switch off

`enable_tailscale_egress` defaults to `false`. A normal `make apply` creates the
`swarm-tailscale-oauth` container. It has no version and no reader.

### 2. The tailnet admin: the OAuth client and the ACL

In the Tailscale admin console:

1. **Tag owner.** In the policy file, make sure `tag:swarmcloud` exists and an
   admin owns it:

   ```jsonc
   "tagOwners": {
     "tag:swarmcloud": ["autogroup:admin"],
   },
   ```

2. **The grant, and nothing more.** Allow `tag:swarmcloud` to reach the decision
   API on `tcp:443` and nothing else. Name the destination however your policy
   already names that device: its own tag, or a `hosts` alias for its tailnet
   address (read the address from the admin console's Machines page):

   ```jsonc
   "hosts": {
     "decision-api-staging": "100.x.y.z",   // the device's tailnet IPv4
   },
   "grants": [
     // SwarmCloud's cluster egress: the decision API, HTTPS, nothing else.
     { "src": ["tag:swarmcloud"], "dst": ["decision-api-staging"], "ip": ["tcp:443"] },
   ],
   ```

   **Check that no broader rule already covers it.** A grant or ACL whose `src`
   is `*`, or whose `dst` is `*` or `tag:swarmcloud`, gives the cluster's node
   (or gives others a path to it) more than this. That includes the default
   "allow all" policy of a new tailnet. Use the policy file's **Preview rules**
   for `tag:swarmcloud` to confirm it can reach only `decision-api-staging:443`
   and that nothing can reach it.

3. **The OAuth client.** Settings → OAuth clients → *Generate OAuth client*:
   scope **Auth Keys: Write**, tag **`tag:swarmcloud`**, and nothing else. Copy
   the **client secret** (`tskey-client-…`). It is shown once. The client ID
   is not needed: the secret carries it.

### 3. The owner stores the secret

Paste it into the script, which reads stdin only and never echoes the value:

```bash
scripts/create-secrets.sh --tailscale-oauth --stdin
```

It refuses anything that does not start `tskey-client-` (an auth key, an API
token, the client ID), and it never creates the secret or grants anyone read on
it. Terraform owns both. Do not paste the secret into a file, a tfvars file,
an environment variable, an issue or a chat. Secret Manager is the only place it
lives.

### 4. Terraform with the switch on

In `terraform/environments/<env>/<env>.tfvars`:

```hcl
enable_tailscale_egress = true
```

`make plan`, then `make apply`. This adds the deployer
(`var.deployer_service_account`) as the secret's one reader and renders
`tailscale_egress_manifest`. `terraform -chdir=terraform/infra output
tailscale_egress` shows the names. It never shows a value.

### 5. The workspace deployer's scope, then swarm-system

The tenant policy `swarm-allow-judge-egress` is new. Admit it to the workspace
deployer's scope before a personal workspace is next onboarded:

```bash
kubernetes/apply.sh --policies            # preview
kubernetes/apply.sh --policies --confirm
```

Then apply swarm-system, which now carries the tailnet egress after the relay
and swarm-egress, and syncs the secret:

```bash
kubernetes/apply.sh --swarm-system            # preview: names the sync, does not read the secret
kubernetes/apply.sh --swarm-system --confirm
```

`--confirm` applies the manifest and then copies `swarm-tailscale-oauth` into
`swarm-system/operator-oauth`. It reads the value with gcloud into a 0600 file,
builds the Secret with `kubectl create --dry-run=client`, applies it, and
removes the file. It prints `secret/operator-oauth configured` and never the
value. Like every `apply.sh` mode, it refuses `agents-staging` (the deny-list in
`scripts/lib/common.sh`) before it reads anything. The release's
"roll the live relay" step runs the same apply, so later releases keep the
Secret in step with Secret Manager.

The tenant side, `swarm-allow-judge-egress`, reaches every tenant namespace with
the release's tenant NetworkPolicy step. To apply it to every tenant namespace
now, use [tenant-network-policies.md](tenant-network-policies.md).

### 6. The dispatcher's label, host alias and URL

Only a pod labelled `swarm-judge: internal` reaches the forwarder. The
dispatcher sets that label on a live-browser or agent pod whose Jev judgment
backend is "Internal Jev" or "Internal Laya", never on a TypeSafe-backed pod,
and never because a caller asked (invariant 10). Until the dispatcher sets it,
the egress is up and nothing can use it.

With the label, and only with it, the dispatcher gives the pod

    hostAliases: [{ip: 10.48.6.120, hostnames: [decision-api-staging.taile0bda9.ts.net]}]
    JEV_API_URL=https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide   (or /laya/)

so the pod calls the decision API **by its tailnet name**, the name resolves to
the `decision-api` Service, and TLS verifies the API's real certificate end to
end (owner decision 2026-10-15, #1380). The in-cluster name does not work for
either scheme: `http://decision-api.swarm-system.svc.cluster.local` times out,
because the Service has no port 80, and `https://` on it fails TLS, because the
certificate is issued for the tailnet name. That is why the Service's
clusterIP is FIXED: every judging pod has it in `/etc/hosts`, so a Service
recreated with a new address would strand them. Change it only together with
`DECISION_API_CLUSTER_IP` in `apps/scheduler/scheduler/judgment.py` (a unit
test holds the two equal), and only to a free address inside the cluster's
`services_cidr` (Terraform refuses one outside it).

The scheduler only does this while `INTERNAL_DECISION_API_URL` is set, i.e.
while `internal_decision_api_enabled` is on. That switch is flipped in its own
change, after the third probe under Verify passes.

## Verify

The Service and the pod:

```bash
kubectl --context gke_saga-agents-staging_us-central1_swarm-autopilot \
  -n swarm-system get svc decision-api
kubectl --context gke_saga-agents-staging_us-central1_swarm-autopilot \
  -n swarm-system get pods -l app.kubernetes.io/name=decision-api-egress
```

Expect `decision-api ClusterIP 10.48.6.120 … 443/TCP`, and one pod `2/2 Running`. The pod
is Ready only once its node has a tailnet address (containerboot's `/healthz`).
A pod stuck in `ContainerCreating` with `secret "operator-oauth" not found` has
not been synced: run step 5's `--confirm`. The tailnet's Machines page shows an
ephemeral `swarmcloud-decision-api` node tagged `tag:swarmcloud`.

**A labelled pod reaches the decision API.** From a throwaway pod in a tenant
namespace, carrying the label and the same `hostAliases` entry the dispatcher
gives a judging pod, under the `restricted` standard. A tenant namespace admits
it only as written: `requests == limits` at the LimitRange's minimum of
`250m` / `512Mi`; `automountServiceAccountToken: false`, which
`swarm-worker-pod-hardening` requires; and an explicit `ephemeral-storage: 1Gi`,
because the namespace's default of 20Gi is over Autopilot's 10Gi cap. Drop any
of the three and the pod is refused.

```bash
CTX=gke_saga-agents-staging_us-central1_swarm-autopilot
NS=swarm-tenant-eng
OVERRIDES='{"spec":{"automountServiceAccountToken":false,
  "hostAliases":[{"ip":"10.48.6.120","hostnames":["decision-api-staging.taile0bda9.ts.net"]}],
  "securityContext":{"runAsNonRoot":true,"runAsUser":65532,"seccompProfile":{"type":"RuntimeDefault"}},
  "containers":[{"name":"judge-probe","image":"curlimages/curl:8.11.1","command":["sleep","300"],
  "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},
  "resources":{"requests":{"cpu":"250m","memory":"512Mi","ephemeral-storage":"1Gi"},
               "limits":{"cpu":"250m","memory":"512Mi","ephemeral-storage":"1Gi"}}}]}}'

kubectl --context "$CTX" -n "$NS" run judge-probe --restart=Never \
  --labels=swarm-judge=internal --image=curlimages/curl:8.11.1 --overrides="$OVERRIDES"
kubectl --context "$CTX" -n "$NS" wait --for=condition=Ready pod/judge-probe --timeout=120s

kubectl --context "$CTX" -n "$NS" exec judge-probe -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 \
    --connect-to decision-api-staging.taile0bda9.ts.net:443:decision-api.swarm-system.svc.cluster.local:443 \
    -H 'content-type: application/json' \
    -d '{"state":"connectivity probe","questions":{}}' \
    https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide
```

Any HTTP status, a 4xx for the empty question set included, proves the path:
the TCP connection went from the pod to the forwarder to the tailnet, and the
TLS handshake verified the decision API's certificate. A timeout or `000` does
not.

**The pod's JEV_API_URL works as the dispatcher sends it.** The same call with
no `--connect-to`: the name resolves through the pod's `hostAliases` entry, as
it does in a live-browser pod judged internal. This is the probe that must pass
before `internal_decision_api_enabled` is flipped:

```bash
kubectl --context "$CTX" -n "$NS" exec judge-probe -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 \
    -H 'content-type: application/json' \
    -d '{"state":"connectivity probe","questions":{}}' \
    https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide
# expect: an HTTP status (400 for this body on 2026-10-15), never 000
```

**An unlabelled pod does not.** This is the control that shows the first result
could have come out the other way:

```bash
kubectl --context "$CTX" -n "$NS" label pod judge-probe swarm-judge-
kubectl --context "$CTX" -n "$NS" exec judge-probe -- \
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 \
    --connect-to decision-api-staging.taile0bda9.ts.net:443:decision-api.swarm-system.svc.cluster.local:443 \
    https://decision-api-staging.taile0bda9.ts.net/
# expect: a timeout, http_code 000

kubectl --context "$CTX" -n "$NS" delete pod judge-probe
```

## Rotation

1. Generate a new OAuth client secret in Tailscale (same client, or a new one
   with the same scope and tag).
2. `scripts/create-secrets.sh --tailscale-oauth --stdin`
3. `kubernetes/apply.sh --swarm-system --confirm` (or wait for the next
   release).
4. `kubectl -n swarm-system rollout restart deployment/decision-api-egress`.
   The running node keeps its login until it restarts.
5. Revoke the old secret in Tailscale, then disable its Secret Manager version
   by hand: `gcloud secrets versions disable <N> --secret swarm-tailscale-oauth`.

## Rollback

1. Set `enable_tailscale_egress = false` and apply. The deployer loses read on
   the secret, the manifest output becomes null, and `apply.sh --swarm-system`
   applies nothing about the tailnet from then on. It does **not** delete what
   is already running.
2. Remove the running objects, through the swarm's own context only (never
   `agents-staging`):

   ```bash
   kubectl --context gke_saga-agents-staging_us-central1_swarm-autopilot -n swarm-system \
     delete deployment/decision-api-egress service/decision-api \
     networkpolicy/decision-api-egress-ingress serviceaccount/decision-api-egress \
     secret/operator-oauth
   ```

   The ephemeral node leaves the tailnet when its pod goes.
3. In Tailscale, revoke the OAuth client and remove the `tag:swarmcloud` grant.
4. `swarm-allow-judge-egress` can stay in the tenant namespaces. With no
   forwarder pod it opens a path to nothing.

## Not measured

* The forwarder's resource sizes and concurrency. Every open connection is one
  `tailscale nc` process (about 20 MiB), and 512 MiB is room for about twenty
  judgments in flight. Read the pod's metrics once Jev runs through it.
* The pod's own egress (to Tailscale's control plane, DERP relays and direct
  UDP paths) is unrestricted, as all of swarm-system's egress is today. Pinning
  it is a separate change, made after measuring which endpoints the node uses.
