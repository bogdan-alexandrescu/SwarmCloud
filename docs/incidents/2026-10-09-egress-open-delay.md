# Incident, 2026-10-09: a new Cloud Run instance's path to the internet opens a median 20 s after its process starts; a GKE pod's opens in about 1 s

**Impact.** Every step that runs as a Cloud Run Job clones its repository at
start, and the clone waits for the instance's path to github.com:443 to open.
Over the 7 days to 2026-10-09, that path opened a median **20.2 s** after the
worker process started on Cloud Run (n=19). On GKE Autopilot pods, through the
**same** Cloud NAT, the same NAT address and the same subnet, it opened in a
median **1.2 s** (n=148). Issue #939, split from #721 part (b).

**The likely cause is Google's documented Direct VPC egress + Cloud NAT
start-up delay, not this platform's NAT settings.** Google's Direct VPC egress
page says, in so many words: "With Cloud NAT, you might experience cold start
delays of 30s or more on instance startup when using Direct VPC egress. For
better startup performance, we recommend using Serverless VPC Access
connectors with Cloud NAT." (§3). The NAT configuration is ruled out as the
cause of the systematic delay by its own error log (§4.2), and DNS, GitHub and
the probe are ruled out by the GKE control group (§4.3 to §4.5). What is NOT
established is which of the two Google-side steps holds the packets: the
instance's Direct VPC egress interface being programmed, or Cloud NAT's
mapping for the instance's new source address. No reading here separates them
(§5), and no option in §6 depends on separating them.

**What was decided.** Nothing yet. §6 lays out the options; the recommendation
is **Recommended, awaiting the owner**. No Terraform for the NAT or for Cloud
Run egress is changed by the PR that adds this document; the change the
recommendation needs is written out in §6 so that, once decided, it is one
contract request and one release.

Related, and not the same span: [2026-10-06: a step's clone spends a median
36.6 s in one TCP connect to GitHub](2026-10-06-clone-connect-stall.md) (#721,
#917) measured the clone's connect; this document measures when the path
under it opens. [2026-09-25: a worker whose first connection to Firestore was
never answered](2026-09-25-worker-startup-network.md) is the same Google
limitation seen on Google APIs. **#625 measures execution creation to
container start, which ends before this document's clock begins** (process
start): the two spans add, they do not overlap.

Every reading below was taken by the operator from their laptop on 2026-10-09
with read-only `gcloud` and read-only API calls, and is quoted with its
command and the time it was read. This document was written without access to
the project or the API: nothing here was re-measured by its author.

---

## 1. What was measured

### 1.1 `egress_ready_seconds` by backend

Read 2026-10-09T16:2xZ, by the pure helper `scripts/egress_ready_report.py`
from PR #979 (branch `recover-939-fix`, not on main when this was written),
over 7 days of `GET /v1/tasks/{id}/events`, with the helper's event-type match
made case-insensitive (the API serves `"type": "running"` in lowercase; the
helper as first written matched only `RUNNING` and read zero marks; the fix is
on `recover-939-fix`). Window from 2026-10-02T16:24:57.380880+00:00.

| backend | n | n_none | p50 | p90 | max | clone_p50 |
|---|---|---|---|---|---|---|
| CLOUD_RUN_JOB | **19** | 0 | **20.20 s** | 36.48 s | 42.65 s | 18.04 s |
| GKE_AUTOPILOT | 148 | 0 | **1.17 s** | 3.76 s | 5.32 s | 2.49 s |

```
visited: 199 task(s), 167 egress_ready mark(s), 167 clone_timed mark(s)
verdict: FAIL -- insufficient n (19 CLOUD_RUN_JOB marks < 30)
```

**How to read it.**

* **n is small on Cloud Run: 19 marks, under the issue's bar of 30.** The
  verdict is therefore `FAIL -- insufficient n`, not a measured failure of the
  bar. But a p50 of 20.2 s against a bar of 5 s is far from passing at any n:
  for the true median to be under 5 s, at least 10 of these 19 values would
  have to be, and the measured median is four times the bar. More samples will
  move the p90 and the max; they cannot plausibly move the median below 5 s.
* **The sample is not every task in the window.** The shell wrapper (§1.2)
  listed at least 2,000 tasks in the same 7 days before it stopped at
  `--max-tasks`; the measured run visited 199. How those 199 were chosen is
  the operator's and is not recorded with the reading. Most steps now run on
  GKE (claude-code moved there on 2026-10-08, contract request 53), which is
  why Cloud Run has 19 marks against GKE's 148.
* **n_none is 0 on both.** Every probe connected within its cap; no Cloud Run
  instance went the whole 80 s without a path.
* **`egress_ready_seconds` is counted from process start**, the same origin as
  `agent_started` (`apps/agent-worker/agent_worker/lifecycle.py::Worker._start_egress_probe`),
  so it includes the worker's own start-up before the probe's first connect.
  GKE's 1.17 s p50 bounds that overhead: the probe code and the worker are the
  same image on both backends.
* **`clone_p50` is the clone's git time, not its wait.** `clone_timed`'s
  `seconds` is the git steps' wall time
  (`apps/agent-worker/agent_worker/lifecycle.py::Worker._mark_clone_timed`), and the clone
  waits for the probe (`apps/agent-worker/agent_worker/gitops.py::await_egress`) before its
  first git step. Read that way, Cloud Run's 18.0 s p50 is git still being
  slow **after** the probe had connected, against 2.5 s on GKE. That matches
  #721's observer P27 (the probe connected, yet git's own connect stalled in 5
  of 7 clones) and is a second, open signal that "the path is open" is
  per-destination or per-connection rather than one instance-wide switch. It
  was not broken down further here (§7).
* **The title's 35 to 70 s was the connect, not the opening.** #721 measured
  git's connect (`connect_seconds`), which includes the kernel's SYN
  retransmission backoff on top of the opening. The probe opens a fresh
  connection every `EGRESS_PROBE_INTERVAL_SECONDS` (1 s) with a
  `EGRESS_CONNECT_TIMEOUT_SECONDS` (1 s) timeout, so it trails the opening by
  about a second at most, and what it measures is the opening itself: a
  median of 20 s, a maximum of 43 s. The SYN-backoff reading of #721 is
  still an **inference**; these numbers are consistent with it (an opening
  around 20 to 40 s, answered on a later retransmit, lands in #721's 36 s and
  68 to 71 s modes) but do not prove it.

### 1.2 Reading 1b: the shell wrapper failed, and how

```
$ SWARM_IMPERSONATE_SA=swarm-verify@saga-agents-staging.iam.gserviceaccount.com PATH=/Users/bogdan/.local/bin:$PATH timeout 600 ./scripts/egress-ready-report.sh --since 7d
read at 2026-10-09T16:04Z

== Tasks created in the last 7d ==
warn stopped listing at --max-tasks 2000; older tasks in the window were not read
==> 2000 task(s) listed

== Reading startup marks ==
 fail GET /v1/tasks/task_421cf00932094597aed6/events?limit=200 answered HTTP 302
./scripts/egress-ready-report.sh: line 76: /var/folders/c_/h186tqvn13l70n19h8gz13f40000gn/T//egress-ready.HfGNDi/page.json: No such file or directory
exit=124
```

This is a **defect of the wrapper**, recorded here and not fixed by this PR:
`scripts/egress-ready-report.sh` is not on main. It is in PR #979 (branches
`recover-939`, `recover-939-fix`), so the fix belongs on that branch. Read
from `recover-939-fix` at 7c3a45f:

* **The temp directory is deleted and the script carries on.** The wrapper
  sets `trap 'rm -rf "${WORK}"' EXIT INT TERM`. On INT or TERM the trap
  removes `WORK` and then **returns**: bash does not exit after a trap
  handler unless the handler says so. `exit=124` is `timeout 600`'s status,
  so the script was sent TERM at 600 s. The likely sequence (an inference
  from the order of the output, not traced): TERM arrived while an events
  request was in flight; the trap deleted `WORK`; the request came back
  without a 2xx; `fetch()` reported it and then ran
  `redact <"${PAGE}"`, a file in the deleted directory: "No such file or
  directory".
* **The 302 is reported bare.** From the IAP front door, a 302 is the
  sign-in redirect: the request carried no credential IAP accepted. `curl`
  in `api_request` (`scripts/lib/common.sh`) does not follow redirects, so
  nothing was read, which is right, but the message names only the status.
  Why the credential was missing on this one request is not known. One
  candidate that fits the timing: the same TERM reached `timeout`'s process
  group, killed the `gcloud` minting the access token inside
  `api_request`'s `token="$(api_credential)"`, and the request went out with
  an empty bearer. `api_request` does not check that the token is non-empty.
  That is an inference; the 302 could equally be a genuine refusal.
* **The window was too large for one 600 s run anyway.** At least 2,000 tasks
  in 7 days, each needing one events read and one token mint, does not fit in
  600 s at the cadence this run reached. That is why the measured run (§1.1)
  used the helper over 199 tasks.

The change the wrapper needs, to be made on PR #979's branch:

```diff
-trap 'rm -rf "${WORK}"' EXIT INT TERM
+# INT and TERM EXIT, and the EXIT trap removes WORK: a handler that only
+# removed it would let the script run on against a deleted directory (#939,
+# reading 1b).
+trap 'rm -rf "${WORK}"' EXIT
+trap 'exit 130' INT
+trap 'exit 143' TERM
@@ fetch() {
   local path="$1"
   if ! api_request GET "${API_PREFIX}${path}" >"${PAGE}"; then
-    err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
-    redact <"${PAGE}" >&2 || true
+    case "${API_STATUS}" in
+      3??)
+        # Refused, never followed: from the IAP front door a 3xx is the
+        # sign-in redirect, so the request carried no credential IAP took.
+        err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}, a redirect this report refuses to follow: from the IAP front door it means no accepted credential was sent. Nothing was read."
+        ;;
+      *)
+        err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
+        # Only a body this request wrote: never a file it did not.
+        [[ ! -s "${PAGE}" ]] || redact <"${PAGE}" >&2 || true
+        ;;
+    esac
     exit 2
   fi
 }
```

and, separately and on main, `api_request` should refuse to send a request
when `api_credential` printed nothing, rather than letting IAP answer it with
a sign-in page. That is a change to every caller of `scripts/lib/common.sh`
and its evidence is the inference above, so it is listed in §7 rather than
made here.

## 2. The readings

All on 2026-10-09, project `saga-agents-staging`, region `us-central1`, on
swarm-owned resources only (`swarm-nat`, `swarm-router`,
`swarm-subnet-us-central1`, `swarm-job-*`, `swarm-verify`). None is on the
deny-list in `scripts/lib/common.sh`. Verbatim output is in issue #939's
investigation brief; the parts that matter are quoted.

**Reading 2, the NAT (16:14Z).**
`gcloud compute routers nats describe swarm-nat --router=swarm-router --region=us-central1 --project=saga-agents-staging --format=yaml`

`natIpAllocateOption: MANUAL_ONLY` with one address, `swarm-nat-ip-0`;
`enableDynamicPortAllocation: true`, `minPortsPerVm: 128`,
`maxPortsPerVm: 8192`; `enableEndpointIndependentMapping: false`;
`endpointTypes: [ENDPOINT_TYPE_VM]`; `sourceSubnetworkIpRangesToNat:
LIST_OF_SUBNETWORKS` with `swarm-subnet-us-central1` on `ALL_IP_RANGES`;
`logConfig: {enable: true, filter: ERRORS_ONLY}`; `tcpTransitoryIdleTimeoutSec:
30`, `tcpEstablishedIdleTimeoutSec: 1200`. This matches
`terraform/modules/network/main.tf` (`google_compute_router_nat.this`) and
`nat_static_ip_count = 1` in `terraform/environments/dev/dev.tfvars`. No drift.

**Reading 3, the subnet (16:14Z).**
`gcloud compute networks subnets describe swarm-subnet-us-central1 ...`:
`10.40.0.0/20`, `privateIpGoogleAccess: true`, `stackType: IPV4_ONLY`, flow
logs on at `flowSampling: 0.5`, `INTERVAL_10_MIN`, `INCLUDE_ALL_METADATA`.
Matches the module. Both the Cloud Run instances (Direct VPC egress takes an
address from this range) and the GKE pods (its secondary range, which
`ALL_IP_RANGES` includes) leave through `swarm-nat`.

**Reading 4, the jobs' egress setting (16:14Z).**
`gcloud run jobs list ... --format='table(metadata.name,metadata.annotations."run.googleapis.com/vpc-access-egress")' --filter='metadata.name~^swarm-'`
printed an empty egress column for all 17 jobs. **That column is the wrong
place to look, not evidence that egress is unset.**

* The Terraform sets egress through the v2 job template:
  `terraform/modules/cloud_run_jobs/main.tf`, `template { template {
  vpc_access { egress = "ALL_TRAFFIC" network_interfaces { network,
  subnetwork, tags } } } }`. A `network_interfaces` block with no `connector`
  is Direct VPC egress; there is no Serverless VPC Access connector anywhere
  in `terraform/`. `swarm-verify` sets the same in
  `terraform/infra/verify.tf` (`egress = "ALL_TRAFFIC"`).
* In the v1 YAML gcloud reads, Google documents those two annotations,
  `run.googleapis.com/network-interfaces` and
  `run.googleapis.com/vpc-access-egress`, under
  **`spec.template.metadata.annotations`** (the execution template), not the
  job's top-level `metadata.annotations`
  ([Direct VPC egress](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc),
  read 2026-10-09). Reading 4 asked the job's own metadata.
* So, from the Terraform: **every worker job sends all traffic, internet
  included, through its Direct VPC egress interface into `swarm-vpc`, and
  internet-bound traffic leaves through `swarm-nat` on `swarm-nat-ip-0`.** The
  repeatable read that confirms it on the live jobs is in §8.

**Reading 5, VPC flow logs (16:14Z).** The reading was titled "to
140.82.0.0/16 github", but its filter has no destination term:
`logName=".../vpc_flows" AND jsonPayload.src_vpc.subnetwork_name="swarm-subnet-us-central1"`,
`--freshness=1d --limit=20`. So it returned the 20 newest flows of any kind,
all within 16:14:01.75 to 16:14:02.70Z, and **none to GitHub**. What it shows:
flows to `172.16.32.2` (inside `gke_master_cidr = "172.16.32.0/28"`, the GKE
control plane: ports 443 and 8132, the latter konnectivity), flows to Google
front ends on 443 (Private Google Access), and several SRC-reported flows with
`bytes_sent` 0. A zero-byte flow at a 10-minute aggregation is not, on its
own, a dropped SYN. **This reading does not answer whether a stalled
instance's packets leave the instance**; §8 has the query that does.

**Reading 6, NAT error logs (16:14Z).**
`gcloud logging read 'resource.type="nat_gateway"' --freshness=3d --limit=20 ...`
returned 20 entries, all `allocation_status: DROPPED`, all within 27 ms at
2026-10-09T07:39:48.177Z to 07:39:48.204Z. With `--limit=20`, newest first,
this is the newest 20 errors in 3 days, not all of them: how many more there
are, and from which source, the reading does not say (it printed neither
`connection.src_ip` nor `endpoint.vm_name` nor the drop reason).

**Reading 7, `egress_ready` in Cloud Logging (16:14Z).** `[]` over 7 days.
The worker writes `egress_ready` as a task event through the API
(`apps/agent-worker/agent_worker/lifecycle.py::Worker._mark_egress_ready`), not as a log
line, so the empty result is expected and the API is the only place the
marks can be read.

## 3. What Google documents (read 2026-10-09)

[Cloud Run, Direct VPC egress](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc):

> You might experience connection establishment delays of a minute or more on
> instance startup when using Direct VPC egress.

> With Cloud NAT, you might experience cold start delays of 30s or more on
> instance startup when using Direct VPC egress. For better startup
> performance, we recommend using Serverless VPC Access connectors with Cloud
> NAT.

> At steady state, Cloud Run uses 2 times (2X) as many IP addresses as the
> number of instances.

The 2026-09-25 incident quoted the first two sentences. The recommendation
that follows the second is new to this repository's records.

[Cloud NAT, monitoring and logging](https://docs.cloud.google.com/nat/docs/monitoring):
`ERRORS_ONLY` "sends a log when a packet is dropped because no port was
available; does not log new connections". A drop is `OUT_OF_RESOURCES` ("if
Cloud NAT runs out of NAT IP addresses or ports") or
`ENDPOINT_INDEPENDENCE_CONFLICT`.

[Cloud NAT, ports and addresses](https://docs.cloud.google.com/nat/docs/ports-and-addresses):
each NAT address has 64,512 TCP source ports; under dynamic port allocation,
"If a VM is close to exhausting all the ports that are allocated to it, the
number of ports assigned to the VM is doubled." Neither Cloud NAT page says how
ports are allocated to a Cloud Run instance on Direct VPC egress, or how long
a new instance's mapping takes to program.

## 4. The candidate causes, ranked

### 4.1 Most likely: Google's Direct VPC egress start-up for a new instance, with Cloud NAT

**For.**
* It is documented, for exactly this combination, at exactly this size:
  "cold start delays of 30s or more ... with Cloud NAT ... Direct VPC egress"
  (§3). Measured: p50 20 s, p90 36 s, max 43 s.
* It is the only difference between the two rows of §1.1. GKE pods use the
  same NAT, the same single NAT address, the same `minPortsPerVm`, the same
  subnet, the same image, the same probe and the same GitHub, and open in
  about 1 s. They do not use Direct VPC egress.
* #721 found Google APIs over Private Google Access fast on the same
  instances, while the internet was slow. Private Google Access does not go
  through Cloud NAT; the internet does.

**Against, or not known.**
* The 2026-09-25 incident saw a Google API connection (Private Google Access,
  no NAT) stall the same way, so the interface itself can be late, not only
  the NAT mapping. Which of the two holds the packets on the internet path is
  not separated (§5).
* §1.1's clone p50 suggests git can still be slow after the probe connected.
  A single instance-wide "programming done" moment does not explain that by
  itself; a per-destination mapping step (endpoint-dependent mapping is the
  configured default) would. Not measured.

### 4.2 Unlikely as the systematic cause: NAT port allocation (`minPortsPerVm` 128, one address)

**For.**
* Reading 6 shows a real burst of `DROPPED` allocations, 20 within 27 ms at
  07:39:48Z. One address is 64,512 ports; at `maxPortsPerVm` 8192 only seven
  endpoints can hold their maximum at once, so a burst of parallel
  connections from a few busy endpoints can starve others.
* Under dynamic allocation, an endpoint that needs more than its block waits
  for the doubling; packets in that gap can drop.

**Against, and this decides it.**
* **A port-allocation drop is logged.** `ERRORS_ONLY` logs every packet
  dropped because no port was available (§3). If port allocation held every
  new Cloud Run instance's first SYNs for 20 s, every Cloud Run start would
  leave `DROPPED` entries. #721 read **0** NAT errors over 3 days
  (2026-10-03 to 2026-10-06) during which every one of 422 clones stalled.
  Reading 6's newest errors are one instant, not one per start.
* The probe needs one port. 128 ports is enough for the first connection of
  any instance.
* GKE pods start new connections through the same NAT, address and minimum
  and are not delayed.

**What it does not rule out.** A drop for a mapping that does not exist yet
(the instance's new source address not yet programmed into the NAT) is not
"no port was available" and would not be logged under `ERRORS_ONLY`. That
belongs to §4.1, not here. The 07:39:48Z burst is a separate defect worth one
read (§7); it is not this one.

### 4.3 Ruled out: DNS

The probe resolves `github.com` on every try
(`socket.create_connection` in `apps/agent-worker/agent_worker/egress.py::EgressProbe`).
Resolution goes to the metadata server, which needs no NAT. #721 measured DNS
at 0.07 to 0.54 s on the same instances, and a resolution failure would end
each try at once rather than produce a 20 s opening. GKE resolves the same
name through the same kind of resolver in about a second.

### 4.4 Ruled out: the probe's own cadence

`apps/agent-worker/agent_worker/egress.py::EgressProbe` opens a fresh connection each
round: `EGRESS_PROBE_INTERVAL_SECONDS` = 1.0 s between round starts,
`EGRESS_CONNECT_TIMEOUT_SECONDS` = 1.0 s per connect, up to
`EGRESS_PROBE_CAP_SECONDS` = 80.0 s from process start; the clone waits at most
`EGRESS_CLONE_WAIT_SECONDS` = 80.0 s
(`apps/agent-worker/agent_worker/gitops.py::await_egress`). A fresh socket never inherits a
SYN backoff, so the probe trails the opening by about 1 to 2 s at most. It
cannot add 19 s, and the same code reads 1.17 s on GKE. The cap is above
every value measured (max 42.65 s, n_none 0), so it truncates nothing.

### 4.5 Ruled out: GitHub

The same destination answers GKE pods in about a second, over the same NAT
address, in the same window.

## 5. What would separate interface from NAT, and why it is not needed to decide

The question "do a stalled instance's SYNs leave the instance and get no
translation, or not leave it at all" is answered by VPC flow logs for one
Cloud Run instance's flows to GitHub's addresses, compared with the time of
its first successful flow (§8 has the query). With `ERRORS_ONLY`, NAT logs
will not show translations; seeing them would take NAT logging on `ALL`, which
is a live change, billed by volume, and was not made.

Every option in §6 bypasses both steps together, or neither. So the answer
would sharpen the record but does not change the decision.

## 6. Decision

**Recommended, awaiting the owner.** The decision is the owner's; nothing in
this section is switched on by the PR that adds it.

Measure each option the same way: `scripts/egress-ready-report.sh --since <window> --backend cloud-run`
(PR #979) shows the Cloud Run `egress_ready_seconds` p50 under 5 s over at
least 30 steps, with the change named. For option A the steps move to GKE, so
the judged row is GKE's for the moved profiles.

### Option A (recommended): run the remaining clone-first profiles on GKE Autopilot

Move `generic`, `codex` and `indexer` from `CLOUD_RUN_JOB` to `GKE_AUTOPILOT`,
as contract request 53 did for `claude-code` (applied 2026-10-08).

* **Shortens.** The whole opening: measured, GKE p50 1.17 s, p90 3.76 s,
  max 5.32 s over n=148, already under the bar, through the same NAT. It
  also takes the Cloud Run start wait of #625 off these profiles: request 53
  measured GKE's fresh-node start p90 at 120 s against Cloud Run's 212 s.
* **Costs.** Autopilot pod billing in place of Cloud Run job billing for the
  same `requests == limits` shape (invariant 7). No new billed resource type:
  the cluster, the `GKE_AUTOPILOT` pool (100 in dev) and per-tenant
  namespaces exist. Spot stays off (invariant 6).
* **Blast radius.** A frozen-contract change: each profile's `backend` is in
  `apps/common/swarm_common/profiles.py`, which this PR does not edit. A
  profile at a time, behind a canary as request 55 was, with each one's Cloud
  Run Jobs kept as the rollback through `cloud_run_fallback_profiles`. Tenant
  isolation (invariant 9) is the GKE path's existing namespace and Workload
  Identity setup, already in use by `claude-code` and `browser`.
* **Not moved.** `merge` is a worker-action profile, and contract request 60
  treats a GKE backend on a worker-action profile as something `__post_init__`
  should refuse; it stays on Cloud Run and keeps the delay until that is
  decided. `mock` is a test profile and can follow or stay.

The exact change, once the owner accepts the request (one per profile, in
this order: `generic` as the canary, then `indexer`, then `codex`):

```diff
--- apps/common/swarm_common/profiles.py   (frozen: by contract request only)
@@ "generic"
-    backend=Backend.CLOUD_RUN_JOB,
+    backend=Backend.GKE_AUTOPILOT,
@@ "codex"
-        backend=Backend.CLOUD_RUN_JOB,
+        backend=Backend.GKE_AUTOPILOT,
@@ "indexer"
-        backend=Backend.CLOUD_RUN_JOB,
+        backend=Backend.GKE_AUTOPILOT,
--- terraform/infra/locals.tf
@@ runner_profiles "generic", "codex", "indexer"
-      backend         = "CLOUD_RUN_JOB"
+      backend         = "GKE_AUTOPILOT"
@@
-  cloud_run_fallback_profiles = ["claude-code"]
+  cloud_run_fallback_profiles = ["claude-code", "generic", "codex", "indexer"]
```

plus the catalogue mirror test (`tests/terraform/catalogue.tftest.hcl`)
following the two files. `indexer` names a model in `runner_models`
(`terraform/infra/locals.tf`); on GKE no Job of this root carries it, so it
reaches the pod only through the scheduler's `WORKER_MODELS`, as
`claude-code`'s does. Check that before the switch, not after.

**Why A over B.** A is the only option with a measurement that already meets
the bar, through the same NAT and address, so it keeps the reserved egress
address and the VPC firewall and flow logs. B is Google's own recommendation
and probably works, but it is unmeasured here, is a new always-on billed
resource, and was taken out of this run by the owner to decide separately.

### Option B: a Serverless VPC Access connector for the worker jobs

Swap each worker job's `network_interfaces` for a `connector`, still
`ALL_TRAFFIC`, still leaving through `swarm-nat`.

* **Shortens.** Google recommends it for exactly this ("For better startup
  performance, we recommend using Serverless VPC Access connectors with Cloud
  NAT", §3): the connector VMs are long-lived NAT endpoints, so a new instance
  needs no new mapping. Not measured here.
* **Costs.** At least 2 connector instances, always on: "The minimum must be
  at least 2" and "Connectors don't scale in"
  ([Serverless VPC Access](https://docs.cloud.google.com/vpc/docs/serverless-vpc-access),
  read 2026-10-09). Billed as Compute Engine VMs plus egress; at e2-micro list
  prices that is roughly two small VMs a month (not re-read here; read the
  pricing page before deciding). Throughput per instance, from the same page:
  e2-micro 200 to 1,000 Mbps, e2-standard-4 3,200 to 16,000 Mbps, up to 10
  instances: every clone and every model call of every tenant shares it.
* **Blast radius.** The instances would have no address of their own in
  `swarm-vpc`: the connector's VMs do, carrying the connector's own tags.
  `fw-deny-worker-ingress` targets the worker tag on each instance's interface
  (`terraform/modules/network/firewall.tf`) and would no longer name
  anything; ingress to a worker becomes impossible rather than denied, which
  is not weaker, but every tenant's egress becomes one source in flow logs,
  so per-instance forensics (§8's query) is lost. Invariant 9 holds at the
  identity, secret, GCS and namespace layers, which do not depend on the
  network address. A new Terraform resource, so `managed-by=swarm-terraform`.

### Option C: egress `PRIVATE_RANGES_ONLY` on the worker jobs

* **Shortens.** Probably most of it, if the delay is the NAT side: internet
  traffic would leave through Cloud Run's own egress, not `swarm-nat`. If the
  delay is the interface (§4.1, against), it shortens nothing, since Google
  APIs and the control plane would still go through it. Not measured: the
  2026-09-25 incident compared `swarm-verify` under both settings for the
  container-start span only, not for this one.
* **Costs.** No money. It gives up the reserved NAT address that
  `vpc_access` in `terraform/modules/cloud_run_jobs/main.tf` exists to keep
  for provider allow-lists. Nothing in this repository records a provider
  that allow-lists `swarm-nat-ip-0` today; whether one does is the owner's
  knowledge, and it is asked separately. It also takes internet egress out
  of the VPC firewall (`restrict_egress`, off in dev and prod today, would
  no longer apply to it) and out of VPC flow logs.
* **Blast radius.** One line per environment, every worker job, every tenant.

### Option D: tune the NAT (more addresses, a larger minimum, static allocation, endpoint-independent mapping)

* **Shortens.** Nothing measured, and §4.2's evidence says port allocation is
  not the systematic cause: a starved allocation is logged, and #721 saw none
  in 3 days of stalled clones.
* **Costs.** A reserved address and its NAT charge per address added; every
  new address must be added to any provider allow-list before it carries
  traffic. Static allocation or a larger `minPortsPerVm` reduces how many
  endpoints one address serves (64,512 / minimum).
* **Not recommended for #939.** The 07:39:48Z `DROPPED` burst deserves its
  own read (§7) and may justify a second address for another reason.

### Option E: accept the wait and hide it behind clone-independent start-up work

* **Shortens.** Nothing in `egress_ready_seconds`, which is counted from
  process start and is the bar. A step's wall time shrinks only by whatever
  start-up work is moved ahead of the clone. #940's per-SHA GCS bundle over
  Private Google Access would take most of the transfer off GitHub, but its
  delta fetch still needs the path, so it shortens the transfer, not the wait.
* **Costs.** No money; worker changes only.
* **Fails the issue's bar by construction.**

## 7. Not done here, and why

* **No Terraform change.** Brief for this PR: list, do not apply. The
  recommended change is in §6.
* **No contract request filed.** Option A is a frozen-contract change; it
  becomes a request in `docs/contract-change-requests.md` once the owner
  chooses it, not before.
* **The wrapper's fix (§1.2) is not made**: the wrapper is not on main. It
  belongs on PR #979's branch.
* **`api_request` sending an empty bearer** (`scripts/lib/common.sh`): the
  case for it rests on §1.2's inference. Proposed as a follow-up, not made.
* **Not broken down**: the 19 Cloud Run marks by `probe_attempts`, by
  `peer`, and the clones' `connect_seconds`, `probe_peer`, `git_peer` and
  `peer_pinned`. The brief asked for them; the operator's reading printed the
  summary only. `scripts/egress-ready-report.sh --json` (PR #979) carries the
  marks to compute them from.
* **The 07:39:48Z NAT drops** were not traced to a source. The read in §8
  does it.

## 8. How to repeat the readings

Read-only, swarm-owned resources only. Bracket every `OR`.

```bash
P=saga-agents-staging; R=us-central1

# Reading 4, at the right level: a job's egress lives on its execution template.
gcloud run jobs describe swarm-job-eng-generic --region=$R --project=$P \
  --format='yaml(spec.template.metadata.annotations)'

# The 19 marks, broken down (PR #979).
scripts/egress-ready-report.sh --since 7d --backend cloud-run --json

# Reading 5, scoped to GitHub (140.82.112.0/20 is github.com's block; check
# https://api.github.com/meta for the current list) and to one instance's
# source address, with the first non-zero flow to compare against.
gcloud logging read 'logName="projects/'$P'/logs/compute.googleapis.com%2Fvpc_flows"
  AND jsonPayload.src_vpc.subnetwork_name="swarm-subnet-us-central1"
  AND jsonPayload.connection.dest_port=443
  AND (ip_in_net(jsonPayload.connection.dest_ip, "140.82.112.0/20"))' \
  --project=$P --freshness=1d --limit=200 \
  --format='table(timestamp,jsonPayload.connection.src_ip,jsonPayload.connection.dest_ip,jsonPayload.bytes_sent,jsonPayload.packets_sent,jsonPayload.reporter)'

# Reading 6, with its source and reason, and its size.
gcloud logging read 'resource.type="nat_gateway" AND jsonPayload.allocation_status="DROPPED"' \
  --project=$P --freshness=3d --limit=1000 \
  --format='table(timestamp,jsonPayload.connection.src_ip,jsonPayload.connection.dest_ip,jsonPayload.endpoint.vm_name)'
```
