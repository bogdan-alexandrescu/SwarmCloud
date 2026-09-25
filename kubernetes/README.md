# `kubernetes/` — the GKE Autopilot execution path

Cloud Run Jobs is the primary backend and holds everything it can. This
directory covers what it cannot: the `browser` profile, which needs a
`/dev/shm` Cloud Run will not let us size, and any future profile above the
Cloud Run ceiling of 8 vCPU / 32 GiB.

That makes this the one path where a tenant's agent shares a kernel and a
network with another tenant's agent, so it is the path where every boundary has
to be written down rather than assumed.

## Layout

| Path | What it is |
| --- | --- |
| `namespaces/tenant-namespace.yaml` | Namespace, ResourceQuota, LimitRange |
| `service-accounts/worker-serviceaccount.yaml` | The worker's Workload Identity account and a disarmed `default`, no mounted API token |
| `service-accounts/legacy-worker-serviceaccount.yaml` | The older `swarm-worker` account and its binding — rendered only where IAM binds it |
| `rbac/worker-rbac.yaml` | A Role with no rules, and the binding |
| `network-policies/default-deny.yaml` | Deny ingress and egress, for every pod |
| `network-policies/allow-egress.yaml` | DNS (NodeLocal DNSCache and kube-dns), the metadata server, Google APIs, the internet minus the cluster |
| `policies/pod-security.yaml` | Cluster-scoped ValidatingAdmissionPolicies |
| `worker-templates/worker-job.yaml` | The canonical worker Job |
| `worker-templates/worker-job-browser.yaml` | The same, plus shared memory for Chromium |
| `render.py` | Fills in the placeholders; sizing comes from the frozen catalogue, the cluster's network from its inputs |
| `apply.sh` | Checks it is pointed at the right cluster, reads that cluster's network and the tenant GSA's Workload Identity bindings, renders, applies |
| `cluster-network.sh` | Reads the pod range, service range, kube-dns IP and NodeLocal DNSCache address from the live cluster (sourced) |
| `network_parity.py` | Compares an applied egress policy with the live network; run by `scripts/lib/check-cluster-network-parity.sh` |

Every file is held to its invariants by `tests/unit/worker/test_kubernetes_manifests.py`,
which renders them with the real renderer and asserts the properties that matter
— including the ones about absence, which is where YAML quietly rots.

## Using it

```bash
# Look at what would be created. With no network inputs the egress policy
# carries documentation addresses and is marked offline-render-not-for-apply;
# the dry run below shows it with the cluster's real values.
kubernetes/render.py tenant --tenant eng

# Dry run against the cluster (reads its network, prints a diff), then apply.
kubernetes/apply.sh --tenant eng
kubernetes/apply.sh --tenant eng --confirm

# Afterwards: every applied egress policy against the live cluster's network.
scripts/lib/check-cluster-network-parity.sh --require-live

# The cluster-scoped admission policies, once per cluster.
kubernetes/apply.sh --policies --confirm

# One Job, for an incident repro.
kubernetes/render.py job --tenant eng --profile browser \
    --task task_9f3a --attempt att_7b21 --lease lease_c4 --generation 3
```

`apply.sh` refuses three ways, and each catches what the others cannot:

* the **cluster** the context names — in its label, in the kubeconfig cluster
  entry it resolves to, and in `--cluster` — must not be on `SHARED_DENY_LIST`.
  That list lives **once**, in `scripts/lib/common.sh`, and `apply.sh` sources it
  rather than restating it, so a resource added there is protected here on the
  same commit. `agents-staging` in particular is a live GKE Standard cluster owned
  by another team **in this same project**, and it is the current context on a
  freshly configured workstation. The cluster is the last segment of
  `gke_<project>_<location>_<cluster>`; the whole string used to be matched, and
  because our project id `saga-agents-staging` contains `agents-staging`, gcloud's
  own name for our cluster (`gke_saga-agents-staging_us-central1_swarm-autopilot`)
  was refused. Both it and the `swarm-dev` label `configure-kubectl.sh` writes are
  accepted now; `gke_saga-agents-staging_us-central1-a_agents-staging` is still
  refused, including behind a label that looks like ours;
* the context must not look like EKS. The EKS cluster's **name appears nowhere in
  this repository**, so this check matches its shape instead — an ARN, or a host
  under `eks.amazonaws.com`. An earlier version carried a literal
  `eks-cluster-name` in its deny list, which matches no real context and made the
  EKS case read as handled while handling nothing;
* the cluster must start with `swarm-` — the same rule terraform enforces on
  `gke_autopilot.cluster_name` — and the context must point at **exactly** it, not
  at a cluster whose name merely contains it: the network `apply.sh` renders is
  read from that cluster by name. This is also what refuses the other team's
  `gke_saga-agents-prod_us-central1_agents-prod`, which is not in this project and
  so not on the shared deny-list.

`apply.sh` also reads the tenant GSA's IAM policy, and passes `render.py` one
`--bound-ksa` per Kubernetes service account it binds in the namespace (a
`roles/iam.workloadIdentityUser` member `<project>.svc.id.goog[<namespace>/<ksa>]`).
The older `swarm-worker` account is rendered only when it is among them — see
§5 below. It refuses `--bound-ksa` on its own command line, and it names the KSA
the dispatcher's pods run as when that one is unbound.

It also resolves kubectl through `common.sh`'s `kubectl_bin` rather than trusting
`PATH`: an EKS kubectl 1.22 shadows the current one on the reference workstation,
and 1.22 does not understand `ValidatingAdmissionPolicy` — it would report success
having applied nothing.

## Why the manifests look the way they do

**Egress selects every pod, not labelled ones.** The GKE manifest in
`apps/scheduler/scheduler/dispatch.py` labels its pods `swarm-task` and
`swarm-tenant` and nothing else. An allow-egress policy keyed on any other label
would match no pod, leave the default deny in force, and break every browser
task with a DNS timeout that looks like a cluster problem. The ResourceQuota
forbids Services, Deployments, StatefulSets, DaemonSets, CronJobs and PVCs, so
"every pod in this namespace" and "every worker" are the same set by
construction.

**The internet is allowed; the cluster is carved out of it.** NetworkPolicy
matches IPs, not names, and the provider APIs sit behind CDN ranges that change
without notice — a pinned allow-list fails as "every task on this tenant is
broken". So egress is `0.0.0.0/0` with all of RFC1918, link-local and CGNAT
excepted **unconditionally**, and the cluster's own pod and service ranges
excepted **as read from the cluster**. The ranges are listed even when private
space already covers them, because a GKE range need not be private: Autopilot's
*default* service range, `34.118.224.0/20`, is public space no RFC1918 entry
covers. swarm-autopilot does not use that default — its ranges are
`10.44.0.0/14` (pods) and `10.48.0.0/20` (services), measured 2026-09-24 — and
until that date the policy was rendered with the default anyway, because the
renderer defaulted it and nothing passed the real one. On Dataplane V2 the pod
entry is belt and braces: GKE documents that pod traffic is never covered by an
`ipBlock` at all, so the default deny is what stops pod-to-pod.

**The cluster's network is read, never typed.** Four values in the egress policy
belong to the cluster: the pod range, the service range, the kube-dns Service IP
and the NodeLocal DNSCache address. `apply.sh` reads them through
`cluster-network.sh` — the ranges from `gcloud container clusters describe`
(`.clusterIpv4Cidr`, `.servicesIpv4Cidr`), the two DNS addresses from
`kube-system` (the `kube-dns` Service's `clusterIP`, and the first `-localip` of
the `node-local-dns` DaemonSet), because `describe` contains neither address —
and refuses them as arguments, in full or abbreviated (`--pod-cid` is refused
as `--pod-cidr`; `render.py` also turns argparse's prefix expansion off, which
is what let an abbreviation forwarded after the read values replace them).
`render.py` takes them as inputs with no defaults; given none, it renders RFC 5737 documentation addresses, marks the
policy `offline-render-not-for-apply`, and `apply.sh` refuses to apply that.
The policy's `swarm.saga.xyz/*` annotations record what it was rendered for, and
`scripts/lib/check-cluster-network-parity.sh` compares every applied copy — the
annotations and the enforced spec — with the live cluster. In CI, with no
credentials, it skips with a `::notice` rather than passing on nothing.

**DNS goes to NodeLocal DNSCache, not to the kube-dns pods.** swarm-autopilot
runs Dataplane V2, Cloud DNS (cluster scope) and NodeLocal DNSCache, which
Autopilot turns on and does not let you turn off. A pod's nameserver is the
hostNetwork `node-local-dns` agent, which listens on `169.254.20.10` *and* on the
kube-dns Service IP (`10.48.0.10`) and forwards to Cloud DNS — never to the
kube-dns pods. The policy used to allow DNS only to those pods, and anetd drops
a denied packet without answering, so on 2026-09-24 a browser worker ran 390 s
with every lookup timing out and printed nothing. The kube-dns podSelector rule
is kept (it is right on a cluster that serves DNS from those pods); rule 1b adds
an `ipBlock` to both addresses on 53/UDP and 53/TCP. **That rule is not yet
proven to match**: GKE documents that on Dataplane V2 an `ipBlock` cannot
select traffic to a hostNetwork Pod, and node-local-dns is one. A probe pod in
the tenant namespace must resolve names under the applied policy before a
worker relies on it.

**The metadata server is `169.254.169.254` on TCP 80 and 8080**, the ports
GKE's network-policy doc gives for Dataplane V2. Port 988 on that address is
gone: this cluster's metadata server listens on `169.254.169.252:988`, which GKE
lists only for clusters without Dataplane V2, so `.254:988` matched nothing.

**Two service accounts, deliberately.** See the conflicts below.

**No provider key in a Job's environment, not even for the profiles that need
one.** Every process in the container runs as uid 10001, so a variable in the
worker's environment is readable at `/proc/1/environ` by the runner child, by
anything the agent spawns, and by a `cat`. A `secretKeyRef` entry would hand the
tenant's key to all of them, and the env allowlist in `workspace.child_env` cannot
take back what PID 1 was started with. The worker reads the key from Secret
Manager itself, as the tenant's own GSA through Workload Identity, and puts it
only into the environment of the one child that needs it. That also removes a
dependency nothing satisfied: `secretKeyRef` names a **Kubernetes** Secret and
nothing in this repository creates one — tenant keys live in Secret Manager as
`swarm-tenant-<tenant>-<provider>`.

**A Role with no rules.** A worker needs nothing from the Kubernetes API: its
state is in Firestore, its artifacts are in GCS, and its credentials arrive as
environment variables projected from the tenant's own Secret Manager secret. The
empty Role makes that absence greppable; `automountServiceAccountToken: false`
makes it true even if someone fills the Role in.

It is spelled by **omitting** `rules`, not as `rules: []`, and
`swarm-deny-cross-tenant-ingress` omits `ingress` the same way. Nothing `apply.sh`
sends may carry an empty list: the API server stores one as null or absent,
client-side apply patches from that live object to the manifest, and so every run
re-sent `[]` and reported the object `configured`. Measured 2026-09-25 on
`swarm-tenant-eng`: the Role's resourceVersion was still its creation's (the
"configured" wrote nothing, and `kubectl diff` showed nothing), while the
NetworkPolicy — whose registry compares specs with `reflect.DeepEqual`, where
nil ≠ `[]` — was written on every apply and stood at generation 4 against 1 for
`swarm-default-deny`; its `generation: 4 → 5` was the unexplained second
NetworkPolicy in every dry-run diff. `test_nothing_apply_sends_carries_an_empty_list_the_api_server_drops`
holds every render to it. The first apply after the change still reports both
objects `configured` once, because it rewrites their last-applied annotation —
a change `kubectl diff` does not display, so that one run's dry-run diff will not
list them. (Measured: `kubectl diff` of the new render against swarm-tenant-eng
shows only the `swarm-worker` RoleBinding losing its `swarm-worker` subject and
`swarm-agent-worker` losing its `alias-of` annotation.)

## What this directory does NOT cover

Worth stating plainly, because the care taken over these files invites the
assumption that they protect the platform: **they protect the GKE path only, and
the GKE path carries the least traffic.** Per `swarm_common.profiles`, `mock`,
`generic`, `claude-code` and `codex` are all pinned to `Backend.CLOUD_RUN_JOB`;
only `browser` (and anything above the Cloud Run 8 vCPU / 32 GiB ceiling) reaches
Autopilot.

So for the profiles a caller actually uses, none of the following applies: the
default-deny NetworkPolicy, the cross-tenant ingress denial, the per-tenant
ResourceQuota and LimitRange, or the ValidatingAdmissionPolicies. Cloud Run worker
jobs run with `vpc_access { egress = "ALL_TRAFFIC" }` into the shared VPC
(`terraform/modules/cloud_run_jobs`), where the equivalent controls are VPC
firewall rules and network tags rather than anything in this directory. Two
consequences are worth being explicit about:

* the ResourceQuota is **not** a platform-wide ceiling. On the Cloud Run path the
  Firestore slot-pool accounting is the only thing bounding a tenant, so the
  "second line of defence for when that accounting is wrong" does not exist where
  most work runs;
* `default-deny.yaml`'s own rationale — one tenant's agent opening a socket to
  another's, which no IAM scoping stops because none of it goes through a Google
  API — describes a risk that is mitigated here and must be mitigated separately
  in `terraform/` for Cloud Run.

Closing this needs a change outside `kubernetes/`, which is why it is recorded
here rather than worked around.

## Open cross-track conflicts

These are divergences between this directory and code owned by other tracks.
Each one is worked around here rather than papered over, and each needs a change
outside `kubernetes/` to close properly.

### 1. The dispatcher overrides the container command (severity: critical)

**Closed 2026-09-24.** Neither dispatcher sets a container `command` or `args`
(incident `wf_ebb3ab2d65664707a559`, PR #31), and the field is no longer called
`command`: it is `RunnerProfile.runner_argv`, the argv the worker lifecycle
starts as its child (contract request 18, accepted by the owner, PR #44). The
text below is the finding as it was recorded.

`apps/scheduler/scheduler/dispatch.py` sets `command=list(profile.command)` on
both the Cloud Run container and the GKE pod template. `profile.command` is the
**runner's** entrypoint — `python -m agent_worker.runners.browser` — and the
image's own ENTRYPOINT is `tini -- python -m agent_worker`, the **worker**.

Overriding it starts the runner directly, with no worker around it: no fencing
generation check, no workspace isolation, no checkpoint restore, no periodic
checkpoint, no heartbeat, no lease release. That is every safety property in the
platform switched off at once, and it fails silently — the agent runs and
produces output.

The fix is to delete the `command` field from both manifests and let the image's
ENTRYPOINT run. `worker-templates/worker-job.yaml` shows the correct shape, and
`test_a_rendered_job_never_overrides_the_container_command` asserts it.

### 2. `managed-by` label values disagree (severity: critical, worked around)

The dispatcher stamps `managed-by: swarm-scheduler`; the reconciler's backend
listing originally filtered on `managed-by=swarm`. The consequence was not a
cosmetic mismatch: the reconciler would list zero executions, conclude that
every running task had nothing behind it, release its slot and let the scheduler
start a second agent on the same task — the exact duplicate execution the
service exists to prevent.

Fixed on this side, since being blind is far worse than seeing too much:
`reconciler/backends.py` now accepts the whole `managed-by=swarm*` family for
**reads** and a narrower runtime-marker set for **deletes**. Covered by
`tests/unit/worker/test_backend_identity.py`. Worth settling on one vocabulary
across the platform anyway.

### 3. Identifiers live in labels that have been sanitised (worked around)

A label value cannot contain an underscore, so `task_9f3a` becomes `task-9f3a`
and matches no Firestore document. The GKE manifest also carries no attempt id
or generation label at all — both exist only in the container environment.

The reconciler now reads identifiers from the container environment first and
falls back to either label spelling, and `detect.normalise_executions` resolves
a sanitised id back to the real one before any rule runs. Without that, an
execution whose task could not be found is an orphan, and orphans get
terminated.

### 4. Pod hardening — CLOSED

The dispatcher's GKE manifest used to set `automountServiceAccountToken: true`
and no container `securityContext`, so the namespace could only enforce PSA
`baseline` and `swarm-worker-hardening-advisories` could only warn: enforcing
the stronger level would have rejected every browser job the dispatcher created.

`apps/scheduler/scheduler/dispatch.py` now sets `automountServiceAccountToken:
False`, `POD_SECURITY_CONTEXT` and `CONTAINER_SECURITY_CONTEXT`, so both sides
of that trade are gone. On this side:

* `render.py --pss-enforce` defaults to `restricted`;
* `swarm-worker-hardening-advisories` became `swarm-worker-pod-hardening` with
  `failurePolicy: Fail` and `validationActions: ["Deny", "Audit"]`.

That matters more than a tidy-up: the `browser` profile runs Chromium with its
own sandbox deliberately disabled, on the argument that the isolation is the
pod. The argument only holds while the pod controls are enforced.

**Applying this on an existing cluster leaves the superseded policy behind** —
`kubectl apply` does not delete a renamed object. Delete it once:

```bash
kubectl delete validatingadmissionpolicybinding swarm-worker-hardening-advisories
kubectl delete validatingadmissionpolicy        swarm-worker-hardening-advisories
```

`test_the_dispatchers_gke_manifest_satisfies_restricted` checks the dispatcher's
own manifest against the same requirements, so a regression on that side fails in
CI rather than as Pending pods.

### 5. Two KSA names for one service account — CLOSED, and it was worse than this said

This section described the mismatch as `swarm-worker` (created) against
`swarm-<tenant>` (asked for), and recorded it as a cosmetic duplicate worked
around by creating both. It was neither cosmetic nor accurate: `dispatch.py`
had since been changed to ask for **`swarm-agent-worker`**, which is also the
name `terraform/modules/tenancy` issues the Workload Identity binding for — and
which nothing here created. Since `terraform/` has no kubernetes provider,
`service-accounts/worker-serviceaccount.yaml` is the *only* thing that creates
a tenant KSA, so the one name that mattered did not exist.

A pod naming a missing ServiceAccount is admitted and then never scheduled: the
Job controller reports `serviceaccount "swarm-agent-worker" not found` on the
Job's events, no pod appears, and the task holds its lease until the deadline.
That reads as a scheduling or capacity problem — the same disguise the
namespace bug wore, one layer down. It is the failure that would have come next
after the dispatcher RBAC was applied.

`render.py` now renders `__KSA_NAME__` as `DEFAULT_KSA_NAME`
(`swarm-agent-worker`), `register-tenant.sh` binds that name, and
`tests/unit/worker/test_kubernetes_manifests.py` asserts the rendered
ServiceAccount set contains exactly what `GkeJobDispatcher.ksa_for` asks for.

**`swarm-worker` is rendered only where it is bound.** It is **not** bound for
`eng`, which terraform provisioned: measured 2026-09-24 and again 2026-09-25, the
only `roles/iam.workloadIdentityUser` member on `swarm-agent-worker-eng@` is
`[swarm-tenant-eng/swarm-agent-worker]`, and nothing in the namespace names
`swarm-worker`. It used to be rendered for every tenant anyway, so eng carried an
account whose Workload Identity annotation IAM does not honour. `apply.sh` now
reads the GSA's IAM policy and `render.py` includes
`service-accounts/legacy-worker-serviceaccount.yaml` (the account, and a
`swarm-worker-legacy` RoleBinding to the empty worker Role) only where
`swarm-worker` is bound — which is where `register-tenant.sh` provisioned the
tenant. That script now issues its bindings **before** it calls `apply.sh`, so a
tenant it registers has the account from its first apply.

**A leftover to delete by hand, once this is applied.** `kubectl apply` never
deletes an object a manifest stopped declaring (apply.sh does not `--prune`), so
the next `apply.sh --tenant eng --confirm` stops naming `swarm-worker` in the
`swarm-worker` RoleBinding and leaves the ServiceAccount itself in place. Check
nothing names it, then delete it:

```bash
KUBECONFIG=build/kubeconfig-dev.yaml kubectl --context swarm-dev -n swarm-tenant-eng \
  get pods,jobs -o jsonpath='{range .items[*]}{.spec.serviceAccountName}{.spec.template.spec.serviceAccountName}{"\n"}{end}'
KUBECONFIG=build/kubeconfig-dev.yaml kubectl --context swarm-dev -n swarm-tenant-eng \
  delete serviceaccount swarm-worker
```

**An earlier leftover.** `swarm-tenant-eng` also held a ServiceAccount
`swarm-eng` — what `__KSA_NAME__` rendered to before this fix (it was no longer
there on 2026-09-25). No manifest here
declares it any more, and `kubectl apply` never deletes an object a manifest
stopped declaring. Nothing uses it (no pod or Job in the namespace names it);
delete it once:

```bash
kubectl --context gke_saga-agents-staging_us-central1_swarm-autopilot \
  -n swarm-tenant-eng delete serviceaccount swarm-eng
```

Full diagnosis, including why the error said 403: [docs/gke-dispatch-403.md](../docs/gke-dispatch-403.md).
The commands: [docs/runbooks/gke-dispatch-redispatch.md](../docs/runbooks/gke-dispatch-redispatch.md).

The GSA half was already closed: `register-tenant.sh` used to create
`swarm-t-<tenant>`, and now creates `swarm-agent-worker-<tenant>` — the same
identity `terraform/modules/tenancy` creates and the one `render.py --gsa`
defaults to. All three agree, so the flag is for rendering against an identity
neither provisioning path made, not for papering over a mismatch.

### 6. Namespace `managed-by` is `swarm-terraform` but no terraform state holds it

`scripts/register-tenant.sh` creates tenant namespaces with kubectl and relabels
them `managed-by=swarm-terraform` on every run, while `terraform/` has no
kubernetes provider at all. Applying the Cloud Run deletion rule to that marker
would have made the reconciler's namespace collection dead code.

So namespace collection keys on the pair (recognised `managed-by` marker,
`swarm-tenant` label) — see `is_namespace_gc_eligible` — and a namespace is
additionally protected while its tenant is *registered at all*, not merely while
it is busy: a Cloud Run Job resource is recreated by `ensure_job` on the next
dispatch, but nothing recreates a namespace, its service account or its
workload-identity binding.
