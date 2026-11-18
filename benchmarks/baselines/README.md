# Benchmark baselines

One file per environment: `dev.json`, `staging.json`, `prod.json`. Each is the
output of `scripts/benchstat.py baseline`, which is a summary with every
not-measured metric stripped out.

**These are checked in on purpose.** A baseline that lives only on the machine
that recorded it is a number nobody else can act on, and a regression nobody
else can reproduce. The comparison has to work on a fresh clone.

## Recording one

```bash
make bench-baseline SUITES=api,reconcile,cost
```

Then read the diff before committing it. Recording a baseline is asserting
*"this is what normal looks like"*, and the commit message should say why you
believed the run — a cold cache, a busy platform or a half-deployed revision
all produce numbers that will then fail every honest run afterwards.

## Why a not-measured metric is excluded rather than recorded as zero

If a collector could not take a measurement, recording the absence as a
baseline bakes the breakage in: the next run compares against a metric with no
value, gets `new`, and the collector stays broken quietly for as long as anyone
looks at the gate instead of the numbers. `benchstat.py baseline` drops them and
names them on stderr, so the operator finds out at the moment they would
otherwise have been enshrined.

## There is no baseline here yet

Deliberately. Every baseline in this directory must come from a run against a
real deployed environment, and the benchmarks were written on a laptop that
cannot reach `swarm-api` — its ingress is `internal-and-cloud-load-balancing`,
so the suites run in-VPC through `scripts/verify-remote.sh`. Committing a
baseline recorded against anything else would be committing a fiction, and
every later run would be compared to it.

Until one is recorded, `scripts/bench.sh` reports every metric as `new` and
says, in as many words, that **nothing was compared**.
