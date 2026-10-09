# Incident, 2026-10-06: a step's clone spends a median 36.6 s in one TCP connect to GitHub

**Impact.** Every SwarmCloud step clones its repository at start, and in dev
96 % of that clone's time was one TCP connect to github.com:443 that stalled
for 4 to 71 seconds. The transfer itself took a median 1.7 s. That adds a
median of about 37 s, and a p90 of about 70 s, to every step: about 2 to 3.5
minutes per three-step workflow. Issue #721, severity S2.

**The likely cause is the instance's internet egress path, not GitHub.** A
freshly started Cloud Run instance reaches the internet through Direct VPC
egress and Cloud NAT, and Google documents connection delays "of a minute or
more" on instance start-up for Direct VPC egress. The earlier incident,
[2026-09-25: a worker whose first connection to Firestore was never answered](2026-09-25-worker-startup-network.md),
quotes that limitation and its source; it is not restated here. That incident
was about Google APIs at the generation check. This one is about the forge,
after the container has started and the control plane has answered.

**What was decided.** The owner chose to build (a), an egress probe, on
2026-10-06, and it is in main. (b), why the path stays closed, is an open
question to Track C. (c), cloning from a per-SHA bundle in GCS, is a later
option. Neither (b) nor (c) is started.

Everything below was measured on 2026-10-06 in dev by the chunk-2 observer,
from `clone_timed` worker events and Cloud NAT logs, unless a section says
otherwise.

---

## 1. What was measured

**The sample.** 60 `clone_timed` events, 2026-10-06 15:31:35Z to 18:24:59Z:
every clone instrumented by then (the clone trace of #708). 2,831 s of clone
time in total.

**The connect.** 2,723 s of those (96.2 %) are `connect_seconds`, from curl's
first `Trying` to its `Connected to`
(`apps/agent-worker/agent_worker/gitops.py::clone_phase_timings`):

| | min | median | p90 | max |
|---|---|---|---|---|
| `connect_seconds` | 4.1 s | **36.6 s** | 69.5 s | 70.6 s |

No clone connected in under 4 s. Every clone had `connect_tries` 1 and
`connections` 1, so the delay is not curl falling back to a second address:
the one address it tried took that long to answer.

**Everything else is fast.** dns 0.07 to 0.54 s; tls 0.13 to 0.34 s; pack
0.003 to 3.9 s; checkout 0.3 s or less.

**Connect times fall on discrete values.** Rounded seconds, with counts:

| seconds | 4 | 7 | 11 | 15 | 19-20 | 23 | **36** | 37-39 | 43 | **68-71** |
|---|---|---|---|---|---|---|---|---|---|---|
| clones | 1 | 1 | 2 | 1 | 3 | 1 | **20** | 4 | 5 | **22** |

**Over three days the same three modes appear**, across 422 clones:

| mode | share | median |
|---|---|---|
| under 30 s | 35 % | 13.7 s |
| 37 to 47 s | 47 % | 38.1 s |
| 69 to 78 s | 18 % | 71.1 s |

**The reading, which is an inference.** The spacing of those values doubles,
as Linux's TCP SYN retransmission backoff does (1, 2, 4, 8, 16, 32 s between
retransmits). That fits SYNs being dropped until the instance's egress path
opens, and git's connect then being answered only on its next retransmit, up
to 32 s after the path opened. This is an inference: the timings fit it and
do not prove it. Nobody has captured the packets of a stalled clone. Keep it
labelled so; the probe in §2 is built so that it does not depend on it being
right.

**Cloud NAT is not reporting a fault.** `swarm-nat` logs errors only, and it
logged 0 errors in those 3 days. That says nothing was refused for lack of a
port; it does not say the path was open. Its configuration: 1 manual IP,
dynamic port allocation, min 128 ports per VM.

**Google APIs are not affected.** They go over Private Google Access, not the
NAT, and answer at once: Secret Manager resolved the git credential 0.3 s
into the clone phase. And once the path is open, the same instances reach
GitHub in well under a second: `fetch_issue` to api.github.com took 0.4 to
0.9 s right after a stalled clone.

**Lanes in chunk 2,** for anyone matching this to the events: C2A 4.1 s, C2C
35.5 s, C2D 68.0 s, C2B 69.4 s, IX3 69.7 s.

## 2. What was built for (a), and why each value is what it is

`apps/agent-worker/agent_worker/egress.py::EgressProbe` is a background thread
that opens a TCP connection to the forge, over and over, until one succeeds.
`apps/agent-worker/agent_worker/lifecycle.py::Worker._start_egress_probe`
starts it at the top of the worker's run, before configuration of the task is
read. The clone, in `gitops.shallow_clone` and `gitops.clone_at_commit`, first
calls `apps/agent-worker/agent_worker/gitops.py::await_egress`, which waits for
the clone host to answer the probe and then clones.

The reason each piece is the way it is:

* **A fresh socket each time.** A connect that is already waiting has entered
  the kernel's SYN retransmission backoff, and if the inference in §1 is right
  that backoff is the overshoot: a connect started at second 0 is next
  retransmitted at 1, 3, 7, 15, 31, 63 s, so if the path opens at 33 s it is
  answered at 63 s. A new socket starts with no backoff. Reusing one socket,
  or raising its timeout, would bring the overshoot back.
* **The per-connect timeout,
  `apps/agent-worker/agent_worker/egress.py::EGRESS_CONNECT_TIMEOUT_SECONDS` = 1 s.**
  A dropped SYN is first retransmitted after 1 s. A probe that waits longer
  than that is waiting inside the backoff it exists to avoid.
* **The interval, `apps/agent-worker/agent_worker/egress.py::EGRESS_PROBE_INTERVAL_SECONDS` = 1 s**
  between the starts of two rounds: the owner's figure in #721 (a). It is the
  most the probe can trail the path opening, against the up to 32 s that one
  connect's backoff can trail it.
* **The cap, `apps/agent-worker/agent_worker/egress.py::EGRESS_PROBE_CAP_SECONDS` = 80 s**
  from the probe's start. The longest stall measured was 78 s (the 69 to 78 s
  mode, 18 % of 422 clones) and 71 s among the 60 traced clones. A cap below
  that would give up on paths that were about to open. Lowering it needs a new
  measurement, not a preference.
* **The clone's wait bound,
  `apps/agent-worker/agent_worker/gitops.py::EGRESS_CLONE_WAIT_SECONDS` = 80 s,**
  the probe's own cap. It is a backstop, not a schedule: the probe ends by
  then anyway, counted from process start, so the wait a clone actually meets
  is shorter. The lease is heartbeated through it
  (`apps/agent-worker/agent_worker/lifecycle.py::Worker._clone_keeping_lease`),
  so a long wait does not cost the attempt to the reconciler's grace.
* **github.com is always probed, the clone host is added later.** The probe
  starts before the worker has read the task document, and the dispatcher sets
  no repository in the environment: the repository is on the signed task
  document. So `apps/agent-worker/agent_worker/egress.py::probe_targets`
  always gives github.com:443, and
  `apps/agent-worker/agent_worker/lifecycle.py::Worker._add_egress_target` adds
  the clone host once that document is verified, when it is somewhere else.
  Starting the probe only once the host is known would throw away the seconds
  the probe exists to use.
* **The probe sends no bytes.** It opens a TCP connection and closes it: no
  request, no header, so no credential is ever on it, and nothing on it can
  leak or be logged.
* **A probe that fails changes nothing.** If the probe cannot start, ends on
  its cap without an answer, or raises, `await_egress` returns at once and
  the clone runs exactly as it did before the probe existed. A measurement is
  never worth an attempt; nothing else at start-up waits for the probe.

**What it writes.**
`apps/agent-worker/agent_worker/lifecycle.py::Worker._mark_egress_ready`
writes the `egress_ready` startup mark once per attempt, next to
`clone_timed`, from `apps/agent-worker/agent_worker/egress.py::EgressProbe.result`:
`egress_ready_seconds` (process start to the first github.com connect that
succeeded, null if none did), `probe_attempts`, how the probe `ended` (ready,
cap, stopped), and the same per target with the `peer` each reached.
`egress_ready_seconds` is counted from process start, the same origin as
`agent_started`'s `seconds_since_process_start`, so the egress, clone and
agent marks sit on one timeline. Counted from the probe's own start it read
1.6 to 4.0 s low (observer P28), because the probe starts only once the
worker's configuration and clients are built. A task that clones nothing
writes no `egress_ready`.

The tests are `tests/unit/worker/test_egress_probe.py` and
`tests/unit/worker/test_clone_peer_pin.py`.

## 3. The follow-up: pinning a stalled clone to the address the probe reached

**Measured by observer P27, chunk 3, 2026-10-06:** the probe connected 11.7 to
39.6 s after process start, and git's own connect still stalled in 5 of 7
clones, for 7.1 to 35.6 s. Waiting for the probe was not enough: git's curl
was not always connecting to the address the probe had reached.

So the probe keeps the address of each target's first successful connect,
and `apps/agent-worker/agent_worker/gitops.py::PeerPin` compares it with the
address git connected to. A try whose connect stalled, by
`apps/agent-worker/agent_worker/gitops.py::connect_stalled`, to an address
other than the probe's, has every later try of that clone run with
`git -c http.curloptResolve=<host>:<port>:<probe address>`.

* **The stall threshold,
  `apps/agent-worker/agent_worker/gitops.py::CLONE_CONNECT_STALL_SECONDS` = 1 s.**
  The kernel retransmits an unanswered SYN after 1 s, so a connect that took a
  second or more waited for at least one retransmit. A healthy connect to
  GitHub takes tens of milliseconds.
* **TLS verification is unchanged.** `http.curloptResolve` is curl's
  `CURLOPT_RESOLVE`: the URL keeps its host name, so the certificate is still
  verified against that name and the Host header is the same. Only the name
  lookup is answered from the probe. The `-c` lives for one git command, so
  nothing outlives the clone.
* **Never pinned** when the probe never answered (there is no address it
  proved reachable), for an ssh or local clone (not curl), or when the stalled
  connect was to the probe's own address (a pin would change nothing).

Each try in `clone_timed`'s `try_log` carries `probe_peer`, `git_peer` and
`peer_pinned`, so whether the pin helped can be read from the events.

## 4. How to confirm the saving, which is not yet confirmed

The issue estimates that the probe saves about 10 s per step: the backoff
overshoot between the path opening and git's next retransmit. That figure is
an estimate. It is **not yet confirmed**, and this document records no
post-probe figure. Do not quote one from here.

To confirm it, read the two marks of the same attempt:

* `egress_ready`: `egress_ready_seconds` says when github.com first answered,
  counted from process start, and `probe_attempts` how many connects that
  took. This is the first direct measurement of when the path opens, which
  §1 could only infer.
* `clone_timed`: each try's `connect_seconds` in `try_log`, and the clone's
  `total_seconds`.

If the probe works as intended, a clone that started before
`egress_ready_seconds` shows a `connect_seconds` close to zero after the wait,
and the distribution of `connect_seconds` loses its 36 s and 68 to 71 s
modes. Compare `total_seconds` plus the wait (`waited_seconds` in the worker's
"egress probe" log line) against the pre-probe figures in §1. The gap between
`egress_ready_seconds` and where the pre-probe `connect_seconds` landed is the
overshoot the probe removed. If `connect_seconds` still stalls after the probe
answered, read `probe_peer` against `git_peer` (§3).

## 5. (b) Open question to Track C: why a new instance's egress path stays closed

**Status: not started.** This is an open question, deliberately not answered
by guessing here.

The question: why does the internet egress path of a freshly started Cloud
Run instance stay closed for 35 to 70 s? The candidates named in #721:

* **Direct VPC egress programming** for the new instance: the documented
  start-up delay the [2026-09-25 incident](2026-09-25-worker-startup-network.md)
  cites. That incident saw Google API traffic stall the same way; here Google
  APIs over Private Google Access are fast and only the internet path is slow,
  which neither candidate explains on its own.
* **Cloud NAT mapping for a new source IP.** `swarm-nat` has 1 manual IP,
  dynamic port allocation, min 128 ports per VM. It logged no errors, but it
  logs errors only.

Answering it needs Track C's view of the network (VPC flow logs and NAT
logging beyond errors for a stalled instance), not more worker events. The
probe's `egress_ready_seconds` gives that investigation the opening time per
instance to line up against.

## 6. (c) A later option: clone from a per-SHA git bundle over Private Google Access

**Status: not started.** An open option, recorded so it is not lost; nothing
of it is built.

Keep a git bundle per commit SHA in the tenant's GCS prefix, fetch it over
Private Google Access (which §1 shows is not affected), then fetch only the
delta from GitHub. That takes GitHub off the start path for the bulk of the
clone, and would save about 36 s median per step: the whole connect stall,
not only the overshoot (a) removes.

Its constraint: the bundle is the tenant's repository, so it must keep
per-tenant isolation (CONTRACT.md invariant 9). It lives under that tenant's
own GCS prefix, is read with that tenant's own GSA, and is never shared
between tenants, even for the same repository. The delta fetch still needs
GitHub, so (c) shortens the stall's cost only if the delta's connect does not
stall too, which is one more reason (b) matters.

## 7. Related, and not the same

* **#625 and #667** measure execution creation to container start. This
  incident is what happens after the container has started, so neither is a
  duplicate of it, and nothing here changes what they cover.
* **#748**: a MERGE verdict's container and its clone cost about 116 s, and
  part of that is this clone stall. Removing that container is a separate
  change.
