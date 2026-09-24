# Testing — what runs, what it proves, and what it does not

The operator entry point for every suite in this repository. If you are about to
say a change is done, you want [the order to run things in](#the-order-and-why).
If a benchmark just told you something, you want [how to read a benchmark
result](#how-to-read-a-benchmark-result). If you are wondering why so many of
these suites exist at all, read [the defect class this exists to
catch](#the-defect-class-this-exists-to-catch) first — it is the reason the
suites are shaped the way they are, and without it several of them look like
duplication.

---

## The one-screen version

**The gate is the CI run, and it is not on this machine.**

```bash
git commit && git push
gh pr create
gh run list --branch <branch>   # read the run
gh run view <id> --log-failed   # read the failure
```

Tests run in GitHub Actions, builds run in GitHub Actions, deployments run in
GitHub Actions. This repository is authored locally and gated remotely — see
[where the gates run](ci.md) for which workflow covers what, and why a pull
request never plans terraform. Report the run's conclusion; there should be no
local exit code to report.

What the remote gate executes is these two targets, and they are described
throughout this document in those terms:

```bash
make lint        # shellcheck, doc links, terraform fmt/validate, tflint, manifests
make test        # EVERYTHING OFFLINE: unit, integration, UI components, terraform, guards
```

They need no credentials, no emulator, no network and no browser, and they
create nothing. That is why they can be the gate at all. **Read the rest of this
file as "what the gate proves", not as "what to run here".**

Everything below needs something the gate deliberately does not: a deployed
environment, a browser, or a UI you are already running. Those are operator
actions against a deployment, not steps in authoring a change.

```bash
make ui-test              # headed browser against the production bundle (~8 min)
make verify-remote        # smoke, concurrency, race, e2e — INSIDE the VPC
make bench                # read-only performance suites against a deployment
```

---

## What each suite covers, and what it does NOT

The "does not" column is the load-bearing one. Every suite in this repository
that ever lied did so by being read as covering something adjacent to what it
actually asserted.

### Offline — no credentials, no network, safe in CI

| Suite | Command | Covers | Does NOT cover |
|---|---|---|---|
| Unit (1926 tests) | `uv run --project . pytest tests/unit -q` | Pure logic: admission, decoding, rollup, the worker's own seams | Anything that needs a real socket, a real process or a real browser |
| Integration (73 tests) | `uv run --project . pytest tests/integration -q` | The real `scripts/*.sh` driven end to end with a fake `gcloud` and a fake `curl` on `PATH` under `--dry-run` | Whether the real `gcloud` behaves like the fake one. It proves the script's logic, not the cloud's |
| UI components (199 tests, 6 files) | `make ui-component-test` | `apps/swarm-ui` typecheck plus Vitest/jsdom rendering, and the four honesty rules at runtime | Layout, contrast, overflow, or anything a person would see. jsdom has no geometry |
| Terraform (91 assertions) | `make tf-test` | Platform promises against a **mock** provider | Whether the real Google provider agrees. A mock cannot refuse what GCP would refuse |
| Guards and parity | folded into `make test` | `destroy.sh`, `plan-guard`, `auth-guard`, `kubectl-guard` self-tests; contract and env parity | — |
| Benchmark engine | `scripts/bench.sh --self-test` | That the gate arithmetic is right: percentiles, baselines, and not-measured-vs-zero | Any actual performance number. It tests the ruler, not the thing measured |

`make test` runs every row above. It is offline **by contract** — if you are
tempted to add something to it that needs a credential, add a new target instead.

Two things in that table are worth stating plainly because their names oversell
them. `tests/integration` is offline; it is "integration" in the sense that it
integrates the *scripts*, not the cloud. And the terraform suite's 91 assertions
run against a mock, so they prove the configuration says what we meant, never
that GCP accepts it.

### Needs a deployed environment (in-VPC)

`swarm-api` ingress is internal-and-cloud-load-balancing and **refuses a
laptop**. Everything here runs through `scripts/verify-remote.sh`, which executes
the suite as a Cloud Run job inside the VPC. Running these from a workstation
gets you an HTTP 404 on `/readyz`, not a result.

| Suite | Command | Covers | Does NOT cover |
|---|---|---|---|
| `smoke` | `scripts/verify-remote.sh smoke-test` | A mock task runs and releases capacity | Anything about a real agent |
| `concurrency` | `scripts/verify-remote.sh concurrency-test` | No pool is ever over its effective limit, at any sample | The moment *between* samples |
| `race` | `scripts/verify-remote.sh race-test` | The last free slot goes to exactly one task | Any runner profile but `mock` (refused by design, see below) |
| `failure` | `scripts/failure-test.sh` | Failure, cancellation and malformed input leak no capacity | — |
| `e2e` | `scripts/verify-remote.sh e2e-test` | **The seams**: workflow handoff on the BYTES, artifact provenance, spend through the API, sign-in, state agreement across Firestore/API/Cloud Run | The `mock` profile's runner **is** its own workload, so it cannot distinguish a file written by an agent child process from one the runner wrote. That exact distinction is proved offline in `tests/unit/worker/test_agent_seam_end_to_end.py` |
| `load`, `quota` | `scripts/load-test.sh`, `scripts/quota-test.sh` | Sustained-load percentiles; provider exhaustion parks rather than pays to wait | — |

`make e2e-test` and the rest run directly **from inside** the VPC or the verify
job. From a laptop, use `verify-remote`.

**`race-test` narrows `runner:mock` through one admin route, and is not an admin.**
It has to narrow a pool to create contention, and that is a write. By owner
decision on 2026-09-24 the suite uses `PUT /v1/admin/limits/runner/mock` — never
a Firestore write — to narrow and to restore, so the change is bounded, cannot
touch `active`, and is stamped on the pool as `admin_changed_by`. The verify
service account reaches that route through `admin_pool_users` in dev: swarm-api
lets that list call an allow-list of admin routes holding the runner ceiling
alone, and answers every other admin route 403. It is deliberately **not** in
`admin_users` — the first form of the decision put it there, and the owner
reversed that the same day, because full admin can disable any tenant. The
suite refuses any profile but `mock`, a pool that does not exist, a drained
pool, and a ceiling the API could not put back. Until a release has applied the
grant, the suite fails at its first step with a 403 that names
`admin_pool_users` — a permissions result, not evidence about a race. The
reasoning is in `docs/audits/2026-09-22/race-test-needs-a-write.md`.

Nothing in `e2e-test` writes to Firestore. Everything it creates is a task or a
workflow submitted through the API, and every one is cancelled on the way out.

### Needs a browser

| Suite | Command | Covers | Does NOT cover |
|---|---|---|---|
| Browser UX/UI | `make ui-test` | The **production bundle** over a real socket: what a person would eyeball — clipping, ellipsis, contrast, overflow, empty states — plus every `/v1` request the page actually made | It is not "the deployed UI". There is no deployed UI: no load balancer was ever built, so nothing serves the bundle to a browser. It serves the production bundle against a fixture control plane on the same origin |
| Browser benchmark | `make bench-ui URL=...` | Paint, load and every `/v1` fetch, from the Performance API | A fixture UI makes no network calls at all, so its API numbers are recorded as **not measured with that reason** rather than as fast |

The browser suite mocks nothing the product owns. The bundle is `npm run build`,
not the dev server, so `import.meta.env.DEV` is false and the fixture path in
`api.ts` is dead code — the app really issues `/v1` requests across a real
socket. The response bodies come from `tests/browser/fixtures.py`, which **calls**
`swarm_api.codec` and two real route handlers rather than restating what they
return.

#### `make ui-test` has four outcomes, not two

| exit | meaning |
|---|---|
| 0 | no findings |
| 1 | findings, every one already in `tests/browser/baseline.json` — **nothing got worse** |
| 2 | at least one **NEW** finding — something got worse |
| 3 | the harness could not run (no browser, no bundle, no node). **Never a pass** |

This UI has real defects today, so a suite returning the same `1` forever could
never report a regression. `baseline.json` is not approval; it is the line
between "still broken" and "newly broken".

GNU make returns 2 for *any* failed recipe, which would collapse all four of
those into one. The `ui-test` target therefore reads the code and says in words
which happened — otherwise the baseline stops meaning anything through `make`.

---

## The order, and why

This is the order the gate resolves in, and the order to read a failed run in.
**Steps 1–3 happen in CI on your push; you do not invoke them.** Steps 4–6 are
operator actions against a deployment or a browser and are invoked by a person
who has one.

1. **`make lint`** first. It is seconds, and a shellcheck failure or a broken doc
   anchor invalidates nothing else you are about to read. In CI this is spread
   across `application.yml`'s `shellcheck` and `kubernetes manifests` jobs and
   `terraform.yml`'s `fmt / validate / tflint`.
2. **`make test`** second, as its **own step**, with its exit code read — which
   is exactly what a workflow job is, and part of why the gate moved there. Six
   things have blocked this gate in one session before and five of them were
   reported as "the last one"; a run whose jobs each report separately cannot
   collapse six failures into one sentence.
3. **`make ui-component-test`** is already inside `make test`, and is
   `application.yml`'s `swarm-ui typecheck / component tests` job.
4. **`make ui-test`** when you touched the UI. It takes minutes and needs a
   browser, which is exactly why it is not in the gate: a cheap gate is one
   nobody is tempted to skip, and the first time Chromium was missing somebody
   would add a skip guard — and a guard that turns "I could not find the
   browser" into a green run is the failure this whole document is about.
5. **`make verify-remote`** when you touched admission, dispatch or
   reconciliation. This is the only step that proves the seams against a real
   deployment, and `release.yml`'s `deploy and smoke` job is where it runs on
   the way to an environment.
6. **`make bench`** last, and only when you want numbers. It is read-only and
   creates nothing, but it compares against a baseline that has to be believed
   before it is recorded.

The order is cheapest-and-most-decisive first. Each step's failures make the
later steps' results meaningless, so reading them out of order mostly produces
confusing evidence.

---

## How to read a benchmark result

Full detail is in [`docs/benchmarks.md`](benchmarks.md). The part an operator
must not get wrong:

### NOT MEASURED is not a zero

A timing that could not be taken arrives as `{"value": null, "not_measured":
"<reason>"}`. It is counted in `missing`, it is **never** counted as `0`, and a
metric whose samples are all missing is reported as `not_measured` and **fails
the gate**.

This is the single most likely way a benchmark lies, and it is this repository's
own recurring defect in another costume: an unread value falling into a
comparison that is true for every bound. A failed read that became `0` would be
recorded as the fastest run ever measured, and the next run would then be a
"regression" against it. So an unmeasured figure fails loudly instead.

Concretely: a fixture UI issues no network requests, so `bench-ui` records the
API metrics as not-measured **with that reason** rather than omitting them.
Omitting them would make a fixture run indistinguishable from a very fast live
one.

### What a verdict line looks like

```
ok    reconcile.duration_seconds{dry_run=false}     p95=12.835s  n=50  missing=0
      new: no comparable baseline; recorded for the next run
FAIL  reconcile.backend_errors{dry_run=false}       p95=1errors  n=50  missing=0
      over_absolute_max: p95=1.0 exceeds the absolute limit 0
```

Read four things: the **status**, the **percentile**, **n**, and **missing**. A
verdict with a large `missing` is a verdict about very little, regardless of how
good the percentile looks.

### Three more rules that change how you read the output

* **No mean, ever.** p50/p95/p99 and n. There is deliberately no `mean` field,
  because a mean hides the tail — and the number here that most needs its tail
  read is Cloud Run cold start, three minutes against eighteen seconds of agent.
  A field nobody may gate on is one somebody will quote, so it is not emitted.
* **A percentile from two samples is a lie.** `min_samples` is part of the
  threshold; below it a metric fails as `insufficient_samples` rather than
  passing on a p99 that is just the larger of a pair.
* **No baseline means nothing was compared.** The run says so, loudly:
  `NOTHING WAS COMPARED`. Recording a baseline asserts "this is what normal
  looks like" — read the diff before committing one, because a cold cache, a
  busy platform or a half-deployed revision all produce numbers every honest run
  afterwards will fail against.

### What each benchmark costs

| Suite | Time | Money | Creates anything? |
|---|---|---|---|
| `api` | ~30s | under a cent | no — GETs only |
| `reconcile` | ~10s | under a cent | no — it reads logs, it does not trigger a pass |
| `cost` | ~15s | under a cent | no — reads history |
| `ui` | ~1m | free | no — drives a browser at a UI you are running |
| `dispatch` | ~2m on `mock` | cents | **submits tasks**, cancels them on exit |
| `admission` | ~3m on `mock` | cents | **submits a 150-task backlog**, cancels on exit |

`make bench` runs the read-only three (`api`, `reconcile`, `cost`), so the
default is safe against production. `dispatch` and `admission` on
`--profile claude-code` **spend real provider tokens**, because they run an
actual agent — worth doing deliberately with a small `--count`, not per commit.

---

## The defect class this exists to catch

Over three days this platform produced a long run of defects that **every
existing test missed**, because each sat on a path nothing executed. They share
one shape: **both ends built, the seam between them never executed.**

These are the real ones, and each is why some suite above is shaped the way it
is:

* **`runners/cliagent.py` never put `SWARM_ARTIFACTS_DIR` in the agent child
  environment.** So no `claude-code` agent could write an artifact, so
  `input_from` could never stage anything, so work could not pass between agents
  — the platform's headline feature. The attempt still reported **SUCCEEDED**,
  because writing an artifact is not a success condition, and
  `result_summary.artifacts` was never empty because the *runner* writes its own
  stdout/stderr there. Caught now by asserting on the **bytes** that reach the
  downstream workspace, and by a real agent child in
  `tests/unit/worker/test_agent_seam_end_to_end.py`.
* **`attempt_from_dict` dropped all five spend fields**, so every cost and token
  figure in the product was unreachable. A passing test asserted they are `None`
  when **absent** — an assertion the bug satisfies — and nothing asserted they
  are present when present. Caught now by `tests/unit/control_plane/test_api_contract_shapes.py`,
  which asserts both directions for every decoder over every field, derived from
  the dataclass rather than typed out.
* **`BrokerClient._call` was called with a keyword it does not accept**, so every
  account registration 500d. Every test faked the account pool *above* that
  layer.
* **`app.state.token_endpoint` was read by a handler and assigned by nothing.**
  The test suite for that route set it in its **own fixture**, so the suite
  passed while production 500d on every request.
* **Nothing in the repository ever advanced a workflow state**, so every workflow
  stayed QUEUED forever while its steps said succeeded, failed and cancelled.
* **The metadata server path said `service_accounts`** where the server serves
  `service-accounts`, so the in-VPC gate could never authenticate.

### What follows from that

A suite that keeps mocking the seam reproduces the blind spot exactly. So:

* **Do not mock what the product owns.** The browser suite serves the real
  production bundle over a real socket. The fixtures call the real serialisers
  rather than restating them.
* **Assert both directions.** Present-when-present *and* absent-when-absent. Half
  of a pair of assertions is not a weaker test; it is a test that cannot fail on
  the bug it is about.
* **Assert on the artifact, not the status.** `SUCCEEDED` was true throughout the
  outage. The bytes were not.
* **A test that cannot fail is not a test.** For every check added here, the
  thing it covers was mutated and the check confirmed to catch the mutation.
* **Never let "could not run" become a pass.** No suite here carries an "if the
  directory exists" guard. A guard that turns a missing suite into a green run is
  a gate that passes hardest when it is most broken — which is how
  `tests/integration` rotted for 95 commits while being in no target at all, and
  how the terraform suite sat outside every gate.

That last point is why `make test` has no skip guards, why `ui-test` exits `3`
rather than `0` when it cannot run, and why `ui-component-test` treats a missing
suite file as a **failure** while treating a missing `node` as the one loud skip.
