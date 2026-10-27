# swarm-graph's answer latency: a scheduled p95 report, and an answer cache that survives merges

Status: **design, for the owner's review. Nothing here is built.** Part of
#1840. Written 2026-10-27 against `main` at `17d6c5a`.

#1840 measured this repository's graph answer p95 rising from 64-68 ms
(2026-10-16, 49,058 symbols) to 152-165 ms (2026-10-21, 58,930 symbols), with the
server's code held fixed and only the data changing. PR 1838 then moved the CI
gate (`tests/unit/worker/test_graphmcp_budgets.py`) onto a generated workload of
fixed size, so nothing measures the design target any more: **answer p95 under
300 ms on THIS repository** (`P95_TARGET_MS` in that test).

The owner decided on 2026-10-27 (issue triage, epic #2525):

* **"Scheduled report"**: #1840's option 2, a report-only measurement of main's
  snapshot that opens an issue when p95 nears 300 ms.
* And: **"think of a caching for swarm-graph answers maybe with an invalidation
  on repo events like merges"**.

This document designs both. The owner reviews it before anything is built.
§4 holds the questions that are the owner's to answer.

---

## 0. The answer in one screen

* **The measurement runs where the snapshot is born: in the indexer, before
  upload** (§1.2). It answers a fixed battery of questions in a fresh,
  cold `python -m agent_worker.graphmcp`, as #1840 and PR 1838 did. It writes
  `latency.json` beside the manifest, never inside it, so taking a measurement
  never changes a manifest digest or a cache key. Promotion copies the
  measurement into the version's Firestore document.
* **The report is a SwarmCloud schedule type, `graph-latency-report`** (§1.3).
  It is an API type, built like `release-health`: it creates no agent and holds
  no capacity. It reads the promoted versions' measurements and opens one issue,
  or comments on the open one, when p95 reaches 240 ms. It is not GitHub cron,
  which fired 2-6% of this repository's frequent schedules. It is not a Cloud
  Scheduler job of its own either: the schedule tick already rides on one Cloud
  Scheduler job, and a report is then data, not infrastructure.
* **swarm-graph already has an answer cache**
  (`apps/swarm-graph/swarm_graph/cache.py::ResultCache`). It is keyed
  `(tenant_id, manifest_digest, tool, canonical_args, TOOLS_VERSION)`, holds
  256 MiB per replica, and never needs invalidating, because a merge makes a new
  digest and so a new key (§2.1). **A stale hit is impossible by construction
  today, and nothing here weakens that.**
* **So repository events do not delete anything. They do three useful things
  instead** (§2.3):
  1. **Warm**: the first ticket naming a newly promoted digest re-asks that
     repository's most-asked questions against it.
  2. **Carry forward**: an answer computed at the previous digest is reused at
     the new one **only when every manifest entry it read resolves to the same
     blob in the new manifest**. This is the literal "invalidation on merges":
     a merge invalidates exactly the answers whose inputs it changed, worked out
     by comparing manifests, never by trusting an event.
  3. **Demote**: entries of a superseded digest go to the cold end of the LRU.
     They are not deleted, because attempts started before the merge still hold
     tickets for that digest.
* **Who benefits.** Only workspaces on `graph_delivery: service` get any of
  this. The default is still `staged` (`graphstage.py:262`,
  `codeintel.py:156`): a pod serves itself from a staged copy, and no cache is
  shared between agents. Q7 asks whether to switch this repository's
  workspace.
* **Measuring it** (§3): the indexer's battery gives cold p95 per promoted
  commit, before and after. A new log-based distribution over swarm-graph's
  existing `graph_answer` events gives served p95 split by `cache`, and the hit
  rate.

---

## 1. The p95 report

### 1.1 What is measured, and why that number

The number #1840 tracks is **cold compute**. It is the p95 of a fixed battery
of questions answered by a fresh `agent_worker.graphmcp` stdio server over one
snapshot. The battery is `test_graphmcp_budgets.py::_questions`. It takes the
four busiest application callees and an even sample of twelve more. For each
of those sixteen symbols it asks six tools: `search`, `context`,
`impact(depth 3)`, `tests_for`, `neighbours` and `territory`. That is 96
answers.

The slowest answers are cold `context` calls on large test modules (#1840:
`tests/unit/control_plane/conftest.py#auth_header` at 330-357 ms). `context`
(`tools.py::tool_context`) reads the symbol's module shard in `callers`,
`callees`, `tests` and `signatures`. A cold call decodes each whole shard, so
latency grows with the largest module, not with the question.

This is the right number to report because **no cache can hide it**. It is
what an agent pays on the first question about a module, in a staged pod, and
in the service right after a promotion. A rising cold p95 is the product
getting slower. A falling served p95 can just mean the cache is warm. §3 keeps
the two apart.

### 1.2 Where it runs: in the indexer, before upload

**Every place the battery could run, and why the indexer wins:**

| where | what it costs | why not |
|---|---|---|
| **the indexer, after the shards are written and before upload** (proposed) | about 15-40 s on a run, local disk, no GCS read, no model | the indexer is the one pod that holds the new snapshot on local disk, in a fresh process, at the moment the snapshot exists |
| an API action inside swarm-api | nothing new to deploy | swarm-api is Cloud Run, serving requests. The battery would measure swarm-api's own load, and it would need the whole snapshot fetched from GCS into a request handler |
| swarm-api calling the live swarm-graph service | measures what agents see | swarm-graph admits only tenant namespaces (NetworkPolicy `swarm-graph-ingress`, graph-service.md §2.1), and swarm-api would need a ticket minted for itself. It also measures a warm cache, not the product (§1.1) |
| a new runner profile, for example `graph-probe` | a clean, separate pod | `RUNNER_PROFILES` is in the frozen `apps/common/swarm_common/profiles.py`, so this is a contract change (rule 1) for a 40-second job |
| one `claude-code` agent, submitted as a schedule's `task` work | an existing profile | a model is paid to run a deterministic script. SD8 (schedules.md §2.7) rejected exactly this: "A deterministic API call does not need a model" |
| GitHub Actions cron | already where CI runs | GitHub fired 2-6% of this repository's frequent cron schedules (#1840, the 2026-10-21 analysis). A report that runs 1 time in 20 is one nobody reads |

**What the indexer does.** The indexer image is built `FROM` the digest-pinned
`agent-runtime-base` (`images/agent-runtime-indexer/Dockerfile:184`), so
`agent_worker.graphmcp` is already in it. After
`repo_graph_shards.build` writes the manifest and blobs, and before the upload,
the indexer runs the following, but only when swarm-api's run document asks for
it (`measure_latency: true`; see "when" below):

1. It opens the snapshot directory in a **fresh**
   `python -m agent_worker.graphmcp` (the battery's `StdioServer`) and asks the
   battery's 96 questions, three times.
2. It times `_reference_ms()` after each answer, as the CI gate does, so a busy
   node can be told apart from a slower product (`P95_BUDGET_RATIO`'s
   docstring).
3. It writes `graph/<sha>/latency.json`:
   `{schema, tool_version, questions, runs: [{p50_ms, p95_ms, max_ms,
   reference_ms, slowest: [{tool, symbol, ms}] (top 5)}], symbols, raw_bytes,
   cpu_limit}`.
4. It uploads `latency.json` with the blobs. **It is not named in the
   manifest.** If it were, the manifest digest would depend on a timing, every
   re-index of one commit would produce a new digest, and every cache key
   (§2.1) would churn for nothing.

**The battery moves into the product.** `_questions`, `_battery`, `_p95` and
`_reference_ms` move from the test file into a new module,
`apps/agent-worker/agent_worker/graphmcp/battery.py`.
`test_graphmcp_budgets.py` imports them from there. The CI gate (on its
generated workload) and the report (on this repository) then ask the same
questions, measured the same way, and cannot drift apart.

**When.** Measuring every promotion would add up to 40 s to every incremental
run of a busy repository. So swarm-api sets `measure_latency` when it queues a
run (`RepoIndex._poll_one`, which already decides the run's `kind`) if both of
these hold:

* the registration has opted in. This is a new registration setting,
  `index.measure_latency_hours`: absent means never, and a number means at
  most once per that many hours. This repository's registration sets 24;
* the last measured version is older than that.

That is one measured index a day, on whatever main was at the time, which is
what "a measurement of main's snapshot" asks.

**Recording.** `_promote_from` reads `latency.json` when one is present. It
copies the median run, the reference, the symbol count and `slowest` into the
`index_versions/{sha}` document as `answer_latency`. A missing or malformed
`latency.json` is recorded as `answer_latency: {state: "unmeasured", cause}`.
It never blocks a promotion: the report measures the index, and must never
gate it.

**This is a Firestore document shape change** (`index_versions`, and a
registration setting). CLAUDE.md gives such a change its own issue, linked from
#1840, once the owner approves the design.

**The CPU caveat, stated plainly.** The indexer's resource class is not the
6-core container #1840 measured in. The first report therefore sets a new
baseline and cannot be compared with the 64-68 ms and 152-165 ms figures. It
records `cpu_limit` and `reference_ms` so later reports compare like with like.
The ratio (p95 over reference) is reported next to the milliseconds.

### 1.3 The report: schedule type `graph-latency-report`

**Why a SwarmCloud schedule, and not Cloud Scheduler.** The schedule tick is
already one Cloud Scheduler job, `google_cloud_scheduler_job.schedule_tick`,
every minute (schedules.md §2.1). A schedule type therefore inherits Cloud
Scheduler's reliability, and adds:

* **exactly once per slot** (§2.2);
* **catch-up** after an outage (§2.4);
* the **firing record as history** (§2.7);
* the owner's **pause and resume** from the console (§2.11).

A separate Cloud Scheduler job would make every change of cadence or threshold
a Terraform apply. It would also need its own identity and its own route, and
it would put a tenant's report outside the tenant check every read makes.
Schedules.md §2.1 rejected per-schedule GCP jobs for exactly these reasons.

**Shape.** It is an `api` executor (SD8): one `api_action` per repository in
scope, ended when it is recorded. It is a new file,
`apps/swarm-api/swarm_api/schedtypes/graph_latency_report.py`, plus an entry in
the catalogue (`scheduletypes.py`), and a §3.19 in docs/schedules.md.

| property | value |
|---|---|
| executor | API (no agent, no capacity, invariants 1-3 untouched) |
| risk | R1: no push, writes one issue |
| default gate / floor | auto / auto |
| creators | members, and the owner for the platform's own repository (as `release-health`) |
| min interval | 6 h |
| parameters | `threshold_ms` (default 240), `ratio_threshold` (default none), `window` (default 7 measured versions), `issue_label` (`bug`) |

**What a firing does, per repository in scope,** as the schedule's tenant, with
that tenant's forge credential, through `ctx.forge_writer`. This is
`release-health`'s marker pattern
(`apps/swarm-api/swarm_api/schedtypes/release_health.py`):

1. **Read** the newest `window` `index_versions` documents that carry
   `answer_latency`, newest first.
2. **Near the target.** The newest measured p95 is at or over `threshold_ms`,
   or the p95/reference ratio is at or over `ratio_threshold` when that is set.
   * With no issue open, it **opens** one. The issue is found by the marker its
     body opens with (`graph-latency:<repo_id>`). The title states the fact:
     "swarm-graph's cold answer p95 on <repo> reached 252 ms at <sha>, 84% of
     the 300 ms target". The body holds the table of the window (sha, date,
     symbols, p95, reference, ratio) and the five slowest answers. The label
     is `issue_label`.
   * With one open, it **comments** with the newest measured version, unless
     the issue already names that sha (its own run marker). A firing every 6
     hours reports a version once, not four times.
3. **Back under** the threshold after an open issue: it comments "under again
   at <sha>" once. **It never closes the issue**; a person does, as
   `release-health` does.
4. **Anything else** writes nothing. That covers under the threshold with no
   open issue, and no measurement in the window. A window with no measurement
   for 3 days is itself reported, once, as "unmeasured since <date>": a report
   that has silently stopped measuring is the failure #1840 is about.

**Why 240 ms** (Q3): 80% of the target. At #1840's measured rate (about
+90 ms in five days, of which part is a step), a daily measurement crossing
240 ms leaves days, not hours, before 300 ms. A threshold at 300 ms would
report the miss rather than warn of it.

---

## 2. An answer cache for swarm-graph

### 2.1 What is built today

`apps/swarm-graph/swarm_graph/cache.py` (graph-service.md §5) holds three
caches per replica, all in memory, all keyed with `tenant_id` first
(invariant 9, owner decision 3):

| cache | key | bound | value |
|---|---|---|---|
| `ShardCache` | `(tenant_id, blob_sha256)` | `SHARD_CACHE_RAW_BYTES` 512 MiB raw, half per tenant while more than one is resident | a decoded `_Shard` |
| **`ResultCache`** | **`(tenant_id, manifest_digest, tool, canonical_args, TOOLS_VERSION)`** | `RESULT_CACHE_BYTES` 256 MiB; one entry at most `RESULT_ENTRY_MAX_BYTES` 4 MiB | a `Computed(head, rows)` serialised compactly, or an `_Unresolved` |
| `ManifestCache` | `(tenant_id, repo_id, manifest_digest)` | `MANIFEST_CACHE_RAW_BYTES` 32 MiB | an opened `Snapshot` |

**The answer cache is `ResultCache`.** `canonical_args` is the validated
arguments without `cursor`, with defaults filled, serialised `sort_keys`
(`cache.py::canonical_args`). So `impact` without `depth` and `impact` with
`depth: 2` are one entry, and every page of one question is served from one
computation. A `SnapshotError` is never cached. `GraphService.answer`
(`server.py`) logs every answer as a `graph_answer` event carrying `tool`,
`status`, `cache` (`hit`, `miss` or `none`) and `ms`.

**Why it is never invalidated** (graph-service.md §5.2). An answer is a
function of five things, and the key holds all of them:

* the manifest bytes, verified against `manifest_digest`, which is in the key;
* the blob bytes, named in the manifest by sha256 and checked before decoding;
* the tool and its arguments, both in the key;
* the handler code, held by `TOOLS_VERSION` (a test fails if output changes
  without it), and a new image starts empty anyway;
* the observation, which is not an input at all: the service always computes
  under the empty one, and the pod's shim marks staleness afterwards (§4.4).

A merge produces a new commit, a new index, a new manifest and **a new
digest**. No existing entry becomes wrong.

**What is not built, and is the reason latency still grows:**

* **Hit rate after a merge is zero.** The first question at a new digest is a
  miss, even when nothing it reads has changed. On a repository merging dozens
  of times a day, the cache keeps being reset by its own key.
* **The first-sight warmer skips the layers `context` reads.**
  `swarm_graph/warm.py::HOT_LAYERS` is `symbols`, `callers`, `files` and
  `terms`. `context` also reads `callees`, `tests` and `signatures`, so the
  slowest answer in the battery is cold after every promotion.
* **Replicas do not share.** With two replicas, each one's hit rate is about
  half (graph-service.md §2.1 accepted this).
* **Staged workspaces get nothing.** `DEFAULT_GRAPH_DELIVERY` is `staged`. Each
  staged pod runs its own server over a 48 MiB raw per-pod LRU
  (`snapshot.py::CACHE_RAW_BYTES`), and no answer outlives the agent.

### 2.2 The repository events, and the path each one takes

The service hears of no event directly. Graph-service.md §6.3: "The service
learns of a promotion only when the first ticket names the new digest." That
holds here too. Every event reaches the cache as **a ticket naming a digest the
replica has not seen**, so there is no new door, no Pub/Sub grant (the owner
declined it, graph-service.md §5.4) and no event that has to be trusted.

| event | the path that exists | what the cache sees |
|---|---|---|
| **a merge to the default branch** | GitHub `push` → the public receiver `apps/swarm-github-hooks/swarm_github_hooks/receiver.py` (`POST /github/events`: signature checked, deduped on `X-GitHub-Delivery`, non-deleting pushes to `refs/heads/<default_branch>` only) → swarm-api `POST /v1/admin/repositories/push` (`routes/admin.py:1419`) → `RepoIndex.push` → `RepoIndex._poll_one`, which re-reads the head with the workspace's own credential and queues an index run. Only registrations with `on_change: webhook`; the 5-minute `repo_index_poll` is the fallback | **nothing yet.** Until the index is promoted, tickets still name the old digest, and old answers are still the right ones for them |
| **a re-index** (incremental, full, or a `repo-index-refresh` schedule) | an `indexer` run writes `graph/<sha>/manifest.json` and blobs | nothing until promotion. A full re-index of an already indexed commit can produce a different digest (a new extractor), which is a new key like any other |
| **a snapshot promotion** | `RepoIndex._promote_from` sets `index.current_sha` and `current_digest` in one transaction; swarm-api mints every later graph ticket from it (graph-service.md §3.1) | **the first ticket naming the new digest.** This is the one event the cache acts on (§2.3) |

So the "invalidation on merges" the owner asked for happens at promotion, not
at the merge. That is not a delay that costs anything: between the merge and
the promotion, no agent can ask about the new commit's index, because none has
been minted a ticket for it.

### 2.3 What the events do: warm, carry forward, demote

#### (a) Warm on promotion: re-ask the popular questions

* **Today.** The first ticket naming a new digest starts a background
  `Warmer.saw(...)` that decodes `HOT_LAYERS`.
* **Proposed.**
  1. `HOT_LAYERS` gains `callees`, `tests` and `signatures`, so the warm covers
     `context` too. It stays within the existing budget, `WARM_BYTES` =
     `MAX_SNAPSHOT_BYTES` (64 MiB stored).
  2. Once the shards are in, the warmer **replays the top questions of the
     previous digest of the same `(tenant_id, repo_id)`** against the new one.
     The previous digest is the `ManifestCache` entry for that pair. The top
     questions are the `PREWARM_QUESTIONS` = 200 `ResultCache` keys of that
     digest with the most hits, which means each entry counts its hits.
* **What stays the same.** A replayed answer is computed exactly as a request
  would compute it, and stored under the **new** digest's ordinary key, so (a)
  cannot produce a stale hit.
* **Cost.** CPU on the replica, off the request path: at most 200 answers,
  bounded by the warmer's workers.
* **Why it helps.** The planner and implementers of one issue run ask the same
  `impact` and `context` questions. After a merge, they are answered before
  they are asked.

#### (b) Carry forward: reuse an answer whose inputs did not change

This is the part that makes a merge **invalidate only what it changed**.

* **What each entry records.** A `ResultCache` entry also records its **read
  set**: every manifest lookup its computation made. `Snapshot` is the only way
  a handler reads the index (graph-service.md §5.2, "Determinism"), so the
  service wraps the `Snapshot` it passes to `tools.compute` in a recorder. The
  recorder logs:
  * every `entry(layer, key)` and `shard(layer, key)` as
    `(layer, key, blob_sha256 or ABSENT)`. **Absent is recorded too.** A
    "no such symbol" answer depends on the key being missing, and a merge that
    adds the symbol must invalidate it;
  * every `keys(layer)` (a listing) as `(layer, LISTED, sha256 of that layer's
    whole key→blob table)`. A listing depends on every key, not just the ones
    it went on to read;
  * every manifest-level read (`term_stats`, the tokenizer version, the format,
    `has_layer`) as `(MANIFEST, field, sha256 of its canonical value)`.
* **The second key.** A carried entry is found by a key that leaves the digest
  out: `(tenant_id, repo_id, tool, canonical_args, TOOLS_VERSION)`, pointing at
  the newest computation of that question for that repository.
* **The lookup at digest D2:**
  1. The exact key `(tenant_id, D2, tool, args, TOOLS_VERSION)` first, which is
     today's behaviour.
  2. On a miss, the second key gives a candidate computed at D1. **Every read in
     its read set is re-resolved against D2's manifest**, which is in memory
     (`ManifestCache`), so no GCS read and no decode is needed. If every lookup
     resolves to the same blob or to the same ABSENT, and every listing and
     manifest field hashes the same, the candidate is copied to D2's exact key
     and served. Any difference is a miss, and the answer is computed.
  3. The served answer's freshness block carries D2's `index_sha`, written by
     the service exactly as it writes it for a computed answer. The rows are
     the candidate's.
* **Why this is exact, not a heuristic.** A handler's output is a function of
  what it read. Equal reads mean equal output: that is the same determinism
  §2.1 already relies on. The check costs a dictionary lookup per recorded
  read, against a computation that decodes shards.
* **The test that holds it.** A property test builds two fixture snapshots one
  incremental commit apart, asks the full battery at both, and asserts that
  every carried answer equals the computed one byte for byte. It also asserts
  that a symbol added in the second commit invalidates the first commit's
  ABSENT answer.
* **What it buys.** An incremental index rewrites only the shards of the
  modules the merge touched. Most of a 58,930-symbol graph's shards keep their
  blob between two merges, so most questions about untouched modules carry
  forward. §3 measures how many.

#### (c) Demote, never delete, superseded digests

* **What changes on promotion.** When a ticket names D2 and the replica has D1
  for the same `(tenant_id, repo_id)`, D1's `ResultCache` entries move to the
  least recently used end. They go first when space is needed, but they are not
  dropped.
* **Why not delete.** An attempt that started before the merge holds a ticket
  for D1 for its whole life (graph-service.md §3: the ticket pins the digest).
  Deleting D1's answers at the merge would make that agent's next question a
  miss for no gain in correctness.
* **Shards need nothing.** `ShardCache` needs no demotion: shards are keyed by
  content, and the ones D1 and D2 share stay hot through D2's use.

### 2.4 Where it lives, and its size

**It lives in each swarm-graph replica's memory, as today.** The alternatives
that were considered:

| where | for | against |
|---|---|---|
| **replica memory** (proposed) | a hit is a dictionary read; nothing new to deploy, secure or pay for; a restart is a cold cache, never a wrong one | each replica warms separately |
| Memorystore (Redis) shared by the replicas | one hit rate for both replicas | new infrastructure, a new IAM grant and a network hop per lookup; a cache shared across tenants is a new isolation surface (invariant 9), where today's per-key `tenant_id` lives inside one process |
| GCS, beside the index | survives restarts | a GET (tens of ms) per lookup, which is the latency being saved; and the service is read-only on the store by design (graph-service.md §3.4) |
| the pod (staged) | no service needed | one agent's lifetime; nothing is shared |

**Size.** The existing `RESULT_CACHE_BYTES` (256 MiB) and
`RESULT_ENTRY_MAX_BYTES` (4 MiB) stay.

* **The read set counts against the bound.** An entry's size becomes its
  serialised answer plus its read set. A read set is about 70 bytes per lookup;
  `impact(depth 3)` on a busy symbol makes a few hundred lookups, about 20 KB.
  An entry over the per-entry cap is computed and not kept, as today.
* **The second (carry) key holds a pointer, not a copy.** It is one dictionary
  entry per `(tenant_id, repo_id, tool, args)`, bounded by the entries it points
  to, and dropped with its target.
* **The memory test still holds.** `resident_bytes()` (held under
  `MEMORY_LIMIT_BYTES` by `tests/unit/graphsvc/test_graphsvc_cache.py`) does
  not change, because the result cache's bound does not.
* **Warming adds no new memory** beyond `WARM_BYTES`, which `HOT_LAYERS`
  already spends.

**Tenant fairness.** `ShardCache` caps a tenant at half its bound while more
than one tenant is resident. `ResultCache` has no such share today. Carry
forward makes one busy repository's entries live longer, so `ResultCache` gains
the same rule: half the bound per tenant while more than one is resident.

### 2.5 What a stale hit would cost, and how the key prevents one

**What it would cost.** A stale answer is **worse than a slow one, and worse
than a missing one**, because it arrives marked fresh. The service writes the
pinned `index_sha`. The shim's staleness marking (graph-service.md §4.3) covers
only the agent's own working tree, against the base commit. A wrong index row
on an untouched path is shown as current. Concretely:

* **`impact` or `callers` missing a caller.** The agent changes a signature and
  does not update a caller. CI goes red at best; at worst an untested path
  breaks after merge.
* **`tests_for` missing a test.** The agent runs a narrowed selection that does
  not include the test its change breaks. The fix round finds it, at a full CI
  cycle's cost.
* **`territory` wrong.** Two parallel lanes are given overlapping files, and
  one overwrites the other. This is the failure CLAUDE.md's "two issues that
  edit the same file are one lane" exists to stop.
* **An `_Unresolved` or "no such symbol" answer carried past the merge that
  added the symbol.** The agent concludes the symbol does not exist and writes
  a second one.

**How the key prevents it:**

* **An exact hit** is keyed by `manifest_digest`, which the service verified
  the manifest bytes against, and which the ticket swarm-api minted pins. No
  event, payload or clock is in the decision. A merge cannot make an exact hit
  stale. It changes which digest the next ticket names.
* **A carried hit** (§2.3 b) is served only after **every input the answer
  read** is shown unchanged in the new manifest:
  * absences included, so the "added symbol" case misses;
  * listings included, so a `search` or `territory` that enumerated a layer
    misses when any key of that layer moved.
* **The handler code** is held by `TOOLS_VERSION` in both keys, and by the cache
  being in memory, so a new image starts empty.
* **Tenant isolation** is held by `tenant_id` leading both keys. The carry key
  also holds `repo_id`, so an answer is never carried between two
  repositories, even within one tenant.
* **What is deliberately NOT trusted:**
  * the webhook payload's `after` sha (the receiver never reads it);
  * the order in which events arrive;
  * any notion of "the merge changed only these files". Carry forward compares
    the manifests it is about to serve from, not a list of changed files
    supplied by anyone.

---

## 3. How to measure both

### 3.1 The p95: before and after

There are two numbers, never mixed.

**1. Cold compute p95**, from §1.2, per measured version. This is the product's
cost, and no cache affects it, because the battery runs in a fresh process over
local shards.

* *Before:* the first measured versions after §1 ships.
* *After:* the same series. It moves only when the handlers or the shard layout
  change, so the cache work of §2 must **not** move it. If it does, the
  measurement is broken, and that is worth knowing.
* *The control:* `reference_ms` in every run, and the CI gate's generated
  workload, which is the same on every commit and tracks the server's code
  alone. If the generated-workload ratio is flat while this repository's p95
  rises, the growth is the data, as #1840 found.

**2. Served p95**, from swarm-graph's existing `graph_answer` events (`ms`,
`tool`, `cache`). A new log-based distribution metric,
`graph-answer-ms`, is added in `terraform/modules/monitoring/metrics.tf`. It is
shaped like `peak_rss`:

* filter: `jsonPayload.event="graph_answer"`;
* value: `EXTRACT(jsonPayload.ms)`;
* labels: `tool` and `cache`;
* exponential buckets from 1 ms.

`tenant_id` and `repo_id` are **not** labels: they are unbounded, and a
per-repository reading is a Logs Analytics query over the same events.

* *Before:* 7 days of served p95, overall and by `cache`, read before §2.3
  ships.
* *After:* the same 7-day window once it ships.
* *The control:* the `cache=miss` p95 alone. It should not move, because a miss
  still computes. If the miss p95 falls too, something else changed (load, the
  image or the node), and the overall improvement cannot be credited to the
  cache.

### 3.2 The hit rate

* **The `cache` field gains `carried`** (§2.3 b) beside `hit`, `miss` and
  `none`.
* **The hit rate** is `(hit + carried) / (hit + carried + miss)` over
  `graph_answer` events, per day, from the same metric's count. `none` is a
  question with no cache key (an invalid argument) and is left out.
* **The rates that tell the three mechanisms apart:**
  * **Exact-hit rate:** what today's cache already earns.
  * **Carried rate:** what §2.3 b adds.
  * **First question after a promotion:** the `graph_warmed` event already logs
    `ms` and `shards` per replica. A new `graph_prewarmed` event logs how many
    questions were replayed and how long that took. The first `graph_answer`
    per `(repo, digest)` per replica, hit or miss, is what §2.3 a should move.
* **The carry check's own rate.** The number of carry candidates and the number
  that passed the check, as `graph_carry` counters. A pass rate near zero means
  every merge touches what is asked, and (b) is not worth its memory.

### 3.3 What would show the design wrong

* **Carried rate under 10% after two weeks.** The read sets are too wide (every
  answer lists a layer), and (b) should be withdrawn.
* **Served p95 unchanged while the hit rate rises.** The misses are the p95. The
  fix is then #1840's option 3 (split or cap the per-module shards), which the
  owner did not choose.
* **The report never opening an issue, with no "unmeasured" comment either.**
  The schedule is not firing. The firing records (schedules.md §2.7) show it.

---

## 4. Questions for the owner

1. **Where does the battery run?**
   * (a) In the indexer at promotion, opt-in per registration, at most once
     every 24 h (§1.2).
   * (b) A new runner profile `graph-probe`, which is a frozen-contract change.
   * (c) One `claude-code` agent, which pays a model for a script.
   * (d) A Cloud Run job of its own.

   **Recommended: (a).** It needs no contract change, no model, no GCS read and
   no new door, and it measures the snapshot at birth.
2. **What fires the report?**
   * (a) A SwarmCloud schedule type, `graph-latency-report`, every 6 h (§1.3).
   * (b) A dedicated Cloud Scheduler job and route.
   * (c) GitHub Actions cron.

   **Recommended: (a).** It inherits the tick's Cloud Scheduler reliability,
   plus exactly-once, catch-up and pause. (b) is a Terraform apply per change
   and a new identity. (c) fired 2-6% of the time.
3. **The threshold.**
   * (a) 240 ms, 80% of the target.
   * (b) 270 ms.
   * (c) A ratio to the reference rather than milliseconds.
   * (d) (a) and (c) together, either one firing.

   **Recommended: (a) for now.** Add (c) only if the indexer's node type makes
   the milliseconds noisy; the report records the ratio either way.
4. **Carry forward across digests (§2.3 b).**
   * (a) Build it after (a) and (c) of §2.3 have run for two weeks, so the
     exact-hit baseline is measured first.
   * (b) Build it now, with the rest.
   * (c) Do not build it.

   **Recommended: (a).** It is the only part of §2 with real complexity (the
   read-set recorder and its property test), and §3.2 says whether it would pay.
5. **Pre-warming the popular questions (§2.3 a).**
   * (a) Replay the top 200 of the previous digest.
   * (b) Warm the shards only, with `HOT_LAYERS` widened to cover `context`.
   * (c) Neither.

   **Recommended: (a).** (b) alone fixes the cold `context` decode but not the
   `impact(depth 3)` computations, which are the expensive repeated ones.
6. **Replica affinity.**
   * (a) Keep round robin; each replica warms on its own.
   * (b) The shim picks a replica by a hash of `(tenant_id, repo_id)` over the
     headless Service's addresses, doubling the hit rate.
   * (c) A shared Memorystore tier.

   **Recommended: (a) until §3.2 measures the per-replica hit rate.** (b) is a
   shim change that also concentrates one busy repository's load on one
   replica. (c) is new infrastructure and a cross-tenant shared store.
7. **Should this repository's workspace move to `graph_delivery: service`?**
   Without it, none of §2 reaches this repository's agents: the default is
   `staged`, and a staged pod shares no cache.
   * (a) Switch it now, on the per-workspace setting (graph-service.md §8.1).
     The fallback to staging keeps a service outage from becoming a gap.
   * (b) Wait for graph-service.md §8's 14-day criterion on other workspaces
     first.
   * (c) Keep it staged; the report still measures cold p95.

   **Recommended: (a).** This repository is the one whose latency #1840
   measured, and the switch is reversible by one setting.
8. **The shard growth itself** (#1840's option 3, not chosen on 2026-10-27).
   * (a) Leave it until the report opens its first issue.
   * (b) Design the per-module shard split now as well.

   **Recommended: (a).** The report exists to say when, and the slowest-answers
   list it carries names the modules to split.
