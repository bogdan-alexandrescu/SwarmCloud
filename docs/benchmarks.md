# Benchmarks — measuring what the correctness suites cannot see

`make test` proves the platform is right. Nothing proves it is fast, and on
2026-09-22 that gap produced its best illustration yet. The first real
`claude-code` run on this platform:

```
dispatched 03:46:05 → starting 03:49:14 (+3m09s) → running +0.1s → succeeded +18s
```

**Three minutes nine seconds of Cloud Run cold start against eighteen seconds
of agent.** Every suite in this repository calls that run a pass, because it
*is* a pass — the task succeeded, no capacity leaked, no invariant was
violated. The UI renders the whole thing as the single word "dispatched".
Nothing anywhere turns 189 seconds into a figure anyone can compare against
last week.

That is what this suite is for.

---

## The four rules

These are not style preferences. Each one is a way a benchmark lies, and each
is enforced in `scripts/benchstat.py` rather than left to the author of a
collector.

### 1. A failed measurement is not a fast one

The rule benchmarks break more often than any other, and the same bug this
repository has produced repeatedly in other costumes — an unread value falling
into a comparison that is true for every bound.

A timing that could not be taken is recorded as `{"value": null,
"not_measured": "<reason>"}`. It is counted in `missing`, never as zero, and a
metric whose samples are *all* missing is reported `not_measured` and **fails
the gate**. A route answering HTTP 500 in five milliseconds does not set a
record; it is recorded as not measured, with the status as the reason.

There is a second, quieter form of the same rule: `max_missing_fraction`. A
metric where more than a fifth of samples failed fails as `too_many_missing`,
because the percentile is then computed only over the samples that worked — and
the ones that failed are the likelier to have been slow.

### 2. No mean. p50 / p95 / p99 and n, always

There is deliberately **no `mean` field** in a summary. A mean hides the tail,
and the number here that most needs its tail read is the cold start above: a
mean over a warm pool erases it. A field nobody may gate on is a field somebody
will quote, so it is not emitted at all.

### 3. A percentile from two samples is a lie

`min_samples` is part of every threshold. Below it, the metric fails as
`insufficient_samples` rather than passing on a "p99" that is just the larger
of a pair. This fires often while a collector is being written, and it is
supposed to: the fix is to take more samples, not to lower the bar.

### 4. A baseline is part of the benchmark

A number with nothing to compare it to is a number nobody acts on. Baselines
live in `benchmarks/baselines/<environment>.json` and are **checked in**, so a
regression is reproducible on someone else's clone. With no baseline present,
every metric reports `new` and the run says plainly that *nothing was
compared* — a first run must not look like a green gate.

A metric the baseline knows about that produces **no samples at all** fails as
`missing_from_current`. That is the collector having stopped running, or the
code path it measures having stopped executing, and both are precisely what a
benchmark exists to notice.

### A ratio threshold is meaningless at the noise floor

A `1.5x` rule on a route that answers in 3ms fires forever: the ordinary jitter
of a process spawn is itself more than 1.5x of 3ms. This is not theoretical —
the offline collector tests failed about one run in three for exactly this
reason, on the *untouched* routes, while the injected 400ms regression was
caught perfectly. The routes really had gone from 2.0ms to 3.4ms.

Where the signal is small against the noise, gate on an **absolute limit**
rather than a ratio, or do not gate that metric at all and read it beside one
that can be. The offline fixture now answers in 50ms so that a few milliseconds
of scheduler jitter is a few percent; on a deployed environment the real
latencies are large enough that the default ratio is meaningful.

### And one more, because clocks

**Negative durations are reported, never clamped.** Segment timings come from
event timestamps written by different processes on different machines. Clock
skew produces a negative segment, and `max(0, x)` would turn a real distributed
systems problem into a suspiciously fast dispatch. A negative sample is kept,
counted, and fails as `anomalous_samples`.

---

## What each suite measures, and what it costs to run

| Suite | Time | Money | Creates anything? |
|---|---|---|---|
| `api` | ~30s | under a cent | no — GETs only |
| `reconcile` | ~10s | under a cent | **no — it reads logs, it does not trigger a pass** |
| `cost` | ~15s | under a cent | no — reads history |
| `ui` | ~1m | free | no — drives a browser at a UI you are running |
| `dispatch` | ~2m on `mock` | cents | **submits tasks**, cancels them on exit |
| `admission` | ~3m on `mock` | cents | **submits a 150-task backlog**, cancels on exit |

`dispatch` and `admission` on `--profile claude-code` **spend real provider
tokens**, because they run an actual agent. That is also the only way to
measure the cold start that matters, so it is worth doing deliberately with a
small `--count` and not on every commit.

`make bench` runs the read-only three (`api`, `reconcile`, `cost`). Nothing in
that set submits work, so it is safe against a production environment.

### `api` — latency per route, with percentiles

`scripts/bench-api.sh`. A fixed concurrency against each route the web UI
drives. Per route, because the UI's own data-source strip measured `/v1/stats`
at 1711ms and `/v1/providers` at 896ms while everything else answered under
300ms — and a single "API p95" averages exactly that away.

**`/v1/stats` gets three metrics, not one.** `service.stats()` issues one
Firestore `count()` per task state, and Firestore bills an aggregation per 1000
index entries **scanned**. So that route's latency and cost grow with
*history*, not with load — a threshold on its raw latency fires on a perfectly
healthy platform that has simply been running for a month, and an operator
learns to ignore it. Recorded instead:

| metric | what it says |
|---|---|
| `api.latency{route=/v1/stats}` | the raw number, which is what the UI shows |
| `api.stats.history_tasks` | how many task documents exist right now |
| `api.stats.ms_per_1k_history` | latency normalised by that history |

**The normalised one is the one to gate on.** Flat means the route is fine and
the collection grew. Rising means the route itself regressed.

### `dispatch` — the decomposition, and cold start

`scripts/bench-dispatch.sh`. "Dispatch got slower" is at least four different
problems with four different owners:

| segment | owner |
|---|---|
| `ready → lease_acquired` | admission — the scheduler's transaction |
| `lease_acquired → dispatched` | the control plane creating the execution |
| `dispatched → starting` | the **backend** — scheduling and image pull |
| `starting → running` | the container reaching the agent |

`dispatch.cold_start` (`dispatched → running`) is its own metric rather than
the sum of its two halves, because **percentiles cannot be added**: p95 of two
segments is not p95 of the whole. It is labelled by `runner_profile` and by
`backend`, so Cloud Run Jobs and GKE Autopilot never share a baseline.

This collector reads the **API**, not Firestore, which is a deliberate
departure from the other suites. Their reason for reading Firestore — that
observing through the API under test would let a broken API report success —
does not transfer: these timestamps are written by the control plane into the
event log, and a broken API produces a transport failure or a non-2xx, both of
which are recorded as not-measured. What it buys is that this collector needs
no `roles/datastore.viewer`, which is the exact permission the verify service
account lacks today and the reason `race-test.sh` cannot run.

### `admission` — throughput under a backlog

`scripts/bench-admission.sh`. 150 tasks, which is `concurrency-test.sh`'s shape
on purpose so the two runs are comparable. The rate is computed from
`lease_acquired` timestamps — first lease to last — not from how long the
script took to notice, because polling rate is the script's property and the
timestamps are the platform's.

**On whether the all-or-nothing multi-pool reservation degrades with pool
count**: not answered by reconfiguring pools, which would be an admin mutation
on a shared project. Different runner profiles already reserve different
numbers of pools — a profile with no provider skips the provider pool — so
running this on two profiles and comparing is the experiment.
`admission.pools_visible` records the platform's pool cardinality beside it,
read from `/v1/capacity` rather than restated from the frozen catalogue.

### `reconcile` — pass duration, and what the pass examined

`scripts/bench-reconcile.sh`. **It reads Cloud Logging. It does not trigger a
pass.** A pass mutates: it terminates executions, releases leases and deletes
job resources, and `dry_run` comes from the service's environment rather than
from the request, so there is no safe way to ask for one. A triggered pass is
also unrepresentative — fired by hand seconds after the last tick, it examines
almost nothing and reports a wonderful duration.

`Reconciler.run_once` already logs its whole report as structured fields, so
the numbers exist for every real pass. This reads them back.

**The important metric here is not the duration.** When a backend cannot be
listed — the GKE 401 seen on 2026-09-22 — the reconciler correctly *skips* that
backend's findings rather than repairing against a list it could not read,
appends one string to `report.errors`, and returns a pass that looks healthy
and is **faster** than a complete one. Every duration and count threshold
rewards it. `reconcile.backend_errors` has an absolute maximum of **zero**, and
it is the only check that fails that pass.

Zero passes found in the window is itself the finding, not the absence of one:
a reconciler that has not run in six hours is a platform whose stale leases are
not being reclaimed. It is recorded as not-measured, which fails.

### `cost` — spend per task

`scripts/bench-cost.sh`. A change that doubles token usage — a longer system
prompt, a retry that re-sends context, a checkpoint restore that replays
history — is invisible to every latency benchmark here. Wall time can even
*improve* while the bill doubles.

It sums across **attempts**, not `result_summary`: that is written once at
terminal state, so a task that failed twice and succeeded on the third carries
only the third attempt's numbers, and a per-task cost that omits the two failed
ones understates every retried task on the platform.

**It is also a regression test for the spend fields.** `attempt_from_dict`
dropped all five of them once, and the unit test that should have caught it
asserted they are `None` when *absent* — an assertion the bug satisfies. This
collector cannot be satisfied that way: an attempt that ran an agent and
carries no `cost_usd` is recorded as not-measured with that reason, and there
is no assertion to get backwards because the absence of a number *is* the
failure.

### `ui` — what the dashboard costs in a real browser

`scripts/bench-ui.sh` plus `scripts/lib/bench-ui-probe.js`. `bench-api.sh`
measures the server's number; this measures the operator's. The Overview screen
issues a dozen `/v1` reads, several in parallel, and the slow one decides when
the screen is usable.

It uses the **Performance API** rather than the UI's internal probe registry:
it needs no change to the app (`apps/` is another track's), and it measures
what the browser did rather than what the app believes it did.

**Fixtures are not a measurement.** `npm run dev` without `VITE_LIVE` serves
fixtures and never touches the network, so a fixture run has zero `/v1`
resource entries. That run measures render cost honestly and API cost not at
all — so the API metrics are recorded as not-measured with that reason rather
than omitted. Omitting them would make a fixture run look like a very fast live
one, which is the single most likely way this benchmark could lie.

---

## Operating the browser driver

Four things cost several hours to learn and are easy to undo:

* **`BROWSE_HEADED=1` and a long `BROWSE_IDLE_TIMEOUT` go in the ENVIRONMENT.**
  Never pass `--headed`: the flag writes a `configHash`, and every later call
  must then match it or the daemon restarts underneath you.
* **Headed mode keeps a cookie jar** (`launchPersistentContext`); `launched`
  mode uses `chromium.launch()` and has none, so a silent fall back to it loses
  the session.
* **Never delete `.gstack/browse.json`** — `buildRestartEnv` reads `mode:headed`
  from it to survive an autostart.
* **The driver acts on the ACTIVE tab, and the active tab drifts.** Pin one by
  id and re-pin before every command. `bench-ui.sh` does, and re-opens a tab
  whose id has gone stale.

Three of this collector's own bugs are worth knowing, because each produced a
plausible wrong number rather than an error:

1. **The driver refuses to read a file outside its allowed roots.** The probe
   was written under `$TMPDIR` (`/var/folders/…` on a Mac), `eval` failed
   silently, and the read-back returned the **previous screen's payload**. Every
   screen would have been recorded with some earlier screen's numbers. The probe
   now lives under `build/`, and — more importantly — carries a **nonce** minted
   per capture. A payload without this capture's nonce is refused.
2. **A `goto` that differs only in the hash does not reload.** The SPA router
   handles it in place, so navigation timing reported the *first* load every
   time and the resource buffer **accumulated**, crediting each screen with
   every request every earlier screen had made. The symptom was
   `ui.nav.dom_content_loaded` reading 83.5ms for all six screens, to the tenth
   of a millisecond. Each capture now navigates to a unique query string before
   the hash, so every one is a genuinely new document.
3. **`set -o pipefail` plus `$(browse newtab | tail -n 1)`** meant that when the
   shared daemon was killed mid-run, the pipeline failed, the substitution
   failed, and `set -e` ended the script with exit 1 and no explanation — the
   "a failure that renders as nothing" shape, produced by the benchmark written
   to catch it. The status is now swallowed deliberately so the emptiness can be
   reported as the real cause.

---

## Running them

```bash
make bench                              # read-only: api, reconcile, cost
make bench SUITES=api,ui                # pick suites
make bench SUITES=dispatch              # submits tasks on mock, cancels them
make bench-baseline SUITES=api          # record this run as the reference
scripts/bench.sh --self-test            # offline; in `make test`
```

Against a deployed environment the API suites must run **in-VPC**, because
`swarm-api` ingress is `internal-and-cloud-load-balancing` and refuses a
laptop:

```bash
scripts/verify-remote.sh bench-api bench-reconcile bench-cost
```

`scripts/bench.sh --self-test` runs in `make test`. It is fully offline — no
cloud, no browser, no credentials — and it exercises the paths a collector
cannot reach on a laptop: the percentile engine, the not-measured semantics,
the baseline comparison, the event decomposition against the real 2026-09-22
timings, and the UI collector's capture-to-samples half through `--from-json`.

---

## Known gaps, stated rather than hidden

* **`benchmarks/baselines/` is empty.** Every baseline must come from a run
  against a real deployed environment; the benchmarks were written on a laptop
  that cannot reach `swarm-api`. A baseline recorded against anything else would
  be a fiction that every later run is compared to. See
  `benchmarks/baselines/README.md`.
* **The engine compares in one direction only** — it fails when a statistic
  rises. That is correct for every duration and wrong for throughput, where
  higher is better. `admission.throughput_per_s` therefore has its ratio gate
  disabled (`max_regression_ratio: null`) and `admission.window_seconds` — the
  duration form of the same thing — is gated instead. The proper fix is a
  `higher_is_better` flag in `benchstat.py`; it is written down here rather than
  faked with a huge threshold, because a huge threshold reads as an oversight
  and the next person tightens it.
* **Cold start is measured per profile and per backend, but only for backends
  that actually run.** GKE dispatch was 401ing on 2026-09-22, so a run today
  produces Cloud Run numbers and nothing for GKE. That shows up as an absent
  label rather than as a fast one.
