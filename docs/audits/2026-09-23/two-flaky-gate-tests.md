# Two flaky tests in `make test`, and what is known about each

Found 2026-09-23 while merging. Both are PRE-EXISTING — neither was introduced
by the merges around them, and both were proved pre-existing rather than
assumed. They are written down because a gate that fails intermittently teaches
people to re-run instead of investigate, which is worse than the test not
existing.

## Status, 2026-09-24

| # | test | state | fixed by |
|---|---|---|---|
| 1 | `test_bench_collectors.py::…::test_a_healthy_run_records_a_baseline_and_passes_against_it` | **FIXED** — cause found, reproduced deterministically in CI, fixed | `da6bf54` on PR #25 (`scripts/lib/benchlib.sh`), pinned by `TestTwoRunsInOneCheckout`, red first on `a1239b5` (run 35975902195) |
| 2 | `shell.test.tsx` B3 — "the head carries a breadcrumb and the age of the newest successful read" | **FIXED** before this file was committed to main | `b5e84f0` (`beforeEach`) and `054803a` (`afterEach`) on the fix branch, merged in `d3fd290` on 2026-09-22; on main since the squash `0857b46` (#1). `apps/swarm-ui/src/__tests__/setup.ts:35-48` |

The sections below are the original record, left as written, with what was
learned since added under each one. The original said "Neither is fixed" and
"the absence is not explained by the code as read"; both were true when
written, and neither is true now.

---

## 1. `test_bench_collectors.py::TestApiCollector::test_a_healthy_run_records_a_baseline_and_passes_against_it`

**Rate: load-dependent.** Failed 1-in-2 and 1-in-3 while three workflows, a
headed browser and concurrent gates were running on this machine. Passed 4-in-4
once the machine was quiet.

**What it asserts:** `api.latency{route=/v1/stats}` is present in the recorded
metrics after a healthy run.

**What actually happens on a failure:** the recorded metric dict contains only

```
api.stats.history_tasks{route=/v1/stats,scope=platform}
api.stats.ms_per_1k_history{route=/v1/stats,scope=platform}
```

Both `api.latency` AND `api.ttfb` are absent. That is the useful detail: it is
not one sample being dropped, it is the whole per-request loop's output
missing, while the metrics derived from the response BODY survive.

**Why that is surprising.** `scripts/bench-api.sh` emits `api.latency` on all
three branches — `bench_sample` on a 2xx, `bench_missing` on any other status,
`bench_missing` on a transport failure — and `burst()` `wait`s for every
background worker, each of which always writes a file. There is no path through
the reader loop that emits nothing. `benchstat.py:24` further states that a
metric whose samples are ALL missing is still reported.

So the absence is not explained by the code as read, which is exactly why it
needs instrumenting under load rather than reasoning about.

**Do not "fix" it by relaxing the assertion.** A benchmark that silently drops
its headline metric under load is a real defect in the benchmark; the test is
reporting something true.

### The cause, found 2026-09-24

The reasoning above was right about the collector and looked in the wrong
process. Nothing in `bench-api.sh` drops samples. **Another run deletes them.**

`bench_init` in `scripts/lib/benchlib.sh` built the sample path from the suite
and `ENVIRONMENT` alone — `build/bench-api-benchtest.jsonl` for every case in
this file — and created it with `: >`, which truncates. `build/` is the
checkout's. So a second collector for the same suite and environment, started
while the first was still measuring, erased every sample the first had written.
The first then appended its last phase — the two `/v1/stats` metrics derived
from the response body, which `bench-api.sh` writes AFTER the per-request loop
— and summarised the file as its own.

That is the signature to the letter: every per-request sample gone, the two
last-written metrics present. "Load-dependent" was the observer's reading of a
timing window: the collision needs two collectors whose runs overlap, which a
busy machine running "concurrent gates" in one checkout provides and a quiet one
does not.

**Why CI never saw it, measured.** `gh run view --log-failed` over every failed
`application` run — 117 of them — plus the failed jobs of the 16 whose run-level
log came back empty: **zero** failures of `test_bench_collectors`, ever. CI runs
this file in one process, one case at a time, so two collectors never overlap
there. The same fact explains why marking the file `serial` (`83fbdb9`) made the
local failures stop: under `-n auto` its cases ran in parallel workers, which is
exactly the overlap. That commit attributed the failures to CPU contention;
the timing premise it states is real, but it was not the mechanism.

**Reproduced deterministically, not under load.** `TestTwoRunsInOneCheckout`
holds one `bench-api.sh` at the start of its `/v1/stats` phase, runs a second
one for the same suite and environment to completion, releases the first, and
asserts the first run's baseline holds its own `api.latency` and nothing of the
second's. On `a1239b5`, with the fix held back, CI run 35975902195 failed it
with the first run's baseline holding:

```
api.latency{route=/v1/capacity}          <- the SECOND run's route
api.ttfb{route=/v1/capacity}             <- the second run's route
api.stats.history_tasks{...}             <- the first run's last phase
api.stats.ms_per_1k_history{...}         <- the first run's last phase
```

— the flake's two surviving metrics, plus what the original report could not
have noticed: the rest of the file was somebody else's measurements.

**Fixed** in `da6bf54`: the sample file and the summary are per run (`$$`). The
`serial` mark stays for the timing premise and its comment now says what it
does and does not protect.

---

## 2. `apps/swarm-ui/src/__tests__/shell.test.tsx:227` — "B3: the head carries a breadcrumb and the age of the newest successful read"

**Rate: order- and cache-dependent.** Expected `'nothing has loaded'`, got
`'newest read just now'`.

**Proved pre-existing** by a lane that checked out the base commit in a
temporary worktree with shared `node_modules` and ran it there: run 1 passed
with a cold vitest transform cache, runs 2 and 3 failed with the identical
message.

**Cause, as far as it was established:** it passes in isolation
(`-t 'carries a breadcrumb and the age'`) and fails when the whole file runs.
The probe registry in `fetch.ts` is MODULE-LEVEL and persists across tests, so
whether a probe has already recorded a success by the time this synchronous
`render(<App/>)` is asserted depends on timing. A warm transform cache is
enough to flip it.

**The shape of the fix** is to reset the probe registry between tests, the way
`vitest.config.ts` already sets `restoreMocks` and `unstubGlobals` so a stubbed
`fetch` cannot leak — the same argument, applied to a module-level singleton
the config cannot reach.

### Fixed, and by exactly that shape

`apps/swarm-ui/src/__tests__/setup.ts:35-48` calls `forgetProbes()` in a
`beforeEach` and again in an `afterEach` after `cleanup()`. Two lanes wrote it
independently on 2026-09-22 — `b5e84f0` added the `beforeEach`, `054803a` the
`afterEach` — and both reached the fix branch in the merge `d3fd290`, whose
message records that `forgetProbes` itself had been written twice and merged
without a conflict. This file was committed at `0562842`, before that merge,
which is why it still called the test unfixed. On main the fix has been present
since the squash `0857b46` (#1).

Not re-proved here by reverting it, and CI history was not searched for this
test's failures: the evidence is the code at `setup.ts:35-48` and its header,
which names this test and the mechanism.

---

## Why neither was fixed in the commits that found them

Both surfaced inside commits about something else — a graph integration and a
type scale. Fixing an unrelated flaky test inside those is the scope mixing
CLAUDE.md warns against, and in the first case there is no failing instance to
work from on an idle machine.

What is NOT acceptable is leaving them undocumented, because the next person to
see a red gate will re-run it, watch it go green, and learn the wrong lesson.
