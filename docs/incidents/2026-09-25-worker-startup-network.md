# Incident, 2026-09-25: a worker whose first connection to Firestore was never answered

**Impact.** One Cloud Run execution, `swarm-job-eng-mock-9ngvq` (task
`task_5254e8f1cb31446ca20b`, step c of workflow `wf_7d0bb295af404d7a8c93`),
could not reach Firestore for the whole of its generation check and exited 69
having written nothing. The task sat DISPATCHED for another two minutes and
fifteen seconds, until the 20:40 reconciler pass reclaimed it at the dispatch
deadline. Attempt 2 succeeded at 20:42:49Z. One attempt was spent. Issue #198.

**The error blamed IPv6. IPv6 was not the cause.** The worker's error ended in
`ipv6:[2607:f8b0:4001:c00::5f]:443 ... Network is unreachable`. The VPC flow
logs show that the IPv4 connection is the one that failed, and that its SYNs
were never answered. The IPv6 error is only the last one gRPC kept.

**The cause is one Google documents.** "You might experience connection
establishment delays of a minute or more on instance startup when using Direct
VPC egress"
([Direct VPC egress, Limitations](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc),
read 2026-09-25). Every worker job reaches the network that way
(`terraform/modules/cloud_run_jobs/main.tf`, `vpc_access`). No change to the
swarm's network removes it, for the reasons in §3. The worker now asks again for
about 90 s before it gives up (PR #200), and Google's own mitigation is the same
thing: test a connection, with retries, before doing work.

Everything below was read on 2026-09-25, with the read-only commands in §6.

---

## 1. Timeline (UTC)

| time | what | source |
|---|---|---|
| 20:34:11.2 | execution created by `swarm-scheduler` | execution `metadata.creationTimestamp` |
| 20:34:12.3 | "Imported container image in 1.09s" | condition `ContainerReady` |
| 20:34:19.95 | task started | `status.startTime` |
| 20:37:22.9 | "Started deployed execution in 3m10.26s" | condition `Started` |
| 20:37:26.05 | `worker process started` | worker log |
| 20:37:26.08 | DNS preflight passed in 28 ms: `173.194.206.95` and `2607:f8b0:4001:c00::5f` for `firestore.googleapis.com` | worker log |
| 20:37:28.83 | phase `validate_generation` | worker log |
| 20:37:28.884 | SYNs from `10.40.0.58` to `173.194.206.95:443`; 18 packets, 0 payload bytes, no reply, until 20:37:48.397 (19.5 s, about gRPC's 20 s minimum connect timeout) | VPC flow log |
| 20:37:48.886 | a second connection, same destination; 14 packets, 0 payload bytes, no reply, until 20:37:56.013 | VPC flow log |
| 20:37:56.53 | exit 69: `Timeout of 30.0s exceeded ... 503 failed to connect to all addresses; last error: ... ipv6:... Network is unreachable` | worker log |
| 20:38:00.57 | execution completed, exit code 69 | execution `status.completionTime` |
| 20:40:15 | reconciler fences the lease, `generation_fenced` | issue #198 |
| 20:42:49 | attempt 2 (`swarm-job-eng-mock-fpwx9`) succeeds | issue #198 |

## 2. What was measured, and why it settles the IPv4 question

**Which instance.** Flow logs do not carry the execution's name (Google: "VPC
Flow Logs doesn't provide the name of the Cloud Run revision"), so the instance
is identified by time and destination. `10.40.0.58` sent its first packet 50 ms
after the worker entered `validate_generation`, to the IPv4 address the worker's
own DNS preflight had resolved, and its last packet 0.5 s before the worker
exited. It has no other flow in that window.

**Why "no reply" is certain despite sampling.** The subnet samples flow logs at
0.5, in 10-minute intervals (`terraform/modules/network/main.tf`, `log_config`).
Sampling could hide a reply packet, but not a completed handshake: a TCP
connection that is answered carries a TLS ClientHello, and both connections
sent 32 packets between them with **zero payload bytes**. There is also no
DEST-reported flow back from `173.194.206.95` at all, where every other job
instance in that half hour's flow logs has one.

**Why IPv6 never mattered.** The subnet is `stackType: IPV4_ONLY` with
`privateIpv6GoogleAccess: DISABLE_GOOGLE_ACCESS`. A connection to an IPv6
address fails locally, at once, with `Network is unreachable`, and never
appears in the flow logs. gRPC put the IPv4 address first (the SYN at
20:37:28.884 is 50 ms into the check), and its "failed to connect to all
addresses" names the most recent failure, which the instant IPv6 retries
always were.

**The rest of the batch was fine.** Cloud Run started six executions of that
job in the same 0.4 s (§5). The other five passed the same generation check in
0.17 to 0.27 s. Only `10.40.0.58` lost its SYNs.

**It has happened before, and been survived.** The same signature (SYNs to a
Google API address with no reply and no payload, in an instance's first
seconds) appears twice more in the 14 days of flow logs kept:

| when | instance | seconds after `Started` | what | outcome |
|---|---|---|---|---|
| 2026-09-22 21:13:10 | `10.40.0.58`, a `claude-code` execution | 7 | one connection unanswered for 19.4 s | recovered; its next flows at 21:14:14 were answered |
| 2026-09-25 10:57:19 | `10.40.0.22`, `swarm-verify-pxwlx` | 2 | three connections unanswered for up to 40 s, while other connections from the same instance were answered | recovered by 10:57:45 |
| 2026-09-25 20:37:28 | `10.40.0.58`, `swarm-job-eng-mock-9ngvq` | 6 | two connections unanswered for 27 s | the worker exited 69 |

The second row matters most: it is not "the instance has no network yet". Some
connections from one instance got through while others, opened at the same
time, were not answered for 40 s.

## 3. The fixes that were considered and why they are not the fix

| option | what it would do | why not |
|---|---|---|
| Private DNS for `googleapis.com` on `swarm-vpc` (the IPv4 `private.googleapis.com` VIP) | removes the IPv6 address from these errors | The VIP is reached over the same Direct VPC egress path whose SYNs went unanswered, so it would not have prevented this. It changes DNS for the whole VPC, the GKE cluster and the control-plane services included, and the deployer holds no DNS role (`terraform/bootstrap`). Not started. |
| Force the client to IPv4 | the same | IPv4 is what gRPC tried, and what failed. |
| Egress `PRIVATE_RANGES_ONLY` | keeps Google API calls off Direct VPC egress | It also takes provider calls off Cloud NAT and the reserved addresses providers allow-list, which is why the job module sets `ALL_TRAFFIC` (`terraform/modules/cloud_run_jobs/main.tf`, `vpc_access.egress`). It is also what guarantees an execution has no public IP of its own. |
| Ask again (PR #200) | the generation check runs at 0, 30 and 60 s, one warning per failed attempt, 69 only after the last | This is the fix. It outlasts a delay "of a minute or more" by the last attempt's own 30 s of retries. |

## 4. Noticed promptly: what PR #200 changes and what it does not

PR #200 adds a reconciler rule (`detect_ended_at_startup`) that requeues a task
still DISPATCHED or STARTING once its execution has ended, 30 s after the end,
with the exit code and the execution in `last_error`. How much sooner that is
than the 300 s dispatch deadline depends on how late the worker exits and on
the reconciler's tick:

* **A 69 from an unreachable control plane** now comes after the ~90 s of
  attempts, so the execution ends about cold start + 95 s after dispatch and
  the rule may act 30 s later. For the cold starts measured that day (103 to
  195 s from dispatch to worker) that is between about 70 s before and 20 s
  after the deadline. At the deployed `*/5` tick both usually act in the same
  pass: for the incident's own timeline the rule would have been eligible at
  about 20:39:31 and acted at the 20:40 pass, the same pass that reclaimed it.
  For this exit the rule mostly improves `last_error`.
* **A worker that dies in its first seconds** (an exit 1, a 143) is eligible
  65 to 155 s before the deadline, so a `*/5` pass falls between the two more
  often, and when it does the task is requeued five minutes sooner.
* **At a `*/1` tick** either is requeued 30 to 90 s after its execution ends.
  That change is prepared in draft PR #202 and is the owner's decision: it is
  1,440 reconciler passes a day instead of 288.

## 5. The 2.5 to 3 minute cold start

Issue #198 noted about three minutes between the image being ready and the
worker starting, as a separate question. Read over every execution Cloud Run
still lists (400, 2026-09-16 to 2026-09-25):

| span | p50 | p90 | max |
|---|---|---|---|
| created to `startTime` | 13 s | 23 s | 51 s |
| `startTime` to `Started` (the wait) | 93 s | 179 s | 359 s |
| created to `Started` | 110 s | 191 s | 376 s |

* **It is Cloud Run's wait, before the container.** The image import is about a
  second (`ContainerReady`), and the worker's first line comes about 3 s after
  `Started`.
* **It does not follow the execution's shape.** 4 CPU / 8 GiB (n=330) and 1 CPU
  / 512 MiB (n=70) both have a p50 of 93 s. Zero, one and two secret
  environment variables: 99, 90 and 90 s.
* **It does not follow the egress setting.** On the two days both ran,
  `swarm-verify` with `PRIVATE_RANGES_ONLY` (n=8) had a p50 of 141 s against 158 s
  with `ALL_TRAFFIC` (n=9). Google also documents that "with Cloud NAT, you
  might experience cold start delays of 30s or more on instance startup when
  using Direct VPC egress", but routing public traffic away from Cloud NAT did
  not shorten it here.
* **Executions created apart are released together.** The incident's six
  executions were created over 66 s (20:34:10.4 to 20:35:16.1) and all reached
  `Started` within 0.4 s of each other (20:37:22.86 to 20:37:23.24). Seven
  groups like that appear in the 400. A wait that ends at one shared instant
  for executions that began it a minute apart is a shared provisioning step,
  not per-container work.
* **What could not be measured.** All 400 executions attach Direct VPC egress,
  so there is no control group without it, and Direct VPC egress's share of the
  wait cannot be separated from this data. Measuring it takes one execution of
  a job with no `vpc_access`, which is a live change and was not made.
* **Whether it shares a cause with the 69 is not shown.** The batch release did
  not leave the network down for the batch: five of its six instances connected
  within a second of asking.
* **How it meets the dispatch deadline.** One cold start in 400 (376 s,
  2026-09-19) passed the 300 s deadline before its worker started. 55 of 400
  (14%) took more than 174 s from creation to `Started`. The worker reaches its
  generation check about 6 s after `Started`, and the worst case of its
  attempts is 120 s, so past 174 s a worker that meets an unreachable control
  plane for all of them is reclaimed at the deadline before its last attempt
  ends. The reconciler then stops the execution and the worker exits 143
  having written nothing, which is the same outcome as a 69, one pass later.

## 6. How to repeat the measurements

All reads, no writes. Project `saga-agents-staging`, region `us-central1`.

```bash
# The subnet: IPv4-only, Private Google Access on, flow logs sampled at 0.5.
gcloud compute networks subnets describe swarm-subnet-us-central1 \
  --region us-central1 --project saga-agents-staging \
  --format='yaml(stackType,privateIpGoogleAccess,privateIpv6GoogleAccess,logConfig)'

# The execution's conditions: ContainerReady, startTime, Started, Completed.
gcloud run jobs executions describe swarm-job-eng-mock-9ngvq \
  --region us-central1 --project saga-agents-staging --format=json

# Every flow of the failed instance in the incident's window.
gcloud logging read 'logName="projects/saga-agents-staging/logs/compute.googleapis.com%2Fvpc_flows"
  AND (jsonPayload.connection.src_ip="10.40.0.58" OR jsonPayload.connection.dest_ip="10.40.0.58")
  AND timestamp>="2026-09-25T20:30:00Z" AND timestamp<="2026-09-25T21:00:00Z"' \
  --project saga-agents-staging --format=json

# Candidates for the same signature elsewhere: zero-payload flows from job
# instances to :443. Keep the ones to a GOOGLE_API destination that last
# about 20 s and have no DEST-reported flow back.
gcloud logging read 'logName="projects/saga-agents-staging/logs/compute.googleapis.com%2Fvpc_flows"
  AND jsonPayload.reporter="SRC" AND jsonPayload.bytes_sent="0"
  AND jsonPayload.src_serverless_details.cloud_run_job_details.job_name:*
  AND jsonPayload.connection.dest_port=443' \
  --project saga-agents-staging --freshness=14d --format=json

# Cold starts: startTime and the Started condition of every listed execution.
gcloud run jobs executions list --region us-central1 \
  --project saga-agents-staging --limit=1000 --format=json
```

In the Logging query language `OR` binds tighter than `AND`. Bracket every `OR`,
or the filter quietly means something else and returns nothing.
