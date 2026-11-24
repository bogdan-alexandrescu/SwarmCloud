# The recording-runner spike: measure Docker inside a sandboxed pod

**Who:** an operator with `kubectl` rights to create pods in one tenant
namespace of a **dev** cluster runs it, after the owner has approved the cost
and named the namespace. **When:** once, before contract request 159 is
decided (the owner's decision of 2026-11-24: "the contract request is decided
AFTER the spike"). **Takes:** about an hour, most of it Autopilot provisioning
two 16-core nodes and the workload itself. **Changes:** two pods and one disk
claim in the named namespace, for the length of the run. Nothing else.

This is lane 0 of [the recording-runner design](../design/recording-runner.md)
(§10), for its recommended option C (§5.2): a Docker daemon inside a GKE Sandbox
(gVisor) pod in a tenant namespace. The owner decided on 2026-11-24: option C
behind this spike; a Guaranteed pod with CPU request equal to limit at 16 (and
64 GiB); Docker's image store on a per-pod disk claim.

The kit:

| Path | What it is |
|---|---|
| [`scripts/recording-runner-spike.sh`](../../scripts/recording-runner-spike.sh) | Preflight, apply, cleanup; prints the result table and writes it to a file |
| [`kubernetes/recording-spike/sandboxed-pod.yaml`](../../kubernetes/recording-spike/sandboxed-pod.yaml) | The sandboxed pod: runtime class `gvisor`, `dockerd` as root inside the sandbox, the 50 GiB image-store claim |
| [`kubernetes/recording-spike/control-pod.yaml`](../../kubernetes/recording-spike/control-pod.yaml) | The control: the same image and shape on the normal runtime, every agent pod's security context unchanged |
| [`kubernetes/recording-spike/workload.sh`](../../kubernetes/recording-spike/workload.sh) | The fixed workload both pods run |
| [`tests/unit/scripts/test_recording_runner_spike.py`](../../tests/unit/scripts/test_recording_runner_spike.py) | The script's guards, offline |

**Nothing in this kit has been run against a cluster.** It was written and its
guards tested offline, with a fake `kubectl`.

## What the spike proves, and its pass bars

The three checks are the design's (§5.2 C, risks 1-3). If any fails, option C
is closed and option D (one Compute Engine VM per task) is the answer; contract
request 159 is amended before it is applied (§5.5).

| Check | What it proves | Pass bar |
|---|---|---|
| **1. Admission** | The cluster admits a pod with the runtime class `gvisor`, root and the daemon's capabilities inside the sandbox, a per-pod disk claim, and the Guaranteed 16-core / 64 GiB shape, in a tenant namespace under its own Pod Security level, LimitRange, quota and the platform's admission policies. It also answers §4's open question, Autopilot's per-pod maximum, for this shape. | The server-side dry run admits the pod; the created pod is Ready within 15 minutes; the served pod has `runtimeClassName: gvisor` and QoS class `Guaranteed`. |
| **2. Daemon** | `dockerd` starts inside the sandbox, writes its image store on the claim, and runs a container. docs/BUILD_PROMPT_V2.md says Docker-in-pod does not work under gVisor, with no measurement; this is the measurement. | `docker info` answers within 2 minutes; an image made in the sandbox from the pod's own `/bin` and `/lib` is imported and a container from it runs and prints its marker. No registry is involved: the inner daemon holds no registry credential, and a pull would test the registry. |
| **3. Speed** | The gVisor penalty on suite-shaped work: the same fixed workload, same image, same shape, in the sandbox and outside it. | The design's bar: the sandboxed run **keeps a margin under the suites' 600 s limit**. The script reads it as sandboxed wall time ≤ 600 s × (1 − margin), with the margin 20 % (480 s) unless `--margin-pct` says otherwise: the design names no number, so the owner is asked to confirm it (the PR's questions). The ratio sandboxed / control is recorded beside it. |

What the fixed workload is: 20,000 fork-and-execs; 64,000 small files written
by 16 parallel workers; five full walks of that tree; a tar copy of it; 1 GiB
hashed on each of 16 cores; then the tree removed. It runs in `/work`, a
memory-backed tmpfs like the worker's `work/`. It reads no network. It is a
stand-in for "one representative suite": the private project's suites are not
in this repository. Its absolute time is not a suite's, so **read the ratio
too**: a real suite that takes *T* seconds outside the sandbox fits when
ratio × *T* stays under the bar.

### Expect check 1 to fail in a namespace rendered from this repository today

Read before running. A tenant namespace as `kubernetes/render.py tenant`
renders it today refuses this pod on three counts, each in a file of this
repository:

* **LimitRange.** `kubernetes/namespaces/tenant-namespace.yaml`,
  `swarm-worker-limits`: `max` memory `32Gi` per container and per pod. The
  pod asks for 64 GiB.
* **Pod Security Admission.** The same file enforces `restricted` (the design's
  §2.5 says `baseline`; that is stale: it was raised when the dispatcher's
  manifest gained its security context). `restricted` refuses `runAsNonRoot:
  false` and every added capability but `NET_BIND_SERVICE`; even `baseline`
  refuses `SYS_ADMIN`, `NET_ADMIN` and `NET_RAW`.
* **The platform's own policy.** `kubernetes/policies/pod-security.yaml`,
  `swarm-worker-pod-hardening`, bound to every namespace labelled
  `app.kubernetes.io/part-of: swarm`: `runAsNonRoot must be true`.

The script never changes any of them. The preflight's server-side dry run
reports every refusal without creating anything, so the first, free result is
which of these refused. A refusal there is a valid spike result: it is what
option C costs in policy, which the owner weighs against option D. Running the
sandboxed pod at all needs a namespace whose LimitRange, Pod Security level and
policy binding the owner has deliberately changed for the spike; that is the
owner's decision, asked in the PR's questions, not something this kit does.

## Impact inventory

**Where.** One tenant namespace, `swarm-tenant-<tenant>`, of a dev cluster,
named with `--namespace`. Nothing outside it is written. Placeholders only in
this runbook: `<tenant>`, `<project>`, `<dev cluster>`.

**Every object created**, each labelled `managed-by=swarm-spike`,
`swarm-spike=recording-runner`, `swarm-spike-run=<run id>`:

| Object | Name | Size | Lives |
|---|---|---|---|
| Pod | `recording-spike-<run id>-sandboxed` | 16 CPU, 64 GiB, 10 GiB ephemeral (request = limit) | until cleanup; ends itself after 4 h (`activeDeadlineSeconds`) |
| PersistentVolumeClaim | `recording-spike-<run id>-sandboxed-image-store`, created by the pod's generic ephemeral volume | 50 GiB on the class given with `--storage-class` | until the pod object is deleted |
| Pod | `recording-spike-<run id>-control` | 16 CPU, 64 GiB, 10 GiB ephemeral | until cleanup; ends itself after 4 h |

No ConfigMap, Secret, service account, role, policy, namespace or node pool.
The pods run as the namespace's disarmed `default` account with no API token
and no Google identity. The dry runs persist nothing.

**Cost per hour**, from the repository's own rates (Autopilot general-purpose
list prices in `us-central1`, docs/browser-startup-speed.md "Cost at list
price", and `apps/swarm-api/swarm_api/runnersizes.py::PRICES`, confirmed on
2026-10-13 only through secondary sources; Autopilot bills requests):

| Item | Per hour |
|---|---|
| One pod: 16 vCPU × $0.0445 | $0.7120 |
| One pod: 64 GiB × $0.0049225 | $0.31504 |
| One pod: 10 GiB ephemeral × $0.0000548 | $0.00055 |
| **One pod** | **$1.0276** |
| **Both pods** (the control starts while the sandboxed pod is still up) | **$2.0553** |
| The claim: 50 GiB pd-balanced at $0.10 per GiB-month (docs/browser-startup-speed.md, "not re-verified 2026-10-13"), over 730 hours | $0.0068 |
| GKE Sandbox surcharge, if any | not in the repository |
| Egress through NAT | not in the repository; the workload reads no network, and only the kubelet's image pull crosses it |

A one-hour run is about $2.06; the worst case, both pods forgotten until their
4-hour deadline, about $8.25, plus the claim at about $0.16 a day until the pod
object is deleted.

**What it can disturb on the cluster:**

* **Capacity.** Autopilot provisions nodes for the two pods: up to 32 vCPU and
  128 GiB more on-demand capacity for the run. They draw on the project's
  regional CPU quota; whether it has 32 vCPU free is a precondition below. The
  platform's own capacity pools in Firestore are **not** touched: the spike
  bypasses the scheduler, so no lease, no pool counter, and nothing a
  reconciler will count or reclaim (CONTRACT.md invariants 1-3 are unaffected
  because nothing in Firestore changes).
* **The tenant's quota.** Both pods count against the namespace's
  ResourceQuota (`swarm-tenant-quota`): 32 CPU and 128 GiB of requests, 2
  pods, 1 claim, 50 GiB of storage. While they run, the tenant's own workers
  have that much less, and a worker Job could be refused for quota. Run it when
  the tenant is idle; the preflight prints used and hard values.
* **Other tenants' pods.** None are touched: the pods are in one namespace
  under its default-deny, cross-tenant-ingress-deny and worker-egress
  policies, which select every pod in it; the spike pods carry no label any
  other policy keys on. A gVisor pod runs on a sandbox node; neither pod
  shares a 16-core node's CPU with anyone (Guaranteed).
* **The never-touch list.** `scripts/lib/common.sh` reads the deployment's
  list when it is sourced, and stops the script if it cannot. The script then
  refuses a namespace, or the tenant id in it, or a storage class on the list,
  before any `kubectl` call; it acts only through a context
  `assert_kube_context` accepts as the swarm's own, which refuses any context
  ending in a listed cluster. The delete selects only the run's own label in
  the named namespace: never a name, never `--all`, never across namespaces
  (held by the unit test).

## Preconditions, each with its read-only check

| # | Precondition | Read-only check |
|---|---|---|
| 1 | The owner has approved the cost above and named the namespace. | The approval is written on the issue. |
| 2 | `kubectl` points at the dev cluster through the swarm's own context. | `scripts/configure-kubectl.sh`, then `kubectl config current-context` shows `swarm-dev` or `gke_<project>_<region>_<dev cluster>`. |
| 3 | The never-touch list is readable. | `bash -c 'source scripts/lib/common.sh && deny_list_require && echo readable'` |
| 4 | The namespace is a tenant namespace with its policies. | `kubectl get namespace swarm-tenant-<tenant> --show-labels`; `kubectl -n swarm-tenant-<tenant> get networkpolicy` lists `swarm-default-deny` and `swarm-allow-worker-egress`. |
| 5 | The cluster offers GKE Sandbox. | `kubectl get runtimeclass gvisor` |
| 6 | The storage class the tenant quota allows exists. | `grep -n 'CLAIMED_CACHE_STORAGE_CLASS =' apps/scheduler/scheduler/dispatch.py` names it; `kubectl get storageclass <that class>`. |
| 7 | The tenant has quota headroom and is idle. | `kubectl -n swarm-tenant-<tenant> describe resourcequota swarm-tenant-quota`; `kubectl -n swarm-tenant-<tenant> get pods`. |
| 8 | The project's regional CPU quota has 32 vCPU free. | `gcloud compute regions describe <region> --project <project> --format='value(quotas)'` (read `CPUS`: limit minus usage). |
| 9 | A daemon image, **by digest**, that the cluster's nodes can pull, with `dockerd`, the `docker` CLI, `tar` and a busybox-style `sh` (the upstream Docker-in-Docker image, mirrored into the deployment's own registry, is one). | `gcloud artifacts docker images describe <registry>/<path>@sha256:<digest>` |

The script's `--preflight` runs checks 2 and 4-7 itself, and a server-side dry
run of both pods.

## Steps

| Step | Who | Command | Success looks like |
|---|---|---|---|
| 1. Preflight | operator | `scripts/recording-runner-spike.sh --preflight --namespace swarm-tenant-<tenant> --daemon-image <registry>/<path>@sha256:<digest> --storage-class <class>` | Every preflight row PASS; `1. admission (dry run)` PASS. If it is FAIL, the excerpt names the refusing layer (LimitRange, Pod Security, the platform policy, quota, Autopilot): **that is the result**; record it (below) and stop. |
| 2. Apply | operator | the same with `--apply` instead of `--preflight`; type the namespace at the prompt | The pods are created only after the namespace is typed. Rows `1. admission (create)`, `2. daemon`, `3a`, `3b`, `3. speed` fill in. |
| 3. Cleanup | operator, at the script's last prompt | type the run id | `cleanup: delete` and `cleanup: nothing left` PASS. |
| 4. Record | operator | below | The findings pull request is open. |
| 5. Decide | owner | on the issue | Contract request 159: accepted for C, amended for D, or declined (the design's Q7). |

The run id is printed at the start and in the result file (`rrs-<UTC time>`).
`SWARM_ASSUME_YES` is ignored: both confirmations are typed at a terminal.

## Rollback, and how to confirm nothing is left

If the run was interrupted, or the last prompt was declined, the script prints
the cleanup command; run it:

```bash
scripts/recording-runner-spike.sh --cleanup --namespace swarm-tenant-<tenant> --run-id rrs-<id>
```

It lists the run's objects, asks for the run id to be typed, deletes pods and
claims by `swarm-spike=recording-runner,swarm-spike-run=rrs-<id>` in that
namespace only, and then lists again: `cleanup: nothing left` PASS means no pod
and no claim carries the label. To confirm by hand, for every run at once:

```bash
kubectl -n swarm-tenant-<tenant> get pod,pvc -l swarm-spike=recording-runner
```

No output means nothing is left. Deleting the pods releases their nodes to
Autopilot's scale-down; nothing else needs undoing, because nothing else was
changed.

## What is recorded afterwards, and where

* **The result table**, written by the script to
  `build/recording-runner-spike/<run id>.md` (git-ignored) and printed. Every
  line from `kubectl` in it went through `redact`.
* **The findings**, appended by the operator to
  [docs/design/recording-runner.md](../design/recording-runner.md) as lane 0's
  findings (its §10 says so) in one pull request, `part of` the issue: the
  table, the run's date, the cluster's GKE version, the daemon image digest,
  and the section 8 items this run could read (the GKE Sandbox surcharge, the
  regional CPU quota headroom) with where each was read and when.
* **The owner's decision** on contract request 159, on the issue.
