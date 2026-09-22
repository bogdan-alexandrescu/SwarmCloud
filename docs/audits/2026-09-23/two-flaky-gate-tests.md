# Two flaky tests in `make test`, and what is known about each

Found 2026-09-23 while merging. Both are PRE-EXISTING — neither was introduced
by the merges around them, and both were proved pre-existing rather than
assumed. Neither is fixed. They are written down because a gate that fails
intermittently teaches people to re-run instead of investigate, which is worse
than the test not existing.

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

---

## Why neither was fixed here

Both surfaced inside commits about something else — a graph integration and a
type scale. Fixing an unrelated flaky test inside those is the scope mixing
CLAUDE.md warns against, and in the first case there is no failing instance to
work from on an idle machine.

What is NOT acceptable is leaving them undocumented, because the next person to
see a red gate will re-run it, watch it go green, and learn the wrong lesson.
