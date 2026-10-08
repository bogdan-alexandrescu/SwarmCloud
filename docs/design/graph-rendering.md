# Graph rendering in the console: Graphify evaluated against today's renderer

**Status: design, lane GFY (2026-10-08), owner request of 2026-10-08. Nothing
here changes a screen.** It evaluates Graphify
(<https://github.com/Graphify-Labs/graphify>) and asks the owner the decisions
in section 7 (also filed as the lane's `questions.json`). A prototype is
committed under `apps/swarm-ui/src/proto/`. No route reaches it, `vite build`
never sees it, and the production bundle's hash is unchanged with it in the
tree (section 2.4). The knowledge-graph design (lane GNX,
`docs/design/knowledge-graph.md`) had not landed on main when this was written.
Section 5.2 says where the two meet.

Every `file:line` below was read on 2026-10-08 at `78ae3ac`, and Graphify at
`6478eb7` (its v0.9.80). Lines move. The symbol named beside each one is what
to search for when they have.

---

## 1. The short answer

**Graphify is not a graph-drawing library, so adopting it as one is not an
available option.** It is a Python CLI and AI-assistant skill (`pyproject.toml`
`name = "graphifyy"`, Apache-2.0) that *builds* a knowledge graph of a codebase
with tree-sitter, clusters it (Leiden) and answers queries over it. It writes
three files: `graph.json`, `GRAPH_REPORT.md` and `graph.html`. Its drawing is
done by three third-party renderers loaded from public CDNs:

| Graphify output | Renderer | Where |
|---|---|---|
| `graph.html`, the interactive knowledge graph | **vis-network 9.1.6**, canvas, forceAtlas2 physics, from unpkg with SRI | `graphify/exporters/html.py` `to_html`, line 632 |
| `tree.html` | d3 v7 `d3.tree()`, from d3js.org, **no SRI** | `graphify/tree_html.py` line 282 |
| `callflow.html` | Mermaid `@11` (floating major), from jsDelivr, **no SRI** | `graphify/callflow_html.py` line 1708 |
| `graph.svg` | matplotlib + `networkx.spring_layout` | `graphify/export.py` `to_svg` |

Graphify publishes no npm package. The npm name `graphify` belongs to an
unrelated 2022 "Random Graph Generator". So "render with Graphify" can only
mean "render with **vis-network**, configured the way Graphify configures it",
and that is what the prototype does.

**Recommendation, per graph:**

| Graph | Option | Why, in one line |
|---|---|---|
| Workflow DAG (`Workflows.tsx` `WorkflowGraph`) | **D: borrow the layered-layout ordering pass**, adopt nothing | A barycentric ordering pass cuts drawn crossings from 129 to 30 on a 48-step run (−77%) and from 6,741 to 1,892 at 200 steps. It costs about 2 ms and adds no dependency. A canvas renderer would lose the stage bands, the HTML step cards, keyboard access, the phone stage cards and every DOM test. |
| Repository graph (`RepoGraph.tsx`) | **D: borrow Graphify's aggregation, search and neighbour-list ideas**, adopt nothing now | At 2,000 modules both renderers draw an unreadable hairball (screens 17-18). Scale here comes from aggregation and focus, not a faster canvas. Two defects measured below (clustering by `src`, nodes piling on the canvas floor) cost more than the renderer does. |
| Both (option C) | Not recommended | vis-network is +159.2 kB gz (+33% on today's 477.8 kB), throws in jsdom, cannot read CSS variables, and Graphify's own configuration has `keyboard: false`. |

**Owner decision 2026-10-08: D for both graphs (§8).** Revisit option B,
with a WebGL renderer (Sigma.js or Cytoscape, not vis-network), when GNX's
knowledge graph must show more than about 2,000 nodes *at once*. Section 3.4 explains why that is a product question before it is a
rendering one.

## 2. Graphify, measured

### 2.1 What it is

* **Library or app.** An app: a CLI (`graphify`), an MCP server
  (`graphify/serve.py`) and a Python library (`ARCHITECTURE.md`, the pipeline
  `detect → extract → build → cluster → analyze → report → export`). It is not a
  React component and not framework-agnostic JavaScript.
* **Rendering technology.** Canvas (vis-network) for the interactive graph;
  SVG for the d3 tree and Mermaid; a raster/SVG matplotlib export.
* **Layout engines.** vis-network `forceAtlas2Based` physics with Graphify's
  constants (`gravitationalConstant -60`, `springLength 120`,
  `avoidOverlap 0.8`, 200 stabilisation iterations, then physics frozen;
  `html.py` `_html_script` lines 177-203). Nodes are seeded on a golden-angle
  spiral to stop Barnes-Hut recursing on coincident points (`html.py` line 155,
  their #3699). It has **no layered/DAG layout of its own**, and no ELK or
  dagre. `tree.html` uses d3's tidy tree. `callflow.html` lets Mermaid lay out
  `flowchart LR` diagrams.
* **Interaction (graph.html).** Zoom and pan (vis-network defaults); a search
  box with a result list; click-to-inspect with a clickable neighbour list; a
  per-community legend with checkboxes that filter; hover tooltips; edges
  hidden while dragging (`hideEdgesOnDrag: true`, line 193). **No
  collapse/expand, no minimap, no focus mode.** There is no keyboard
  navigation: `keyboard: false` (line 195).
* **Scale.** `MAX_NODES_FOR_VIZ = 5_000` (`html.py` line 14), overridable by
  environment. Above the limit, with `node_limit` set, Graphify **draws an
  aggregated community meta-graph instead**: one node per community, one edge
  per community pair, weighted by the count of cross-community edges (`to_html`,
  lines 436-487). That is the idea most worth borrowing (section 4.2). The
  claim that 5,000 nodes "stay smooth" is the README's and the constant's.
  Section 3.4 measures it: 2,000 nodes take 7.2 s to settle.
* **Styling and theming.** Hard-coded dark hex values in an inline stylesheet
  (`html.py` `_html_styles`, line 35 `background: #0f0f1a`). There is no light
  theme, no `prefers-color-scheme` and no CSS variables. Community colours are a
  fixed palette.
* **Accessibility.** One `<canvas>` with no roles and no per-node DOM. The only
  `data-role` attributes are on callflow's zoom toolbar. Graph data reaches
  the sidebar through `innerHTML` behind an `esc()` helper.
* **Bundle.** Not applicable to Graphify. Its renderer, vis-network, is
  measured in section 2.3.
* **License.** Apache-2.0 (`LICENSE`; `NOTICE` and `LICENSE-MIT` ship
  alongside). vis-network is `Apache-2.0 OR MIT`.
* **Maintenance**, read from the GitHub API on 2026-10-08:
  * created 2026-04-03, 124,714 stars, 12,042 forks, 1,573 open issues;
  * default branch `v8`;
  * releases v0.9.76 to v0.9.80 between 2026-10-04 and 2026-10-07;
  * ≥100 commits since 2026-09-08 (the API page cap).

  It moves fast and releases near-daily. **It pins vis-network 9.1.6
  (2023-03-23) while 10.1.2 (2026-08-19) is current.**
* **Security.**
  * A grep of `graphify/*.py` for telemetry, analytics, posthog or sentry
    finds nothing.
  * The network calls are the semantic pass, which calls the assistant's model
    or a configured API key (README), and URL ingest through
    `security.py` `safe_fetch`.
  * The HTML outputs fetch their renderers from public CDNs. Two of them do so
    **without integrity hashes**, and one uses a floating major version.
  * The console self-hosts every asset (`main.tsx`: fonts "never ask Google
    Fonts for anything"). So any adoption means bundling the renderer, never
    linking a CDN.
* **jsdom.** Graphify has no browser tests (its 349 test files are Python).
  vis-network in jsdom **throws**: `Not implemented:
  HTMLCanvasElement.prototype.getContext (without installing the canvas npm
  package)`, measured in `src/proto/proto.test.tsx`. Making it run needs the
  native `canvas` package or a mock. Even then, a canvas has no DOM nodes for a
  test to find.

### 2.2 What it does well, worth borrowing

1. **Aggregate above a limit** instead of drawing a hairball: a
   community-level meta-graph whose edge weights are crossing-edge counts
   (`to_html`).
2. **Search with a result list, and an inspector whose neighbours are
   links.** You walk the graph by clicking names, not by finding dots.
3. **A legend that filters.** One checkbox per community, plus select-all.
4. **Freeze after settling.** Physics runs a bounded number of iterations and
   stops, so the picture never drifts while it is being read.
5. **Every edge says how it was known**: `EXTRACTED` / `INFERRED` /
   `AMBIGUOUS`. The console already does this as evidence and confidence
   (`RepoGraphData.ts` `edgeLook`), so this confirms it rather than adding to it.
6. **Graph queries the UI could draw:**
   * `affected` (a reverse BFS from a seed, `graphify/affected.py`
     `affected_nodes`), which is a blast radius;
   * `path A B`, the shortest path;
   * "god nodes", the highest-degree concepts.

   These are server-side questions. They belong to GNX's knowledge-graph API,
   and the console draws their answers (section 5).
7. **A section-level overview as a layered `flowchart LR`** (callflow.html):
   the architecture drawn as aggregated package-to-package edges, in layers
   rather than as a force cloud.

### 2.3 Capability table

Bundle sizes were measured on 2026-10-08 with esbuild 0.21.5 `--bundle --minify
--format=esm`, React external, `gzip -9`. Versions are the current releases
on npm that day. Trivy `fs --severity HIGH,CRITICAL` over a lockfile holding
all of them reported **0** findings.

| | Today: hand SVG (+ @visx 4.0.0, TimeSeries only) | Graphify's renderer: vis-network 10.1.2 | React Flow (@xyflow/react 12.12.0) + dagre (@dagrejs/dagre 3.1.1) | Cytoscape.js 3.34.3 |
|---|---|---|---|---|
| License | repository's own | Apache-2.0 OR MIT | MIT + MIT | MIT |
| Added, min / gz | 0 | 669.0 kB / **159.2 kB** | 179.9 kB / 59.3 kB + 48.2 kB / 16.8 kB | 443.9 kB / 141.5 kB |
| Drawing | HTML cards over one SVG (DAG); SVG (repo) | canvas | DOM nodes + SVG edges | canvas |
| Layered/DAG layout | `dag.ts` `layoutOf`: levels, stage bands, skip-level lanes, **no crossing reduction** | hierarchical mode with `edgeMinimization` | dagre (Sugiyama) or ELK* | dagre/ELK/klay extensions |
| Force layout | `RepoGraphData.ts` `forceLayout`, O(n²), main thread | Barnes-Hut / forceAtlas2, with stabilise-then-freeze | none built in | cose / fcose (extension) |
| Collapse / expand | stage bands (DAG); package clusters above 60 (repo) | clustering API, manual | sub-flows, manual | compound nodes; expand-collapse extension |
| Keyboard, screen reader | native buttons (DAG); `role="button"` + `tabIndex` (repo) | canvas only; keyboard pans and zooms | nodes are DOM, focusable | canvas only |
| Theme tokens (CSS vars) | yes, directly | no: colours re-read and re-applied on theme change | yes | no (stylesheet takes values) |
| Renders in jsdom (vitest) | yes (every graph test today) | **no**, throws on `getContext` (measured) | not measured here (DOM output, so testable in principle) | no (canvas) |
| Phone (390 px) | DAG: stage cards; repo: module list | fits the whole graph: unreadable (screen 15) | DOM, scrolls | fits to view |

\* ELK (elkjs 0.12.0) is **EPL-2.0 OR GPL-3.0-or-later** and 1,459.1 kB /
440.0 kB gz bundled. That is both a license question for the owner and the
largest option measured. dagre is the MIT, 16.8 kB alternative.

### 2.4 The prototype

The prototype lives in `apps/swarm-ui/src/proto/`, imported by nothing in the
app:

* `fixtures.ts`:
  * `protoWorkflow(rounds)`: a 48-step workflow at the default 3 rounds, or
    200 steps at 22, built from `types.ts` `Workflow` / `WorkflowStep` / `Task`.
    Its states are scripted for one instant and labelled as a fixture on
    screen. A step whose parents have not all succeeded has no task, so it is
    never given a state.
  * `protoGraphBody(n)`: the raw `GET /v1/repositories/{id}/graph` body for
    `n` modules in six packages, seeded, so every picture is the same.
* `layered.ts`, option D:
  * `barycentricOrder`, the ordering pass;
  * `orderCrossings`;
  * `drawnCrossings`, which counts crossings on `layoutOf`'s own geometry, so
    the number is what a reader sees.
* `GraphifyWorkflow.tsx`, options A and B: both graphs drawn by vis-network.
  * The repo graph uses Graphify's options verbatim and its golden-angle seed.
  * The DAG uses vis-network's hierarchical layout on `levelsOf`'s levels.
  * Colours come from the console's tokens, re-read on a theme change.
  * There is a screen-reader list beside the canvas.
* `preview.html` / `preview.tsx`: the dev-server-only page that puts today's
  renderers and the prototypes side by side. Run `VITE_LIVE=1 npx vite` and
  open
  `/src/proto/preview.html?graph=dag|repo&r=today|layered|graphify&theme=light|dark[&n=2000&flat=1]`.
  It answers the graph route from the fixture itself, so it makes no network
  call.
* `proto.test.tsx`: holds three things:
  * the ordering pass invents, drops and moves no step;
  * crossings at least halve;
  * the canvas renderer says in place that jsdom has no canvas.

  It also prints the measurements quoted here.

`vis-network` is pinned exactly (`10.1.2`) as a **devDependency** because only
the prototype imports it. `vite build` before and after: `index-BB349dhz.js`,
1,564.85 kB / 477.78 kB gz, the same hash. No byte of the prototype ships.

### 2.5 Screenshots

These were taken in headless Chromium 156 (Playwright 1.64) against the
preview page, into the lane's artifacts at `screens/`:

| # | File | What it shows |
|---|---|---|
| 1-2 | `dag-today-dark.png`, `dag-today-light.png` | Today's DAG at Auto zoom: the two wide stages (12 implementers, 13 tests) folded into bands. |
| 3-4 | `dag-today-open-{dark,light}.png` | The same, both bands opened: 129 crossings between adjacent stages. |
| 5-6 | `dag-layered-open-{dark,light}.png` | **Option D**: today's renderer, unchanged, drawing the barycentric order. 30 crossings. |
| 7-8 | `dag-graphify-{dark,light}.png` | **Option A**: vis-network hierarchical, fitted. No bands, no figures; 7-px labels at 1440 px; text overruns its boxes because the canvas measured it before DM Mono loaded. |
| 9-10 | `repo-today-{dark,light}.png` | Today's Modules view, 48 modules: two overlapping package rectangles (`src`, `lib`) and a row of nodes pinned against the canvas floor. |
| 11-12 | `repo-graphify-{dark,light}.png` | **Option B**: Graphify's physics on the same 48 modules. Labels unreadable at fit. |
| 13 | `dag-today-390-dark.png` | 390 px: today's phone stage cards, two steps named per stage. |
| 14 | `repo-today-390-dark.png` | 390 px: today's module list, by hot-spot count. |
| 15 | `dag-graphify-390-dark.png` | 390 px: vis-network fits 48 boxes into the width, with no legible text. |
| 16 | `repo-graphify-390-dark.png` | 390 px: the same for the repo graph. |
| 17 | `repo-today-2000-flat-dark.png` | Today's canvas, 2,000 modules unclustered, after 9.0 s. |
| 18 | `repo-graphify-2000-flat-dark.png` | vis-network, 2,000 modules, settled after 7.2 s. |

## 3. Our graphs, first-hand

### 3.1 Where the console draws a graph

| Surface | File and symbol | How it is drawn |
|---|---|---|
| Workflow DAG | `Workflows.tsx` `WorkflowGraph` (line 2652, private, rendered by the exported `WorkflowCard` at 1238); layout `dag.ts` `layoutOf` (1980) | HTML step cards (`button.node`, `aria-pressed`) absolutely placed over one `<svg class="wf-edges" aria-hidden>`. Two passes: halos, then strokes. Levels run top to bottom from `levelsOf` (51). A stage too wide for the column folds into a band (`StageBand` 3179, `role="group"`, `aria-expanded`). Semantic zoom tiers (figures / details / names), not scaling. A horizontal minimap. Below 560 px, `WfPhoneStages` (3341). |
| Workflow table / timeline | `WorkflowViews.tsx` | Rows and lanes, not node-and-edge. |
| Workflow composer | `SubmitWorkflow.tsx` | **No DAG preview.** Stages as `.wfb-stage` blocks (449-450, 564-599) with a fixed 22×26 down-arrow between them (567-572). |
| Repo modules | `RepoGraph.tsx` `ModuleCanvas`; `RepoGraphData.ts` `forceLayout` (424), `graphView` (260) | Hand SVG. A deterministic O(n²) force layout, 160 iterations, seeded by an id hash. Package rectangles. Folds to packages above `COLLAPSE_AT = 60` (220). Zoom −/+/Fit and drag-pan, no wheel. `role="button"` + `tabIndex=0` nodes. At ≤640 px, a module list. |
| Repo call graph | `RepoGraph.tsx` `CallDrawing` (732); `RepoGraphData.ts` `callColumns` (630) | Columns: callers left, callees right, depth 1-6. Bézier edges styled by evidence. |
| Impact | `RepoImpact.tsx` `ImpactPlanView` (147) | **Lists**, not a graph: diff → changed symbols → affected callers → tests, in four columns. |
| Test map | `RepoTestMap.tsx` | Tables. |
| Spine, Runs, Ledger | `Spine.tsx` icons; `Runs.tsx` `MarkGlyph`; `Ledger.tsx` hand-SVG strips and sparklines | Charts and glyphs, not graphs. Out of scope here; charts follow `charts/parts.tsx`. |

### 3.2 Pain points, with evidence

**Workflow DAG**

1. **Edges cross because a level is drawn in listing order.** `layoutOf`
   places a level's steps in the order the workflow lists them. It reorders
   only when every step of a level has its own distinct parent
   (`oneToOneParents`, `dag.ts` 2089-2120). Composers add steps round by round,
   so a 4 → 12 fan-out braids:
   * the 48-step fixture draws **129** crossings between adjacent stages with
     both bands open (`drawnCrossings`, measured on `layoutOf`'s geometry);
   * at 200 steps it draws **6,741**.

   The halo (`styles.css` 1636-1641) softens crossings; it does not remove
   them. Screens 3-4.
2. **Deep runs are tall.** A 30-step run measured 1776×4134 px, about 4.6
   screens (`dag.ts` 1409-1415). The 48-step fixture with both bands open is
   4,740 px tall at 1440 wide. The minimap tracks horizontal scroll only
   (`Workflows.tsx` 2494).
3. **One long id widens every card.** `nodeWidthAt` sizes all nodes to the
   longest `step_id` / `runner_profile` (`dag.ts` 569-587).
4. **No critical path.** `critical` appears nowhere in `dag.ts`,
   `Workflows.tsx` or `WorkflowViews.tsx`.
5. **The composer draws no graph.** A reader composing a 20-step workflow sees
   stacked stage blocks, not the dependencies they are creating
   (`SubmitWorkflow.tsx` 564-599).
6. **Render cost is fine at the cap.** `WorkflowCard` first render in jsdom:
   * 185 ms for 48 steps, every stage open;
   * 450 ms for 200 steps.

   The server caps a workflow at 50 steps
   (`apps/common/swarm_common/config.py` `max_workflow_steps: int = 50`),
   so performance is not the DAG's problem; layout quality is.

**Repository graph**

7. **A `src/` repository clusters into one package.** `packageOf`
   (`RepoGraphData.ts` 162-166) takes the first path segment unless it is one
   of `MONO_ROOTS` (`apps`, `packages`, `services`, `libs`, `cmd`, `internal`,
   `modules`, line 161). `src` and `lib` are not in that list. So a
   conventional `src/api`, `src/core`, … layout clusters as `src` and `lib`.
   At 200 modules the folded view draws **2 nodes** (measured), which is
   useless, and the package rectangles overlap (screens 9-10).
8. **Nodes pile on the canvas floor.** `forceLayout` clamps positions into a
   640×440 box. With 48 modules in two clusters, a row of nodes sits pinned
   along the bottom edge (screen 9).
9. **The layout is O(n²) on the main thread.**

   | Modules | Pure layout (Node, `proto.test.tsx`) | Unfolded draw in Chromium, click to paint |
   |---|---|---|
   | 48 | 6 ms | |
   | 200 | 39 ms | |
   | 500 | 254 ms | 798 ms |
   | 2,000 | 3,399 ms | 9,011 ms |

   The page is frozen while it computes.
10. **Labels are leaves.** `leafOf` shows `orders` for `src/api/orders.py`,
    `src/core/orders.py` and `src/db/orders.py` alike (screen 9).
11. **No neighbour list or filter.** The inspector says "called from 1 module ·
    calls 2 modules" but does not list them as links. The legend does not
    filter.

### 3.3 What a canvas renderer costs here, concretely

These are the costs the prototype hit, not ones predicted:

* **Theme.** A canvas cannot use `var(--…)`. `GraphifyWorkflow.tsx`
  `readTokens` re-reads 14 tokens and rebuilds the network whenever
  `<html data-theme>` or the OS scheme changes. The console's SVG and HTML
  graphs get that for free.
* **Fonts.** vis-network measures label text when it builds. If DM Mono has
  not loaded, boxes are sized for the fallback font and the text overruns them
  (screens 7-8).
* **Tests.** Every existing graph test finds nodes in the DOM
  (`.node-slot`, `.rg-node[data-id=…]`). A canvas has none, and in jsdom it
  throws before drawing.
* **Accessibility.** The canvas gets one `role="img"` name. Each node's name,
  state and pick action must be rebuilt as a parallel DOM list (`.gfy-sr` in the
  prototype). Today's renderer *is* that DOM.
* **Phone.** `docs/web-ui/design-system.md` §7.2 says the DAG "scrolls
  horizontally … and is **not** scaled to fit". A canvas fitted to 390 px is
  exactly what the rule forbids (screens 15-16).

### 3.4 Scale: the renderer is not the bottleneck past ~500 nodes

vis-network with Graphify's options, in headless Chromium in this container:

| Modules | Constructed | Physics settled |
|---|---|---|
| 500 | 48 ms | 1,289 ms |
| 2,000 | 144 ms | 7,246 ms |

That is faster than today's 9,011 ms at 2,000, and both pictures are
unreadable (screens 17-18). Graphify's own answer at that size is not to draw
it: past its node limit it draws the community meta-graph. The repository index
can hold thousands of modules and tens of thousands of symbols (the mock-up's
footer reads 31,240 symbols and 148,900 edges). So the explorer must start
aggregated and open by focus (a package, a neighbourhood, a path, a blast
radius), whatever draws it. That is a product decision GNX's design should
make. A renderer choice cannot make it.

## 4. Options

### 4.1 Workflow DAG

| Option | What it means | Verdict |
|---|---|---|
| A. Adopt Graphify | Render with vis-network hierarchical (screens 7-8) | **No.** It loses stage bands, figures per step, semantic zoom, native-button access, phone cards and DOM tests, for +159 kB gz. Its `edgeMinimization` still leaves the security-scan's twelve edges crossing the row. |
| D. Borrow the layout step | Add the barycentric ordering pass to `layoutOf`, before x is assigned (screens 5-6) | **Chosen (owner decision 2026-10-08).** 129 → 30 crossings at 48 steps. 2 ms. No dependency. The renderer, bands, zoom and phone view are untouched. |
| E. Don't adopt, change nothing | Keep listing order | Leaves the braid. |

The ordering pass is not Graphify's: it is the Sugiyama step every layered
engine (dagre, ELK, vis-network's hierarchical mode) runs, and Graphify does not
have it. It is written out in `src/proto/layered.ts` `barycentricOrder`:
four down-and-up sweeps keeping the best order. It is proven to keep every step
on its own level (`proto.test.tsx`).

### 4.2 Repository knowledge-graph explorer

| Option | What it means | Verdict |
|---|---|---|
| B. Adopt Graphify | vis-network with Graphify's physics (screens 11-12, 18) | **Not now.** It is faster than today at 2,000 nodes and still unreadable there. It is canvas-only, has no light theme of its own, throws in jsdom, and adds +159 kB gz. If GNX needs thousands of nodes drawn at once, evaluate a WebGL renderer (sigma.js) or Cytoscape at that point, behind a lazily loaded chunk. |
| D. Borrow Graphify's ideas | Aggregate above a limit; cluster on real structure; search plus a neighbour list; a filtering legend; the layout off the main thread | **Chosen (owner decision 2026-10-08).** It fixes defects 7-11. Every piece is a change to `RepoGraph*.tsx` and needs no dependency. |
| E. Don't adopt, change nothing | | Leaves a one-node clustered view for `src/` repositories. |

### 4.3 Both (C)

Not recommended. The two graphs have different needs: a layered, stateful,
≤50-node DAG versus a large, clustered, exploratory graph. A canvas suits
neither as well as what they have.

## 5. Every place that draws a graph, or could, and what it gains

### 5.1 Workflows

| Place | Improvement | Data it reads (never invented) |
|---|---|---|
| `WorkflowGraph` | **Crossing reduction** (option D). | `depends_on` only. |
| `WorkflowGraph` | **Critical-path highlight**: the chain of steps whose measured durations sum longest, drawn as a heavier edge stroke plus a "critical path · 41m" caption. | `dag.ts` `stepDuration`. A step with no measured duration breaks the chain: the caption then says "critical path not measured" with the Mark primitive (`primitives.tsx` `Mark kind="absent"`). It never guesses a duration. |
| `WorkflowGraph` | **Collapse a stage.** Exists for wide stages. Extend it to any stage whose steps all succeeded, so a finished prefix folds to one band. | `stageCensus`. |
| `WorkflowGraph` | **Live state overlay.** Exists (1 Hz). Add a ring on the stage band that holds the most recently changed step. | Task `updated_at`. |
| `WorkflowViews.tsx` timeline | **Plan vs actual.** Draw each step's declared stage beside the moment it actually started, so a step held back by capacity (invariant 1: `QUEUED` and `PARKED` cost nothing) reads as waiting, not as late work. | `started_at`, `stepDuration` kinds. A step with no `started_at` is drawn as not started, never at zero. |
| `WorkflowGraph` | **Vertical minimap**, for runs over one screen tall (pain point 2). | Layout only. |
| `SubmitWorkflow.tsx` | **DAG preview while composing**, through the same `layoutOf` at the names tier, with no state marks: a draft step has no task, and drawing one as "queued" would be a fake state. | The draft's `depends_on`. |

### 5.2 Repository and knowledge graph

| Place | Improvement | Data it reads |
|---|---|---|
| Modules view | **Cluster on real structure**: treat `src`/`lib` as roots like `apps`, or better, take a community id served by the indexer (Graphify's Leiden step) when GNX serves one. | `GET …/graph`. |
| Modules view | **Aggregated first view** above the limit: packages or communities, with edge weight = cross-cluster edge count (Graphify's meta-graph). Open by click. | Same. |
| Modules view | **Neighbour list and search**: inspector neighbours as links, a filtering legend, and the existing symbol search kept. | Same. |
| Modules view | **Layout off the main thread** (a Web Worker running today's `forceLayout`, or Barnes-Hut at O(n log n)), and a soft boundary in place of the clamp. | Layout only. |
| Impact (`RepoImpact.tsx`) | **Blast-radius view**: today's four lists drawn as a layered graph with the edges between them, so "why is this test selected" is a visible path. The lists stay the phone view and the accessible form. | `ImpactPlan.changed / affected / tests` with each row's `evidence` and `depth`. A list the route cut (`lists_cut`, `node_cap_hit`) says "N more not drawn" and draws no stand-in node. |
| Test map (`RepoTestMap.tsx`) | **Test-impact view**: symbol → tests as a two-column graph, edge style by confidence. | `SymbolTests`. |
| Call graph | **Path between two symbols** (Graphify's `path A B`), once GNX serves it. | A GNX route. |

## 6. Constraints any lane must honour

* **Theme tokens.** Colours by role from `styles.css` (`--bg`, `--surface`,
  `--line`, `--text*`, `--s-live/park/bad/warn/neu`, `--sk-ac`) in both
  themes. Selection is a surface step plus the accent (design-system §1.3).
  No hex in a component.
* **Phone widths.** At ≤560 px the DAG keeps the stage cards and the repo graph
  keeps the list (§7.1, §7.2). Nothing is scaled to fit. Touch targets are
  44 px.
* **Keyboard and screen reader.** Every node a reader can act on is a focusable
  element with an accessible name and its state in words. Colour is never the
  only channel (WCAG 1.4.1). `prefers-reduced-motion` stops any animation.
* **Real-zero marks and no fake data.** A measured zero is drawn with the
  `Mark` primitive (`primitives.tsx` 51-82, `kind="zero"`). An absent value is
  `kind="absent"`, never a zero. A graph never invents a node, an edge or a
  state:
  * the ordering pass reorders and does nothing else;
  * the composer preview shows no state;
  * a cut list says how much it cut.
* **Dependencies.** Any new npm dependency is pinned to an exact version and
  must pass the release's trivy scan, which refuses HIGH and CRITICAL. The
  prototype's `vis-network 10.1.2` and its peers scanned clean on 2026-10-08.
  None of the recommended lanes adds one.
* **vitest in jsdom.** Every graph stays testable by DOM query. A renderer that
  throws in jsdom is a renderer whose graph has no tests.

## 7. Phased plan: lanes and territories

Each of `Workflows.tsx`, `RepoGraph*.tsx` and `SubmitWorkflow.tsx` belongs to
exactly one lane. Lanes that share no file can run in parallel. GR2 depends on
GR1 only through `dag.ts`'s exported API, and GR1 owns `dag.ts`.

| Lane | Territory | Delivers | Acceptance (measured in vitest/jsdom unless said) |
|---|---|---|---|
| **GR1** Workflow DAG | `dag.ts`, `Workflows.tsx`, `styles/workflows.css`, their tests | Barycentric ordering inside `layoutOf`; critical-path highlight; vertical minimap | On the 48-step fixture, `drawnCrossings` ≤ 30 (from 129). The ordering pass ≤ 5 ms at 50 steps. Every existing workflow test is green. The critical path names only steps with measured durations, else shows the `absent` Mark. Bundle +≤ 3 kB gz. No new dependency. |
| **GR2** Composer preview | `SubmitWorkflow.tsx`, its CSS and tests | A DAG preview from `layoutOf` at the names tier | Preview nodes are exactly the draft's steps, with no state mark on any. At 390 px it shows stage cards, not a scaled canvas. Bundle +≤ 2 kB gz. |
| **GR3** Repo graph | `RepoGraph.tsx`, `RepoGraphData.ts`, `styles/repograph.css`, their tests | Structural clustering; aggregated first view; neighbour list and filtering legend; layout in a Worker; soft bounds; disambiguated labels | A 200-module `src/` fixture folds to ≥ 6 clusters (from 2). The 2,000-module fixture keeps the main thread under 100 ms per task (Chromium, CI's browser job if one exists, else a recorded manual run). No node rests on the canvas boundary. Labels are unique within a view. Bundle +≤ 5 kB gz. |
| **GR4** Impact graph | `RepoImpact.tsx`, `RepoTestMap.tsx`, their tests | Blast-radius and test-impact drawn as layered graphs, lists kept for phone and screen reader | Drawn nodes = the plan's rows, exactly. A cut list shows "N more not drawn". Bundle +≤ 4 kB gz. Lands **after GNX**'s API settles. |
| GR0, only if a renderer is later adopted (see the §8 trigger; not chosen) | a lazily loaded chunk for one route | The adopted renderer behind a dynamic `import()` | Main chunk +0. Route chunk within a budget the owner sets at that point. Trivy clean. A DOM fallback list for tests and screen readers. |

**Owner decision 2026-10-08: GR1 and GR3 run IN PARALLEL, first** (they share
no file). GR2 and GR4 follow as above. The graph work's bundle budget is
**+10 kB gz in total with no new runtime dependency** (§8 question 3), so the
per-lane figures above sit inside it.

**Owner decision 2026-10-08: the prototype stays until GR1 lands, and GR1
deletes it.** When GR1 lands, `src/proto/` and the `vis-network` devDependency
are deleted in the same PR.

## 8. Owner decisions

**Owner decision 2026-10-08.** The chosen option is in bold; the alternatives
stay listed.

1. The workflow DAG.
   * A. Adopt vis-network.
   * **D. Borrow the ordering pass.**
   * E. Unchanged.
2. The repo graph.
   * B. Adopt vis-network.
   * B′. A different renderer when GNX needs one.
   * **D. Borrow Graphify's ideas, now.**
   * E. Unchanged.

   **Stated trigger:** evaluate a WebGL renderer (Sigma.js or Cytoscape, **not**
   vis-network) when the knowledge graph must show more than about 2,000 nodes
   at once. Until then nothing is adopted (§3.4).
3. The bundle budget for graph work.
   * **+10 kB gz, no new runtime dependency.**
   * +80 kB gz (React Flow + dagre).
   * A lazily loaded route chunk ≤ 160 kB gz.
4. Which screens go first.
   * **GR1 (DAG) and GR3 (repo graph) IN PARALLEL, because their files are
     disjoint.**
   * GR2 (composer) or GR4 (impact) first.
5. The prototype.
   * **It stays until GR1 lands, and GR1 deletes it.**
   * Delete it now.

## 9. What was not verified

* React Flow and Cytoscape were **sized and trivy-scanned only**, not
  prototyped. Their jsdom behaviour is from their documentation.
* Timings come from headless Chromium in a SwarmCloud container. A laptop will
  differ. The ratios are what to compare.
* Graphify's 5,000-node visual limit is its constant and README. It was not
  drawn at 5,000 here.
* The light-theme screenshots of the vis-network prototype use the console's
  tokens. Graphify itself has no light theme.
