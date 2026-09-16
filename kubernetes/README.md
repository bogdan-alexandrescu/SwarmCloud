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
| `service-accounts/worker-serviceaccount.yaml` | Workload Identity accounts, no mounted API token |
| `rbac/worker-rbac.yaml` | A Role with no rules, and the binding |
| `network-policies/default-deny.yaml` | Deny ingress and egress, for every pod |
| `network-policies/allow-egress.yaml` | DNS, the metadata server, Google APIs, the internet minus the cluster |
| `policies/pod-security.yaml` | Cluster-scoped ValidatingAdmissionPolicies |
| `worker-templates/worker-job.yaml` | The canonical worker Job |
| `worker-templates/worker-job-browser.yaml` | The same, plus shared memory for Chromium |
| `render.py` | Fills in the placeholders; sizing comes from the frozen catalogue |
| `apply.sh` | Renders, checks it is pointed at the right cluster, applies |

Every file is held to its invariants by `tests/unit/worker/test_kubernetes_manifests.py`,
which renders them with the real renderer and asserts the properties that matter
— including the ones about absence, which is where YAML quietly rots.

## Using it

```bash
# Look at what would be created.
kubernetes/render.py tenant --tenant eng

# Dry run against the cluster, then apply.
kubernetes/apply.sh --tenant eng
kubernetes/apply.sh --tenant eng --confirm

# The cluster-scoped admission policies, once per cluster.
kubernetes/apply.sh --policies --confirm

# One Job, for an incident repro.
kubernetes/render.py job --tenant eng --profile browser \
    --task task_9f3a --attempt att_7b21 --lease lease_c4 --generation 3
```

`apply.sh` refuses to run against `agents-staging`, `agents-prod` or the EKS
cluster in this kubeconfig, and requires the target cluster's name to start with
`swarm-` — the same rule terraform enforces on `gke_autopilot.cluster_name`.
`agents-staging` in particular is a live GKE Standard cluster owned by another
team **in this same project**, and it is the current context on a freshly
configured workstation.

It also resolves kubectl explicitly rather than trusting `PATH`: an EKS kubectl
1.22 shadows the current one on the reference workstation, and 1.22 does not
understand `ValidatingAdmissionPolicy` — it would report success having applied
nothing.

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
broken". So egress is `0.0.0.0/0` with the pod range, the service range,
RFC1918, link-local and CGNAT excepted. The service range has to be listed
explicitly: GKE Autopilot's default, `34.118.224.0/20`, is public address space
that no RFC1918 entry covers.

**Two service accounts, deliberately.** See the conflicts below.

**A Role with `rules: []`.** A worker needs nothing from the Kubernetes API: its
state is in Firestore, its artifacts are in GCS, and its credentials arrive as
environment variables projected from the tenant's own Secret Manager secret. The
empty Role makes that absence greppable; `automountServiceAccountToken: false`
makes it true even if someone fills the Role in.

## Open cross-track conflicts

These are divergences between this directory and code owned by other tracks.
Each one is worked around here rather than papered over, and each needs a change
outside `kubernetes/` to close properly.

### 1. The dispatcher overrides the container command (severity: critical)

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

### 4. `automountServiceAccountToken: true` in the dispatcher's pod spec

The dispatcher's GKE manifest sets it to `true`, which overrides the `false` on
the ServiceAccount. Nothing in the worker calls the Kubernetes API, so the token
is a credential with no use to the pod and real use to anything that can read its
filesystem — in a container whose job is to read files and follow instructions
found in them.

Until it changes, the namespace enforces PSA `baseline` while auditing and
warning at `restricted`, and `swarm-worker-hardening-advisories` records every
non-compliant pod in the audit log. Closing it is two edits: the dispatcher sets
`automountServiceAccountToken: false` and a container `securityContext`
matching the Job template, then
`render.py tenant --pss-enforce restricted` and the advisory binding flips to
`["Deny"]`.

### 5. Two service account names, two GSA naming schemes

`scripts/register-tenant.sh` creates the KSA `swarm-worker` and the GSA
`swarm-t-<tenant>`; `dispatch.py` asks for the KSA `swarm-<tenant>`;
`terraform/modules/tenancy` creates the GSA `swarm-agent-worker-<tenant>`. A pod
naming a service account that does not exist stays Pending until its deadline
expires, which reads as a scheduling problem rather than a missing object.

Worked around by creating both KSA names here, both annotated for Workload
Identity, and by making `--gsa` an explicit flag on `render.py` whose default is
the terraform name. Three names for two objects should become one each.

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
