# A code knowledge graph for SwarmCloud's agents: GitNexus measured against our indexer

**Status: PROPOSED 2026-10-08, design only (lane GNX).** Nothing described
here is built. The owner asked on 2026-10-08 to evaluate
[GitNexus](https://github.com/abhigyanpatwari/GitNexus) and to "explore all of
the ways our agents in SwarmCloud can access and use this knowledge graph to
write better features, better code, better reviews and overall better work".
This document is that evaluation plus a design. The owner's decisions it
needs are listed in §9 and were filed with the lane as `questions.json`. It
extends [repo-index.md](../repo-index.md) and does not replace it. Every
consumer below reads the index and graph that design already stores, and
§5.1's staleness rule there ("an index older than the head it describes says
so") governs everything here. The frozen-contract half is one amendment to
repo-index.md's unfiled request (A), written out in §7.4. It is not filed by
this lane.

Every measurement below was taken on 2026-10-08 in this lane's SwarmCloud pod
(6 vCPU, 31 GiB, Linux x86-64, Node 24.21.0, Python 3.11), at this
repository's commit `da1ca30`. Every code citation is `path::symbol`. Symbols
move less than lines do, so search for the symbol.

---

## 0. The answer in one screen

* **GitNexus is real, fast-moving and capable, but we cannot ship it.** It is
  licensed **PolyForm Noncommercial 1.0.0**. Running it inside a product
  that does paid work is commercial use and needs a separate licence from
  its vendor (Akon Labs). Even setting the licence aside, on this repository
  it does **less of what we need** than the indexer we already have:
  * it parses no Terraform/HCL and no bash;
  * by default it skipped our largest source file and every UI test;
  * it resolves **211** test→application call edges where ours resolves
    **4,438**;
  * its index is **698 MB** on disk where our whole graph is **2.2 MB**
    gzipped;
  * on its first run it downloaded an unpinned native extension.
* **What it does better is the agent-facing surface, not the graph.** That
  means:
  * a live MCP server agents can query in the middle of a task;
  * module "communities" and execution "processes";
  * a risk-scored `impact` answer;
  * `detect_changes` mapping a diff to symbols;
  * keyword search over symbols;
  * staleness reported on every answer.

  Our graph already holds the facts behind most of these
  (`apps/swarm-api/swarm_api/impact.py::plan_impact` is our `impact` plus
  test selection). What we lack is **delivery**: no agent step sees the
  graph today. The planner gets only the forge's open work
  (`apps/swarm-api/swarm_api/issueruns.py::planner_prompt`), and the
  claude-code steps get only their prompt
  (`apps/agent-worker/agent_worker/runners/cliagent.py::run_cli_agent`).
* **Recommendation: option D, a hybrid that ships none of GitNexus's code.**
  * Keep our extractor as the engine.
  * Add, by clean-room reimplementation of published ideas, the three
    techniques worth having: module clustering, symbol search and
    signature-change detection.
  * Serve the graph to the planner and the reviewer through swarm-api in
    phase 1. That needs no contract change.
  * Then give every claude-code step a small, read-only, local MCP server of
    our own (`swarm-graph`). It reads a snapshot the worker stages from the
    tenant's own GCS prefix.
  * The first build is the cheapest and most valuable: the planner's graph
    section, plus a **code-enforced** rule that parallel steps never share
    a file. Today that rule exists only as a sentence in the prompt.

---

## 1. GitNexus, measured

### 1.1 What it is

GitNexus is a monorepo:
* `gitnexus/`, the npm package `gitnexus`, which is the CLI, the MCP server,
  an HTTP API and the ingestion pipeline;
* `gitnexus-web/`, a React graph explorer with an AI chat;
* `gitnexus-shared/`;
* plugins for Claude Code, Cursor and Factory.

It was read at commit `50aa4be` (2026-10-08 20:53Z). The npm package was
installed at **1.6.12**, the latest stable (2026-09-12).

* **Parsing.** Native **Tree-sitter** (`tree-sitter` 0.21.1 with grammars
  0.23.x), run in a worker pool.
  * Languages (`gitnexus-shared/src/languages.ts`): JavaScript, TypeScript,
    Python, Java, C, C++, Objective-C, C#, Go, Ruby, Rust, PHP, Kotlin,
    Swift, Dart, Vue, Zig, plus COBOL by regex.
  * Markdown becomes `Section` nodes.
  * **No HCL/Terraform, no bash, no YAML.** On our repository, `.tf`, `.sh`
    and `.yaml` files became `File` nodes with **zero** symbols (Cypher:
    0 `DEFINES` edges out of those files).
  * There is **no language server**. Calls are resolved by its own
    scope-resolution passes (import resolution, receiver typing, MRO), each
    edge with a confidence.
* **Graph.** A pipeline of 19 phases (`ARCHITECTURE.md`, "Pipeline Phase
  DAG"):
  * scan → structure → parse → routes/tools/ORM → cross-file type
    propagation → scope resolution → MRO → dependency injection →
    **communities** → **processes**.
  * Node tables include File, Folder, Function, Class, Method, Interface,
    Route, Tool, Community, Process, Section, Embedding and a dozen
    type-level kinds.
  * Relation types include `CALLS`, `IMPORTS`, `EXTENDS`, `IMPLEMENTS`,
    `METHOD_OVERRIDES`, `HAS_METHOD`, `ACCESSES`, `HANDLES_ROUTE`,
    `FETCHES`, `MEMBER_OF` (community), `STEP_IN_PROCESS` and
    `ENTRY_POINT_OF`.
  * **Communities** come from the Leiden algorithm over the call graph.
  * **Processes** are execution flows traced from ranked entry points.
  * `--pdg` adds control-flow and data-dependence edges and taint paths.
* **Storage.** **LadybugDB**, an embedded graph database (`@ladybugdb/core`
  0.21.1; the successor of KuzuDB, per its migration notes).
  * It is stored in `<repo>/.gitnexus/` or under `GITNEXUS_STORAGE_PATH`.
  * There is a global registry in `~/.gitnexus/registry.json`, and
    full-text search through a LadybugDB FTS extension.
* **Query.** Three surfaces share one backend:
  * MCP over stdio or HTTP (`gitnexus mcp [--http --auth-token]`);
  * an HTTP API (`gitnexus serve`);
  * the CLI.

  The tools and their arguments (from `gitnexus/src/mcp/tools.ts`):

  | tool | arguments (required in bold) |
  |---|---|
  | `list_repos` | limit, offset |
  | `query` | **search_query**, task_context, goal, limit, max_symbols, include_content, chain_depth, maxTokens, repo, service |
  | `context` | name, uid, file_path, file, kind, include_content, chain_depth, maxTokens, repo, service |
  | `impact` | target, target_uid, direction, mode, line, maxDepth, relationTypes, minConfidence, includeTests, limit, offset, summaryOnly, repo |
  | `detect_changes` | scope, base_ref, worktree, repo |
  | `check` | cycles, repo |
  | `rename` | symbol_name, symbol_uid, **new_name**, file_path, dry_run, repo |
  | `trace` | from, to, from_uid, to_uid, maxDepth, includeTests, pdg, crossDepth, limit, repo |
  | `cypher` | **statement**, params, repo |
  | `route_map`, `tool_map`, `shape_check`, `api_impact` | route / tool, file, method, repo |
  | `explain`, `pdg_query` | target, **mode**, variable, limit, repo (need `analyze --pdg`) |
  | `read_file`, `grep` | **path** / **pattern**, ranges, filters |
  | `group_list`, `group_sync` | name, exactOnly |

  `GITNEXUS_MCP_READ_ONLY=1` keeps only the read-only tools; it drops
  `rename`, `cypher` and the group tools
  (`gitnexus/src/mcp/read-only-policy.ts`).
* **Embeddings.** These are opt-in (`analyze --embeddings`):
  * a local Snowflake arctic-embed-xs model (384-d) on ONNX, downloaded
    from Hugging Face, or any OpenAI-compatible endpoint;
  * hybrid BM25 + vector search merged by reciprocal rank fusion.

  **Not run here.** It downloads a model and, at 58k nodes, sits above its
  own 50,000-node safety cap. Keyword (BM25) search was measured instead.
* **Incremental.** A parse cache and a durable per-file `ParsedFile` store.
  An unchanged `HEAD` is meant to exit early; measured, it did not (§1.2).
  Staleness is `current` / `behind` / `diverged` / `unknown`, against the
  indexed `lastCommit` (`core/git-staleness.ts`). That is the same idea as
  our repo-index.md §5.1.
* **Side effects.** Unless `--index-only` (or the `--skip-agents-md` and
  `--skip-skills` flags) is passed, `analyze`:
  * **edits `AGENTS.md` and `CLAUDE.md`** and installs skills under
    `.claude/skills/`;
  * writes `.gitnexus/` into the checkout.

  Run inside a step's checkout, that would land in the step's diff.

### 1.2 What it did on this repository

This repository's tracked files: 1,908 in all (880 `.py`, 345 `.tsx`, 92
`.ts`, 117 `.tf`, 55 `.hcl`, 88 `.sh`, 119 `.md`).

| run | wall | peak RSS | CPU (user) | result |
|---|---|---|---|---|
| `npm install gitnexus@1.6.12` (install scripts blocked by npm 11's default) | 38 s | — | — | `node_modules` **1.4 GB** (onnxruntime-node 548 MB, onnxruntime-web 141 MB, gitnexus 227 MB, tree-sitter grammars ≈ 250 MB) |
| `analyze --index-only`, defaults | 133 s (116 s reported) | 2.33 GiB | 274 s | 47,083 nodes, 110,413 edges, 2,041 communities, 1,290 flows |
| same, configured (`GITNEXUS_MAX_FILE_SIZE=2048`, `.gitnexusignore` `!__tests__/` `!fixtures/`) | 166 s (160 s reported) | 2.53 GiB | 372 s | 58,320 nodes, 136,547 edges, 2,621 communities, 1,273 flows; 1,828 files covered |
| re-run, nothing changed (after a commit) | 36.5 s | 1.71 GiB | 50 s | no early exit despite `lastCommit == HEAD` |
| incremental, one-line edit committed | 61.6 s | 2.06 GiB | 81 s | up to date |
| on-disk index | — | — | — | **698 MB** (`lbug` 430 MB, plus parse caches) |

The defaults lose code that matters to us:

* **`apps/agent-worker/agent_worker/lifecycle.py` (664 KB, 13,438 lines; the
  worker's core) was skipped** as "likely generated/vendored". The cap is
  512 KB.
* **301 of `apps/swarm-ui`'s files were skipped**, because `__tests__` is
  ignored by default (`src/config/ignore-service.ts`). So were every
  `fixtures/` directory and 14 Terraform module files.
* The flow tracer reported itself truncated: "3923 of 4123 candidate entry
  point(s) never ranked in … An absent flow does NOT mean the code path does
  not exist." Callable-value flow refused 1,621 sites at its 32-candidate
  cap.

Sample answers, checked against `grep` (warm MCP calls answered in 0.9 s; a
cold CLI call took 1.4 s):

| question | GitNexus | ours (`repo_index_extract.py --no-lsp`, same commit) | truth (grep) |
|---|---|---|---|
| callers of `staleness_line` | `render_markdown` | `render_markdown` | `render_markdown`, plus one test that names it without calling it |
| callers of `planner_prompt` | `planner_task`; `impact` adds `create_run` at depth 2 | `planner_task` | `planner_task`, plus **6 test calls** in 2 files (`issueruns.planner_prompt(...)`); **both tools miss them** |
| callers of `compile_plan` | `_approve` only | `_approve` plus **18 test edges in 3 files** | `_approve` plus the tests |
| test→application call edges, whole repository | **211** of 6,240 test calls | **4,438** of 28,418 test calls | — |
| `query "where is the planner prompt built for issue runs"` (BM25, no embeddings) | top hits: swarm-mcp `create_run` flows; `planner_prompt` absent | no search surface | `issueruns.py::planner_prompt` |
| `detect_changes` on a staged one-line rename | 1 file, 1 symbol (`freshness_renamed`), 0 flows, risk low | `impact.py::plan_impact` does the same from a diff | correct |

The MCP server listed **17 tools** whose schemas total **71,649 bytes**
(≈ 17.9k tokens). If the client loads every MCP schema up front, every turn
of every step pays that cost, before any answer (§7.7).

### 1.3 Licence, maintenance and security

* **Licence: PolyForm Noncommercial 1.0.0** (`LICENSE`, `package.json`).
  * Use, change and redistribution are allowed **only for a noncommercial
    purpose**. A derivative work carries the same limit.
  * The README sells commercial licences and an enterprise SaaS ("Commercial
    use of the OSS version is also available with proper licensing").
  * The vendored grammars and Leiden carry their own permissive licences.
    That does not lift the package's own licence.
  * **Consequence:** options A and B need a commercial licence. Option C
    must be a clean-room reimplementation of *ideas*: no copied code, no
    ported files.
* **Maintenance: very active and very fast-moving.**
  * 47.8k stars, 5.2k forks, ≈ 300 open issues (GitHub API, read
    2026-10-08).
  * Release candidates `v1.6.13-rc.87` to `rc.91` were cut in two days.
  * Stable releases are about weekly (1.6.10 on 2026-08-27, 1.6.11 on
    09-04, 1.6.12 on 09-12).
  * The top contributors are a co-maintainer (622 commits), dependabot (388)
    and the author (270).
  * The pace is a strength for features and a cost for us: a pinned version
    is old within a week, and the graph schema and tool shapes change
    between minors (`MIGRATION.md`).
* **Security posture.** Network behaviour was read from the code; this lane
  could not observe traffic (no network namespace in the pod):
  * **It downloaded a native binary at run time.** The first `analyze`
    fetched `libfts.lbug_extension` (2.2 MB) from `extension.ladybugdb.com`
    into `~/.lbdb/extension/0.18.1/linux_amd64/fts/`. That is outside
    `GITNEXUS_HOME`, at 21:05:22Z, during this lane's run. The vendored
    manifest records `"upstreamDigestPublished": false`. Our releases pin
    every binary by digest (`images/agent-runtime-indexer/Dockerfile`), and
    an unpinned run-time download contradicts that.
  * An **update check** calls the npm registry unless `NO_UPDATE_NOTIFIER`,
    `GITNEXUS_NO_UPDATE_NOTIFIER` or `CI` is set
    (`src/core/update-cache.ts`).
  * It depends on **`@scarf/scarf`**, install-time download analytics. Its
    install script was blocked here by npm 11's install-script policy; on an
    older npm it would run unless `SCARF_ANALYTICS=false`.
  * **No LLM key is needed** for index, query or MCP.
    * `wiki` sends code to an LLM endpoint you configure.
    * `--embeddings` with `--embedding-base-url` sends symbol text to that
      endpoint.
    * `publish` notifies a third-party registry if given a token.

    None of these is on by default.
  * Its own CI runs CodeQL, Gitleaks, Scorecard, zizmor and Trivy
    (`SECURITY.md`). Trivy there is advisory, not blocking.

### 1.4 Capabilities against ours

| | GitNexus 1.6.12 | our indexer (`images/agent-runtime-indexer/`, extractor version 2) |
|---|---|---|
| parsing | Tree-sitter (Node, native), worker pool | Tree-sitter (Python wheels 0.25.x), one parse per file (`images/agent-runtime-indexer/repo-index/repo_index_extract.py::extract`) |
| call resolution | own scope resolution + MRO, confidence per edge | AST candidates + **LSP**: pyright, tsserver, gopls (`images/agent-runtime-indexer/repo-index/lsp/driver.py`); terraform-ls disabled, 0 references in its self-test |
| languages we use | Python, TS/TSX, JS, Go; **no HCL, no bash** | Python, TS/TSX, JS, Go, **HCL**; bash and YAML as files only |
| graph | symbols, CALLS / IMPORTS / EXTENDS / IMPLEMENTS / overrides / accesses / routes, **communities**, **processes**, optional PDG/taint | symbols, call / reference / inherit / import / route_handler edges with evidence and confidence, **symbol→test map**, co-change and hot spots (90-day history, `apps/agent-worker/agent_worker/indexrun.py::HISTORY_DAYS`) |
| storage | LadybugDB, 698 MB here, one writer | content-addressed gzip JSONL shards per module per layer under the tenant's GCS prefix (`apps/swarm-api/swarm_api/repograph.py::graph_root`); **2.2 MB** gzipped for the whole graph here |
| query | MCP (17 tools), HTTP, CLI, Cypher | swarm-api routes: `impact`, `graph`, `symbols`, `test-map`, `tests:select`, `index` (`apps/swarm-api/swarm_api/routes/repositories.py::repository_impact` and its neighbours); **no agent-side surface** |
| search | BM25 FTS + optional embeddings | none |
| incremental | parse cache; 62 s for a one-line commit here | base graph + changed files (`--base-sha`, `--base-graph`); repo-index.md §3.4 |
| test mapping | `includeTests` on impact; 211 test→application edges here | `symbol_test_map` with depth and confidence; 25,763 rows here; drives `tests:select` and `plan_impact` |
| staleness | per answer, `current/behind/diverged/unknown` | per answer, `current/behind/stale/unknown/none` (`apps/swarm-api/swarm_api/repoindex.py::freshness`, `apps/swarm-api/swarm_api/repoindex.py::staleness_line`) |
| cost here | 160 s, 2.5 GiB RSS, 372 CPU-s; 1.4 GB install | 15.7 s, 294 MiB RSS without LSP; 448.6 s, 1.34 GiB with LSP, Python timing out (§2.3) |
| tenancy | single-user tool; one registry per `$HOME` | per-tenant prefix, per-tenant service account (repo-index.md §2.4) |
| licence | PolyForm Noncommercial | ours |
| maturity | broad, fast-moving, well-tested; schema churn | narrow, ours, pinned by digest, built 2026-10-05/06 |

---

## 2. Our indexing today, first-hand

### 2.1 What exists

* **Registration and index runs.**
  * `POST/GET/PATCH/DELETE /v1/repositories` and `POST .../index:run` are
    in `apps/swarm-api/swarm_api/routes/repositories.py`.
  * An index run is a task on the `indexer` profile
    (`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`, contract
    request 48). That is claude-code on `agent-runtime-indexer`, resource
    class `standard` (4 CPU, 8 GiB), timeout 7,200 s.
  * swarm-api overrides the timeout to 1,800 s for a full run and 900 s for
    an incremental one (`apps/swarm-api/swarm_api/repoindex.py::indexer_task`).
* **The deterministic passes run in the worker**, not the agent
  (repo-index.md §3.6). `apps/agent-worker/agent_worker/indexrun.py::budgets`
  splits a 1,800 s run into:
  * extract: 0.4 of the timeout (720 s);
  * LSP: the extract budget minus 180 s (540 s);
  * graph write: 0.15 of the timeout (270 s);
  * base read: 0.25 of the extract budget.

  The agent then writes the judgement fields: purposes, territory, notes
  and `test_layout`.
* **The graph** is written by `swarm-repo-graph`
  (`images/agent-runtime-indexer/repo-index/repo_graph_shards.py`):
  * content-addressed blobs and a manifest at
    `tenants/<tenant>/repos/<repo_id>/graph/<commit_sha>/manifest.json`;
  * one shard per module per layer (symbols, callers, callees, tests,
    files);
  * swarm-api reads it in `apps/swarm-api/swarm_api/repograph.py`.
* **The impact engine**, `apps/swarm-api/swarm_api/impact.py::plan_impact`:
  * input: a diff → changed symbols → bounded transitive callers → covering
    tests, with `fallback_triggers`;
  * served at `POST /v1/repositories/{id}/impact`;
  * also `module_graph` and `neighbourhood` for the explorer.
* **The console** draws:
  * the graph (`apps/swarm-ui/src/RepoGraph.tsx`);
  * a Context card;
  * a selected-tests row on an issue run (`apps/swarm-ui/src/RunIndex.tsx`).

### 2.2 What is measured to be wrong or missing

1. **Nothing an agent runs sees the graph.**
   * `planner_prompt` puts the issue file reference and the forge's open
     work in the prompt, and nothing from the index (repo-index.md §4.1 is
     lane RI5, unbuilt). The console says so: "the run carries no
     `index_sha`" (`apps/swarm-ui/src/RunIndex.tsx`, header comment).
   * `run_cli_agent` assembles prompt, issue line, children line, expected
     outputs and the publish paragraph, with no index or graph.
   * No MCP server is configured for any step:
     `apps/agent-worker/agent_worker/runners/claude_code.py::write_headless_settings`
     writes no `mcpServers`.
   * The `repo_index` runner input (request A) is unfiled, so
     `apps/common/swarm_common/profiles.py::_CLI_AGENT_INPUTS` still takes
     only `issue`.
2. **The "never two parallel steps on one file" rule is a sentence, not a
   check.**
   * `planner_prompt` tells the planner that "Steps that edit the SAME file
     must be in one dependency line".
   * But `apps/swarm-api/swarm_api/issueruns.py::PlanSpec._dependencies`
     checks only order, cycles and width.
   * `apps/swarm-api/swarm_api/issueruns.py::PlanStep` documents `files` as
     "A plan, not a fence".
   * A plan that runs two steps on one file in parallel is approved, and
     the join's `git apply --3way` is where it fails
     (`apps/swarm-api/swarm_api/issueruns.py::_compile_staged`).
   * The territory locks that would catch this between *lanes* are designed
     but unbuilt: [lane-queue.md](../lane-queue.md) §4, status PROPOSED.
3. **Index runs were slow for reasons that were not parsing.**
   * repo-index.md §2.5 and §3.6 record a full run of about 20-24 minutes on
     2026-10-06 (1,685 files, 25,541 symbols, 48,717 edges).
   * The graph write was serial at ≈ 3.9 s a blob, ≈ 24 minutes for ≈ 370
     blobs. It was killed by the agent CLI's 10-minute command limit after
     150 blobs.
   * The retry failed with **HTTP 412** because the listing kept a
     versioned bucket's `#<generation>` suffix.
   * Both are fixed:
     * batched `gcloud storage cp --no-clobber` in rounds of 500
       (`images/agent-runtime-indexer/repo-index/repo_graph_shards.py::BATCH_FILES`);
     * listing by object name.
   * The extractor's own ≈ 6 minutes were "most of it Pyright reaching its
     300 s server budget" (repo-index.md §3.6).
   * The tree-sitter pass alone took **15.7 s** here (§1.2). **The LSP pass
     is the cost, not the parse.** Measured here with the pinned servers and
     production's 540 s LSP budget: see §2.3.
4. **Some edges are judgement, not facts.**
   * The agent's `test_layout` produces `declared` test edges at confidence
     0.2.
   * The `path-ref` evidence must be served as `declared`.
   * Purposes, territory and notes are "the agent's reading"
     (repo-index.md §2.1, §2.5).
   * A merge gate that reads those edges reads an LLM's guess.
5. **Hot spots were meaningless on a one-commit clone.** Every file showed
   `changes: 1`. This is fixed by deepening history to 90 days
   (`apps/agent-worker/agent_worker/indexrun.py::clone_history_days`) and
   reporting `window_covered`.
6. **Both engines miss calls made through a module object.** The six
   `issueruns.planner_prompt(...)` calls in the tests resolve to nothing in
   either tool (§1.2). Whichever engine we keep needs that fixed before
   `planner_prompt`'s tests can be selected by symbol.

### 2.3 Our extractor with its language servers, here

The run used:
* `repo_index_extract.py` with `pyright` 1.1.414 and
  `typescript-language-server` 6.0.1 from
  `images/agent-runtime-indexer/repo-index/lsp/package.json`;
* `--lsp-total-budget-seconds 540`, production's LSP share of a 1,800 s
  full run.

| | without LSP | with LSP (540 s budget) |
|---|---|---|
| wall | **15.7 s** | **448.6 s** |
| peak RSS | 294 MiB | 1,342 MiB |
| symbols | 30,475 (21,217 Python, 7,938 TS/TSX, **1,167 Terraform/HCL**) | 30,475 |
| call/reference edges | 59,698 | 61,087, of which **15,101 carry LSP evidence** |
| languages' LSP status | — | TypeScript `ok`, JavaScript `ok`, **Python `timed_out`** ("over the 270-second budget for pyright") |
| test→application call edges | 4,438 | 4,438 |
| graph document | 32 MB JSON, 2.2 MB gzip | 41 MB JSON |

So **96% of the extractor's time is the language servers, and Python, our
main language, still gets no LSP edges** inside the budget. That matches the
2026-10-06 production reading (pyright at its budget). Faster index runs come
from budgeting LSP per changed module on incremental runs, not from a
different parser. GitNexus would not change this: it has no LSP pass, and it
took 10 times our tree-sitter time.

---

## 3. The options

Costs are engineering lanes (§6) plus run-time cost. Benefits are stated
against the actors in §4.

### A. Adopt GitNexus as the indexer engine inside the `indexer` profile

* **Cost:**
  * a commercial licence;
  * an image carrying Node and 1.4 GB of modules, which grows the indexer
    image by several times its 916 MB build;
  * 2.5 GiB RSS, which needs at least the `standard` class's 8 GiB;
  * a converter from LadybugDB to our shards, or a rewrite of
    `repograph.py` and `impact.py` against Cypher;
  * a re-pin every week to stay supported.
* **Benefit:**
  * communities, processes and search;
  * a maintained parser for 17 languages.
* **Risk:**
  * we lose HCL and LSP-grade resolution;
  * test edges drop 20-fold (211 against 4,438);
  * the defaults silently skip our largest file and our UI tests;
  * a run-time native download contradicts digest pinning;
  * schema churn breaks the merge gate's inputs.

**Not recommended.**

### B. Run GitNexus's MCP server in every claude-code and review step

* **Cost:**
  * the same licence;
  * Node and 1.4 GB in **`agent-runtime-base`**, the image every profile
    but two runs. Its toolchain growth of 188 MB was already refused once
    by the release scan (`docs/worker-images.md`);
  * a 166 s index, or a 698 MB snapshot to stage, per step;
  * ≈ 17.9k tokens of tool schemas per turn unless deferred.
* **Benefit:** agents get `context`, `impact`, `detect_changes`, `query`
  and `trace` live, mid-task, on the step's own working tree. That
  *liveness* is the real prize.
* **Risk:**
  * every step pays index time or staging size;
  * a per-step `.gitnexus/` and `AGENTS.md` edit lands in the diff unless
    flags are right;
  * tenant isolation is ours to enforce around a tool that assumes one user.

**Not recommended as GitNexus. The *shape* is adopted in D.**

### C. Borrow the techniques into our own indexer

* **What:** clean-room implementations, from the published idea and not
  from GitNexus's code:
  * **module clustering**: Louvain/Leiden over the call+import graph, with
    an MIT/BSD implementation or about 200 lines of our own;
  * **symbol search**: BM25 over names, qualified names, docstrings and
    paths;
  * **signature fingerprints**: a hash of parameters and return type per
    symbol, so a diff knows when callers must change;
  * **execution flows from entry points**: routes and CLI mains, bounded.

  Drop the LLM-judged edges from anything a gate reads.
* **Cost:**
  * extractor work: one lane, in `images/agent-runtime-indexer/` only;
  * a few seconds per run (the parse is 15.7 s; clustering 60k edges is
    sub-second in NumPy-free Python at this size, an estimate from the
    algorithm's near-linear cost).
* **Benefit:** better facts.
* **Risk:** low. But it changes *nothing an agent sees* until delivery is
  built.

### D. Hybrid (recommended)

**C's techniques in our engine, plus GitNexus's delivery shape, built by
us:**

1. **Phase 1, no contract change.**
   * swarm-api composes a **graph section** into the planner's prompt and
     the review step's prompt.
   * Plan validation **enforces file-disjoint parallel steps**.
2. **Phase 2.** A small **read-only MCP server of our own, `swarm-graph`**:
   * Python, stdio, about six tools, under 2k tokens of schema;
   * it ships inside the worker package that is already in
     `agent-runtime-base`;
   * it reads a **snapshot** the worker stages from the tenant's own
     prefix;
   * it is enabled by the platform when a step's input asks for the
     repository's index by name (request A, amended, §7.4).
3. The orchestrator, the observer and the console get the same queries
   through swarm-api routes.

* **Cost:**
  * seven lanes (§6);
  * no new third-party runtime;
  * ≈ 7 MB of tree-sitter wheels if the overlay re-parse (§5.2) moves into
    the base image.
* **Benefit:** every actor in §4.
* **Risk:** §8.

### E. Don't adopt anything

* **Cost:** nothing now.
* **Benefit:** none.
* **Risk:** the measured problems in §2.2 stay:
  * the planner splits steps blind to the code's shape;
  * same-file parallel steps are caught only at integration;
  * implementers and reviewers rediscover the call graph with `grep` on
    every task.

### Recommendation

**D**, for four reasons.

1. Our graph is already the better graph for *this* code:
   * HCL coverage;
   * LSP-resolved Python and TypeScript;
   * a 20-fold denser test map;
   * a 300-fold smaller store.
2. GitNexus's licence rules out shipping it.
3. The gain the owner is after comes from *agents using* a graph, and the
   part GitNexus gets right is the agent surface. We can copy that shape
   with roughly six tools and a staged snapshot.
4. Phase 1 needs no frozen-contract change and goes straight at today's top
   conflict cause.

---

## 4. Every way agents could use the graph

Each row gives the query, where it hooks in (`path::symbol`) and the
expected gain, with its basis. "Measured" means measured in this lane. Every
other gain is an estimate and says what it rests on.

### 4.1 The issue-run planner

Hook: `apps/swarm-api/swarm_api/issueruns.py::planner_prompt`, called by
`apps/swarm-api/swarm_api/issueruns.py::planner_task` from
`apps/swarm-api/swarm_api/routes/runs.py::create_run`. The graph section
joins repo-index.md §4.1's `REPO INDEX` section inside the same
`MAX_PLANNER_PROMPT_BYTES` (64 KiB), between delimiters, as data.

| use | query | gain |
|---|---|---|
| **where the issue lands** | symbol search (C's BM25) over the issue's title, body and named paths and identifiers → top 20 symbols with file, module and community | The planner today clones and greps. Estimate: one fewer exploration round of 5-15 tool calls per plan, based on the turn counts behind CLAUDE.md's "8.9 billion cached tokens" lane measurement. **Measure:** planner turns and tokens before and after. |
| **impact of each candidate** | `impact(symbol, upstream, depth 2)`: callers, modules and tests; the fan-in count is the risk | Steps name their `tests` from the map instead of guessing. **Measure:** fraction of a plan's `tests` that exist and cover the changed symbols. |
| **overlap with open PRs** | for every open PR in the snapshot `planner_prompt` already reads (`apps/swarm-api/swarm_api/issueruns.py::_open_work_section`): changed files → changed symbols → upstream depth 1; intersect with the candidates | The overlap list is evidence, not the planner's guess. Today `PlanOverlap` is filled from titles and file lists. |
| **step splitting by territory** | the communities that contain the candidates; propose one step per community, and put steps that share a file or a seam in one dependency line | This is the #1 conflict cause. §4.9's validator makes it a guarantee for *declared* files. The graph extends it to *undeclared* ones: the call sites a signature change forces. |
| **seams** | file fan-in plus co-change rank; replaces lane-queue.md §4.1's "seeded from the index's `hot_spots`" | `hot_spots` counts edits, not dependants. `main.py`, `App.tsx` and `schemas.py` are seams because everything *registers* there, which fan-in sees and edit counts may not. |

### 4.2 Implementers (claude-code steps)

Hook:
* phase 1: the compiled step prompt,
  `apps/swarm-api/swarm_api/issueruns.py::_step_prompt` and
  `apps/swarm-api/swarm_api/issueruns.py::_step_detail`, which already
  injects the step's files and tests;
* phase 2: the `swarm-graph` MCP server, configured by
  `apps/agent-worker/agent_worker/runners/claude_code.py::write_headless_settings`
  and named in one prompt line by
  `apps/agent-worker/agent_worker/runners/cliagent.py::run_cli_agent`.

| use | `swarm-graph` tool | gain |
|---|---|---|
| callers and callees before changing a function | `context(symbol)` | A direct-caller question is a wash against grep: grep found all 31 `compile_plan` lines in 3.5 KB. **Graph answers win at depth ≥ 2 and on test coverage**, where grep needs one round per level. |
| blast radius | `impact(symbol, depth ≤ 3)` with tests and modules | Fewer missed call sites. **Measure:** review findings of the form "call site not updated". |
| conventions of neighbouring code | `neighbours(symbol)`: same file and same community, same kind, with their docstrings, decorators and test files | The agent copies the local idiom (CLAUDE.md: "match the surrounding code"). |
| which tests to run before pushing | `tests_for(files or symbols)`, the same engine as `tests:select` | Runs the right tests first, not "the area". Basis: 25% of lane PRs were red on first CI run (#642), and 2 of 6 SwarmCloud fix PRs were red on failures a unit run would have caught (#248, #249). |
| "am I about to collide?" | `territory(files)` → other in-flight steps and lanes holding them (phase 3, from §4.8) | The step learns of a conflict before it writes, not at integration. |

### 4.3 Reviewers

Hook: the `review` step that
`apps/swarm-api/swarm_api/issueruns.py::compile_plan` adds after the
implement steps. Its prompt gains an **impact block** computed by swarm-api
from the step's diff through
`apps/swarm-api/swarm_api/impact.py::plan_impact`, plus `swarm-graph` in
phase 2.

| use | query | gain |
|---|---|---|
| did the change update every call site | changed symbols whose **signature fingerprint** changed (C) → upstream callers **not in the diff** | Turns "check call sites" from advice into a list. |
| what tests cover the changed symbols, and were any added | `plan_impact`'s covering tests, minus the diff's test files | The review asks for the missing one by name. |
| risky hot spots | changed symbols with high fan-in, a seam file, or high co-change | Review effort goes where breakage spreads. |
| requirement ↔ code | each requirement (`PlanSpec.requirements`) against the symbols the diff touched | Supports the verdict the review already writes. |

### 4.4 Fixers

Hook: the CI-fix round, `apps/swarm-api/swarm_api/issueci.py::ci_fix_workflow`,
fed by `apps/swarm-api/swarm_api/issueci.py::excerpt_at`; and the plan's
`fix` step from `compile_plan`.

* Query: failing test ids, parsed from the excerpt → the **reverse** of the
  `symbol_test_map` → the symbols under test, their files, and which of them
  this PR changed.
* Gain: the fixer starts at the code the test exercises, not at the test.
* **Measure:** fix rounds per run and time to green.

### 4.5 The merge step

Hook: test selection is already designed and partly built:
* `apps/swarm-api/swarm_api/impact.py::plan_impact`;
* the `swarmcloud/selected-tests` check, lane RI12, which waits on the
  owner's P1/P2/P3 policy (repo-index.md §4.4);
* the merge workflow, `apps/swarm-api/swarm_api/issueci.py::merge_workflow`.

The graph's contribution here is **better inputs, not a new consumer**:
* a denser test map, once module-object calls resolve (§2.2 item 6);
* signature fingerprints, so a signature change widens the walk;
* the rule that **a gate never reads a `declared` (LLM-judged) edge**.

Run affected tests first and fail fast; the full suite stays whatever the
owner's policy says.

### 4.6 The observer

The observer is the role that writes the "observer P*" findings cited
across the worker (`apps/agent-worker/agent_worker/runners/cliagent.py`
comments; [agent-output.md](../agent-output.md)).

* Query: over all in-flight lanes in one repository, territory (declared
  files plus graph expansion) pairwise intersected; collisions reported with
  the shared files and symbols.
* Hook: the lane queue's `lane_territories` document (lane-queue.md §4.2),
  and a read route (§6, KG3).
* Gain: collisions named while both lanes run, not after the second PR goes
  red.

### 4.7 The console

Hook: `apps/swarm-ui/src/RepoGraph.tsx` and
`apps/swarm-ui/src/RepoGraphData.ts`, which already render the module graph
and neighbourhood from `apps/swarm-api/swarm_api/impact.py::module_graph`
and `apps/swarm-api/swarm_api/impact.py::neighbourhood`.

* Add a **community layer** (colour by community; collapse a community to a
  node).
* Add an **issue overlay**: the planner's candidates and the plan's
  territory per step.
* Add a **lane overlay**: in-flight lanes' territories, with collisions in
  red.

Gain: the owner sees where a plan will work before approving it.

### 4.8 The orchestrator

Hook: before a batch of parallel lanes is dispatched, in
`apps/swarm-mcp/swarm_mcp/server.py::_dispatch_batch`, through a new
swarm-mcp tool `swarm_territory`. RI5 already planned a sibling,
`swarm_repo_tests`.

* Query: each brief's named files, expanded to their seams and to the
  callers a signature change would force, then pairwise intersected.
* Gain: "two issues that edit the same file are one lane" (CLAUDE.md,
  Issues) checked by a query, not by reading.

### 4.9 Plan validation (phase 1, no graph needed)

Hook: `apps/swarm-api/swarm_api/issueruns.py::PlanSpec._dependencies`.

The rule: two steps that are **not** on one dependency line and whose
`files` intersect, by exact path or by directory prefix as lane-queue.md
§4.1 defines it, make the plan **invalid**.

This is pure, offline and graph-free. It is the cheapest change in this
document and goes straight at the #1 conflict cause. The graph later widens
`files` with forced call sites and seams. Whether to refuse such a plan or
auto-chain the steps is owner question Q4.

---

## 5. Delivery mechanics

### 5.1 How the graph reaches a step

| path | for | built on |
|---|---|---|
| **composed into the prompt by swarm-api** | planner, review, fix: prompts swarm-api already composes | the stored graph via `repograph.py`; phase 1 |
| **served by swarm-api routes** | console, orchestrator (swarm-mcp), observer | `routes/repositories.py`; tenant-scoped reads exist |
| **a per-step snapshot plus a local MCP server** | every claude-code step that asks for the index by name | worker staging from the tenant's prefix (request A, amended); `swarm-graph` in the worker package |

The snapshot is the manifest plus the shards the graph already stores: 2.2
MB gzipped for this repository's whole graph. It is staged **beside** the
checkout, never in it (like `work/repo-index.json` in repo-index.md §4.2),
so it is in no diff.

`swarm-graph` reads it lazily, shard by shard, so memory stays near the
size of the shards touched.

It talks only stdio. It makes no network call and holds no credential, so
nothing an agent asks it can reach another tenant's data or anything
beyond this one snapshot.

### 5.2 Freshness against the step's base commit

repo-index.md §5.1 holds here unchanged: **a stale index says so, every
time**.

1. The worker stages the newest promoted index whose `commit_sha` is an
   **ancestor of the step's base commit**. A non-ancestor is never staged.
2. It records `index_sha`, `base_sha`, `behind_by` and the paths changed in
   `index_sha..base_sha` (`git diff --name-only`, local and free).
3. Every `swarm-graph` answer begins with that freshness block. Any answer
   that touches a path changed since `index_sha`, **or dirtied by the agent
   in this step** (`git status`), marks those rows `stale: true` with the
   reason.
4. Phase 4 option: an **overlay**. Re-parse the changed files with the same
   tree-sitter extractor (about 10 ms a file; 15.7 s for 1,909 files here)
   and replace their symbols and outgoing edges in memory.
   * Incoming edges from unchanged files stay valid unless a symbol was
     renamed or removed. Those are reported as `possibly_broken_callers`,
     which is exactly the reviewer's question.
   * The overlay needs the ≈ 7 MB tree-sitter wheels in the base image.
5. Prompt-composed sections (phase 1) carry
   `apps/swarm-api/swarm_api/repoindex.py::staleness_line` first, as §4.1 of
   repo-index.md already requires.

### 5.3 Tenant isolation (invariant 9)

* Graphs live only under `tenants/<tenant>/repos/<repo_id>/graph/`
  (`apps/swarm-api/swarm_api/repograph.py::manifest_key`).
* The worker reads them with **the task's own tenant service account**,
  resolving the registration from the task's own tenant and
  `repository_url` (repo-index.md §4.2).
* No graph is shared, cached across tenants, or merged.
* GitNexus's "group" and cross-repo features are exactly what we must not
  have, and `swarm-graph` has no repository argument at all.

### 5.4 Callers choose by name (invariant 10)

* The tool arrives in an image and a profile, by name: `swarm-graph` is a
  module of the worker package in `agent-runtime-base`, started by the
  worker with a fixed argv.
* A caller's only lever is request A's boolean (`"repo_index": true`). It
  names no image, command, server, bucket, object or sha.
* The planner, review and fix steps get their sections because swarm-api
  composes their prompts.

---

## 6. Build plan as lanes

Rules:
* No two lanes in one phase share a file.
* A phase starts when the one before it has merged.
* Every lane ships tests first, red in CI, then the change (CLAUDE.md).
* Territory is files, not subjects.

| lane | phase | builds | territory (files edited) | needs | success measure |
|---|---|---|---|---|---|
| **KG1** | 1 | §4.9 validator; RI5's `REPO INDEX` section **plus** the graph section (candidates, impact, overlaps, communities); `index_sha` on the run | `apps/swarm-api/swarm_api/issueruns.py`, new `apps/swarm-api/swarm_api/plancontext.py`, new `tests/unit/control_plane/test_plan_territory.py`, new `tests/unit/control_plane/test_plan_context.py` | — (absorbs RI5's issueruns half; RI5's `plugin/` half stays RI5's) | join conflicts per staged run → 0 for declared files; plan `files` recall (files the merged PR changed that the plan named), baseline then +; planner tokens |
| **KG2** | 1 | extractor version 3: communities, BM25 term index, signature fingerprints, module-object call resolution (§2.2 item 6), entry-point flows; new shard layers | `images/agent-runtime-indexer/repo-index/repo_index_extract.py`, `images/agent-runtime-indexer/repo-index/repo_graph_shards.py`, new `tests/unit/worker/test_repo_index_communities.py` | — | extract time within +10% of version 2; test→application edges ≥ 4,438 and covering the 6 `planner_prompt` tests; community stability across incremental runs (Jaccard ≥ 0.9) |
| **KG3** | 2 | read routes: `POST .../search`, `GET .../communities`, `POST .../territory` (files → expanded territory, seams, overlap with open PRs and in-flight lanes) | new `apps/swarm-api/swarm_api/territory.py`, `apps/swarm-api/swarm_api/routes/repositories.py`, new tests | KG2 | p95 latency; ETag hit rate |
| **KG4** | 2 | `swarm-graph` MCP server, read-only, stdio, ≤ 6 tools (`search`, `context`, `impact`, `tests_for`, `neighbours`, `territory`), freshness block on every answer, 4 KiB answer cap with paging | new `apps/agent-worker/agent_worker/graphmcp/` package and new tests | KG2 | schema ≤ 2k tokens; answer p95 < 300 ms on this repo's snapshot |
| **KG5** | 3 | worker staging of the snapshot (request A amended, §7.4), `--mcp-config` for claude-code, one prompt line | new `apps/agent-worker/agent_worker/graphstage.py`, `apps/agent-worker/agent_worker/runners/claude_code.py`, `apps/agent-worker/agent_worker/runners/cliagent.py`; the frozen-contract edit **by the owner** | KG4, request A' accepted | tokens per task; first-push CI red rate (baseline 25%, #642); tool calls per step |
| **KG6** | 3 | review impact block and fixer test→symbol context | `apps/swarm-api/swarm_api/issueci.py`, new `apps/swarm-api/swarm_api/reviewcontext.py`, `issueruns.py` (review step prompt only; KG1 has merged by then) | KG1, KG3 | review findings that name a missed call site or test; fix rounds per run |
| **KG7** | 3 | orchestrator `swarm_territory` tool and the dispatch-time warning | `apps/swarm-mcp/swarm_mcp/server.py`, `apps/swarm-mcp/swarm_mcp/client.py`, new tests | KG3 | parallel lanes dispatched with overlapping territory, unannounced → 0 |
| **KG8** | 4 | console community layer, issue and lane overlays | `apps/swarm-ui/src/RepoGraph.tsx`, `apps/swarm-ui/src/RepoGraphData.ts`, `apps/swarm-ui/src/api.ts` | KG3; never beside another `apps/swarm-ui` lane on `api.ts` | owner's pick from a mock-up first, as RI13 did |

Notes on the plan:
* KG5 and KG6 share phase 3 with KG7, with disjoint files.
* KG1 and RI5 cannot both run, because both edit `issueruns.py`. KG1
  *is* RI5's swarm-api half.
* The overlay (§5.2 item 4) is phase 4 and optional. It adds the
  tree-sitter wheels to `images/agent-runtime-base/Dockerfile`, which makes
  it its own lane.

**Baselines to take before KG1 merges** (a measurement, not a build): the
last 30 days of
* staged issue runs' join conflicts;
* plan `files` recall against merged PRs;
* first-push CI red rate;
* review verdicts;
* cached input tokens per step.

Without them, none of the success measures above can come out either way.

---

## 7. Risks

### 7.1 Licence

* GitNexus is noncommercial-only. Under D we run none of its code and
  copy none of it. The ideas (Leiden, BM25, entry-point flows,
  confidence-scored edges, per-answer staleness) are published and
  older than it.
* A lane implementing them must not open GitNexus's sources while it
  writes ours.
* Any library we take for clustering must be MIT, BSD or Apache. `leidenalg`
  and `python-igraph` are GPL and are refused.

### 7.2 Supply chain

* New dependencies come in pinned by hash (`--require-hashes`, as
  `images/agent-runtime-indexer/repo-index/requirements.txt` does).
* Images are pinned by digest.
* Every image is scanned by the release's Trivy gate, which refuses HIGH and
  CRITICAL (`scripts/push-images.sh`).
* A run-time download of any binary, as GitNexus's FTS extension did, is a
  refusal.

### 7.3 Image size and disk

* D adds no runtime to the indexer image. The overlay would add ≈ 7 MB to
  `agent-runtime-base`, against the 188 MB toolchain growth the release scan
  already refused once.
* A staged snapshot is a few MB on the step's disk (`standard`: 4 GiB). For
  comparison, a GitNexus index is 698 MB and its install 1.4 GB.

### 7.4 Contract change request (not filed)

This amends repo-index.md §6.3's unfiled request (A). It does not add a
second input.

* *What is true today:*
  `apps/common/swarm_common/profiles.py::_CLI_AGENT_INPUTS` declares only
  `issue`, and swarm-api refuses an undeclared input.
* *The requested change:* request A's `repo_index` boolean, with its
  `means` widened to
  "stage the tenant's current index **and graph snapshot** of the task's own
  repository beside the checkout, and enable the read-only `swarm-graph`
  tool, named in the prompt".
* *What it would break if accepted:* nothing existing. The input is
  optional and false by default.
* *If it is declined:* phases 1 and 2 still deliver the planner, review,
  fix, orchestrator and console uses. Hand-written workflow steps never see
  the graph.

No new profile is needed. Request B's agent-free indexer
(repo-index.md §6.3) would remove the LLM from the index run entirely,
which is the cleanest form of "drop the LLM-judged parts". It stays the
owner's call (Q5).

### 7.5 Index cost

* KG2's additions are graph algorithms over about 60k edges: seconds, not
  minutes.
* The cost centre stays the LSP pass (§2.3) and, while index runs are
  agent steps, the agent.

### 7.6 Staleness

The risks are an answer from an old graph presented as current, and a
reviewer trusting a "no callers" answer.

Mitigations:
* §5.2's freshness block on every answer;
* ancestor-only staging;
* rows on changed or dirty paths marked stale;
* GitNexus's own rule, worth keeping: **zero resolved callers is
  `UNKNOWN`, never "safe"**, because a call through a module object (§2.2
  item 6) or a dynamic dispatch is invisible.

### 7.7 Prompt budgets

* Planner: the graph section shares repo-index.md's 24 KiB `REPO INDEX`
  allowance inside the 64 KiB prompt (`MAX_PLANNER_PROMPT_BYTES`). Owner
  question Q6.
* Review block: 8 KiB.
* `swarm-graph`: ≤ 2k tokens of schema (GitNexus's is ≈ 17.9k), and answers
  capped at 4 KiB with paging.

### 7.8 Judgement in gates

Edges with `declared` evidence and the agent's prose (purposes, territory
notes) are shown to agents as hints and **never** read by the merge gate,
the plan validator or the territory check.

---

## 8. What this lane did not do

* **Embeddings and GitNexus's semantic search were not measured.** They
  need a model download, and this repository is above GitNexus's 50k-node
  cap.
* **GitNexus's Docker images, web UI, `wiki` and `--pdg` were not run.**
* **Network traffic was not observed**, only read from code, plus the one
  download found on disk (§1.3).
* No agent was given either graph. **Every agent-side gain in §4 is an
  estimate** until KG1 and KG5 report against the baselines in §6.
* Our extractor ran outside its image, from this repository's pinned
  requirements and LSP lockfile. gopls was not installed, because this
  repository has no Go.

---

## 9. Owner decisions

These are filed as `questions.json` with this lane:

* **Q1:** which option (A-E). Recommended: D.
* **Q2:** whether to seek a commercial GitNexus licence anyway.
  Recommended: no.
* **Q3:** which phase first. Recommended: KG1 plus KG2.
* **Q4:** refuse or auto-chain a plan whose parallel steps share a file.
  Recommended: refuse with the reason, so the planner re-plans.
* **Q5:** whether index runs drop the agent (request B's worker-only
  shape).
* **Q6:** the planner's graph budget.
* **Q7:** whether to file request A, amended, now or after phase 1's
  numbers.
