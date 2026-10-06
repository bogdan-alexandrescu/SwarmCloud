# Registered repositories and the repository index

**Status: PROPOSED 2026-10-04, design and mock-ups only (functionality wave 4,
lane RI0).** Nothing described here is built. The owner asked on 2026-10-04
for "a section where we register new repos to work in and we have an agent
index the repo on a set interval or when a new change is detected and
provides that context to any planning job that is given an issue to work on
... Including what tests should be run based on the committed change about to
be turned into a PR". This document is the design that request needs before
any code, so the owner can pick; the screens are drawn, in three variants
each, in [web-ui/mockups/repositories.html](web-ui/mockups/repositories.html).
The frozen-contract half is one request, written out in §6.3 for filing; it
is not filed by this lane.

**Revised 2026-10-04 by the owner, second pass (lane RI0b, drawn
2026-10-05):** symbols from tree-sitter, LSP-resolved call graphs, a graph
explorer, test selection for merge, and git tokens per repository and per
user. The section "Revised 2026-10-04 (owner): AST and LSP" below lists what
changed and where; still nothing is built and the status stays PROPOSED.

What the design settles, in one line each, with the section that carries the
detail:

* **A registered repository is a per-tenant Firestore document** naming one
  GitHub `owner/repo`, its default branch, the runner profiles allowed to
  work in it and its index schedule. The credential is the tenant's existing
  `swarm-tenant-<tenant>-git` secret, never a new one (§1).
* **The index is a JSON document plus a rendered summary**, keyed by the
  commit sha it describes, stored under the tenant's own GCS prefix, with a
  hard size budget so it always fits a planner's prompt (§2).
* **An index run is an ordinary task**: queued, admitted, leased and counted
  like any other, so a tenant's indexing competes with its own work and with
  nobody else's (§3).
* **The issue-run planner gets the index of the issue's repository**; any
  other step asks for it by name; a `tests:select` query maps a diff's
  changed paths to the tests that cover them (§4).
* **An index older than the head it describes says so**, every time it is
  served, and is never silently treated as current (§5).
* **Phase 1 needs no frozen-contract change.** Staging the index into an
  arbitrary step's workspace (phase 2) needs one runner input on
  `claude-code` and `codex`, which is a request, not an edit (§6.3).
* **(RI0b) Symbols and a call graph, below the file**: tree-sitter symbols and
  LSP-resolved edges with evidence and confidence, stored as graph shards per
  commit under the tenant's prefix (§2.5, §3.5).
* **(RI0b) An impact query and a selected-tests gate**: a diff becomes a test
  plan with a reason per test (§4.3a); three policies for making it the merge
  gate are written out for the owner, not decided (§4.4).

---

## Revised 2026-10-04 (owner): AST and LSP

The owner read RI0's design and mock-ups on 2026-10-04 and took four
decisions. Lane RI0b folds them into the sections they touch; this section
says what changed and where, so a reader of RI0's version can find it.

* **(a) The index goes below the file.** Beside the file-level map, the
  indexer builds an **AST symbol table** with tree-sitter — functions,
  classes, methods and routes, with their line ranges, per language — and
  **LSP-resolved call graphs**: type-resolved definitions and references from
  each language's own server, pyright for Python, tsserver for TypeScript and
  JavaScript, gopls for Go and terraform-ls for HCL. The owner accepted about
  ten times RI0's extractor time and per-language tooling for it. The index
  gains three layers, `symbols`, `call_edges` (every edge carrying its
  evidence — `lsp`, `ast`, `import`, `naming` or `co-change` — and a
  confidence) and `symbol_test_map` (which tests reach which symbols through
  the resolved call graph): §2.1 and §2.5. How they are produced, with the
  budget, the timeouts and the fallback for a language no server covers:
  §3.5. Where they are stored, as graph shards per commit under the tenant's
  own prefix: §2.5.
* **(b) The graph is visible in the console**: a graph explorer in the
  repository section (module dependency graph, a symbol's call graph, a
  test-map view), drawn in three variants in the mock-ups, sections 8-11.
* **(c) The AST chooses the tests a pull request must pass.** The
  commit/PR impact query (§4.3a) turns a diff into changed symbols, their
  transitive callers and the tests that cover them, each with a reason; §4.4
  sets out three policies (P1, P2, P3) for how that selection becomes what a
  pull request must pass before the merge step (#295, lane M1a) may merge it.
  The policy is the owner's to pick, by picking a screen.
* **(d) Git tokens per repository and per user, and what each can do.**
  Written as a sibling document, [git-tokens.md](git-tokens.md), because a
  forge token is used by every task that clones or publishes, registered
  repository or not. §1 here points to it.

The API sketch (§6) gains the graph, symbol and impact routes and the
selection policy; the frozen-contract section (§6.3) gains requests (C) and
(D); the build plan (§7) makes RI3's extractor tree-sitter and adds the LSP,
graph-storage, impact, selected-tests and graph-explorer lanes.

---


## 0. Why a registry at all

Today a repository is not a thing the platform knows about. It is a string on
a task: `TaskCreate.repository_url`, checked by one rule,
`check_repository_url` (`apps/swarm-api/swarm_api/validation.py::check_repository_url`), which
accepts any https, ssh or `git@` URL that carries no credential. An issue run
derives its repository from the issue reference (`IssueRef`,
`apps/swarm-api/swarm_api/validation.py::IssueRef`; GitHub only, `ISSUE_FORGE_HOSTS`)
and reads the forge for that one run: the issue for the preview, and the
repository's open issues and pull requests for the planner
(`read_open_work`, `apps/swarm-api/swarm_api/forge.py::read_open_work`). Nothing outlives
the run. Every planner therefore starts from nothing: it clones the
repository and spends the first part of its budget discovering the layout,
the test conventions and the territory rules that the previous planner on the
same repository discovered an hour earlier.

That is the cost the owner's request removes. Anything that runs on a
schedule or reacts to a push needs something to hang the schedule on, and
"the set of repository URLs that happen to appear on recent tasks" is not a
set anyone chose. So the index needs a registry, and the registry is the
smallest thing that can carry a schedule, a credential reference and a
default branch.

---

## 1. A registered repository

A registered repository is a **per-tenant** record: tenant `eng` registering
`example-org/example-api` says nothing about tenant `ops`, which may register
the same repository and gets its own record, its own index and its own index
runs (§2.4 says why indexes are not shared).

| field | meaning | why it is there |
|---|---|---|
| `repo_id` | `repo_` + the first 16 hex of sha256(`tenant_id` + `github.com/` + lower-cased `owner/repo`) | deterministic, so registering twice is idempotent, and a Firestore id cannot contain `/` |
| `tenant_id` | the owner of the record | every read compares it with the caller's tenant and answers a mismatch with the same 404 as a missing record, as `issue_runs` does |
| `forge` | `github` — the only value accepted in phase 1 | the issue fetch, the preview and `read_open_work` all read GitHub's API and nothing else; a second forge is a second client, not a field |
| `owner/repo` | stored as `owner` and `repo`, validated with `IssueRef`'s owner and repository patterns | one spelling with the issue runs, so an issue run finds its registration by equality, not by URL parsing |
| `repository_url` | derived, `https://github.com/<owner>/<repo>`, and passed through `check_repository_url` | a registration must name a URL a task would have accepted, so nothing the registry hands a task is refused later |
| `default_branch` | read from the forge at registration, overridable by an admin of the tenant | the change trigger watches it and the index describes its head; a fork that works on `develop` says so here |
| `allowed_profiles` | runner profile **names** that may work in the repository, default `["claude-code"]` | a registration narrows what a tenant runs against a repository; it never widens what a profile is (invariant 10) |
| `index` | the schedule and trigger settings of §3, plus a pointer to the current index (§2.3) | the registry is where a schedule lives |
| `created_by`, `created_at`, `updated_at` | the caller's email and timestamps | the record of who asked |

**The credential is the tenant's existing forge token,
`swarm-tenant-<tenant>-git`, by name.** It is the secret the worker clones and
publishes with, that swarm-api already reads through
`SecretManagerForgeTokens` (`apps/swarm-api/swarm_api/forge.py::SecretManagerForgeTokens`) for the
issue preview and the open-work read, and that `scripts/create-secrets.sh
--stdin` stores. Registration does not accept a token, does not store one,
and does not echo one: the record holds no credential field at all.
Registering a repository the token cannot read is refused at registration,
by a read of `GET /repos/{owner}/{repo}` with the tenant's token, answering
`no_access` with the secret's name and no part of its value. Registration is
also where `default_branch` comes from, so the read is not extra work.

**Revised 2026-10-04 (owner): a repository may have its own token, and a
user may have theirs.** [git-tokens.md](git-tokens.md) designs a token
registry with three scopes — tenant default (today's `-git`), per repository,
per user — the order in which a task resolves one, the Secret Manager naming
that keeps each under the tenant (invariant 9), and a server-side view of
what each token can do in each repository. The registration record still
holds no credential field: a per-repository token is a separate record that
names the registration's `repo_id`, and the read at registration uses
whichever token resolves for the registering admin under the order the owner
picks (git-tokens.md §3.1). The repository's Settings show that token and its
capability row (mock-ups, section 11).

**How it relates to today.** Nothing that works today changes. A task with a
`repository_url` that no registration names still runs exactly as it does:
the registry is opt-in context, not a gate. Where a registration exists, three
things use it: an issue run looks up the registration whose `owner/repo`
equals its `IssueRef.repository` and, finding one, gives its planner the index
(§4.1); a task whose `repository_url` matches a registration may ask for the
index by name (§4.2); and the console's Repositories section lists them. Phase
1 does **not** make `allowed_profiles` a submission check for tasks that do
not use the index; turning the registry into a gate is a product decision the
owner may take later, and the field exists so it can be taken without a
migration.

---

## 2. The index

### 2.1 What it contains

The index is what a competent engineer would write on their first day in the
repository, as data. Every field answers a question a planner or an
implementer otherwise spends tokens answering:

| key | content | question it answers |
|---|---|---|
| `commit_sha`, `branch`, `built_at`, `kind` (`full` / `incremental`), `base_sha` | what the index describes | "is this about the code I am looking at?" (§5) |
| `modules` | one entry per package or top-level directory that holds code: `path`, `language`, a one-line `purpose`, `files`, `lines` | "where does X live?" |
| `entry_points` | executables, service mains, CLI commands, workers, `create_app()` factories, with `path` and how they are started | "what runs?" |
| `routes` | public APIs: HTTP routes (`method`, `path`, handler `file`), exported library symbols, MCP tools | "what do callers depend on?" — the impact question |
| `test_layout` | test roots, frameworks, how each suite runs, what needs an emulator or credentials | "how are tests organised here?" |
| `test_map` | source path globs → the tests that cover them, each edge with its `evidence` (`import` / `naming` / `co-change` / `declared`) | "which tests does this change need?" (§4.3) |
| `territory` | ownership hints read from the repository: CLAUDE.md track tables, CODEOWNERS, frozen directories, "do not edit" notes, quoted with their source file | "may I edit this, and who do I tell?" |
| `commands` | build, lint, test and CI commands, read from Makefile, `package.json`, `pyproject.toml` and the CI workflows, each with its source | "how do I prove it works?" |
| `hot_spots` | the files changed most in the last 90 days of the default branch, with change counts and the paths most often changed together | "what is fragile, and what else moves when this moves?" |
| `notes` | at most 20 one-line facts the indexer judged a newcomer must know (an invariant, a frozen contract, a known trap) | "what will bite me?" |
| `symbols` *(revised 2026-10-04)* | per language, every function, class, method and route tree-sitter finds: `id` (`<path>#<qualified name>`), `kind`, `path`, `start_line`, `end_line`, `language`, `exported`; for a route also `method` and `path` | "where exactly is X defined, and how big is it?" |
| `call_edges` *(revised)* | caller → callee and reference edges between symbols, each with `kind` (`call` / `reference` / `inherit` / `route_handler` / `import`), its `evidence` and a `confidence` from 0 to 1 | "what calls this, and how sure are we?" — the impact question at symbol level |
| `symbol_test_map` *(revised)* | for each symbol, the tests (test functions, as symbols) that reach it through `call_edges`, with the `depth` of the shortest path and the path's confidence | "which tests exercise this function?" (§4.3a) |
| `languages` *(revised)* | per language: files, the tree-sitter grammar used, the language server and its status (`ok` / `unsupported` / `failing` / `timed_out`) and what the index fell back to | "how far can I trust the graph for this language?" |

`modules`, `test_layout`, `test_map`, `commands` and `hot_spots` are computed
mechanically (§3.4), and so are `symbols`, `call_edges`, `symbol_test_map`
and `languages` (§3.5); the one-line purposes, `territory` and `notes` are the
agent's reading. Every entry that came from a file names the file, so a
consumer can check a claim rather than trust it.

### 2.2 Format and size budget

Two objects per indexed commit:

* **`repo-index.json`**, the structured index, schema `swarm.repo-index/v1`,
  at most **512 KiB**. Validated by the API against a pydantic model
  (`RepoIndexSpec`, phase 1 lane RI2) the way `plan.json` is validated against
  `PlanSpec`: an extra key is refused, naming it, and every list has its own
  bound (`modules` 400, `routes` 1,000, `test_map` 4,000 edges, `hot_spots`
  50, `notes` 20), so one huge field cannot spend the whole file.
* **`repo-index.md`**, the rendered summary, at most **24 KiB**, rendered by
  the API from the JSON (never written by the agent, so it cannot say
  something the JSON does not). 24 KiB is chosen against the planner's prompt:
  `MAX_PLANNER_PROMPT_BYTES` is 64 KiB
  (`apps/swarm-api/swarm_api/issueruns.py::MAX_PLANNER_PROMPT_BYTES`) because the claude-code runner
  passes the prompt as one argv string and Linux refuses one argument over
  128 KiB. The open-work section, the instructions and the summary together
  must fit; 24 KiB leaves the open work more than half the remaining budget.
  When the rendering would exceed it, the renderer drops whole sections from
  the end of a fixed order (notes, hot-spots, routes beyond the first 100) and
  says what it dropped, the way the open-work section says "N more open items
  not shown".

**Revised 2026-10-04: the graph is not in the 512 KiB document.** A
2,000-file repository has tens of thousands of symbols and a few hundred
thousand edges, which no prompt budget holds. `repo-index.json` carries only
the graph's summary — symbol and edge counts per language, the `languages`
table, the 100 most-called symbols and the routes — and points at the graph
shards of §2.5 by their manifest's digest. The planner's 24 KiB rendering
is unchanged; a consumer that needs the graph asks the impact or symbol
routes (§6.1), which read the shards.

A repository too large for the budget is still indexed: the JSON keeps the
module map and the test map at directory granularity and says so in
`truncated: ["modules", ...]`. A truncated index is honest about it; it is
never padded to look complete.

### 2.3 Where it lives, and how it is versioned

The index objects are the indexer task's **artifacts**:

    tenants/<tenant>/tasks/<task>/attempts/<attempt>/artifacts/repo-index.json

That path is already under the tenant's own GCS prefix, written by the
tenant's own worker service account and readable by nobody else's
(invariant 9: own GSA, own secrets, own GCS prefix, own namespace). Using the
artifact path rather than a new `tenants/<tenant>/repos/` prefix means phase 1
needs no new bucket, no new IAM binding and no new writer: the worker uploads
it like any artifact, and swarm-api reads it back like it reads `plan.json`.
The catch is retention: an index is an artifact and lives as long as
artifacts do. Lane RI2 reads the bucket's lifecycle rule and, if it is shorter
than the longest schedule a registration may set, copies the current index to
`tenants/<tenant>/repos/<repo_id>/index/<commit_sha>/` under the same tenant
prefix instead.

**Versioned by commit sha.** When an index run succeeds, swarm-api validates
the JSON, renders the markdown, and in one Firestore transaction writes an
`index_versions/{commit_sha}` entry under the registration (task, attempt,
object paths, the JSON's sha256 digest, `kind`, `built_at`) and moves
`repositories/{repo_id}.index.current_sha` to it — **only if** the new sha is
the branch head or a descendant of the current one, so a slow full run
finishing after a newer incremental one cannot move the pointer backwards.
The last 20 versions are kept; older entries are deleted with their objects
left to the artifact lifecycle. A consumer reads a version by sha and checks
the digest, so an artifact rewritten after promotion is detected rather than
served (§5.2).

### 2.4 Why two tenants on one repository do not share an index

It would halve the indexing cost, and it is refused. A shared index is an
object one tenant's agent wrote and another tenant's agent reads: a channel
between tenants, which invariant 9 exists to close. Two tenants can also see
different things in one repository (one token may read a private submodule
the other cannot), so a shared index would leak what one tenant's credential
could reach. The cost of indexing twice is paid instead.

### 2.5 The symbol and call-graph layers (revised 2026-10-04, owner)

**Evidence and confidence.** Every edge says how it is known, and that
decides how far a consumer may lean on it:

| evidence | how it is produced | confidence |
|---|---|---|
| `lsp` | the language server resolved the call site to a definition (`textDocument/definition`, call hierarchy) | 0.95; 0.8 when the server resolved it through an inferred, not declared, type |
| `ast` | tree-sitter saw a call whose name matches exactly one definition in scope or in an imported module, but no server confirmed it | 0.6 for a unique match; 0.3 when several definitions share the name (each gets an edge) |
| `import` | the file imports the module the symbol lives in; no call was resolved | 0.4 |
| `naming` | the convention `src/x/y.py` ↔ `tests/**/test_y.py`, `foo.ts` ↔ `foo.test.ts` | 0.3 |
| `co-change` | the two files changed together in at least 5 of the last 90 days' commits | the pair's Jaccard support, capped at 0.5 |

An edge found by more than one method keeps the strongest evidence and lists
the others. Confidence is a number so the impact query can bound a path by
the product along it, and it is shown, never hidden, so "the graph says no
test reaches this" can always be answered with "through an `ast` edge at
0.3".

**Graph shards, per commit, under the tenant's own prefix.**

    tenants/<tenant>/repos/<repo_id>/graph/<commit_sha>/manifest.json
    tenants/<tenant>/repos/<repo_id>/graph/blobs/<sha256>.jsonl.zst

The manifest lists the shards and their digests, the `languages` table and
the counts. Shards are content-addressed blobs: symbols sharded by module,
edges sharded twice (by the caller's module and by the callee's, so "who
calls X" reads only X's module's reverse shard), and `symbol_test_map` by
module. An incremental run writes only the shards whose content changed and
a new manifest naming the unchanged blobs by digest, so twenty incremental
commits cost twenty manifests and a handful of blobs, not twenty graphs.
Sizes, estimated for budgeting and to be measured by lane RI9: a 2,000-file
Python service is about 30,000 symbols and 150,000 edges, 20 MB as JSON
lines and 4-6 MB compressed; a 10,000-file monorepo about 30 MB compressed.
The hard ceiling is **256 MiB per commit**; a graph over it keeps the
module-level edges and drops symbol edges below confidence 0.4, and says so
in the manifest's `truncated`. Graph manifests are kept for the last 20
index versions (§2.3) and for every commit an open pull request's impact plan
names, for 30 days; unreferenced blobs are deleted by a sweep in the RI9
lane.

**The write is resumable (revised 2026-10-06, lane IX1).** Blobs go up
first, the manifest last. A blob already at its content-addressed path is
*written* when its bytes are ours -- checked against the listing's MD5, or
by reading it back -- so a write interrupted after any number of blobs
completes when it is run again, and a path holding different bytes is a hard
error: nothing overwrites it and no manifest is written over it. Blobs go up
in batches (one `gcloud storage cp` of many files, which gcloud uploads in
parallel), never one process per blob. Measured on
`task_209ba9e0c9c948e284e9` (2026-10-06): the serial writer took ~3.9 s a
blob, ~24 minutes for a full graph's ~370, more than an index run has; and
its retry failed with `HTTPError 412` on its first blob because the store
listed each object by its url, which on the versioned artifact bucket ends
in `#<generation>`, so no blob ever read as present (and the sweep, on the
same listing, never saw a manifest). The listing now names each object by
its name.

**Invariant 9 holds without a new grant.** The prefix is under
`tenants/<tenant>/`, which the tenant's own worker service account may
already write — everything there except `verdicts/`
(`terraform/modules/tenancy/main.tf` (`!${local.verdicts_prefix[t]}`)) — and which no other tenant's
account can read. The indexer writes the shards directly; swarm-api reads
them as it reads `plan.json`. The digest of the manifest is recorded in the
`index_versions` entry at promotion (§2.3), so a shard rewritten after
promotion is detected, not served. If the owner makes the selection a merge
gate (§4.4, P1 or P3), the graph decides what a pull request must pass, and
"any agent in the tenant can write the prefix" becomes a gate an agent could
shrink: the `graph/` prefix should then be carved out of the worker's write
grant the way `verdicts/` is, and written only by an agent-free indexer step
(§6.3, request B). That is a Terraform change, not a frozen one, and it is
listed in §7.

---

## 3. How the index is built

### 3.1 The indexer step

An index run is **one ordinary task**, submitted by swarm-api on the tenant's
behalf through the same signed `submit_tasks` path every task takes:

| setting | phase 1 value | why |
|---|---|---|
| runner profile | `claude-code` | the only enabled agent profile; a purpose-built profile would be a frozen-contract change (§6.3, optional request B) and is not needed to start |
| resource class | the profile's own, `standard` | indexing is reading and one tool run, not a build |
| `repository_url` / `repository_ref` | the registration's URL and the exact sha being indexed | the index describes a commit, not a moving branch |
| `timeout_seconds` | 1,800 for a full run, 900 for an incremental one | may only shorten the profile's timeout; a run that cannot finish in 30 minutes is a repository that needs the directory-granularity fallback, not more time |
| `priority` | −50 | below the tenant's default-0 work: an index makes work better, it is not the work |
| `metadata` | `{"repo_index": "<repo_id>", "commit_sha": "<sha>", "index_kind": "full"}` | how the promotion path (§2.3) finds a finished index run |
| prompt | fixed, composed by swarm-api; never a caller's text | invariant 10: the caller chooses a repository and a schedule, nothing that runs |

It writes exactly one file, `$SWARM_ARTIFACTS_DIR/repo-index.json`, and
changes nothing in the repository; it does not publish, so it opens no pull
request and pushes no branch.

**At most one index run per registration is in flight.** A trigger that
fires while one is QUEUED or running records the newer head on the
registration (`index.pending_sha`) and does nothing else; when the running one
ends, the newest pending sha is indexed, once. Ten pushes in a minute cost one
run, not ten.

### 3.2 Indexing is ordinary capacity-accounted work (invariants 1-3)

Nothing about indexing bypasses admission, and that is deliberate: the
alternative — a side channel that runs index jobs outside the scheduler — is
exactly how a platform ends up with work nobody counted.

**Invariant 1.** A scheduled or triggered index run is a QUEUED task, which
creates no infrastructure demand: no Job execution, no pod, nothing pending.
A registration with a schedule and no due run is a Firestore document and
nothing else. The schedule never creates a pending pod as a backlog; it
creates a QUEUED task, or records a pending sha on an existing one.

**Invariant 2.** The index run's lease is acquired by the same
`acquire_lease_in_transaction` as any task, reserving every pool its profile
needs (global, tenant, profile, provider) all-or-nothing in one Firestore
transaction. Indexing adds no pool and reserves none of its own.

**Invariant 3.** It counts against the tenant's `max_active` from the moment
it is LEASED, like any task, so a schedule that fires for twenty repositories
at once cannot oversubscribe a slow-starting tenant. Indexing spends the
tenant's own capacity and only the tenant's own: one tenant's twenty
registrations queue behind that tenant's limits, never another's. A tenant
that wants its capacity for work can pause the schedule (§3.3), and the
console shows index runs among the tenant's agents like any other task, so the
cost is visible where capacity is read.

The worker rules that bind any task bind this one: it checkpoints
periodically (invariant 8), carries a fencing generation and exits without
running if it is stale (invariant 5), and parks on a provider wait rather
than sleeping (invariant 4).

### 3.3 Triggers: an interval, and a change on the default branch

Each registration carries:

| setting | default | range | why |
|---|---|---|---|
| `index.interval_hours` | 24 | 1-168, or `off` | the backstop: a repository whose pushes are never detected is still re-indexed |
| `index.on_change` | `poll` | `poll`, `webhook` (phase 3), `off` | the default branch moving is the event the owner named |
| `index.min_change_interval_minutes` | 30 | 10-1,440 | a busy repository merging every five minutes is indexed at most twice an hour by change; the interval still applies |
| `index.full_every_days` | 7 | 1-30 | incremental runs drift; a weekly full run resets them |
| `index.paused` | false | — | an operator stops all indexing of the repository without deleting the record |

**Polling the forge (phase 1).** A per-tenant Cloud Scheduler job,
`repo_index_poll`, calls `POST /v1/admin/repositories/poll?tenant_id=<tenant>`
every 5 minutes, the pattern `issue_run_advance` already uses
(`terraform/modules/scheduler/jobs.tf` (`resource "google_cloud_scheduler_job" "issue_run_advance"`)), and like it says
`managed-by=swarm-terraform` in its description because a Cloud Scheduler job
has no labels. The route reads, for each of the tenant's registrations whose
`on_change` is `poll`, `GET /repos/{owner}/{repo}/commits/{default_branch}`
with the tenant's token and the last response's `ETag`; GitHub answers an
unchanged branch `304 Not Modified`, which does not count against the
token's rate limit, so polling forty repositories every five minutes costs
almost nothing. The route stores `head_sha` and `head_read_at` on every
registration it read — the head is what staleness is measured against
(§5) — and queues an index run where the head moved, the minimum change
interval has passed and no run is in flight. The interval trigger is checked
in the same pass: `last_indexed_at + interval_hours` passed means queue one.

**A webhook (phase 3, optional).** A GitHub `push` webhook would cut the
detection delay from five minutes to seconds. It is not first because it is
the only part of this design that needs an **unauthenticated** route on
swarm-api (GitHub cannot present a Google ID token), verified instead by an
HMAC over the body with a per-registration webhook secret held in Secret
Manager. That is a new kind of ingress, and polling every five minutes is
already fast enough for "the planner has today's index". The webhook, when
built, only records a pending head; the same poll route queues the run, so
the two triggers cannot double-queue.

### 3.4 Incremental and full

A **full** run reads the whole tree. An **incremental** run is given the
previous index's JSON (staged as an `input_from` file from the earlier index
task) and `git diff --name-status <base_sha>..<head>`, and rewrites only the
entries for changed paths, carrying the rest forward with their original
`commit_sha` on each module so a consumer can tell which entries are fresh.

Incremental is chosen when all of these hold, and full otherwise: a previous
index exists for an ancestor of the head; the diff touches fewer than 300
files; no build or test configuration changed (`Makefile`, `pyproject.toml`,
`package.json`, lockfiles, anything under `.github/workflows/`, a `conftest.py`
or a test runner config), because those change what `commands` and
`test_map` mean everywhere; and the last full run is younger than
`full_every_days`.

**The mechanical half is a tool, not tokens.** The file tree, line counts,
the language of each module, the import graph that grounds `test_map`
`import` edges, the naming-convention edges (`src/x/y.py` ↔
`tests/**/test_y.py`), the co-change pairs and the hot-spot counts from `git
log --numstat --since=90.days` are deterministic and cheap. Lane RI3 ships
them as one script in the `agent-runtime-indexer` image (in `agent-runtime-base` until #625; see [worker-images.md](worker-images.md)), which the worker
runs before the agent (§3.6; until 2026-10-06 the prompt told the agent to
run it); the agent then spends its tokens on what
needs reading: purposes, territory, notes, and checking the edges the tool
was unsure of. That is what keeps a full run on a 2,000-file repository under
the 30-minute timeout.

*Revised 2026-10-04:* RI3's tool is now a tree-sitter pass, not a regex and
import scan: the file tree, the symbols and the `ast` and `import` edges all
come from one parse per file (§3.5). The timeouts in §3.1 were RI0's; §3.5's
budget table replaces them for any run that builds the graph.

**Built (revised 2026-10-06, owner, lane IX2).** Until this lane
`check_run_kind` refused `incremental`, because the design above stages the
previous index as an `input_from` file and a standalone index task cannot
be given one (a plain task's `metadata.input_from` is refused at submission,
`validation._RESERVED_BECAUSE`). Every trigger was therefore a full rebuild:
on 2026-10-06 the run for `36ac73bd` re-indexed all 1,685 files two commits
after the previous index (`fe9e69c6`). What runs now, and why each piece is
shaped the way it is:

* **The base is staged by reference, not copied.** swarm-api names it in the
  task's prompt, on a line of its own (`swarm-index-base: <sha>`,
  `repoindex.BASE_LINE`). The prompt is inside the signed `input`;
  `metadata.index_kind` and `base_sha` are swarm-api's own record and are
  outside the spec signature, so the worker never reads them
  (`tests/unit/common/test_specsign_covers.py`), and a signed metadata key
  would need `SIGNED_METADATA_KEYS`, which is frozen. Before the extractor
  the worker runs a fourth phase, `stage_base`
  (`agent_worker/indexrun.py`): it reads the promoted version
  (`repositories/<repo_id>/index_versions/<base_sha>`, under the repo_id the
  signed spec derives), fetches that version's task's `repo-index.json`
  through the staged-input path a workflow input takes
  (`inputs.fetch_upstream_task`, `inputs.artifact_reference`: the successful
  attempt's manifest, a key inside the tenant's own prefix), checks it
  against the digest promotion recorded, and reads the base graph back from
  its shards with `swarm-repo-graph read`, checked against the recorded
  manifest digest. No new grant: the step's account already reads its
  tenant's artifacts and graph prefix (invariant 9). The base line is still
  a request: whatever names it can only make the run full or pick another
  promoted version of the same registration, whose content the digests
  vouch for.
* **What changed is measured by blob id, not by `git diff`.** The worker's
  checkout is one commit deep, so `<base_sha>` is not in it; the head's
  tree is. The extractor records each file's git blob id in the graph's
  `files` rows and compares the head's (`git ls-files -s`) with the base's.
  A base graph from before this lane has no blob ids, so the first run after
  it is full.
* **What is re-resolved.** The tree-sitter pass still parses every file
  (seconds; a changed file's calls resolve against every other file's
  definitions). The LSP pass -- the minutes -- is asked only about the
  affected files: the changed ones, every file whose base edges point into
  a changed or deleted file, and every file whose fresh edges point into a
  changed one (a new definition can capture an old call, which §3.5's rule
  alone would miss). Every other file keeps its base edges verbatim, `lsp`
  evidence included, so its shards come out as the same blobs and
  `swarm-repo-graph write --base-commit` counts them carried
  (`shards_carried`) rather than writing them. Deleted files leave every list.
* **The agent's reading is carried.** The extractor's output carries each
  module's base `purpose` and the commit its entry was read at
  (`commit_sha`: the base's for an untouched module, the head's for a touched
  one), the base's entry points, test layout, always-tests, territory,
  commands and notes minus rows naming a deleted file (`carried`), and the
  diff (`changes`). The agent revises only what the diff touches.
* **Who decides.** swarm-api first (`repoindex.choose_kind`, on the promoted
  version and GitHub's compare): a promoted index with a graph, of an
  ANCESTOR of the head (`ahead`), fewer than 300 changed files (GitHub's
  compare lists at most 300, so 300 is exactly what this side cannot see
  whole), no build, test, lockfile, CI or language-server configuration
  changed, and the last full run younger than `full_every_days`
  (`last_full_at`, written only when a full index is promoted). Then the
  extractor again, on the diff it measures (`incremental_changes`, the same
  lists, held equal by `tests/unit/worker/test_repo_index_incremental.py`).
  Either one falling back makes the run full and records why: the run's
  `kind_reason`, or the extractor's `extractor.incremental.reason` and the
  `stage_base` phase record. A fallback is never a failed run.
* **What is recorded.** The run: `kind`, `base_sha`, `requested_kind`,
  `kind_reason`; the version and the promotion: the document's `kind` and
  `base_sha`. Promotion refuses an index that claims a base its run was not
  given. "Index now" with no kind stays full; the console's button, the
  poll and a pending head ask for `incremental`.

§3.5 asks for a configuration change to force a full run *for that
language*; it forces the whole run full, because one index carries one
`kind`.

### 3.5 The AST and LSP passes (revised 2026-10-04, owner)

The owner accepted about ten times RI0's extractor time for a graph a
consumer can trust below the file. The indexer runs, in order:

1. **The tree-sitter pass.** One parse per source file with the grammar for
   its language (Python, TypeScript/TSX, JavaScript, Go, HCL in phase 1; any
   other grammar tree-sitter ships can be added as data, without a server).
   Per-language queries extract definitions — functions, classes, methods,
   and routes from the frameworks' own shapes (a FastAPI or Flask decorator,
   an Express `app.get`, a Go `HandleFunc`, a Terraform `resource`, `module`
   or `variable` block) — with their line ranges, and every call site and
   import. That yields `symbols`, `import` edges and candidate `ast` edges.
   It is fast (seconds for thousands of files) and needs nothing installed.
2. **The LSP pass, one server per language, run headless.** pyright
   (`pyright-langserver --stdio`), tsserver (through
   `typescript-language-server --stdio`), gopls (`gopls serve`) and
   terraform-ls (`terraform-ls serve`), each started by the indexer as a
   child process speaking LSP over stdio — no editor, no network listener.
   For each candidate call site from step 1 the indexer asks
   `textDocument/definition`; where the server supports call hierarchy
   (pyright, tsserver, gopls) it asks `callHierarchy/incomingCalls` for each
   exported symbol, and terraform-ls, which has none, answers
   `textDocument/references`. A resolved site becomes an `lsp` edge; one the
   server could not resolve stays `ast`, with its lower confidence.
3. **The test map.** Test functions are symbols too (found by each
   framework's convention: pytest's `test_*`, vitest/jest `it`/`test`
   blocks, Go's `TestXxx`). A breadth-first walk from each test over
   `call_edges`, at most depth 6 and stopping where the path's confidence
   falls below 0.2, gives `symbol_test_map`. RI0's file-level `test_map` is
   kept and derived from it, so its consumers do not change.

**No dependencies are installed.** A language server resolves third-party
symbols only against installed packages (a virtualenv, `node_modules`, the Go
module cache), and installing them runs the repository's own install scripts
and reaches the network. Phase 1 resolves in-repository edges only, which is
what impact needs; a call into a library is recorded as an edge to an
`external:` symbol at `ast` confidence. Installing dependencies in a
sandboxed step is a later option for the owner, not a default.

**Incremental by changed files plus their reverse dependencies.** An
incremental run re-parses the files in `git diff --name-status
<base_sha>..<head>` and, from the previous graph's reverse shards, every file
holding an edge into a symbol those files define or used to define (a
renamed or deleted function's callers must be re-resolved). Only those
files' call sites are re-asked of the servers; the servers still load the
whole workspace, which is most of their cost, so an incremental run is
cheaper in queries, not in start-up. The full-run conditions of §3.4 still
apply, with one more: a change to a language server's configuration
(`pyrightconfig.json`, `tsconfig*.json`, `go.mod`, `.terraform.lock.hcl`)
forces a full run for that language.

**Budget and timeout, by repository size.**

| source files | full run | incremental | per language server | resource class |
|---|---|---|---|---|
| under 2,000 | 20 min | 10 min | 10 min, 10 s per request | the indexer profile's own (§6.3 B) |
| 2,000 - 10,000 | 60 min | 20 min | 30 min, 10 s per request | one size up |
| over 10,000 | 120 min | 30 min | 45 min, 10 s per request | the largest the indexer profile allows |

A language server is memory-hungry (pyright on a large repository holds
several GiB), and requests equal limits (invariant 7), so the indexer's
resource class is sized for the server, not for bursting past it. A server
that exceeds its budget, crashes or exceeds its memory is stopped; its
language is marked `timed_out` or `failing` in `languages` with the reason,
its edges stay `ast`, and the run still succeeds. A timeout is never a
failed index: it is a less certain one, and says so.

**What happens for an unsupported language.** A language with a tree-sitter
grammar and no server listed (Ruby, Java, Rust in phase 1) gets symbols and
`ast` edges only and is marked `unsupported` with "no language server; edges
are syntactic". A language with neither falls back to RI0's file level
(`import` where a regex can find imports, `naming`, `co-change`) and is
marked `unsupported` with "file level only". The console's Settings show
this per language (mock-ups, section 11), and the impact query treats any
changed file in such a language as a fallback trigger (§4.4, P3).

**Where the tooling lives.** Two shapes, for the owner: **one indexer image**
carrying tree-sitter, its grammars and the four servers (about 1.5 GB;
simplest to run, one image to scan and pin), or **one image per language**,
each indexer step a workflow step per language and a merge step combining
their shards (smaller images, a language upgrade without rebuilding the
rest, more steps per index). The recommendation is the single image for
phase 1. Either way it is a pinned image in the platform's registry, not
something a caller names (invariant 10).

*Revised 2026-10-05 (#625):* the single image exists as
`images/agent-runtime-indexer`, built FROM `agent-runtime-base` by digest.
The toolchain had shipped in the base, where it added +188 MB (compressed)
to every agent start and only an index run used it. Contract request 48 (the
image half of request B, §6.3), accepted by the owner the same day, added
the `indexer` profile: claude-code on that image, and what index runs are
submitted as. [worker-images.md](worker-images.md) has the measurement.

### 3.6 The deterministic passes are the worker's steps (revised 2026-10-06, owner)

Until 2026-10-06 the indexer prompt told the agent to run the extractor
first and `swarm-repo-graph write` last, through its shell. Measured on
`task_209ba9e0c9c948e284e9` (`repo_4c5105947752b3f3`, 15:08-15:32): the
extractor wrote `repo-index.json` (1,685 files, 25,541 symbols, 48,717 call
edges), the agent's graph write was killed by Claude Code's 10-minute
command limit after 150 blobs, and its retry met the 412 of §2.5. Neither
pass reads anything a model has to read, so the worker runs them, around the
agent, as its own supervised steps (`apps/agent-worker/agent_worker/
indexrun.py`):

| phase | what | timeout, of a 1,800 s full run |
|---|---|---|
| `stage_base` *(incremental runs only, lane IX2, §3.4)* | the base's `repo-index.json` by the staged-input path, then `swarm-repo-graph read --commit <base_sha> --manifest-digest <recorded> --out $SWARM_WORK_DIR/repo-graph.base.json` | a quarter of the extractor's budget (90 s of an incremental run's 360) |
| `extract` | `swarm-repo-index --repo <checkout> --out $SWARM_WORK_DIR/repo-index.extract.json --graph-out $SWARM_WORK_DIR/repo-graph.json --lsp-total-budget-seconds <extract - 180>` | 0.4 of the task's timeout, 720 s: twice the ~6 minutes measured |
| `agent` | the runner, from the extractor's output | what is left, less the write's reserve |
| `graph_write` | `swarm-repo-graph write --graph ... --index $SWARM_ARTIFACTS_DIR/repo-index.json --repo-id <r> --destination tenants/<t>/repos/<r>/graph` | 0.15 of the task's timeout, 270 s, reserved before the agent starts |

Why each rule:

* **Supervised like the runner.** A phase beats the lease, polls the
  control plane and checkpoints on the runner's cadences (invariants 5 and
  8): the extractor takes longer than `_heartbeat_meanwhile`'s bound. A
  fence, a cancel or a SIGTERM ends the attempt exactly as it would mid-agent.
* **A phase never fails the run.** An extractor that is missing, fails or
  times out leaves the agent to compute the mechanical fields itself, as the
  prompt has always allowed; `$SWARM_WORK_DIR/repo-index.phases.json` tells
  it why. A graph write that fails leaves the index without
  `graph.manifest_digest`, which promotion reads as "no graph"; the write is
  resumable, so the next run completes it. A timeout is a less certain
  index, never a lost one (§3.5).
* **Each phase's duration is recorded**, in the step's
  `result_summary.repo_index_phases` and in the phases file, so "where did
  the 30 minutes go" is answered from the run, not from a log search.
* **The target comes from the signed spec** (invariant 9). The tenant and
  the bucket are the worker's own configuration; the `repo_id` is derived
  from the spec's tenant and `repository_url` by the registration's recipe
  (`repositories.repo_id_for`). `metadata.repo_index`, which the spec
  signature does not cover, is never read by the worker, so a rewritten
  metadata cannot point the write at another registration.
* **The prompt starts from the extractor's output** and no longer names
  either command line. It still names the graph prefix, as the one place the
  agent must never write.

Promotion is unchanged: it still checks the index's `graph.manifest_digest`
against the manifest the writer stored (§2.3, `repograph`).

Not in this change: the extractor's own time (~6 minutes, most of it
Pyright reaching its 300 s server budget). `full_every_days` is read since
lane IX2 (§3.4).

---

## 4. How the index is consumed

### 4.1 The issue-run planner (#454)

`POST /v1/runs` already reads the forge for the planner and puts the open work
in its prompt between two delimiter lines carrying the run id, as data
(`planner_prompt`, `apps/swarm-api/swarm_api/issueruns.py::planner_prompt`). The index
joins it the same way, **in phase 1, with no frozen-contract change**: when a
registration matches the issue's `owner/repo` and has a current index,
`planner_prompt` adds a section

    === REPO INDEX <run_id> <commit_sha> ===
    (repo-index.md, at most 24 KiB, with the staleness line of §5 first)
    === END REPO INDEX <run_id> ===

before the open work, inside the same 64 KiB budget, and the run stores
`index_sha` and `index_digest` beside `open_work`, so the plan records which
index it was made from and the console can show it as a "context used" chip.
The planner's instructions gain one sentence: use the index's `test_map` to
fill each step's `tests`, and its `territory` to keep steps out of files the
repository says are frozen. The planner still clones the repository: the
index tells it where to look, not what the code says.

A planner never **waits** for an index. If the registration has no current
index, the planner runs without one and the run says `index: none` with the
reason; if an index run is in flight, the planner uses the current (older)
index with its staleness line. Holding a planner for an index would hold an
issue run on work it can do without.

The compiled implement steps get the slice that concerns them: for each plan
step, the `tests:select` answer (§4.3) for the step's planned `files`, as a
short list in the step's prompt, which `compile_plan` composes today anyway.

### 4.2 Any step in the same repository, by name (invariant 10)

A task or workflow step whose `repository_url` matches a registration asks for
the index **by name**, never by path: phase 2 adds one runner input to
`claude-code` and `codex`,

    "input": {"prompt": "...", "repo_index": true}

and the worker stages it exactly as it stages `input.issue` today
(`apps/agent-worker/agent_worker/issue.py`): it resolves the registration from
the task's **own** tenant and `repository_url`, reads the current version's
objects with the tenant's own service account, checks the digest, writes
`work/repo-index.md` and `work/repo-index.json` beside the checkout — so they
are in no diff and no pull request — and adds one prompt line naming them.
A caller cannot name an object, a sha, a bucket or another repository's index:
the only value is `true`. That is invariant 10 applied to context: a caller
picks a runner profile by name and, now, a context by name, and the platform
resolves everything behind the name. A step asking for an index its
repository does not have starts without it and its prompt line says so; a
missing index is context missing, not an input failure, so it never ends a
task `INPUTS_UNAVAILABLE`.

Phase 2 needs the frozen-contract request in §6.3, because `_CLI_AGENT_INPUTS`
(`apps/common/swarm_common/profiles.py::_CLI_AGENT_INPUTS`) declares `issue` as the only
input those profiles take, and swarm-api refuses an undeclared input.

### 4.3 "What tests should run for this diff"

`POST /v1/repositories/{repo_id}/tests:select` takes changed paths and
answers which tests cover them, from the current index's `test_map`:

    {"paths": ["src/api/routes/users.py", "src/api/models.py"]}
    ->
    {"index_sha": "...", "head_sha": "...", "behind_by": 3, "stale": false,
     "tests": [{"target": "tests/api/test_users.py", "command": "uv run pytest tests/api/test_users.py -q",
                "because": ["src/api/routes/users.py"], "evidence": "import"}],
     "always": [{"target": "tests/unit/test_contract.py", "because": "declared in CLAUDE.md"}],
     "unmapped": ["src/api/models.py"],
     "fallback": "uv run pytest tests/api -q"}

`unmapped` is the honest half of the answer: a changed path the map has no
edge for is listed, and the `fallback` is the narrowest suite whose directory
contains it, never an empty list that reads as "no tests needed". Paths are
data: the route matches them against the index and never touches a
filesystem, so a `../` in one is a path that matches nothing.

Who calls it:

* **implement and fix steps** — in phase 1 through their prompt (§4.1), in
  phase 2 through the staged JSON, which carries the whole map so the agent
  can select for its own diff after it is written. CLAUDE.md asks an agent to
  run the offline unit tests for what it touched; this is how it knows which.
* **the merge step** (docs/merge-step.md, gated on #342) — before merging, it
  asks for the pull request's changed paths and records in its checklist
  which selected tests ran in CI and which did not. **Advisory in v1**: the
  merge step's gate stays the required GitHub checks (merge-step.md §5.2); a
  selection derived from an index an agent wrote is context, not a gate.
  *Revised 2026-10-04:* the owner asked for the selection to decide what a
  pull request must pass. §4.4 sets out three ways it can, for the owner to
  pick; until a pick, this paragraph stands.
* **the operator**, through the console's repository page and an MCP tool
  (`swarm_repo_tests`, lane RI5), before turning a local commit into a pull
  request.

---

### 4.3a The commit / pull-request impact query (revised 2026-10-04, owner)

`tests:select` answers from paths. The impact query answers from symbols:
diff → changed symbols → transitive callers, to a bounded depth → covering
tests → a test plan with a reason per test.

    POST /v1/repositories/{repo_id}/impact
    {"pull_request": 57}            or   {"commit": "<sha>"}
                                    or   {"base": "<sha>", "head": "<sha>"}
    {"depth"?: 3}

1. **The diff.** swarm-api reads it from the forge — the pull request's files,
   or `GET /repos/{o}/{r}/compare/{base}...{head}` for a commit (its first
   parent is the base) — with the token that resolves for the repository
   ([git-tokens.md](git-tokens.md) §3). The hunks give changed line ranges
   per file; nothing is checked out.
2. **Changed symbols.** Each hunk is matched against the graph of the nearest
   indexed commit: removed and modified lines against the **base**'s
   symbols (what the change touched), added lines against the head's if it
   is indexed. A symbol the head adds that no index has seen yet is listed as
   `unindexed`, and its file falls back to file level.
3. **Transitive callers, bounded.** A breadth-first walk over the reverse
   `call_edges` from each changed symbol, to `depth` (default 3, at most 6,
   per repository in Settings), stopping a path whose confidence product
   falls below 0.2 and recording where it stopped (`low_confidence_cut`), so
   a short answer is never mistaken for a complete one.
4. **Covering tests.** The union of `symbol_test_map` for every changed and
   affected symbol, every test file the diff itself changes, and the
   `always` set.
5. **The test plan**, each test with its reason:

       {"base_sha": "...", "head_sha": "...", "index_sha": "...", "stale": false,
        "changed_symbols": 4, "affected_callers": 17, "selected": 12, "total_tests": 1480,
        "tests": [{"id": "tests/api/test_orders.py#test_empty_cart_total",
                   "command": "uv run pytest 'tests/api/test_orders.py::test_empty_cart_total' -q",
                   "reason": "reaches OrderService.total (changed) via checkout_total, depth 2",
                   "evidence": ["lsp", "lsp"], "confidence": 0.9}],
        "fallback_triggers": [{"kind": "test_config_changed", "path": "tests/conftest.py"}],
        "unmapped": [...], "low_confidence_cut": [...]}

`fallback_triggers` lists what §4.4's policy P3 would fall back on, under
every policy, so the console can show it whichever policy is picked: a build
or test configuration file, a shared fixture (`conftest.py`, a `fixtures/`
or `testutils/` module), a changed symbol with an edge below 0.4 on its path
to every test that reaches it, a changed file in a language marked
`unsupported`, `failing` or `timed_out`, an `unindexed` symbol, and an index
that is stale (§5). The query reads only the reverse shards of the modules on
its frontier, so its cost scales with the change, not the repository. The
same answer serves a commit and a pull request; the console draws both
(mock-ups, section 9).

### 4.4 Test selection for merge (revised 2026-10-04, owner)

The owner asked that the selection, based on the AST for a repository or a
commit, "choose what tests need to be ran before a PR is green and can be
merged". Three policies, written out for the owner, not decided here. The
mock-ups draw one screen per policy (section 10), so the owner picks the
policy by picking the screen. Each is set per tenant as a default and may be
overridden per repository (`selection_policy`, §6.2).

**(P1) Selected tests are the merge gate.** A pull request is green when its
selected tests pass. The full suite runs on a schedule (nightly by default)
and on the default branch after each merge; a failure there opens an issue
through the bug form with the failing tests, the merged pull requests since
the last green full run, and the impact plans that left the failing test
out. Fastest feedback and cheapest; a selection miss reaches the default
branch and is caught after the fact.

**(P2) Selected tests run first as a fast gate; the full suite is still
required.** The selected tests report within minutes and a failure stops the
pull request early; green still needs the full suite. No change to what
merging means today, only a faster red. Cheapest to trust, saves no CI time
on a green pull request.

**(P3) Selected tests gate merge, except where the selection cannot be
trusted, which falls back to the full suite.** The fallback triggers are
§4.3a's `fallback_triggers`: build or test configuration touched, a shared
fixture changed, a changed symbol reached only through low-confidence edges,
an unsupported or failing language, an unindexed symbol, a stale index. The
check's summary names the trigger. Most pull requests get P1's speed; the
ones the graph cannot speak for get P2's certainty.

**How "green" reaches the forge.** As one check run on the pull request's
head sha, named `swarmcloud/selected-tests`, whose summary carries the plan:
"12 of 1,480 selected · policy P3 · no fallback", then each test with its
reason, and for P2 or a P3 fallback the full suite's result. Its conclusion is
`success` or `failure`; never `neutral`, which the merge step treats as not
green for a required check (merge-step.md §5.2). A check run can only be
created by a GitHub App, so the poster is either a SwarmCloud checks App or
GitHub Actions itself — the two execution modes below.

**How the merge step reads it.** The merge step (#295, lane M1a; the owner's
decisions are recorded on #295) already reads the branch's required checks
from GitHub's rules endpoint, each pinned to an `app_id`, and requires every
one to be completed with `success` or `skipped` at the pinned sha
(merge-step.md §5.2); a required check with no `app_id` is refused. So the
selection needs **no special case in the merge step**: the repository's
ruleset lists `swarmcloud/selected-tests` as a required check pinned to the
App that posts it, and the merge step reads it alongside the repository's
other required checks exactly as it reads them. What differs by policy is
the ruleset, and the console's repository Settings say which it should be:
under P1 the full-suite checks leave the required set; under P2 they stay;
under P3 they leave it, and `swarmcloud/selected-tests` itself runs the full
suite when it falls back. Under every policy the repository's lint, type and
build checks stay required: the selection is about tests only.

**Two ways to run the selected tests in a repository whose CI is GitHub
Actions:**

* **(X1) SwarmCloud runs them itself, in a test step.** A workflow step after
  the implement and fix steps runs the plan's commands — taken from the
  repository's own index (`commands`, `test_layout`), never from a caller
  (invariant 10) — in the tenant's worker, admitted and counted like any task
  (invariants 1-3). A separate agent-free step, the way `post-verdict` is in
  merge-step.md, posts `swarmcloud/selected-tests` with a SwarmCloud checks
  App whose key is `swarm-tenant-<tenant>-git-checks`, read only by that
  step. Works for any CI and keeps the repository's CI minutes untouched;
  it does not reproduce the repository's CI environment (service
  containers, CI secrets), so a test that needs one is a fallback trigger.
  Needs request (C).
* **(X2) SwarmCloud passes the list to the repository's CI.** SwarmCloud
  calls `workflow_dispatch` on a workflow the repository adds
  (`swarm-selected-tests.yml`), with inputs carrying the head sha and the
  test ids. The dispatch is made on the **default branch's** copy of the
  workflow, which checks out the head sha — so a pull request cannot rewrite
  the workflow that judges it. A `workflow_dispatch` run's own check suite
  and job check runs attach to the commit the dispatched ref points at (the
  default branch's HEAD, `GITHUB_SHA`), **not** to the sha the job checks
  out, so the job's own check run is never on the pull request and is not
  the gate. The workflow therefore **posts the gate itself**: its last step
  creates a check run with the Checks API (`POST
  /repos/{owner}/{repo}/check-runs`, `head_sha` = the input sha, `name` =
  `swarmcloud/selected-tests`, the plan in its summary) using the job's
  `GITHUB_TOKEN` with `permissions: checks: write`. That check run is
  attributed to GitHub Actions' `app_id`, so the ruleset still pins it to
  GitHub Actions' `app_id` as every rule in docs/ci.md does, and the merge
  step reads it at the pinned head sha like any required check. A commit
  status cannot stand in for it: a status has no `app_id`, and merge-step.md
  M5 refuses a required check without one. Needs the
  `workflow_dispatch` capability on the resolved token
  ([git-tokens.md](git-tokens.md) §5.1) and a workflow file in the
  repository. Inputs are strings with a size limit, so a plan too long for
  them falls back to the full suite and says so.

The choice of X1 or X2 is per repository and is also for the owner;
the mock-ups show both on the PR card.

**Integrity, stated because a gate is now at stake.** Under P1 and P3 the
graph decides what must pass, so whoever can write the graph can shrink the
gate. §2.5 says how that is closed (an agent-free indexer step, request B,
and the `graph/` prefix carved out of the worker's write grant), and P3's
fallback and P1's scheduled full suite are what catch a selection that was
wrong without being forged.

---

## 5. Staleness

### 5.1 An index older than the head it describes says so

Every place an index is served carries three values: `index_sha` (what it
describes), `head_sha` with `head_read_at` (the default branch's head when last
polled) and `behind_by` (commits between them, from GitHub's compare API,
read when the head is polled). The rules:

* **current** — `index_sha == head_sha`.
* **behind** — the index describes an ancestor of the head. Served, with the
  line "This index describes `<index_sha>`, N commits behind `<head_sha>`
  (read <time>); files changed since: …" first in every rendering, and up to
  50 of the changed paths, so a consumer knows which parts to distrust.
* **stale** — behind by more than 200 commits, older than 7 days, or the
  index's sha is not an ancestor of the head (a force-push): served with
  `stale: true`, the console draws it in the warning colour, and an index run
  is queued if none is in flight.
* **unknown** — the head has never been read (polling off, or the token lost
  access): the freshness is a dash with that reason, never "current".

A consumer is never handed an index without these, so "the index said there
were no tests for that file" can always be answered with "the index was 40
commits old and the file was added since".

### 5.2 Security notes (minors; functionality first)

Tenant isolation is the one property that is not a minor, and §2.3 and §2.4
are how it holds: the index is written and read only under the tenant's own
prefix, with the tenant's own credentials, and never shared. The rest are
minors, listed so they are decided rather than discovered:

* **The index is agent-written text.** A repository's own content can steer
  the indexer, and an agent in the same tenant can overwrite an artifact
  after it is written. The digest recorded at promotion catches the second;
  the first is bounded by the schema (data fields only, no instructions
  field), by the planner's delimiters, and by the prompt line calling it
  data. A minor because it is the same tenant's own repository and agents.
* **Paths and purposes are masked like open work.** Every string goes through
  the same `neutral_line` masking `read_open_work` applies before it is
  stored or rendered, with the tenant's token as a literal to redact.
* **The webhook secret** (phase 3) is a Secret Manager secret per
  registration, never a field on the record, never in a log line.
* **`tests:select` reveals file paths** to anyone in the tenant, which the
  repository's clone already does; the impact query and the graph explorer
  (revised 2026-10-04) reveal symbol names the same way.
* **A merge gate built on the graph** (§4.4 P1 or P3) is not a minor: it is
  why §2.5 asks for an agent-free indexer and a carved `graph/` prefix
  before either policy is enabled.
* **Git tokens** have their own rules in [git-tokens.md](git-tokens.md) §5.4:
  served as labels, never as values, and never in an `aria-label` or a copy
  button.

---

## 6. API sketch

### 6.1 Routes

All tenant-scoped exactly as issue runs are: the caller's tenant is resolved
from their identity, and another tenant's `repo_id` is a 404
indistinguishable from a missing one.

| route | does |
|---|---|
| `POST /v1/repositories` | register: `{"repository": "owner/repo", "default_branch"?: "...", "allowed_profiles"?: [...], "index"?: {...}}`; reads the forge once (§1); idempotent on `repo_id` |
| `GET /v1/repositories` | the tenant's registrations with freshness (§5), last index run, schedule and `test_map` coverage |
| `GET /v1/repositories/{repo_id}` | one registration, with its last 20 index runs and the runs and workflows that used its index |
| `PATCH /v1/repositories/{repo_id}` | schedule, trigger, `allowed_profiles`, `default_branch`, `paused` |
| `DELETE /v1/repositories/{repo_id}` | unregister; typed confirmation in the console; index objects are left to the artifact lifecycle |
| `POST /v1/repositories/{repo_id}/index:run` | queue an index run now (`{"kind": "full" \| "incremental"}`); the in-flight rule of §3.1 applies |
| `GET /v1/repositories/{repo_id}/index` | the current index's summary and freshness; `?sha=` for a kept version; `?format=json` for the structured document |
| `POST /v1/repositories/{repo_id}/tests:select` | §4.3 |
| `POST /v1/admin/repositories/poll?tenant_id=` | the Cloud Scheduler job's route (§3.3); admits only the scheduler's OIDC identity |
| `GET /v1/repositories/{repo_id}/graph` *(revised 2026-10-04)* | the module dependency graph of the current (or `?sha=`) version, aggregated for drawing: modules, weighted edges, per-module hot-spot and test-reach figures; `?cluster=package` |
| `GET /v1/repositories/{repo_id}/symbols` *(revised)* | `?q=` symbol search; `?id=<symbol>&depth=2&direction=callers\|callees\|both` the call graph centred on one symbol, each edge with evidence and confidence; `?id=<symbol>&tests=1` its test map |
| `POST /v1/repositories/{repo_id}/impact` *(revised)* | §4.3a: a pull request, a commit or a base..head range → the test plan |
| `GET /v1/repositories/{repo_id}/languages` *(revised)* | the `languages` table: per language, grammar, server, status and fallback |

The graph routes read the shards of §2.5 with swarm-api's existing read of
the tenant's prefix, and answer from the manifest named by the version's
recorded digest. Every answer carries the staleness values of §5. Token
routes are in [git-tokens.md](git-tokens.md) §7.

### 6.2 Firestore documents

Kept by their own module, read and written there only, not through
`store.py` or `codec.py`, for the reason `issue_runs` gives
(`apps/swarm-api/swarm_api/issueruns.py` (`THIS MODULE KEEPS ITS OWN DOCUMENT`)): the shape is not the frozen
contract's and must not leak into it.

    repositories/{repo_id}
      tenant_id, forge, owner, repo, repository_url, default_branch,
      allowed_profiles, created_by, created_at, updated_at,
      index: {interval_hours, on_change, min_change_interval_minutes,
              full_every_days, paused,
              current_sha, current_digest, last_indexed_at, last_kind,
              head_sha, head_read_at, behind_by, etag,
              pending_sha, in_flight_task_id, coverage},
      graph: {depth (default 3, 1-6), min_confidence (default 0.2),
              languages_enabled}                   (revised 2026-10-04)
      selection_policy: {policy (P1|P2|P3|off), mode (X1|X2),
                         inherited_from_tenant}   (revised 2026-10-04)

    repositories/{repo_id}/index_versions/{commit_sha}
      task_id, attempt_id, json_object, md_object, digest, kind, base_sha,
      built_at, bytes, truncated,
      graph_manifest, graph_digest, languages      (revised 2026-10-04)

    repo_index_runs/{task_id}
      tenant_id, repo_id, commit_sha, kind, trigger (interval|change|manual),
      state (mirrors the task), queued_at, ended_at, end_cause

    issue_runs/{run_id}                     (existing; two new optional fields)
      index_sha, index_digest

    impact_plans/{plan_id}                  (revised 2026-10-04)
      tenant_id, repo_id, pull_request, base_sha, head_sha, index_sha,
      policy, plan_object, selected, total_tests, fallback_triggers,
      check_run_id, conclusion, created_at

    tenant_settings/{tenant_id}.selection_policy   (revised; the tenant default)

`repo_index_runs` duplicates a little of the task for one reason: the
repository page lists a repository's index runs, and a query over all tasks by
a metadata key is a composite index on a collection every other lane writes.
Indexes: `repositories (tenant_id, owner, repo)` and
`repo_index_runs (tenant_id, repo_id, queued_at desc)`.

### 6.3 The frozen contract

**Phase 1 needs no change.** Registrations, index runs and promotion are
swarm-api documents; the indexer is `claude-code`; the planner gets the index
through its prompt, which is composed in swarm-api; no state, event type,
model or admission rule changes. An index run's task is a `Task` like any
other, its events are the task's events.

**Phase 2 needs one request (A)**, to be filed in
[contract-change-requests.md](contract-change-requests.md) when the owner
picks this design (next free number at the time of writing: 47). It is
written here, not filed, because this lane's territory is this document and
the mock-up:

* *What is true today:* `_CLI_AGENT_INPUTS` in `profiles.py` declares one
  input, `issue`, for `claude-code`, `codex` and `claude-code-review`, and
  swarm-api refuses any other input key on those profiles.
* *The requested change:* add `"repo_index": RunnerInput("boolean", means=
  "stage the tenant's current index of the task's own repository as
  work/repo-index.md and work/repo-index.json, named in the prompt")`.
* *What it would break if accepted:* nothing existing; the input is optional
  and false by default. The worker gains a staging step beside
  `stage_issue`, and `tests/unit/mcp/test_runner_inputs.py` gains the key.
* *If it is declined:* only steps whose prompt swarm-api composes (the
  planner and the compiled issue-run steps) get the index; a hand-written
  workflow step does not, and the owner's "accessed by other workflows or
  tasks working in the same repo" is met only for issue runs.

**Optional request (B), only if the owner wants it:** a `repo-indexer`
runner profile in `RUNNER_PROFILES` — the same image, a smaller resource
class, a 30-minute timeout, no `publish` — so index runs are visible as their
own profile in capacity and cost views and can be given their own profile
pool ceiling. Not needed to build anything above.

*Revised 2026-10-04:* with the AST and LSP passes, (B) changes shape and
becomes **required if the owner picks P1 or P3**. The profile is the indexer
image of §3.5, a resource class sized for the language servers (requests
equal limits, invariant 7), the budget table's timeouts, and **no agent**:
the tool pass runs as a worker-run step, as `merge` and `post-verdict` do, so
no agent in the tenant writes the graph a merge gate reads. The agent's
reading (purposes, territory, notes) stays a `claude-code` step after it.

*Revised 2026-10-05 (#625):* the image half of (B) is contract request 48,
accepted by the owner: the `indexer` profile is `claude-code` on
`agent-runtime-indexer` (same resource class, timeouts and inputs), and index
runs are submitted as it. The agent-free, worker-run shape above is still
the later change.

**Request (C), revised 2026-10-04, needed only for execution mode X1 (§4.4):**

* *What is true today:* no runner profile runs a list of test commands
  without an agent; `claude-code` and `codex` run an agent, `merge` and
  `post-verdict` run fixed forge calls.
* *The requested change:* a `test-runner` profile: an agent-free step that
  checks out the head sha and runs the commands of an impact plan that
  swarm-api composed from the repository's own index (never from a caller,
  invariant 10), writing a result artifact; and a checks-posting step, or a
  mode of `post-verdict`, that reads that result and posts
  `swarmcloud/selected-tests` with the tenant's checks App
  (`swarm-tenant-<tenant>-git-checks`, sole accessor, as `-git-merge` is).
* *What it would break if accepted:* nothing existing; two new profiles.
* *If it is declined:* only X2 (the repository's own CI via
  `workflow_dispatch`) is available, so a repository without GitHub Actions
  cannot have a selected-tests gate.

**Request (D), revised 2026-10-04, extending request (A):** a
`"test_plan": RunnerInput("boolean", ...)` on `claude-code` and `codex` that
stages the impact plan for the step's own diff as `work/test-plan.json`
beside `work/repo-index.json`, so an implement or fix step runs exactly the
tests the gate will, before it finishes. If declined, the step reads the plan
through the `swarm_repo_tests` MCP tool (lane RI5) instead, which works but
costs a tool call.

The git-token registry's own request is (E), in
[git-tokens.md](git-tokens.md) §8. None of (B)-(E) is filed by this lane;
each goes to [contract-change-requests.md](contract-change-requests.md) when
the owner picks.

---

## 7. Build plan

Each lane is one territory, so no two lanes edit the same file (CLAUDE.md:
territory, not subject, splits work). Lanes in one phase run side by side;
a phase starts when the one before it has merged. The per-language LSP
adapters RI10a-RI10d need RI10's driver, so they sit in phase 4 rather than
beside it in phase 3. One ordering inside a phase is kept from RI0's plan:
RI2 follows RI1 within phase 1, because index runs are scoped to a
registration.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| RI1 | 1 | registrations: the `repositories` module, `POST/GET/PATCH/DELETE /v1/repositories`, the registration forge read, tenant scoping, unit tests | new `repositories.py` and `routes/repositories.py` in `apps/swarm-api/swarm_api/`, the router line in `main.py`, tests | — |
| RI2 | 1 | index runs and promotion: `RepoIndexSpec`, the indexer prompt, `index:run`, promotion with the sha-order rule and digest, `repo_index_runs`, the markdown renderer, `GET .../index`, `tests:select` | new `repoindex.py` in `apps/swarm-api/swarm_api/`, the index routes in `routes/repositories.py`, tests | RI1 |
| RI3 | 1 | *(revised 2026-10-04)* the mechanical extractor, now a **tree-sitter** pass: one parse per file for Python, TypeScript/TSX, JavaScript, Go and HCL; `symbols` with line ranges, routes, `import` and candidate `ast` edges, naming edges, co-change, hot-spots, the `languages` table; and its tests | `images/agent-runtime-indexer/` (moved out of `images/agent-runtime-base/` by #625), `tests/unit/worker/` | — |
| RI4 | 2 | triggers: `POST /v1/admin/repositories/poll`, ETag polling, interval check, in-flight coalescing; the per-tenant `repo_index_poll` Cloud Scheduler job (description says `managed-by=swarm-terraform`) and its OIDC grant | the poll in `repoindex.py`, `terraform/modules/scheduler/` | RI2 |
| RI5 | 2 | consumption: the planner's REPO INDEX section and `index_sha` on the run, `tests:select` slices in compiled step prompts, the `swarm_repo_tests` MCP tool | `apps/swarm-api/swarm_api/issueruns.py`, `plugin/` | RI2 |
| RI6 | 2 | the console section from the owner's pick in the mock-ups: Repositories list, Register form, repository detail, the "context used" chip on a run and in an agent's Details | `apps/swarm-ui/` | RI1, RI2 |
| RI7 | 3 | `input.repo_index` staging in the worker, after request A is accepted and applied | `apps/agent-worker/agent_worker/`, the frozen-contract edit by the owner | request A |
| RI8 | 3 | optional: the GitHub push webhook with per-registration HMAC secret | `apps/swarm-api/`, `terraform/modules/secret_manager/` | RI4, owner's go-ahead |
| RI9 | 2 | *(revised)* **graph storage**: the shard writer (content-addressed blobs, manifest, caller and callee shards), promotion recording `graph_manifest` and `graph_digest`, the blob sweep, measured sizes against §2.5's estimates | the shard writer beside RI3's tool, new `repograph.py` in `apps/swarm-api/swarm_api/` | RI2, RI3 |
| RI10 | 3 | *(revised)* the headless LSP driver: start a server over stdio, budget and per-request timeouts, memory stop, `definition` / call hierarchy / `references`, the `failing` / `timed_out` / `unsupported` fallback; the four servers installed in the indexer image (one lane, because they share its Dockerfile) | the indexer image, a new `lsp/` package beside RI3's tool | RI9 |
| RI10a | 4 | *(revised)* Python through **pyright**: its adapter, decorator routes resolved, pytest test discovery, tests on a fixture repository | `lsp/python.py` and its tests | RI10 |
| RI10b | 4 | *(revised)* TypeScript and JavaScript through **tsserver** (`typescript-language-server`): adapter, Express/Next routes, vitest/jest discovery | `lsp/typescript.py` and its tests | RI10 |
| RI10c | 4 | *(revised)* Go through **gopls**: adapter, `HandleFunc` routes, `TestXxx` discovery, no module download | `lsp/go.py` and its tests | RI10 |
| RI10d | 4 | *(revised)* HCL through **terraform-ls**: adapter, `references` in place of call hierarchy, `terraform test` discovery | `lsp/terraform.py` and its tests | RI10 |
| RI11 | 3 | *(revised)* the **impact** query: diff from the forge, changed symbols, bounded transitive callers, covering tests, `fallback_triggers`, `impact_plans`, `POST .../impact`, the graph, symbols and languages routes | new `impact.py` and the routes in `routes/repositories.py` | RI9 |
| RI12 | 4 | *(revised)* the **`swarmcloud/selected-tests`** check and merge-step integration: the X2 dispatch and its example workflow (whose last step posts `swarmcloud/selected-tests` on the input head sha through the Checks API with `checks: write`, because the dispatched run's own check runs land on the default branch commit), the check summary, the P1 scheduled full suite and its issue, the P3 fallback; a merge-step test that a required `swarmcloud/selected-tests` pinned to an App is read like any required check | `impact.py`'s check composer, `apps/agent-worker/agent_worker/merge.py` tests (after M1a merges) | RI11, the owner's policy pick |
| RI12b | 4 | *(revised)* X1: the `test-runner` and checks-posting steps | `apps/agent-worker/`, the frozen-contract edit by the owner | request C |
| RI13 | 4 | *(revised)* the **graph explorer** UI from the owner's pick (module graph, symbol call graph, test map), the impact view and the PR card's gate row | `apps/swarm-ui/` | RI11, RI6 |
| RI14 | 4 | *(revised)* the carved `graph/` prefix (worker write grant excludes it, the indexer profile's account writes it) and the per-tenant `-git-checks` secret with its sole accessor | `terraform/modules/tenancy/`, `terraform/modules/secret_manager/` | request B, before P1 or P3 is enabled |

*Revised 2026-10-04:* the **git token** registry, its permission probe and its
pages are lanes GT1-GT5 in [git-tokens.md](git-tokens.md) §9. GT1 and RI1
both add swarm-api modules but no shared file, so they may run side by side;
GT3 and RI13 both edit `apps/swarm-ui/` and are therefore one lane or
sequential, never parallel.

Every lane ships with the mutation rule CLAUDE.md states: tests pushed first
and red in CI, then the change. RI2 and RI8 are where a review belongs
(credentials, tenant isolation): RI2 because promotion decides what every
later consumer trusts, RI8 because it is the only unauthenticated route.

**Not decided here, for the owner:** which mock-up variants to build (the
page's recommendation is a recommendation); whether `allowed_profiles`
should ever gate submission (§1); whether request B is wanted; and whether
the webhook is wanted at all once polling is live. *Revised 2026-10-04,
added:* the merge policy, P1, P2 or P3 (§4.4), picked by picking a gate
screen; X1 or X2 per repository (§4.4); one indexer image or one per
language (§3.5); whether dependencies are ever installed for the language
servers (§3.5); and every git-token choice listed in
[git-tokens.md](git-tokens.md) §9.
