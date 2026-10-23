# Context quality: graph-ranked repository context for every agent step, with Jev as an advisory ranker and judge

**Status: PROPOSED 2026-10-14 (lane CTXQ-DESIGN). Nothing in §4-§7 is built.**
The owner approves it, or picks the options in §8, before any build lane starts.
§1-§3 describe what exists and what was measured, and those parts are facts
with their sources.

Owner request, 2026-10-14: *"architect, design and implement a solution to
improve overall task output quality for coding implementations or code
reviews, test coverage and overall architecture, design and especially
planning tasks resulting from our automation runs like github issue sweeps.
Use the repository indexing we've done so far, AST, LSP graphs, call graphs,
overall code structure AND integrate JEV alongside claude to further improve
agent context, regardless of the task."*

Owner decision on data, 2026-10-14:
* Jev may see code **only when a workspace opts in**: Settings › Workspace ›
  Code intelligence. It is off by default and can be overridden per
  repository.
* SwarmCloud's own repository is on.
* The local index (AST, LSP, call graph) is always available, because it
  never leaves the platform.

**The short version.**
* Most of the machinery already exists: the index, the graph, the planner and
  review context, and an MCP server. The one gap that matters is that the
  **implementer step, which writes most of the code, sees none of it.**
* The measurement in §2 and §3 shows where the misses are. Of 69 classified
  failures in the last 150 lane PRs and recent issue runs, **40 (58%) were
  context failures**, and only 7 were reasoning failures:
  * a guard test or house rule the agent never met (14);
  * a test covering the changed code that it never ran (8);
  * a parallel lane it could not see (9);
  * a caller or callee contract it never read (5);
  * a doc that restates the fact it changed (2);
  * a path it could not push (2).
* So the design wires the existing graph into every step type. It adds two
  layers the graph lacks: **history** (what changed together before) and
  **rules** (which guard and which doc polices a path). It also adds a
  **live in-flight view**. Jev ranks and judges where a workspace opts in,
  and only as an advisory signal.

---

## 1. Inventory: what exists today

Read from `main` at `33d2c87` (2026-10-14). Code is cited by symbol rather
than by line, so a citation survives the code moving
(`tests/unit/scripts/test_docs_cite_worker_symbols.py` holds other docs to
that rule).

### 1.1 The index and the graph (built)

**The indexer.**

| Thing | What it is |
|---|---|
| Runner profile | `indexer` (`apps/common/swarm_common/profiles.py`): claude-code on image `agent-runtime-indexer`, GKE, priority −50 |
| Who submits it | swarm-api (`swarm_api.repoindex`, `INDEXER_PROFILE`): 1800 s for a full run, 900 s for an incremental one |
| Deterministic phases | `stage_base` → `extract` → `agent` → `graph_write` (`agent_worker.indexrun`) |
| What the agent writes | only the judgement fields: module purposes, territory, notes, `test_layout` |
| What gates may read | never the agent's judged edges (knowledge-graph.md §9 Q5) |

**What it parses.**
* **Tree-sitter AST** for Python, TypeScript, TSX, JavaScript, Go and HCL
  (`images/agent-runtime-indexer/repo-index/repo_index_extract.py`,
  `EXTRACTOR_VERSION = "3"`).
* **An LSP pass** for definitions and call hierarchy: pyright,
  typescript-language-server and gopls (`repo-index/lsp/driver.py`).
* There is no SCIP, no ctags and no embeddings. Search is BM25 over a `terms`
  shard.

**Two limits that matter here.** terraform-ls is disabled (`lsp/servers.py`
`DISABLED_SERVERS`), so HCL has tree-sitter edges only. And on 2026-10-08
the Python LSP pass `timed_out` inside its 540 s budget (knowledge-graph.md
§2.3), so Python call edges are AST-resolved in practice.

**What it stores.**
* **Under the tenant's own GCS prefix** (invariant 9):
  * `repo-index.json` (schema `swarm.repo-index/v1`, at most 512 KiB). It
    holds modules, entry points, routes, `test_layout`, `test_map`,
    `always_tests`, territory, commands, hot spots, notes, languages, a graph
    summary, `test_coverage` and `truncated`.
  * Content-addressed graph shards (`swarm.repo-graph-manifest/v1`,
    `repo_graph_shards.py`):

    | Shard group | Layers |
    |---|---|
    | per module | `symbols`, `callers`, `callees`, `tests` (symbol→test map), `files` |
    | index shards | `communities` (Louvain), `terms` (BM25), `signatures`, `signature_changes`, `flows` |

  * Each edge carries a `kind`: call, reference, inherit, route_handler or
    import.
  * Each edge also carries `evidence` (lsp, ast, import, naming, co-change,
    path-ref, declared) and a `confidence`.
* **In Firestore:** `repositories/{repo_id}.index` (current, head and pending
  sha, `behind_by`, coverage), the last 20 `index_versions`, and
  `repo_index_runs`.

**Freshness.**
* Cloud Scheduler `repo_index_poll` runs every 5 minutes. The poll is
  ETag-guarded, re-indexes at most every 30 minutes on change, and at least
  every 24 hours.
* Incremental runs are built (IX2). Each re-asks the LSP only for affected
  files, and falls back to a full run past 300 changed files or a non-ancestor
  base.
* Every read carries `index_sha`, `head_sha`, `behind_by` and `stale`. An
  index is stale when it has been behind the head for longer than its
  interval and a 12-hour grace (`behind_since`), when it was built over 7 days
  ago, or when it is not an ancestor (`swarm_api.repoindex.freshness`); never
  by a commit count, which a merged branch inflates (#1693).
* The push webhook (RI8) is not built.

**Where people see it.** Work › Repositories (`Repositories.tsx`) and a
repository's detail tabs: Overview, Graph, Impact, Test map, Hot-spots, Index
runs, Settings, Used by (`RepositoriesDetail.tsx`). The routes are in
`swarm_api/routes/repositories.py`: `/graph`, `/symbols`, `/impact`,
`/test-map`, `/tests:select`, `/search`, `/communities`, `/territory`.

**Stale headers.** docs/repo-index.md and docs/design/knowledge-graph.md still
open with "PROPOSED … Nothing described here is built". Both bodies record
lanes RI1-RI4, RI6, RI9-RI11, RI13, IX1-IX3 and KG1-KG4, KG6 and KG7 as built.
Fixing those headers is outside this lane's territory and is reported in the
PR rather than done.

### 1.2 How agents reach the graph today

| Step | What it gets from the index | Where |
|---|---|---|
| **Planner** (issue runs, sweeps) | `REPO INDEX` and `REPO GRAPH` prompt sections: candidates, 2-level impact, overlaps with open work, communities. They share a 24 KiB allowance and appear only with an extractor-v3 index | `swarm_api.plancontext`, called from `swarm_api.routes.runs.start_run`; `issueruns.planner_prompt` |
| **Plan validation** | Parallel steps that list the same file, or a directory prefix of it, are refused, and the planner re-plans | `issueruns.territory_refusal` |
| **Run hold** | An auto run whose files overlap a live run is held | `routes.runs.territory_conflict` |
| **Reviewer** | A `REVIEW IMPACT` block (`territory.expand`, 1 hop) | `reviewcontext.read_review_context`, compiled by `issueruns.compile_plan` |
| **CI fixer** | A `TESTED CODE` block for the red tests | `reviewcontext.read_fix_context`, in `issueci.ci_fix_continuation` |
| **Orchestrator** (operator's MCP) | `swarm_territory`, and a warn-only territory check on `swarm_dispatch` and `swarm_workflow` units that declare `files`. It expands files to callers and tests through `POST /v1/repositories/{id}/territory` at depth 1 (`TERRITORY_DEPTH_DEFAULT = 1`, max 2), and is silent below format 3 | `swarm_mcp.server._territory_check` → `swarm_api.territory.expand` |
| **Implementer** (every lane and issue-run step) | **Nothing.** It gets the checkout, whatever `CLAUDE.md` the CLI loads itself, and its brief | — |

**The implementer gap, precisely:**
* **`swarm-graph`, the MCP server, is built (KG4) and started by nothing.**
  `agent_worker/graphmcp/` serves six read-only tools from a local snapshot:
  `search`, `context`, `impact` (depth ≤ 3), `tests_for`, `neighbours` and
  `territory`. Every answer begins with a freshness block, is capped at
  4 KiB with paging, and "zero resolved callers is UNKNOWN, never safe".
* Its own docstring says that nothing starts it. Staging the snapshot and
  naming the server is lane KG5, which waits on contract request A′
  (knowledge-graph.md §7.4). The owner chose to file A′ only after phase 1's
  numbers, and §2 is those numbers.
* `claude_code.write_mcp_config` registers `playwright` (live browser only),
  `swarm-git` and `swarm-search`, and nothing else. `codex.py` registers
  `swarm-git` only.

### 1.3 Where a step's context is assembled (the plug points)

| Plug point | Symbol | What it does today |
|---|---|---|
| Prompt chain for every CLI agent | `agent_worker.runners.cliagent.run_cli_agent` | Appends the issue-file line (`issue.prompt_line`), the child-agents line (`children.prompt_line`), deliverables, the step's time-limit line (`time_limit_line`, #2139) and `PUBLISH_PARAGRAPH`. There is no system prompt: stdin is "the one instruction channel every CLI runner shares" |
| claude-code body | `agent_worker.runners.claude_code.body` | Adds resume, egress and browser notes; passes `--mcp-config` |
| Workspace files before the agent starts | `children.prepare` pattern | Writes `work/.swarm-children/README.md` and `work/issue.md` **outside the checkout**, so the worker's publish never commits them |
| Pre-publish gate (SPEED-D1, merged 2026-10-14) | `agent_worker.prepublish.select_checks`, `Gate.run` | Rebases onto the base (one conflict turn). Runs `scripts/changed-guards.sh --base`, checkov, tflint, shellcheck, ruff and the build-mirror pytest. Gives one fix turn (`FIX_TURNS = 1`). Publishes anyway, with `prepublish-checks.md` appended to the PR body (`Worker._prepublish_section`) |
| Review and fix (SPEED-D2) | `issueruns.compile_plan`, `agent_worker.verdict` | Review verdict `MERGE` / `NOT_YET` with findings `{summary, file, line, side}`. The fix step is gated `verdict_in: [NOT_YET]`. `COMPILED_REVIEW_ROUNDS = 1`. Lanes default to `LANE_FIX_ROUNDS_DEFAULT = 2` CI fix rounds |
| Requirements | `issueruns` module docstring, `issueci.evaluate_requirements` | The reviewer answers `requirements: [{index, met, note}]`; only a complete, all-met list lets the PR say `Closes #N` |

**What `changed-guards.sh` selects:**
* every `tests/unit/**/test_*.py` whose **text names a changed path or
  basename**;
* the fixed guard set;
* contract parity.

So it is a grep, not the graph's test map. **It runs no vitest and no `tsc`**,
and `select_checks` has no `terraform fmt`. §3 shows that matters.

### 1.4 The planner and the issue sweep

* **Entry points:** `swarm_run_issue` (MCP), `sc run --issue`, and
  `POST /v1/runs` → `start_run`. They read the issue and the plan context, then
  submit a `claude-code` planner agent (`issueruns.planner_task`).
* **The plan** (`PlanSpec`, extra keys refused) carries `summary`,
  `steps[1..8]`, `mode`, `requirements`, `overlaps` and `estimate`. Each step
  has `{step_id, title, prompt, files, tests, estimate, depends_on}`.
  `plan_digest` is the sha256 of the canonical JSON.
* **States:** PLANNING → PLANNED → APPROVED → RUNNING → CHECKING ⇄ FIXING →
  DONE. Approve and edit carry the digest; a mismatch is 409 `plan_changed`.
* **The sweep:** `issue-sweep` schedule type (`schedtypes/issue_sweep.py`,
  candidates from `issuesweep.collect_candidates`, oldest-updated first).
  * It calls the same `start_run` with `created_by=schedule:<id>`.
  * Defaults: plan approval **auto**, merge approval required,
    `max_live_runs` 8.
  * `sch_6ecbe1307511` fires at `7,37 * * * *` (from #1133's body).
* **Since 2026-10-12** (#1150) the sweep passes over issues a live workflow
  or a fresh merge is working on: a partial fix for §3's duplicate class.

### 1.5 Jev today

* **The API**, read from the jev-browser 0.2.0 client (`src/jev.mjs`), which is
  the only Jev client the platform ships:
  * `POST https://api.typesafe.ai/v1/systemone` with body
    `{state, model: "jev-latest", questions}`.
  * Each question is `{type: "noul" | "choice" | "score", instructions,
    criteria?}`.
  * Answers are `{noul}` (a probability of yes), or
    `{choice, probabilities, confidence}`, plus `usage.input_tokens`.
* **Its limits:** Jev never generates text; a choice takes at most 255
  options; a request is capped at 32,768 input tokens (`session.mjs`
  `MAX_STATE_CHARS = 70_000`).
* **Measured in jev-browser's RESULTS.md (2026-09-17):** about 290 ms per call
  and about 2,850 input tokens per call. A ranking call over page blocks takes
  700 ms to 1.5 s. Ranking is done the way §4.3 proposes: one call carries one
  yes/no question per block, and code owns the budget.
* **What exists on main:**
  * Key storage only: `swarm_api.provider_keys`, Settings › API keys, and
    `/sc key`. Resolution is the submitter's own key in their confirmed
    personal tenant, else the agent tenant's key (`resolve_provider_key`).
  * docs/provider-keys.md says "Nothing uses these keys yet".
  * The `agent-runtime-browser` image carries a patched jev-browser (JEV-IMAGE,
    #1296).
* **Not on main yet:** the first consumer, JEV-RUNNER (#1297, open). Its
  worker-side key path is the one §5 reuses:
  * `agent_worker/jevkey.py` resolves the key in the worker process.
  * The key is written to `<attempt>/private/` with mode 0600, never into the
    agent's environment.
  * Only the launcher process reads it.
* **The "jev-browser assessment"** the brief points to is docs/design/live-browser.md
  §3.2.2. It exists on #1297's branch, not on main.
* **There is no Code intelligence setting anywhere yet.** Settings has only
  API keys. A repository's Settings tab has no context toggle.

---

## 2. Baseline quality, measured

**Window.** The last 150 *merged* SwarmCloud lane PRs (head `swarm/task_*`),
#1104-#1296, created 2026-10-11 12:41Z to 2026-10-14 12:46Z. That is three
days of production lane output. Read 2026-10-14 from:
* the public GitHub API (PRs, Actions runs, issues);
* the PR heads fetched as `refs/pull/*/head`.

§9 has the commands. **Not measured:** plan edits, rejections and approvals
of issue runs. swarm-api answered 401 (IAP) to this container's
`swarm_runs` / `swarm_schedules` reads. §2.6 defines the metric so the lane
CTXQ-M script reads it with an operator's credentials.

### 2.1 The headline

| Metric | Value | Definition (re-measurable, §9) |
|---|---|---|
| **Red CI runs per PR** | **0.45** (67 red / 150) | Completed `pull_request` runs of application, terraform and security with `conclusion == failure` on the PR's head branch, cancelled and skipped excluded |
| **PRs with at least one red run** | **37 / 150 = 24.7%** | The same runs, per PR |
| **First-pass application CI** | **122 / 150 = 81.3%** green | The PR's earliest completed `application` run is `success` |
| **Review NOT_YET** | **8 / 150 = 5.3%** | PR body line `Review verdict: **NOT_YET**` |
| **Red after a MERGE verdict** | **33 / 37** red PRs had a MERGE review | The review ran before CI and passed what CI then failed |
| **Hand fix-up** | **20 / 150 = 13.3%** (25 commits) | The PR head carries a non-merge commit authored by the operator's own identity (`bogdan-alexandrescu`), not the worker's (`bogdan`). Commits brought in from main (`Bogdan Alexandrescu`) are excluded |
| **Needed anything after the agent** | **45 / 150 = 30%** | Red CI, NOT_YET or a hand commit (union) |
| **Source PRs that touched no test** | **0 / 147** | Every lane PR that changed `apps/`, `scripts/`, `images/`, `terraform/`, `kubernetes/` or `plugin/` also changed a test file |

**Earlier figures differ by definition, not by drift:**
* The 0.52 red runs/PR measured earlier counted cancelled runs. A newer push
  supersedes them, so they say nothing about the change.
* The earlier 23% hand fix-up counted operator-side commits of any kind,
  including owner-answer doc commits. §3 classifies 3 of the 20 here as
  owner input rather than defects.
* #642's 25% first-push red rate, which KG5's success measure uses, matches
  24.7% here.

### 2.2 CI: which jobs went red

| Workflow | Red runs |
|---|---|
| application | 65 |
| terraform | 2 |
| security | 0 |

Of the 37 red PRs, the first `application` run was the red one on 28. The
other 9 went red after a later push: a fix round, a merge from main, or a hand
commit.

Within application, the red jobs named in the fix commits and `## Continuation`
sections are in two groups. (Job-level API reads were not spent; the job
names come from those texts.)
* Python unit shards, which the pre-publish gate now runs in part, through
  `changed-guards.sh`.
* The swarm-ui component, typecheck and build jobs. These are vitest and
  `tsc`, two families the gate does not run at all.

### 2.3 Review: what NOT_YET found

All 8 NOT_YET verdicts, findings quoted from the PR bodies:

| PR | Finding, abridged | Class (§3) |
|---|---|---|
| #1262 | The patch carried a different lane's whole change (runner git tools, about 2,000 lines) under this lane's title | parallel lane |
| #1181 | `_audited` classifies rows in Firestore stream order, which depends on random IDs, though its sibling `audit_entries` already sorts by `(at, id)`. And no test can fail on it: the fake streams in insertion order | missed sibling idiom + test adequacy |
| #1180 | The wiring that puts "parked since" in the banner has no test; deleting the line leaves every test green | test adequacy |
| #1148 | Added a group to `GROUPS`, but `accept.yml`, which the lane cannot push, still lists five, so a unit test is red | territory constraint |
| #1141 | The guard is red on 30 leftovers in other lanes' files, and touched vitest files are red | guard not run + missed tests |
| #1132 | The socket is opened before noVNC attaches, so the first frame is dropped | reasoning (race) |
| #1107 | Fixed the finding on the detail page, but the list card the finding was about still says "1,000 edges" | missed caller surface |
| #1105 | Half fix (a 200-agent page), and the fixture never puts an agent off the page | reasoning + test adequacy |

**The reviewer is cheap insurance with low recall.** 33 of the 37 PRs that
went red had passed review. A reviewer that cannot see which tests cover the
change cannot be expected to know which will fail.

### 2.4 Escaped defects

* **Bugs citing a PR in the window:** #1151 and #1152. CLAUDE.md and
  docs/ci.md still said "5 acceptance groups" after #1148 made it 6. A doc
  restating a changed fact was missed.
* **Latent defects in earlier merges that turned PRs red in this window:**
  * the repo-index fixture's pinned `built_at` aged past `STALE_AGE`
    (`tests/unit/control_plane/repo_index_fakes.py`). It turned 4 PRs red on
    2026-10-12 (#1162, #1167, #1169, #1170);
  * a time-of-day test (`qa.g5.accounts.test.tsx`) red between 00:00 and
    03:00. It turned 4 PRs red (#1126, #1129, #1138, #1142);
  * each was fixed **independently, four times**, by four fixers who could
    not see the others' identical fix.
* **Wave epic #1194** (2026-10-12) holds 18 out-of-territory findings. One
  matters to this design directly: "every claude-code attempt ignores the
  repository's 23 `permissions.allow` entries because the CLI never trusted
  the workspace". It was fixed by #1196 (WK-TRUST). Until 2026-10-12 the
  agents' own project settings were not even loaded.

### 2.5 Issue runs, as GitHub sees them

* **11 issue-run PRs** (body marker `swarmcloud-issue-run:`), #970-#1155,
  2026-10-09 to 2026-10-12:
  * 7 merged;
  * 1 open;
  * **3 closed unmerged, every one a duplicate of a lane already in flight**:
    * **#1136** — the sweep started it at 22:12Z; a lane for the same issue
      started at 22:20Z and edited the same 3 files.
    * **#1094** — built its own PERM-1 with a different response shape from
      the one already merged as #1090.
    * **#1080** — superseded by the MERGE-GAPS lane on `merge.py`.
* **Plan quality as GitHub shows it:** #1155 kept the run inside the issue and
  named the sibling defect (#1152) as left out on purpose, which is the
  behaviour wanted. Plan edits and rejections are in swarm-api (§2.6).

### 2.6 Metric definitions for the script (lane CTXQ-M)

Each is computed per PR or per run, over a window, by a script that reads only
GitHub and swarm-api. None needs the agent's transcript.

| ID | Metric | Source |
|---|---|---|
| M1 | red completed runs per PR (cancelled and skipped excluded) | Actions runs by head branch |
| M2 | PRs with at least one red run | M1 |
| M3 | first-pass: the earliest completed `application` run is green | M1 |
| M4 | review NOT_YET rate, and findings per NOT_YET | PR body verdict block |
| M5 | red after MERGE: M2 ∩ verdict MERGE | M2, M4 |
| M6 | hand fix-up: a non-merge, non-worker-authored commit on the head | `refs/pull/N/head`, author identity |
| M7 | escaped defects: issues labelled `bug`, or wave-epic comments, citing a merged PR of the window within 14 days | issues and epic comments |
| M8 | issue-run outcomes: merged, closed as duplicate, closed otherwise, open | `swarmcloud-issue-run:` marker plus closing comment |
| M9 | plan approvals, edits (`plan:edit` count) and rejections per run; NOT_READY rate | `GET /v1/runs` (needs an operator credential) |
| M10 | fix rounds per PR (continuations), and how many repeat an earlier fix in another PR | PR body `## Continuation` sections |
| M11 | changed-symbol test reach: the share of changed symbols with at least one covering-test edge in the graph | graph `tests` layer at the PR's base |
| M12 | lane cost and wall time | the ledger (`GET /v1/...` cost report) |

**Coverage deltas are not measurable today.** No CI job runs coverage (no
`--cov`, c8 or istanbul in the workflows), and every source PR already touches
a test (0/147 without one). Test **adequacy** is the gap: 3 of the 8 NOT_YET
verdicts were tests that could not fail. M11 measures reach from the graph
instead of lines. Line coverage is option §8 Q7.

---

## 3. Failure taxonomy

**Sample.** 69 root causes, one row per root cause per PR:
* all 37 red-CI PRs, with each red run's cause read from the fix commit that
  followed it and the PR's `## Continuation` section (47 rows);
* all 11 findings of the 8 NOT_YET reviews;
* the 4 hand-fixed PRs that never went red, plus #1217's apply-time fix (5);
* the 3 duplicate issue runs;
* the 3 escaped bugs.

### 3.1 By class

| Class | Context? | n | % | Examples |
|---|---|---|---|---|
| **G: guard or house rule not met** | context | 14 | 20% | Refusal-switch guard (`test_new_refusals_ship_off.py`) on #1215, #1221, #1255, #1261 and #1273. Import-time guard (`test_collection_stays_cheap.py`) on #1148 and #1271. `NotImplementedError` marker on #1149 and #1273. Typescale or letter-spacing CSS guard on #1113 and #1164 (×2). `terraform fmt` on #1217. Real-zero guard on #1141 |
| **P: parallel lane or main moved** | context | 9 | 13% | UX1 replaced a select under #1167 and #1169. LB-CURSOR under #1198. A removed route under #1142. A test from #1264 under #1266. Another lane's change carried in #1262. 3 duplicate issue runs |
| **T: covering test not run** | context | 8 | 12% | The build-report test (#1248). The LiveTicket fixture (#1146). The nav-heading test (#1114). The RT-31 clamp test (#1105). The phone-layout budget (#1162). The node tests outside `__tests__` (#1141 ×2). The guard self-test (#1217) |
| **X: caller or callee contract** | context | 5 | 7% | `refusals.refuse` raises when its switch is on (#1273). UI calls to routes swarm-api does not serve (#1132). A case-only name collision with `SignOut.tsx` (#1293). The sibling's sort idiom (#1181). The other surface of the same finding (#1107) |
| **D: doc restates the changed fact** | context | 2 | 3% | #1151, #1152 |
| **W: path the lane cannot push** | context | 2 | 3% | `release.yml` (#1179), `accept.yml` (#1148) |
| **TA: test that cannot fail** | partly | 3 | 4% | #1180, #1181, #1105 |
| **L: latent defect from an earlier merge** | no | 8 | 12% | 2 root defects (§2.4) × 4 PRs each |
| **R: reasoning or logic** | no | 7 | 10% | hashchange ordering (#1242), per-mutation `getComputedStyle` (#1258), socket race (#1132), half fix (#1105), unchecked index type (#1140, #1141), sweep firing bug (#1133) |
| **E: environment the container cannot reproduce** | no | 4 | 6% | CI-runner identity (#1207, 10 red runs), Playwright metadata in the image build (#1296), Chromium path (#1149), GCP's 300-character role description (#1217) |
| **U: unexplained** (green on rerun, no change) | no | 3 | 4% | #1145, #1229, #1232 |
| **O: owner input, not a defect** | no | 3 | 4% | design answers recorded (#1226, #1263), adds-only parity (#1216) |
| **H: hygiene** | no | 1 | 1% | stray sed backup (#1244) |
| **Total** | | **69** | | **context 40 (58%)**, test adequacy 3, other 26 |

### 3.2 What the taxonomy decides

1. **The implementer needs the context, not only the reviewer.**
   * Every G, T, X and W row, and 6 of the 9 P rows, was made by the
     implementer. The other 3 P rows are the planner's duplicate runs.
   * Review flagged 4 of the 37 red PRs. The other 33 passed review, and CI
     caught them.
   * Context delivered only to the reviewer (KG6, built) leaves the 33 to CI.
2. **Context alone would not have prevented two of the classes.**
   * **G is already partly caught by SPEED-D1.** Its `changed-guards.sh` now
     runs the Python guards before publishing. It misses the vitest CSS
     guards, `terraform fmt`, and any guard whose file does not name the
     changed path. The refusal guard is one example: it discovers codes by
     scan, not by name.
   * So G needs a **rule index** (§4.2 layer R): which guard polices which
     kind of change, learned from which tests changed together with which
     paths. It also needs gate coverage for vitest and fmt (§4.6).
3. **T is a test-selection problem the graph already solves.**
   * Its `tests` layer covers vitest (`_is_test_file` matches
     `.test.tsx` and `__tests__`). Its route_handler edges join `api.ts`
     calls to routes.
   * Feeding `tests_for` of the changed files into the gate and into
     CONTEXT.md covers all 8 T rows.
   * One caveat: #1141's node tests under `apps/swarm-ui/tests/` would need to
     be classified as test files. That is a gap to check in lane CTXQ-H.
4. **P is the costliest per instance: a whole lane thrown away.**
   * Neither the graph nor the checkout can see it. It needs a live
     **in-flight view**: open PRs and running lanes with their declared and
     expanded territory.
   * It must be shown at the start of the step, re-checked before publishing,
     and given to the planner.
5. **L is the most instructive non-context class.** Four fixers fixed the
   same time bomb four times. A `similar_changes` retrieval over recent fix
   commits, given to the CI fixer, turns the second through fourth into a
   cherry-pick. A test-writing checklist item ("no wall-clock or date
   literals in fixtures") stops the next one being written.
6. **Reasoning failures are 10%.** A better model or more review rounds is not
   where the remaining quality is. Context is.

---

## 4. Design: a context pipeline per agent type

### 4.1 Shape

```
intake ──► retrieve ──► rank (local, then Jev if opted in) ──► deliver ──► agent ──► gates
  │           │                 │                                 │                     │
  task type   graph snapshot    budgeted pack                     CONTEXT.md            pre-publish:
  seeds       history layer     per-kind quotas                   swarm-graph MCP       graph-selected tests
  brief       rules layer                                         prompt line           self-check
              in-flight view                                                            advisory signals
```

**Principles, each taken from §3:**
* **Deterministic first.** The graph, history and rules produce the
  candidates. Jev only reorders and judges them. With Jev off or down, the
  pack is the local ranking, and nothing waits on it.
* **One pack format for every agent type.** Only the seeds, the quotas and the
  checklist differ.
* **Files, not prompt growth.** CONTEXT.md lives outside the checkout, beside
  `.swarm-children/`, so the publish never commits it. The prompt gains one
  line. The agent reads the file once, and the MCP tools page anything further
  on demand. That respects the cost lesson in CLAUDE.md's "Lanes": cost is
  turns × context.
* **Stale says so.** The pack carries the index sha, `behind_by` and `stale`.
  It marks rows on paths changed since the index as stale, as `graphmcp`
  already does.
* **Advisory signals never decide a merge.** Jev's outputs and the self-check
  appear in the PR body and in metrics. Auto-merge, `requirements_finding` and
  the review verdict never read them.

### 4.2 Retrieval sources

| Layer | Built? | Content | Serves classes |
|---|---|---|---|
| **S: symbols and edges** | built (KG2) | definitions; callers and callees N hops; route_handler; communities; BM25 | X, T |
| **T: test map** | built (RI) | symbol→test and file→test with evidence and confidence; `always_tests` | T, TA |
| **H: history** (new) | — | For each file: the last N merged PRs touching it (number, title, files, whether a fix round ran). Co-change pairs (file A ↔ test or guard B, support, confidence). The fix-commit subjects of continuations | G, L, D |
| **R: rules** (new) | — | For each path glob: the CLAUDE.md and docs sections that name it, and the guard tests that police it. Mined from (a) path mentions in docs and guard docstrings, and (b) H's co-change with `tests/unit/scripts/test_*guard*` and the fixed guard set in `changed-guards.sh`. Also the paths a lane cannot push (`.github/workflows/`, `apps/common/swarm_common/`) | G, W, D |
| **I: in-flight** (new, live) | partial (KG3 overlap) | Open PRs and running lanes and steps with declared `files`, expanded 1 hop, and who owns them | P |
| **B: brief** | — | Paths and symbols named in the brief, issue or plan step (`files`, `tests`), plus the issue's `requirements` | seeds |

**Layers H and R are indexer output, not swarm-api state.** They live in the
same content-addressed shards, under new layer names, built in `graph_write`.
* H needs the clone's history, which the indexer already has. It is plain git
  plus the PR numbers in squash subjects.
* R reuses `terms` for docs.
* **Firestore and the frozen contract are untouched.**

**Layer I is the one live query.** It cannot be staged, because it changes
by the minute. A new attempt-proved route serves it,
`POST /v1/attempts/{id}/context/inflight`, authenticated and fenced exactly
as `POST /v1/attempts/{id}/git/{tool}`:
* `ChildService.authenticate_worker` and `verify_proof`;
* the lease fence (invariant 5);
* the signed spec read for the repository.

It answers only the agent's own tenant and repository (invariant 9).

### 4.3 Ranking under a token budget

**Candidates.**
* Each is `{kind, ref, why, score, evidence, stale}`.
* `kind` is one of: symbol, caller, callee, test, guard, rule, doc, similar_pr,
  in_flight, cannot_push.

**Local score.** A weighted sum, with weights fixed in code and tuned only by
the replay (§6):

```
score = w_d · 1/(1+hops)            graph distance from the seeds
      + w_c · edge confidence        lsp > ast > import > naming
      + w_h · co-change support      layer H
      + w_t · BM25(brief, candidate) layer S terms
      + w_r · recency                last touched, half-life 14 days
```

**Quotas per kind,** so one kind cannot crowd out the rest:
* at least 3 tests and 2 guards when they exist;
* at most 5 docs and 5 similar PRs;
* **every in-flight overlap and every cannot-push row, always**, outside the
  budget, because P and W are the expensive misses.

**Budget.** CONTEXT.md is capped at 16 KiB, about 4k tokens (§8 Q2). Rows
past the budget are listed by count with the MCP tool that pages them, never
silently cut. That follows graphmcp's "never a silent truncation".

**Jev rerank** (opt-in, §5):
1. The top 60 local candidates go to Jev in one or two requests, one `noul`
   question each: *"Would an engineer doing `task` need to read this to get
   it right?"*
2. The final order is `0.5·local_rank_score + 0.5·p_yes`.
3. Candidates Jev scores below 0.15 drop below the budget line. They are
   kept in the MCP paging, not deleted, which matches jev-browser's
   bias-to-keep.

### 4.4 Delivery

**Files the worker writes**, all outside the checkout:
* `work/.swarm-context/CONTEXT.md` — the human-readable pack, ranked, with
  freshness at the top;
* `work/.swarm-context/pack.json` — the same pack, machine-readable, also
  uploaded as the artifact `context-pack.json` (§6 reads it);
* `work/.swarm-context/snapshot/` — the KG5 snapshot `graphmcp` reads.

**Prompt line.** One line appended in `run_cli_agent`, beside
`children.prompt_line`:
> Repository context for this step is in `$SWARM_CONTEXT/CONTEXT.md`: callers,
> covering tests, guards, rules, similar past changes and parallel work. Read
> it before editing. The `swarm-graph` tools answer anything it pages out.

**MCP.** `claude_code.write_mcp_config` adds `swarm-graph`
(`python -m agent_worker.graphmcp --snapshot … --workdir …`). Codex takes the
same server through `-c mcp_servers.swarm-graph.*`, as `codex.py` registers
`swarm-git`.

**The tools the brief names, against the KG4 tools.** No aliases, because the
schema budget is under 2k tokens:

| Asked for | Served by |
|---|---|
| `callers_of` | `impact` (depth 1, or more) |
| `symbol_def` | `context` |
| `tests_for` | `tests_for` |
| `similar_changes` | **new** `similar_changes(files \| symbols)`, from layer H, offline |
| — | **new** `rules_for(files)`, from layer R, offline |
| — | **new** `in_flight(files)`. The only remote tool: it calls the attempt-proved route through the worker's proof key, as `swarm-git` does. With no network or no proof, it answers `UNKNOWN`, never "nothing in flight" |

### 4.5 Pipelines per agent type

**How the agent type is known.** By the step's role, not by guessing:
* an issue run's compiled steps are planner, implementer, review and fix;
* a `swarm_workflow` step is an implementer unless it gates on `verdict_in`
  (fixer) or its prompt is compiled from the review template;
* an index run is excluded.

An ad-hoc agent with no role is an implementer. Jev may classify it when the
workspace opts in (§5.2 call J0).

| Type | Seeds | Retrieval emphasis | Jev (opt-in) | Checklist in CONTEXT.md | Plugs into |
|---|---|---|---|---|---|
| **Implement** | plan step `files` and `tests`, or brief paths; BM25 over the brief when none | callers 2 hops, callees 1, `tests_for`, guards (R), rules (R), similar PRs (H), in-flight (I), cannot-push | J1 rerank | run every listed test; obey every listed guard; update every doc row naming a changed fact; never edit a cannot-push path; stop and say so on an in-flight overlap | worker staging, then `run_cli_agent` and `write_mcp_config` |
| **Review** | the diff's changed symbols | callers of changed symbols **outside the diff** (missed call sites), tests covering changed symbols outside the diff, `neighbours` (sibling idiom), guards, the requirements | J3 finding triage, J4 test gap | one row per requirement; name any caller not updated; say for each new test whether it fails without the change | `issueruns.compile_plan` review prompt (extends KG6's `REVIEW IMPACT`); the reviewer is a claude-code step and also gets the MCP |
| **Fix** (review or CI) | the red tests, or the finding's file | the red tests' covered symbols (KG6 `TESTED CODE`), plus **H: earlier fix commits for the same test or fixture** | — | quote the earlier fix if one exists | `issueci.ci_fix_continuation(context=)`, the fix step's prompt |
| **Test-writing** | target symbols | `tests_for`, `test_layout`, fixtures used by sibling tests, the test-side rules (import-time guard, no date literals, fake Firestore ordering) | J4 test gap | each test must fail without the change, and say how that was checked | as implement, with this checklist |
| **Architecture / design** | topic terms from the brief | BM25 and communities, `docs/design/*` sections by term, owner-decision sections, `docs/contract-change-requests.md`, frozen-contract types, seams between communities | J1 rerank of doc sections | owner decisions as options with a recommendation; cite code by symbol; name frozen-contract requests | as implement; the pack is doc-heavy by quota |
| **Plan / issue sweep** | the issue body and `requirements` | KG1's sections (built) **plus** I (open PRs and lanes on the expanded territory), H (closed issues and PRs on the same files), R | J2 plan coverage, J5 duplicate check | every requirement is mapped to a step; every overlap says `none` or `required` with a reason | `plancontext` (prompt), then a post-plan judgement the run serves on approval |

**The planner's in-flight check is the one structural change.** The sweep
has plan approval **auto**, so a duplicate plan runs unseen. That is exactly
what produced #1094 and #1136.
* With layer I in the planner's prompt, the planner can answer NOT_READY
  (`kind: duplicate`, naming the lane).
* With J5 (opt-in), a duplicate scoring ≥ 0.85 holds an auto-approved plan
  for a human (§8 Q5). It does not reject it. Holding is a reversible delay;
  rejecting would be Jev deciding.

### 4.6 Quality gates after the agent

All of these run in `prepublish.Gate`. They are additions to `select_checks`
and keep its contract: one fix turn, and publish anyway with a PR section.

1. **Graph-selected tests (blocking with a fix turn, like today's checks).**
   * `tests_for` over the diff's changed files, taken from the staged
     snapshot.
   * Python tests not already selected by `changed-guards.sh` run with
     `pytest -n auto`.
   * **vitest files run with `npx vitest run <files>`**, and `tsc --noEmit`
     runs when `apps/swarm-ui` changed.
   * `terraform fmt -check` runs on changed `.tf` files.
   * This closes T and the vitest and fmt part of G. #1140, #1141, #1113,
     #1164 and #1217 would each have met their failure in the container.
2. **In-flight re-check (advisory).**
   * Layer I is read again before the rebase.
   * A new overlap since the start adds a line to the PR body and a
     `territory_overlap` field to `context-pack.json`.
   * It does not block, because the rebase step already handles textual
     conflicts.
3. **Self-check (advisory).** The prompt asks the agent to write
   `self-check.md`, one row per checklist item with done or not done and
   evidence. The gate only checks that it exists and appends it to the PR
   body. A missing file is a line in the PR body, not a failure.
4. **Test reach (advisory, M11).** The changed symbols that have no covering
   test edge after the change, listed in the PR body.
5. **Jev verdicts (advisory, opt-in).** J4's test-gap answers and J3's
   finding triage go in a PR section, "Advisory signals (Jev, not a gate)".
   No merge path reads them.

**Invariants.** Nothing here touches admission, leasing or capacity
(invariants 1-3). Staging and the gate run inside the attempt and checkpoint
like any other phase (invariant 8). The in-flight route is fenced
(invariant 5) and tenant-scoped (invariant 9). Callers still pick
`runner_profile` by name and pass nothing new (invariant 10), because staging
is decided by a stored setting (§4.7), not by the caller.

### 4.7 Opt-in and staging without a frozen-contract change

KG5 was waiting on request A′, a `repo_index` runner input in
`swarm_common.profiles`. **This design does not need it.**
* Whether a step is staged is a **stored setting**: a repository's
  `context: inherit | on | off` override over the workspace's Code
  intelligence default. It is not a caller's choice.
* The worker reads the setting at staging, the way it already reads the
  repository registration for the clone.
* An index-less or stale-beyond-ancestry repository stages nothing and says
  so in one prompt line.

**Two separate switches:**

| Setting | Default | Controls |
|---|---|---|
| `context` (local pack and MCP) | **on** for every registered repository with a format-3 index | Nothing leaves the platform |
| `code_intelligence.jev` (sending code to Jev) | **off** for every workspace; **on** for SwarmCloud's own repository, per the owner's decision | Whether Jev ever sees code |

**Where the workspace setting lives (decided, owner 2026-10-14).** In the
workspace's ONE settings document, `workspace_settings/{tenant_id}`, as the
field `code_intelligence` -- beside JUDGE-SELECT's `judgment_backend` and
TRAIN-WS's `merge`, one field per feature. There is no `tenant_settings`
collection, and nothing goes on `swarm_common.models.Tenant`, which is
frozen. The field is a map:

| Key | Values | Unset means | Read by |
|---|---|---|---|
| `jev` | `true` / `false` | `false`: Jev sees no code | `agent_worker.judge.settings` |
| `context` | `on` / `off` | the platform default, `on` | `agent_worker.graphstage.decide` |
| `context_ab` | `ab` / `on` / `off` | the platform default, `ab` | `agent_worker.graphstage.decide` |

A write changes that one field and nothing else, so it never touches the
other features' fields, and keeps keys of the map it does not own. The
per-repository override goes on the registration (`RepositoryPatch`,
field `context`: `on`, `off`, or `inherit`, which clears it to null), as
`merge_policy` already does.

Only the workspace's owner (a personal workspace's principal) or a platform
admin changes either, exactly as for `judgment_backend`: a member is 403, a
caller outside the workspace 404. Every change is one `admin_audit` row
written in the same Firestore transaction as the change. CTXQ-SET built it:
`GET`/`PUT /v1/workspaces/{tenant_id}/code-intelligence`
(`swarm_api/codeintel.py`) and `PATCH /v1/repositories/{repo_id}`.

**Not yet read from there by staging.** CTXQ-S, built before this decision,
reads `tenant_settings/{tenant_id}` (`agent_worker.graphstage.SETTINGS_COLLECTION`,
and `tests/unit/worker/test_graphstage.py` seeds that path). Until that one
constant moves to `workspace_settings`, a workspace's `context` and
`context_ab` written here do not reach staging; a repository's override
does, because it is read from the registration. The move is outside
CTXQ-SET's territory and is a follow-up.

Because the shape was decided this way -- a field of an existing document,
not a new collection -- it needs no separate issue.

**What the console offers (CTXQ-UI).** Settings › Workspace
(`/settings/workspace`) shows the Jev switch, off by default, with what is
sent when it is on (§5.4), what never is, and where it goes: the workspace's
judgment backend, by name (Settings › API keys). Only the workspace's owner or
a platform admin sees it enabled. A repository's Settings tab offers `context`
as Inherit, On or Off, and says local context sends nothing off the platform.
The client calls are all in `apps/swarm-ui/src/api.ts` and follow the API's
shape (`swarm_api/codeintel.py` `view`): `GET /v1/code-intelligence` (the
selected workspace; the same answer as the path form `GET
/v1/workspaces/{tenant_id}/code-intelligence`: `tenant_id`, `can_edit`, `jev`,
`context`, `context_ab`, `defaults`, `updated_at`, `updated_by`,
`repositories`), `PUT /v1/workspaces/{tenant_id}/code-intelligence` with
`{jev: true | false}`, and `PATCH /v1/repositories/{repo_id}` with
`{context: on | off | inherit}`. **One implementation serves these routes.**
CTXQ-UI first shipped a minimal self-contained backend
(a nested `{code_intelligence: {...}}` body);
CTXQ-SET's designed version replaced it and the client was moved to CTXQ-SET's
flat shape, so two routers never register the same path.


---

## 5. Jev integration

### 5.1 Where Jev runs: the worker process, never the agent, never swarm-api

* **Every Jev call is made by the worker process** (`agent_worker`), with the
  key resolved by #1297's `jevkey` order: the submitter's confirmed personal
  key, else the tenant's key, else none.
* The key file stays in `<attempt>/private/`. **The agent's process tree never
  holds the key**, and it is never put in the agent's environment.
* Calls happen at three moments: staging (before the agent exists), the gate
  (after the agent has exited its turns), and the planner's and reviewer's
  post-step.
* **Not in swarm-api.** swarm-api only *writes* tenant keys today
  (docs/provider-keys.md), so reading them would add a `secretAccessor` grant
  to the control plane; and the worker already holds the checkout, so nothing
  new leaves the tenant's boundary.
* **No Jev MCP tool is given to the agent in this design.** That is option
  §8 Q6.

**Owner override, 2026-10-14 (built by lane CTXQ-J, `agent_worker/judge/`).**
The owner chose Q6 B as well as the worker's fixed-point calls: the agent also
gets `jev_decide(state, questions)`, an MCP tool served by `swarm-jev`
(`agent_worker.judge.mcp`).
* **When it exists.** `judge.register.register_jev_tool` adds it only when the
  workspace opted in to Jev for code and the repository's override is not
  `off`. Lane CTXQ-JW wires that function into the runners.
* **Same rules as the worker's calls.** It uses the same `JudgeClient`, so
  §5.4's redaction, §5.5's retries and breaker, and one per-agent ledger
  (`judge.budget.Ledger`, a locked file in the attempt directory) apply to
  both. Its calls are audited as kind `agent` and count against the same
  8 calls and 120k tokens.
* **The backend** comes from the workspace's judgment-backend setting
  (JUDGE-SELECT):
  * **Internal Jev** (the default) and **Internal Laya** are the internal
    decision API over the tailnet. No credential exists anywhere in the
    agent's process tree. `state` is sent JSON-stringified, because the API
    takes a string.
  * **TypeSafe**: the key is written to `<attempt>/private/jev-key`
    (mode 0600) for the tool process. **That process is the agent's child,
    so the agent can read the key.** The owner accepted this residual. The
    key is never put in the config, the MCP entry, an environment variable, a
    log or an audit row.
* **The ledger is a cost control, not a boundary.** The tool process runs
  as the agent's user, so a hostile agent can rewrite the ledger. The
  backend's own records are the authority on spend.

### 5.2 Call shapes

All calls are `POST /v1/systemone`, model `jev-latest`, as `jev.mjs` sends
them. `state` is JSON, and every string in it has passed §5.4.

| ID | When | `state` sent | `questions` | Used for |
|---|---|---|---|---|
| **J0** classify | intake, only when the role is unknown | `{task: {brief ≤ 4,000 chars}}` | `type: choice` over `implement, review, fix, test, design, plan` | pick the pipeline; below 0.6 confidence, keep `implement` |
| **J1** rerank | staging | `{task: {type, brief ≤ 4,000 chars}, candidates: [{i, kind, path, symbol, signature, excerpt ≤ 600 chars, why}] ≤ 60}` | one `noul` per candidate, `c<i>`: "Would an engineer doing `task` need to read candidate i to get it right?" | §4.3 final order |
| **J2** plan coverage | after the planner writes `plan.json` | `{requirements: [...], steps: [{step_id, title, files, tests, prompt ≤ 1,500 chars}]}` | one `choice` per requirement, over the `step_id`s plus `none` | flag a requirement whose best step has p < 0.5, or that picks `none`; shown on the plan approval screen and in `swarm_runs` |
| **J3** finding triage | after the reviewer writes `verdict.json` | `{finding: {summary, file, line}, code: {path, window ±40 lines}, diff_hunk}` per finding | `noul`: "Is this finding a real defect in this diff, as stated?" | annotate each finding `p_real`; findings under 0.15 reach the fixer marked "verify before acting", never dropped |
| **J4** test gap | gate, after the agent | `{changed: [{symbol, hunk ≤ 1,200 chars}], tests: [{i, path, name, excerpt ≤ 800 chars}]}` | one `choice` per changed symbol, over the test `i`s plus `none` | PR-body list of changed behaviours with no test that would fail |
| **J5** duplicate | planner post-step, sweep runs | `{issue: {title, body ≤ 3,000 chars}, in_flight: [{i, title, files}] ≤ 40}` | `choice` over in-flight `i`s plus `none` | p ≥ 0.85 on a lane: hold the auto-approved plan for a human (§8 Q5) |

### 5.3 Cost, latency and the budget

**Measured** (jev-browser RESULTS.md, 2026-09-17, 202 to 211 calls per run):
about 290 ms and 2,850 input tokens per call on average; 0.7 to 1.5 s for a
ranking call over many blocks; a hard cap of 32,768 input tokens per request.

**Estimated per step from §5.2's shapes:**

| Step | Calls | Jev input tokens | Added wall time |
|---|---|---|---|
| implement | J1 ×1-2 | 15k-30k | ≤ 3 s |
| review | J3 ×(findings, mostly 0) + J4 ×1 | ≤ 20k | ≤ 3 s |
| plan | J2 ×1, J5 ×1 | ≤ 15k | ≤ 2 s |
| **budget per agent** (hard) | **≤ 8 calls** | **≤ 120k tokens** | — |

A lane runs for minutes to hours, so the added wall time is noise.
**The TypeSafe per-token price is not in this repository:**
docs/provider-keys.md stores keys but records no rate card. The budget is
therefore set in calls and tokens, which the worker counts exactly from
`usage.input_tokens`. Converting it to dollars is §8 Q4's input. Spend is
recorded per attempt as `jev_calls` and `jev_input_tokens` in
`context-pack.json`, which the cost-report schedule type can sum once a rate
exists.

### 5.4 What is sent, and what never is, even when opted in

Applied in this order. Every step is code, and each is tested with fakes
built at runtime.

1. **Path deny.** No content is read from:
   * `.env*`, `*.pem`, `*.key`, `*.p12`, `*.tfvars`, `*.tfstate*`;
   * anything under a `secrets/` or `credentials/` directory;
   * the repository's `hard_stop_paths`.

   Only the path name may be sent.
2. **Credential scan, then drop.** Every excerpt goes through the publish
   scan's detector (`agent_worker.publish_scan`, which wraps the worker's
   `_DiffLeakScanner` and `_credential_in`). A hit **drops the excerpt
   entirely**. The candidate is sent as path and symbol only. Masking is not
   used, because a masked key's neighbourhood can still identify it.
3. **Redaction rules.** Every string goes through `swarm_redaction.rules.redact`
   (`RULES`: private keys, OpenAI, Google API and OAuth, JWT, GitHub tokens and
   PATs, Slack, AWS access keys, HTTP authorization). These are the same rules
   `redact()` in `scripts/lib/common.sh` mirrors, held in step by
   `tests/fixtures/redaction-parity.json`.
4. **The agent's registered secrets** -- the values `agent_worker.redact.scrub_text`
   scrubs -- are removed. The names are the agent profile's `RunnerProfile.secrets`,
   which the lifecycle hands to `graphstage.prepare`, which hands them to
   `judge.attempt.setup` and into the attempt's Jev config (names only, never
   a value); the runner's client reads the values from its own environment.
   Stricter than scrubbing: a string holding a registered value is **dropped
   whole**, as in step 2, not sent with the value masked. This step is the
   one that catches a profile secret that does not look like a credential,
   which steps 2 and 3 would pass.
5. **What is never sent:**
   * issue or PR author identities and emails;
   * another repository's content;
   * anything from the in-flight view beyond titles and file paths;
   * the agent's transcript;
   * environment variables.
6. **Audit.** Each call -- sent, failed, malformed or skipped -- appends one
   row `{call_id, kind (J0-J5, or agent for the jev_decide tool), backend,
   bytes, input_tokens, ms, outcome}` to the attempt's ledger,
   `<attempt>/judge/ledger.json`. `backend` is the backend's name
   (`internal-jev`, `internal-laya` or `typesafe`), never its URL or key. The
   payload, the questions, the answer and the key are never recorded.
   `context-pack.json` carries only J1's totals, `jev_calls` and
   `jev_input_tokens`, and **not** the per-call rows, on purpose: §5.6 keeps
   the pack byte-identical to the local-only run when Jev fails, and a pack
   that held a row for every failed call would differ from it.

Each rule's enforcing code and the test that fails without it: [docs/jev-data-flow.md](../jev-data-flow.md).

### 5.5 Failure modes: never block

| Failure | Behaviour |
|---|---|
| No key, workspace not opted in, or repository override `off` | No call. The pack's header says `jev: off (reason)` |
| Timeout (10 s per call, against jev.mjs's 60 s default), 429 or 5xx | Up to 2 retries with 0.8 s × n backoff, as `jev.mjs` does. Then that call's candidates keep their local rank |
| Two consecutive failures in one attempt | A circuit breaker: no further Jev calls in this attempt, and `jev_unavailable` is logged once |
| Over the per-agent budget | Remaining calls are skipped and the pack says so |
| Malformed answer | Treated as a failure. No partial probabilities are used |

**In every case the attempt proceeds.** A Jev problem can make a pack less well
ordered; it cannot fail, park or delay an agent beyond the per-call timeout.

### 5.6 What lane CTXQ-JW built: J1-J5 in the pipeline

**Built 2026-10-15 (owner decision ~04:30 PDT: "Build CTXQ-JW now").** Jev
was unreachable that day (the internal-jev backend has no tailnet route), so
every piece is held by fake-Jev tests in `tests/unit/worker/test_jev_wiring.py`
and `tests/unit/control_plane/test_issue_run_jev_hold.py`, not by a live call.

**Set-up, once per attempt.** `agent_worker.judge.attempt.setup` runs inside
`graphstage.prepare`, the lifecycle's one existing setup call, so
`lifecycle.py` is unchanged. It reads `workspace_settings/{tenant}`
(`code_intelligence.jev`, and `judgment_backend` beside it) and the
repository's registration (its `jev` override, else its `context` override),
then `register.prepare` writes `<attempt>/judge/config.json` and the ledger.
The runner gets `SWARM_JUDGE_CONFIG` (a path, never a key) and
`judge.attempt.load` builds its client from it. No config, no client, no call.

* **Internal backends** need `SWARM_JUDGE_INTERNAL_URL` in the worker's
  environment. Nothing sets it yet: the scheduler sends `JEV_API_URL` to
  live-browser attempts only. Until a deployment sets it, an opted-in
  workspace reads `no_internal_url` and makes no call.
* **TypeSafe** uses only a key the worker already wrote to
  `private/jev-typesafe.key` (a live-browser attempt). Any other attempt
  reads `no_key` and makes no call. Resolving the key for every attempt is a
  lifecycle change and was left out of this lane.

**Where each J runs.** All of them run in the runner process, after the agent
or before it, and all share the attempt's one ledger:

| J | Call site | What it changes when Jev answers |
|---|---|---|
| J1 | `contextpack.prepare` → `build_pack` → `rank.rerank` | The sent rows (the top 60 local rows; in-flight and cannot-push never) score `0.5 · local/best + 0.5 · p`. A row under p 0.15 goes after every other row and fills no quota minimum: it is paged, never deleted. The header says `jev: on`, and `jev_calls` / `jev_input_tokens` count the call |
| J4 | `prepublish.Gate._advise` → `jev_test_gap` | "Advisory signals (Jev, not a gate)" in the advisory section, listing the changed symbols for which Jev chose `none`. Also `jev_test_gap` in the gate's record |
| J3 | `runners/cliagent._jev_post_steps` → `judge.poststeps.review_post_step`, for a review step | `p_real` on each finding in `verdict.json` (`verdict.annotate_findings`). Under 0.15 the finding also carries `"triage": "verify before acting"`, and `verdict.read_verdict` prefixes its text with that mark. It still reaches the fixer, because nothing is dropped. At most 5 findings are asked about |
| J2, J5 | the same post-step → `plan_post_step`, for a planner step | `plan-advisory.json` beside `plan.json`, which is never touched. It holds the requirements whose best step is under p 0.5 or `none`, and the in-flight lane J5 picked at p ≥ 0.85. swarm-api's planner post-step (`routes/runs.py::_from_planner`) reads the file. On a hold, it sets an `auto` run's `plan_approval` to `required`, so a person approves the plan (§8 Q5 A) |

**§5.4 at the call site, not only in the client.** The client's sanitizer
reduces only an object that carries a `path` or `file` key. J3's `diff_hunk`
is a top-level string, and J4's `changed` rows carry only `{symbol, hunk}`.
So `review_post_step` and `jev_test_gap` send an empty hunk themselves for
any path `is_denied_path` holds, the client's `hard_stop_paths` included.

**Advisory outputs never decide.**
* `verdict` is never read, written or reordered by any of this.
* Auto-merge, the review gate and the requirements read what they read
  before.
* The J5 hold only makes a person look.

**All or nothing, and identical when Jev fails.** Each of these makes an
output byte-identical to the local-only run (`judge=None`):
* a skipped call (breaker or budget);
* a failed call, a timeout, a 429 or a 5xx;
* a malformed answer, or one missing any question.

That covers the pack, CONTEXT.md, the advisory section, the gate's record,
`verdict.json` and the planner's artifacts. One J3 finding that fails leaves
every finding unannotated. **This differs from §5.5's "the pack says so" for
an exhausted budget:** the reason is in the attempt's ledger audit
(`<attempt>/judge/ledger.json`, `outcome: skipped:<reason>`), not in the
pack, because a pack that changed whenever Jev failed would not be the
local-only pack.

**Not built here:**
* the coverage flags on the plan approval screen and in `swarm_runs`. They
  are in the planner's `plan-advisory.json` artifact only, because showing
  them needs an `IssueRun` field and console work;
* `register_jev_tool` (the agent's `jev_decide` MCP tool) in the runners.

---

## 6. Evaluation

### 6.1 Offline replay

**Corpus.** The 150 lanes of §2, each with:
* its base commit (the PR body's `base commit:` line);
* its brief (the agent's input, read through `swarm_result` with an operator's
  credential);
* its known outcome (M1-M6).

**The 45 that needed post-agent work are the signal set. The other 105 are
controls**, there to show the pipeline does not make clean lanes worse.

**Arms.** For each lane, two SwarmCloud tasks are submitted at its base
commit with the identical brief:
* **A:** today's runner.
* **B:** the pipeline, local ranking only.
* **C** (on a subset of 40): the pipeline with Jev, on SwarmCloud's own
  repository, which is opted in.

The replay is a `swarm_workflow` spec: one implementer step and one review
step per arm. **It publishes to `replay/<lane>/<arm>` branches and opens no
PRs**, so the public repository gets no PR noise and auto-merge never sees a
replay.

**Human-free judgements**, recorded by a scoring step:

| Measure | How |
|---|---|
| **CI-equivalent pass** | The scoring container runs the full `tests/unit` suite, swarm-ui vitest and `tsc`, and `terraform fmt -check`, at the arm's head. This is what `application.yml` runs, minus integration and images |
| **Historic-red recurrence** | For the 37 red lanes, whether the exact tests that went red in history (from the `## Continuation` sections and fix commits) pass on the arm's first publish |
| **Review verdict** | MERGE or NOT_YET from the arm's own review step, and the count of findings |
| **Similarity to the merged change** | Jaccard of changed-file sets, and hunk-line overlap, against the merged PR's diff |
| **In-flight hits** | For P-class lanes, whether CONTEXT.md listed the colliding lane, and whether the agent stopped or adapted |
| **Cost and time** | Tokens, wall time and Jev calls per arm, from the ledger and `context-pack.json` |

**Sample size.**
* The test is paired, with the same brief on each arm. For a drop in red-PR
  rate from 25% to 15% with about 20% discordant pairs, McNemar at α = 0.05
  and 80% power needs about **150 pairs**: the whole corpus.
* A **40-lane pilot** goes first: 30 from the signal set across G, T, X and P,
  plus 10 controls. It checks that the harness measures what it claims before
  paying for 150.

**Cost of the eval, in lane-equivalents:**

| Stage | Agents (implementer + review + scoring) | Lane-equivalents |
|---|---|---|
| Pilot | 40 × 2 arms × 3 | about 80 lanes |
| Full run | 150 × 2 + 40 for arm C | about 340 lanes |

The ledger's measured cost per lane (`cost-report`) turns that into dollars on
the day it runs. This design does not quote a number it cannot read: the
API is not reachable from this container (§2).

### 6.2 Online A/B

**Assignment.**
* The flag is `context.ab` on the workspace setting: `off`, `ab` or `on`.
* With `ab`, staging assigns the arm from `sha256(task_id) mod 2`, records it
  in `context-pack.json` and the PR body ("Context pack: arm B, 23 rows,
  3.9k tokens"), and stages nothing on arm A.
* Assignment by task id makes the split reproducible and unbiased by who
  submits.

**Duration.** At the measured pace of about 50 lane PRs a day, two weeks
gives about 700 lanes, 350 per arm: enough to see a 10-point drop in M2.

**Scope.** SwarmCloud's own repository first, where Jev is opted in; another
workspace joins only by its own opt-in.

### 6.3 Thresholds for default-on

Measured on the online A/B, arm B against arm A:

| Gate | Threshold |
|---|---|
| M2: PRs with a red run | **at least 30% relative drop** (24.7% → 17% or better) |
| M3: first-pass application CI | at least 88% (from 81.3%) |
| M6: hand fix-up | no increase |
| M8: issue runs closed as duplicates | **0** in the window |
| Clean lanes (controls) | no more than 2 points worse on M2 |
| Cost per merged lane | at most 15% more (tokens + Jev) |
| Wall time p50 | at most 10% more |
| Jev outage drill | zero failed or parked agents with Jev forced off mid-window |
| Redaction | zero credential-scan hits in the Jev audit's sampled payloads (a test re-scans recorded `state` in a canary) |

**For Jev specifically (arm C against B):**
* J1 must beat local ranking on "pack rows the agent actually opened". The
  MCP and Read tool calls on pack paths are logged.
* J3 must have at least 0.8 precision on the findings it scores under 0.15,
  as judged against the replay's CI outcome.

Below those thresholds, Jev stays available as an opt-in and does not become
a default.

---

## 7. Phased build plan

**Rules for these lanes.** Each lane ships its tests first, red in CI, then
the change. **No two lanes in one phase share a file.** Lanes in later phases
that reopen a file run only after the earlier lane has merged.

| Lane | Phase | Builds | Territory (files edited) | Tests prove |
|---|---|---|---|---|
| **CTXQ-M** | 0 | the §2.6 metrics as a script; reads GitHub and, with credentials, swarm-api | new `scripts/quality-baseline.sh`, new `scripts/lib/quality_metrics.py`, new `tests/unit/scripts/test_quality_metrics.py` | each metric over recorded fixtures (runs, PR bodies, commit authors): cancelled runs are excluded, a main-merge commit is not a hand fix-up, a `## Continuation` is counted once |
| **CTXQ-H** | 1 | indexer layers H (history and co-change) and R (rules: docs and guards by path, plus cannot-push); the swarm-ui `tests/` node files classified as tests | `images/agent-runtime-indexer/repo-index/repo_graph_shards.py`, new `repo-index/repo_index_history.py`, new `tests/unit/worker/test_repo_index_history.py` | on a fixture repo: a guard that co-changes with refusal additions is linked to `refusals.py`; a doc naming a path is a rule row; the node tests are test edges; the shard cap holds |
| **CTXQ-S** | 1 | snapshot staging (absorbs KG5); the stored-setting decision (§4.7); `swarm-graph` registered for claude-code and codex; the A/B arm | new `agent_worker/graphstage.py`, `runners/claude_code.py`, `runners/codex.py`, `lifecycle.py` (one call in the attempt setup, beside `jevkey.prepare` once #1297 merges) | staged only for format ≥ 3 and an ancestor sha; `mcp.json` names the server only when staged; arm assignment is deterministic; a staging failure never fails the attempt |
| **CTXQ-P** | 1 | pack builder: retrieval, local ranking, quotas, CONTEXT.md and `pack.json`, the prompt line, per-type checklists | new `agent_worker/contextpack/` package, `runners/cliagent.py` | per type, the seeds produce the expected rows on a fixture snapshot; in-flight and cannot-push rows are never cut; the cap holds; stale rows are marked; the file is outside the checkout and never committed |
| **CTXQ-T** | 1 | graphmcp tools `similar_changes` and `rules_for` | `agent_worker/graphmcp/tools.py`, `graphmcp/snapshot.py` | the schema budget stays under 2k tokens; answers stay under 4 KiB with paging; zero rows is `UNKNOWN` |
| **CTXQ-G** | 1 | gate additions: graph-selected pytest, vitest, `tsc`, `terraform fmt`; test reach; self-check presence; the advisory PR section | `agent_worker/prepublish.py` | §3's T and G rows replayed as fixtures (a changed `.tsx` selects its vitest file; a changed `.tf` runs fmt); a missing tool is `skipped`, never sent to the agent as a failure |
| **CTXQ-I** | 2 | in-flight route and the `in_flight` remote tool | new `swarm_api/attemptcontext.py`, new `swarm_api/routes/attemptcontext.py`, `swarm_api/main.py` (router line), `agent_worker/graphmcp/remote.py` (new) | unproven is 403; fenced is 409; another tenant's lane is never listed; no network gives `UNKNOWN` |
| **CTXQ-PL** | 2 | planner: layer I and H in `plancontext`; a NOT_READY `duplicate` kind | `swarm_api/plancontext.py`, `swarm_api/issueruns.py` | #1094, #1136 and #1080 replayed as fixtures: the planner prompt lists the colliding lane; the 24 KiB allowance holds |
| **CTXQ-RV** | 2 | review and fix: callers and tests outside the diff, the sibling idiom, earlier fixes of the same test (H) | `swarm_api/reviewcontext.py`, `swarm_api/issueci.py` | #1181's sibling sort is listed; the second time-bomb fixer is shown the first fix |
| **CTXQ-SET** | 3 | settings API: `workspace_settings/{tenant}.code_intelligence` (owner decision 2026-10-14, §4.7), repository `context` override | new `swarm_api/codeintel.py`, new `swarm_api/routes/codeintel.py`, `swarm_api/main.py` (router line), `swarm_api/repositories.py` (`RepositoryPatch`), `swarm_api/routes/repositories.py` | off by default; only the owner or an admin sets the workspace default (a member 403, another workspace 404); `inherit` clears; an audit row is written in the same transaction, and a failed write writes neither; `judgment_backend` and `merge` are unchanged by a write |
| **CTXQ-UI** | 3 | Settings › Workspace › Code intelligence, and the repository Settings tab override | `apps/swarm-ui/src/` (new `CodeIntelligence.tsx`, `RepositoriesDetail.tsx`, `api.ts`, `paths.ts`; `App.tsx` and `Spine.tsx` for the new `/settings/workspace` page and its link), the issue forms' "Where" list and `test_issue_forms.py` | vitest: the toggle shows off by default and says what leaves the platform; only an owner or admin sees it enabled; the repository tab writes on/off/inherit; the issue-forms test follows the new nav entry |
| **CTXQ-J** | 3 | Jev client in the worker: §5.2 shapes, §5.4 redaction, §5.5 breaker, budget, audit | new `agent_worker/judge/` package | fake Jev: timeout, 429 and malformed answers fall back; a credential-shaped excerpt (built at runtime) is dropped, not masked; the denied paths send names only; no key, no call |
| **CTXQ-JW** | 4 | J1 into the pack, J2 and J5 into the planner post-step, J3 into the review post-step, J4 into the gate | `contextpack/rank.py`, `prepublish.py`, `agent_worker/verdict.py` (annotation only) | advisory outputs never change `verdict`; auto-merge ignores them; a Jev outage leaves every output identical to the local-only run |
| **CTXQ-E** | 4 | replay harness and scoring workflow | new `scripts/ctxq-replay.sh`, new `scripts/lib/ctxq_replay.py`, new `tests/unit/scripts/test_ctxq_replay.py` | branches are `replay/*` only, with no PR opened; scoring reads historic-red tests from a fixture body; the pairing is stable |

**Order.**
* Phase 0, then phase 1. The four phase-1 lanes have disjoint files; CTXQ-S
  and CTXQ-P meet only through the staged directory's layout, which this doc
  fixes.
* Phase 2 runs after CTXQ-S, CTXQ-P and CTXQ-H merge.
* Phase 3 can run beside phase 2, because its files are disjoint.
* Phase 4 runs after both.
* CTXQ-E's pilot needs only phase 1.

**Frozen-contract requests: none.** §4.7 replaces request A′ with a stored
setting. If the owner prefers the caller-visible input after all (§8 Q1,
option B), A′ is filed as written in knowledge-graph.md §7.4, and CTXQ-S waits
on it.

**Separate issues this design needs before building**, per CLAUDE.md "Issues":
* ~~the `tenant_settings` Firestore document shape (CTXQ-SET)~~ -- decided by
  the owner on 2026-10-14 instead: the setting is the `code_intelligence`
  field of the existing `workspace_settings/{tenant_id}` document (§4.7), so
  no new document shape and no issue;
* the Jev data-flow decision record (what §5.4 sends) for the security epic
  #476's review, because credentials and tenant isolation get a review.

---

## 8. Owner decisions

Recommendation first in each. They are also in this lane's `questions.json`.

**Q1. How staging is switched on.**
* **A. A stored setting.** The repository's `context` override over the
  workspace default. On by default for local context; no contract change
  (§4.7). *Recommended.*
* B. Request A′: a `repo_index` runner input in the frozen contract, as
  knowledge-graph.md §7.4 wrote it. Each caller opts in per agent.
* C. Both: the setting is the default, and A′ lets a caller turn it off.

**Q2. CONTEXT.md budget.**
* **A. 16 KiB (about 4k tokens), with MCP paging past it.** *Recommended.*
* B. 8 KiB: cheaper per turn, more MCP calls.
* C. 24 KiB, the planner's allowance: fewest misses, highest cached-token
  cost on every turn.

**Q3. Which agent types first.**
* **A. Implement and plan/sweep first** (phases 1 and 2). They hold 9 of 9 P
  rows and all G, T, X and W rows. Review follows in phase 2.
  *Recommended.*
* B. Review first: cheapest and lowest-risk, but §2.3 shows review catches
  4 of 37.
* C. All five types at once.

**Q4. Jev budget per agent.**
* **A. At most 8 calls and 120k input tokens per agent, and a per-workspace
  monthly cap the owner sets once TypeSafe's rate is known.** *Recommended.*
* B. At most 3 calls (J1 only, no judges).
* C. No per-agent cap; only the monthly cap.

**Q5. What J5 (duplicate) does on an auto-approved sweep plan.**
* **A. Hold the plan for a human when p ≥ 0.85.** Reversible, and Jev never
  decides. *Recommended.*
* B. Annotate only; the plan still runs.
* C. Off: rely on layer I in the planner's prompt.

**Q6. Whether Jev is a tool the agent can call mid-run.**
* **A. No. The worker calls Jev at fixed points, and the key never enters the
  agent's process tree.** *Recommended.*
* B. Yes, as a `jev` MCP tool, as #1297 does for the live browser. That puts
  the key one process away from the agent.

**Q7. Line coverage.**
* **A. Not now. Use M11 (graph test reach) and J4.** *Recommended.*
* B. Add `pytest --cov` on changed files to the gate (slower gate, real
  numbers).
* C. Add coverage to CI (`application.yml`, a `.github/workflows` change).

**Q8. Eval budget.**
* **A. The 40-lane pilot (about 80 lane-equivalents) now; the 150-pair run
  only if the pilot shows at least a 20% relative drop in historic-red
  recurrence.** *Recommended.*
* B. The full 150 pairs at once (about 340 lane-equivalents).
* C. No offline replay; online A/B only.

**Q9. Default-on.**
* **A. Local context on by default as soon as §6.3's thresholds pass; Jev
  stays per-workspace opt-in forever.** *Recommended.*
* B. Local context on by default right after phase 1, before the A/B.
* C. Everything stays opt-in per workspace.

---

## 9. Appendix: the measurement commands

Run 2026-10-14 against the public repository, with no credentials.

```bash
# PR heads and main, history only
git init -q m && cd m
git fetch -q --filter=blob:none https://github.com/bogdan-alexandrescu/SwarmCloud.git \
  '+refs/heads/main:refs/remotes/main' '+refs/pull/*/head:refs/pr/*'

# the last 300 closed PRs (3 pages); keep the 150 newest merged swarm/* lanes
for p in 1 2 3; do
  curl -s "https://api.github.com/repos/bogdan-alexandrescu/SwarmCloud/pulls?state=closed&per_page=100&page=$p&sort=created&direction=desc" >"closed$p.json"
done
jq -s '[add[] | select(.merged_at != null and (.head.ref | startswith("swarm/")))]
       | sort_by(.number) | reverse | .[:150]' closed*.json >lanes150.json

# pull_request runs since the window opened. The list API stops at 1,000
# results, so read it in two created-at ranges and de-duplicate by id.
#   .../actions/runs?event=pull_request&per_page=100&page=N&created=>=2026-10-11T00:00:00Z
#   .../actions/runs?event=pull_request&per_page=100&page=N&created=2026-10-11T00:00:00Z..2026-10-12T23:59:59Z

# M1-M3: join runs to PRs on head_branch; drop cancelled, skipped and null conclusions
# M4: grep 'Review verdict: \*\*NOT_YET' in each PR body
# M6: git log --no-merges refs/remotes/main..refs/pr/N, author name bogdan-alexandrescu
# M8: PR bodies with '<!-- swarmcloud-issue-run:', plus the closing comment
```

The counts were checked to be non-empty: 150 PRs in `lanes150.json`, 1,473
runs after de-duplication, 380 lane-head commits, and 37 red PRs, each listed
with its fix commit.
