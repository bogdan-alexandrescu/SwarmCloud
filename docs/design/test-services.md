# Test services: a repository names the services its tests need, and the pre-publish check runs them

**Status: designed 2026-10-23 (lane TEST-SERVICES-DESIGN, part of #1627),
decided the same day (§10), and built by TS-IMAGE, TS-CATALOGUE, TS-RUN,
TS-CLOUDRUN-REFUSE and TS-GATE. §11 records what each lane built and where
the code departs from this design; TS-ACCEPT and TS-SIGNAL are not built.**
Recorded by the owner as an approved follow-up of #1605 on 2026-10-19. The
repository-facing page on writing the declaration is
[../test-services.md](../test-services.md).

The request (#1627), as its author wrote it:

> A repository declares the services its tests need, and the pre-publish check
> starts them, points the tests at them, and runs the tests for real. For
> example, a checked-in file such as `.swarm/test-services.yml` with
> `postgres: {version: 16, database: app_test}`. The declaration names a
> service from a platform catalogue, never an image or a command
> (invariant 10), and the service fits inside the runner's fixed resources
> (invariant 7).

and how it would be known to work:

> A repository with a declared Postgres and a test that writes to it: an
> agent's change that breaks that test is caught by the pre-publish check
> (status `failed`, a fix round) before the PR opens. The same repository
> without the declaration still reports `skipped`.

Every file and symbol cited below was read on 2026-10-23 at `9aa7483`. Lines
move, so each is cited by symbol or by the exact text it points at.

What the design settles, in one line each:

* **The declaration is `.swarm/test-services.toml`**, checked into the
  repository: a service NAME from the platform catalogue, a major version the
  catalogue offers, and the names of the databases to create. No image, no
  command, no port, no resource figure; an unknown key or name is refused
  with the reason (§2). TOML rather than the issue's YAML because the worker
  reads it with the standard library and YAML would be its first parser
  dependency (Q2).
* **The catalogue is one service in v1: PostgreSQL 16**, the only service any
  repository SwarmCloud serves has been measured needing (§3). It lives in the
  worker, beside the binaries the image carries, so it is **not** a frozen
  contract change under the recommended option (§3.3).
* **The service runs as a child process of the worker, from binaries in
  `agent-runtime-base`** (§4, option A), inside the worker container's
  existing memory and CPU, its data on the attempt's own scratch volume,
  listening on loopback only, gone with the pod. A native sidecar (option B)
  is the stronger memory boundary and the recommended next step if option A's
  memory measurements say so; a separate per-attempt service (option C) is
  rejected.
* **Tests find it where they already look:** `127.0.0.1:5432`, plus the
  standard libpq variables and `DATABASE_URL` in the checks' environment
  (§5). A test that fails against a running declared service is `failed` and
  gets the fix round; an address nobody declared stays `skipped` exactly as
  PR 1606 made it (§5.3).

---

## 1. Inventory: what exists today

### 1.1 The check

The pre-publish gate (`apps/agent-worker/agent_worker/prepublish.py`, its
module docstring, SPEED-D1) runs after the agent's turn ends, from
`apps/agent-worker/agent_worker/runners/cliagent.py::run_prepublish_gate`.
One of its checks, `graph-pytest`, is built by
`apps/agent-worker/agent_worker/prepublish.py::_graph_pytest`: the test files
the graph says cover the change, run as one `uv run pytest -q -n auto
--junitxml=<report>` per Python project
(`apps/agent-worker/agent_worker/prepublish.py::_pytest_project`).

Since PR 1606 (#1605), a failed run whose junitxml report says every test
errored at setup or collection with a refused connection to a LOCAL address,
and no test passed or failed on its own, is reported `skipped` with "a
service the tests need is unreachable (127.0.0.1:5432); every selected test
errored at setup connecting to it; CI runs it". The decision is
`apps/agent-worker/agent_worker/prepublish.py::unreachable_services`, and it
is deliberately conservative: one assertion failure, one pass, an
ImportError or a non-local address keeps the check `failed`.

The checks run with the agent's environment minus its provider credential
(`apps/agent-worker/agent_worker/prepublish.py::check_env`). Every check that
runs pytest -- `graph-pytest` and `scripts/changed-guards.sh` when the
repository has one -- inherits that environment, so whatever this design adds
to it reaches both.

### 1.2 What the evidence says repositories need

| repository | service | evidence |
|---|---|---|
| product-oracle | PostgreSQL, at `localhost:5432` by default (`ORACLE_TEST_DATABASE_URL`, `api/tests/conftest.py` migrates into it) | #1605: four lanes on 2026-10-19 (task_11c00e381a634e03b7b3, task_523c25d8f2384576b9ae, task_cf4b712bde6d4b3b89e1, task_b396fea765b1489f9978), every selected test `Connect call failed ('127.0.0.1', 5432)`; re-run against a local Postgres 16 the same files pass (95/95, 113/113, 118/118) |
| sagaxyz/ai-studio | none recorded | its pre-publish findings in this repository (PREPUB-UV-OUTSIDE, #1964) are about uv, not a service |
| SwarmCloud itself | the Firestore emulator, in `tests/integration` only | CI's integration job; the unit tests run with no emulator by rule (CLAUDE.md, "Writing Python"), and `graph-pytest` runs unit tests |

Redis, MySQL, MongoDB, Kafka, the Firestore emulator for a tenant: **no
issue, incident or pre-publish record names any of them**: on 2026-10-23
`grep -rlw -i redis docs apps tests` found one hit, a rejected design option
in `docs/design/live-browser.md`, and the worker's pre-publish tests name
only `5432`. The
product-oracle repository itself could not be read from this lane (the
platform's private-read tool refused it: not on this tenant's readable
list), so its CI's service list is not quoted here; #1605's reproduction is
the evidence. **v1 is Postgres alone**; a second service is added when a
repository's skipped check names its address (§3.2).

### 1.3 The runner's fixed resources

`apps/common/swarm_common/profiles.py::RESOURCE_CLASSES` (FROZEN, read only):

| class | CPU request / limit | memory = limit | workspace | profiles that run the gate |
|---|---|---|---|---|
| standard | 4 / 8 | 8 GiB | 4 GiB | claude-code |
| browser | 4 / 8 | 8 GiB | 4 GiB | claude-code-browser |
| large | 8 / 16 | 32 GiB | 16 GiB | claude-code-large |

Memory is request AND limit: "memory is not compressible, and a memory burst
is a kill" (the `ResourceClass` docstring). CPU may burst to twice the
request on GKE Autopilot (contract request 108,
`apps/common/swarm_common/profiles.py::CPU_BURST_MAX_RATIO`). Every profile
above runs on GKE Autopilot; claude-code-review, the one claude-code-runner
profile on Cloud Run Jobs, publishes no branch of its own.

**The standard class is already tight on memory.** #1926 measured 8 GiB
out-of-memory kills on `claude-code` on 2026-10-22, which is why
`claude-code-large` exists (contract request 113). Whatever a test service
costs comes out of the same 8 GiB, so §4 budgets it in MiB, not GiB, and
makes the service, not the agent, the process the kernel kills first.

### 1.4 The pod

On GKE the worker container has `readOnlyRootFilesystem`; it writes only to
its volumes: the workspace, `/tmp`, its HOME, and the per-pod network disk
`apps/scheduler/scheduler/dispatch.py::CACHE_MOUNT` (`/cache`), under which
the worker puts the attempt's tool caches and TMPDIR
(`apps/agent-worker/agent_worker/cachedirs.py::CACHE_ROOT_ENV`). That disk is
a PVC created with the pod and deleted with it; it is never checkpointed and
never counted against the pod's ephemeral-storage limit (CACHE-PVC). It is
the natural home for a test database's data directory: disk, not memory, and
destroyed with the attempt.

The pod already has a precedent for a second container that shares the
class: the live-browser `display` native sidecar
(`apps/scheduler/scheduler/dispatch.py::with_live_display`,
`apps/scheduler/scheduler/dispatch.py::live_split_for`), whose requests and
memory are carved out of the class so admission's units and the bill do not
change. §4.2 reuses that pattern.

### 1.5 The image and its scan

`images/agent-runtime-base/Dockerfile` is `python:3.11-slim-bookworm`, an
`apt-get upgrade` layer on every build (`APT_REFRESH`, so Debian's fixes land
without a base bump), the agent CLIs and a toolbox, running as
`images/agent-runtime-base/Dockerfile` (`USER swarm:swarm`). It is about
781 MB (`docs/browser-startup-speed.md`) to ~900 MB compressed
(`docs/ci.md`). `scripts/push-images.sh` refuses an image with any fixable
HIGH or CRITICAL trivy finding, and the gate refuses the whole image, not
the tool: that is why tflint and tofu are not in it
(`images/agent-runtime-base/Dockerfile` (`ARG INSTALL_TFLINT=0`): every
release carries Go modules with fixed HIGH advisories). A service added to
the image takes on the same gate.

---

## 2. The declaration

### 2.1 The file

`.swarm/test-services.toml` at the repository root, checked in:

```toml
# What this repository's tests need. Names only: the platform owns the image,
# the command, the port and the resources (SwarmCloud invariant 10).
[postgres]
version = 16
databases = ["app_test"]
```

That is the whole v1 schema:

| key | type | rule |
|---|---|---|
| table name | a catalogue service name | one of the catalogue's names (§3); any other table is refused |
| `version` | integer | a major the catalogue offers for that service (v1: `16`) |
| `databases` | list of 1-8 strings | each `[a-z_][a-z0-9_]{0,62}`; created empty, owned by the service's user |

Nothing else is accepted. In particular there is no `image`, `command`,
`args`, `port`, `env`, `memory`, `cpu`, `settings`, `init_sql` or `user` key:
each is a way for a repository to choose what runs or how big it is
(invariant 10) or what the platform's process does at start. A repository
whose tests need a schema, an extension or seed data creates it in its own
fixtures, exactly as product-oracle's conftest already migrates.

### 2.2 Validation and what an invalid file does

The worker parses the file with `tomllib` (Python 3.11's standard library)
and validates it against the catalogue: unknown table, unknown key, a
version the catalogue does not offer, a malformed or duplicated database
name, a file over 4 KiB, or a TOML syntax error. Every refusal is collected,
not just the first, and each names the key and the rule, the way
`check_size` reports a runner size.

An invalid declaration never starts anything and never stops the gate:

* **If the agent's diff touched `.swarm/test-services.toml`**, the gate adds a
  check `test-services` with status `failed` and the refusals as its output.
  It is the agent's own error, so it goes into the one fix turn like any
  other failed check.
* **Otherwise** the file came from the base branch and the agent cannot be
  blamed for it: `test-services` is `error` with the refusals, recorded in
  the advisory section, never sent to the agent. The tests then run as if
  nothing were declared, so an unreachable service is `skipped` as today.

No file at all is the default and changes nothing: the gate behaves exactly
as it does on 2026-10-23.

### 2.3 Which copy is read

The gate reads the file from the working tree it is about to check, the tree
the publish commits (`worktree_tree` in the same module). An agent that adds
a declaration in its own change has it honoured in the same attempt; one
that deletes it has the service stop. Since a declaration only ever selects
a catalogue entry inside the attempt's own resources, reading the agent's
version gives the agent nothing it could not already do.

---

## 3. The catalogue

### 3.1 v1: PostgreSQL 16

One entry. Everything a repository cannot choose is fixed here:

| field | value | why |
|---|---|---|
| name | `postgres` | the name the declaration uses |
| versions | `16` | the version #1605's reproduction passed on; one major keeps one set of binaries in the image (Q3) |
| listen | `127.0.0.1:5432` and a Unix socket in the service's own directory | the address tests already default to (product-oracle: `localhost:5432`); loopback only, so nothing outside the pod can reach it |
| user | `postgres`, no password, `trust` authentication on loopback and the socket | no credential exists, so nothing can leak and nothing needs redaction; any password a repository's default URL carries is accepted |
| data | `<cache root>/<attempt>/services/postgres`, mode 0700 | disk, not memory, on GKE; destroyed with the pod (§1.4) |
| memory settings | `shared_buffers=64MB`, `work_mem=4MB`, `maintenance_work_mem=32MB`, `max_connections=50`, `max_wal_size=256MB` | a resident set measured, not guessed, in phase 1 (§7), held under the budget of §4 |
| durability | `fsync=off`, `synchronous_commit=off`, `full_page_writes=off` | throwaway test data: these cut the setup time of a migrating conftest and lose nothing a test needs |
| ready | `pg_isready` on the socket, every 100 ms, up to 30 s | a service that is not ready is an `error`, never a test failure (§5.3) |
| extensions | what the server package ships (the contrib set: `pgcrypto`, `uuid-ossp`, `citext`, `hstore`, `pg_trgm`, ...) | no extra package; `pgvector` and others are a catalogue change, not a declaration key |

### 3.2 Others, and the evidence that would add one

Redis is the obvious second candidate and is **not worth building now**: no
repository SwarmCloud serves has been recorded needing it (§1.2). The
signal that would add a service is already collected: every `skipped`
graph-pytest names the addresses it could not reach, in the run's
`prepublish` record. Phase 3 (§7) counts those by port; a port that recurs
across repositories is the next entry, each a catalogue row, a set of
binaries and a scan, never a declaration change.

### 3.3 Where it lives: not the frozen contract

The catalogue is read by exactly one component: the worker, inside the
container that carries the binaries it names. Nothing the API, the
scheduler or the console decides depends on it under option A (§4.1):
admission charges the class as it does today, and dispatch renders the same
pod. So the catalogue is a module of the worker,
`apps/agent-worker/agent_worker/testservices.py`, shipped in the same image as
the binaries it points at, so the two cannot disagree. **No change to
`apps/common/swarm_common/` is requested** under the recommendation.

That changes under option B (§4.2): a sidecar must be known when the pod is
rendered, so the scheduler, and therefore the frozen contract, must hold the
catalogue's names and their shares. The request that would then be filed is
§8, written out so the owner can see its cost; it is not filed in
`docs/contract-change-requests.md` unless Q1 picks B.

---

## 4. Running it inside fixed resources (invariant 7)

Three ways, judged on the same questions. Figures marked *est.* are
estimates this lane could not measure (it builds nothing); phase 1 measures
them before anything ships, and the build stops if a measurement breaks
the budget.

### 4.1 Option A: the binaries in `agent-runtime-base`, a child process of the worker (recommended)

The image installs the PostgreSQL 16 server from the PostgreSQL project's
Debian repository (PGDG; bookworm's own archive carries only 15), signing
key pinned by fingerprint, in the same `apt-get upgrade` layer, so
`APT_REFRESH` picks up every minor release on the next build. The worker
lifecycle (never the agent, never a command from the repository) starts
`initdb` and `postgres` as its own child, as the worker's uid, when the gate
finds a valid declaration, and stops it, then removes the data directory,
when the gate ends or the attempt is torn down.

* **Resources.** Taken from the worker container's existing 8 GiB and 4-8
  CPU: nothing is added to the pod, so admission, units, the bill and
  `requests == limits` are untouched. Budget: **at most 512 MiB resident**
  (*est.* 100-250 MiB for the server with the settings above and 2x CPU
  test backends, phase 1 measures it under product-oracle's three
  reproduction files). CPU: shared and burstable to the class limit, which
  is the throttle, never a kill.
* **The memory risk, and its mitigation.** Inside one container a
  Postgres that grows takes memory from the agent, and the cgroup's OOM
  killer then picks a victim among all of them. The worker writes `1000`
  to the service's `/proc/<pid>/oom_score_adj` (raising one's own or a
  child's score needs no privilege), so the kernel kills the Postgres
  first, the check reports `error` ("the declared postgres was killed"),
  and the agent and the worker live. This is a ranking, not a boundary:
  it is why option B exists (Q1).
* **Start time.** *est.* 1-3 s for `initdb` plus server start on the
  cache disk, paid once per gate round (or once per attempt under Q4's
  option b), against the gate's 600 s per-check limit
  (`apps/agent-worker/agent_worker/prepublish.py::CHECK_TIMEOUT_SECONDS`).
* **Image size and scan.** *est.* +60-90 MB compressed: the server, its
  libraries (ICU, libxml2, LLVM for JIT; the JIT dependency is the larger
  part, and phase 1 drops it if the package allows). Postgres is C from a
  Debian-format repository: no Go modules, the class of finding that keeps
  tflint out. A fixable HIGH on the server would refuse the image like any
  other, and the fix is the next `APT_REFRESH` build. **One honest gap:**
  trivy matches Debian packages against Debian's security tracker, which
  does not track PGDG's `postgresql-16` (bookworm has no such package); its
  findings would be under-reported, not over-reported. Phase 1 records what
  trivy says about the package, and the build adds a pinned minimum version
  the image check refuses below.
* **Cloud Run vs GKE.** Works on both. On GKE the data directory is on the
  cache PVC; on Cloud Run (no second disk) it would be in the tmpfs, so
  charged to memory: the worker uses a 256 MiB cap there and refuses (`error`)
  above it. No profile that runs the gate is on Cloud Run today (§1.3).
  **Decided (BGD, 2026-10-24): refuse on Cloud Run.** With no `SWARM_CACHE_ROOT` the worker starts no declared service and reports each as "service not started: test services need SWARM_CACHE_ROOT on its own volume; this runner keeps its workspace in memory" (`testservices.NO_CACHE_VOLUME`, TS-CLOUDRUN-REFUSE).
* **Isolation.** One attempt is one pod. The server listens on loopback
  and a 0700 socket directory; the pod's network namespace is its own and
  the tenant ingress policy admits nothing to it; the data is on a disk
  created and deleted with that pod. No attempt's data can reach another's,
  and no other tenant's pod can connect. Within the attempt, the agent runs
  as the same uid as the service and can read its data: the boundary is the
  attempt, not the process (as in `docs/design/workspace-secrets.md` §3.5),
  and the data is the attempt's own test data.
* **Every image pays.** The binaries are in every `agent-runtime-base` pull,
  used or not, including codex and the indexer. That is the cost Q1 weighs.

### 4.2 Option B: a native sidecar in the GKE pod

A second container, `postgres`, rendered by the scheduler as a native
sidecar (an init container with `restartPolicy: Always`), exactly as
`with_live_display` renders `display`, from a platform-built image
(`agent-runtime-postgres`, built from the same mirrored Debian base and
scanned by the same gate) or the mirrored official `postgres:16-bookworm`.

* **Resources.** Carved from the class like the live split: e.g. 0.5 CPU
  request / 1 limit and 1 GiB memory to the sidecar, the rest to the worker,
  requests summing to the class so units and the bill do not change. **A
  real boundary**: the sidecar's OOM kills only the sidecar. But it is
  reserved for the whole attempt, used or not, so every standard attempt of
  a declaring repository runs with 7 GiB for the agent, in a class #1926
  already found too small.
* **The declaration must be known at dispatch.** The pod is rendered before
  the clone, so the scheduler cannot read the checked-in file. Either the
  repository index records it when it indexes the base (a step's
  declaration is then the base's, never the agent's change), or the
  submission carries it. Both need the scheduler to know the catalogue: the
  frozen-contract change of §8, plus a scheduler and an indexer lane.
* **Start time.** Starts in parallel with the worker's clone; the image
  pull is the cost (*est.* 150 MB compressed for the official image, less
  for a platform build), cached per node after the first.
* **Image size and scan.** A new image in the promote gate. The official
  image carries `gosu`, a Go binary, whose standard-library advisories are
  the class of finding that keeps tflint out; a platform-built image without
  it is a new Dockerfile to own.
* **Cloud Run vs GKE.** GKE only in this design. Cloud Run Jobs' support for
  multi-container executions was not verified for this design; no gate
  profile is on Cloud Run, so it is not needed.
* **Isolation.** As A: one pod, loopback shared only between its own
  containers, the data in the sidecar's own volume. Stronger within the
  attempt: a different uid, as `display` has.

### 4.3 Option C: a separate per-attempt service

A Cloud SQL instance, or a pod in the tenant namespace, per attempt; or a
long-lived per-tenant server with a database per attempt.

* **Resources.** Outside the runner's class entirely: capacity the
  reservation never counted, so infrastructure demand that admission does
  not see (against invariants 1 and 2), or a new pool with its own
  all-or-nothing accounting.
* **Start time.** Cloud SQL: minutes per instance (*est.* 5-10), longer
  than the check. A pod: tens of seconds plus scheduling, and a second
  object to fence and reap.
* **Image and scan.** As B, or none for Cloud SQL.
* **Cloud Run vs GKE.** Either, but a network path from the runner,
  NetworkPolicy, and for Cloud SQL a credential and an IAM grant per tenant.
* **Isolation.** The weakest: the service is reachable over the network,
  needs a credential that can leak, and a shared server holds several
  attempts' data with only database roles between them.

**Rejected**: it costs most, isolates least, and starts slowest.

### 4.4 Summary

| | A: binary, child process | B: native sidecar | C: separate service |
|---|---|---|---|
| CPU / memory | shared inside the class; 512 MiB budget; OOM kills the service first (ranking) | carved from the class for the whole attempt; own OOM boundary | outside the class (unaccounted) |
| start | *est.* 1-3 s, per use | parallel with clone; image pull | minutes (Cloud SQL) |
| image | *est.* +60-90 MB on every base pull | new image, *est.* 150 MB | none or new |
| scan | Debian-format C packages; PGDG tracking gap (§4.1) | official image's `gosu` risk, or a new Dockerfile | none / as B |
| Cloud Run | yes (tmpfs-capped) | not relied on | yes |
| GKE | yes | yes | yes |
| isolation | pod + loopback + per-pod disk; same uid as agent | pod + loopback + own volume; own uid | network, credential, shared server |
| contract change | none | §8 | new pool |
| declaration read | the working tree, at the gate | the base, at dispatch | either |

---

## 5. Wiring the tests

### 5.1 The environment

When a declared service is ready, the gate adds to the checks' environment
(`check_env`'s output; never the gate's own, which the agent's session
continues with):

| variable | value |
|---|---|
| `PGHOST` | `127.0.0.1` |
| `PGPORT` | `5432` |
| `PGUSER` | `postgres` |
| `PGDATABASE` | the first declared database |
| `DATABASE_URL` | `postgresql://postgres@127.0.0.1:5432/<first database>` |
| `SWARM_TEST_SERVICES` | `postgres` (the services started, comma-separated, so a conftest can tell) |

A repository whose tests read their own variable (product-oracle's
`ORACLE_TEST_DATABASE_URL`) is served by the default address, which is
what that variable defaults to. Letting the declaration name extra variable
names to set is Q5. No password is in any value (§3.1), so nothing here is
secret and `redact` has nothing to find; the values are still logged only
as variable names.

### 5.2 The order inside the gate

1. Read and validate the declaration (§2.2). Invalid: the `test-services`
   check, as §2.2 says.
2. If any selected check can run pytest (`graph-pytest`, or the guards
   script), start the declared services and wait for ready. A service that
   does not start or become ready is `test-services: error` with its last
   log lines (scrubbed by `clean_output`), and the tests run without it.
3. Run the checks with the environment above.
4. After each pytest check, probe the service: alive, or dead and why
   (exit status, OOM).
5. Stop the services and remove their data after the round; the fix round,
   if any, starts them afresh, so it never sees the first round's data.

### 5.3 `skipped`, `failed`, `error` once a service is declared

| what happened | status | fix round |
|---|---|---|
| nothing declared; every test refused at a local address | `skipped` (today's text, PR 1606) | no |
| declared and ready; any test fails or errors | `failed` | yes |
| declared and ready; every test refused at an address **no declared service owns** (e.g. `:6379`) | `skipped`, naming the address and adding "not declared in .swarm/test-services.toml" | no |
| declared and ready; every test refused at the declared address, service alive after the run | `failed`: the service was there, so the tests connected wrongly | yes |
| declared; the service died during the run (OOM, crash) | `error`: "the declared postgres stopped during the run (<why>)" | no |
| declared; did not start or not ready in 30 s | `test-services: error`; the tests run without it, so `skipped` as row 1 | no |
| declaration invalid, touched by the diff | `test-services: failed` | yes |
| declaration invalid, from the base | `test-services: error`, advisory only | no |

`unreachable_services` stays the one judge of "every test was refused";
what changes is that its addresses are first matched against the declared
services' addresses. The issue's acceptance case is rows 2 and 1: the
declaring repository's broken test is `failed` with a fix round; the same
repository without the file is `skipped`.

### 5.4 During the agent's turn

The gate is what #1627 asks for, but the agent also runs tests while it
works (CLAUDE.md asks a SwarmCloud agent to). Without the service its own
run errors the same way and it learns nothing until the gate. Starting the
service before the turn, for the whole attempt, lets the agent see the
failure itself and is what makes the fix round's rerun meaningful; it holds
the service's memory for the whole attempt. Q4.

### 5.5 Checkpoint, resume, park

The data is never checkpointed (it is under the cache root, not the
workspace). A resumed or re-dispatched attempt starts an empty service;
tests that need a schema create it, as product-oracle's do. A parked
attempt exits, and the pod, the disk and the data go with it (invariant 4).

---

## 6. Invariants

1. **Demand only from leased states.** Nothing is created outside the pod;
   the service lives and dies inside a `RUNNING` attempt. Option C is
   rejected partly on this.
2. **All-or-nothing reservation.** Unchanged: no new pool, the class is
   charged as today.
3. **Concurrency from `LEASED`.** Unchanged.
4. **No sleeping through provider waits.** The service is stopped before
   a park; the ready wait is bounded at 30 s.
5. **Fencing.** A stale worker exits before running the agent, so before
   any gate, so before any service.
6. **Spot disabled.** Unchanged.
7. **`requests == limits`.** Option A adds nothing to the pod; option B
   splits the class with memory request == limit on each container.
8. **Checkpointing.** Unchanged; the service's data is deliberately outside
   it.
9. **Per-tenant isolation.** One pod, loopback, a per-pod disk, no
   credential (§4.1).
10. **Names, never images or commands.** The declaration is a service
    name, a catalogue version and database names (§2.1).

---

## 7. Cost and the build plan

Each lane owns the files named; two lanes that touch one file are one lane
(CLAUDE.md, "Issues"). Every lane pushes its tests first to the PR branch
and its fix to `<branch>-fix`, and runs `scripts/changed-guards.sh` before
finishing. This is option A with Q2-Q5's recommendations; §8 lists what B
would add.

| phase | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 1 | **TS-IMAGE** | `images/agent-runtime-base/Dockerfile`, `images/agent-runtime-base/tool-mirror.sha256` if the key or packages go through the tool mirror, `tests/unit/scripts/test_agent_toolbox_image.py` | the image's tool check lists `postgres` and `initdb` at major 16; the PGDG key is pinned by fingerprint, not fetched unverified; the server package sits in the `apt-get upgrade` layer. The lane's PR body records the measured image growth and trivy's output on the package, and stops if a fixable HIGH remains |
| 1 | **TS-CATALOGUE** | `apps/agent-worker/agent_worker/testservices.py` (new), `tests/unit/worker/test_testservices_declaration.py` (new) | a valid file parses; each refused key (`image`, `command`, `port`, `memory`, `env`, `user`) is refused by name; an unknown table, version 15, a bad or duplicated database name, a 5 KiB file and a TOML syntax error are each refused, all refusals reported together |
| 2 | **TS-RUN** | `testservices.py` (same file as TS-CATALOGUE: runs after it merges), `tests/unit/worker/test_testservices_process.py` (new) | with a fake runner: `initdb` then `postgres` are started with the catalogue's argv and data under the cache root; the child's `oom_score_adj` is 1000; not ready in the bound is an error with scrubbed log lines; stop removes the data directory; nothing listens on a non-loopback address |
| 3 | **TS-GATE** | `apps/agent-worker/agent_worker/prepublish.py` (module docstring, `select_checks`, `_pytest_project`, `unreachable_services`'s caller), `tests/unit/worker/test_prepublish_test_services.py` (new) | each row of §5.3 with a fake runner and junitxml reports built in the test; the environment of §5.1 reaches the checks and never the gate's own; the issue's acceptance pair: declared + a failing test is `failed` with a fix round, the same tree without the file is `skipped` |
| 3 | **TS-ACCEPT** | a fixture repository under `tests/acceptance/` with a declared Postgres and one writing test (after TS-IMAGE and TS-GATE merge) | on a deployed dev worker, a lane that breaks the test reports `graph-pytest: failed` before its PR opens |
| 4 | **TS-DOCS** | this file ("What X built" sections), `docs/agent-output.md` where it describes the pre-publish checks, a repository-facing page on writing the declaration | `test_docs_describe_what_was_built` |
| later | **TS-SIGNAL** | the pre-publish record's skipped addresses counted by port (a read in an existing report) | a skipped run's addresses are counted; a declared address is never counted |

TS-CATALOGUE and TS-RUN share `testservices.py` and could be one lane; they
are split only because TS-RUN needs TS-IMAGE's binaries to be measurable and
TS-CATALOGUE does not. If they are scheduled together, they are one lane.

**Cost.** Five build lanes and a docs lane. Running: *est.* 60-90 MB more
per base image pull; *est.* 1-3 s and up to 512 MiB per gate round of a
declaring repository; nothing at all for a repository that declares nothing.
What it saves: one CI cycle and one CI-driven fix round per lane whose
database test breaks (four lanes on product-oracle on 2026-10-19 alone).

---

## 8. Contract change request (only if Q1 picks B)

Not filed. If the owner picks option B, this is the request that lane
TS-CONTRACT would add to `docs/contract-change-requests.md`, in its form.

**`profiles.py`: a test-service catalogue and a per-class share**

*What is true today.* `apps/common/swarm_common/profiles.py` names every
container the scheduler may render: the worker, and the live-browser
`display` split (`LiveShare`, `ResourceSize.live_split`). Nothing names a
service container.

*The requested change.* A frozen `TEST_SERVICES` mapping, like
`apps/common/swarm_common/profiles.py::RUNTIME_TOOLS`: each entry a name, the
majors offered, the image name (platform-built, by name, never a caller's),
and per class a `LiveShare`-shaped (cpu request, cpu limit, memory GiB)
share; `check_size` extended so a size leaves the worker its minimum with
the share taken. And on the repository record the indexer writes, the
validated declaration of the base.

*What it would break if accepted.* Nothing that exists; the scheduler, the
indexer and the size validator each gain a reader. Every declaring
repository's attempts run with the share taken from the worker.

*If it is declined.* Option A, as recommended.

---

## 9. Owner questions

Each is also in this lane's `questions.json`.

**Q1. Where does the service run?**
(a) **A: binaries in `agent-runtime-base`, a child of the worker**
(recommended: no contract change, no dispatch change, nothing reserved when
unused; costs *est.* 60-90 MB on every base pull and an OOM ranking rather
than a boundary);
(b) B: a native sidecar (a real memory boundary; costs the §8 contract
change, a scheduler and an indexer lane, a new image, and the share held
for the whole attempt in a class already measured tight);
(c) C: a separate per-attempt service (rejected: outside the reservation,
minutes to start, a credential to leak).

**Q2. The declaration's format?**
(a) **TOML, `.swarm/test-services.toml`** (recommended: Python 3.11's
`tomllib`, no new dependency, no YAML type surprises such as `version: 16.0`
becoming a float);
(b) YAML, `.swarm/test-services.yml` as the issue suggests (adds PyYAML,
the worker's first parser dependency, to the image and the scan);
(c) a table in the repository's `pyproject.toml` (`[tool.swarm.test-services]`;
no new file, but only Python repositories have one, and a monorepo has several).

**Q3. Which Postgres versions in v1?**
(a) **16 only** (recommended: the one version the evidence ran; one set of
binaries);
(b) 15 from bookworm's own archive (no new apt source, Debian's security
tracking is complete; not the version the evidence used);
(c) 15, 16 and 17 (each major *est.* +40-60 MB more).

**Q4. When is the service up?**
(a) **for the whole attempt, started after the clone, so the agent's own
test runs reach it** (recommended: the agent finds the failure itself and
the fix round's rerun is real; holds up to 512 MiB the whole attempt);
(b) only during the gate's rounds (cheapest; the agent works blind to its
database tests, as today);
(c) on the agent's request through a worker tool (no idle memory; the
agent must know to ask, and a new tool surface).

**Q5. May the declaration name environment variables to set to the URL?**
(a) **no in v1: `DATABASE_URL`, `PG*` and the default address only**
(recommended: product-oracle's own variable defaults to the address; no
variable-name surface to validate);
(b) yes, a `url_env = ["ORACLE_TEST_DATABASE_URL"]` list, each name
`[A-Z][A-Z0-9_]{0,63}` and refused if it is a platform or loader variable
(`PATH`, `LD_*`, `PYTHON*`, `SWARM_*`, ...).

**Q6. Redis or another service in v1?**
(a) **no: add one when the skipped-address count (TS-SIGNAL) shows it
recurring** (recommended: no repository has been recorded needing one);
(b) Redis 7 now (*est.* +5 MB; a catalogue row and one more lane).

---

## 10. Owner decisions (2026-10-23, ~09:47 PDT)

BGD answered all six questions in Claude Code with the recommended option.
Each answer is recorded on the design agent (`task_2d20d9db23d941459f06`,
questions 0-5), so the console shows them as answered.

| question | decision |
|---|---|
| Q1 where the service runs | **A**: the binaries in `agent-runtime-base`, started as a child of the worker. No contract change; §8 is not filed. |
| Q2 the declaration's format | **TOML**, `.swarm/test-services.toml` |
| Q3 Postgres versions in v1 | **16 only** |
| Q4 when the service is up | **for the whole attempt**, started after the clone, so the agent's own test runs reach it |
| Q5 extra URL variables | **no in v1**: `DATABASE_URL`, `PG*` and the default address only |
| Q6 Redis or another service | **no**: one is added when TS-SIGNAL's skipped-address count shows it recurring |

The build follows §7's table as written: TS-IMAGE and TS-CATALOGUE, then
TS-RUN, then TS-GATE, then TS-ACCEPT and TS-DOCS. TS-SIGNAL comes later.

---

## 11. What was built

Read on 2026-10-24 from `main` at `5f5d511`, by TS-DOCS. Each entry names the
code that holds the lane's rule and the tests that hold it there, and says
where the code departs from §2-§5. Where the two disagree, the code is what
runs; the departures are listed in §11.8 so nobody "fixes" the code back to
the design.

### 11.1 What TS-IMAGE built

`agent-runtime-base` carries the PostgreSQL 16 server from PGDG, in the
`apt-get upgrade` layer, so `APT_REFRESH` brings every 16.x minor
(`images/agent-runtime-base/Dockerfile` (`ARG POSTGRES_MAJOR=16`)):

* the signing key is fetched over HTTPS and must hold exactly one primary
  key whose full fingerprint is `PGDG_KEY_FINGERPRINT` before apt trusts it;
  gpg is installed for that check alone and purged in the same `RUN`;
* the build starts nothing and creates no data directory:
  `create_main_cluster` is turned off before the server's postinst runs, and
  the build fails if `/var/lib/postgresql/16` or `/etc/postgresql/16`
  appears anyway;
* the archive floats, so the build refuses a server older than
  `images/agent-runtime-base/Dockerfile` (`ARG POSTGRES_MIN_VERSION=16.15`),
  the release measured when the lane went in;
* `postgres`, `initdb`, `pg_ctl` and `pg_isready` are linked into
  `/usr/local/bin`, and the toolbox smoke step fails the build on any of them
  that does not say `(PostgreSQL) 16.`;
* the PGDG source and keyring are removed once the server is in
  (`images/agent-runtime-base/Dockerfile` (`rm -f /etc/apt/sources.list.d/pgdg.list /usr/share/keyrings/pgdg.gpg`)),
  so the derived images' own `apt-get update` never depends on
  apt.postgresql.org; and ssl-cert's snakeoil pair, identical in every pull,
  is deleted, since the service listens on loopback without TLS.

The scan gap of §4.1 is recorded beside the layer: trivy does not track
PGDG's `postgresql-16`, so findings on the server are under-reported; its
libraries are Debian's and are tracked. The image growth and trivy's reading
were measured in the lane's pull request, not in the code, and are not
quoted here. Tests: the PGDG, fingerprint, layer and smoke-step tests in
`tests/unit/scripts/test_agent_toolbox_image.py`.

### 11.2 What TS-CATALOGUE built

`apps/agent-worker/agent_worker/testservices.py` reads
`apps/agent-worker/agent_worker/testservices.py::DECLARATION_PATH` from the
clone and holds it to
`apps/agent-worker/agent_worker/testservices.py::CATALOGUE`, one entry:
`postgres`, versions `(16,)`. A table may carry only
`apps/agent-worker/agent_worker/testservices.py::ALLOWED_KEYS` (`version`,
`databases`); the keys a repository is most likely to try are refused with a
reason of their own
(`apps/agent-worker/agent_worker/testservices.py::_REFUSED_BECAUSE`), and any
other key is refused as "not a declaration key".
`apps/agent-worker/agent_worker/testservices.py::validate` collects every
refusal, not the first: an unknown table, a table that is not a table, a
missing or unoffered `version` (a boolean is refused, not read as 1), a
`databases` that is not a list of 1 to
`apps/agent-worker/agent_worker/testservices.py::MAX_DATABASES` names each
matching `apps/agent-worker/agent_worker/testservices.py::DATABASE_NAME`, and
a duplicated name. `apps/agent-worker/agent_worker/testservices.py::parse_declaration`
adds the file over
`apps/agent-worker/agent_worker/testservices.py::MAX_DECLARATION_BYTES`,
bytes that are not UTF-8, and a TOML syntax error.
`apps/agent-worker/agent_worker/testservices.py::read_declaration` adds two
the design did not name: a declaration that resolves outside the clone (a
symlinked file or `.swarm`) and one that is not a regular file are refused
unread. A declaration with any refusal declares no service. Tests:
`tests/unit/worker/test_testservices_declaration.py`.

### 11.3 What TS-RUN built

`apps/agent-worker/agent_worker/testservices.py::start` runs a valid
declaration's services; `apps/agent-worker/agent_worker/testservices.py::_start_postgres`
is the one starter:

1. `initdb` (`apps/agent-worker/agent_worker/testservices.py::initdb_argv`:
   `--username=postgres`, `--auth=trust`, `--locale=C`, `--no-sync`) into
   `<cache>/services/postgres/data`, where `<cache>` is the attempt's cache
   directory, `<SWARM_CACHE_ROOT>/<attempt>`
   (`apps/agent-worker/agent_worker/workspace.py::create`), with the
   directory and its socket directory mode 0700;
2. the declared databases, created in single-user mode **before** the server
   listens, so no test sees the server without them; the three `initdb`
   makes itself are not created again
   (`apps/agent-worker/agent_worker/testservices.py::INITDB_DATABASES`);
3. `postgres` with `listen_addresses=127.0.0.1` and
   `apps/agent-worker/agent_worker/testservices.py::POSTGRES_SETTINGS`, §3.1's
   settings exactly, plus a 0700 socket and no logging collector;
   `apps/agent-worker/agent_worker/testservices.py::_loopback_only` refuses to
   start anything if the listen address were not loopback;
4. `oom_score_adj` set to
   `apps/agent-worker/agent_worker/testservices.py::OOM_SCORE_ADJ` (1000) on the
   server **before** readiness is awaited; a server whose rank cannot be set is
   not run, because the kernel would rank it beside the agent;
5. readiness by `pg_isready` on the socket every 0.1 s, up to
   `apps/agent-worker/agent_worker/testservices.py::READY_TIMEOUT_SECONDS`.

The child's environment is built from nothing
(`apps/agent-worker/agent_worker/testservices.py::child_env`): no worker
secret and no `PG*` variable reaches it. A step that fails is an error on that
service, never an exception: the service is stopped, its directory removed,
and the error carries the server's last 20 log lines through the worker's
scrub. `apps/agent-worker/agent_worker/testservices.py::ServiceProcess.stop`
sends SIGINT (PostgreSQL's fast shutdown), kills after 10 s, and removes the
directory.

The worker calls it once, right after the clone, from
`apps/agent-worker/agent_worker/lifecycle.py::Worker._start_test_services`,
with the lease heartbeated meanwhile, and stops it from
`apps/agent-worker/agent_worker/lifecycle.py::Worker._cleanup`, which every
exit reaches. Nothing there ends the attempt: no file starts nothing, an
invalid file is logged and starts nothing (the gate reports it), and a service
that does not come up is logged with its scrubbed error while the agent runs
without it. Tests: `tests/unit/worker/test_testservices_process.py`.

### 11.4 What TS-CLOUDRUN-REFUSE built

BGD's decision of 2026-10-24 (§4.1, "refuse on Cloud Run"). With no
`SWARM_CACHE_ROOT` in the environment `start` runs no `initdb` and gives each
declared service the error
`apps/agent-worker/agent_worker/testservices.py::NO_CACHE_VOLUME`, which
`apps/agent-worker/agent_worker/testservices.py::RunningServices.report`
shows as `postgres: service not started: test services need SWARM_CACHE_ROOT
on its own volume; this runner keeps its workspace in memory`. It is read
from the environment, not a profile name, so any runner without its own cache
volume refuses. The 256 MiB tmpfs cap §4.1 described is therefore not built:
there is no in-memory Postgres at all. Tests: the cache-volume tests in
`tests/unit/worker/test_testservices_process.py`.

### 11.5 What TS-GATE built

`apps/agent-worker/agent_worker/prepublish.py::Gate._services` runs §5.2's
steps 1-2 each round, and `apps/agent-worker/agent_worker/prepublish.py::Gate._round`
the rest:

* the declaration is read from the tree being checked. Invalid: a
  `test-services` check
  (`apps/agent-worker/agent_worker/prepublish.py::TEST_SERVICES_CHECK`),
  `failed` when the diff touched `.swarm/test-services.toml`, else `error`;
  nothing is started.
* only when a check in
  `apps/agent-worker/agent_worker/prepublish.py::SERVICE_CHECKS` (`graph-pytest`
  and the guards script) will run, each declared service is probed at its
  address (`apps/agent-worker/agent_worker/prepublish.py::probe_argv`:
  `pg_isready` over TCP on `127.0.0.1:5432`). One that answers is the
  worker's and is used as it is; one that does not is started by the gate for
  this round, under a `pp-svc-` directory in the checks' TMPDIR
  (`apps/agent-worker/agent_worker/prepublish.py::_outside`). One that does not
  start is `test-services: error`, and the tests run without it.
* the checks' environment, never the gate's own, gains
  `apps/agent-worker/agent_worker/prepublish.py::service_env`: `PGHOST`,
  `PGPORT`, `PGUSER`, `PGDATABASE`, `DATABASE_URL` and `SWARM_TEST_SERVICES`.
  When no service is ready, `SWARM_TEST_SERVICES` is removed from the checks'
  environment, so an agent cannot claim a service the gate did not find.
* each pytest check is wrapped by
  `apps/agent-worker/agent_worker/prepublish.py::watched`: a service that
  stopped during the run
  (`apps/agent-worker/agent_worker/prepublish.py::_RoundServices.stopped`)
  makes the check `error`, naming signal 9 as the OOM killer when the gate
  started the server.
* `apps/agent-worker/agent_worker/prepublish.py::_pytest_project` matches the
  refused addresses `unreachable_services` returns against the ports of the
  services named in `SWARM_TEST_SERVICES`
  (`apps/agent-worker/agent_worker/prepublish.py::declared_services`): one a
  running declared service owns is `failed` ("the tests connect to it
  wrongly"); any other is `skipped`, adding "and not declared in
  .swarm/test-services.toml".
* a `test-services` error is the base's or the platform's, not the branch's:
  `apps/agent-worker/agent_worker/prepublish.py::pr_section` leaves it out of
  **Pre-publish checks failed**, and
  `apps/agent-worker/agent_worker/prepublish.py::advisory_section` shows it as
  "test-services (error, not the change's)". The fix turn gets `failed`
  checks only, so an `error` is never sent to the agent.

Every row of §5.3 holds as written. Tests:
`tests/unit/worker/test_prepublish_test_services.py`, one test per row and the
issue's acceptance pair.

### 11.6 What TS-DOCS built

This section; the pre-publish part of
[../agent-output.md](../agent-output.md#before-the-worker-publishes-a-rebase-and-the-repositorys-own-checks-speed-d1);
and [../test-services.md](../test-services.md), the page a repository reads
to write its declaration.

### 11.7 Not built

* **TS-ACCEPT (not built).** No fixture under `tests/acceptance/` declares a
  service, so the deployed proof that a lane breaking a database test reports
  `graph-pytest: failed` before its pull request opens has not run. The
  offline acceptance pair in `tests/unit/worker/test_prepublish_test_services.py`
  is the only proof today.
* **TS-SIGNAL (not built).** Nothing counts skipped addresses by port yet;
  §3.2's rule for adding a second service has no data behind it.

### 11.8 Where the code departs from §2-§5

1. **The agent's turn gets the server, not the variables.** The worker starts
   the service before the agent (Q4), so the agent's own test runs reach
   `127.0.0.1:5432`; but `PGHOST`, `DATABASE_URL` and the rest are set only in
   the gate's checks' environment (§5.1 said so, §5.4 did not). A test that
   reads only `DATABASE_URL` and has no default finds it during the gate, not
   during the turn.
2. **The fix round does not always start afresh.** §5.2 step 5 said every
   round's services stop and the fix round never sees the first round's data.
   The gate stops only what it started; the worker's service is probed, never
   started or stopped by the gate, and keeps its data across the agent's turn,
   both gate rounds and the fix turn between them. A test that depends on an
   empty database must create its own state, as §5.5 already asks.
3. **The gate starts a service only when none answers** (a declaration the
   agent's change added, or a worker service that died), and its data
   directory is under the checks' TMPDIR, not `<cache>/services`.
4. **No in-memory fallback.** §4.1's 256 MiB tmpfs cap was replaced by
   TS-CLOUDRUN-REFUSE (§11.4).
5. **Two more refusals and one more stop.** The declaration is refused unread
   when it resolves outside the clone or is not a regular file (§11.2), and a
   server whose `oom_score_adj` cannot be set is not run (§11.3).
